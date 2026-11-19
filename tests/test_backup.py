"""Tests for weekly refresh + 7-Zip encrypted archive backup (issue #17)."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import uuid
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import pytest

import readers.backup as backup_module
from readers.backup import (
    CREDENTIAL_TARGET,
    CredentialNotFoundError,
    default_refresh_command,
    encrypt_and_verify,
    find_7z_binary,
    lookup_windows_credential,
    prune_old_backups,
    read_windows_credential,
    run_backup,
    run_refresh,
)


def _has_7z() -> bool:
    try:
        find_7z_binary()
        return True
    except FileNotFoundError:
        return False


requires_7z = pytest.mark.skipif(not _has_7z(), reason="7-Zip (7z.exe) not available on system")


@pytest.fixture(autouse=True)
def fake_wechat_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    account = tmp_path / "Documents" / "xwechat_files" / "wxid_test" / "db_storage"
    account.mkdir(parents=True)
    (account / "messages.db").write_bytes(b"fake")
    for name in ("msg", "config", "business", "resource"):
        (account.parent / name).mkdir()
    key = tmp_path / ".wechat-cli" / "all_keys.json"
    key.parent.mkdir()
    key.write_text("{}", encoding="utf-8")


def _init_dummy_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE dummy (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO dummy VALUES (1, 'hello')")
        conn.commit()


def test_real_credential_manager_lookup_nonexistent() -> None:
    """Real Credential Manager lookup of a random non-existent target returns 'missing'.

    Must read only, never write credentials.
    """
    random_target = f"local-message-readers/test-nonexistent-{uuid.uuid4().hex}"
    # lookup_windows_credential returns None (indicating missing)
    result = lookup_windows_credential(random_target)
    assert result is None

    # read_windows_credential raises CredentialNotFoundError naming the target and stating missing
    with pytest.raises(CredentialNotFoundError) as exc_info:
        read_windows_credential(random_target)
    err = str(exc_info.value)
    assert random_target in err
    assert "missing" in err.lower()


def test_missing_credential(tmp_path: Path) -> None:
    """Missing credential -> exit 1, error names the target, dest has no new file."""
    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    log_file = tmp_path / "backup-log.jsonl"

    fake_refresh = [sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"]

    # Use the real Credential Manager read path, swapping in only a target that certainly does not exist (read-only, creates no credential)
    random_target = f"local-message-readers/test-nonexistent-{uuid.uuid4().hex}"

    def missing_provider() -> str:
        return read_windows_credential(random_target)

    report, exit_code = run_backup(
        archive_path=archive_file,
        dest=dest_dir,
        log_path=log_file,
        password_provider=missing_provider,
        refresh_command=fake_refresh,
    )

    assert exit_code == 1
    assert report["backup"]["ok"] is False
    assert report["backup"]["path"] is None
    assert report["backup"]["error"] is not None
    assert random_target in report["backup"]["error"]
    assert "missing" in report["backup"]["error"].lower()
    assert "docs/teams-archive-backup.md" in report["backup"]["error"]

    # Dest has no new files and no .partial files
    assert list(dest_dir.iterdir()) == []


def test_missing_7z(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing 7-Zip -> exit 1, loud error (real lookup with PATH and Program Files emptied)."""
    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    log_file = tmp_path / "backup-log.jsonl"

    fake_refresh = [sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"]

    monkeypatch.setattr(backup_module.shutil, "which", lambda _name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "no-program-files"))

    report, exit_code = run_backup(
        archive_path=archive_file,
        dest=dest_dir,
        log_path=log_file,
        password_provider=lambda: "dummy-password",
        refresh_command=fake_refresh,
    )

    assert exit_code == 1
    assert report["backup"]["ok"] is False
    assert report["backup"]["error"] is not None
    assert "7-zip" in report["backup"]["error"].lower() or "7z" in report["backup"]["error"].lower()
    assert list(dest_dir.iterdir()) == []


def test_refresh_exits_nonzero_still_backs_up(tmp_path: Path) -> None:
    """Refresh exits non-zero -> backup still written and verified, report refresh.ok=false / backup.ok=true, exit 2."""
    if not _has_7z():
        pytest.skip("7z required for full backup test")

    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    log_file = tmp_path / "backup-log.jsonl"

    failing_refresh = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('simulated refresh failure'); sys.exit(42)",
    ]

    report, exit_code = run_backup(
        archive_path=archive_file,
        dest=dest_dir,
        log_path=log_file,
        password_provider=lambda: "valid-pass-1234",
        refresh_command=failing_refresh,
    )

    assert exit_code == 2
    assert report["refresh"]["ok"] is False
    assert "42" in report["refresh"]["detail"]
    assert report["backup"]["ok"] is True
    assert report["backup"]["path"] is not None
    assert Path(report["backup"]["path"]).is_file()


