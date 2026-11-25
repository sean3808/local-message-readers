import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import settings_args, write_settings

import readers.settings as settings_module
from readers.backup import default_refresh_command
from readers.settings import configure, get_path_setting, get_setting, load_settings


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "custom.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_settings_example_lists_every_known_key():
    example = Path(__file__).resolve().parent.parent / "settings.example.toml"
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
        ('refresh_min_interval_seconds = -1\n', "refresh_min_interval_seconds"),
        ('refresh_min_interval_seconds = "120"\n', "refresh_min_interval_seconds"),
        ('refresh_min_interval_seconds = true\n', "refresh_min_interval_seconds"),
        ('refresh_min_interval_seconds = 1.5\n', "refresh_min_interval_seconds"),
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


def _run_with_localappdata(localappdata: Path, *args: str, cwd: Path | None = None, check: bool = True):
    env = {**os.environ, "LOCALAPPDATA": str(localappdata)}
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
    )


def test_default_settings_path_uses_localappdata(tmp_path):
    localappdata = tmp_path / "localappdata"
    expected = localappdata / "local-message-readers" / "settings.toml"
    expected.parent.mkdir(parents=True)
    expected.write_text('teams_self_id = "8:from-localappdata"\n', encoding="utf-8")

    result = _run_with_localappdata(
        localappdata,
        "-c",
        "from readers.settings import DEFAULT_SETTINGS_PATH, get_setting; "
        "print(DEFAULT_SETTINGS_PATH); print(get_setting('teams_self_id'))",
    )
    lines = result.stdout.strip().splitlines()
    assert Path(lines[0]) == expected
    assert lines[1] == "8:from-localappdata"


def test_missing_localappdata_settings_ignores_repo_root_and_cwd(tmp_path):
    localappdata = tmp_path / "empty-localappdata"
    localappdata.mkdir()
    (tmp_path / "settings.toml").write_text('teams_self_id = "8:from-cwd"\n', encoding="utf-8")

    result = _run_with_localappdata(
        localappdata,
        "-c",
        "import json; from readers.settings import load_settings; print(json.dumps(load_settings()))",
        cwd=tmp_path,
    )
    assert json.loads(result.stdout) == {}


@pytest.mark.parametrize(
    ("command", "stream_name"),
    [
        (["-m", "readers.teams", "--no-refresh", "conversations"], "stdout"),
        (["-m", "readers.wechat", "conversations"], "stdout"),
        (["-m", "readers.backup"], "stderr"),
        ([str(Path(__file__).resolve().parent.parent / "scripts" / "check_commit_blocklist.py")], "stderr"),
    ],
)
def test_every_entry_point_reads_default_localappdata_settings_without_flag(tmp_path, command, stream_name):
    localappdata = tmp_path / "localappdata"
    settings_file = localappdata / "local-message-readers" / "settings.toml"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text('unknown_default_probe = "x"\n', encoding="utf-8")

    result = _run_with_localappdata(localappdata, *command, cwd=tmp_path, check=False)
    assert result.returncode == 1
    output = json.loads(result.stdout)["error"] if stream_name == "stdout" else result.stderr
    assert str(settings_file) in output and "unknown_default_probe" in output


def test_refresh_min_interval_seconds_preserves_zero(tmp_path):
    assert get_setting("refresh_min_interval_seconds") is None
    zero_file = write(tmp_path, "refresh_min_interval_seconds = 0\n")
    configure(zero_file)
    assert get_setting("refresh_min_interval_seconds") == 0
    positive_file = write(tmp_path, "refresh_min_interval_seconds = 45\n")
    configure(positive_file)
    assert get_setting("refresh_min_interval_seconds") == 45


def test_repo_settings_warning_default_with_repo_file_warns_once(tmp_path, monkeypatch, capsys):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    repo_settings = checkout / "settings.toml"
    repo_settings.write_text('teams_self_id = "8:ignored"\n', encoding="utf-8")
    monkeypatch.setattr(settings_module, "_checkout_root", lambda: checkout)

    configure(None)
    load_settings()
    get_setting("teams_self_id")
    configure(None)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("Warning:") == 1
    assert str(repo_settings) in captured.err
    assert str(settings_module.DEFAULT_SETTINGS_PATH) in captured.err
    assert "not read" in captured.err.lower()


