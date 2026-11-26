"""wechat-reader tests for multi-keyword OR search, hit enrichment, truncated reporting, and recent compatibility."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import zstandard

from readers import wechat


def _make_wechat_db(db_path: Path) -> None:
    cctx = zstandard.ZstdCompressor()
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)")
    conn.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (1, 'wxid_alice'), (2, 'wxid_bob')")

    # Group table
    conn.execute("""
        CREATE TABLE Msg_11111111111111111111111111111111 (
            local_id INTEGER PRIMARY KEY,
            local_type INTEGER,
            create_time INTEGER,
            real_sender_id INTEGER,
            message_content BLOB,
            WCDB_CT_message_content INTEGER
        )
    """)
    # 1:1 DM table
    conn.execute("""
        CREATE TABLE Msg_22222222222222222222222222222222 (
            local_id INTEGER PRIMARY KEY,
            local_type INTEGER,
            create_time INTEGER,
            real_sender_id INTEGER,
            message_content BLOB,
            WCDB_CT_message_content INTEGER
        )
    """)

    # Group messages (prefixed with sender id + ":\n")
    conn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, 1794285100, 1, "wxid_alice:\nNeed bill of lading", 0),
    )
    conn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (2, 1, 1794285300, 2, cctx.compress(b"wxid_bob:\nBill of lading and telex release"), 4),
    )
    # Message whose sender id contains "telex" in prefix, NOT in body: must NOT match "telex"
    conn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (3, 1, 1794285350, 2, "wxid_telex:\nUnrelated message", 0),
    )

    # DM messages
    conn.execute(
        "INSERT INTO Msg_22222222222222222222222222222222 VALUES (?, ?, ?, ?, ?, ?)",
        (10, 1, 1794285200, 1, "Telex release approved", 0),
    )
    conn.execute(
        "INSERT INTO Msg_22222222222222222222222222222222 VALUES (?, ?, ?, ?, ?, ?)",
        (11, 1, 1794285400, 1, cctx.compress(b"Updated telex release pdf"), 4),
    )
    conn.commit()
    conn.close()


def _patch_wechat(monkeypatch: pytest.MonkeyPatch, db_path: Path, tmp_path: Path) -> None:
    names = {
        "1001@chatroom": "Logistics Group",
        "wxid_alice": "Alice Example",
        "wxid_bob": "Bob Example",
    }
    contexts = [
        {
            "db_path": str(db_path),
            "table_name": "Msg_11111111111111111111111111111111",
            "username": "1001@chatroom",
            "display_name": "Logistics Group",
            "is_group": True,
        },
        {
            "db_path": str(db_path),
            "table_name": "Msg_22222222222222222222222222222222",
            "username": "wxid_alice",
            "display_name": "Alice Example",
            "is_group": False,
        },
    ]

    class FakeCache:
        def get(self, key: str) -> str:
            return str(db_path)

    fake_app = SimpleNamespace(
        cache=FakeCache(),
        decrypted_dir=str(tmp_path),
        db_dir=str(tmp_path),
        msg_db_keys=["msg_0.db"],
        display_name_fn=lambda uid, m: m.get(uid, uid),
    )
    monkeypatch.setattr(wechat, "AppContext", lambda: fake_app)
    monkeypatch.setattr(wechat, "get_contact_names", lambda cache, dec_dir: names)
    monkeypatch.setattr(wechat, "get_self_username", lambda db_dir, cache, dec_dir: "wxid_self")
    monkeypatch.setattr(wechat, "_load_search_contexts_from_db", lambda conn, path, n: contexts)
    monkeypatch.setattr(
        wechat,
        "resolve_chat_context",
        lambda conv, keys, cache, dec_dir: next((c for c in contexts if c["username"] == conv or c["display_name"] == conv), None),
    )
    monkeypatch.setattr(wechat, "_iter_table_contexts", lambda ctx: [ctx])


def test_wechat_search_multi_keyword_zstd_and_enrichment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "msg_0.db"
    _make_wechat_db(db_path)
    _patch_wechat(monkeypatch, db_path, tmp_path)

    code = wechat.main(["search", "telex release", "bill of lading", "telex release", "--limit", "3"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)

    assert out["keywords"] == ["telex release", "bill of lading"]
    assert "keyword" not in out
    assert out["truncated"] is True
    assert out["failures"] == []
    assert [m["id"] for m in out["messages"]] == [10, 2, 11]

    assert out["messages"][0]["conversation_name"] == "Alice Example"
    assert out["messages"][0]["is_group"] is False
    assert out["messages"][0]["matched_keywords"] == ["telex release"]

    assert out["messages"][1]["conversation_name"] == "Logistics Group"
    assert out["messages"][1]["is_group"] is True
    assert out["messages"][1]["matched_keywords"] == ["telex release", "bill of lading"]

    assert out["messages"][2]["conversation_name"] == "Alice Example"
    assert out["messages"][2]["matched_keywords"] == ["telex release"]


def test_wechat_search_single_keyword_and_exact_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "msg_0.db"
    _make_wechat_db(db_path)
    _patch_wechat(monkeypatch, db_path, tmp_path)

    code = wechat.main(["search", "telex release", "--limit", "3"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)

    assert out["keyword"] == "telex release"
    assert out["keywords"] == ["telex release"]
    assert out["truncated"] is False
    assert [m["id"] for m in out["messages"]] == [10, 2, 11]


def test_wechat_search_rejects_blank_keywords(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "msg_0.db"
    _make_wechat_db(db_path)
    _patch_wechat(monkeypatch, db_path, tmp_path)

    for blank in ("", "   "):
        code = wechat.main(["search", blank])
        assert code != 0
        out = json.loads(capsys.readouterr().out)
        assert set(out) == {"error"}


def test_wechat_messages_truncated_with_offset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "msg_0.db"
    _make_wechat_db(db_path)
    _patch_wechat(monkeypatch, db_path, tmp_path)

    # Logistics Group has 3 messages (1, 2, 3)
    code = wechat.main(["messages", "Logistics Group", "--limit", "2", "--offset", "0"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["truncated"] is True
    assert [m["id"] for m in out["messages"]] == [2, 3]

    code = wechat.main(["messages", "Logistics Group", "--limit", "2", "--offset", "1"])
    assert code == 0
    out_off = json.loads(capsys.readouterr().out)
    assert out_off["truncated"] is False
    assert [m["id"] for m in out_off["messages"]] == [1, 2]


def test_wechat_recent_unchanged_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "msg_0.db"
    _make_wechat_db(db_path)
    _patch_wechat(monkeypatch, db_path, tmp_path)

    code = wechat.main(["recent", "--since", "2026-01-01", "--limit", "3"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["truncated"] is True
    assert len(out["messages"]) == 3
    for m in out["messages"]:
        assert "matched_keywords" not in m
        assert "conversation_name" not in m
        assert "is_group" not in m


def test_wechat_search_keeps_same_local_id_from_different_databases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """local_id is unique only inside one message database; the multi-keyword de-duplication must not merge two databases' rows."""
    table = "Msg_33333333333333333333333333333333"
    contexts = []
    for n, (ts, body) in enumerate([(1794285100, "older telex release"), (1794285200, "newer telex release")]):
        db_path = tmp_path / f"msg_{n}.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)")
        conn.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (1, 'wxid_alice')")
        conn.execute(
            f"CREATE TABLE {table} (local_id INTEGER PRIMARY KEY, local_type INTEGER, create_time INTEGER, "
            "real_sender_id INTEGER, message_content BLOB, WCDB_CT_message_content INTEGER)"
        )
        conn.execute(f"INSERT INTO {table} VALUES (1, 1, ?, 1, ?, 0)", (ts, body))
        conn.commit()
        conn.close()
        contexts.append(
            {"db_path": str(db_path), "table_name": table, "username": "wxid_alice",
             "display_name": "Alice Example", "is_group": False}
        )
    fake_app = SimpleNamespace(
        cache=None, decrypted_dir=str(tmp_path), db_dir=str(tmp_path), msg_db_keys=[],
        display_name_fn=lambda uid, m: m.get(uid, uid),
    )
    monkeypatch.setattr(wechat, "get_contact_names", lambda cache, dec_dir: {"wxid_alice": "Alice Example"})
    monkeypatch.setattr(wechat, "get_self_username", lambda db_dir, cache, dec_dir: "wxid_self")

    messages, truncated, failures = wechat._collect(fake_app, contexts, 1, 0, None, None, ["telex release", "release"])
    assert (truncated, failures) == (True, [])
    assert [m["text"] for m in messages] == ["newer telex release"]

    messages, truncated, failures = wechat._collect(fake_app, contexts, 5, 0, None, None, ["telex release", "release"])
    assert (truncated, failures) == (False, [])
    assert [m["text"] for m in messages] == ["older telex release", "newer telex release"]