"""Teams reader CLI.

Handles command-line argument parsing, conversation name disambiguation and query response formatting.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from readers.archive import Archive, get_default_archive_path
from readers.backup import get_default_log_path
from readers.settings import configure as configure_settings
from readers.teams.download import download_message
from readers.teams.export import import_export
from readers.teams.reconcile import reconcile_export
from readers.teams.repair import repair_fffd
from readers.teams.system_text import migrate_system_text
from readers.teams.snapshot import (
    get_tfl_profile_dir,
    ingest_cache,
    read_cache_messages,
)


def resolve_conversation(archive: Archive, conversation_arg: str) -> dict[str, Any]:
    """Resolve the conversation argument (by id or display name).

    If the name matches several candidates, raises an ambiguity error with distinguishing details; raises a not-found error if nothing matches.
    """
    # 1. Exact id match first
    conv = archive.get_conversation("teams", conversation_arg)
    if conv:
        return conv

    # 2. Match by name
    convs = archive.find_conversations_by_name("teams", conversation_arg)
    if not convs:
        raise ValueError(f"Conversation not found: '{conversation_arg}'")

    if len(convs) == 1:
        return convs[0]

    # Multiple candidates: report an ambiguity error
    details = [
        f"- id={c['id']}, last_time={c['last_time'] or 'unknown'}, is_group={c['is_group']}"
        for c in convs
    ]
    msg = f"Conversation name '{conversation_arg}' matches multiple conversations; specify one by id:\n" + "\n".join(details)
    raise ValueError(msg)


def resolve_conversation_for_query(
    archive: Archive,
    conv_arg: str,
    refresh_status: str,
    refresh_error: str | None,
) -> dict[str, Any]:
    """Resolve the conversation argument; if the cache refresh failed and resolution fails, wrap it as an explicit source error."""
    try:
        return resolve_conversation(archive, conv_arg)
    except Exception as e:
        if refresh_status == "failed":
            raise RuntimeError(f"Teams cache source error: {refresh_error}; also could not resolve conversation: {e}") from e
        raise


def _parser() -> argparse.ArgumentParser:
    # Options shared by subcommands: default=argparse.SUPPRESS keeps a subcommand that omits them from overriding values already parsed by the main command line
    sub_common = argparse.ArgumentParser(add_help=False)
    sub_common.add_argument("--archive-path", default=argparse.SUPPRESS, help="Path of the archive SQLite file")
    sub_common.add_argument("--no-refresh", action="store_true", default=argparse.SUPPRESS, help="Skip the cache refresh and query the existing archive directly")
    sub_common.add_argument("--profile-dir", default=argparse.SUPPRESS, help="Teams personal profile path (takes precedence over the settings file)")
    sub_common.add_argument("--settings", default=argparse.SUPPRESS, help="Settings file path (default: settings.toml in the repo root)")

    p = argparse.ArgumentParser(
        prog="teams-reader",
        description="Read the local Teams personal client read-only; JSON output goes to stdout",
    )
    p.add_argument("--archive-path", default=None, help="Path of the archive SQLite file")
    p.add_argument("--no-refresh", action="store_true", default=False, help="Skip the cache refresh and query the existing archive directly")
    p.add_argument("--profile-dir", default=None, help="Teams personal profile path (takes precedence over the settings file)")
    p.add_argument("--settings", default=None, help="Settings file path (default: settings.toml in the repo root)")

    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser(
        "conversations",
        parents=[sub_common],
        help="List conversations by last message time",
    )
    c.add_argument("--limit", type=int, default=20, help="Maximum number of results (default 20)")

    m = sub.add_parser(
        "messages",
        parents=[sub_common],
        help="Get the messages of one conversation (oldest first)",
    )
    m.add_argument("conversation", help="Display name or id")
    m.add_argument("--limit", type=int, default=50, help="Maximum number of results (default 50)")
    m.add_argument("--offset", type=int, default=0, help="Number of results to skip (default 0)")
    m.add_argument("--start", default="", help="Start time (YYYY-MM-DD [HH:MM[:SS]])")
    m.add_argument("--end", default="", help="End time (YYYY-MM-DD [HH:MM[:SS]])")

    s = sub.add_parser(
        "search",
        parents=[sub_common],
        help="Search message content by keyword",
    )
    s.add_argument("keyword", help="Search keyword")
    s.add_argument("--conversation", default="", help="Search only this conversation (display name or id)")
    s.add_argument("--limit", type=int, default=20, help="Maximum number of results (default 20)")
    s.add_argument("--start", default="", help="Start time (YYYY-MM-DD [HH:MM[:SS]])")
    s.add_argument("--end", default="", help="End time (YYYY-MM-DD [HH:MM[:SS]])")

    r = sub.add_parser(
        "recent",
        parents=[sub_common],
        help="Messages from all conversations after a given time (oldest first)",
    )
    r.add_argument("--since", required=True, help="YYYY-MM-DD [HH:MM[:SS]], the time you last read up to; inclusive")
    r.add_argument("--limit", type=int, default=100, help="Maximum number of results (default 100)")

    d = sub.add_parser("download", parents=[sub_common], help="Download a message's attachments")
    d.add_argument("message_id", help="Message id")
    d.add_argument("--to", required=True, help="Destination folder")
    d.add_argument("--conversation", default="", help="Conversation name or id (needed when the message id is not unique)")
    d.add_argument("--name", help="Download only the attachment with this name")

    ie = sub.add_parser(
        "import-export",
        parents=[sub_common],
        help="Import an official Teams export (.tar or messages.json) into the archive",
    )
    ie.add_argument("path", help="Path of the official export .tar file or messages.json file (directories are not accepted)")

    rc = sub.add_parser(
        "reconcile",
        parents=[sub_common],
        help="Reconcile: official export vs the cached versions in the archive (read-only, overlapping period only)",
    )
    rc.add_argument("path", help="Path of the official export .tar file or messages.json file (directories are not accepted)")
    rc.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of locators printed per kind of difference (default 0 = all)",
    )

    repair = sub.add_parser("repair-fffd", parents=[sub_common], help="One-time repair of U+FFFD messages")
    repair.description = "One-time repair of U+FFFD messages; previews by default, use --write to apply."
    repair.add_argument("--write", action="store_true", help="Actually write the repair result")

    mig = sub.add_parser("migrate-system-text", parents=[sub_common], help="One-time rewrite of old Chinese system-event texts to English")
    mig.description = "One-time rewrite of archived Chinese system-event texts (#39) to the English text; previews by default, use --write to apply."
    mig.add_argument("--write", action="store_true", help="Actually write the migration result")
    return p


def _format_message(m: dict[str, Any]) -> dict[str, Any]:
    """Format the output message fields, conforming to GLOSSARY.md and the output contract."""
    ts = m["timestamp"]
    return {
        "id": m["id"],
        "conversation_id": m["conversation_id"],
        "time": m["time"],
        "timestamp": int(ts) if ts == int(ts) else ts,
        "sender_id": m["sender_id"],
        "sender": m["sender"],
        "is_self": m["is_self"],
        "type": m["type"],
        "text": m["text"],
        "edited": m["edited"],
        "deleted": m["deleted"],
        "files": m["files"],
        "attachments": m["attachments"],
        "quoted": m["quoted"],
        "sources": m["sources"],
    }


def validate_pagination(limit: int, offset: int = 0) -> None:
    """Same rules as wechat-reader (wechat_cli.core.messages.validate_pagination, limit_max=None)."""
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    if offset < 0:
        raise ValueError("offset must not be less than 0")


# Backups run weekly plus one day of grace; beyond that they count as stale (#21)
BACKUP_STALE_AFTER = timedelta(days=8)


def backup_status(failures: list[str]) -> dict[str, Any]:
    """Read the backup log (read-only) and report the time of the last successful backup and whether it is stale.

    Bad lines (including bytes that cannot be decoded) are skipped one by one; if the whole file cannot be read it counts as never succeeded and is recorded in failures.
    """
    last_success = None
    finished_at = None
    try:
        with get_default_log_path().open("rb") as log:
            for raw in log:
                try:
                    # Strict per-line decoding: a line with undecodable bytes is skipped entirely (UnicodeDecodeError is a ValueError)
                    entry = json.loads(raw.decode("utf-8"))
                    if not isinstance(entry, dict) or not isinstance(entry.get("backup"), dict):
                        continue
                    if entry["backup"].get("ok") is not True or not isinstance(entry.get("finished"), str):
                        continue
                    parsed = datetime.fromisoformat(entry["finished"])
                    if parsed.tzinfo is None:
                        continue
                    last_success, finished_at = entry["finished"], parsed
                except (ValueError, TypeError):
                    continue
    except FileNotFoundError:
        pass
    except OSError:
        failures.append("Cannot read the backup log")
        last_success, finished_at = None, None
    return {
        "last_success": last_success,
        "stale": finished_at is None or datetime.now(UTC) - finished_at > BACKUP_STALE_AFTER,
    }


def _profile_dir(args: argparse.Namespace) -> Path:
    override = getattr(args, "profile_dir", None)
    return Path(override) if override else get_tfl_profile_dir()


def _open_one_shot_archive(args: argparse.Namespace, failures: list[str]) -> Archive:
    """Open the archive for a one-shot repair command: read-only preview, or --write guarded by target and backup."""
    if not args.write:
        return Archive.open_read_only(args.archive_path)
    default_path = get_default_archive_path()
    selected_path = Path(args.archive_path) if args.archive_path is not None else default_path
    if selected_path.resolve() != default_path.resolve():
        raise RuntimeError("Writes are allowed only to the default archive; the given path does not match the backup log")
    if backup_status(failures)["stale"]:
        raise RuntimeError("No successful backup yet or the backup is stale; refusing to write")
    return Archive.open_existing_for_write(args.archive_path)


def _run(args: argparse.Namespace) -> dict[str, Any]:
    failures: list[str] = []

    if args.cmd in ("conversations", "messages", "search", "recent"):
        validate_pagination(args.limit, getattr(args, "offset", 0))

    if args.cmd == "reconcile":
        # Reconcile is read-only: do not create, migrate or write the archive
        return reconcile_export(
            Archive.open_read_only(args.archive_path), args.path, failures=failures, locator_limit=args.limit
        )

    if args.cmd == "repair-fffd":
        archive = _open_one_shot_archive(args, failures)
        cache_failures: list[str] = []
        result = repair_fffd(archive, read_cache_messages(_profile_dir(args), cache_failures), write=args.write)
        result["cache_read_failures"] = cache_failures
        return result

    if args.cmd == "migrate-system-text":
        return migrate_system_text(_open_one_shot_archive(args, failures), write=args.write)

    archive = (Archive.open_read_only(args.archive_path) if args.cmd == "download" and args.no_refresh
               else Archive(db_path=args.archive_path))

    if args.cmd == "import-export":
        return import_export(archive, args.path, failures=failures)

    profile_dir = _profile_dir(args)
    refresh_status = "ok"
    refresh_error: str | None = None

    if getattr(args, "no_refresh", False):
        refresh_status = "skipped"
    elif not profile_dir.exists():
        refresh_status = "failed"
        refresh_error = f"Teams personal profile not found ({profile_dir})"
        failures.append(refresh_error)
    else:
        outcome = ingest_cache(archive, profile_dir)
        failures.extend(outcome.failures)
        if outcome.success:
            refresh_status = "ok"
        else:
            refresh_status = "failed"
            refresh_error = outcome.error or (outcome.failures[0] if outcome.failures else "Cache refresh failed")

    current_up_to = archive.get_data_current_up_to("teams")
    refresh_outcome: dict[str, Any] = {
        "status": refresh_status,
        "current_up_to": current_up_to,
    }
    if current_up_to is None:
        refresh_outcome["current_up_to_reason"] = "The Teams cache has never been synced successfully; data currency is unknown"
    if refresh_status == "failed":
        refresh_outcome["error"] = refresh_error

    # A source error must not masquerade as "no conversations": when the refresh fails and the archive holds no Teams data, every query command reports an error
    if refresh_status == "failed" and not archive.query_conversations("teams"):
        raise RuntimeError(f"Teams cache source error: {refresh_error} (no conversations in the archive)")

    backup_outcome = backup_status(failures)

    if args.cmd == "download":
        conv_id = (resolve_conversation_for_query(archive, args.conversation, refresh_status, refresh_error)['id']
                   if args.conversation else None)
        result = download_message(archive, args.message_id, Path(args.to), conv_id, args.name)
        result.update(refresh=refresh_outcome, backup=backup_outcome, failures=failures)
        return result

    if args.cmd == "conversations":
        convs = archive.query_conversations("teams", limit=args.limit)
        return {
            "conversations": convs,
            "refresh": refresh_outcome,
            "backup": backup_outcome,
            "failures": failures,
        }

    if args.cmd == "messages":
        conv = resolve_conversation_for_query(archive, args.conversation, refresh_status, refresh_error)
        msgs = archive.query_messages(
            channel="teams",
            conversation_id=conv["id"],
            limit=args.limit,
            offset=args.offset,
            start=args.start,
            end=args.end,
        )
        return {
            "conversation": {
                "id": conv["id"],
                "name": conv["name"],
                "is_group": conv["is_group"],
            },
            "messages": [_format_message(m) for m in msgs],
            "refresh": refresh_outcome,
            "backup": backup_outcome,
            "failures": failures,
        }

    if args.cmd == "search":
        conv_id = None
        if args.conversation:
            conv = resolve_conversation_for_query(archive, args.conversation, refresh_status, refresh_error)
            conv_id = conv["id"]

        msgs = archive.search_messages(
            channel="teams",
            keyword=args.keyword,
            conversation_id=conv_id,
            limit=args.limit,
            start=args.start,
            end=args.end,
        )
        out_messages = []
        for m in msgs:
            fmt = _format_message(m)
            fmt["hit_in_quote"] = m["hit_in_quote"]
            out_messages.append(fmt)

        return {
            "keyword": args.keyword,
            "messages": out_messages,
            "refresh": refresh_outcome,
            "backup": backup_outcome,
            "failures": failures,
        }

    if args.cmd == "recent":
        msgs, truncated = archive.query_recent_messages(
            channel="teams",
            since=args.since,
            limit=args.limit,
        )
        return {
            "since": args.since,
            "truncated": truncated,
            "messages": [_format_message(m) for m in msgs],
            "refresh": refresh_outcome,
            "backup": backup_outcome,
            "failures": failures,
        }

    raise ValueError(f"Unsupported command: {args.cmd}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        configure_settings(args.settings)
        result = _run(args)
        code = 1 if args.cmd == "download" and "error" in result else 0
    except Exception as e:  # noqa: BLE001
        result = {"error": str(e)}
        code = 1

    print(json.dumps(result, ensure_ascii=False, indent=None if code else 2))
    return code
