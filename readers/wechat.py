"""WeChat reader: normalizes data read by the upstream tool (wechat-cli) into the GLOSSARY.md vocabulary and prints JSON to stdout.

Takes the raw rows the upstream query returns directly, without parsing its pre-formatted strings (issue #3).
Read-only: exposes no send path (ADR-0002). Decrypted artifacts are written to a temp directory dedicated to this run and deleted at exit, leaving no plaintext on disk.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import string
import sys
import tempfile
from contextlib import closing
from datetime import datetime

from wechat_cli.core.contacts import get_contact_names, get_self_username
from wechat_cli.core.context import AppContext
from wechat_cli.core.db_cache import DBCache
from wechat_cli.core.messages import (
    _build_message_filters,
    _format_message_text,
    _is_safe_msg_table_name,
    _iter_table_contexts,
    _load_name2id_maps,
    _load_search_contexts_from_db,
    _parse_message_content,
    _query_messages,
    decompress_content,
    format_msg_type,
    resolve_chat_context,
    validate_pagination,
)

from readers.settings import SETTINGS_HELP, get_setting
from readers.settings import configure as configure_settings
from readers.timeparse import FORMATS_HELP, parse_time_bound, parse_time_range
from readers.wechat_media import _is_reparse_or_symlink, resolve_and_download_media

TEMP_PREFIX = "wechat-reader-"
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


# Official accounts (subscription / service accounts) are out of scope: their ids start with gh_, and WeChat folds
# them into placeholder sessions that have no message table of their own
_OFFICIAL_ACCOUNT_PLACEHOLDERS = frozenset({"brandsessionholder", "brandservicesessionholder"})


def is_official_account(username):
    return username.startswith("gh_") or username in _OFFICIAL_ACCOUNT_PLACEHOLDERS


def _iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _conversation(ctx):
    return {"id": ctx["username"], "name": ctx["display_name"], "is_group": ctx["is_group"]}


def _message(row, ctx, names, id_to_username, self_id, app):
    local_id, local_type, ts, real_sender_id, content, ct = row
    content = decompress_content(content, ct)
    if content is None:
        content = "(decompression failed)"
    sender_in_content, text = _format_message_text(
        local_id, local_type, content, ctx["is_group"], ctx["username"], ctx["display_name"],
        names, app.display_name_fn, db_dir=app.db_dir, create_time_ts=ts,
    )
    _, raw = _parse_message_content(content, local_type, ctx["is_group"])
    sender_id = id_to_username.get(real_sender_id, "")
    # In group messages the sender may appear only in the content prefix "<id>:<newline>", consistent with the upstream _resolve_sender_label logic
    if ctx["is_group"] and (not sender_id or sender_id == ctx["username"]):
        sender_id = sender_in_content
    return {
        "id": local_id,
        "conversation_id": ctx["username"],
        "time": _iso(ts),
        "timestamp": ts,
        "sender_id": sender_id,
        "sender": app.display_name_fn(sender_id, names) if sender_id else "",
        "is_self": bool(self_id) and sender_id == self_id,
        "type": format_msg_type(local_type),
        "text": text,
        # text is a human-readable summary (image placeholder, links keep only the title); when the original differs it is also emitted, for item-by-item reconciliation (issue #3)
        **({"raw": raw} if raw != text else {}),
    }


def _keyword_matches(conn, table_name, is_group, keywords, start_ts, end_ts, limit):
    """Keyword search, not the upstream SQL LIKE: it treats % and _ as wildcards and cannot match content stored zstd-compressed.

    Matching rule: literal OR match across keywords, case-insensitive for ASCII letters only (same as SQLite lower(), so the SQL pre-filter and the Python match agree).
    Uncompressed rows are pre-filtered literally with instr; compressed rows (mostly links, files, long text) are decompressed and then matched.
    Compressed rows are inspected only for text, links/files and system messages; the compressed content of images, stickers and videos is media metadata (URLs, md5), and matching it only adds noise.
    """
    if not _is_safe_msg_table_name(table_name):
        raise ValueError(f"Invalid message table name: {table_name}")
    needles = [(kw, kw.translate(_ASCII_LOWER)) for kw in keywords]
    clauses, params = _build_message_filters(start_ts, end_ts)
    instr_clauses = " OR ".join("instr(lower(message_content), ?) > 0" for _ in needles)
    clauses.append(f"""(
        (IFNULL(WCDB_CT_message_content, 0) != 4 AND ({instr_clauses}))
        OR (WCDB_CT_message_content = 4 AND (local_type & 0xFFFFFFFF) IN (1, 49, 10000))
    )""")
    params.extend(needle for _, needle in needles)
    matches = []
    for row in conn.execute(f"""
        SELECT local_id, local_type, create_time, real_sender_id, message_content, WCDB_CT_message_content
        FROM [{table_name}] WHERE {" AND ".join(clauses)} ORDER BY create_time DESC
    """, params):
        # Group message content is prefixed with the sender id; strip it before matching, otherwise the id itself would match
        _, body = _parse_message_content(decompress_content(row[4], row[5]), row[1], is_group)
        if body:
            body_lower = body.translate(_ASCII_LOWER)
            matched = [kw for kw, needle in needles if needle in body_lower]
            if matched:
                matches.append((row, matched))
                if len(matches) >= limit:
                    break
    return matches


def _collect(app, table_contexts, limit, offset, start_ts=None, end_ts=None, keywords=None):
    """Take limit+offset+1 raw rows from each table, sort globally by time, detect truncation, slice the page, then format only the page that remains."""
    kw_list = list(keywords) if keywords else []
    fetch_limit = limit + offset + 1
    candidates, failures = [], []
    by_db = {}
    for tc in table_contexts:
        by_db.setdefault(tc["db_path"], []).append(tc)
    for db_path, contexts in by_db.items():
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                id_to_username = _load_name2id_maps(conn)
                for tc in contexts:
                    if kw_list:
                        rows = _keyword_matches(conn, tc["table_name"], tc["is_group"], kw_list, start_ts, end_ts,
                                                fetch_limit)
                        candidates.extend((row, tc, id_to_username, matched) for row, matched in rows)
                    else:
                        rows = _query_messages(conn, tc["table_name"], start_ts=start_ts, end_ts=end_ts,
                                               limit=fetch_limit)
                        candidates.extend((row, tc, id_to_username, None) for row in rows)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{db_path}: {e}")
    candidates.sort(key=lambda c: c[0][2], reverse=True)
    if kw_list:
        seen = set()
        deduped = []
        for item in candidates:
            # local_id is unique only inside one message database: the same conversation can have a table in several of them
            key = (item[1]["db_path"], item[1]["username"], item[0][0])
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        candidates = deduped
    truncated = len(candidates) > offset + limit
    page = candidates[offset:offset + limit]
    page.reverse()  # oldest to newest, matching reading order

    names = get_contact_names(app.cache, app.decrypted_dir)
    self_id = get_self_username(app.db_dir, app.cache, app.decrypted_dir)
    messages = []
    for row, tc, id_to_username, matched in page:
        try:
            msg = _message(row, tc, names, id_to_username, self_id, app)
            if matched is not None:
                msg["conversation_name"] = tc["display_name"]
                msg["is_group"] = tc["is_group"]
                msg["matched_keywords"] = matched
            messages.append(msg)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{tc['username']} id={row[0]}: {e}")
    return messages, truncated, failures


def list_conversations(app, limit):
    path = app.cache.get(os.path.join("session", "session.db"))
    if not path:
        raise ValueError("Cannot decrypt session.db")
    names = get_contact_names(app.cache, app.decrypted_dir)
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("""
            SELECT username, unread_count, summary, last_timestamp, last_msg_type, last_msg_sender
            FROM SessionTable WHERE last_timestamp > 0
            ORDER BY last_timestamp DESC
        """).fetchall()
    rows = [r for r in rows if not is_official_account(r[0])][:limit]
    result = []
    for username, unread, summary, ts, msg_type, sender in rows:
        if isinstance(summary, bytes):
            summary = decompress_content(summary, 4) or ""
        if isinstance(summary, str) and ":\n" in summary:
            summary = summary.split(":\n", 1)[1]
        result.append({
            "id": username,
            "name": names.get(username, username),
            "is_group": "@chatroom" in username,
            "unread": unread or 0,
            "last_time": _iso(ts),
            "last_timestamp": ts,
            "last_type": format_msg_type(msg_type),
            "last_sender_id": sender or "",
            "last_message": str(summary or ""),
        })
    return {"conversations": result}


def _resolve(app, conversation):
    ctx = resolve_chat_context(conversation, app.msg_db_keys, app.cache, app.decrypted_dir)
    if not ctx:
        raise ValueError(f"conversation not found: {conversation}")
    return ctx


def get_messages(app, conversation, limit, offset, start, end):
    start_ts, end_ts = parse_time_range(start, end)
    ctx = _resolve(app, conversation)
    messages, truncated, failures = _collect(app, list(_iter_table_contexts(ctx)), limit, offset, start_ts, end_ts)
    return {"conversation": _conversation(ctx), "truncated": truncated, "messages": messages, "failures": failures}


def _load_all_contexts(app):
    names = get_contact_names(app.cache, app.decrypted_dir)
    contexts, failures = [], []
    for rel_key in app.msg_db_keys:
        path = app.cache.get(rel_key)
        if not path:
            continue
        try:
            with closing(sqlite3.connect(path)) as conn:
                # For a table that cannot be mapped to a conversation id, the results cannot be attributed; skip it
                contexts.extend(
                    c for c in _load_search_contexts_from_db(conn, path, names)
                    if c["username"] and not is_official_account(c["username"])
                )
        except Exception as e:  # noqa: BLE001
            failures.append(f"{rel_key}: {e}")
    return contexts, failures


def _count_messages(table_contexts, start_ts=None, end_ts=None):
    clauses, params = _build_message_filters(start_ts, end_ts)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total, failures = 0, []
    by_db = {}
    for tc in table_contexts:
        by_db.setdefault(tc["db_path"], []).append(tc)
    for db_path, contexts in by_db.items():
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                for tc in contexts:
                    table_name = tc["table_name"]
                    if not _is_safe_msg_table_name(table_name):
                        raise ValueError(f"Invalid message table name: {table_name}")
                    total += conn.execute(f"SELECT COUNT(*) FROM [{table_name}] {where_sql}", params).fetchone()[0]
        except Exception as e:  # noqa: BLE001
            failures.append(f"{db_path}: {e}")
    return total, failures


def search(app, keyword, conversation, limit, start, end):
    start_ts, end_ts = parse_time_range(start, end)
    kw_list = [keyword] if isinstance(keyword, str) else list(keyword)
    kw_list = list(dict.fromkeys(kw for kw in kw_list if kw))
    failures = []
    if conversation:
        contexts = list(_iter_table_contexts(_resolve(app, conversation)))
    else:
        contexts, failures = _load_all_contexts(app)
    messages, truncated, collect_failures = _collect(app, contexts, limit, 0, start_ts, end_ts, keywords=kw_list)
    out = {"keywords": kw_list, "truncated": truncated, "messages": messages, "failures": failures + collect_failures}
    if len(kw_list) == 1:
        out["keyword"] = kw_list[0]
    return out


def recent(app, since, limit, exclude_conversations=None):
    # The reader does not keep a read position: the Local Agent remembers the last time it read up to and passes it back with --since next time (issue #2).
    # --since includes the boundary second, so messages in the same second are not missed but the previous last message reappears: the caller dedupes by (conversation_id, id)

    # Fetch one extra message to detect truncation; when truncated, keep the latest `limit` messages and the caller should raise --limit to read the rest
    start_ts, end_ts = parse_time_range(since, "")
    excluded_ids = {cid for cid in (exclude_conversations or ()) if cid}
    contexts, failures = _load_all_contexts(app)
    included = [c for c in contexts if c["username"] not in excluded_ids]
    excluded_contexts = [c for c in contexts if c["username"] in excluded_ids]
    messages, truncated, collect_failures = _collect(app, included, limit, 0, start_ts, end_ts)
    excluded_messages, count_failures = _count_messages(excluded_contexts, start_ts, end_ts)
    return {
        "since": since,
        "truncated": truncated,
        "excluded_messages": excluded_messages,
        "messages": messages,
        "failures": failures + collect_failures + count_failures,
    }


def _norm_compare_path(path):
    real = os.path.realpath(path)
    if real.upper().startswith("\\\\?\\UNC\\"):
        real = "\\\\" + real[8:]
    elif real.startswith("\\\\?\\"):
        real = real[4:]
    return os.path.normcase(real)


def _validate_dest_dir(app, dest_dir):
    if not os.path.isdir(dest_dir):
        raise ValueError(f"Destination is not an existing directory: {dest_dir}")
    db_dir_norm = os.path.normpath(app.db_dir)
    account_root = os.path.dirname(db_dir_norm)
    msg_root = os.path.join(account_root, "msg")
    protected_roots = [account_root, db_dir_norm, msg_root]
    for parent in (account_root, msg_root):
        if os.path.isdir(parent):
            try:
                with os.scandir(parent) as entries:
                    for entry in entries:
                        try:
                            st = os.lstat(entry.path)
                        except OSError:
                            continue
                        if _is_reparse_or_symlink(st):
                            protected_roots.append(os.path.realpath(entry.path))
            except OSError:
                pass
    norm_dest = _norm_compare_path(dest_dir)
    for root in protected_roots:
        norm_root = _norm_compare_path(root)
        try:
            common = os.path.commonpath([norm_root, norm_dest])
        except ValueError:
            common = ""
        if common == norm_root:
            raise ValueError("Destination must not be inside the WeChat account folder")


def _find_message(app, ctx, message_id, target_ts):
    table_name = "Msg_" + hashlib.md5(ctx["username"].encode("utf-8")).hexdigest()
    if not _is_safe_msg_table_name(table_name):
        raise ValueError(f"Invalid message table name: {table_name}")
    matches = []
    failures = []
    for rel_key in app.msg_db_keys:
        if not os.path.exists(os.path.join(app.db_dir, rel_key)):
            continue
        try:
            db_path = app.cache.get(rel_key)
        except Exception:  # noqa: BLE001
            db_path = None
        if not db_path:
            failures.append(f"{rel_key}: could not be opened")
            continue
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                has_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,),
                ).fetchone()
                if not has_table:
                    continue
                rows = conn.execute(
                    f"""
                    SELECT local_id, local_type, create_time, real_sender_id, message_content, WCDB_CT_message_content
                    FROM [{table_name}] WHERE local_id = ?
                    """,
                    (message_id,),
                ).fetchall()
                matches.extend(rows)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{rel_key}: {e}")

    if failures and target_ts is None:
        return None, failures, {
            "error": f"message id {message_id} could not be checked in every message database; retry with --time or fix the failures",
            "failures": failures,
        }

    if target_ts is not None:
        matches = [row for row in matches if int(row[2]) == target_ts]

    seen_messages = set()
    deduped = []
    for row in matches:
        key = (int(row[2]), row[1], row[5], row[4])
        if key not in seen_messages:
            seen_messages.add(key)
            deduped.append(row)

    if not deduped:
        raise ValueError(f"Message not found: {message_id}")
    if len(deduped) > 1:
        deduped.sort(key=lambda row: int(row[2]))
        alternatives = [
            {
                "time": _iso(row[2]),
                "timestamp": row[2],
                "type": format_msg_type(row[1]),
            }
            for row in deduped
        ]
        if target_ts is not None:
            err = f"message id {message_id} is ambiguous: {len(deduped)} distinct messages share it at the same time; cannot disambiguate with --time"
        else:
            err = f"message id {message_id} is ambiguous: {len(deduped)} messages share it; retry with --time"
        return None, failures, {"error": err, "alternatives": alternatives}

    return deduped[0], failures, None


def download_media(app, message_id, conversation, dest_dir, time_str=""):
    _validate_dest_dir(app, dest_dir)
    parsed_time = parse_time_bound(time_str) if time_str else None
    target_ts = int(parsed_time) if parsed_time is not None else None
    ctx = _resolve(app, conversation)

    row, failures, err_result = _find_message(app, ctx, message_id, target_ts)
    if err_result is not None:
        return err_result

    _, local_type, create_ts, _, raw_content, ct = row
    content = decompress_content(raw_content, ct)

    try:
        hardlink_db_path = app.cache.get(os.path.join("hardlink", "hardlink.db"))
    except Exception:  # noqa: BLE001
        hardlink_db_path = None

    msg_root = os.path.join(os.path.dirname(os.path.normpath(app.db_dir)), "msg")
    items = resolve_and_download_media(
        hardlink_db_path,
        msg_root,
        local_type,
        content,
        dest_dir,
        is_group=ctx["is_group"],
    )
    return {
        "conversation": _conversation(ctx),
        "message_id": message_id,
        "time": _iso(create_ts),
        "timestamp": create_ts,
        "message_type": format_msg_type(local_type),
        "items": items,
        "failures": failures,
    }


def _parser():
    p = argparse.ArgumentParser(prog="wechat-reader", description="Read the local WeChat read-only; JSON output to stdout")
    p.add_argument("--settings", default=None, help=SETTINGS_HELP)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("conversations", help="List conversations by last message time")
    c.add_argument("--limit", type=int, default=20)

    m = sub.add_parser("messages", help="Get the messages of a single conversation (newest N results, returned oldest to newest)")
    m.add_argument("conversation", help="Display name, remark name or id (such as wxid_xxx, xxx@chatroom)")
    m.add_argument("--limit", type=int, default=50, help="Maximum number of newest results to keep, returned oldest to newest (default 50)")
    m.add_argument("--offset", type=int, default=0, help="How many messages to skip, counting back from the newest")
    m.add_argument("--start", default="", help=FORMATS_HELP)
    m.add_argument("--end", default="", help=FORMATS_HELP)

    s = sub.add_parser("search", help="Search message content by one or more keywords (OR; newest N results, returned oldest to newest)")
    s.add_argument("keywords", nargs="+", metavar="keyword", help="One or more search keywords (OR)")
    s.add_argument("--conversation", default="", help="Search only this conversation")
    s.add_argument("--limit", type=int, default=20, help="Maximum number of newest results to keep, returned oldest to newest (default 20)")
    s.add_argument("--start", default="", help=FORMATS_HELP)
    s.add_argument("--end", default="", help=FORMATS_HELP)

    r = sub.add_parser("recent", help="Messages after a given time across all conversations (oldest to newest)")
    r.add_argument("--since", required=True, help=f"The time last read up to: {FORMATS_HELP}; passing back the last message's `time` is immune to time zone changes. Boundary included, dedupe by (conversation_id, id)")
    r.add_argument("--limit", type=int, default=100)

    d = sub.add_parser("download", help="Resolve and safely copy locally cached media/attachments for a message")
    d.add_argument("message_id", type=int, metavar="message-id", help="Message integer local_id as reported by messages")
    d.add_argument("--conversation", required=True, help="Display name, remark name or id (such as wxid_xxx, xxx@chatroom)")
    d.add_argument("--to", required=True, dest="to_dir", help="Existing destination directory")
    d.add_argument("--time", default="", dest="msg_time", help=f"The message's `time` from messages/search/recent output, passed back unchanged ({FORMATS_HELP})")
    return p


def _run(args):
    if args.cmd != "download":
        validate_pagination(args.limit, getattr(args, "offset", 0), limit_max=None)
    if args.cmd == "search":
        if any(not kw.strip() for kw in args.keywords):
            raise ValueError("Search keyword must not be blank")
    app = AppContext()
    if args.cmd == "conversations":
        return list_conversations(app, args.limit)
    if args.cmd == "messages":
        return get_messages(app, args.conversation, args.limit, args.offset, args.start, args.end)
    if args.cmd == "recent":
        excluded = get_setting("wechat_exclude_conversations") or []
        return recent(app, args.since, args.limit, exclude_conversations=excluded)
    if args.cmd == "download":
        return download_media(app, args.message_id, args.conversation, args.to_dir, time_str=args.msg_time)
    return search(app, args.keywords, args.conversation, args.limit, args.start, args.end)


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    # The upstream cache directory is a class attribute; point it at this run's dedicated temp directory and delete the whole thing at exit, leaving the upstream shared cache untouched
    cache_dir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    DBCache.CACHE_DIR = cache_dir
    DBCache.MTIME_FILE = os.path.join(cache_dir, "_mtimes.json")
    try:
        configure_settings(args.settings)
        result = _run(args)
        if "error" in result:
            code = 1
        elif "items" in result:
            code = 0 if any(item["status"] == "resolved" for item in result["items"]) else 1
        else:
            code = 0
    except Exception as e:  # noqa: BLE001 — any failure is reported to the Local Agent as JSON
        result, code = {"error": str(e)}, 1
    finally:
        # finally also runs on Ctrl+C interruption; the plaintext is still deleted on interruption
        shutil.rmtree(cache_dir, ignore_errors=True)
    if os.path.exists(cache_dir):
        # Plaintext that cannot be deleted (for example a locked file) must not count as success; do not print what was read, report an error for the caller to handle
        result, code = {"error": f"Decrypted plaintext could not be deleted; delete it manually: {cache_dir}"}, 3
    print(json.dumps(result, ensure_ascii=False, indent=None if code else 2))
    return code


if __name__ == "__main__":
    sys.exit(main())
