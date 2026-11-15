import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REAL_SETTINGS_PATH, settings_args, write_settings

import readers.settings as settings_module
from readers.backup import default_refresh_command
from readers.settings import configure, get_path_setting, get_setting, load_settings


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "custom.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_settings_example_lists_every_known_key():
    example = Path(REAL_SETTINGS_PATH).parent / "settings.example.toml"
    text = example.read_text(encoding="utf-8")
    loaded = load_settings(example)
    top = {key for key in settings_module._SCHEMA if key != "smoke"}
    assert top <= loaded.keys()
    assert set(settings_module._SCHEMA["smoke"]) <= loaded["smoke"].keys()
    assert "settings.toml" in text


def test_reads_values_from_explicit_file(tmp_path):
    path = write(tmp_path, 'backup_dest = "X:/dest"\nteams_self_id = " 8:abc "\n[smoke]\nwechat = true\n')
    configure(path)
    assert get_path_setting("backup_dest") == Path("X:/dest")
    assert get_setting("teams_self_id") == "8:abc"
    assert get_setting("wechat", section="smoke") is True
    assert get_setting("archive_path") is None


def test_missing_default_file_means_nothing_configured():
    assert load_settings() == {}


def test_missing_explicit_file_names_the_file(tmp_path):
    missing = tmp_path / "nope.toml"
    with pytest.raises(ValueError, match="nope.toml"):
        load_settings(missing)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('unknown_key = "value"\n', "unknown_key"),
        ('backup_dst = "x"\n', "backup_dst"),
        ('backup_dest = 5\n', "backup_dest"),
        ('[smoke]\nwechat = "yes"\n', "smoke.wechat"),
        ('[smoke]\nteams_group_idd = "x"\n', "smoke.teams_group_idd"),
        ('smoke = "x"\n', "smoke"),
    ],
)
def test_invalid_content_names_file_and_key(tmp_path, text, key):
    path = write(tmp_path, text)
    with pytest.raises(ValueError) as exc:
        load_settings(path)
    assert str(path) in str(exc.value) and key in str(exc.value)


def test_malformed_toml_names_the_file(tmp_path):
    path = write(tmp_path, "backup_dest = \n")
    with pytest.raises(ValueError, match="custom.toml"):
        load_settings(path)


def test_refresh_command_carries_the_active_settings_file(tmp_path):
    assert "--settings" not in default_refresh_command(tmp_path / "a.db")
    path = write_settings(tmp_path)
    configure(path)
    command = default_refresh_command(tmp_path / "a.db")
    assert command[-2:] == ["--settings", str(path)]


def run_module(module: str, *args: str):
    return subprocess.run([sys.executable, "-m", module, *args], capture_output=True, text=True,
                          encoding="utf-8", check=False)


def test_teams_cli_reports_malformed_settings_as_json_error(tmp_path):
    bad = write(tmp_path, 'archive_pth = "x"\n')
    result = run_module("readers.teams", "--settings", str(bad), "--no-refresh", "conversations")
    assert result.returncode == 1
    error = json.loads(result.stdout)["error"]
    assert str(bad) in error and "archive_pth" in error


def test_wechat_cli_reports_malformed_settings_as_json_error(tmp_path):
    bad = write(tmp_path, 'wechat_typo = "x"\n')
    result = run_module("readers.wechat", "--settings", str(bad), "conversations")
    assert result.returncode == 1
    error = json.loads(result.stdout)["error"]
    assert str(bad) in error and "wechat_typo" in error


def test_backup_cli_reports_malformed_settings(tmp_path):
    bad = write(tmp_path, 'backup_destt = "x"\n')
    result = run_module("readers.backup", "--settings", str(bad), "--log-path", str(tmp_path / "log.jsonl"))
    assert result.returncode == 1
    assert str(bad) in result.stderr and "backup_destt" in result.stderr


def test_teams_cli_reads_archive_from_settings_and_profile_flag_wins(tmp_path):
    from readers.archive import Archive

    archive = tmp_path / "from-settings.db"
    Archive(archive)
    settings = settings_args(tmp_path, archive_path=archive, profile_dir=tmp_path / "settings-profile")
    flag_profile = tmp_path / "flag-profile"
    result = run_module("readers.teams", *settings, "--profile-dir", str(flag_profile), "conversations")
    # The empty archive plus a missing profile makes the command fail; the message names the profile used.
    assert result.returncode == 1, result.stdout + result.stderr
    error = json.loads(result.stdout)["error"]
    assert str(flag_profile) in error and "settings-profile" not in error


def test_smoke_is_skipped_without_a_settings_file_or_section(tmp_path, monkeypatch):
    import conftest

    monkeypatch.setattr(conftest, "REAL_SETTINGS_PATH", tmp_path / "absent.toml")
    assert conftest.real_smoke_settings() == {}
    present = write(tmp_path, 'backup_dest = "X:/dest"\n')
    monkeypatch.setattr(conftest, "REAL_SETTINGS_PATH", present)
    assert conftest.real_smoke_settings() == {}
