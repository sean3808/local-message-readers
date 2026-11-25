from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from readers.teams.snapshot import load_latest_replychain_records


def _make_record(key: tuple[str, object], value: object, seq: int | None) -> SimpleNamespace:
    return SimpleNamespace(key=SimpleNamespace(value=key), value=value, ldb_seq_no=seq)


def _load_records(records: list[SimpleNamespace], failures: list[str] | None = None) -> dict[object, object]:
    store = MagicMock()
    store.iterate_records.return_value = records
    target = MagicMock()
    target.__contains__.side_effect = lambda name: name == "replychains"
    target.get_object_store_by_name.return_value = store
    db = MagicMock()
    db.database_ids = [SimpleNamespace(name="https_teams.live.com_0:replychain-manager:")]
    db.__getitem__.return_value = target
    with patch("readers.teams.snapshot.WrappedIndexDB") as wrapped:
        wrapped.return_value.__enter__.return_value = db
        return load_latest_replychain_records(Path("dummy"), None, failures)


def test_loader_older_dict_then_newer_placeholder_evicts_key() -> None:
    failures: list[str] = []
    records = _load_records(
        [
            _make_record(("conv1", -1), {"messageMap": {"1": {"id": "1"}}}, seq=10),
            _make_record(("conv1", -1), None, seq=20),
        ],
        failures,
    )
    assert ("conv1", -1) not in records
    assert failures == []


def test_loader_newer_placeholder_then_older_dict_evicts_key() -> None:
    failures: list[str] = []
    records = _load_records(
        [
            _make_record(("conv1", -1), None, seq=20),
            _make_record(("conv1", -1), {"messageMap": {"1": {"id": "1"}}}, seq=10),
        ],
        failures,
    )
    assert ("conv1", -1) not in records
    assert failures == []


def test_loader_older_placeholder_then_newer_dict_keeps_dict() -> None:
    failures: list[str] = []
    newer_dict = {"messageMap": {"1": {"id": "1"}}}
    records = _load_records(
        [
            _make_record(("conv1", -1), None, seq=10),
            _make_record(("conv1", -1), newer_dict, seq=20),
        ],
        failures,
    )
    assert ("conv1", -1) in records
    assert records[("conv1", -1)].value == newer_dict
    assert failures == []


def test_loader_none_seq_does_not_crash_and_yields_to_sequenced_records() -> None:
    """When ldb_seq_no is None: does not crash, never overwrites a seen key, and is superseded by a sequenced record."""
    failures: list[str] = []
    dict_none = {"messageMap": {"1": {"id": "1"}}}
    dict_seq = {"messageMap": {"2": {"id": "2"}}}
    records = _load_records(
        [
            # None seq dict followed by sequenced placeholder -> placeholder wins
            _make_record(("conv_a", -1), dict_none, seq=None),
            _make_record(("conv_a", -1), None, seq=5),
            # Sequenced dict followed by None seq placeholder -> sequenced dict stays
            _make_record(("conv_b", -1), dict_seq, seq=5),
            _make_record(("conv_b", -1), None, seq=None),
            # None seq dict alone -> kept
            _make_record(("conv_c", "1"), dict_none, seq=None),
            # None seq placeholder followed by sequenced dict -> sequenced dict wins
            _make_record(("conv_d", -1), None, seq=None),
            _make_record(("conv_d", -1), dict_seq, seq=5),
        ],
        failures,
    )
    assert ("conv_a", -1) not in records
    assert records[("conv_b", -1)].value == dict_seq
    assert records[("conv_c", "1")].value == dict_none
    assert records[("conv_d", -1)].value == dict_seq
    assert failures == []


