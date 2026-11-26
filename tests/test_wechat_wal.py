"""Tests for readers.wechat_wal: WAL-consistent encrypted page snapshots."""

from __future__ import annotations

import sqlite3
import struct
import sys
from pathlib import Path

import pytest

from readers.wechat_wal import merge_committed_pages, read_file_shared, read_snapshot

PAGE_SIZE = 4096
WAL_MAGIC_LE = 0x377F0682
WAL_MAGIC_BE = 0x377F0683
WAL_VERSION = 3007000


def _wal_checksum(data: bytes, big_endian_words: bool, s0: int = 0, s1: int = 0) -> tuple[int, int]:
    fmt = ">II" if big_endian_words else "<II"
    for offset in range(0, len(data), 8):
        x0, x1 = struct.unpack_from(fmt, data, offset)
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def _build_wal(
    frames: list[tuple[int, int, bytes, tuple[int, int] | None]],
    *,
    magic: int = WAL_MAGIC_LE,
    version: int = WAL_VERSION,
    page_size: int = PAGE_SIZE,
    checkpoint_seq: int = 1,
    salt: tuple[int, int] = (0x11223344, 0x55667788),
    trailing_bytes: bytes = b"",
) -> bytes:
    big_endian_words = magic == WAL_MAGIC_BE
    header_prefix = struct.pack(">IIIIII", magic, version, page_size, checkpoint_seq, salt[0], salt[1])
    s0, s1 = _wal_checksum(header_prefix, big_endian_words)
    out = bytearray(header_prefix + struct.pack(">II", s0, s1))
    for pgno, commit_size, page_data, frame_salt in frames:
        actual_salt = salt if frame_salt is None else frame_salt
        frame_prefix = struct.pack(">IIII", pgno, commit_size, actual_salt[0], actual_salt[1])
        s0, s1 = _wal_checksum(frame_prefix[:8], big_endian_words, s0, s1)
        s0, s1 = _wal_checksum(page_data, big_endian_words, s0, s1)
        out.extend(frame_prefix)
        out.extend(struct.pack(">II", s0, s1))
        out.extend(page_data)
    out.extend(trailing_bytes)
    return bytes(out)


def _page(fill: int) -> bytes:
    return bytes([fill]) * PAGE_SIZE


@pytest.mark.parametrize("magic", [WAL_MAGIC_LE, WAL_MAGIC_BE])
def test_merge_committed_pages_applies_committed_and_drops_uncommitted(magic: int) -> None:
    main = _page(1) + _page(2)
    wal = _build_wal(
        [
            (1, 0, _page(10), None),
            (2, 2, _page(20), None),
            (1, 0, _page(99), None),  # uncommitted trailing frame
        ],
        magic=magic,
        trailing_bytes=b"\x00\x01\x02\x03",
    )

    merged = merge_committed_pages(main, wal)
    assert merged == _page(10) + _page(20)


def test_merge_committed_pages_empty_wal_returns_main() -> None:
    main = _page(1) + _page(2)
    assert merge_committed_pages(main, b"") == main


@pytest.mark.parametrize("wal", [b"", None])
def test_merge_committed_pages_rejects_unaligned_main_with_or_without_wal(wal: bytes | None) -> None:
    wal_bytes = _build_wal([(1, 1, _page(10), None)]) if wal is None else wal
    with pytest.raises(ValueError, match="multiple of page size"):
        merge_committed_pages(_page(1) + b"x", wal_bytes)


def test_merge_committed_pages_stops_at_salt_mismatch() -> None:
    main = _page(1) + _page(2)
    wal = _build_wal(
        [
            (1, 2, _page(10), None),
            (2, 2, _page(99), (0xDEAD0001, 0xDEAD0002)),  # stale generation salt
        ]
    )
    assert merge_committed_pages(main, wal) == _page(10) + _page(2)


