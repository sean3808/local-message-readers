"""wechat-reader download tests for hardlink resolution, fail-closed statuses, and safe copy."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import zstandard
from conftest import settings_args

from readers import wechat, wechat_media


def _make_hardlink_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE dir2id (rowid INTEGER PRIMARY KEY, username TEXT)")
    for table in ("image_hardlink_info_v4", "file_hardlink_info_v4", "video_hardlink_info_v4"):
        conn.execute(f"""
            CREATE TABLE {table} (
                md5_hash INTEGER,
                md5 TEXT,
                type INTEGER,
                file_name TEXT,
                file_size INTEGER,
                modify_time INTEGER,
                dir1 INTEGER,
                dir2 INTEGER,
                extra_buffer BLOB
            )
        """)
    conn.commit()
    return conn


def _make_msg_db(db_path: Path, table_name: str = "Msg_11111111111111111111111111111111") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)")
    conn.execute("INSERT OR IGNORE INTO Name2Id (rowid, user_name) VALUES (1, 'wxid_alice')")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            local_id INTEGER PRIMARY KEY,
            local_type INTEGER,
            create_time INTEGER,
            real_sender_id INTEGER,
            message_content BLOB,
            WCDB_CT_message_content INTEGER
        )
    """)
    conn.commit()
    return conn


def _setup_wechat_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    hardlink_db: Path | None = None,
    msg_dbs: list[tuple[Path, str]] | None = None,
    is_group: bool = False,
) -> tuple[Path, Path]:
    account_root = tmp_path / "wxid_account"
    db_dir = account_root / "db_storage"
    msg_root = account_root / "msg"
    db_dir.mkdir(parents=True, exist_ok=True)
    msg_root.mkdir(parents=True, exist_ok=True)

    contexts = []
    msg_db_keys = []
    if msg_dbs is None:
        default_db = tmp_path / "msg_0.db"
        _make_msg_db(default_db).close()
        msg_dbs = [(default_db, "Msg_11111111111111111111111111111111")]

    conv_id = "1001@chatroom" if is_group else "wxid_alice"
    conv_name = "Logistics Group" if is_group else "Alice Example"
    for idx, (db_path, table_name) in enumerate(msg_dbs):
        key = f"message/msg_{idx}.db"
        msg_db_keys.append(key)
        contexts.append({
            "db_path": str(db_path),
            "table_name": table_name,
            "username": conv_id,
            "display_name": conv_name,
            "is_group": is_group,
        })

    class FakeCache:
        def get(self, key: str) -> str | None:
            if key == os.path.join("hardlink", "hardlink.db"):
                return str(hardlink_db) if hardlink_db is not None else None
            for idx, (db_path, _) in enumerate(msg_dbs):
                if key == f"message/msg_{idx}.db":
                    return str(db_path)
            return str(msg_dbs[0][0])

    fake_app = SimpleNamespace(
        cache=FakeCache(),
        decrypted_dir=str(tmp_path),
        db_dir=str(db_dir),
        msg_db_keys=msg_db_keys,
        display_name_fn=lambda uid, m: m.get(uid, uid),
    )
    monkeypatch.setattr(wechat, "AppContext", lambda: fake_app)
    monkeypatch.setattr(wechat, "get_contact_names", lambda cache, dec_dir: {conv_id: conv_name})
    monkeypatch.setattr(wechat, "get_self_username", lambda d, c, dec: "wxid_self")
    monkeypatch.setattr(
        wechat,
        "resolve_chat_context",
        lambda conv, keys, cache, dec_dir: contexts[0] if conv in (conv_id, conv_name) else None,
    )
    monkeypatch.setattr(wechat, "_iter_table_contexts", lambda ctx: contexts)
    return account_root, msg_root


def _run_download(
    tmp_path: Path,
    msg_id: int,
    conv: str,
    dest: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict]:
    code = wechat.main([
        *settings_args(tmp_path),
        "download",
        str(msg_id),
        "--conversation",
        conv,
        "--to",
        str(dest),
    ])
    out = json.loads(capsys.readouterr().out)
    return code, out