def test_normalize_replychains_skips_placeholder_id_minus_one_only() -> None:
    from readers.teams.snapshot import _normalize_replychains

    msg_map = {
        "k_int": {"id": -1, "content": "pending int", "originalArrivalTime": 1795500733000},
        "k_str": {"id": "-1", "content": "pending str", "originalArrivalTime": 1795500733100},
        "k_ws": {"id": " -1 ", "content": "pending ws", "originalArrivalTime": 1795500733200},
        "k_confirmed": {
            "id": "1795500734838",
            "content": "confirmed edited",
            "originalArrivalTime": 1795500734838,
            "skypeeditedid": "1795500734838",
        },
        "-1": {"id": "1795500735000", "content": "key is -1 but id is real", "originalArrivalTime": 1795500735000},
        "k_neg2": {"id": "-2", "content": "other negative id", "originalArrivalTime": 1795500736000},
        "k_abc": {"id": "abc", "content": "non-numeric id", "originalArrivalTime": 1795500737000},
    }
    records = {("19:conv@thread.v2", "root"): _make_record(("19:conv@thread.v2", "root"), {"messageMap": msg_map}, seq=100)}
    failures: list[str] = []
    messages = _normalize_replychains(records, failures)
    assert failures == []
    assert [m["id"] for m in messages] == ["1795500734838", "1795500735000", "-2", "abc"]


def _seed_cleanup_archive(path: Path):
    import sqlite3

    from readers.archive import Archive

    archive = Archive(path)
    archive.ingest_conversations(
        "teams",
        [
            {"id": "conv_a", "name": "Conv A", "is_group": False},
            {"id": "conv_b", "name": "Conv B", "is_group": False},
            {"id": "conv_c", "name": "Conv C", "is_group": False},
        ],
        source="cache",
    )
    archive.ingest_messages(
        "teams",
        [
            {
                "conversation_id": "conv_a",
                "id": "100",
                "time": "2026-11-10T10:00:00+08:00",
                "timestamp": 1794285600.0,
                "text": "real msg",
                "version": 1000,
            },
            {
                "conversation_id": "conv_a",
                "id": "-1",
                "time": "2026-11-10T10:05:00+08:00",
                "timestamp": 1794285900.0,
                "text": "secret pending text",
                "version": 2000,
            },
            {
                "conversation_id": "conv_b",
                "id": "-1",
                "time": "2026-11-10T10:05:00+08:00",
                "timestamp": 1794285900.0,
                "text": "only pending",
                "version": 2000,
            },
            {
                "conversation_id": "conv_c",
                "id": "-1",
                "time": "2026-11-10T10:05:00+08:00",
                "timestamp": 1794285900.0,
                "text": "exported pending",
                "version": 2000,
            },
        ],
        source="cache",
    )
    from contextlib import closing

    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE messages SET sources = '[\"cache\", \"export\"]' WHERE channel = 'teams' AND conversation_id = 'conv_c' AND id = '-1'"
        )
    return archive


def _cleanup_args(path: Path, *extra: str):
    from readers.teams.cli import _parser

    return _parser().parse_args(["clean-placeholder-messages", "--archive-path", str(path), *extra])


def test_clean_placeholder_is_preview_by_default() -> None:
    from readers.teams.cli import _parser

    assert _parser().parse_args(["clean-placeholder-messages"]).write is False


def test_preview_lists_counts_and_locators_without_modifying_or_leaking_text(
    tmp_path: Path, use_settings, monkeypatch
) -> None:
    import hashlib
    import json

    from readers.teams.cli import _run

    monkeypatch.setattr("readers.teams.cli.backup_status", lambda _: {"stale": False})
    path = tmp_path / "archive.db"
    use_settings(archive_path=path)
    _seed_cleanup_archive(path)
    digest = hashlib.sha256(path.read_bytes()).digest()

    result = _run(_cleanup_args(path))
    assert hashlib.sha256(path.read_bytes()).digest() == digest
    assert result["candidates"] == 2
    assert result["skipped"] == 1
    assert result["conversation_ids"] == ["conv_a", "conv_b"]
    assert result["skipped_conversation_ids"] == ["conv_c"]
    assert "deleted" not in result
    dumped = json.dumps(result)
    assert "secret pending text" not in dumped
    assert "only pending" not in dumped