def test_refresh_exit_0_with_failures(tmp_path: Path) -> None:
    """Refresh exit 0 but JSON has non-empty failures -> refresh.ok=false."""
    if not _has_7z():
        pytest.skip("7z required for full backup test")

    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    log_file = tmp_path / "backup-log.jsonl"

    failures_refresh = [
        sys.executable,
        "-c",
        "import json; print(json.dumps({'conversations': [], 'failures': [{'id': 'c1', 'error': 'corrupt'}]}))",
    ]

    report, exit_code = run_backup(
        archive_path=archive_file,
        dest=dest_dir,
        log_path=log_file,
        password_provider=lambda: "valid-pass-1234",
        refresh_command=failures_refresh,
    )

    assert exit_code == 2
    assert report["refresh"]["ok"] is False
    assert "failures" in report["refresh"]["detail"].lower()
    assert report["backup"]["ok"] is True


def test_default_refresh_passes_archive_path_after_subcommand(tmp_path: Path) -> None:
    """teams-reader drops --archive-path given before the subcommand; refresh must target the backed-up file."""
    archive = tmp_path / "custom-archive.db"
    cmd = default_refresh_command(archive)

    assert cmd.index("conversations") < cmd.index("--archive-path")
    assert cmd[cmd.index("--archive-path") + 1] == str(archive)


def test_default_refresh_forces_refresh_past_throttle(tmp_path: Path) -> None:
    """A query just before the backup must not let the throttle skip the backup's own refresh."""
    cmd = default_refresh_command(tmp_path / "a.db")

    assert "--refresh" in cmd
    assert "--no-refresh" not in cmd


def test_refresh_hang_times_out_instead_of_blocking() -> None:
    """A refresh that hangs is reported as failed after the time limit, so the backup can still run."""
    hanging_refresh = [sys.executable, "-c", "import time; time.sleep(30)"]

    ok, detail = run_refresh(Path("unused.db"), refresh_command=hanging_refresh, timeout=1)

    assert ok is False
    assert "timed out" in detail


