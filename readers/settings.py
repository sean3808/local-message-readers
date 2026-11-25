"""Locate, parse and validate the personal settings file (settings.toml).

Every entry point accepts ``--settings <file>`` and calls ``configure`` with it;
without it ``%LOCALAPPDATA%\\local-message-readers\\settings.toml`` is used. A
missing default file means "nothing configured"; a missing explicit file is an error.
"""

import os
import sys
import tomllib
from pathlib import Path

DEFAULT_SETTINGS_PATH = (
    Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"))
    / "local-message-readers"
    / "settings.toml"
)
SETTINGS_HELP = "Settings file path (default: %%LOCALAPPDATA%%\\local-message-readers\\settings.toml)"
_SCHEMA = {
    "archive_path": str,
    "profile_dir": str,
    "backup_dest": str,
    "teams_self_id": str,
    "refresh_min_interval_seconds": int,
    "commit_blocklist": str,
    "commit_blocklist_public": list[str],
    "smoke": {
        "wechat": bool,
        "teams_group_id": str,
        "teams_second_id": str,
        "teams_group_name": str,
        "teams_ambiguous_name": str,
        "teams_ambiguous_id_1": str,
        "teams_ambiguous_id_2": str,
    },
}

class SettingsError(ValueError):
    """Settings file missing, malformed, or holding an unknown key or wrong type."""


REPO_SETTINGS_WARNING_PREFIX = "Warning: ignoring repository settings file"
_active_path: Path | None = None
_warned_repo_settings: bool = False


def _checkout_root() -> Path | None:
    """Return the repository root when running from a source checkout, else None."""
    root = Path(__file__).resolve().parents[1]
    if (root / "pyproject.toml").is_file() and (root / ".git").exists():
        return root
    return None


def _warn_if_repo_settings() -> None:
    """Warn once per process when default settings ignore a repo-root settings.toml."""
    global _warned_repo_settings
    if _warned_repo_settings:
        return
    root = _checkout_root()
    if root is None:
        return
    repo_settings = root / "settings.toml"
    if not repo_settings.is_file():
        return
    _warned_repo_settings = True
    sys.stderr.write(
        f"{REPO_SETTINGS_WARNING_PREFIX} {repo_settings} (repository copy is not read); "
        f"default settings path is {DEFAULT_SETTINGS_PATH}\n"
    )


def configure(path: Path | str | None) -> None:
    """Select the settings file used by get_setting; None restores the default.

    Validates the selected file immediately, so every entry point reports a bad file up front.
    """
    global _active_path
    _active_path = Path(path) if path is not None else None
    if _active_path is None:
        _warn_if_repo_settings()
    load_settings()


def active_settings_path() -> Path | None:
    """The explicitly selected settings file, or None when the default is in use."""
    return _active_path


def load_settings(path: Path | str | None = None) -> dict:
    explicit = path is not None or _active_path is not None
    path = Path(path) if path is not None else _active_path or DEFAULT_SETTINGS_PATH
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        if not explicit:
            return {}
        raise SettingsError(f"{path}: settings file does not exist") from None
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise SettingsError(f"{path}: {exc}") from exc

    def validate(values, schema, prefix=""):
        for key, value in values.items():
            full_key = f"{prefix}.{key}" if prefix else key
            expected = schema.get(key)
            if expected is None:
                raise SettingsError(f"{path}: unknown key {full_key}")
            if isinstance(expected, dict):
                if not isinstance(value, dict):
                    raise SettingsError(f"{path}: {full_key} must be a table")
                validate(value, expected, full_key)
            elif expected == list[str]:
                if not isinstance(value, list):
                    raise SettingsError(f"{path}: {full_key} must be an array of strings")
                if any(type(item) is not str or not item.strip() or item != item.strip() for item in value):
                    raise SettingsError(
                        f"{path}: {full_key} must contain only non-empty strings without leading or trailing whitespace"
                    )
            elif type(value) is not expected:
                raise SettingsError(f"{path}: {full_key} must be {expected.__name__}")
            elif expected is int and value < 0:
                raise SettingsError(f"{path}: {full_key} must be >= 0")

    validate(data, _SCHEMA)
    return data


def get_setting(key: str, section: str | None = None):
    """Return a configured value (stripped when a string) or None when unset."""
    data = load_settings()
    if section is not None:
        data = data.get(section, {})
    value = data.get(key)
    if isinstance(value, str):
        value = value.strip()
    if isinstance(value, int):
        return value
    return value or None


def get_path_setting(key: str) -> Path | None:
    value = get_setting(key)
    return Path(value) if value else None