def test_write_deletes_both_tables_updates_last_message_and_is_idempotent(
    tmp_path: Path, use_settings, monkeypatch
) -> None:
    import sqlite3

    from readers.teams.cli import _run

    monkeypatch.setattr("readers.teams.cli.backup_status", lambda _: {"stale": False})
    path = tmp_path / "archive.db"
    use_settings(archive_path=path)
    _seed_cleanup_archive(path)

    result = _run(_cleanup_args(path, "--write"))
    assert result["deleted"] == 2
    assert result["candidates"] == 2
    assert result["skipped"] == 1

    with sqlite3.connect(path) as conn:
        for table in ("messages", "cache_messages"):
            rows = conn.execute(f"SELECT conversation_id, id FROM {table} WHERE id = '-1'").fetchall()
            assert rows == [("conv_c", "-1")]
        conv_a = conn.execute(
            "SELECT last_message_time, last_message_timestamp FROM conversations WHERE id = 'conv_a'"
        ).fetchone()
        assert conv_a == ("2026-11-10T10:00:00+08:00", 1794285600.0)
        conv_b = conn.execute(
            "SELECT last_message_time, last_message_timestamp FROM conversations WHERE id = 'conv_b'"
        ).fetchone()
        assert conv_b == ("", 0.0)

    second = _run(_cleanup_args(path, "--write"))
    assert second["candidates"] == 0
    assert second["deleted"] == 0
    assert second["skipped"] == 1


def test_clean_placeholder_stale_backup_refuses_write(tmp_path: Path, monkeypatch, use_settings) -> None:
    import sqlite3

    import pytest

    from readers.teams.cli import _run

    path = tmp_path / "archive.db"
    use_settings(archive_path=path)
    _seed_cleanup_archive(path)
    monkeypatch.setattr("readers.teams.cli.backup_status", lambda _: {"stale": True})
    with pytest.raises(RuntimeError, match="backup"):
        _run(_cleanup_args(path, "--write"))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE id = '-1'").fetchone()[0] == 3


