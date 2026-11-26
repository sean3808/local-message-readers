"""wechat-reader download tests for hardlink resolution, fail-closed statuses, and safe copy."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import zstandard
from conftest import settings_args
from wechat_cli.core.messages import _XML_UNSAFE_RE

from readers import wechat, wechat_media


def _msg_table(username: str) -> str:
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


MSG_TABLE = _msg_table("wxid_alice")
MSG_TABLE_GROUP = _msg_table("1001@chatroom")


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _file_xml(md5: str, prefix: str = "") -> str:
    return f"{prefix}<msg><appmsg><type>6</type><md5>{md5}</md5></appmsg></msg>"


def _add_file_link(
    hl: sqlite3.Connection,
    md5: str,
    file_name: str,
    *,
    row_type: int = 1,
    dir1: int = 1,
    dir2: int = 0,
) -> None:
    hl.execute(
        "INSERT INTO file_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
        (md5, row_type, file_name, dir1, dir2),
    )


def _add_msg(
    conn: sqlite3.Connection,
    local_id: int,
    local_type: int,
    ts: int,
    content: str | bytes,
    *,
    ct: int = 0,
    table: str = MSG_TABLE,
) -> None:
    conn.execute(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?)", (local_id, local_type, ts, 1, content, ct))


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


def _make_msg_db(db_path: Path, table_name: str = MSG_TABLE) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)")
    conn.execute("INSERT OR IGNORE INTO Name2Id (rowid, user_name) VALUES (1, 'wxid_alice')")
    if table_name:
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


def _shard_db_path(tmp_path: Path, idx: int = 0) -> Path:
    return tmp_path / "wxid_account" / "db_storage" / "message" / f"message_{idx}.db"


def _setup_wechat_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    hardlink_db: Path | None = None,
    msg_dbs: list[tuple[Path, str]] | None = None,
    extra_msg_db_keys: list[str] | None = None,
    cache_overrides: dict[str, str | None] | None = None,
    is_group: bool = False,
    db_dir_override: str | None = None,
) -> tuple[Path, Path]:
    account_root = tmp_path / "wxid_account"
    db_dir = account_root / "db_storage"
    msg_root = account_root / "msg"
    db_dir.mkdir(parents=True, exist_ok=True)
    msg_root.mkdir(parents=True, exist_ok=True)

    contexts = []
    msg_db_keys = []
    key_to_path: dict[str, str | None] = {}
    if msg_dbs is None:
        default_db = _shard_db_path(tmp_path, 0)
        _make_msg_db(default_db, MSG_TABLE_GROUP if is_group else MSG_TABLE).close()
        msg_dbs = [(default_db, MSG_TABLE_GROUP if is_group else MSG_TABLE)]

    conv_id = "1001@chatroom" if is_group else "wxid_alice"
    conv_name = "Logistics Group" if is_group else "Alice Example"
    for idx, (db_path, table_name) in enumerate(msg_dbs):
        rel_key = os.path.join("message", f"message_{idx}.db")
        disk_path = db_dir / rel_key
        if db_path.exists() and disk_path != db_path and not disk_path.exists():
            disk_path.parent.mkdir(parents=True, exist_ok=True)
            disk_path.write_bytes(db_path.read_bytes())
        msg_db_keys.append(rel_key)
        key_to_path[rel_key] = str(db_path)
        contexts.append({
            "db_path": str(db_path),
            "table_name": table_name,
            "username": conv_id,
            "display_name": conv_name,
            "is_group": is_group,
        })

    if extra_msg_db_keys:
        msg_db_keys.extend(extra_msg_db_keys)
    if cache_overrides:
        key_to_path.update(cache_overrides)

    class FakeCache:
        def get(self, key: str) -> str | None:
            if key == os.path.join("hardlink", "hardlink.db"):
                return str(hardlink_db) if hardlink_db is not None else None
            if key in key_to_path:
                return key_to_path[key]
            return str(msg_dbs[0][0])

    fake_app = SimpleNamespace(
        cache=FakeCache(),
        decrypted_dir=str(tmp_path),
        db_dir=db_dir_override or str(db_dir),
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
    msg_time: str | None = None,
) -> tuple[int, dict]:
    argv = [
        *settings_args(tmp_path),
        "download",
        str(msg_id),
        "--conversation",
        conv,
        "--to",
        str(dest),
    ]
    if msg_time is not None:
        argv.extend(["--time", msg_time])
    code = wechat.main(argv)
    out = json.loads(capsys.readouterr().out)
    return code, out


def test_download_file_type_1_and_type_6(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    file1_dir = msg_root / "file" / "2026-11"
    file1_dir.mkdir(parents=True)
    (file1_dir / "invoice.pdf").write_bytes(b"%PDF-1.4\nhello")

    file6_dir = msg_root / "file" / "2026-10"
    file6_dir.mkdir(parents=True)
    (file6_dir / "packing.xlsx").write_bytes(b"PK\x03\x04excel")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11'), (2, '2026-10'), (3, 'wrong_dir')")
    _add_file_link(hl, "aaaa" * 8, "invoice.pdf", row_type=1, dir1=1, dir2=3)
    _add_file_link(hl, "bbbb" * 8, "packing.xlsx", row_type=6, dir1=3, dir2=2)
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 101, 49, 1794285100, _file_xml("AAAA" * 8))
    _add_msg(mconn, 102, 49, 1794285200, _file_xml("bbbb" * 8))
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()

    code1, out1 = _run_download(tmp_path, 101, "Alice Example", dest, capsys)
    assert code1 == 0
    assert out1["conversation"] == {"id": "wxid_alice", "name": "Alice Example", "is_group": False}
    assert out1["message_id"] == 101
    assert out1["time"] == _iso(1794285100)
    assert out1["timestamp"] == 1794285100
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
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

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

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 201, 3, 1794285100, f'<msg><img md5="{"cccc" * 8}" /></msg>')
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
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

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

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 202, 3, 1794285100, f'<msg><img md5="{"dddd" * 8}" /></msg>')
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
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

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

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 301, 43, 1794285100, f'<msg><videomsg md5="{v_md5}" /></msg>')
    _add_msg(mconn, 302, 43, 1794285200, '<msg><videomsg length="10" /></msg>')
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
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

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

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 401, 3, 1794285100, f'<msg><img md5="{"1111" * 8}" originsourcemd5="{"2222" * 8}" /></msg>')
    _add_msg(mconn, 402, 3, 1794285200, f'<msg><img md5="{"9999" * 8}" originsourcemd5="{"2222" * 8}" /></msg>')
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
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "5555" * 8, "gone.pdf")
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 501, 49, 1794285100, _file_xml("5555" * 8))
    _add_msg(mconn, 502, 49, 1794285200, _file_xml("6666" * 8))
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

    _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=None, msg_dbs=[(msg_db, MSG_TABLE)])
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
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    d1 = msg_root / "file" / "2026-10"
    d2 = msg_root / "file" / "2026-11"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)

    (d1 / "dup.txt").write_bytes(b"same content")
    (d2 / "dup.txt").write_bytes(b"same content")
    (d1 / "diff.txt").write_bytes(b"version A")
    (d2 / "diff.txt").write_bytes(b"version B")
    (d1 / "single_real.txt").write_bytes(b"only real")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-10'), (2, '2026-11'), (3, '2026-09')")
    for d_id in (1, 2, 3):
        _add_file_link(hl, "aaaa" * 8, "dup.txt", dir1=d_id)
    for d_id in (1, 2):
        _add_file_link(hl, "bbbb" * 8, "diff.txt", dir1=d_id)
    for d_id in (1, 3):
        _add_file_link(hl, "cccc" * 8, "single_real.txt", dir1=d_id)
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 601, 49, 1794285100, _file_xml("aaaa" * 8))
    _add_msg(mconn, 602, 49, 1794285200, _file_xml("bbbb" * 8))
    _add_msg(mconn, 603, 49, 1794285300, _file_xml("cccc" * 8))
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


def test_download_image_mixed_plaintext_and_v2_is_ambiguous_and_identical_pairs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    d1 = msg_root / "attach" / ("d" * 32) / "2026-10" / "Img"
    d2 = msg_root / "attach" / ("d" * 32) / "2026-11" / "Img"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)

    # Mixed plaintext JPEG + V2 .dat
    (d1 / "mixed.dat").write_bytes(b"\xff\xd8\xff\xe0jpegdata")
    (d2 / "mixed.dat").write_bytes(b"\x07\x08V2\x08\x07encrypted")

    # Two identical V2 .dat
    (d1 / "both_v2.dat").write_bytes(b"\x07\x08V2\x08\x07samev2")
    (d2 / "both_v2.dat").write_bytes(b"\x07\x08V2\x08\x07samev2")

    # Two identical JPEGs
    (d1 / "both_jpg.dat").write_bytes(b"\xff\xd8\xff\xe0samejpg")
    (d2 / "both_jpg.dat").write_bytes(b"\xff\xd8\xff\xe0samejpg")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, ?), (2, '2026-10'), (3, '2026-11')", ("d" * 32,))
    for d2_id in (2, 3):
        hl.execute(
            "INSERT INTO image_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
            ("e111" * 8, 2, "mixed.dat", 1, d2_id),
        )
        hl.execute(
            "INSERT INTO image_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
            ("e222" * 8, 2, "both_v2.dat", 1, d2_id),
        )
        hl.execute(
            "INSERT INTO image_hardlink_info_v4 (md5, type, file_name, dir1, dir2) VALUES (?, ?, ?, ?, ?)",
            ("e333" * 8, 2, "both_jpg.dat", 1, d2_id),
        )
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 620, 3, 1794285100, f'<msg><img md5="{"e111" * 8}" /></msg>')
    _add_msg(mconn, 621, 3, 1794285200, f'<msg><img md5="{"e222" * 8}" /></msg>')
    _add_msg(mconn, 622, 3, 1794285300, f'<msg><img md5="{"e333" * 8}" /></msg>')
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()

    c_mix, o_mix = _run_download(tmp_path, 620, "wxid_alice", dest, capsys)
    assert c_mix == 1
    assert o_mix["items"] == [{
        "kind": "image",
        "status": "ambiguous",
        "reason": wechat_media.REASON_MULTIPLE_DISTINCT_FILES,
    }]
    assert list(dest.iterdir()) == []

    c_v2, o_v2 = _run_download(tmp_path, 621, "wxid_alice", dest, capsys)
    assert c_v2 == 1
    assert o_v2["items"] == [{
        "kind": "image",
        "status": "encrypted_unsupported",
        "reason": wechat_media.REASON_ENCRYPTED_IMAGE,
    }]
    assert list(dest.iterdir()) == []

    c_jpg, o_jpg = _run_download(tmp_path, 622, "wxid_alice", dest, capsys)
    assert c_jpg == 0
    assert len(o_jpg["items"]) == 1
    assert o_jpg["items"][0]["status"] == "resolved"
    assert o_jpg["items"][0]["name"] == "both_jpg.jpg"
    assert (dest / "both_jpg.jpg").read_bytes() == b"\xff\xd8\xff\xe0samejpg"


def test_download_hash_file_oserror_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    d1 = msg_root / "file" / "2026-10"
    d2 = msg_root / "file" / "2026-11"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)
    (d1 / "dup.txt").write_bytes(b"same")
    (d2 / "dup.txt").write_bytes(b"same")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-10'), (2, '2026-11')")
    _add_file_link(hl, "abcd" * 8, "dup.txt", dir1=1)
    _add_file_link(hl, "abcd" * 8, "dup.txt", dir1=2)
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 610, 49, 1794285100, _file_xml("abcd" * 8))
    mconn.commit()
    mconn.close()

    orig_hash = wechat_media._hash_file
    calls = {"n": 0}

    def fail_first_hash(path: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("unreadable candidate")
        return orig_hash(path)

    monkeypatch.setattr(wechat_media, "_hash_file", fail_first_hash)

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 610, "wxid_alice", dest, capsys)
    assert code == 1
    assert out["items"] == [{"kind": "file", "status": "not_found", "reason": wechat_media.REASON_FILE_MISSING}]
    assert list(dest.iterdir()) == []


def test_download_unsupported_and_group_prefix_xml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(
        monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE_GROUP)], is_group=True
    )

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    (fdir / "group_doc.txt").write_bytes(b"group file")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "7777" * 8, "group_doc.txt")
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db, MSG_TABLE_GROUP)
    _add_msg(mconn, 701, 1, 1794285100, "wxid_alice:\nhello", table=MSG_TABLE_GROUP)
    _add_msg(mconn, 702, 47, 1794285200, "wxid_alice:\n<msg></msg>", table=MSG_TABLE_GROUP)
    _add_msg(mconn, 703, 49, 1794285300, "wxid_alice:\n<msg><appmsg><type>19</type></appmsg></msg>", table=MSG_TABLE_GROUP)
    _add_msg(mconn, 704, 3, 1794285400, "wxid_alice:\n<msg><broken", table=MSG_TABLE_GROUP)
    _add_msg(mconn, 705, 3, 1794285500, b"\x00\xffrawbytes", ct=4, table=MSG_TABLE_GROUP)
    cctx = zstandard.ZstdCompressor()
    _add_msg(
        mconn, 706, 49, 1794285600,
        cctx.compress(_file_xml("7777" * 8, prefix="wxid_alice:\n").encode()),
        ct=4, table=MSG_TABLE_GROUP,
    )
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


def test_download_integer_message_content_returns_unsupported_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=None, msg_dbs=[(msg_db, MSG_TABLE)])

    mconn = _make_msg_db(msg_db)
    mconn.execute(f"INSERT INTO {MSG_TABLE} VALUES (?, ?, ?, ?, ?, ?)", (708, 3, 1794285100, 1, 12345, 0))
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 708, "wxid_alice", dest, capsys)
    assert code == 1
    assert "error" not in out
    assert out["items"] == [{
        "kind": None,
        "status": "unsupported",
        "reason": wechat_media.REASON_UNSUPPORTED_MESSAGE_TYPE,
    }]


@pytest.mark.parametrize("lead", ["", "\ufeff", " \ufeff\n"])
def test_download_group_xml_without_prefix_containing_colon_newline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], lead: str
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(
        monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE_GROUP)], is_group=True
    )

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    (fdir / "no_prefix.txt").write_bytes(b"no prefix body")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "7778" * 8, "no_prefix.txt")
    hl.commit()
    hl.close()

    xml_with_colon_nl = f"{lead}<msg><appmsg><type>6</type><des>note:\nline2</des><md5>{'7778' * 8}</md5></appmsg></msg>"
    mconn = _make_msg_db(msg_db, MSG_TABLE_GROUP)
    _add_msg(mconn, 707, 49, 1794285700, xml_with_colon_nl, table=MSG_TABLE_GROUP)
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 707, "1001@chatroom", dest, capsys)
    assert code == 0
    assert out["items"][0]["name"] == "no_prefix.txt"
    assert (dest / "no_prefix.txt").read_bytes() == b"no prefix body"


def test_download_unsafe_xml_doctype_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=None, msg_dbs=[(msg_db, MSG_TABLE)])

    unsafe_xml = '<!DOCTYPE foo [<!ENTITY xxe "bar">]><msg><img md5="aaaa" /></msg>'
    assert _XML_UNSAFE_RE.search(unsafe_xml)

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 750, 3, 1794285100, unsafe_xml)
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 750, "wxid_alice", dest, capsys)
    assert code == 1
    assert out["items"] == [{"kind": None, "status": "unsupported", "reason": wechat_media.REASON_MALFORMED_XML}]


def test_download_path_traversal_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    account_root, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    (account_root / "secret.txt").write_bytes(b"secret")
    (msg_root / "file" / "2026-11").mkdir(parents=True)

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11'), (2, '../..')")
    _add_file_link(hl, "8881" * 8, "../../secret.txt", dir1=1)
    _add_file_link(hl, "8882" * 8, "secret.txt", dir1=2)
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 801, 49, 1794285100, _file_xml("8881" * 8))
    _add_msg(mconn, 802, 49, 1794285200, _file_xml("8882" * 8))
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()

    c1, o1 = _run_download(tmp_path, 801, "wxid_alice", dest, capsys)
    assert c1 == 1
    assert o1["items"] == [{"kind": "file", "status": "not_found", "reason": wechat_media.REASON_PATH_OUTSIDE_MSG_ROOT}]

    c2, o2 = _run_download(tmp_path, 802, "wxid_alice", dest, capsys)
    assert c2 == 1
    assert o2["items"] == [{"kind": "file", "status": "not_found", "reason": wechat_media.REASON_PATH_OUTSIDE_MSG_ROOT}]
    assert list(dest.iterdir()) == []


def test_download_symlink_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    account_root, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    outside_file = account_root / "secret.txt"
    outside_file.write_bytes(b"secret")

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    try:
        os.symlink(outside_file, fdir / "link.txt")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"OS refused symlink creation: {exc}")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "8883" * 8, "link.txt")
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 803, 49, 1794285300, _file_xml("8883" * 8))
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    c3, o3 = _run_download(tmp_path, 803, "wxid_alice", dest, capsys)
    assert c3 == 1
    assert o3["items"] == [{"kind": "file", "status": "not_found", "reason": wechat_media.REASON_SYMLINK_REJECTED}]
    assert list(dest.iterdir()) == []


def test_download_junction_reparse_point_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        import _winapi
        create_junction = _winapi.CreateJunction
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"_winapi.CreateJunction unavailable: {exc}")

    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    account_root, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    target_dir = account_root / "outside_dir"
    target_dir.mkdir()
    (target_dir / "payload.txt").write_bytes(b"junction target")

    file_root = msg_root / "file"
    file_root.mkdir(parents=True)
    create_junction(str(target_dir), str(file_root / "2026-11"))

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "8884" * 8, "payload.txt")
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 804, 49, 1794285400, _file_xml("8884" * 8))
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 804, "wxid_alice", dest, capsys)
    assert code == 1
    assert out["items"] == [{"kind": "file", "status": "not_found", "reason": wechat_media.REASON_SYMLINK_REJECTED}]
    assert list(dest.iterdir()) == []


def test_download_dir2id_unresolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    hl = _make_hardlink_db(hl_db)
    _add_file_link(hl, "8885" * 8, "orphan.pdf", dir1=99)
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 805, 49, 1794285500, _file_xml("8885" * 8))
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 805, "wxid_alice", dest, capsys)
    assert code == 1
    assert out["items"] == [{"kind": "file", "status": "not_found", "reason": wechat_media.REASON_DIR2ID_UNRESOLVED}]
    assert list(dest.iterdir()) == []


def test_download_source_changed_mid_copy_and_no_overwrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    (fdir / "report.txt").write_bytes(b"initial bytes")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "9991" * 8, "report.txt")
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 901, 49, 1794285100, _file_xml("9991" * 8))
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "report.txt").write_bytes(b"existing keep me")

    c1, o1 = _run_download(tmp_path, 901, "wxid_alice", dest, capsys)
    assert c1 == 0
    assert o1["items"][0]["name"] == "report (1).txt"
    assert (dest / "report.txt").read_bytes() == b"existing keep me"
    assert (dest / "report (1).txt").read_bytes() == b"initial bytes"

    orig_fstat = os.fstat
    calls = {"n": 0}

    def fake_fstat(fd: int):
        res = orig_fstat(fd)
        calls["n"] += 1
        if calls["n"] == 2:
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
    assert sorted(p.name for p in dest.iterdir()) == ["report (1).txt", "report.txt"]


def test_download_source_path_stat_changed_mid_copy_with_stable_fstat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    src = fdir / "report.txt"
    src.write_bytes(b"initial bytes")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "9992" * 8, "report.txt")
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 902, 49, 1794285100, _file_xml("9992" * 8))
    mconn.commit()
    mconn.close()

    orig_stat = os.stat
    calls = {"n": 0}

    def fake_stat(path, *args, **kwargs):
        res = orig_stat(path, *args, **kwargs)
        if os.path.normcase(str(path)) == os.path.normcase(str(src)):
            calls["n"] += 1
            if calls["n"] == 2:
                return os.stat_result((
                    res.st_mode, res.st_ino, res.st_dev, res.st_nlink,
                    res.st_uid, res.st_gid, res.st_size + 5,
                    res.st_atime, res.st_mtime, res.st_ctime,
                ))
        return res

    monkeypatch.setattr(wechat_media.os, "stat", fake_stat)

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 902, "wxid_alice", dest, capsys)
    assert code == 1
    assert out["items"] == [{
        "kind": "file",
        "status": "not_found",
        "reason": wechat_media.REASON_SOURCE_CHANGED,
    }]
    assert list(dest.iterdir()) == []


def test_download_arg_and_lookup_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    m1 = _make_msg_db(msg_db)
    _add_msg(m1, 50, 1, 1794285100, "hello")
    m1.commit()
    m1.close()

    _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=tmp_path / "hardlink.db", msg_dbs=[(msg_db, MSG_TABLE)])
    dest = tmp_path / "out"
    dest.mkdir()

    code2, out2 = _run_download(tmp_path, 9999, "wxid_alice", dest, capsys)
    assert code2 == 1
    assert "error" in out2

    code3, out3 = _run_download(tmp_path, 50, "unknown_person", dest, capsys)
    assert code3 == 1
    assert "error" in out3

    code4, out4 = _run_download(tmp_path, 50, "wxid_alice", tmp_path / "no_such_dir", capsys)
    assert code4 == 1
    assert "error" in out4


def test_download_temp_dir_deleted_after_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hl_fixture = tmp_path / "fixture_hl.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_fixture, msg_dbs=[(msg_db, MSG_TABLE)])

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    (fdir / "clean.pdf").write_bytes(b"%PDF-clean")

    hl = _make_hardlink_db(hl_fixture)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "cc01" * 8, "clean.pdf")
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 999, 49, 1794285100, _file_xml("cc01" * 8))
    mconn.commit()
    mconn.close()

    created_dirs: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        created_dirs.append(d)
        return d

    monkeypatch.setattr(wechat.tempfile, "mkdtemp", spy_mkdtemp)

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 999, "wxid_alice", dest, capsys)
    assert code == 0
    assert out["items"][0]["name"] == "clean.pdf"
    assert len(created_dirs) == 1
    assert not Path(created_dirs[0]).exists()
    assert list(dest.rglob("*hardlink*")) == []
    assert [p for p in tmp_path.rglob("*hardlink*")] == []


def test_download_hidden_unopened_shard_in_msg_db_keys_fails_closed_without_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hidden_db = _shard_db_path(tmp_path, 1)
    hidden_db.parent.mkdir(parents=True, exist_ok=True)
    hidden_db.write_bytes(b"encrypted-on-disk")
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(
        monkeypatch,
        tmp_path,
        hardlink_db=hl_db,
        msg_dbs=[(msg_db, MSG_TABLE)],
        extra_msg_db_keys=[os.path.join("message", "message_1.db")],
        cache_overrides={os.path.join("message", "message_1.db"): None},
    )

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    (fdir / "one.pdf").write_bytes(b"%PDF-one")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "dd01" * 8, "one.pdf")
    hl.commit()
    hl.close()

    ts = 1794285100
    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 77, 49, ts, _file_xml("dd01" * 8))
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()

    c_no_time, o_no_time = _run_download(tmp_path, 77, "wxid_alice", dest, capsys)
    assert c_no_time == 1
    assert "--time" in o_no_time["error"]
    assert len(o_no_time["failures"]) == 1
    assert list(dest.iterdir()) == []

    c_with_time, o_with_time = _run_download(tmp_path, 77, "wxid_alice", dest, capsys, msg_time=_iso(ts))
    assert c_with_time == 0
    assert o_with_time["items"][0]["name"] == "one.pdf"


def test_download_six_shards_absent_empty_and_populated_and_missing_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hl_db = tmp_path / "hardlink.db"
    shards = [_shard_db_path(tmp_path, i) for i in range(6)]
    # Shards 0,1: absent table; 2,3: empty table; 4: populated; 5: absent table
    _make_msg_db(shards[0], table_name="").close()
    _make_msg_db(shards[1], table_name="").close()
    _make_msg_db(shards[2], MSG_TABLE).close()
    _make_msg_db(shards[3], MSG_TABLE).close()
    m4 = _make_msg_db(shards[4], MSG_TABLE)
    ts = 1794285100
    _add_msg(m4, 81, 49, ts, _file_xml("aa11" * 8))
    m4.commit()
    m4.close()
    _make_msg_db(shards[5], table_name="").close()

    _, msg_root = _setup_wechat_env(
        monkeypatch,
        tmp_path,
        hardlink_db=hl_db,
        msg_dbs=[(p, MSG_TABLE) for p in shards],
        # Listed key whose file does not exist on disk must be skipped cleanly
        extra_msg_db_keys=[os.path.join("message", "message_missing_disk.db")],
        cache_overrides={os.path.join("message", "message_missing_disk.db"): None},
    )

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    (fdir / "six.pdf").write_bytes(b"%PDF-six")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "aa11" * 8, "six.pdf")
    hl.commit()
    hl.close()

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 81, "wxid_alice", dest, capsys)
    assert code == 0
    assert out["failures"] == []
    assert out["items"][0]["name"] == "six.pdf"


def test_download_corrupt_shard_fails_closed_without_time_and_records_with_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good_db = _shard_db_path(tmp_path, 0)
    corrupt_db = _shard_db_path(tmp_path, 1)
    corrupt_db.parent.mkdir(parents=True, exist_ok=True)
    corrupt_db.write_bytes(b"not a sqlite database at all")
    hl_db = tmp_path / "hardlink.db"

    ts = 1794285100
    m0 = _make_msg_db(good_db, MSG_TABLE)
    _add_msg(m0, 82, 49, ts, _file_xml("aa22" * 8))
    m0.commit()
    m0.close()

    _, msg_root = _setup_wechat_env(
        monkeypatch,
        tmp_path,
        hardlink_db=hl_db,
        msg_dbs=[(good_db, MSG_TABLE), (corrupt_db, MSG_TABLE)],
    )
    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    (fdir / "corrupt_ok.pdf").write_bytes(b"%PDF-ok")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "aa22" * 8, "corrupt_ok.pdf")
    hl.commit()
    hl.close()

    dest = tmp_path / "out"
    dest.mkdir()

    c1, o1 = _run_download(tmp_path, 82, "wxid_alice", dest, capsys)
    assert c1 == 1
    assert len(o1["failures"]) == 1
    assert "message_1.db" in o1["failures"][0]
    assert str(corrupt_db) not in o1["failures"][0]
    assert list(dest.iterdir()) == []

    c2, o2 = _run_download(tmp_path, 82, "wxid_alice", dest, capsys, msg_time=_iso(ts))
    assert c2 == 0
    assert len(o2["failures"]) == 1
    assert o2["items"][0]["name"] == "corrupt_ok.pdf"


def test_download_shard_stat_permission_error_fails_closed_without_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good_db = _shard_db_path(tmp_path, 0)
    perm_db = _shard_db_path(tmp_path, 1)
    hl_db = tmp_path / "hardlink.db"

    ts = 1794285100
    m0 = _make_msg_db(good_db, MSG_TABLE)
    _add_msg(m0, 83, 49, ts, _file_xml("aa33" * 8))
    m0.commit()
    m0.close()
    _make_msg_db(perm_db, MSG_TABLE).close()

    _, msg_root = _setup_wechat_env(
        monkeypatch,
        tmp_path,
        hardlink_db=hl_db,
        msg_dbs=[(good_db, MSG_TABLE), (perm_db, MSG_TABLE)],
    )
    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    (fdir / "perm_ok.pdf").write_bytes(b"%PDF-perm")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "aa33" * 8, "perm_ok.pdf")
    hl.commit()
    hl.close()

    orig_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        if os.path.normcase(str(path)) == os.path.normcase(str(perm_db)):
            raise PermissionError("access denied")
        return orig_stat(path, *args, **kwargs)

    monkeypatch.setattr(wechat.os, "stat", fake_stat)

    dest = tmp_path / "out"
    dest.mkdir()

    c1, o1 = _run_download(tmp_path, 83, "wxid_alice", dest, capsys)
    assert c1 == 1
    assert len(o1["failures"]) == 1
    assert "message_1.db" in o1["failures"][0]
    assert list(dest.iterdir()) == []

    c2, o2 = _run_download(tmp_path, 83, "wxid_alice", dest, capsys, msg_time=_iso(ts))
    assert c2 == 0
    assert len(o2["failures"]) == 1
    assert o2["items"][0]["name"] == "perm_ok.pdf"


def test_download_hardlink_db_in_hash_and_space_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    special_dir = tmp_path / "dir #1 with space"
    special_dir.mkdir()
    hl_db = special_dir / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    (fdir / "hashpath.pdf").write_bytes(b"%PDF-hash")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "ee01" * 8, "hashpath.pdf")
    hl.commit()
    hl.close()

    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 88, 49, 1794285100, _file_xml("ee01" * 8))
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 88, "wxid_alice", dest, capsys)
    assert code == 0
    assert out["items"][0]["name"] == "hashpath.pdf"


def test_download_rejects_destination_inside_account_folder_and_trailing_slash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    _make_msg_db(msg_db).close()
    account_root, msg_root = _setup_wechat_env(
        monkeypatch,
        tmp_path,
        hardlink_db=None,
        msg_dbs=[(msg_db, MSG_TABLE)],
        db_dir_override=str(tmp_path / "wxid_account" / "db_storage") + os.sep,
    )

    for bad_dest in (account_root, msg_root):
        code, out = _run_download(tmp_path, 1, "wxid_alice", bad_dest, capsys)
        assert code == 1
        assert "account" in out["error"].lower()


def test_download_rejects_destination_via_account_and_msg_junctions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        import _winapi
        create_junction = _winapi.CreateJunction
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"_winapi.CreateJunction unavailable: {exc}")

    msg_db = _shard_db_path(tmp_path, 0)
    _make_msg_db(msg_db).close()
    account_root, _ = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=None, msg_dbs=[(msg_db, MSG_TABLE)])

    # Remove plain msg dir and replace with junction <account>/msg -> tmp/real_msg
    msg_link = account_root / "msg"
    msg_link.rmdir()
    real_msg = tmp_path / "real_msg"
    real_msg.mkdir()
    (real_msg / "x").mkdir()
    create_junction(str(real_msg), str(msg_link))

    # Also create junction <account>/msg/file -> tmp/real_file
    real_file = tmp_path / "real_file"
    real_file.mkdir()
    create_junction(str(real_file), str(msg_link / "file"))

    for bad_dest in (real_msg / "x", real_file):
        code, out = _run_download(tmp_path, 1, "wxid_alice", bad_dest, capsys, msg_time="2026-11-10 10:00:00")
        assert code == 1
        assert "account" in out["error"].lower()


def test_download_nested_type6_appmsg_in_compound_card_unsupported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(msg_db, MSG_TABLE)])

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    (fdir / "nested.pdf").write_bytes(b"%PDF-nested")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "ff01" * 8, "nested.pdf")
    hl.commit()
    hl.close()

    nested_xml = (
        "<msg><wrapper><appmsg><type>6</type>"
        f"<md5>{'ff01' * 8}</md5>"
        "</appmsg></wrapper></msg>"
    )
    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 99, 49, 1794285100, nested_xml)
    mconn.commit()
    mconn.close()

    dest = tmp_path / "out"
    dest.mkdir()
    code, out = _run_download(tmp_path, 99, "wxid_alice", dest, capsys)
    assert code == 1
    assert out["items"] == [{
        "kind": None,
        "status": "unsupported",
        "reason": wechat_media.REASON_UNSUPPORTED_MESSAGE_TYPE,
    }]
    assert list(dest.iterdir()) == []


def test_existing_messages_subcommand_json_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    msg_db = _shard_db_path(tmp_path, 0)
    mconn = _make_msg_db(msg_db)
    _add_msg(mconn, 1, 1, 1794285100, "hello")
    mconn.commit()
    mconn.close()

    _setup_wechat_env(monkeypatch, tmp_path, hardlink_db=None, msg_dbs=[(msg_db, MSG_TABLE)])
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


def _setup_two_shard_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path, Path]:
    db1 = _shard_db_path(tmp_path, 0)
    db2 = _shard_db_path(tmp_path, 1)
    hl_db = tmp_path / "hardlink.db"
    _, msg_root = _setup_wechat_env(
        monkeypatch, tmp_path, hardlink_db=hl_db, msg_dbs=[(db1, MSG_TABLE), (db2, MSG_TABLE)]
    )

    fdir = msg_root / "file" / "2026-11"
    fdir.mkdir(parents=True)
    (fdir / "jan_quote.pdf").write_bytes(b"%PDF-jan")
    (fdir / "feb_quote.pdf").write_bytes(b"%PDF-feb")
    (fdir / "same_doc.pdf").write_bytes(b"%PDF-same")

    hl = _make_hardlink_db(hl_db)
    hl.execute("INSERT INTO dir2id (rowid, username) VALUES (1, '2026-11')")
    _add_file_link(hl, "aa01" * 8, "jan_quote.pdf")
    _add_file_link(hl, "aa02" * 8, "feb_quote.pdf")
    _add_file_link(hl, "aa03" * 8, "same_doc.pdf")
    hl.commit()
    hl.close()

    dest = tmp_path / "out"
    dest.mkdir()
    return db1, db2, dest


def test_download_sharded_local_id_ambiguous_returns_alternatives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db1, db2, dest = _setup_two_shard_files(monkeypatch, tmp_path)
    ts1, ts2 = 1794285100, 1794285200

    m1 = _make_msg_db(db1)
    _add_msg(m1, 10, 49, ts1, _file_xml("aa01" * 8))
    m1.commit()
    m1.close()

    m2 = _make_msg_db(db2)
    _add_msg(m2, 10, 49, ts2, _file_xml("aa02" * 8))
    m2.commit()
    m2.close()

    code, out = _run_download(tmp_path, 10, "wxid_alice", dest, capsys)
    assert code == 1
    assert "--time" in out["error"]
    assert str(db1) not in out["error"]
    assert str(db2) not in out["error"]
    assert [a["timestamp"] for a in out["alternatives"]] == [ts1, ts2]
    assert all(set(a.keys()) == {"time", "timestamp", "type"} for a in out["alternatives"])
    assert list(dest.iterdir()) == []


def test_download_sharded_local_id_disambiguated_by_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db1, db2, dest = _setup_two_shard_files(monkeypatch, tmp_path)
    ts1, ts2 = 1794285100, 1794285200

    m1 = _make_msg_db(db1)
    _add_msg(m1, 10, 49, ts1, _file_xml("aa01" * 8))
    m1.commit()
    m1.close()

    m2 = _make_msg_db(db2)
    _add_msg(m2, 10, 49, ts2, _file_xml("aa02" * 8))
    m2.commit()
    m2.close()

    c1, o1 = _run_download(tmp_path, 10, "wxid_alice", dest, capsys, msg_time=_iso(ts1))
    assert c1 == 0
    assert o1["items"][0]["name"] == "jan_quote.pdf"
    assert (dest / "jan_quote.pdf").read_bytes() == b"%PDF-jan"
    assert not (dest / "feb_quote.pdf").exists()

    naive_2 = datetime.fromtimestamp(ts2).strftime("%Y-%m-%d %H:%M:%S")
    c2, o2 = _run_download(tmp_path, 10, "wxid_alice", dest, capsys, msg_time=naive_2)
    assert c2 == 0
    assert o2["items"][0]["name"] == "feb_quote.pdf"
    assert (dest / "feb_quote.pdf").read_bytes() == b"%PDF-feb"

    c_none, o_none = _run_download(tmp_path, 10, "wxid_alice", dest, capsys, msg_time="2020-01-01 00:00:00")
    assert c_none == 1
    assert "Message not found" in o_none["error"]


def test_download_sharded_identical_duplicate_deduped_and_same_time_tie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db1, db2, dest = _setup_two_shard_files(monkeypatch, tmp_path)
    ts_same, ts_tie = 1794285300, 1794285400

    m1 = _make_msg_db(db1)
    _add_msg(m1, 20, 49, ts_same, _file_xml("aa03" * 8))
    _add_msg(m1, 30, 49, ts_tie, _file_xml("aa01" * 8))
    m1.commit()
    m1.close()

    m2 = _make_msg_db(db2)
    _add_msg(m2, 20, 49, ts_same, _file_xml("aa03" * 8))
    _add_msg(m2, 30, 49, ts_tie, _file_xml("aa02" * 8))
    m2.commit()
    m2.close()

    c_dup, o_dup = _run_download(tmp_path, 20, "wxid_alice", dest, capsys)
    assert c_dup == 0
    assert o_dup["items"][0]["name"] == "same_doc.pdf"

    c_tie, o_tie = _run_download(tmp_path, 30, "wxid_alice", dest, capsys, msg_time=_iso(ts_tie))
    assert c_tie == 1
    assert "same time" in o_tie["error"]
    assert [a["timestamp"] for a in o_tie["alternatives"]] == [ts_tie, ts_tie]
