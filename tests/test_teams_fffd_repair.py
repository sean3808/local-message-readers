import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from readers.archive import Archive
from readers.teams.cli import _parser, _run
from readers.teams.snapshot import SnapshotError, read_cache_messages


def test_repair_is_preview_by_default():
    args = _parser().parse_args(["repair-fffd"])
    assert args.write is False


def test_preview_write_and_repeat(tmp_path, monkeypatch, use_settings):
    path = tmp_path / "archive.db"
    use_settings(archive_path=path)
    archive = Archive(path)
    texts = ["gi�p", "hello \n�", "a�b", "c�d", "e�f", "g�h"]
    messages = [{"conversation_id": "c", "id": str(i), "text": text, "version": 900}
                for i, text in enumerate(texts)]
    archive.ingest_messages("teams", messages, source="cache")
    archive.ingest_messages("teams", [messages[4]], source="export")
    with sqlite3.connect(path) as conn:
        for table in ("messages", "cache_messages"):
            conn.execute(f"UPDATE {table} SET time = ?, timestamp = ?, edited = 1, deleted = 1 WHERE id = '0'",
                         ("2026-01-01T12:00:00+00:00", 1767268800))
            conn.execute(f"UPDATE {table} SET time = ?, timestamp = ?, edited = 1 WHERE id = '1'",
                         ("2026-02-02T12:00:00+00:00", 1770033600))
    fresh = [{"conversation_id": "c", "id": str(i), "text": text} for i, text in
             enumerate(["giúp", "hello", "a�b", "xzzb", "eéf"])]
    def fake_read(_, failures):
        failures.append("Could not parse record: store='replychains' (unrelated record)")
        return fresh

    monkeypatch.setattr("readers.teams.cli.read_cache_messages", fake_read)
    monkeypatch.setattr("readers.teams.cli.ingest_cache", lambda *_: pytest.fail("unexpected refresh"))
    monkeypatch.setattr("readers.teams.cli.backup_status", lambda _: {"stale": False})
    args = _parser().parse_args(["repair-fffd", "--archive-path", str(path)])
    digest = hashlib.sha256(path.read_bytes()).digest()
    result = _run(args)
    assert hashlib.sha256(path.read_bytes()).digest() == digest
    assert result["candidates"] == 6
    assert result["repairable"] == 2
    assert result["skipped"] == {"sources_not_cache_only": 1, "not_in_cache": 1, "still_fffd": 1, "other_difference": 1}
    # An unparseable cache record elsewhere is reported, not fatal.
    assert result["cache_read_failures"] == ["Could not parse record: store='replychains' (unrelated record)"]
    with sqlite3.connect(path) as conn:
        before = {table: conn.execute(
            f"SELECT id, time, timestamp, edited, deleted, version{', sources' if table == 'messages' else ''} "
            f"FROM {table} WHERE id IN ('0', '1') ORDER BY id"
        ).fetchall() for table in ("messages", "cache_messages")}
    args.write = True
    assert _run(args)["updated"] == 2
    with sqlite3.connect(path) as conn:
        for table in ("messages", "cache_messages"):
            assert conn.execute(f"SELECT id, text FROM {table} WHERE id IN ('0', '1') ORDER BY id").fetchall() == [("0", "giúp"), ("1", "hello")]
            assert conn.execute(
                f"SELECT id, time, timestamp, edited, deleted, version{', sources' if table == 'messages' else ''} "
                f"FROM {table} WHERE id IN ('0', '1') ORDER BY id"
            ).fetchall() == before[table]
    assert _run(args)["repairable"] == 0


def test_stale_backup_refuses_write(tmp_path, monkeypatch, use_settings):
    path = tmp_path / "archive.db"
    use_settings(archive_path=path)
    Archive(path)
    monkeypatch.setattr("readers.teams.cli.backup_status", lambda _: {"stale": True})
    args = _parser().parse_args(["repair-fffd", "--archive-path", str(path), "--write"])
    with pytest.raises(RuntimeError, match="backup"):
        _run(args)


def test_write_rejects_non_default_archive_but_preview_allowed(tmp_path, monkeypatch, use_settings):
    default_path = tmp_path / "default.db"
    other_path = tmp_path / "other.db"
    use_settings(archive_path=default_path)
    Archive(other_path)
    monkeypatch.setattr("readers.teams.cli.read_cache_messages", lambda *_: [])
    monkeypatch.setattr("readers.teams.cli.backup_status", lambda _: {"stale": False})
    args = _parser().parse_args(["repair-fffd", "--archive-path", str(other_path)])
    assert _run(args)["candidates"] == 0
    args.write = True
    with pytest.raises(RuntimeError, match="default archive"):
        _run(args)


