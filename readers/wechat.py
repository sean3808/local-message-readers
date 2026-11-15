"""WeChat reader: normalizes data read by the upstream tool (wechat-cli) into the GLOSSARY.md vocabulary and prints JSON to stdout.

Takes the raw rows the upstream query returns directly, without parsing its pre-formatted strings (issue #3).
Read-only: exposes no send path (ADR-0002). Decrypted artifacts are written to a temp directory dedicated to this run and deleted at exit, leaving no plaintext on disk.
"""

import argparse
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
    parse_time_range,
    resolve_chat_context,
    validate_pagination,
)

from readers.settings import configure as configure_settings

TEMP_PREFIX = "wechat-reader-"
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


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


def _keyword_matches(conn, table_name, is_group, keyword, start_ts, end_ts, limit):
    """Keyword search, not the upstream SQL LIKE: it treats % and _ as wildcards and cannot match content stored zstd-compressed.

    Matching rule: literal match, case-insensitive for ASCII letters only (same as SQLite lower(), so the SQL pre-filter and the Python match agree).
    Uncompressed rows are pre-filtered literally with instr; compressed rows (mostly links, files, long text) are decompressed and then matched.
    Compressed rows are inspected only for text, links/files and system messages; the compressed content of images, stickers and videos is media metadata (URLs, md5), and matching it only adds noise.
    """
    if not _is_safe_msg_table_name(table_name):
        raise ValueError(f"Invalid message table name: {table_name}")
    needle = keyword.translate(_ASCII_LOWER)
    clauses, params = _build_message_filters(start_ts, end_ts)
    clauses.append("""(
        (IFNULL(WCDB_CT_message_content, 0) != 4 AND instr(lower(message_content), ?) > 0)
        OR (WCDB_CT_message_content = 4 AND (local_type & 0xFFFFFFFF) IN (1, 49, 10000))
    )""")
    params.append(needle)
    matches = []
    for row in conn.execute(f"""
        SELECT local_id, local_type, create_time, real_sender_id, message_content, WCDB_CT_message_content
        FROM [{table_name}] WHERE {" AND ".join(clauses)} ORDER BY create_time DESC
    """, params):
        # Group message content is prefixed with the sender id; strip it before matching, otherwise the id itself would match
        _, body = _parse_message_content(decompress_content(row[4], row[5]), row[1], is_group)
        if body and needle in body.translate(_ASCII_LOWER):
            matches.append(row)
            if len(matches) >= limit:
                break
    return matches


def _collect(app, table_contexts, limit, offset, start_ts=None, end_ts=None, keyword=""):
    """Take limit+offset raw rows from each table, sort globally by time and slice the page, then format only the page that remains."""
    candidates, failures = [], []
    by_db = {}
    for tc in table_contexts:
        by_db.setdefault(tc["db_path"], []).append(tc)
    for db_path, contexts in by_db.items():
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                id_to_username = _load_name2id_maps(conn)
                for tc in contexts:
                    if keyword:
                        rows = _keyword_matches(conn, tc["table_name"], tc["is_group"], keyword, start_ts, end_ts,
                                                limit + offset)
                    else:
                        rows = _query_messages(conn, tc["table_name"], start_ts=start_ts, end_ts=end_ts,
                                               limit=limit + offset)
                    candidates.extend((row, tc, id_to_username) for row in rows)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{db_path}: {e}")
    candidates.sort(key=lambda c: c[0][2], reverse=True)
    page = candidates[offset:offset + limit]
    page.reverse()  # oldest to newest, matching reading order

    names = get_contact_names(app.cache, app.decrypted_dir)
    self_id = get_self_username(app.db_dir, app.cache, app.decrypted_dir)
    messages = []
    for row, tc, id_to_username in page:
        try:
            messages.append(_message(row, tc, names, id_to_username, self_id, app))
        except Exception as e:  # noqa: BLE001
            failures.append(f"{tc['username']} id={row[0]}: {e}")
    return messages, failures


