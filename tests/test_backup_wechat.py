"""End-to-end WeChat backup tests with synthetic local data."""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from readers import backup


@pytest.fixture
def inputs(tmp_path: Path):
    root = tmp_path / "xwechat_files"
    account = root / "wxid_fake"
    for name in ("db_storage", "msg", "config", "business", "resource", "cache", "temp", "apm_record"):
        (account / name).mkdir(parents=True)
        (account / name / "data").write_text(name, encoding="utf-8")
    (account / "db_storage" / "data-wal").write_text("wal", encoding="utf-8")
    (account / "db_storage" / "data-shm").write_text("shm", encoding="utf-8")
    key = tmp_path / "all_keys.json"
    key.write_text('{"fake": true}', encoding="utf-8")
    archive = tmp_path / "archive.db"
    with sqlite3.connect(archive) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER)")
    dest = tmp_path / "dest"
    dest.mkdir()
    return root, account, key, archive, dest


def run(inputs, tmp_path):
    root, _, key, archive, dest = inputs
    return backup.run_backup(
        archive_path=archive, dest=dest, log_path=tmp_path / "log", wechat_root=root, wechat_key=key,
        password_provider=lambda: "test-password", refresh_command=[sys.executable, "-c", "print('{}')"],
    )


def extract(report, tmp_path):
    output = tmp_path / "out"
    subprocess.run([str(backup.find_7z_binary()), "x", report["backup"]["path"], "-ptest-password", f"-o{output}", "-y"], check=True, capture_output=True)
    return {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}