@requires_7z
def test_consistent_copy_wal_mode_uncommitted_write(tmp_path: Path) -> None:
    """Consistent copy: archive in WAL mode with committed rows still in the -wal file (no checkpoint)

    and another connection holding an open uncommitted write -> decrypting the backup with 7z
    yields exactly one file archive.db whose rows include the committed ones and not the uncommitted ones,
    integrity ok.
    """
    archive_file = tmp_path / "archive.db"
    conn_writer = sqlite3.connect(archive_file)
    conn_writer.execute("PRAGMA journal_mode=WAL")
    conn_writer.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    conn_writer.execute("INSERT INTO messages VALUES (1, 'committed 1')")
    conn_writer.execute("INSERT INTO messages VALUES (2, 'committed 2')")
    conn_writer.commit()

    # Open uncommitted write transaction
    conn_writer.execute("INSERT INTO messages VALUES (3, 'uncommitted 3')")

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    log_file = tmp_path / "backup-log.jsonl"
    password = "secret-wal-password"

    ok_refresh = [sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"]

    try:
        report, exit_code = run_backup(
            archive_path=archive_file,
            dest=dest_dir,
            log_path=log_file,
            password_provider=lambda: password,
            refresh_command=ok_refresh,
        )
        assert exit_code == 0
        assert report["backup"]["ok"] is True
        archive_7z = Path(report["backup"]["path"])
        assert archive_7z.is_file()

        # Decrypt backup to a new folder
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        seven_zip = find_7z_binary()
        cmd = [str(seven_zip), "x", f"-p{password}", f"-o{extract_dir}", "-y", str(archive_7z)]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        assert res.returncode == 0

        # The two channels have separate folders.
        extracted_files = list(extract_dir.iterdir())
        assert {p.name for p in extracted_files} == {"teams", "wechat"}

        # Verify contents: committed rows present, uncommitted not present, integrity ok
        with sqlite3.connect(extract_dir / "teams" / "archive.db") as conn_check:
            row_integrity = conn_check.execute("PRAGMA integrity_check").fetchone()
            assert row_integrity and row_integrity[0].lower() == "ok"
            rows = conn_check.execute("SELECT id, content FROM messages ORDER BY id").fetchall()
            assert rows == [(1, "committed 1"), (2, "committed 2")]
    finally:
        conn_writer.close()


@requires_7z
def test_keep_8_prunes_oldest_keeps_unrelated(tmp_path: Path) -> None:
    """Keep 8: 9 pre-existing teams-archive-*.7z files + 1 unrelated file in dest

    -> after run exactly 8 teams-archive-*.7z remain (new one included, oldest ones removed),
    unrelated file untouched.
    """
    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    log_file = tmp_path / "backup-log.jsonl"

    # Create 9 pre-existing archives with timestamps
    # Day 01 to Day 09
    pre_existing_names = []
    for day in range(1, 10):
        name = f"teams-archive-2026110{day}-120000.7z"
        (dest_dir / name).write_text(f"dummy-archive-{day}", encoding="utf-8")
        pre_existing_names.append(name)

    unrelated_file = dest_dir / "unrelated_notes.txt"
    unrelated_file.write_text("the user notes", encoding="utf-8")

    ok_refresh = [sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"]

    report, exit_code = run_backup(
        archive_path=archive_file,
        dest=dest_dir,
        keep=8,
        log_path=log_file,
        password_provider=lambda: "dummy-pass",
        refresh_command=ok_refresh,
    )

    assert exit_code == 0
    assert report["backup"]["ok"] is True

    # Check remaining archive files
    archives_remaining = sorted(
        [f.name for f in dest_dir.iterdir() if f.name.startswith("teams-archive-") and f.name.endswith(".7z")]
    )
    assert len(archives_remaining) == 8

    # The two oldest should have been pruned: 20261101 and 20261102
    assert "teams-archive-20261101-120000.7z" not in archives_remaining
    assert "teams-archive-20261102-120000.7z" not in archives_remaining

    # Report pruned list should contain exactly those two
    assert set(report["backup"]["pruned"]) == {
        "teams-archive-20261101-120000.7z",
        "teams-archive-20261102-120000.7z",
    }

    # Unrelated file remains untouched
    assert unrelated_file.is_file()
    assert unrelated_file.read_text(encoding="utf-8") == "the user notes"


@requires_7z
def test_verification_failure_cleanup(tmp_path: Path) -> None:
    """Verification failure (corrupt file before verify via injected hook)

    -> exit 1, no pruning, no .partial left.
    """
    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    log_file = tmp_path / "backup-log.jsonl"

    # Pre-existing archive to ensure no pruning happens on failure
    pre_existing = dest_dir / "teams-archive-20261101-120000.7z"
    pre_existing.write_text("pre-existing-content", encoding="utf-8")

    ok_refresh = [sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"]

    def corrupt_hook(target_file: Path) -> None:
        # Corrupt the 7z archive file so 7z t fails
        target_file.write_bytes(b"corrupted invalid 7z bytes header")

    report, exit_code = run_backup(
        archive_path=archive_file,
        dest=dest_dir,
        log_path=log_file,
        password_provider=lambda: "dummy-pass",
        refresh_command=ok_refresh,
        _before_verify_hook=corrupt_hook,
    )

    assert exit_code == 1
    assert report["backup"]["ok"] is False
    assert report["backup"]["error"] is not None
    assert "verification" in report["backup"]["error"].lower()

    # Pre-existing file is NOT pruned
    assert pre_existing.is_file()

    # No .partial or new corrupted file left in dest
    files_in_dest = list(dest_dir.iterdir())
    assert files_in_dest == [pre_existing]


@requires_7z
def test_password_never_appears_in_stdout_or_log(tmp_path: Path) -> None:
    """The fake password string never appears in stdout or the log file."""
    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    log_file = tmp_path / "backup-log.jsonl"

    fake_password = "SUPER_SECRET_TOKEN_DO_NOT_LEAK_987654"
    ok_refresh = [sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"]

    stdout_capture = io.StringIO()
    with redirect_stdout(stdout_capture):
        report, exit_code = run_backup(
            archive_path=archive_file,
            dest=dest_dir,
            log_path=log_file,
            password_provider=lambda: fake_password,
            refresh_command=ok_refresh,
        )

    assert exit_code == 0
    stdout_text = stdout_capture.getvalue()
    log_text = log_file.read_text(encoding="utf-8")

    assert fake_password not in stdout_text
    assert fake_password not in log_text
    assert fake_password not in json.dumps(report)


def test_missing_archive_file(tmp_path: Path) -> None:
    """Missing archive file -> backup fails loudly."""
    nonexistent_archive = tmp_path / "nonexistent-archive.db"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    log_file = tmp_path / "backup-log.jsonl"

    ok_refresh = [sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"]

    report, exit_code = run_backup(
        archive_path=nonexistent_archive,
        dest=dest_dir,
        log_path=log_file,
        password_provider=lambda: "dummy-pass",
        refresh_command=ok_refresh,
    )

    assert exit_code == 1
    assert report["backup"]["ok"] is False
    assert "not found" in report["backup"]["error"].lower()


def test_missing_dest_dir_fails_loudly(tmp_path: Path) -> None:
    """Missing dest folder -> fails loudly, does not create it."""
    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    nonexistent_dest = tmp_path / "nonexistent-dest-folder"
    log_file = tmp_path / "backup-log.jsonl"

    ok_refresh = [sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"]

    report, exit_code = run_backup(
        archive_path=archive_file,
        dest=nonexistent_dest,
        log_path=log_file,
        password_provider=lambda: "dummy-pass",
        refresh_command=ok_refresh,
    )

    assert exit_code == 1
    assert report["backup"]["ok"] is False
    assert not nonexistent_dest.exists()


def test_cli_main_help() -> None:
    """Test that main(['--help']) shows help and exits with 0."""
    from readers.backup import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


@pytest.mark.parametrize("bad_keep", [0, -1])
def test_keep_below_one_is_rejected_before_any_backup(tmp_path: Path, bad_keep: int) -> None:
    """--keep 0 / negative would delete the new backup too: refuse before touching dest."""
    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    existing = dest_dir / "teams-archive-20261101-120000.7z"
    existing.write_text("a", encoding="utf-8")

    ok_refresh = [sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"]

    report, exit_code = run_backup(
        archive_path=archive_file,
        dest=dest_dir,
        keep=bad_keep,
        log_path=tmp_path / "backup-log.jsonl",
        password_provider=lambda: "dummy-pass",
        refresh_command=ok_refresh,
    )

    assert exit_code == 1
    assert "at least 1" in report["backup"]["error"]
    assert list(dest_dir.iterdir()) == [existing]


def test_prune_never_deletes_new_backup_even_with_future_named_files(tmp_path: Path) -> None:
    """Files named with a later timestamp than the new backup must not push it out."""
    dest = tmp_path / "dest"
    dest.mkdir()
    new_backup = dest / "teams-archive-20261111-120000.7z"
    new_backup.write_text("new", encoding="utf-8")
    future = [dest / f"teams-archive-2099010{d}-120000.7z" for d in range(1, 9)]
    for f in future:
        f.write_text("future", encoding="utf-8")

    pruned, errors = prune_old_backups(dest, new_backup, keep=8)

    assert new_backup.is_file()
    assert pruned == ["teams-archive-20990101-120000.7z"]
    assert errors == []
    assert len(list(dest.glob("teams-archive-*.7z"))) == 8


def test_prune_ignores_lookalike_names(tmp_path: Path) -> None:
    """Only exact teams-archive-YYYYMMDD-HHMMSS.7z files are backups; lookalikes are neither counted nor deleted."""
    dest = tmp_path / "dest"
    dest.mkdir()
    new_backup = dest / "teams-archive-20261111-120000.7z"
    new_backup.write_text("new", encoding="utf-8")
    real = [dest / f"teams-archive-2026100{d}-120000.7z" for d in range(1, 10)]
    for f in real:
        f.write_text("old", encoding="utf-8")
    lookalikes = [
        dest / "teams-archive-notes.7z",
        dest / "teams-archive-20261111-120000.7z.partial",
        dest / "teams-archive-20261111-120000 (1).7z",
    ]
    for f in lookalikes:
        f.write_text("not a backup", encoding="utf-8")

    pruned, errors = prune_old_backups(dest, new_backup, keep=8)

    assert errors == []
    # The new backup + the latest 7 real backups stay; the oldest 2 are deleted; look-alike files are left untouched
    assert set(pruned) == {"teams-archive-20261001-120000.7z", "teams-archive-20261002-120000.7z"}
    assert all(f.is_file() for f in lookalikes)
    assert new_backup.is_file()


def test_prune_delete_failure_keeps_backup_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An old copy that cannot be deleted is reported, but the verified new backup still counts as success."""
    if not _has_7z():
        pytest.skip("7z required for full backup test")

    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    stuck = dest_dir / "teams-archive-20261001-120000.7z"
    stuck.write_text("locked", encoding="utf-8")

    real_unlink = Path.unlink

    def flaky_unlink(self: Path, *args, **kwargs):
        if self.name == stuck.name:
            raise PermissionError("file is in use")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    report, exit_code = run_backup(
        archive_path=archive_file,
        dest=dest_dir,
        keep=1,
        log_path=tmp_path / "backup-log.jsonl",
        password_provider=lambda: "dummy-pass",
        refresh_command=[sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"],
    )

    assert exit_code == 0
    assert report["backup"]["ok"] is True
    assert report["backup"]["pruned"] == []
    assert len(report["backup"]["prune_errors"]) == 1
    assert stuck.name in report["backup"]["prune_errors"][0]
    assert Path(report["backup"]["path"]).is_file()


@requires_7z
def test_prune_scan_failure_keeps_verified_backup_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the folder cannot be listed during pruning, the verified backup is still reported as success."""
    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    def broken_prune(*_args, **_kwargs):
        raise PermissionError("cannot list folder")

    monkeypatch.setattr(backup_module, "prune_old_backups", broken_prune)

    report, exit_code = run_backup(
        archive_path=archive_file,
        dest=dest_dir,
        log_path=tmp_path / "backup-log.jsonl",
        password_provider=lambda: "dummy-pass",
        refresh_command=[sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"],
    )

    assert exit_code == 0
    assert report["backup"]["ok"] is True
    assert Path(report["backup"]["path"]).is_file()
    assert report["backup"]["prune_errors"] == ["prune failed: cannot list folder"]


@requires_7z
@pytest.mark.parametrize("corrupt_before_verify", [False, True])
def test_same_second_name_collision_keeps_existing_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt_before_verify: bool
) -> None:
    """A rerun inside the same second (verify passing or failing) must not touch the backup at that name."""
    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    class _FixedNow:
        @staticmethod
        def now():
            return datetime(2026, 11, 11, 12, 0, 0).astimezone()

    monkeypatch.setattr(backup_module, "datetime", _FixedNow)
    existing = dest_dir / "teams-archive-20261111-120000.7z"
    existing.write_bytes(b"the only good backup")

    def corrupt(target: Path) -> None:
        target.write_bytes(b"corrupted")

    with pytest.raises((FileExistsError, RuntimeError)):
        encrypt_and_verify(
            seven_zip=find_7z_binary(),
            source_file=archive_file,
            dest_dir=dest_dir,
            password="dummy-pass",
            _before_verify_hook=corrupt if corrupt_before_verify else None,
        )

    assert existing.read_bytes() == b"the only good backup"
    assert list(dest_dir.iterdir()) == [existing]


@pytest.mark.parametrize("bad_password", ['has"quote', "\u4e2d\u6587\u5bc6\u78bc", "tab\there", "sp ace\\", ""])
def test_password_that_cannot_round_trip_is_rejected_loudly(tmp_path: Path, bad_password: str) -> None:
    """Passwords 7-Zip rejects or that cannot be typed back at restore fail before any file is written."""
    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    report, exit_code = run_backup(
        archive_path=archive_file,
        dest=dest_dir,
        log_path=tmp_path / "backup-log.jsonl",
        password_provider=lambda: bad_password,
        refresh_command=[sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"],
    )

    assert exit_code == 1
    assert CREDENTIAL_TARGET in report["backup"]["error"]
    if bad_password:
        assert bad_password not in report["backup"]["error"]
    assert list(dest_dir.iterdir()) == []


@requires_7z
def test_backup_opens_with_password_typed_at_7zip_prompt(tmp_path: Path) -> None:
    """Restore path: the archive opens when the password is typed at 7-Zip's prompt (no argv)."""
    archive_file = tmp_path / "archive.db"
    _init_dummy_db(archive_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    # Every allowed symbol appears once, including whitespace and a backslash in the middle
    password = r"Ab1 ~!@#$%^&*()_+-=[]{}|;:',.<>/?\x\\y end"

    report, exit_code = run_backup(
        archive_path=archive_file,
        dest=dest_dir,
        log_path=tmp_path / "backup-log.jsonl",
        password_provider=lambda: password,
        refresh_command=[sys.executable, "-c", "import json; print(json.dumps({'conversations': []}))"],
    )
    assert exit_code == 0, report

    out_dir = tmp_path / "restored"
    proc = subprocess.run(
        [str(find_7z_binary()), "x", report["backup"]["path"], f"-o{out_dir}"],
        input=password + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert {p.name for p in out_dir.iterdir()} == {"teams", "wechat"}