def test_failed_repair_rolls_back_all_rows(tmp_path):
    path = tmp_path / "archive.db"
    archive = Archive(path)
    archive.ingest_messages("teams", [
        {"conversation_id": "c", "id": "1", "text": "a�"},
        {"conversation_id": "c", "id": "2", "text": "b�"},
    ], source="cache")
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM cache_messages WHERE id='2'")
    with pytest.raises(RuntimeError, match="cache mirror"):
        archive.repair_texts("teams", [("c", "1", "a�", "aé"), ("c", "2", "b�", "bé")])
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT text FROM messages WHERE id='1'").fetchone() == ("a�",)
        assert conn.execute("SELECT text FROM cache_messages WHERE id='1'").fetchone() == ("a�",)


def test_mirror_text_changed_since_preview_rolls_back_all_rows(tmp_path):
    path = tmp_path / "archive.db"
    archive = Archive(path)
    archive.ingest_messages("teams", [
        {"conversation_id": "c", "id": "1", "text": "a�"},
        {"conversation_id": "c", "id": "2", "text": "b�"},
    ], source="cache")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE cache_messages SET text = 'changed' WHERE id='2'")
    with pytest.raises(RuntimeError, match="cache mirror"):
        archive.repair_texts("teams", [("c", "1", "a�", "aé"), ("c", "2", "b�", "bé")])
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT text FROM messages WHERE id='1'").fetchone() == ("a�",)
        assert conn.execute("SELECT text FROM cache_messages WHERE id='1'").fetchone() == ("a�",)
        assert conn.execute("SELECT text FROM cache_messages WHERE id='2'").fetchone() == ("changed",)


def test_repair_accepts_equivalent_sources_json(tmp_path):
    path = tmp_path / "archive.db"
    archive = Archive(path)
    archive.ingest_messages("teams", [{"conversation_id": "c", "id": "1", "text": "a�"}], source="cache")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE messages SET sources = ? WHERE id = '1'", ('[ "cache" ]',))
    assert archive.repair_texts("teams", [("c", "1", "a�", "aé")]) == 1


def test_failed_newer_version_marks_key_even_when_older_version_loads():
    from readers.teams.snapshot import _load_latest_store_records

    def rec(seq, value):
        return SimpleNamespace(key=SimpleNamespace(value=("c", "chain")), value=value, ldb_seq_no=seq)

    class Store:
        def iterate_records(self, bad_deserializer_data_handler=None):
            return iter([rec(1, {"messageMap": {}}), rec(2, None)])

    failures: list[str] = []
    failed_keys: set = set()
    latest = _load_latest_store_records(Store(), "replychains", failures, failed_keys)
    assert latest[("c", "chain")].ldb_seq_no == 1  # the stale version survives in the loader...
    assert ("c", "chain") in failed_keys  # ...so the key must be reported as failed


def test_read_cache_drops_conversations_with_failed_records(tmp_path, monkeypatch):
    leveldb = tmp_path / "IndexedDB" / "https_teams.live.com_0.indexeddb.leveldb"
    leveldb.mkdir(parents=True)

    def fake_load(_leveldb, _blob, failures, failed_keys):
        failures.append("bad record")
        failed_keys.add(("stale", "chain"))
        return {}

    monkeypatch.setattr("readers.teams.snapshot.load_latest_replychain_records", fake_load)
    monkeypatch.setattr("readers.teams.snapshot._normalize_replychains", lambda *_, **__: [
        {"conversation_id": "stale", "id": "1", "text": "old text"},
        {"conversation_id": "ok", "id": "2", "text": "fresh"},
    ])
    failures: list[str] = []
    assert read_cache_messages(tmp_path, failures) == [{"conversation_id": "ok", "id": "2", "text": "fresh"}]
    assert failures == ["bad record"]


def test_read_cache_refuses_failures_with_unknown_key(tmp_path, monkeypatch):
    from readers.teams.snapshot import UNKNOWN_KEY

    leveldb = tmp_path / "IndexedDB" / "https_teams.live.com_0.indexeddb.leveldb"
    leveldb.mkdir(parents=True)

    def fake_load(_leveldb, _blob, failures, failed_keys):
        failures.append("unreadable key")
        failed_keys.add(UNKNOWN_KEY)
        return {}

    monkeypatch.setattr("readers.teams.snapshot.load_latest_replychain_records", fake_load)
    with pytest.raises(SnapshotError, match="unidentifiable origin"):
        read_cache_messages(tmp_path, [])


def test_repair_cache_reports_missing_profile(tmp_path):
    with pytest.raises(SnapshotError, match="Teams IndexedDB LevelDB not found"):
        read_cache_messages(tmp_path / "missing", [])


def test_repair_cache_reports_invalid_message(tmp_path, monkeypatch):
    leveldb = tmp_path / "IndexedDB" / "https_teams.live.com_0.indexeddb.leveldb"
    leveldb.mkdir(parents=True)
    records = {("c", "chain"): SimpleNamespace(ldb_seq_no=1, value={"messageMap": {"bad": "invalid"}})}
    monkeypatch.setattr("readers.teams.snapshot.load_latest_replychain_records", lambda *_: records)
    failures: list[str] = []
    assert read_cache_messages(tmp_path, failures) == []
    assert len(failures) == 1 and "TypeError" in failures[0]