@pytest.mark.parametrize(
    "corrupt_fn",
    [
        lambda w: w[:10],  # truncated header
        lambda w: struct.pack(">I", 0x12345678) + w[4:],  # bad magic
        lambda w: w[:4] + struct.pack(">I", 3007001) + w[8:],  # bad version
        lambda w: w[:8] + struct.pack(">I", 1024) + w[12:],  # bad page size
        lambda w: w[:24] + bytes([w[24] ^ 0xFF]) + w[25:],  # bad header checksum
        lambda w: w[: 32 + 16] + bytes([w[32 + 16] ^ 0xFF]) + w[32 + 17 :],  # bad frame checksum
        lambda w: w[: 32 + 24] + bytes([w[32 + 24] ^ 0xFF]) + w[32 + 25 :],  # corrupted payload
    ],
)
def test_merge_committed_pages_rejects_corrupt_wal(corrupt_fn) -> None:
    main = _page(1)
    wal = _build_wal([(1, 1, _page(10), None)])
    with pytest.raises(ValueError):
        merge_committed_pages(main, corrupt_fn(wal))


def test_merge_committed_pages_rejects_page_zero() -> None:
    main = _page(1)
    wal = _build_wal([(0, 1, _page(10), None)])
    with pytest.raises(ValueError, match="page number 0"):
        merge_committed_pages(main, wal)


def test_merge_committed_pages_growth_and_shrink_without_resurrection() -> None:
    main = _page(1) + _page(2) + _page(3)
    # Grow to 5, then shrink to 2
    wal_shrink = _build_wal(
        [
            (4, 0, _page(4), None),
            (5, 5, _page(5), None),
            (2, 2, _page(20), None),
        ]
    )
    assert merge_committed_pages(main, wal_shrink) == _page(1) + _page(20)

    # Shrink to 2, then regrow to 4 writing only page 4 -> page 3 must not resurrect
    wal_resurrect = _build_wal(
        [
            (2, 2, _page(20), None),
            (4, 4, _page(40), None),
        ]
    )
    with pytest.raises(ValueError, match="missing page 3"):
        merge_committed_pages(main, wal_resurrect)

    # Shrink to 2, then regrow to 4 writing both page 3 and page 4
    wal_regrow = _build_wal(
        [
            (2, 2, _page(20), None),
            (3, 0, _page(30), None),
            (4, 4, _page(40), None),
        ]
    )
    assert merge_committed_pages(main, wal_regrow) == _page(1) + _page(20) + _page(30) + _page(40)


def _xor_bytes(data: bytes, key: int = 0x5A) -> bytes:
    return bytes(b ^ key for b in data)


def _recompute_wal_with_xor(wal: bytes, key: int = 0x5A) -> bytes:
    """Re-encode a real WAL as an encrypting writer would: XOR each payload, rebuild the checksum chain."""
    magic, version, page_size, ckpt, salt1, salt2, _c1, _c2 = struct.unpack(">IIIIIIII", wal[:32])
    frame_size = 24 + page_size
    frames = []
    offset = 32
    while offset + frame_size <= len(wal):
        pgno, commit_size, f_salt1, f_salt2 = struct.unpack(">IIII", wal[offset : offset + 16])
        if (f_salt1, f_salt2) != (salt1, salt2):
            break
        frames.append((pgno, commit_size, _xor_bytes(wal[offset + 24 : offset + frame_size], key), None))
        offset += frame_size
    return _build_wal(
        frames,
        magic=magic,
        version=version,
        page_size=page_size,
        checkpoint_seq=ckpt,
        salt=(salt1, salt2),
        trailing_bytes=wal[offset:],
    )


def test_real_sqlite_wal_with_reversible_page_cipher(tmp_path: Path) -> None:
    db_path = tmp_path / "real.db"
    wal_path = tmp_path / "real.db-wal"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA page_size = 4096")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("PRAGMA cache_size = 1")
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO notes (id, body) VALUES (1, 'committed')")
        conn.commit()

        wal_after_commit = wal_path.read_bytes()

        # Start an uncommitted transaction that spills frames into the WAL
        conn.execute("BEGIN")
        for idx in range(2, 40):
            conn.execute("INSERT INTO notes (id, body) VALUES (?, ?)", (idx, f"uncommitted-{idx}-" + ("x" * 200)))

        raw_main = db_path.read_bytes()
        raw_wal = wal_path.read_bytes()
        assert len(raw_wal) > len(wal_after_commit)
    finally:
        conn.close()

    enc_main = _xor_bytes(raw_main)
    enc_wal = _recompute_wal_with_xor(raw_wal)

    merged_enc = merge_committed_pages(enc_main, enc_wal, page_size=PAGE_SIZE)
    decrypted = _xor_bytes(merged_enc)

    restored_path = tmp_path / "restored.db"
    restored_path.write_bytes(decrypted)
    with sqlite3.connect(restored_path) as rconn:
        rows = rconn.execute("SELECT id, body FROM notes ORDER BY id").fetchall()
    assert rows == [(1, "committed")]