def test_download_file_type_1_and_type_6(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = tmp_path / "msg_0.db"
    hl_db = tmp_path / "hardlink.db"
    account_root, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")])

    # Type 1 uses dir1; Type 6 uses dir2
    file1_dir = msg_root / "file" / "2026-11"
    file1_dir.mkdir(parents=True)
    file1_path = file1_dir / "invoice.pdf"
    file1_path.write_bytes(b"%PDF-1.4\nhello")

    file6_dir = msg_root / "file" / "2026-10"
    file6_dir.mkdir(parents=True)
    file6_path = file6_dir / "packing.xlsx"
    file6_path.write_bytes(b"PK\x03\x04excel")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11'), (2, '2026-10'), (3, 'wrong_dir')")
    hl.execute(
        "INSERT INTO file_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        ("aaaa" * 8, 1, "invoice.pdf", 1, 3),
    )
    hl.execute(
        "INSERT INTO file_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        ("bbbb" * 8, 6, "packing.xlsx", 3, 2),
    )
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db, "Msg_11111111111111111111111111111111")
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (101, 49, 1794285100, 1, f"<msg><appmsg><type>6</type><md5>{'AAAA' * 8}</md5></appmsg></msg>", 0),
    )
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (102, 49, 1794285200, 1, f"<msg><appmsg><type>6</type><md5>{'bbbb' * 8}</md5></appmsg></msg>", 0),
    )
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()

    code1, out1 = _run_download(tmp_path, 101, "Alice Example", dest, capsys)
    assert code1 == 0
    assert out1["conversation"] == {"id": "wxid_alice", "name": "Alice Example", "is_group": False}
    assert out1["message_id"] == 101
    assert out1["failures"] == []
    assert out1["items"] == [{
        "kind": "file",
        "status": "resolved",
        "reason": wechat_media.REASON_RESOLVED,
        "name": "invoice.pdf",
        "path": str(dest / "invoice.pdf"),
        "size": len(b"%PDF-1.4\nhello"),
    }]
    assert (dest / "invoice.pdf").read_bytes() == b"%PDF-1.4\nhello"

    code2, out2 = _run_download(tmp_path, 102, "wxid_alice", dest, capsys)
    assert code2 == 0
    assert out2["items"][0]["status"] == "resolved"
    assert out2["items"][0]["name"] == "packing.xlsx"
    assert (dest / "packing.xlsx").read_bytes() == b"PK\x03\x04excel"


@pytest.mark.parametrize(
    ("magic", "expected_ext"),
    [
        (b"\xff\xd8\xff\xe0jpegdata", ".jpg"),
        (b"\x89PNG\r\n\x1a\npngdata", ".png"),
        (b"GIF87agifdata", ".gif"),
        (b"GIF89agifdata", ".gif"),
        (b"RIFF\x00\x00\x00\x00WEBPvp8", ".webp"),
    ],
)
def test_download_plaintext_image_dat_detected_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    magic: bytes,
    expected_ext: str,
) -> None:
    msg_db = tmp_path / "msg_0.db"
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")])

    img_dir = msg_root / "attach" / ("a" * 32) / "2026-11" / "Img"
    img_dir.mkdir(parents=True)
    (img_dir / "photo_123.dat").write_bytes(magic)

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, ?), (2, '2026-11')", ("a" * 32,))
    hl.execute(
        "INSERT INTO image_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        ("cccc" * 8, 2, "photo_123.dat", 1, 2),
    )
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db, "Msg_11111111111111111111111111111111")
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (201, 3, 1794285100, 1, f'<msg><img md5="{"cccc" * 8}" /></msg>', 0),
    )
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 201, "wxid_alice", dest, capsys)
    assert code == 0
    assert out["items"] == [{
        "kind": "image",
        "status": "resolved",
        "reason": wechat_media.REASON_RESOLVED,
        "name": f"photo_123{expected_ext}",
        "path": str(dest / f"photo_123{expected_ext}"),
        "size": len(magic),
    }]
    assert (dest / f"photo_123{expected_ext}").read_bytes() == magic


