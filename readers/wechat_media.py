"""Deterministic resolution and safe export of locally cached WeChat 4.x media."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from wechat_cli.core.messages import _parse_message_content, _parse_xml_root, _split_msg_type

REASON_RESOLVED = "resolved"
REASON_HARDLINK_DB_UNAVAILABLE = "hardlink_db_unavailable"
REASON_NO_MD5_IN_MESSAGE = "no_md5_in_message"
REASON_NO_HARDLINK_ROW = "no_hardlink_row"
REASON_FILE_MISSING = "file_missing"
REASON_SOURCE_CHANGED = "source_changed_during_copy"
REASON_SYMLINK_REJECTED = "symlink_rejected"
REASON_PATH_OUTSIDE_MSG_ROOT = "path_outside_msg_root"
REASON_DIR2ID_UNRESOLVED = "dir2id_unresolved"
REASON_MULTIPLE_DISTINCT_FILES = "multiple_distinct_files"
REASON_ENCRYPTED_IMAGE = "encrypted_or_unrecognized_image"
REASON_UNSUPPORTED_MESSAGE_TYPE = "unsupported_message_type"
REASON_MALFORMED_XML = "malformed_message_xml"

_XML_LEAD_JUNK = "\ufeff \t\r\n"
_REPARSE_ATTR = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_CHUNK_SIZE = 1024 * 1024


def _item(kind: str | None, status: str, reason: str, **extra: object) -> dict[str, object]:
    return {"kind": kind, "status": status, "reason": reason, **extra}


def _detect_image_suffix(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    return None


def _is_safe_component(part: str | None) -> bool:
    if not part or not isinstance(part, str):
        return False
    if part in (".", ".."):
        return False
    if "/" in part or "\\" in part or ":" in part or "\x00" in part:
        return False
    return os.path.splitdrive(part)[0] == ""


def _is_inside_root(candidate: str, root: str) -> bool:
    norm_root = os.path.normcase(os.path.realpath(root))
    norm_cand = os.path.normcase(os.path.realpath(candidate))
    try:
        common = os.path.commonpath([norm_root, norm_cand])
    except ValueError:
        return False
    return common == norm_root and norm_cand != norm_root


def _is_reparse_or_symlink(st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_ATTR)


def _validate_candidate_path(msg_root: str, parts: list[str]) -> tuple[str | None, str | None]:
    for part in parts:
        if not _is_safe_component(part):
            return None, REASON_PATH_OUTSIDE_MSG_ROOT
    candidate = os.path.join(msg_root, *parts)

    cur = msg_root
    last_st: os.stat_result | None = None
    for part in parts:
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except OSError:
            return None, REASON_FILE_MISSING
        if _is_reparse_or_symlink(st):
            return None, REASON_SYMLINK_REJECTED
        last_st = st

    if not _is_inside_root(candidate, msg_root):
        return None, REASON_PATH_OUTSIDE_MSG_ROOT
    if last_st is None or not stat.S_ISREG(last_st.st_mode):
        return None, REASON_FILE_MISSING
    return candidate, None


def _hash_file(path: str) -> tuple[str, bytes]:
    hasher = hashlib.sha256()
    header = b""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            if len(header) < 12:
                header += chunk[: 12 - len(header)]
            hasher.update(chunk)
    return hasher.hexdigest(), header


def _open_unique_dest(dest_dir: str, dest_name: str):
    if not _is_safe_component(dest_name):
        raise ValueError(f"Unsafe destination filename: {dest_name}")
    original = Path(dest_name)
    number = 0
    while True:
        candidate_name = original.name if number == 0 else f"{original.stem} ({number}){original.suffix}"
        dest_path = os.path.join(dest_dir, candidate_name)
        if not _is_inside_root(dest_path, dest_dir):
            raise ValueError(f"Destination escapes --to directory: {candidate_name}")
        try:
            return candidate_name, dest_path, open(dest_path, "xb")
        except FileExistsError:
            number += 1


def _copy_verified(kind: str, src_path: str, dest_dir: str, dest_name: str, expected_digest: str) -> dict[str, object]:
    dest_path: str | None = None
    committed = False
    try:
        stat_before = os.stat(src_path)
        actual_name, dest_path, dst_f = _open_unique_dest(dest_dir, dest_name)
        copy_hasher = hashlib.sha256()
        total_bytes = 0
        with dst_f, open(src_path, "rb") as src_f:
            fstat_before = os.fstat(src_f.fileno())
            while True:
                chunk = src_f.read(_CHUNK_SIZE)
                if not chunk:
                    break
                dst_f.write(chunk)
                copy_hasher.update(chunk)
                total_bytes += len(chunk)
            fstat_after = os.fstat(src_f.fileno())
        stat_after = os.stat(src_path)

        stat_ok = (stat_before.st_size, stat_before.st_mtime_ns) == (stat_after.st_size, stat_after.st_mtime_ns)
        fstat_ok = (fstat_before.st_size, fstat_before.st_mtime_ns) == (fstat_after.st_size, fstat_after.st_mtime_ns)
        digest_ok = copy_hasher.hexdigest() == expected_digest
        if stat_ok and fstat_ok and digest_ok:
            committed = True
            return _item(
                kind,
                "resolved",
                REASON_RESOLVED,
                name=actual_name,
                path=dest_path,
                size=total_bytes,
            )
    except Exception:  # noqa: BLE001
        pass
    finally:
        if not committed and dest_path and os.path.exists(dest_path):
            os.unlink(dest_path)
    return _item(kind, "not_found", REASON_SOURCE_CHANGED)


def _resolve_group(
    kind: str,
    candidate_specs: list[tuple[list[str] | None, str]],
    msg_root: str,
    dest_dir: str,
    *,
    is_image: bool = False,
) -> dict[str, object]:
    if not candidate_specs:
        return _item(kind, "not_found", REASON_NO_HARDLINK_ROW)

    security_errors: list[str] = []
    valid_files: list[tuple[str, str]] = []
    had_dir2id_miss = False
    had_file_missing = False
    for rel_parts, raw_name in candidate_specs:
        if rel_parts is None:
            had_dir2id_miss = True
            continue
        path, err = _validate_candidate_path(msg_root, rel_parts)
        if path is not None:
            valid_files.append((path, raw_name))
        elif err in (REASON_SYMLINK_REJECTED, REASON_PATH_OUTSIDE_MSG_ROOT):
            security_errors.append(err)
        else:
            had_file_missing = True

    if not valid_files:
        if security_errors:
            return _item(kind, "not_found", security_errors[0])
        if had_dir2id_miss and not had_file_missing:
            return _item(kind, "not_found", REASON_DIR2ID_UNRESOLVED)
        return _item(kind, "not_found", REASON_FILE_MISSING)

    hashed: list[tuple[str, str, str, bytes]] = []
    for path, raw_name in valid_files:
        try:
            digest, header = _hash_file(path)
        except OSError:
            return _item(kind, "not_found", REASON_FILE_MISSING)
        hashed.append((path, raw_name, digest, header))

    distinct_digests = {d for _, _, d, _ in hashed}
    if len(distinct_digests) > 1:
        return _item(kind, "ambiguous", REASON_MULTIPLE_DISTINCT_FILES)

    chosen_path, chosen_raw_name, chosen_digest, chosen_header = hashed[0]
    if is_image:
        detected_ext = _detect_image_suffix(chosen_header)
        if detected_ext is None:
            return _item(kind, "encrypted_unsupported", REASON_ENCRYPTED_IMAGE)
        chosen_name = f"{Path(chosen_raw_name).stem}{detected_ext}"
    else:
        chosen_name = chosen_raw_name
    return _copy_verified(kind, chosen_path, dest_dir, chosen_name, chosen_digest)


def _open_hardlink_db(hardlink_db_path: str | None) -> sqlite3.Connection | None:
    if not hardlink_db_path or not os.path.isfile(hardlink_db_path):
        return None
    try:
        uri = Path(hardlink_db_path).resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None


def _load_dir2id(conn: sqlite3.Connection) -> dict[int, str]:
    return {rowid: name for rowid, name in conn.execute("SELECT rowid, username FROM dir2id")}


def _query_hardlink_rows(conn: sqlite3.Connection, table: str, md5_list: list[str]) -> list[tuple[int, str, int, int]]:
    candidates = list(dict.fromkeys(form for k in md5_list for form in (k.lower(), k.upper())))
    placeholders = ",".join("?" for _ in candidates)
    sql = f"SELECT type, file_name, dir1, dir2 FROM {table} WHERE md5 IN ({placeholders})"
    seen: set[tuple[int, str, int, int]] = set()
    rows: list[tuple[int, str, int, int]] = []
    for row in conn.execute(sql, candidates):
        if row not in seen:
            seen.add(row)
            rows.append(row)
    return rows


def _parse_message_xml(content: str | bytes | None, local_type: int, is_group: bool):
    if content is None or isinstance(content, bytes):
        return None, REASON_UNSUPPORTED_MESSAGE_TYPE
    if content.lstrip(_XML_LEAD_JUNK).startswith("<"):
        body = content.lstrip(_XML_LEAD_JUNK)
    else:
        _, body = _parse_message_content(content, local_type, is_group)
    root = _parse_xml_root(body.strip())
    if root is None:
        return None, REASON_MALFORMED_XML
    return root, None


def _find_tag(root, tag: str):
    return root if root.tag == tag else root.find(tag)


def _missing_key_item(kind: str, hardlink_db_path: str | None) -> list[dict[str, object]]:
    reason = REASON_NO_MD5_IN_MESSAGE if hardlink_db_path and os.path.isfile(hardlink_db_path) else REASON_HARDLINK_DB_UNAVAILABLE
    return [_item(kind, "not_found", reason)]


def _lookup_hardlink(
    hardlink_db_path: str | None,
    table: str,
    key_groups: list[list[str]],
) -> tuple[dict[int, str], list[tuple[int, str, int, int]]] | str:
    """Open hardlink.db read-only and query the first key group that yields rows, or return a failure reason."""
    conn = _open_hardlink_db(hardlink_db_path)
    if conn is None:
        return REASON_HARDLINK_DB_UNAVAILABLE
    try:
        with closing(conn):
            dir_map = _load_dir2id(conn)
            for keys in key_groups:
                if not keys:
                    continue
                rows = _query_hardlink_rows(conn, table, keys)
                if rows:
                    return dir_map, rows
            return dir_map, []
    except sqlite3.Error:
        return REASON_HARDLINK_DB_UNAVAILABLE


def resolve_and_download_media(
    hardlink_db_path: str | None,
    msg_root: str,
    local_type: int,
    content: str | bytes | None,
    dest_dir: str,
    *,
    is_group: bool = False,
) -> list[dict[str, object]]:
    """Resolve a WeChat message's cached media via hardlink.db and safely copy plaintext assets to dest_dir."""
    base_type, _ = _split_msg_type(local_type)
    if base_type not in (3, 43, 49):
        return [_item(None, "unsupported", REASON_UNSUPPORTED_MESSAGE_TYPE)]

    root, xml_err = _parse_message_xml(content, local_type, is_group)
    if root is None:
        return [_item(None, "unsupported", xml_err or REASON_MALFORMED_XML)]

    if base_type == 49:
        appmsg = _find_tag(root, "appmsg")
        if appmsg is None or (appmsg.findtext("type") or "").strip() != "6":
            return [_item(None, "unsupported", REASON_UNSUPPORTED_MESSAGE_TYPE)]
        file_md5 = (appmsg.findtext("md5") or "").strip().lower()
        if not file_md5:
            return _missing_key_item("file", hardlink_db_path)
        res = _lookup_hardlink(hardlink_db_path, "file_hardlink_info_v4", [[file_md5]])
        if isinstance(res, str):
            return [_item("file", "not_found", res)]
        dir_map, rows = res
        specs: list[tuple[list[str] | None, str]] = []
        for row_type, file_name, dir1, dir2 in rows:
            dir_name = dir_map.get(dir1) if row_type == 1 else dir_map.get(dir2) if row_type == 6 else None
            specs.append((["file", dir_name, file_name] if dir_name is not None else None, file_name))
        return [_resolve_group("file", specs, msg_root, dest_dir)]

    if base_type == 43:
        videomsg = _find_tag(root, "videomsg")
        if videomsg is None:
            return [_item(None, "unsupported", REASON_UNSUPPORTED_MESSAGE_TYPE)]
        keys = list(dict.fromkeys(
            k for k in (
                (videomsg.attrib.get("md5") or "").strip().lower(),
                (videomsg.attrib.get("rawmd5") or "").strip().lower(),
            ) if k
        ))
        if not keys:
            return _missing_key_item("video", hardlink_db_path)
        res = _lookup_hardlink(hardlink_db_path, "video_hardlink_info_v4", [keys])
        if isinstance(res, str):
            return [_item("video", "not_found", res)]
        dir_map, rows = res
        if not rows:
            return [_item("video", "not_found", REASON_NO_HARDLINK_ROW)]

        video_specs: list[tuple[list[str] | None, str]] = []
        thumb_specs: list[tuple[list[str] | None, str]] = []
        for _, file_name, dir1, _ in rows:
            dir_name = dir_map.get(dir1)
            rel = ["video", dir_name, file_name] if dir_name is not None else None
            if isinstance(file_name, str) and file_name.lower().endswith(".jpg"):
                thumb_specs.append((rel, file_name))
            else:
                video_specs.append((rel, file_name))

        items: list[dict[str, object]] = []
        if video_specs:
            items.append(_resolve_group("video", video_specs, msg_root, dest_dir))
        if thumb_specs:
            items.append(_resolve_group("thumbnail", thumb_specs, msg_root, dest_dir))
        return items

    # base_type == 3 (image)
    img = _find_tag(root, "img")
    if img is None:
        return [_item(None, "unsupported", REASON_UNSUPPORTED_MESSAGE_TYPE)]
    img_md5 = (img.attrib.get("md5") or "").strip().lower()
    origin_md5 = (img.attrib.get("originsourcemd5") or "").strip().lower()
    if not img_md5 and not origin_md5:
        return _missing_key_item("image", hardlink_db_path)
    key_groups = [g for g in ([img_md5] if img_md5 else [], [origin_md5] if origin_md5 else []) if g]
    res = _lookup_hardlink(hardlink_db_path, "image_hardlink_info_v4", key_groups)
    if isinstance(res, str):
        return [_item("image", "not_found", res)]
    dir_map, rows = res
    specs = []
    for _, file_name, dir1, dir2 in rows:
        d1, d2 = dir_map.get(dir1), dir_map.get(dir2)
        specs.append((["attach", d1, d2, "Img", file_name] if d1 is not None and d2 is not None else None, file_name))
    return [_resolve_group("image", specs, msg_root, dest_dir, is_image=True)]
