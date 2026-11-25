"""Teams reader CLI.

Handles command-line argument parsing, conversation name disambiguation and query response formatting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from readers.archive import Archive, get_default_archive_path
from readers.backup import get_default_log_path
from readers.settings import SETTINGS_HELP, get_setting
from readers.settings import configure as configure_settings
from readers.teams.download import download_message
from readers.teams.export import import_export
from readers.teams.placeholder import clean_placeholder_messages
from readers.teams.reconcile import reconcile_export
from readers.teams.repair import repair_fffd
from readers.teams.system_text import migrate_system_text
from readers.timeparse import FORMATS_HELP
from readers.teams.snapshot import (
    get_tfl_profile_dir,
    ingest_cache,
    read_cache_messages,
)


def _last_time_sort_key(conv: dict[str, Any]) -> tuple[bool, float]:
    """Order by last_time as "known instant or unknown": unknown sorts oldest, and all unknowns tie.

    Only an ISO time carrying an offset is a known instant. Blank, malformed or offset-less values
    are unknown rather than guessed, so two of them always tie and keep the ambiguity error.
    """
    raw = conv.get("last_time")
    try:
        dt = datetime.fromisoformat(raw.strip()) if isinstance(raw, str) else None
    except ValueError:
        dt = None
    if dt is None or dt.tzinfo is None:
        return (False, 0.0)
    return (True, dt.timestamp())


def resolve_conversation_with_alternatives(
    archive: Archive, conversation_arg: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Resolve the conversation argument (by id or display name) and return (chosen, alternatives).

    When multiple non-group conversations share the display name with distinct last_time values,
    resolves to the newest and returns the remaining candidates newest-first.
    Raises an ambiguity error if any candidate is a group or any candidates tie on last_time.
    """
    # 1. Exact id match first
    conv = archive.get_conversation("teams", conversation_arg)
    if conv:
        return conv, []

    # 2. Match by name
    convs = archive.find_conversations_by_name("teams", conversation_arg)
    if not convs:
        raise ValueError(f"Conversation not found: '{conversation_arg}'")

    if len(convs) == 1:
        return convs[0], []

    # Only same-name 1:1 chats auto-resolve; a candidate found through an old alias has a different name
    if all(not c["is_group"] and c["name"] == conversation_arg for c in convs):
        keyed = sorted(((_last_time_sort_key(c), c) for c in convs), key=lambda kc: kc[0], reverse=True)
        if len({k for k, _ in keyed}) == len(keyed):
            alternatives = [
                {"id": c["id"], "last_time": c.get("last_time") or None}
                for _, c in keyed[1:]
            ]
            return keyed[0][1], alternatives

    # Multiple candidates: report an ambiguity error
    details = [
        f"- id={c['id']}, last_time={c['last_time'] or 'unknown'}, is_group={c['is_group']}"
        for c in convs
    ]
    msg = f"Conversation name '{conversation_arg}' matches multiple conversations; specify one by id:\n" + "\n".join(details)
    raise ValueError(msg)


def resolve_conversation(archive: Archive, conversation_arg: str) -> dict[str, Any]:
    """Resolve the conversation argument (by id or display name).

    If the name matches several candidates that cannot be disambiguated, raises an ambiguity error
    with distinguishing details; raises a not-found error if nothing matches.
    """
    conv, _ = resolve_conversation_with_alternatives(archive, conversation_arg)
    return conv