@pytest.mark.parametrize(
    "payload",
    [
        b"\x07\x08V1\x08\x07encryptedv1",
        b"\x07\x08V2\x08\x07encryptedv2",
        b"UNKNOWN_MAGIC_BYTES_1234",
    ],
)
def test_download_encrypted_or_unrecognized_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: bytes,
) -> None:
    msg_db = tmp_path / "msg_0.db"
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")])

    img_dir = msg_root / "attach" / ("b" * 32) / "2026-11" / "Img"
    img_dir.mkdir(parents=True)
    (img_dir / "secret.dat").write_bytes(payload)

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, ?), (2, '2026-11')", ("b" * 32,))
    hl.execute(
        "INSERT INTO image_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        ("dddd" * 8, 4, "secret.dat", 1, 2),
    )
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db, "Msg_11111111111111111111111111111111")
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (202, 3, 1794285100, 1, f'<msg><img md5="{"dddd" * 8}" /></msg>', 0),
    )
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 202, "wxid_alice", dest, capsys)
    assert code == 1
    assert out["items"] == [{
        "kind": "image",
        "status": "encrypted_unsupported",
        "reason": wechat_media.REASON_ENCRYPTED_IMAGE,
    }]
    assert list(dest.iterdir()) == []


def test_download_video_and_thumbnail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = tmp_path / "msg_0.db"
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")])

    vid_dir = msg_root / "video" / "2026-11"
    vid_dir.mkdir(parents=True)
    v_md5 = "1234" * 8
    (vid_dir / f"{v_md5}.mp4").write_bytes(b"ftypmp4video")
    (vid_dir / f"{v_md5}.jpg").write_bytes(b"\xff\xd8\xff\xe0thumb")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    hl.execute(
        "INSERT INTO video_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        (v_md5, 3, f"{v_md5}.mp4", 1, 0),
    )
    hl.execute(
        "INSERT INTO video_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        (v_md5, 3, f"{v_md5}.jpg", 1, 0),
    )
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db, "Msg_11111111111111111111111111111111")
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (301, 43, 1794285100, 1, f'<msg><videomsg md5="{v_md5}" /></msg>', 0),
    )
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (302, 43, 1794285200, 1, '<msg><videomsg length="10" /></msg>', 0),
    )
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 301, "wxid_alice", dest, capsys)
    assert code == 0
    assert [item["kind"] for item in out["items"]] == ["video", "thumbnail"]
    assert all(item["status"] == "resolved" for item in out["items"])
    assert (dest / f"{v_md5}.mp4").read_bytes() == b"ftypmp4video"
    assert (dest / f"{v_md5}.jpg").read_bytes() == b"\xff\xd8\xff\xe0thumb"

    code2, out2 = _run_download(tmp_path, 302, "wxid_alice", dest, capsys)
    assert code2 == 1
    assert out2["items"] == [{
        "kind": "video",
        "status": "not_found",
        "reason": wechat_media.REASON_NO_MD5_IN_MESSAGE,
    }]