def test_both_channels_and_selected_files(inputs, tmp_path):
    account = inputs[1]
    before = {p.relative_to(account): (p.stat().st_size, p.stat().st_mtime_ns)
              for p in account.rglob("*") if p.is_file()}
    report, code = run(inputs, tmp_path)
    assert code == 0
    assert report["wechat"] == {"ok": True, "error": None, "file_count": 8,
                                "folders": ["db_storage", "msg", "config", "business", "resource"]}
    assert extract(report, tmp_path) == {
        "teams/archive.db", "wechat/all_keys.json", "wechat/wxid_fake/db_storage/data",
        "wechat/wxid_fake/db_storage/data-wal", "wechat/wxid_fake/db_storage/data-shm",
        "wechat/wxid_fake/msg/data", "wechat/wxid_fake/config/data", "wechat/wxid_fake/business/data",
        "wechat/wxid_fake/resource/data",
    }
    after = {p.relative_to(account): (p.stat().st_size, p.stat().st_mtime_ns)
             for p in account.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("failure", ["missing_account", "missing_key", "missing_folder", "multiple_accounts", "copy_error"])
def test_wechat_failure_preserves_teams_only(inputs, tmp_path, monkeypatch, failure):
    root, account, _key, _, _ = inputs
    if failure == "missing_account":
        inputs = (tmp_path / "missing", *inputs[1:])
    elif failure == "missing_key":
        inputs = (*inputs[:2], tmp_path / "missing-key", *inputs[3:])
    elif failure == "missing_folder":
        for item in (account / "db_storage").iterdir():
            item.unlink()
        (account / "db_storage").rmdir()
    elif failure == "multiple_accounts":
        (root / "second" / "db_storage").mkdir(parents=True)
    else:
        def fail(*args, **kwargs):
            raise OSError("copy failed")
        monkeypatch.setattr(backup.shutil, "copy2", fail)
    report, code = run(inputs, tmp_path)
    assert code == 2
    assert report["backup"]["ok"] is True
    assert report["wechat"]["ok"] is False
    assert report["wechat"]["error"]
    assert extract(report, tmp_path) == {"archive.db"}


@pytest.mark.parametrize("folder", ["msg", "config", "business", "resource"])
def test_optional_folder_missing_still_backs_up(inputs, tmp_path, folder):
    directory = inputs[1] / folder
    (directory / "data").unlink()
    directory.rmdir()
    report, code = run(inputs, tmp_path)
    assert code == 0
    assert report["wechat"]["ok"] is True
    assert report["wechat"]["folders"] == [
        name for name in ("db_storage", "msg", "config", "business", "resource") if name != folder
    ]
    assert f"wechat/wxid_fake/{folder}/data" not in extract(report, tmp_path)


@pytest.mark.parametrize("target", ["account", "account_child", "key_folder", "key_child", "ancestor"])
def test_collect_refuses_source_overlapping_destination(inputs, tmp_path, target):
    root, account, key, _, _ = inputs
    destinations = {"account": account, "account_child": account / "db_storage",
                    "key_folder": key.parent, "key_child": key.parent / "nested", "ancestor": tmp_path.parent}
    before = {p: p.read_bytes() for p in account.rglob("*") if p.is_file()}
    key_before = key.read_bytes()
    with pytest.raises(ValueError, match="overlaps source"):
        backup.collect_wechat(root, key, destinations[target])
    assert {p: p.read_bytes() for p in account.rglob("*") if p.is_file()} == before
    assert key.read_bytes() == key_before


@pytest.mark.parametrize("changes,expected_code", [(1, 0), (2, 2)])
def test_changes_retry_once(inputs, tmp_path, monkeypatch, changes, expected_code):
    source = inputs[1] / "db_storage" / "data"
    original = backup.shutil.copy2
    calls = []

    def copy_and_change(src, dst, *args, **kwargs):
        result = original(src, dst, *args, **kwargs)
        if src == source and len(calls) < changes:
            calls.append(1)
            source.write_text("changed" * len(calls), encoding="utf-8")
        return result

    monkeypatch.setattr(backup.shutil, "copy2", copy_and_change)
    report, code = run(inputs, tmp_path)
    assert code == expected_code
    assert len(calls) == changes
    assert report["wechat"]["ok"] is (expected_code == 0)
    files = extract(report, tmp_path)
    assert ("wechat/wxid_fake/db_storage/data" in files) is (expected_code == 0)
    if expected_code == 0:
        assert (tmp_path / "out" / "wechat" / "wxid_fake" / "db_storage" / "data").read_text() == "changed"
    else:
        assert files == {"archive.db"}


def test_collect_wechat_snapshots_db_and_omits_wal_shm(inputs, tmp_path):
    account = inputs[1]
    db_path = account / "db_storage" / "msg.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA page_size = 4096")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("PRAGMA cache_size = 1")
        conn.execute("CREATE TABLE chat (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO chat (id, body) VALUES (1, 'committed')")
        conn.commit()
        conn.execute("BEGIN")
        for idx in range(2, 40):
            conn.execute("INSERT INTO chat (id, body) VALUES (?, ?)", (idx, f"uncommitted-{idx}-" + ("x" * 200)))

        assert (account / "db_storage" / "msg.db-wal").is_file()
        assert (account / "db_storage" / "msg.db-shm").is_file()
        report, code = run(inputs, tmp_path)
    finally:
        conn.close()

    assert code == 0
    assert report["wechat"]["ok"] is True
    files = extract(report, tmp_path)
    assert "wechat/wxid_fake/db_storage/msg.db" in files
    assert "wechat/wxid_fake/db_storage/msg.db-wal" not in files
    assert "wechat/wxid_fake/db_storage/msg.db-shm" not in files
    assert "wechat/all_keys.json" in files

    restored_db = tmp_path / "out" / "wechat" / "wxid_fake" / "db_storage" / "msg.db"
    with sqlite3.connect(restored_db) as rconn:
        assert rconn.execute("SELECT id, body FROM chat ORDER BY id").fetchall() == [(1, "committed")]


def test_collect_wechat_corrupt_wal_fails_wechat_preserves_teams(inputs, tmp_path):
    account = inputs[1]
    (account / "db_storage" / "broken.db").write_bytes(b"\x00" * 4096)
    (account / "db_storage" / "broken.db-wal").write_bytes(b"corrupt-wal-header")

    report, code = run(inputs, tmp_path)
    assert code == 2
    assert report["backup"]["ok"] is True
    assert report["wechat"]["ok"] is False
    assert report["wechat"]["error"]
    assert extract(report, tmp_path) == {"archive.db"}
