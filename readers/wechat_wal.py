"""WAL-consistent opaque-page snapshot helpers for encrypted WeChat SQLite databases.

Adapted from MIT-licensed `freanocank/wecom-agent` (`decrypt/wal_snapshot.py`),
which was ported from `tzwkb/wechat-decrypt` (MIT License).
"""

from __future__ import annotations

import ctypes
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

    if not wal:
        return main
    if len(main) % page_size != 0:
        raise ValueError(f"Main database size {len(main)} is not a multiple of page size {page_size}")
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

    pages: dict[int, bytes] = {
        idx + 1: main[idx * page_size : (idx + 1) * page_size]
        for idx in range(len(main) // page_size)
    }
    current_db_size = len(pages)
    committed = False

    frame_size = WAL_FRAME_HEADER_SIZE + page_size
    pending: dict[int, bytes] = {}
    offset = WAL_HEADER_SIZE
    while offset + frame_size <= len(wal):
        pgno, commit_size, f_salt1, f_salt2, f_cksum1, f_cksum2 = struct.unpack(
            ">IIIIII", wal[offset : offset + WAL_FRAME_HEADER_SIZE]
        )
        if (f_salt1, f_salt2) != (hdr_salt1, hdr_salt2):
            break

        frame_header = wal[offset : offset + WAL_FRAME_HEADER_SIZE]
        payload = wal[offset + WAL_FRAME_HEADER_SIZE : offset + frame_size]
        s0, s1 = _wal_checksum(frame_header[:8], big_endian_words, s0, s1)
        s0, s1 = _wal_checksum(payload, big_endian_words, s0, s1)
        if (s0, s1) != (f_cksum1, f_cksum2):
            raise ValueError(f"Invalid WAL frame checksum at offset {offset}")

        if pgno == 0:
            raise ValueError(f"Invalid WAL frame page number 0 at offset {offset}")

        pending[pgno] = payload
        if commit_size != 0:
            for p_num, p_bytes in pending.items():
                if p_num <= commit_size:
                    pages[p_num] = p_bytes
            for existing_pgno in [k for k in pages if k > commit_size]:
                del pages[existing_pgno]
            for required_pgno in range(1, commit_size + 1):
                if required_pgno not in pages:
                    raise ValueError(f"Committed WAL references missing page {required_pgno}")
            current_db_size = commit_size
            committed = True
            pending.clear()

        offset += frame_size

    if not committed:
        return main

    return b"".join(pages[p] for p in range(1, current_db_size + 1))


_READ_CHUNK_SIZE = 1024 * 1024
_WIN32_GENERIC_READ = 0x80000000
_WIN32_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_WIN32_OPEN_EXISTING = 3
_WIN32_FILE_ATTRIBUTE_NORMAL = 0x00000080


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
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def read_file_shared(path: Path) -> bytes:
    """Read a file allowing concurrent writers (Win32 FILE_SHARE_* on Windows)."""
    if sys.platform != "win32":
        return path.read_bytes()

    from ctypes import wintypes

    kernel32 = _win32_kernel32()
    invalid_handle = wintypes.HANDLE(-1).value
    handle = kernel32.CreateFileW(
        str(path),
        _WIN32_GENERIC_READ,
        _WIN32_FILE_SHARE_ALL,
        None,
        _WIN32_OPEN_EXISTING,
        _WIN32_FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    chunks: list[bytes] = []
    buf = ctypes.create_string_buffer(_READ_CHUNK_SIZE)
    bytes_read = wintypes.DWORD(0)
    try:
        while True:
            ok = kernel32.ReadFile(handle, buf, _READ_CHUNK_SIZE, ctypes.byref(bytes_read), None)
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
            if bytes_read.value == 0:
                break
            chunks.append(buf.raw[: bytes_read.value])
    finally:
        kernel32.CloseHandle(handle)

    return b"".join(chunks)


def _stat_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def read_snapshot(
    source: Path,
    attempts: int = 3,
    reader: Callable[[Path], bytes] | None = None,
) -> bytes:
    """Read a WAL-consistent standalone snapshot of an encrypted SQLite database."""
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, got {attempts}")

    wal_path = Path(f"{source}-wal")
    actual_reader = reader or read_file_shared

    for _ in range(attempts):
        before_db = _stat_signature(source)
        if before_db is None:
            raise FileNotFoundError(f"WeChat database not found: {source}")
        before_wal = _stat_signature(wal_path)

        main_bytes = actual_reader(source)
        if before_wal is not None:
            try:
                wal_bytes = actual_reader(wal_path)
            except FileNotFoundError:
                continue
        else:
            wal_bytes = b""

        after_db = _stat_signature(source)
        after_wal = _stat_signature(wal_path)
        if (before_db, before_wal) == (after_db, after_wal):
            return merge_committed_pages(main_bytes, wal_bytes)

    raise RuntimeError(f"WeChat database changed during {attempts} snapshot attempts: {source}")