def test_clean_placeholder_write_rejects_non_default_archive_but_preview_allowed(
    tmp_path: Path, use_settings, monkeypatch
) -> None:
    import sqlite3

    import pytest

    from readers.teams.cli import _run

    monkeypatch.setattr("readers.teams.cli.backup_status", lambda _: {"stale": False})
    default_path = tmp_path / "default.db"
    other_path = tmp_path / "other.db"
    use_settings(archive_path=default_path)
    _seed_cleanup_archive(other_path)
    assert _run(_cleanup_args(other_path))["candidates"] == 2
    with pytest.raises(RuntimeError, match="default archive"):
        _run(_cleanup_args(other_path, "--write"))
    with sqlite3.connect(other_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE id = '-1'").fetchone()[0] == 3


def test_clean_placeholder_failed_write_rolls_back(tmp_path: Path) -> None:
    import sqlite3

    import pytest

    from readers.teams.placeholder import clean_placeholder_messages

    path = tmp_path / "archive.db"
    archive = _seed_cleanup_archive(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TRIGGER block BEFORE DELETE ON cache_messages WHEN OLD.conversation_id = 'conv_b' "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
    with pytest.raises(sqlite3.DatabaseError):
        clean_placeholder_messages(archive, write=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE id = '-1'").fetchone()[0] == 3
        conv_a = conn.execute(
            "SELECT last_message_time FROM conversations WHERE id = 'conv_a'"
        ).fetchone()
        assert conv_a == ("2026-11-10T10:05:00+08:00",)


def _fake_ingest_snapshot(message_map: dict[str, dict[str, object]]) -> MagicMock:
    conv_id = "19:ingest_conv@thread.v2"
    conv_rec = _make_record(
        conv_id,
        {
            "id": conv_id,
            "threadProperties": {"topic": "Ingest Group"},
            "lastMessageTimeUtc": 1795500734838,
        },
        seq=10,
    )
    chain_rec = _make_record(
        conv_id,
        {"conversationId": conv_id, "messageMap": message_map},
        seq=11,
    )

    def _store_db(store_name: str, recs: list[SimpleNamespace]) -> MagicMock:
        store = MagicMock()
        store.iterate_records.return_value = recs
        db = MagicMock()
        db.get_object_store_by_name.return_value = store
        db.__contains__.side_effect = lambda name: name == store_name
        return db

    dbs = {
        "conversation-manager": _store_db("conversations", [conv_rec]),
        "replychain-manager": _store_db("replychains", [chain_rec]),
    }
    ids = []
    by_id = {}
    for marker, db in dbs.items():
        db_id = MagicMock()
        db_id.name = f"https_teams.live.com_0:{marker}:"
        ids.append(db_id)
        by_id[id(db_id)] = db

    fake = MagicMock()
    fake.database_ids = ids
    fake.__getitem__.side_effect = lambda db_id: by_id[id(db_id)]
    wrapped = MagicMock()
    wrapped.return_value.__enter__.return_value = fake
    return wrapped


def test_ingest_cache_ignores_int_and_str_placeholder_ids_in_both_tables(tmp_path: Path) -> None:
    import sqlite3

    from readers.archive import Archive
    from readers.teams.snapshot import ingest_cache

    conv_id = "19:ingest_conv@thread.v2"
    profile = tmp_path / "profile"
    (profile / "IndexedDB" / "https_teams.live.com_0.indexeddb.leveldb").mkdir(parents=True)
    arc = Archive(db_path=tmp_path / "arc.db")

    msg_map = {
        "k_int": {
            "id": -1,
            "originalArrivalTime": 1795500733199,
            "creator": "8:alice",
            "imDisplayName": "Alice",
            "messageType": "RichText/Html",
            "content": "<p>pending copy</p>",
        },
        "k_str": {
            "id": "-1",
            "originalArrivalTime": 1795500733250,
            "creator": "8:alice",
            "imDisplayName": "Alice",
            "messageType": "RichText/Html",
            "content": "<p>pending copy str</p>",
        },
        "k_real": {
            "id": "1795500734838",
            "originalArrivalTime": 1795500734838,
            "creator": "8:alice",
            "imDisplayName": "Alice",
            "messageType": "RichText/Html",
            "content": "<p>confirmed copy</p>",
        },
    }
    with patch("readers.teams.snapshot.WrappedIndexDB", _fake_ingest_snapshot(msg_map)):
        outcome = ingest_cache(arc, profile)

    assert outcome.success
    assert outcome.failures == []
    with sqlite3.connect(arc.db_path) as conn:
        for table in ("messages", "cache_messages"):
            ids = [r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE conversation_id = ?", (conv_id,)).fetchall()]
            assert ids == ["1795500734838"]


def test_ingest_cache_edited_deleted_or_quoted_confirmed_copy_never_ingests_minus_one(tmp_path: Path) -> None:
    import sqlite3

    from readers.archive import Archive
    from readers.teams.snapshot import ingest_cache

    conv_id = "19:ingest_conv@thread.v2"
    profile = tmp_path / "profile"
    (profile / "IndexedDB" / "https_teams.live.com_0.indexeddb.leveldb").mkdir(parents=True)
    arc = Archive(db_path=tmp_path / "arc.db")

    msg_map = {
        "p_edited": {
            "id": -1,
            "originalArrivalTime": 1795500733000,
            "creator": "8:alice",
            "imDisplayName": "Alice",
            "messageType": "RichText/Html",
            "content": "<p>original unedited text</p>",
        },
        "c_edited": {
            "id": "1795500734000",
            "originalArrivalTime": 1795500734000,
            "creator": "8:alice",
            "imDisplayName": "Alice",
            "messageType": "RichText/Html",
            "content": '<blockquote itemtype="http://schema.skype.com/Reply">quote</blockquote><p>edited text</p>',
            "skypeeditedid": "1795500734000",
        },
        "p_deleted": {
            "id": "-1",
            "originalArrivalTime": 1795500735000,
            "creator": "8:alice",
            "imDisplayName": "Alice",
            "messageType": "RichText/Html",
            "content": "<p>before delete</p>",
        },
        "c_deleted": {
            "id": "1795500736000",
            "originalArrivalTime": 1795500736000,
            "creator": "8:alice",
            "imDisplayName": "Alice",
            "messageType": "RichText/Html",
            "content": "",
            "properties": {"deletetime": "1795500737000"},
        },
    }
    with patch("readers.teams.snapshot.WrappedIndexDB", _fake_ingest_snapshot(msg_map)):
        outcome = ingest_cache(arc, profile)

    assert outcome.success
    assert outcome.failures == []
    with sqlite3.connect(arc.db_path) as conn:
        for table in ("messages", "cache_messages"):
            ids = sorted(
                r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE conversation_id = ?", (conv_id,)).fetchall()
            )
            assert ids == ["1795500734000", "1795500736000"]