def list_conversations(app, limit):
    path = app.cache.get(os.path.join("session", "session.db"))
    if not path:
        raise ValueError("Cannot decrypt session.db")
    names = get_contact_names(app.cache, app.decrypted_dir)
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("""
            SELECT username, unread_count, summary, last_timestamp, last_msg_type, last_msg_sender
            FROM SessionTable WHERE last_timestamp > 0
            ORDER BY last_timestamp DESC LIMIT ?
        """, (limit,)).fetchall()
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
    messages, failures = _collect(app, list(_iter_table_contexts(ctx)), limit, offset, start_ts, end_ts)
    return {"conversation": _conversation(ctx), "messages": messages, "failures": failures}


def search(app, keyword, conversation, limit, start, end):
    start_ts, end_ts = parse_time_range(start, end)
    failures = []
    if conversation:
        contexts = list(_iter_table_contexts(_resolve(app, conversation)))
    else:
        names = get_contact_names(app.cache, app.decrypted_dir)
        contexts = []
        for rel_key in app.msg_db_keys:
            path = app.cache.get(rel_key)
            if not path:
                continue
            try:
                with closing(sqlite3.connect(path)) as conn:
                    # For a table that cannot be mapped to a conversation id, the results cannot be attributed; skip it
                    contexts.extend(c for c in _load_search_contexts_from_db(conn, path, names) if c["username"])
            except Exception as e:  # noqa: BLE001
                failures.append(f"{rel_key}: {e}")
    messages, collect_failures = _collect(app, contexts, limit, 0, start_ts, end_ts, keyword=keyword)
    return {"keyword": keyword, "messages": messages, "failures": failures + collect_failures}


def recent(app, since, limit):
    # The reader does not keep a read position: the Local Agent remembers the last time it read up to and passes it back with --since next time (issue #2).
    # --since includes the boundary second, so messages in the same second are not missed but the previous last message reappears: the caller dedupes by (conversation_id, id)

    # Fetch one extra message to detect truncation; when truncated, keep the latest `limit` messages and the caller should raise --limit to read the rest
    result = search(app, "", "", limit + 1, since, "")
    messages = result["messages"]
    return {"since": since, "truncated": len(messages) > limit, "messages": messages[-limit:],
            "failures": result["failures"]}


def _parser():
    p = argparse.ArgumentParser(prog="wechat-reader", description="Read the local WeChat read-only; JSON output to stdout")
    p.add_argument("--settings", default=None, help="Settings file path (default: settings.toml in the repo root)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("conversations", help="List conversations by last message time")
    c.add_argument("--limit", type=int, default=20)

    m = sub.add_parser("messages", help="Get the messages of a single conversation (oldest to newest)")
    m.add_argument("conversation", help="Display name, remark name or id (such as wxid_xxx, xxx@chatroom)")
    m.add_argument("--limit", type=int, default=50)
    m.add_argument("--offset", type=int, default=0, help="How many messages to skip, counting back from the newest")
    m.add_argument("--start", default="", help="YYYY-MM-DD [HH:MM[:SS]]")
    m.add_argument("--end", default="", help="YYYY-MM-DD [HH:MM[:SS]]")

    s = sub.add_parser("search", help="Search message content by keyword")
    s.add_argument("keyword")
    s.add_argument("--conversation", default="", help="Search only this conversation")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--start", default="")
    s.add_argument("--end", default="")

    r = sub.add_parser("recent", help="Messages after a given time across all conversations (oldest to newest)")
    r.add_argument("--since", required=True, help="YYYY-MM-DD [HH:MM[:SS]], the time last read up to; boundary included, dedupe by (conversation_id, id)")
    r.add_argument("--limit", type=int, default=100)
    return p


def _run(args):
    validate_pagination(args.limit, getattr(args, "offset", 0), limit_max=None)
    app = AppContext()
    if args.cmd == "conversations":
        return list_conversations(app, args.limit)
    if args.cmd == "messages":
        return get_messages(app, args.conversation, args.limit, args.offset, args.start, args.end)
    if args.cmd == "recent":
        return recent(app, args.since, args.limit)
    return search(app, args.keyword, args.conversation, args.limit, args.start, args.end)


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    # The upstream cache directory is a class attribute; point it at this run's dedicated temp directory and delete the whole thing at exit, leaving the upstream shared cache untouched
    cache_dir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    DBCache.CACHE_DIR = cache_dir
    DBCache.MTIME_FILE = os.path.join(cache_dir, "_mtimes.json")
    try:
        configure_settings(args.settings)
        result, code = _run(args), 0
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
