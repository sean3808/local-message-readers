"""Teams ingest_cache tests for failed replychain keys and UNKNOWN_KEY (issue #19)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from readers.archive import Archive
from readers.teams.snapshot import UNKNOWN_KEY, ingest_cache


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "profile"
    (profile / "IndexedDB" / "https_teams.live.com_0.indexeddb.leveldb").mkdir(parents=True)
    return profile


def _conv_record(cid: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        key=SimpleNamespace(value=cid),
        ldb_seq_no=10,
        value={
            "id": cid,
            "threadProperties": {"topic": name},
            "lastMessageTimeUtc": 1794268800000,
        },
    )


def _replychain_record(cid: str, mid: str, text: str, *, version: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        key=SimpleNamespace(value=(cid, "chain")),
        ldb_seq_no=11,
        value={
            "conversationId": cid,
            "messageMap": {
                mid: {
                    "id": mid,
                    "originalArrivalTime": 1794268800000,
                    "creator": "8:alice",
                    "imDisplayName": "Alice",
                    "messageType": "RichText/Html",
                    "content": f"<p>{text}</p>",
                    "version": version,
                }
            },
        },
    )


def test_ingest_cache_excludes_conversation_with_failed_key(tmp_path: Path, monkeypatch) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    profile = _profile(tmp_path)

    monkeypatch.setattr(
        "readers.teams.snapshot.load_latest_conversation_records",
        lambda *_args, **_kwargs: {
            "stale": _conv_record("stale", "Stale Chat"),
            "ok": _conv_record("ok", "OK Chat"),
        },
    )

    def fake_load_replychains(_leveldb, _blob, failures=None, failed_keys=None):
        assert failures is not None
        assert failed_keys is not None
        failures.append("Could not parse record: store='replychains', key='('stale', 'chain')': deserialization failed")
        failed_keys.add(("stale", "chain"))
        return {
            ("stale", "chain"): _replychain_record("stale", "m_stale", "stale text"),
            ("ok", "chain"): _replychain_record("ok", "m_ok", "fresh text"),
        }

    monkeypatch.setattr("readers.teams.snapshot.load_latest_replychain_records", fake_load_replychains)

    outcome = ingest_cache(arc, profile)

    assert outcome.success
    assert any("stale" in f for f in outcome.failures), outcome.failures
    assert arc.query_messages(channel="teams", conversation_id="stale")[0] == []
    ok_msgs, _ = arc.query_messages(channel="teams", conversation_id="ok")
    assert [m["id"] for m in ok_msgs] == ["m_ok"]
    assert ok_msgs[0]["text"] == "fresh text"


def test_tainted_ingest_leaves_existing_archived_message_unchanged(tmp_path: Path, monkeypatch) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    profile = _profile(tmp_path)

    monkeypatch.setattr(
        "readers.teams.snapshot.load_latest_conversation_records",
        lambda *_args, **_kwargs: {"stale": _conv_record("stale", "Stale Chat")},
    )
    monkeypatch.setattr(
        "readers.teams.snapshot.load_latest_replychain_records",
        lambda *_args, **_kwargs: {
            ("stale", "chain"): _replychain_record("stale", "m1", "archived text", version=200),
        },
    )

    first = ingest_cache(arc, profile)
    assert first.success

    with sqlite3.connect(arc.db_path) as conn:
        before_msg = conn.execute(
            "SELECT text, version, last_seen FROM messages WHERE channel = 'teams' AND conversation_id = 'stale' AND id = 'm1'"
        ).fetchone()
        before_cache = conn.execute(
            "SELECT text, version, last_seen FROM cache_messages WHERE channel = 'teams' AND conversation_id = 'stale' AND id = 'm1'"
        ).fetchone()
    assert before_msg is not None and before_msg[0] == "archived text" and before_msg[1] == 200
    assert before_cache == before_msg

    def tainted_load(_leveldb, _blob, failures=None, failed_keys=None):
        assert failures is not None and failed_keys is not None
        failures.append("Could not parse record: store='replychains', key='('stale', 'chain')': deserialization failed")
        failed_keys.add(("stale", "chain"))
        return {
            ("stale", "chain"): _replychain_record("stale", "m1", "older stale text", version=100),
        }

    monkeypatch.setattr("readers.teams.snapshot.load_latest_replychain_records", tainted_load)
    second = ingest_cache(arc, profile)
    assert second.success
    assert any("stale" in f for f in second.failures), second.failures

    with sqlite3.connect(arc.db_path) as conn:
        after_msg = conn.execute(
            "SELECT text, version, last_seen FROM messages WHERE channel = 'teams' AND conversation_id = 'stale' AND id = 'm1'"
        ).fetchone()
        after_cache = conn.execute(
            "SELECT text, version, last_seen FROM cache_messages WHERE channel = 'teams' AND conversation_id = 'stale' AND id = 'm1'"
        ).fetchone()
    assert after_msg == before_msg
    assert after_cache == before_cache


def test_ingest_cache_unknown_key_aborts_messages_and_keeps_conversations(tmp_path: Path, monkeypatch) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    profile = _profile(tmp_path)

    monkeypatch.setattr(
        "readers.teams.snapshot.load_latest_conversation_records",
        lambda *_args, **_kwargs: {"ok": _conv_record("ok", "OK Chat")},
    )

    def unknown_key_load(_leveldb, _blob, failures=None, failed_keys=None):
        assert failures is not None and failed_keys is not None
        failures.append("Could not parse record: store='replychains', key='<unknown>': deserialization failed")
        failed_keys.add(UNKNOWN_KEY)
        return {
            ("ok", "chain"): _replychain_record("ok", "m_ok", "should not be imported"),
        }

    monkeypatch.setattr("readers.teams.snapshot.load_latest_replychain_records", unknown_key_load)

    outcome = ingest_cache(arc, profile)

    assert not outcome.success
    assert outcome.error and "unidentifiable origin" in outcome.error
    assert any("unidentifiable origin" in f for f in outcome.failures), outcome.failures
    assert [c["id"] for c in arc.query_conversations("teams", limit=10)] == ["ok"]
    assert arc.get_data_current_up_to("teams") is None
    with sqlite3.connect(arc.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cache_messages").fetchone()[0] == 0
