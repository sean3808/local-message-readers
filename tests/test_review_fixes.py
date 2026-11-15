import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import settings_args

from readers.teams.normalizer import (
    normalize_teams_export_message,
    normalize_teams_message,
)


def run_backup_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings_dest: str | None, *args: str):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    log = tmp_path / "backup-log.jsonl"
    with sqlite3.connect(tmp_path / "archive.db"):
        pass
    result = subprocess.run([sys.executable, "-m", "readers.backup", *settings_args(tmp_path, backup_dest=settings_dest), "--archive-path", str(tmp_path / "archive.db"),
                             "--log-path", str(log), *args], capture_output=True, text=True, encoding="utf-8", check=False)
    return result, json.loads(log.read_text(encoding="utf-8").splitlines()[-1])


def test_backup_settings_destination_is_used(tmp_path, monkeypatch):
    dest = tmp_path / "settings-destination"
    result, report = run_backup_cli(tmp_path, monkeypatch, str(dest))
    assert result.returncode != 0
    assert "--dest" not in result.stderr
    assert str(dest) in report["backup"]["error"]


def test_backup_cli_destination_wins_over_settings(tmp_path, monkeypatch):
    env_dest = tmp_path / "settings-destination"
    flag_dest = tmp_path / "flag-destination"
    result, report = run_backup_cli(tmp_path, monkeypatch, str(env_dest), "--dest", str(flag_dest))
    assert result.returncode != 0
    assert str(flag_dest) in report["backup"]["error"]


def test_backup_missing_destination_logs_failure(tmp_path, monkeypatch):
    result, report = run_backup_cli(tmp_path, monkeypatch, None)
    assert result.returncode != 0
    assert "--dest" in result.stderr
    assert report["backup"]["ok"] is False
    assert "--dest" in report["backup"]["error"]


def test_identity_comparison_is_case_insensitive(use_settings):
    use_settings(teams_self_id="8:EXAMPLE")
    assert normalize_teams_message({"id": "1", "creator": "8:example"}, "19:test")["is_self"] is True
    assert normalize_teams_export_message({"id": "1", "from": "8:example"}, "19:test", "8:EXAMPLE")["is_self"] is True


def test_run_backup_without_destination_logs_failure(tmp_path, capsys):
    from readers.backup import run_backup

    log = tmp_path / "log.jsonl"
    report, code = run_backup(archive_path=tmp_path / "archive.db", log_path=log)
    assert code != 0
    assert report["backup"]["ok"] is False
    assert "--dest" in json.loads(log.read_text(encoding="utf-8"))["backup"]["error"]
    assert "--dest" in capsys.readouterr().err