def resolve_conversation_for_query(
    archive: Archive,
    conv_arg: str,
    refresh_status: str,
    refresh_error: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Resolve the conversation argument; if the cache refresh failed and resolution fails, wrap it as an explicit source error."""
    try:
        return resolve_conversation_with_alternatives(archive, conv_arg)
    except Exception as e:
        if refresh_status == "failed":
            raise RuntimeError(f"Teams cache source error: {refresh_error}; also could not resolve conversation: {e}") from e
        raise


def _parser() -> argparse.ArgumentParser:
    # Options shared by subcommands: default=argparse.SUPPRESS keeps a subcommand that omits them from overriding values already parsed by the main command line
    sub_common = argparse.ArgumentParser(add_help=False)
    sub_common.add_argument("--archive-path", default=argparse.SUPPRESS, help="Path of the archive SQLite file")
    sub_common.add_argument("--no-refresh", action="store_true", default=argparse.SUPPRESS, help="Skip the cache refresh and query the existing archive directly")
    sub_common.add_argument("--refresh", action="store_true", default=argparse.SUPPRESS, help="Force a cache refresh even if the last refresh was recent")
    sub_common.add_argument("--profile-dir", default=argparse.SUPPRESS, help="Teams personal profile path (takes precedence over the settings file)")
    sub_common.add_argument("--settings", default=argparse.SUPPRESS, help=SETTINGS_HELP)

    p = argparse.ArgumentParser(
        prog="teams-reader",
        description="Read the local Teams personal client read-only; JSON output goes to stdout",
    )
    p.add_argument("--archive-path", default=None, help="Path of the archive SQLite file")
    p.add_argument("--no-refresh", action="store_true", default=False, help="Skip the cache refresh and query the existing archive directly")
    p.add_argument("--refresh", action="store_true", default=False, help="Force a cache refresh even if the last refresh was recent")
    p.add_argument("--profile-dir", default=None, help="Teams personal profile path (takes precedence over the settings file)")
    p.add_argument("--settings", default=None, help=SETTINGS_HELP)

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
    m.add_argument("--start", default="", help=f"Start time: {FORMATS_HELP}")
    m.add_argument("--end", default="", help=f"End time: {FORMATS_HELP}")

    s = sub.add_parser(
        "search",
        parents=[sub_common],
        help="Search message content by keyword",
    )
    s.add_argument("keyword", help="Search keyword")
    s.add_argument("--conversation", default="", help="Search only this conversation (display name or id)")
    s.add_argument("--limit", type=int, default=20, help="Maximum number of results (default 20)")
    s.add_argument("--start", default="", help=f"Start time: {FORMATS_HELP}")
    s.add_argument("--end", default="", help=f"End time: {FORMATS_HELP}")

    r = sub.add_parser(
        "recent",
        parents=[sub_common],
        help="Messages from all conversations after a given time (oldest first)",
    )
    r.add_argument("--since", required=True, help=f"The time you last read up to: {FORMATS_HELP}; passing back the last message's `time` is immune to time zone changes. Inclusive")
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

    clean = sub.add_parser("clean-placeholder-messages", parents=[sub_common], help="One-time removal of archived cache placeholder messages (id -1)")
    clean.description = "One-time removal of cache-only placeholder messages (id -1); previews by default, use --write to apply."
    clean.add_argument("--write", action="store_true", help="Actually delete the placeholder messages")
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


DEFAULT_REFRESH_MIN_INTERVAL_SECONDS = 120


def _now() -> datetime:
    return datetime.now(UTC)


def _profile_key(profile_dir: Path) -> str:
    return os.path.normcase(str(profile_dir.resolve()))


def _throttle_reason(archive: Archive, profile_dir: Path) -> str | None:
    """Return a skip reason when the last clean refresh of this profile is within the interval, else None."""
    interval = get_setting("refresh_min_interval_seconds")
    if interval is None:
        interval = DEFAULT_REFRESH_MIN_INTERVAL_SECONDS
    if interval <= 0:
        return None
    token = archive.get_clean_refresh("teams")
    if token is None or token[1] != _profile_key(profile_dir):
        return None
    elapsed = _now().timestamp() - token[0]
    if 0 <= elapsed < interval:
        return f"Last clean refresh was {int(elapsed)}s ago (within {interval}s interval)"
    return None


def _refresh_cache(archive: Archive, profile_dir: Path) -> tuple[str, str | None, list[str]]:
    """Run one cache refresh and own all throttle bookkeeping. Returns (status, error, failures).

    The throttle token is dropped when the refresh starts and, unless the refresh finished cleanly
    (success and no per-item failures), dropped again when it ends. A refresh that raises, is killed,
    reports failures, or fails after a concurrent clean refresh therefore never leaves a token behind.
    """
    archive.clear_clean_refresh("teams")
    clean = False
    try:
        if not profile_dir.exists():
            error = f"Teams personal profile not found ({profile_dir})"
            return "failed", error, [error]
        outcome = ingest_cache(archive, profile_dir)
        if not outcome.success:
            error = outcome.error or (outcome.failures[0] if outcome.failures else "Cache refresh failed")
            return "failed", error, list(outcome.failures)
        if not outcome.failures:
            archive.set_clean_refresh("teams", _now().timestamp(), _profile_key(profile_dir))
            clean = True
        return "ok", None, list(outcome.failures)
    finally:
        if not clean:
            archive.clear_clean_refresh("teams")


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

    if args.cmd == "clean-placeholder-messages":
        return clean_placeholder_messages(_open_one_shot_archive(args, failures), write=args.write)

    archive = (Archive.open_read_only(args.archive_path) if args.cmd == "download" and args.no_refresh
               else Archive(db_path=args.archive_path))

    if args.cmd == "import-export":
        return import_export(archive, args.path, failures=failures)

    profile_dir = _profile_dir(args)
    refresh_status = "ok"
    refresh_error: str | None = None
    refresh_reason: str | None = None

    if getattr(args, "no_refresh", False):
        refresh_status = "skipped"
        refresh_reason = "Refresh skipped because --no-refresh was specified"
    elif not getattr(args, "refresh", False) and (throttle_reason := _throttle_reason(archive, profile_dir)) is not None:
        refresh_status = "skipped"
        refresh_reason = throttle_reason
    else:
        refresh_status, refresh_error, refresh_failures = _refresh_cache(archive, profile_dir)
        failures.extend(refresh_failures)

    current_up_to = archive.get_data_current_up_to("teams")
    refresh_outcome: dict[str, Any] = {
        "status": refresh_status,
        "current_up_to": current_up_to,
    }
    if refresh_reason:
        refresh_outcome["reason"] = refresh_reason
    if current_up_to is None:
        refresh_outcome["current_up_to_reason"] = "The Teams cache has never been synced successfully; data currency is unknown"
    if refresh_status == "failed":
        refresh_outcome["error"] = refresh_error

    # A source error must not masquerade as "no conversations": when the refresh fails and the archive holds no Teams data, every query command reports an error
    if refresh_status == "failed" and not archive.query_conversations("teams"):
        raise RuntimeError(f"Teams cache source error: {refresh_error} (no conversations in the archive)")

    backup_outcome = backup_status(failures)

    if args.cmd == "download":
        alternatives: list[dict[str, Any]] = []
        conv_id = None
        if args.conversation:
            conv, alternatives = resolve_conversation_for_query(archive, args.conversation, refresh_status, refresh_error)
            conv_id = conv["id"]
        try:
            result = download_message(archive, args.message_id, Path(args.to), conv_id, args.name)
        except Exception as e:  # noqa: BLE001
            if not alternatives:
                raise
            # The newest conversation may not hold the message: keep the candidates so the caller can retry by id
            result = {"error": str(e)}
        result.update(refresh=refresh_outcome, backup=backup_outcome, failures=failures)
        if alternatives:
            result["alternatives"] = alternatives
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
        conv, alternatives = resolve_conversation_for_query(archive, args.conversation, refresh_status, refresh_error)
        msgs = archive.query_messages(
            channel="teams",
            conversation_id=conv["id"],
            limit=args.limit,
            offset=args.offset,
            start=args.start,
            end=args.end,
        )
        out: dict[str, Any] = {
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
        if alternatives:
            out["alternatives"] = alternatives
        return out

    if args.cmd == "search":
        conv_id = None
        alternatives = []
        if args.conversation:
            conv, alternatives = resolve_conversation_for_query(archive, args.conversation, refresh_status, refresh_error)
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

        out = {
            "keyword": args.keyword,
            "messages": out_messages,
            "refresh": refresh_outcome,
            "backup": backup_outcome,
            "failures": failures,
        }
        if alternatives:
            out["alternatives"] = alternatives
        return out

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
    if getattr(args, "refresh", False) and getattr(args, "no_refresh", False):
        parser.error("--refresh and --no-refresh cannot be used together")

    try:
        configure_settings(args.settings)
        result = _run(args)
        code = 1 if args.cmd == "download" and "error" in result else 0
    except Exception as e:  # noqa: BLE001
        result = {"error": str(e)}
        code = 1

    print(json.dumps(result, ensure_ascii=False, indent=None if code else 2))
    return code
