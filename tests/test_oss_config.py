import json
import subprocess
import sys
from pathlib import Path

from conftest import settings_args

from readers.archive import Archive
from readers.teams.export import import_export
from readers.teams.normalizer import normalize_teams_message


def test_cache_identity_prefers_explicit_flag_and_falls_back_to_setting(use_settings):
    use_settings(teams_self_id="8:invented")
    raw = {"id": "1", "creator": "8:invented"}
    assert normalize_teams_message(raw, "19:test")["is_self"] is True
    assert normalize_teams_message({**raw, "isSentByCurrentUser": False}, "19:test")["is_self"] is False


def test_export_identity_uses_data_before_setting(tmp_path: Path, use_settings):
    use_settings(teams_self_id="8:override")
    path = tmp_path / "export.json"
    path.write_text(json.dumps({"userId": "8:actual", "conversations": [{"id": "19:test", "MessageList": [
        {"id": "1", "from": "8:actual"}, {"id": "2", "from": "8:override"}]}]}), encoding="utf-8")
    archive = Archive(db_path=tmp_path / "archive.db")
    import_export(archive, path)
    messages = {m["id"]: m for m in archive.query_messages("teams", "19:test", limit=None)[0]}
    assert messages["1"]["is_self"] is True
    assert messages["2"]["is_self"] is False


def test_backup_cli_requires_destination(tmp_path: Path):
    settings = settings_args(tmp_path, backup_dest=None)
    result = subprocess.run([sys.executable, "-m", "readers.backup", *settings, "--log-path", str(tmp_path / "log.jsonl")],
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "--dest" in result.stderr and "settings.toml" in result.stderr


def test_export_setting_is_used_only_when_identity_is_missing(tmp_path: Path, use_settings):
    use_settings(teams_self_id="8:invented")
    path = tmp_path / "export.json"
    path.write_text(json.dumps({"conversations": [{"id": "19:test", "MessageList": [
        {"id": "1", "from": "8:invented"}, {"id": "2", "from": "8:other"}]}]}), encoding="utf-8")
    archive = Archive(db_path=tmp_path / "archive.db")
    import_export(archive, path)
    messages = {m["id"]: m for m in archive.query_messages("teams", "19:test", limit=None)[0]}
    assert messages["1"]["is_self"] is True
    assert messages["2"]["is_self"] is False


def test_unknown_cache_identity_is_false():
    assert normalize_teams_message({"id": "1", "creator": "8:invented"}, "19:test")["is_self"] is False