def test_download_image_md5_before_originsourcemd5_priority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = tmp_path / "msg_0.db"
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")])

    img_dir = msg_root / "attach" / ("c" * 32) / "2026-11" / "Img"
    img_dir.mkdir(parents=True)
    (img_dir / "primary.dat").write_bytes(b"\xff\xd8\xff\xe0primary")
    (img_dir / "origin.dat").write_bytes(b"\x89PNG\r\n\x1a\norigin")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, ?), (2, '2026-11')", ("c" * 32,))
    hl.execute(
        "INSERT INTO image_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        ("1111" * 8, 2, "primary.dat", 1, 2),
    )
    hl.execute(
        "INSERT INTO image_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        ("2222" * 8, 2, "origin.dat", 1, 2),
    )
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db, "Msg_11111111111111111111111111111111")
    # Both present -> md5 wins
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (401, 3, 1794285100, 1, f'<msg><img md5="{"1111" * 8}" originsourcemd5="{"2222" * 8}" /></msg>', 0),
    )
    # md5 has no hardlink row -> falls back to originsourcemd5
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (402, 3, 1794285200, 1, f'<msg><img md5="{"9999" * 8}" originsourcemd5="{"2222" * 8}" /></msg>', 0),
    )
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()

    code1, out1 = _run_download(tmp_path, 401, "wxid_alice", dest, capsys)
    assert code1 == 0
    assert out1["items"][0]["name"] == "primary.jpg"

    code2, out2 = _run_download(tmp_path, 402, "wxid_alice", dest, capsys)
    assert code2 == 0
    assert out2["items"][0]["name"] == "origin.png"


def test_download_not_found_states(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = tmp_path / "msg_0.db"
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")])

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    hl.execute(
        "INSERT INTO file_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        ("5555" * 8, 1, "gone.pdf", 1, 0),
    )
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db, "Msg_11111111111111111111111111111111")
    # Missing file on disk
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (501, 49, 1794285100, 1, f"<msg><appmsg><type>6</type><md5>{'5555' * 8}</md5></appmsg></msg>", 0),
    )
    # Missing hardlink row
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (502, 49, 1794285200, 1, f"<msg><appmsg><type>6</type><md5>{'6666' * 8}</md5></appmsg></msg>", 0),
    )
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()

    c1, o1 = _run_download(tmp_path, 501, "wxid_alice", dest, capsys)
    assert c1 == 1
    assert o1["items"] == [{"kind": "file", "status": "not_found", "reason": wechat_media.REASON_FILE_MISSING}]

    c2, o2 = _run_download(tmp_path, 502, "wxid_alice", dest, capsys)
    assert c2 == 1
    assert o2["items"] == [{"kind": "file", "status": "not_found", "reason": wechat_media.REASON_NO_HARDLINK_ROW}]

    # Missing hardlink.db
    _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=None, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")])
    c3, o3 = _run_download(tmp_path, 501, "wxid_alice", dest, capsys)
    assert c3 == 1
    assert o3["items"] == [{
        "kind": "file",
        "status": "not_found",
        "reason": wechat_media.REASON_HARDLINK_DB_UNAVAILABLE,
    }]


def test_download_duplicates_identical_vs_distinct_and_missing_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = tmp_path / "msg_0.db"
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")])

    d1 = msg_root / "file" / "2026-10"
    d2 = msg_root / "file" / "2026-11"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)

    # Identical content + one non-existing candidate row
    (d1 / "dup.txt").write_bytes(b"same content")
    (d2 / "dup.txt").write_bytes(b"same content")

    # Different content
    (d1 / "diff.txt").write_bytes(b"version A")
    (d2 / "diff.txt").write_bytes(b"version B")

    # One existing + one non-existing (must NOT be ambiguous)
    (d1 / "single_real.txt").write_bytes(b"only real")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-10'), (2, '2026-11'), (3, '2026-09')")
    for d_id in (1, 2, 3):
        hl.execute(
            "INSERT INTO file_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
            ("aaaa" * 8, 1, "dup.txt", d_id, 0),
        )
    for d_id in (1, 2):
        hl.execute(
            "INSERT INTO file_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
            ("bbbb" * 8, 1, "diff.txt", d_id, 0),
        )
    for d_id in (1, 3):
        hl.execute(
            "INSERT INTO file_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
            ("cccc" * 8, 1, "single_real.txt", d_id, 0),
        )
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db, "Msg_11111111111111111111111111111111")
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (601, 49, 1794285100, 1, f"<msg><appmsg><type>6</type><md5>{'aaaa' * 8}</md5></appmsg></msg>", 0),
    )
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (602, 49, 1794285200, 1, f"<msg><appmsg><type>6</type><md5>{'bbbb' * 8}</md5></appmsg></msg>", 0),
    )
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (603, 49, 1794285300, 1, f"<msg><appmsg><type>6</type><md5>{'cccc' * 8}</md5></appmsg></msg>", 0),
    )
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()

    c1, o1 = _run_download(tmp_path, 601, "wxid_alice", dest, capsys)
    assert c1 == 0
    assert len(o1["items"]) == 1
    assert o1["items"][0]["status"] == "resolved"

    c2, o2 = _run_download(tmp_path, 602, "wxid_alice", dest, capsys)
    assert c2 == 1
    assert o2["items"] == [{
        "kind": "file",
        "status": "ambiguous",
        "reason": wechat_media.REASON_MULTIPLE_DISTINCT_FILES,
    }]

    c3, o3 = _run_download(tmp_path, 603, "wxid_alice", dest, capsys)
    assert c3 == 0
    assert o3["items"][0]["status"] == "resolved"