def test_repo_settings_warning_default_without_repo_file_is_silent(tmp_path, monkeypatch, capsys):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    monkeypatch.setattr(settings_module, "_checkout_root", lambda: checkout)

    configure(None)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_repo_settings_warning_suppressed_with_explicit_settings(tmp_path, monkeypatch, capsys):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / "settings.toml").write_text('teams_self_id = "8:ignored"\n', encoding="utf-8")
    monkeypatch.setattr(settings_module, "_checkout_root", lambda: checkout)

    explicit = write(tmp_path, 'teams_self_id = "8:explicit"\n')
    configure(explicit)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_repo_settings_warning_silent_outside_checkout(monkeypatch, capsys):
    monkeypatch.setattr(settings_module, "_checkout_root", lambda: None)

    configure(None)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_checkout_root_detects_git_dir_and_worktree_file(tmp_path, monkeypatch):
    import conftest

    monkeypatch.setattr(settings_module, "_checkout_root", conftest.REAL_CHECKOUT_ROOT)
    fake_pkg = tmp_path / "repo" / "readers"
    fake_pkg.mkdir(parents=True)
    fake_file = fake_pkg / "settings.py"
    fake_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings_module, "__file__", str(fake_file))

    # Neither pyproject.toml nor .git -> None (installed snapshot)
    assert settings_module._checkout_root() is None

    # pyproject.toml + .git file (worktree) -> repo root
    (tmp_path / "repo" / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "repo" / ".git").write_text("gitdir: /some/path\n", encoding="utf-8")
    assert settings_module._checkout_root() == tmp_path / "repo"


def _load_blocklist_main():
    import importlib.util

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "check_commit_blocklist.py"
    spec = importlib.util.spec_from_file_location("check_commit_blocklist", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def test_entry_points_keep_stdout_unchanged_with_repo_settings_warning(tmp_path, monkeypatch, capsys):
    from readers.archive import Archive
    from readers.backup import main as backup_main
    from readers.teams.cli import main as teams_main
    from readers.wechat import main as wechat_main

    blocklist_main = _load_blocklist_main()
    archive = tmp_path / "archive.db"
    Archive(archive)

    def run_all():
        outputs = {}

        settings_module._warned_repo_settings = False
        teams_code = teams_main(["--no-refresh", "--archive-path", str(archive), "conversations"])
        teams_cap = capsys.readouterr()
        outputs["teams"] = (teams_code, teams_cap.out, teams_cap.err)

        settings_module._warned_repo_settings = False
        wechat_code = wechat_main(["conversations", "--limit", "0"])
        wechat_cap = capsys.readouterr()
        outputs["wechat"] = (wechat_code, wechat_cap.out, wechat_cap.err)

        settings_module._warned_repo_settings = False
        with pytest.raises(SystemExit) as backup_exc:
            backup_main(["--log-path", str(tmp_path / "log.jsonl")])
        backup_cap = capsys.readouterr()
        outputs["backup"] = (backup_exc.value.code, backup_cap.out, backup_cap.err)

        settings_module._warned_repo_settings = False
        blocklist_code = blocklist_main([])
        blocklist_cap = capsys.readouterr()
        outputs["blocklist"] = (blocklist_code, blocklist_cap.out, blocklist_cap.err)

        return outputs

    monkeypatch.setattr(settings_module, "_checkout_root", lambda: None)
    baseline = run_all()

    checkout = tmp_path / "repo"
    checkout.mkdir()
    repo_settings = checkout / "settings.toml"
    repo_settings.write_text('teams_self_id = "8:ignored"\n', encoding="utf-8")
    monkeypatch.setattr(settings_module, "_checkout_root", lambda: checkout)
    with_warning = run_all()

    for name in ("teams", "wechat", "backup", "blocklist"):
        base_code, base_out, _ = baseline[name]
        warn_code, warn_out, warn_err = with_warning[name]
        assert warn_code == base_code
        assert warn_out == base_out
        assert str(repo_settings) in warn_err
        assert str(settings_module.DEFAULT_SETTINGS_PATH) in warn_err
        if name in ("teams", "wechat"):
            json.loads(warn_out)