def test_read_snapshot_without_wal(tmp_path: Path) -> None:
    db_path = tmp_path / "only.db"
    db_path.write_bytes(_page(1) + _page(2))
    assert read_snapshot(db_path) == _page(1) + _page(2)


def test_read_snapshot_retries_on_mid_read_change(tmp_path: Path) -> None:
    db_path = tmp_path / "msg.db"
    wal_path = tmp_path / "msg.db-wal"
    db_path.write_bytes(_page(1))
    wal_path.write_bytes(_build_wal([(1, 1, _page(10), None)]))

    reads = 0

    def mutating_reader(path: Path) -> bytes:
        nonlocal reads
        data = path.read_bytes()
        if path == wal_path and reads < 2:
            reads += 1
            wal_path.write_bytes(_build_wal([(1, 1, _page(20 + reads), None)]))
        return data

    result = read_snapshot(db_path, attempts=3, reader=mutating_reader)
    assert reads == 2
    assert result == _page(22)


def test_read_snapshot_retries_when_wal_appears_mid_read(tmp_path: Path) -> None:
    db_path = tmp_path / "appear.db"
    wal_path = tmp_path / "appear.db-wal"
    db_path.write_bytes(_page(1))

    triggered = False

    def appearing_reader(path: Path) -> bytes:
        nonlocal triggered
        data = path.read_bytes()
        if path == db_path and not triggered:
            triggered = True
            wal_path.write_bytes(_build_wal([(1, 1, _page(33), None)]))
        return data

    result = read_snapshot(db_path, attempts=3, reader=appearing_reader)
    assert triggered is True
    assert result == _page(33)


def test_read_snapshot_raises_on_persistent_change(tmp_path: Path) -> None:
    db_path = tmp_path / "busy.db"
    wal_path = tmp_path / "busy.db-wal"
    db_path.write_bytes(_page(1))
    wal_path.write_bytes(_build_wal([(1, 1, _page(10), None)]))

    counter = 0

    def always_changing_reader(path: Path) -> bytes:
        nonlocal counter
        data = path.read_bytes()
        if path == db_path:
            counter += 1
            db_path.write_bytes(_page(counter + 1) * (counter + 1))
        return data

    with pytest.raises(RuntimeError, match="changed"):
        read_snapshot(db_path, attempts=3, reader=always_changing_reader)
    assert counter == 3


def test_read_snapshot_rejects_corrupt_wal_without_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "corrupt.db"
    wal_path = tmp_path / "corrupt.db-wal"
    db_path.write_bytes(_page(1))
    wal_path.write_bytes(b"not-a-wal")

    with pytest.raises(ValueError):
        read_snapshot(db_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 shared-read test")
def test_read_file_shared_while_open_for_write(tmp_path: Path) -> None:
    from ctypes import wintypes

    from readers.wechat_wal import (
        _WIN32_FILE_ATTRIBUTE_NORMAL,
        _WIN32_FILE_SHARE_ALL,
        _WIN32_OPEN_EXISTING,
        _win32_kernel32,
    )

    target = tmp_path / "locked.db"
    payload = _page(7)
    target.write_bytes(payload)

    kernel32 = _win32_kernel32()
    generic_write = 0x40000000
    handle = kernel32.CreateFileW(
        str(target),
        generic_write,
        _WIN32_FILE_SHARE_ALL,
        None,
        _WIN32_OPEN_EXISTING,
        _WIN32_FILE_ATTRIBUTE_NORMAL,
        None,
    )
    assert handle != wintypes.HANDLE(-1).value
    try:
        assert read_file_shared(target) == payload
    finally:
        kernel32.CloseHandle(handle)
