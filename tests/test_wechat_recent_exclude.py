"""wechat-reader tests for wechat_exclude_conversations and excluded_messages in recent."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import settings_args

from readers import wechat


def _make_wechat_dbs(db_path: Path, session_db_path: Path) -> None:
    base_ts = int(datetime.fromisoformat("2026-11-10T10:00:00+08:00").timestamp())
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)")
    conn.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (1, 'wxid_alice'), (2, 'wxid_bob')")

    for table in (
        "Msg_11111111111111111111111111111111",
        "Msg_22222222222222222222222222222222",
        "Msg_33333333333333333333333333333333",
    ):
        conn.execute(f"""
            CREATE TABLE {table} (
                local_id INTEGER PRIMARY KEY,
                local_type INTEGER,
                create_time INTEGER,
                real_sender_id INTEGER,
                message_content BLOB,
                WCDB_CT_message_content INTEGER
            )
        """)

    # Excluded group (12345678@chatroom): 1 before since, 3 in range
    conn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, base_ts - 3600, 1, "wxid_alice:\nBefore since", 0),
    )
    conn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (2, 1, base_ts + 60, 1, "wxid_alice:\nExcluded 1", 0),
    )
    conn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (3, 1, base_ts + 180, 2, "wxid_bob:\nExcluded 2", 0),
    )
    conn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (4, 1, base_ts + 300, 1, "wxid_alice:\nExcluded 3", 0),
    )

    # Kept DM (wxid_alice): 2 in range
    conn.execute(
        "INSERT INTO Msg_22222222222222222222222222222222 VALUES (?, ?, ?, ?, ?, ?)",
        (10, 1, base_ts + 120, 1, "Keep 1", 0),
    )
    conn.execute(
        "INSERT INTO Msg_22222222222222222222222222222222 VALUES (?, ?, ?, ?, ?, ?)",
        (11, 1, base_ts + 240, 1, "Keep 2", 0),
    )

    # Official account (gh_0123abcd): 2 in range; must always be ignored by official-account filter
    conn.execute(
        "INSERT INTO Msg_33333333333333333333333333333333 VALUES (?, ?, ?, ?, ?, ?)",
        (20, 1, base_ts + 150, 1, "Official 1", 0),
    )
    conn.execute(
        "INSERT INTO Msg_33333333333333333333333333333333 VALUES (?, ?, ?, ?, ?, ?)",
        (21, 1, base_ts + 270, 1, "Official 2", 0),
    )
    conn.commit()
    conn.close()

    sconn = sqlite3.connect(session_db_path)
    sconn.execute("""
        CREATE TABLE SessionTable (
            username TEXT PRIMARY KEY,
            unread_count INTEGER,
            summary TEXT,
            last_timestamp INTEGER,
            last_msg_type INTEGER,
            last_msg_sender TEXT
        )
    """)
    sconn.execute(
        "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?)",
        ("12345678@chatroom", 0, "Excluded 3", base_ts + 300, 1, "wxid_alice"),
    )
    sconn.execute(
        "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?)",
        ("wxid_alice", 0, "Keep 2", base_ts + 240, 1, "wxid_alice"),
    )
    sconn.commit()
    sconn.close()


def _patch_wechat(monkeypatch: pytest.MonkeyPatch, db_path: Path, session_db_path: Path, tmp_path: Path) -> None:
    names = {
        "12345678@chatroom": "Example Game Group",
        "wxid_alice": "Alice Example",
        "wxid_bob": "Bob Example",
        "gh_0123abcd": "Official News",
    }
    contexts = [
        {
            "db_path": str(db_path),
            "table_name": "Msg_11111111111111111111111111111111",
            "username": "12345678@chatroom",
            "display_name": "Example Game Group",
            "is_group": True,
        },
        {
            "db_path": str(db_path),
            "table_name": "Msg_22222222222222222222222222222222",
            "username": "wxid_alice",
            "display_name": "Alice Example",
            "is_group": False,
        },
        {
            "db_path": str(db_path),
            "table_name": "Msg_33333333333333333333333333333333",
            "username": "gh_0123abcd",
            "display_name": "Official News",
            "is_group": False,
        },
    ]

    class FakeCache:
        def get(self, key: str) -> str:
            if "session" in key:
                return str(session_db_path)
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


def test_wechat_recent_exclude_unset_and_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "msg_0.db"
    session_db_path = tmp_path / "session.db"
    _make_wechat_dbs(db_path, session_db_path)
    _patch_wechat(monkeypatch, db_path, session_db_path, tmp_path)

    for extra in ({}, {"wechat_exclude_conversations": []}):
        sargs = settings_args(tmp_path, **extra)
        code = wechat.main([*sargs, "recent", "--since", "2026-11-10T10:00:00+08:00"])
        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["excluded_messages"] == 0
        assert out["truncated"] is False
        assert [m["id"] for m in out["messages"]] == [2, 10, 3, 11, 4]


def test_wechat_recent_excludes_before_limit_and_counts_full_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "msg_0.db"
    session_db_path = tmp_path / "session.db"
    _make_wechat_dbs(db_path, session_db_path)
    _patch_wechat(monkeypatch, db_path, session_db_path, tmp_path)

    # Include an official account id too: it must NOT inflate excluded_messages
    sargs = settings_args(tmp_path, wechat_exclude_conversations=["12345678@chatroom", "gh_0123abcd"])
    code = wechat.main([*sargs, "recent", "--since", "2026-11-10T10:00:00+08:00", "--limit", "2"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["excluded_messages"] == 3
    assert out["truncated"] is False
    assert [m["id"] for m in out["messages"]] == [10, 11]

    # Even with --limit 1 (truncating kept messages), excluded_messages still counts all 3 in range
    code_trunc = wechat.main([*sargs, "recent", "--since", "2026-11-10T10:00:00+08:00", "--limit", "1"])
    assert code_trunc == 0
    out_trunc = json.loads(capsys.readouterr().out)
    assert out_trunc["excluded_messages"] == 3
    assert out_trunc["truncated"] is True
    assert [m["id"] for m in out_trunc["messages"]] == [11]


def test_wechat_recent_unknown_excluded_id_is_not_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "msg_0.db"
    session_db_path = tmp_path / "session.db"
    _make_wechat_dbs(db_path, session_db_path)
    _patch_wechat(monkeypatch, db_path, session_db_path, tmp_path)

    sargs = settings_args(tmp_path, wechat_exclude_conversations=["99999999@chatroom"])
    code = wechat.main([*sargs, "recent", "--since", "2026-11-10T10:00:00+08:00"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["excluded_messages"] == 0
    assert [m["id"] for m in out["messages"]] == [2, 10, 3, 11, 4]


def test_wechat_other_commands_still_return_excluded_conversation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "msg_0.db"
    session_db_path = tmp_path / "session.db"
    _make_wechat_dbs(db_path, session_db_path)
    _patch_wechat(monkeypatch, db_path, session_db_path, tmp_path)

    sargs = settings_args(tmp_path, wechat_exclude_conversations=["12345678@chatroom"])

    assert wechat.main([*sargs, "conversations"]) == 0
    out_c = json.loads(capsys.readouterr().out)
    assert any(c["id"] == "12345678@chatroom" for c in out_c["conversations"])

    assert wechat.main([*sargs, "messages", "12345678@chatroom"]) == 0
    out_m = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in out_m["messages"]] == [1, 2, 3, 4]

    assert wechat.main([*sargs, "search", "Excluded"]) == 0
    out_s = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in out_s["messages"]] == [2, 3, 4]