def test_download_unsupported_and_group_prefix_xml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = tmp_path / "msg_0.db"
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(
        monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")], is_group=True
    )

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    (fdir / "group_doc.txt").write_bytes(b"group file")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    hl.execute(
        "INSERT INTO file_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        ("7777" * 8, 1, "group_doc.txt", 1, 0),
    )
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db, "Msg_11111111111111111111111111111111")
    # Text
    mconn.execute("INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)", (701, 1, 1794285100, 1, "wxid_alice:\nhello", 0))
    # Sticker
    mconn.execute("INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)", (702, 47, 1794285200, 1, "wxid_alice:\n<msg></msg>", 0))
    # Forwarded/compound card (type 49, appmsg type 19)
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (703, 49, 1794285300, 1, "wxid_alice:\n<msg><appmsg><type>19</type></appmsg></msg>", 0),
    )
    # Malformed XML
    mconn.execute("INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)", (704, 3, 1794285400, 1, "wxid_alice:\n<msg><broken", 0))
    # Undecompressible zstd bytes (WCDB_CT_message_content = 4)
    mconn.execute("INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)", (705, 3, 1794285500, 1, b"\x00\xffrawbytes", 4))
    # Group file message compressed with zstd and sender prefix
    cctx = zstandard.ZstdCompressor()
    xml_bytes = f"wxid_alice:\n<msg><appmsg><type>6</type><md5>{'7777' * 8}</md5></appmsg></msg>".encode()
    mconn.execute("INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)", (706, 49, 1794285600, 1, cctx.compress(xml_bytes), 4))
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()

    for mid, expected_reason in [
        (701, wechat_media.REASON_UNSUPPORTED_MESSAGE_TYPE),
        (702, wechat_media.REASON_UNSUPPORTED_MESSAGE_TYPE),
        (703, wechat_media.REASON_UNSUPPORTED_MESSAGE_TYPE),
        (704, wechat_media.REASON_MALFORMED_XML),
        (705, wechat_media.REASON_UNSUPPORTED_MESSAGE_TYPE),
    ]:
        code, out = _run_download(tmp_path, mid, "Logistics Group", dest, capsys)
        assert code == 1
        assert out["items"] == [{"kind": None, "status": "unsupported", "reason": expected_reason}]

    code_ok, out_ok = _run_download(tmp_path, 706, "1001@chatroom", dest, capsys)
    assert code_ok == 0
    assert out_ok["items"][0]["name"] == "group_doc.txt"


