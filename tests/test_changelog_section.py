"""Release notes come from CHANGELOG.md; a version without notes must stop the release."""

import subprocess
import sys
from pathlib import Path

from scripts.changelog_section import section

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "changelog_section.py"

CHANGELOG = """# Changelog

## [0.2.0] - 2026-11-19

### New

- second

## [0.1.0] - 2026-11-16

- first
"""


def test_section_stops_at_the_next_version() -> None:
    assert section(CHANGELOG, "0.2.0") == "### New\n\n- second"
    assert section(CHANGELOG, "0.1.0") == "- first"


def test_missing_or_empty_section_is_none_or_empty() -> None:
    assert section(CHANGELOG, "0.3.0") is None
    assert section("## [0.3.0] - 2026-12-01\n\n## [0.2.0]\n- x\n", "0.3.0") == ""


def test_version_is_matched_literally() -> None:
    """0.1.0 must not match a heading like 0x1y0."""
    assert section("## [0x1y0]\n- wrong\n", "0.1.0") is None


def test_cli_exits_non_zero_without_notes(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    ok = subprocess.run(
        [sys.executable, str(SCRIPT), "0.2.0", "--changelog", str(changelog)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    missing = subprocess.run(
        [sys.executable, str(SCRIPT), "0.3.0", "--changelog", str(changelog)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert ok.returncode == 0 and ok.stdout.strip() == "### New\n\n- second"
    assert missing.returncode == 1 and "0.3.0" in missing.stderr


def test_repository_changelog_has_notes_for_the_project_version() -> None:
    """The version in pyproject.toml always has written notes."""
    import tomllib

    root = Path(__file__).resolve().parent.parent
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert section((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
