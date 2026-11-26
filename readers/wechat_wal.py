"""WAL-consistent opaque-page snapshot helpers for encrypted WeChat SQLite databases.

Adapted from MIT-licensed `freanocank/wecom-agent` (`decrypt/wal_snapshot.py`),
which was ported from `tzwkb/wechat-decrypt` (MIT License).
"""

from __future__ import annotations

import ctypes
import functools
import os
import struct
import sys
from collections.abc import Callable
from pathlib import Path

DEFAULT_PAGE_SIZE = 4096
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_MAGIC_LE = 0x377F0682
WAL_MAGIC_BE = 0x377F0683
WAL_VERSION = 3007000


def _wal_checksum(data: bytes, big_endian_words: bool, s0: int = 0, s1: int = 0) -> tuple[int, int]:
    fmt = ">II" if big_endian_words else "<II"
    for (x0, x1) in struct.iter_unpack(fmt, data):
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def merge_committed_pages(main: bytes, wal: bytes, page_size: int = DEFAULT_PAGE_SIZE) -> bytes:
    """Merge committed SQLite WAL frames onto opaque encrypted main database pages."""
    if page_size <= 0 or page_size % 8 != 0:
        raise ValueError(f"Invalid SQLite page size: {page_size}")
    if len(main) % page_size != 0:
        raise ValueError(f"Main database size {len(main)} is not a multiple of page size {page_size}")
    if not wal:
        return main
    if len(wal) < WAL_HEADER_SIZE:
        raise ValueError(f"Truncated WAL header: {len(wal)} bytes")

    magic, version, wal_page_size, _ckpt_seq, hdr_salt1, hdr_salt2, hdr_cksum1, hdr_cksum2 = struct.unpack(
        ">IIIIIIII", wal[:WAL_HEADER_SIZE]
    )
    if magic == WAL_MAGIC_LE:
        big_endian_words = False
    elif magic == WAL_MAGIC_BE:
        big_endian_words = True
    else:
        raise ValueError(f"Invalid WAL magic: {magic:#x}")

    if version != WAL_VERSION:
        raise ValueError(f"Unsupported WAL version: {version}")
    if wal_page_size != page_size:
        raise ValueError(f"WAL page size {wal_page_size} does not match expected {page_size}")

    s0, s1 = _wal_checksum(wal[:24], big_endian_words)
    if (s0, s1) != (hdr_cksum1, hdr_cksum2):
        raise ValueError("Invalid WAL header checksum")

    # Pages 1..main_valid may still come from `main`; a committed shrink lowers it so a
    # later regrowth cannot resurrect truncated pages. `overrides` holds committed WAL pages.
    db_size = main_valid = len(main) // page_size
    overrides: dict[int, bytes] = {}
    pending: dict[int, bytes] = {}

    frame_size = WAL_FRAME_HEADER_SIZE + page_size
    offset = WAL_HEADER_SIZE
    while offset + frame_size <= len(wal):
        frame_header = wal[offset : offset + WAL_FRAME_HEADER_SIZE]
        pgno, commit_size, f_salt1, f_salt2, f_cksum1, f_cksum2 = struct.unpack(">IIIIII", frame_header)
        if (f_salt1, f_salt2) != (hdr_salt1, hdr_salt2):
            break

        payload = wal[offset + WAL_FRAME_HEADER_SIZE : offset + frame_size]
        s0, s1 = _wal_checksum(frame_header[:8], big_endian_words, s0, s1)
        s0, s1 = _wal_checksum(payload, big_endian_words, s0, s1)
        if (s0, s1) != (f_cksum1, f_cksum2):
            raise ValueError(f"Invalid WAL frame checksum at offset {offset}")

        if pgno == 0:
            raise ValueError(f"Invalid WAL frame page number 0 at offset {offset}")

        pending[pgno] = payload
        if commit_size != 0:
            overrides.update((p, data) for p, data in pending.items() if p <= commit_size)
            pending.clear()
            if commit_size < db_size:
                main_valid = min(main_valid, commit_size)
                for stale in [p for p in overrides if p > commit_size]:
                    del overrides[stale]
            db_size = commit_size

        offset += frame_size

    # Only the final committed state must be complete: after a checkpoint has truncated
    # main, an earlier commit in the same WAL may reference pages that no longer exist.
    for required in range(main_valid + 1, db_size + 1):
        if required not in overrides:
            raise ValueError(f"Committed WAL references missing page {required}")

    out = bytearray(main[: main_valid * page_size])
    out.extend(bytes((db_size - main_valid) * page_size))
    for p, data in overrides.items():
        out[(p - 1) * page_size : p * page_size] = data
    return bytes(out)


_WIN32_GENERIC_READ = 0x80000000
_WIN32_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_WIN32_OPEN_EXISTING = 3
_WIN32_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WIN32_INVALID_HANDLE = ctypes.c_void_p(-1).value


@functools.cache
def _win32_kernel32():
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def read_file_shared(path: Path) -> bytes:
    """Read a file allowing concurrent writers (Win32 FILE_SHARE_* on Windows)."""
    if sys.platform != "win32":
        return path.read_bytes()

    import msvcrt

    kernel32 = _win32_kernel32()
    handle = kernel32.CreateFileW(
        str(path),
        _WIN32_GENERIC_READ,
        _WIN32_FILE_SHARE_ALL,
        None,
        _WIN32_OPEN_EXISTING,
        _WIN32_FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle is None or handle == _WIN32_INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as f:  # closing the file object closes the Win32 handle
        return f.read()


def _stat_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def read_snapshot(
    source: Path,
    attempts: int = 3,
    reader: Callable[[Path], bytes] = read_file_shared,
) -> bytes:
    """Read a WAL-consistent standalone snapshot of an encrypted SQLite database."""
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, got {attempts}")

    wal_path = Path(f"{source}-wal")

    for _ in range(attempts):
        before_db = _stat_signature(source)
        if before_db is None:
            raise FileNotFoundError(f"WeChat database not found: {source}")
        before_wal = _stat_signature(wal_path)

        # Read the WAL on both sides of the main read. A checkpoint that rewrites main and
        # resets the WAL in place during the main read can keep (size, mtime_ns, ino) equal
        # at coarse timestamp resolution, but it changes the WAL contents (new salt).
        try:
            wal_before = reader(wal_path) if before_wal is not None else b""
            main_bytes = reader(source)
            wal_bytes = reader(wal_path) if before_wal is not None else b""
        except FileNotFoundError:
            continue

        after_db = _stat_signature(source)
        after_wal = _stat_signature(wal_path)
        if (before_db, before_wal) == (after_db, after_wal) and wal_before == wal_bytes:
            return merge_committed_pages(main_bytes, wal_bytes)

    raise RuntimeError(f"WeChat database changed during {attempts} snapshot attempts: {source}")