def test_download_path_traversal_and_symlink_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = tmp_path / "msg_0.db"
    hl_db = tmp_path / "hardlink.db"
    account_root, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")])

    outside_file = account_root / "secret.txt"
    outside_file.write_bytes(b"secret")

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11'), (2, '../..')")
    # .. in file_name
    hl.execute(
        "INSERT INTO file_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        ("8881" * 8, 1, "../../secret.txt", 1, 0),
    )
    # dir2id escapes msg/
    hl.execute(
        "INSERT INTO file_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        ("8882" * 8, 1, "secret.txt", 2, 0),
    )
    hl.commit()

    mconn = _make_msg_db(msg_db, "Msg_11111111111111111111111111111111")
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (801, 49, 1794285100, 1, f"<msg><appmsg><type>6</type><md5>{'8881' * 8}</md5></appmsg></msg>", 0),
    )
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (802, 49, 1794285200, 1, f"<msg><appmsg><type>6</type><md5>{'8882' * 8}</md5></appmsg></msg>", 0),
    )
    mconn.commit()

    dest = tmp_path / "out"
    dest.mkdir()

    c1, o1 = _run_download(tmp_path, 801, "wxid_alice", dest, capsys)
    assert c1 == 1
    assert o1["items"] == [{"kind": "file", "status": "not_found", "reason": wechat_media.REASON_PATH_OUTSIDE_MSG_ROOT}]

    c2, o2 = _run_download(tmp_path, 802, "wxid_alice", dest, capsys)
    assert c2 == 1
    assert o2["items"] == [{"kind": "file", "status": "not_found", "reason": wechat_media.REASON_PATH_OUTSIDE_MSG_ROOT}]

    # Symlink test (skip if OS refuses symlink creation)
    sym_path = fdir / "link.txt"
    try:
        os.symlink(outside_file, sym_path)
    except (OSError, NotImplementedError) as exc:
        hl.close()
        mconn.close()
        pytest.skip(f"OS refused symlink creation: {exc}")

    hl.execute(
        "INSERT INTO file_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        ("8883" * 8, 1, "link.txt", 1, 0),
    )
    hl.commit()
    hl.close()
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (803, 49, 1794285300, 1, f"<msg><appmsg><type>6</type><md5>{'8883' * 8}</md5></appmsg></msg>", 0),
    )
    mconn.commit()
    mconn.close()

    c3, o3 = _run_download(tmp_path, 803, "wxid_alice", dest, capsys)
    assert c3 == 1
    assert o3["items"] == [{"kind": "file", "status": "not_found", "reason": wechat_media.REASON_SYMLINK_REJECTED}]


def test_download_source_changed_mid_copy_and_no_overwrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = tmp_path / "msg_0.db"
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")])

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    src = fdir / "report.txt"
    src.write_bytes(b"initial bytes")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    hl.execute(
        "INSERT INTO file_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        ("9991" * 8, 1, "report.txt", 1, 0),
    )
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db, "Msg_11111111111111111111111111111111")
    mconn.execute(
        "INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)",
        (901, 49, 1794285100, 1, f"<msg><appmsg><type>6</type><md5>{'9991' * 8}</md5></appmsg></msg>", 0),
    )
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    # Existing destination file must not be overwritten
    (dest / "report.txt").write_bytes(b"existing keep me")

    c1, o1 = _run_download(tmp_path, 901, "wxid_alice", dest, capsys)
    assert c1 == 0
    assert o1["items"][0]["name"] == "report (1).txt"
    assert (dest / "report.txt").read_bytes() == b"existing keep me"
    assert (dest / "report (1).txt").read_bytes() == b"initial bytes"

    # Simulate source modified mid-copy via _copy_verified seam
    orig_fstat = os.fstat
    calls = {"n": 0}

    def fake_fstat(fd: int):
        res = orig_fstat(fd)
        calls["n"] += 1
        if calls["n"] == 2:
            # Return altered mtime_ns on post-copy fstat
            return os.stat_result((
                res.st_mode, res.st_ino, res.st_dev, res.st_nlink,
                res.st_uid, res.st_gid, res.st_size,
                res.st_atime, res.st_mtime + 10, res.st_ctime,
            ))
        return res

    monkeypatch.setattr(wechat_media.os, "fstat", fake_fstat)
    c2, o2 = _run_download(tmp_path, 901, "wxid_alice", dest, capsys)
    assert c2 == 1
    assert o2["items"] == [{
        "kind": "file",
        "status": "not_found",
        "reason": wechat_media.REASON_SOURCE_CHANGED,
    }]
    # Partial output report (2).txt must have been removed
    assert sorted(p.name for p in dest.iterdir()) == ["report (1).txt", "report.txt"]


