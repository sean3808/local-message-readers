"""Shared test isolation: tests never read the real settings.toml.

- In-process tests: the autouse fixture points the default settings path at a file that does
  not exist, so the real repo-root settings.toml is never consulted.
- Subprocess tests: always pass ``--settings <file>`` built by ``write_settings`` /
  ``settings_args``; archive, profile and backup destination all live under ``tmp_path``.
"""

import json
import os
from pathlib import Path

import pytest

import readers.settings as settings_module

# Resolved before the autouse fixture replaces it; only the real-data smoke tests read this file.
REAL_SETTINGS_PATH = settings_module.DEFAULT_SETTINGS_PATH


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path_factory, monkeypatch):
    missing = tmp_path_factory.mktemp("no-settings") / "settings.toml"
    monkeypatch.setattr(settings_module, "DEFAULT_SETTINGS_PATH", missing)
    settings_module.configure(None)
    yield
    settings_module.configure(None)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value))


def write_settings(directory: Path, **values) -> Path:
    """Write an isolated settings file and return its path.

    Defaults point archive, profile and backup destination under ``directory``. Pass a key
    with value None to leave it out; ``smoke`` takes a dict for the [smoke] table.
    """
    directory = Path(directory)
    merged = {
        "archive_path": directory / "settings-archive.db",
        "profile_dir": directory / "settings-missing-profile",
        "backup_dest": directory / "settings-backup-dest",
    }
    merged.update(values)
    smoke = merged.pop("smoke", None)
    lines = [f"{key} = {_toml_value(value)}" for key, value in merged.items() if value is not None]
    if smoke:
        lines.append("[smoke]")
        lines += [f"{key} = {_toml_value(value)}" for key, value in smoke.items()]
    folder = directory / "settings-files"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"settings-{len(list(folder.iterdir()))}.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def use_settings(tmp_path):
    """In-process helper: write an isolated settings file and make it the active one."""

    def apply(**values) -> Path:
        path = write_settings(tmp_path, **values)
        settings_module.configure(path)
        return path

    return apply


def settings_args(directory: Path, **values) -> list[str]:
    """``["--settings", <isolated file>]`` for subprocess command lines."""
    return ["--settings", str(write_settings(directory, **values))]


def real_smoke_settings() -> dict:
    """The [smoke] table of the owner's real settings.toml; empty when absent.

    Called at import/collection time by the real-data smoke tests, before the autouse fixture
    swaps the default path. It parses the file only; nothing here touches archive or profile data.
    """
    if not REAL_SETTINGS_PATH.exists():
        return {}
    return settings_module.load_settings(REAL_SETTINGS_PATH).get("smoke", {})


def _stat_mtime_ns(path: Path) -> int | None:
    """Modification time via stat only; never opens, creates or writes the file."""
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


REAL_ARCHIVE_PATH = Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / "local-message-readers" / "archive.db"


@pytest.fixture(scope="session", autouse=True)
def real_archive_mtime_at_start():
    """Real archive.db mtime when the session began (stat only), for the guard test."""
    return REAL_ARCHIVE_PATH, _stat_mtime_ns(REAL_ARCHIVE_PATH)