def test_download_duplicate_local_id_across_dbs_and_arg_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db1 = tmp_path / "msg_0.db"
    db2 = tmp_path / "msg_1.db"
    hl_db = tmp_path / "hardlink.db"

    m1 = _make_msg_db(db1, "Msg_11111111111111111111111111111111")
    m1.execute("INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)", (50, 1, 1794285100, 1, "first", 0))
    m1.commit()
    m1.close()

    m2 = _make_msg_db(db2, "Msg_11111111111111111111111111111111")
    m2.execute("INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)", (50, 1, 1794285200, 1, "second", 0))
    m2.commit()
    m2.close()

    _setup_wechat_env(
        monkeypatch,
        tmp_path,
        hardlink_db=hl_db,
        msg_dbs=[(db1, "Msg_11111111111111111111111111111111"), (db2, "Msg_11111111111111111111111111111111")],
    )
    dest = tmp_path / "out"
    dest.mkdir()

    # Duplicate local_id across two dbs
    code, out = _run_download(tmp_path, 50, "wxid_alice", dest, capsys)
    assert code == 1
    assert "error" in out
    assert "2" in out["error"]
    assert str(db1) not in out["error"]
    assert str(db2) not in out["error"]

    # Message not found
    code2, out2 = _run_download(tmp_path, 9999, "wxid_alice", dest, capsys)
    assert code2 == 1
    assert "error" in out2

    # Conversation not found
    code3, out3 = _run_download(tmp_path, 50, "unknown_person", dest, capsys)
    assert code3 == 1
    assert "error" in out3

    # Invalid --to (missing dir)
    code4, out4 = _run_download(tmp_path, 50, "wxid_alice", tmp_path / "no_such_dir", capsys)
    assert code4 == 1
    assert "error" in out4


def test_download_temp_dir_deleted_after_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = tmp_path / "msg_0.db"
    _make_msg_db(msg_db, "Msg_11111111111111111111111111111111").close()
    _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=None, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")])

    created_dirs: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        created_dirs.append(d)
        return d

    monkeypatch.setattr(wechat.tempfile, "mkdtemp", spy_mkdtemp)

    dest = tmp_path / "out"
    dest.mkdir()
    code, _ = _run_download(tmp_path, 999, "wxid_alice", dest, capsys)
    assert code == 1
    assert len(created_dirs) == 1
    assert not Path(created_dirs[0]).exists()


def test_existing_messages_subcommand_json_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = tmp_path / "msg_0.db"
    mconn = _make_msg_db(msg_db, "Msg_11111111111111111111111111111111")
    mconn.execute("INSERT INTO Msg_11111111111111111111111111111111 VALUES (?, ?, ?, ?, ?, ?)", (1, 1, 1794285100, 1, "hello", 0))
    mconn.commit()
    mconn.close()

    _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=None, msg_dbs=[(msg_db, "Msg_11111111111111111111111111111111")])
    code = wechat.main([*settings_args(tmp_path), "messages", "wxid_alice", "--limit", "10"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out.keys()) == {"conversation", "truncated", "messages", "failures"}
    assert out["conversation"] == {"id": "wxid_alice", "name": "Alice Example", "is_group": False}
    assert out["truncated"] is False
    assert out["failures"] == []
    assert len(out["messages"]) == 1
    assert out["messages"][0]["id"] == 1
    assert out["messages"][0]["text"] == "hello"
