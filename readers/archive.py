"""Channel-agnostic message and conversation archive (ADR-0006).

Cumulative archive shared across channels. Once written, messages and conversations are not lost when a platform purges its cache.
Tables are keyed by (channel, id) and (channel, conversation_id, id), so channels stay isolated from each other.
Versioning follows "newest version wins", and an older version never overwrites a deleted state.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from readers.settings import get_path_setting
from readers.timeparse import parse_time_bound

REPLACEMENT_CHARACTER = "\ufffd"


def _load_sources(raw: str | None) -> list[str]:
    try:
        return json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_json_field(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _merge_attachments(old: str | None, new: str | None) -> str:
    previous = _parse_json_field(old, [])
    incoming = _parse_json_field(new, [])
    if not incoming:
        return json.dumps(previous, ensure_ascii=False)
    merged = []
    for index, item in enumerate(incoming):
        url = item["url"]
        if url is None:
            if index < len(previous) and previous[index]["name"] == item["name"]:
                url = previous[index]["url"]
            else:
                matches = [entry for entry in previous if entry["name"] == item["name"]]
                if len(matches) == 1:
                    url = matches[0]["url"]
        merged.append({"name": item["name"], "url": url})
    return json.dumps(merged, ensure_ascii=False)


def _merge_attachments_sql(old: str, new: str) -> str:
    """SQL version of the attachment merge (rules: see _merge_attachments); keeps the old value as-is when the new one is empty or equal to it, without going through Python."""
    return (
        f"CASE WHEN COALESCE({new}, '[]') = '[]' OR {new} = {old} THEN {old} "
        f"ELSE merge_attachments({old}, {new}) END"
    )


def _row_to_conversation(row: sqlite3.Row) -> dict[str, Any]:
    unread_raw = row["unread"]
    # Unknown (None) is emitted as null as-is and must not be treated as read (spec #10: never a stale value)
    unread = bool(unread_raw) if unread_raw is not None else None
    res = {
        "id": row["id"],
        "name": row["name"],
        "is_group": bool(row["is_group"]),
        "unread": unread,
        "last_time": row["last_message_time"] or "",
        "last_timestamp": int(row["last_message_timestamp"] or 0),
    }
    try:
        res["last_seen_in_cache"] = row["last_seen_in_cache"]
    except (IndexError, KeyError):
        res["last_seen_in_cache"] = None
    return res


def _row_to_message(r: sqlite3.Row, has_sources: bool = True) -> dict[str, Any]:
    files = _parse_json_field(r["files"], [])
    quoted = _parse_json_field(r["quoted"], None)
    sources = _load_sources(r["sources"]) if has_sources else ["cache"]
    return {
        "id": r["id"],
        "conversation_id": r["conversation_id"],
        "time": r["time"],
        "timestamp": r["timestamp"],
        "sender_id": r["sender_id"],
        "sender": r["sender"],
        "is_self": bool(r["is_self"]),
        "type": r["type"],
        "text": r["text"],
        "version": r["version"],
        "edited": bool(r["edited"]),
        "deleted": bool(r["deleted"]),
        "files": files,
        "attachments": _parse_json_field(r["attachments"], []),
        "quoted": quoted,
        "sources": sources,
    }


def _merge_sources(raw: str | None, source: str) -> str:
    sources = _load_sources(raw)
    if source not in sources:
        sources.append(source)
    return json.dumps(sources)


def get_default_data_dir() -> Path:
    """Get the reader's user data directory; unaffected by archive path overrides."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "local-message-readers"


def get_default_archive_path() -> Path:
    """Get the default archive SQLite file path (under the user's local application directory, outside the repo)."""
    configured = get_path_setting("archive_path")
    if configured:
        return configured
    return get_default_data_dir() / "archive.db"


SCHEMA_VERSION = 2

# Send time (milliseconds): use the id when the message id is a millisecond number, otherwise convert timestamp (seconds)
# Existing message rows seen only by "the same source arriving now" (m is messages, s is _stage_messages)
_SAME_SOURCE_ONLY_SQL = (
    "(json_array_length(m.sources) = 1 AND json_extract(m.sources, '$[0]') = s.source)"
)
_REREAD_OVERWRITES_SQL = f"(NOT s.seen_this_import AND {_SAME_SOURCE_ONLY_SQL})"

_SEND_MS_SQL = (
    "CASE WHEN id <> '' AND id NOT GLOB '*[^0-9]*' AND CAST(id AS REAL) >= 1e11 "
    "THEN CAST(id AS REAL) ELSE timestamp * 1000.0 END"
)


def _iso_to_ms_sql(column_expr: str) -> str:
    return f"COALESCE(CAST(strftime('%s', {column_expr}) AS REAL) * 1000.0, 0)"


def _migrate_teams_legacy_versions(conn: sqlite3.Connection) -> None:
    """V1 legacy version rule (one-time, Teams only).

    Rows whose version only reflects the send time (or ldb_seq_no) cannot be ordered against other sources; their content was current when we last observed it,
    so new version = max(send time, observation time). Target: rows whose sources do not include export and whose version <= send time.
    After that, every comparison is a plain comparison of the rows' own versions.
    """
    conn.execute(f"""
        UPDATE messages
        SET version = MAX({_SEND_MS_SQL}, {_iso_to_ms_sql("last_seen")})
        WHERE channel = 'teams'
          AND COALESCE(sources, '') NOT LIKE '%"export"%'
          AND version <= ({_SEND_MS_SQL})
    """)
    conn.execute(f"""
        UPDATE cache_messages
        SET version = MAX({_SEND_MS_SQL}, {_iso_to_ms_sql("last_seen")})
        WHERE channel = 'teams'
          AND version <= ({_SEND_MS_SQL})
    """)
    conn.execute(f"""
        UPDATE conversations
        SET version = MAX(
            last_message_timestamp * 1000.0,
            {_iso_to_ms_sql("COALESCE(last_seen_in_cache, last_seen)")}
        )
        WHERE channel = 'teams'
          AND COALESCE(sources, '') NOT LIKE '%"export"%'
          AND version <= last_message_timestamp * 1000.0
    """)


class Archive:
    """Channel-agnostic SQLite archive."""

    def fffd_candidates(self, channel: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT conversation_id, id, text, sources FROM messages WHERE channel = ? AND instr(text, ?) > 0",
                (channel, REPLACEMENT_CHARACTER),
            ).fetchall()
            return [dict(row) for row in rows]

    def system_messages(self, channel: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT conversation_id, id, text FROM messages WHERE channel = ? AND type = 'system'",
                (channel,),
            ).fetchall()
            return [dict(row) for row in rows]

    def repair_texts(
        self,
        channel: str,
        repairs: list[tuple[str, str, str, str]],
        *,
        cache_only: bool = True,
        require_mirror: bool = True,
        message_type: str | None = None,
    ) -> int:
        """Atomically replace only message text and its cache mirror, preserving metadata.

        Each row is rewritten only while it still holds the old text; any mismatch cancels the whole batch.
        cache_only: the message must come from the cache alone. require_mirror: the cache mirror must hold
        the same old text (otherwise it is left alone). message_type: restrict both rows to this type.
        """
        type_clause, type_args = (" AND type = ?", (message_type,)) if message_type is not None else ("", ())
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            for conversation_id, message_id, old, new in repairs:
                key = (channel, conversation_id, message_id)
                if cache_only:
                    row = conn.execute(
                        "SELECT sources FROM messages WHERE channel = ? AND conversation_id = ? AND id = ?", key
                    ).fetchone()
                    if row is None or _load_sources(row["sources"]) != ["cache"]:
                        raise RuntimeError("archive message sources changed; repair cancelled")
                cur = conn.execute(
                    "UPDATE messages SET text = ? WHERE channel = ? AND conversation_id = ? AND id = ? AND text = ?"
                    + type_clause,
                    (new, *key, old, *type_args),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("archive messages changed; repair cancelled")
                cur = conn.execute(
                    "UPDATE cache_messages SET text = ? WHERE channel = ? AND conversation_id = ? AND id = ? AND text = ?"
                    + type_clause,
                    (new, *key, old, *type_args),
                )
                if require_mirror and cur.rowcount != 1:
                    raise RuntimeError("cache mirror is missing or changed; repair cancelled")
        return len(repairs)

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            self.db_path = get_default_archive_path()
        else:
            self.db_path = Path(db_path)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    _read_only = False

    @classmethod
    def open_read_only(cls, db_path: str | Path | None = None) -> Archive:
        """Open an existing, already-migrated archive over a read-only connection; creates no file, migrates nothing, writes nothing.

        Raises RuntimeError if the file does not exist or its schema version is below SCHEMA_VERSION (run a normal teams-reader command first).
        """
        path = Path(db_path) if db_path is not None else get_default_archive_path()
        if not path.is_file():
            raise RuntimeError(f"archive not found: '{path}'. Run a normal teams-reader command first to create and migrate the archive")
        self = cls.__new__(cls)
        self.db_path = path
        self._read_only = True
        try:
            with closing(self._connect()) as conn:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        except sqlite3.DatabaseError as e:
            raise RuntimeError(f"Cannot open archive '{path}' read-only: {type(e).__name__}") from e
        required = {"conversations", "messages", "cache_messages", "conversation_aliases"}
        if version < SCHEMA_VERSION or not required <= tables:
            raise RuntimeError(
                f"archive has not been migrated to the current version (user_version={version}). Run a normal teams-reader command (such as conversations) first, then reconcile"
            )
        return self

    @classmethod
    def open_existing_for_write(cls, db_path: str | Path | None = None) -> Archive:
        """Validate an existing archive without migration, then permit explicit writes."""
        self = cls.open_read_only(db_path)
        self._read_only = False
        return self

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            conn = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.create_function("merge_attachments", 2, _merge_attachments)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    channel TEXT NOT NULL,
                    id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    is_group INTEGER NOT NULL,
                    unread INTEGER,
                    last_message_time TEXT,
                    last_message_timestamp REAL DEFAULT 0,
                    version REAL DEFAULT 0,
                    last_seen TEXT,
                    sources TEXT,
                    PRIMARY KEY (channel, id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    time TEXT,
                    timestamp REAL DEFAULT 0,
                    sender_id TEXT,
                    sender TEXT,
                    is_self INTEGER DEFAULT 0,
                    type TEXT,
                    text TEXT,
                    version REAL DEFAULT 0,
                    deleted INTEGER DEFAULT 0,
                    edited INTEGER DEFAULT 0,
                    files TEXT,
                    attachments TEXT,
                    quoted TEXT,
                    sources TEXT,
                    first_seen TEXT,
                    last_seen TEXT,
                    PRIMARY KEY (channel, conversation_id, id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache_messages (
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    time TEXT,
                    timestamp REAL DEFAULT 0,
                    sender_id TEXT,
                    sender TEXT,
                    is_self INTEGER DEFAULT 0,
                    type TEXT,
                    text TEXT,
                    version REAL DEFAULT 0,
                    deleted INTEGER DEFAULT 0,
                    edited INTEGER DEFAULT 0,
                    files TEXT,
                    attachments TEXT,
                    quoted TEXT,
                    last_seen TEXT,
                    PRIMARY KEY (channel, conversation_id, id)
                )
            """)

            # Migration: Ensure files and quoted columns exist in messages table
            msg_cols = [c[1] for c in conn.execute("PRAGMA table_info(messages)").fetchall()]
            if "files" not in msg_cols:
                conn.execute("ALTER TABLE messages ADD COLUMN files TEXT")
            if "quoted" not in msg_cols:
                conn.execute("ALTER TABLE messages ADD COLUMN quoted TEXT")

            for table in ("messages", "cache_messages"):
                cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
                if "attachments" not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN attachments TEXT")

            # Migration: Ensure last_seen_in_cache column exists in conversations table
            conv_cols = [c[1] for c in conn.execute("PRAGMA table_info(conversations)").fetchall()]
            if "last_seen_in_cache" not in conv_cols:
                conn.execute("ALTER TABLE conversations ADD COLUMN last_seen_in_cache TEXT")
                conn.execute("""
                    UPDATE conversations
                    SET last_seen_in_cache = last_seen
                    WHERE sources LIKE '%"cache"%' AND last_seen_in_cache IS NULL
                """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    channel TEXT NOT NULL,
                    source TEXT NOT NULL,
                    last_sync_time TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY (channel, source)
                )
            """)

            # Migration: conversation member mri and aliases (for merging 1-to-1 conversations across sources)
            if "member_mris" not in conv_cols:
                conn.execute("ALTER TABLE conversations ADD COLUMN member_mris TEXT")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_aliases (
                    channel TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    alias_name TEXT,
                    PRIMARY KEY (channel, alias)
                )
            """)

            # One-time schema migration (guarded by PRAGMA user_version)
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if user_version < 1:
                _migrate_teams_legacy_versions(conn)
            if user_version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            # Composite indexes for efficient pagination (channel, conversation_id, timestamp ASC, id ASC)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_lookup
                ON messages (channel, conversation_id, timestamp ASC, id ASC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_channel_id
                ON messages (channel, id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_channel_ts
                ON messages (channel, timestamp, id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cache_messages_lookup
                ON cache_messages (channel, conversation_id, timestamp ASC, id ASC)
            """)
            conn.commit()

    def record_sync(
        self,
        channel: str,
        source: str = "cache",
        status: str = "ok",
        sync_time: str | None = None,
    ) -> None:
        """Record the sync status and time for the given channel and source."""
        t = sync_time or datetime.now().astimezone().isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO sync_state (channel, source, last_sync_time, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (channel, source) DO UPDATE SET
                    last_sync_time = excluded.last_sync_time,
                    status = excluded.status
                """,
                (channel, source, t, status),
            )
            conn.commit()

    def get_data_current_up_to(self, channel: str) -> str | None:
        """Get the time up to which the data is current (ISO 8601 format).

        Uses only successful cache sync times; returns None if there has never been a successful sync.
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT last_sync_time FROM sync_state WHERE channel = ? AND source = 'cache' AND status = 'ok'",
                (channel,),
            ).fetchone()
            return str(row[0]) if row and row[0] else None

    def get_sync_status(self, channel: str, source: str = "cache") -> str | None:
        """Return the recorded status for (channel, source) in sync_state, or None if absent."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT status FROM sync_state WHERE channel = ? AND source = ?",
                (channel, source),
            ).fetchone()
            return str(row[0]) if row and row[0] else None

    def ingest_conversations(
        self,
        channel: str,
        conversations: list[dict[str, Any]],
        source: str = "cache",
        stats: dict[str, int] | None = None,
    ) -> int:
        """Import normalized conversations.

        Follows newest version wins: if the existing version is newer, main fields are not overwritten and only the source is added.
        When the source is cache (a successful snapshot, including an empty list), the unread of existing conversations absent from this snapshot is set to NULL (meaning the unread state is unknown, not a stale value).
        """
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        ingested = 0
        incoming_ids: set[str] = set()

        with closing(self._connect()) as conn:
            for c in conversations:
                cid = str(c["id"])
                incoming_ids.add(cid)
                name = str(c.get("name", cid))
                is_group = 1 if c.get("is_group") else 0
                unread_val = c.get("unread")
                unread = None if unread_val is None else (1 if unread_val else 0)

                last_time = c.get("last_time") or c.get("last_message_time") or ""
                last_ts = float(c.get("last_timestamp") or c.get("last_message_timestamp") or 0.0)
                version = float(c.get("version", last_ts))
                last_seen_in_cache = now_iso if source == "cache" else c.get("last_seen_in_cache")
                member_mris = c.get("member_mris")
                members_json = json.dumps(list(member_mris), ensure_ascii=False) if member_mris else None

                row = conn.execute(
                    "SELECT version, sources, unread, last_seen_in_cache, last_message_timestamp FROM conversations WHERE channel = ? AND id = ?",
                    (channel, cid),
                ).fetchone()

                if row is None:
                    if stats is not None:
                        stats["conversations_new"] = stats.get("conversations_new", 0) + 1
                    sources = json.dumps([source])
                    conn.execute(
                        """
                        INSERT INTO conversations (
                            channel, id, name, is_group, unread,
                            last_message_time, last_message_timestamp,
                            version, last_seen, sources, last_seen_in_cache, member_mris
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (channel, cid, name, is_group, unread, last_time, last_ts, version, now_iso, sources, last_seen_in_cache, members_json),
                    )
                    ingested += 1
                else:
                    if stats is not None:
                        stats["conversations_overlapping"] = stats.get("conversations_overlapping", 0) + 1
                    cur_version = float(row["version"] or 0.0)
                    sources = _merge_sources(row["sources"], source)
                    cached_seen = last_seen_in_cache or row["last_seen_in_cache"]

                    if version >= cur_version:
                        # Newer version overwrites
                        conn.execute(
                            """
                            UPDATE conversations SET
                                name = ?,
                                is_group = ?,
                                unread = ?,
                                last_message_time = ?,
                                last_message_timestamp = ?,
                                version = ?,
                                last_seen = ?,
                                sources = ?,
                                last_seen_in_cache = ?,
                                member_mris = COALESCE(?, member_mris)
                            WHERE channel = ? AND id = ?
                            """,
                            (name, is_group, unread, last_time, last_ts, version, now_iso, sources, cached_seen, members_json, channel, cid),
                        )
                        ingested += 1
                    else:
                        # Older version does not overwrite main fields; only update sources and last_seen
                        conn.execute(
                            """
                            UPDATE conversations SET
                                sources = ?,
                                last_seen = ?,
                                last_seen_in_cache = COALESCE(?, last_seen_in_cache),
                                member_mris = COALESCE(?, member_mris)
                            WHERE channel = ? AND id = ?
                            """,
                            (sources, now_iso, last_seen_in_cache, members_json, channel, cid),
                        )

            # When the source is cache (a successful snapshot), any conversation that was in the archive but not in this cache
            # has rolled out of the cache, so its unread state is set to NULL (unknown); likewise when the snapshot is empty
            if source == "cache":
                placeholders = ",".join("?" for _ in incoming_ids)
                conn.execute(
                    f"UPDATE conversations SET unread = NULL WHERE channel = ? AND id NOT IN ({placeholders})",
                    [channel, *incoming_ids],
                )

            conn.commit()
        return ingested

    def query_conversations(
        self,
        channel: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """List conversations ordered by last message time."""
        query = "SELECT id, name, is_group, unread, last_message_time, last_message_timestamp, last_seen_in_cache FROM conversations"
        params: list[Any] = []
        if channel:
            query += " WHERE channel = ?"
            params.append(channel)

        query += " ORDER BY last_message_timestamp DESC, id ASC"
        if limit is not None and limit > 0:
            query += " LIMIT ?"
            params.append(limit)

        with closing(self._connect()) as conn:
            return [_row_to_conversation(row) for row in conn.execute(query, params)]

    def get_conversation(self, channel: str, conversation_id: str) -> dict[str, Any] | None:
        """Get a single conversation by channel and id."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, name, is_group, unread, last_message_time, last_message_timestamp, last_seen_in_cache FROM conversations WHERE channel = ? AND id = ?",
                (channel, conversation_id),
            ).fetchone()
            if row is None:
                # Old id alias -> merged conversation
                row = conn.execute(
                    """
                    SELECT c.id, c.name, c.is_group, c.unread, c.last_message_time,
                           c.last_message_timestamp, c.last_seen_in_cache
                    FROM conversation_aliases a
                    JOIN conversations c ON c.channel = a.channel AND c.id = a.conversation_id
                    WHERE a.channel = ? AND a.alias = ?
                    """,
                    (channel, conversation_id),
                ).fetchone()
            if row is None:
                return None
            return _row_to_conversation(row)

    def add_conversation_alias(
        self,
        channel: str,
        alias: str,
        conversation_id: str,
        alias_name: str | None = None,
    ) -> None:
        """Register an old conversation id (and its display name) as an alias of the merged conversation."""
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO conversation_aliases (channel, alias, conversation_id, alias_name) VALUES (?, ?, ?, ?)",
                (channel, alias, conversation_id, alias_name),
            )
            conn.commit()

    def merge_conversation_into(
        self,
        channel: str,
        source_id: str,
        target_id: str,
        failures: list[str] | None = None,
        stats: dict[str, int] | None = None,
    ) -> int:
        """Merge the messages of conversation source_id into target_id (single transaction); returns the number of messages moved.

        Message ids the target lacks are simply re-parented to the target; where both sides have one, the higher version is kept (same version with different content is recorded as an
        unorderable conflict and the target's current state is kept), sources are unioned, first_seen takes the earliest and last_seen the latest.
        Afterwards the source's conversation row is deleted and the source (with its name) is registered as an alias of the target.
        If the source does not exist (no conversation row and no messages), nothing is done and 0 is returned.
        """
        if source_id == target_id:
            return 0
        moved = 0
        with closing(self._connect()) as conn:
            src_conv = conn.execute(
                "SELECT name FROM conversations WHERE channel = ? AND id = ?", (channel, source_id)
            ).fetchone()
            src_msgs = conn.execute(
                "SELECT * FROM messages WHERE channel = ? AND conversation_id = ?", (channel, source_id)
            ).fetchall()
            if src_conv is None and not src_msgs:
                return 0

            dst = {
                r["id"]: r
                for r in conn.execute(
                    "SELECT * FROM messages WHERE channel = ? AND conversation_id = ?", (channel, target_id)
                )
            }
            content_cols = (
                "time", "timestamp", "sender_id", "sender", "is_self", "type", "text",
                "version", "deleted", "edited", "files", "attachments", "quoted",
            )
            for s in src_msgs:
                d = dst.get(s["id"])
                if d is None:
                    conn.execute(
                        "UPDATE messages SET conversation_id = ? WHERE channel = ? AND conversation_id = ? AND id = ?",
                        (target_id, channel, source_id, s["id"]),
                    )
                    moved += 1
                    continue
                sources: list[str] = list(_load_sources(d["sources"]))
                for x in _load_sources(s["sources"]):
                    if x not in sources:
                        sources.append(x)
                firsts = [x for x in (d["first_seen"], s["first_seen"]) if x]
                lasts = [x for x in (d["last_seen"], s["last_seen"]) if x]
                sv, dv = float(s["version"] or 0), float(d["version"] or 0)
                source_sources = _load_sources(s["sources"])
                same_source = conn.execute(
                    f"SELECT {_SAME_SOURCE_ONLY_SQL} FROM (SELECT ? AS sources) m, (SELECT ? AS source) s",
                    (d["sources"], source_sources[0] if len(source_sources) == 1 else None),
                ).fetchone()[0]
                if sv > dv or (sv == dv and same_source):
                    sets = ", ".join(f"{c} = ?" for c in content_cols)
                    content = [s[c] for c in content_cols]
                    if s["files"] == d["files"]:
                        content[content_cols.index("attachments")] = _merge_attachments(d["attachments"], s["attachments"])
                else:
                    sets = "attachments = ?"
                    same_files = s["files"] == d["files"]
                    # The retained target is the base; only fill its missing links from source.
                    content = [_merge_attachments(s["attachments"], d["attachments"]) if same_files else d["attachments"]]
                    differs = any(
                        (s[c] or "") != (d[c] or "") for c in ("text", "type", "files", "quoted")
                    ) or s["deleted"] != d["deleted"] or s["edited"] != d["edited"]
                    if sv == dv and differs:
                        if failures is not None:
                            failures.append(
                                f"Version conflict (cannot be ordered): conversation_id='{target_id}', message_id='{s['id']}', version={sv}: content differs, keeping current state"
                            )
                        if stats is not None:
                            stats["unorderable_conflicts"] = stats.get("unorderable_conflicts", 0) + 1
                conn.execute(
                    f"UPDATE messages SET {sets}, sources = ?, first_seen = ?, last_seen = ? "
                    "WHERE channel = ? AND conversation_id = ? AND id = ?",
                    [
                        *content,
                        json.dumps(sources),
                        min(firsts) if firsts else None,
                        max(lasts) if lasts else None,
                        channel,
                        target_id,
                        s["id"],
                    ],
                )
                moved += 1
            conn.execute(
                "DELETE FROM messages WHERE channel = ? AND conversation_id = ?", (channel, source_id)
            )
            conn.execute(f"""
                UPDATE cache_messages AS target
                SET attachments = {_merge_attachments_sql("target.attachments", "source.attachments")}
                FROM cache_messages AS source
                WHERE target.channel = ? AND target.conversation_id = ?
                  AND source.channel = target.channel AND source.conversation_id = ?
                  AND source.id = target.id AND COALESCE(source.version, 0) <= COALESCE(target.version, 0)
                  AND source.files = target.files
            """, (channel, target_id, source_id))
            conn.execute(f"""
                UPDATE cache_messages AS source
                SET attachments = {_merge_attachments_sql("target.attachments", "source.attachments")}
                FROM cache_messages AS target
                WHERE source.channel = ? AND source.conversation_id = ?
                  AND target.channel = source.channel AND target.conversation_id = ?
                  AND target.id = source.id AND COALESCE(source.version, 0) > COALESCE(target.version, 0)
                  AND source.files = target.files
            """, (channel, source_id, target_id))
            conn.execute(
                """DELETE FROM cache_messages AS target
                   WHERE target.channel = ? AND target.conversation_id = ?
                   AND EXISTS (SELECT 1 FROM cache_messages AS source
                       WHERE source.channel = target.channel AND source.conversation_id = ?
                       AND source.id = target.id AND COALESCE(source.version, 0) > COALESCE(target.version, 0))""",
                (channel, target_id, source_id),
            )
            conn.execute(
                "UPDATE OR IGNORE cache_messages SET conversation_id = ? WHERE channel = ? AND conversation_id = ?",
                (target_id, channel, source_id),
            )
            conn.execute(
                "DELETE FROM cache_messages WHERE channel = ? AND conversation_id = ?", (channel, source_id)
            )
            conn.execute(
                """
                UPDATE conversations
                SET (last_message_time, last_message_timestamp) = (
                    SELECT m.time, m.timestamp FROM messages m
                    WHERE m.channel = conversations.channel AND m.conversation_id = conversations.id
                    ORDER BY m.timestamp DESC, m.id DESC LIMIT 1
                )
                WHERE channel = ? AND id = ?
                  AND EXISTS (SELECT 1 FROM messages m WHERE m.channel = conversations.channel AND m.conversation_id = conversations.id)
                """,
                (channel, target_id),
            )
            conn.execute("DELETE FROM conversations WHERE channel = ? AND id = ?", (channel, source_id))
            conn.execute(
                "UPDATE conversation_aliases SET conversation_id = ? WHERE channel = ? AND conversation_id = ?",
                (target_id, channel, source_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO conversation_aliases (channel, alias, conversation_id, alias_name) VALUES (?, ?, ?, ?)",
                (channel, source_id, target_id, src_conv["name"] if src_conv else None),
            )
            conn.commit()
        return moved

    def list_conversation_members(self, channel: str) -> dict[str, list[str]]:
        """Return {conversation_id: [member mri, ...]} (only conversations with recorded members)."""
        result: dict[str, list[str]] = {}
        with closing(self._connect()) as conn:
            for row in conn.execute(
                "SELECT id, member_mris FROM conversations WHERE channel = ? AND member_mris IS NOT NULL",
                (channel,),
            ):
                members = _parse_json_field(row["member_mris"], [])
                if isinstance(members, list) and members:
                    result[row["id"]] = [str(x) for x in members]
        return result

    def count_cache_conversation_overlaps(
        self,
        channel: str,
        message_ids: list[str],
        exclude_conversation_id: str | None = None,
    ) -> dict[str, int]:
        """Return {conversation_id: number of shared message ids}, counting only non-group conversations from the cache (queried in batches)."""
        counts: dict[str, int] = {}
        step = 500
        with closing(self._connect()) as conn:
            for i in range(0, len(message_ids), step):
                part = message_ids[i : i + step]
                placeholders = ",".join("?" for _ in part)
                rows = conn.execute(
                    f"""
                    SELECT m.conversation_id, COUNT(*)
                    FROM messages m
                    JOIN conversations c ON c.channel = m.channel AND c.id = m.conversation_id
                    WHERE m.channel = ? AND m.id IN ({placeholders})
                      AND c.is_group = 0 AND COALESCE(c.sources, '') LIKE '%"cache"%'
                    GROUP BY m.conversation_id
                    """,
                    [channel, *part],
                ).fetchall()
                for cid, n in rows:
                    if cid != exclude_conversation_id:
                        counts[cid] = counts.get(cid, 0) + int(n)
        return counts

    def get_alias_target(self, channel: str, alias: str) -> str | None:
        """Read-only: the merged conversation id that an old conversation id alias maps to; None if no alias is registered."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT conversation_id FROM conversation_aliases WHERE channel = ? AND alias = ?",
                (channel, alias),
            ).fetchone()
        return row[0] if row else None

    def list_cache_conversation_ids(self, channel: str) -> list[str]:
        """Read-only: conversation ids that have messages in cache_messages (uses the channel, conversation_id prefix index)."""
        with closing(self._connect()) as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT conversation_id FROM cache_messages WHERE channel = ?",
                    (channel,),
                )
            ]

    def get_cache_messages_for_reconcile(self, channel: str, conversation_id: str) -> dict[str, dict[str, Any]]:
        """Read-only: {message id: comparison fields as presented by the cache itself} (reads only cache_messages, excludes merged content)."""
        out: dict[str, dict[str, Any]] = {}
        with closing(self._connect()) as conn:
            for r in conn.execute(
                """
                SELECT id, time, timestamp, sender_id, text, edited, deleted
                FROM cache_messages
                WHERE channel = ? AND conversation_id = ?
                """,
                (channel, conversation_id),
            ):
                out[r["id"]] = {
                    "id": r["id"],
                    "time": r["time"] or "",
                    "timestamp": float(r["timestamp"] or 0.0),
                    "sender_id": r["sender_id"] or "",
                    "text": r["text"] or "",
                    "edited": bool(r["edited"]),
                    "deleted": bool(r["deleted"]),
                }
        return out

    def list_cache_one_to_one_message_ids(self, channel: str) -> list[tuple[str, str]]:
        """Read-only: [(message id, cache non-group conversation id)], used to match 1-to-1 conversations by shared message ids (reads only cache_messages)."""
        with closing(self._connect()) as conn:
            return [
                (r[0], r[1])
                for r in conn.execute(
                    """
                    SELECT m.id, m.conversation_id
                    FROM cache_messages m
                    JOIN conversations c ON c.channel = m.channel AND c.id = m.conversation_id
                    WHERE m.channel = ? AND c.is_group = 0
                    """,
                    (channel,),
                )
            ]

    def count_messages(self, channel: str) -> int:
        """Number of rows in the messages table for the given channel."""
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM messages WHERE channel = ?", (channel,)).fetchone()[0])

    def find_conversations_by_name(self, channel: str, name: str) -> list[dict[str, Any]]:
        """Get all conversations of the given channel whose name matches."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, name, is_group, unread, last_message_time, last_message_timestamp, last_seen_in_cache FROM conversations WHERE channel = ? AND name = ?",
                (channel, name),
            ).fetchall()
            found = [_row_to_conversation(r) for r in rows]
            seen = {c["id"] for c in found}
            # The display name of an old source (alias) also points to the merged conversation
            alias_rows = conn.execute(
                """
                SELECT DISTINCT c.id, c.name, c.is_group, c.unread, c.last_message_time,
                       c.last_message_timestamp, c.last_seen_in_cache
                FROM conversation_aliases a
                JOIN conversations c ON c.channel = a.channel AND c.id = a.conversation_id
                WHERE a.channel = ? AND a.alias_name = ?
                """,
                (channel, name),
            ).fetchall()
            for r in alias_rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    found.append(_row_to_conversation(r))
            return found

    def ingest_messages(
        self,
        channel: str,
        messages: list[dict[str, Any]],
        source: str = "cache",
        failures: list[str] | None = None,
        stats: dict[str, int] | None = None,
        seen_this_import: set[tuple[str, str]] | None = None,
    ) -> int:
        """Batch-import normalized messages.

        Rules:
        1. newest version wins (compared by the message's own version, independent of import order)
        2. A deleted message is not resurrected by older non-deleted data arriving later (older versions do not overwrite); a newer version (such as the sender undoing the deletion) wins
        3. A message read from both cache and export is stored once, with sources containing both
        4. Messages from the cache source are additionally kept in cache_messages (for reconciliation)
        5. Always processed in a single transaction in batches (executemany and set updates), not row-by-row Python statements.
        6. Same version with different content seen earlier in the same import is a conflict; re-reading from the same source in a different import may update the normalized result.
        """
        if not messages:
            return 0

        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        stage_rows = []
        batch_seen: dict[tuple[str, str], tuple[Any, ...]] = {}
        batch_conflicts: set[tuple[str, str]] = set()

        for m in messages:
            cid = str(m["conversation_id"])
            mid = str(m["id"])
            time_str = m.get("time", "")
            ts = float(m.get("timestamp") or 0.0)
            sender_id = str(m.get("sender_id", ""))
            sender = str(m.get("sender", ""))
            is_self = 1 if m.get("is_self") else 0
            msg_type = str(m.get("type", "text"))
            text = str(m.get("text", ""))
            version = float(m.get("version", ts))
            deleted = 1 if m.get("deleted") else 0
            edited = 1 if m.get("edited") else 0
            files_json = json.dumps(m.get("files") or [], ensure_ascii=False)
            attachments_json = json.dumps(m.get("attachments") or [], ensure_ascii=False)
            quoted_val = m.get("quoted")
            quoted_json = json.dumps(quoted_val, ensure_ascii=False) if quoted_val else None
            already_seen = int(seen_this_import is not None and (cid, mid) in seen_this_import)

            row = (
                channel, cid, mid, time_str, ts, sender_id, sender,
                is_self, msg_type, text, version, deleted, edited,
                files_json, quoted_json, source, already_seen, attachments_json,
            )
            stage_rows.append(row)

            # Same (conversation, id) and same version but different content within the batch: cannot be ordered; keep the first and report
            key = (cid, mid)
            first = batch_seen.setdefault(key, row)
            if first is not row and version > first[10]:
                batch_seen[key] = row
            elif first is not row and first[10] == version and key not in batch_conflicts:
                differs = any(first[i] != row[i] for i in (8, 9, 11, 12, 13)) or (first[14] or "") != (row[14] or "")
                if differs:
                    batch_conflicts.add(key)
                    if failures is not None:
                        failures.append(
                            f"Version conflict (cannot be ordered): conversation_id='{cid}', message_id='{mid}', version={version}: content differs, keeping current state"
                        )
                    if stats is not None:
                        stats["unorderable_conflicts"] = stats.get("unorderable_conflicts", 0) + 1

        if seen_this_import is not None:
            seen_this_import.update(batch_seen)

        with closing(self._connect()) as conn:
            conn.execute("""
                CREATE TEMP TABLE IF NOT EXISTS _stage_messages (
                    channel TEXT,
                    conversation_id TEXT,
                    id TEXT,
                    time TEXT,
                    timestamp REAL,
                    sender_id TEXT,
                    sender TEXT,
                    is_self INTEGER,
                    type TEXT,
                    text TEXT,
                    version REAL,
                    deleted INTEGER,
                    edited INTEGER,
                    files TEXT,
                    quoted TEXT,
                    source TEXT,
                    seen_this_import INTEGER,
                    attachments TEXT,
                    PRIMARY KEY (channel, conversation_id, id)
                )
            """)
            conn.execute("DELETE FROM _stage_messages")

            conn.executemany(f"""
                INSERT INTO _stage_messages (
                    channel, conversation_id, id, time, timestamp, sender_id, sender,
                    is_self, type, text, version, deleted, edited, files, quoted, source, seen_this_import, attachments
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (channel, conversation_id, id) DO UPDATE SET
                    {", ".join(f"{col} = CASE WHEN excluded.version > _stage_messages.version THEN excluded.{col} ELSE _stage_messages.{col} END" for col in ("time", "timestamp", "sender_id", "sender", "is_self", "type", "text", "version", "deleted", "edited", "files", "quoted", "source"))},
                    attachments = CASE
                        WHEN excluded.version > _stage_messages.version THEN excluded.attachments
                        WHEN excluded.files = _stage_messages.files THEN {_merge_attachments_sql("_stage_messages.attachments", "excluded.attachments")}
                        ELSE _stage_messages.attachments END
                WHERE excluded.version >= _stage_messages.version
            """, stage_rows)

            # Count inserted and overlapping rows
            if stats is not None:
                stat_row = conn.execute("""
                    SELECT
                        SUM(CASE WHEN m.id IS NULL THEN 1 ELSE 0 END),
                        SUM(CASE WHEN m.id IS NOT NULL THEN 1 ELSE 0 END)
                    FROM _stage_messages s
                    LEFT JOIN messages m ON m.channel = s.channel AND m.conversation_id = s.conversation_id AND m.id = s.id
                """).fetchone()
                if stat_row:
                    stats["messages_new"] = stats.get("messages_new", 0) + int(stat_row[0] or 0)
                    stats["messages_overlapping"] = stats.get("messages_overlapping", 0) + int(stat_row[1] or 0)

            # Check for unorderable conflicts (different sources, same version, different content).
            # For rows seen only by the same source, re-reading the same version counts as re-normalization and updates directly with the new result (see step 2); not a conflict.
            conflict_rows = conn.execute(f"""
                SELECT s.conversation_id, s.id, s.version, m.version
                FROM _stage_messages s
                JOIN messages m ON m.channel = s.channel AND m.conversation_id = s.conversation_id AND m.id = s.id
                WHERE s.version = m.version
                AND NOT {_REREAD_OVERWRITES_SQL}
                AND (
                    COALESCE(s.text, '') != COALESCE(m.text, '')
                    OR s.deleted != m.deleted
                    OR s.edited != m.edited
                    OR s.type != m.type
                    OR COALESCE(s.files, '') != COALESCE(m.files, '')
                    OR COALESCE(s.quoted, '') != COALESCE(m.quoted, '')
                )
            """).fetchall()
            for crow in conflict_rows:
                c_cid, c_mid, c_sver = crow[0], crow[1], crow[2]
                if failures is not None:
                    failures.append(
                        f"Version conflict (cannot be ordered): conversation_id='{c_cid}', message_id='{c_mid}', version={c_sver}: content differs, keeping current state"
                    )
            if stats is not None:
                stats["unorderable_conflicts"] = stats.get("unorderable_conflicts", 0) + len(conflict_rows)

            # 1. Insert new messages into the messages table
            conn.execute("""
                INSERT INTO messages (
                    channel, conversation_id, id, time, timestamp, sender_id, sender,
                    is_self, type, text, version, deleted, edited, files, attachments, quoted,
                    sources, first_seen, last_seen
                )
                SELECT
                    s.channel, s.conversation_id, s.id, s.time, s.timestamp, s.sender_id, s.sender,
                    s.is_self, s.type, s.text, s.version, s.deleted, s.edited, s.files, s.attachments, s.quoted,
                    json_array(s.source), ?, ?
                FROM _stage_messages s
                LEFT JOIN messages m ON m.channel = s.channel AND m.conversation_id = s.conversation_id AND m.id = s.id
                WHERE m.id IS NULL
            """, (now_iso, now_iso))

            # 2. Update existing messages (the newer own version wins; a sender may undo a deletion, and the newer version wins);
            #    for the same version seen only by the same source, update with the re-normalized result
            conn.execute(f"""
                UPDATE messages AS m
                SET
                    time = s.time,
                    timestamp = s.timestamp,
                    sender_id = s.sender_id,
                    sender = s.sender,
                    is_self = s.is_self,
                    type = s.type,
                    text = s.text,
                    version = s.version,
                    deleted = s.deleted,
                    edited = s.edited,
                    files = s.files,
                    attachments = CASE
                        WHEN s.source != 'cache' THEN m.attachments
                        WHEN s.files = m.files THEN {_merge_attachments_sql("m.attachments", "s.attachments")}
                        WHEN s.version > m.version THEN s.attachments
                        ELSE m.attachments
                    END,
                    quoted = s.quoted,
                    sources = CASE 
                        WHEN instr(m.sources, '"' || s.source || '"') > 0 THEN m.sources 
                        ELSE json_insert(m.sources, '$[#]', s.source) 
                    END,
                    last_seen = ?
                FROM _stage_messages s
                WHERE m.channel = s.channel AND m.conversation_id = s.conversation_id AND m.id = s.id
                  AND (s.version > m.version OR (s.version = m.version AND {_REREAD_OVERWRITES_SQL}))
            """, (now_iso,))

            # 3. Older, or same version but another source already present: do not overwrite content; only add sources and last_seen
            conn.execute(f"""
                UPDATE messages AS m
                SET
                    sources = CASE 
                        WHEN instr(m.sources, '"' || s.source || '"') > 0 THEN m.sources 
                        ELSE json_insert(m.sources, '$[#]', s.source) 
                    END,
                    last_seen = ?
                FROM _stage_messages s
                WHERE m.channel = s.channel AND m.conversation_id = s.conversation_id AND m.id = s.id
                  AND (s.version < m.version OR (s.version = m.version AND NOT {_REREAD_OVERWRITES_SQL}))
            """, (now_iso,))

            # Backfill links from matching older cache rows without changing content or version.
            conn.execute(f"""
                UPDATE messages AS m SET attachments = {_merge_attachments_sql("m.attachments", "s.attachments")}
                FROM _stage_messages s
                WHERE m.channel = s.channel AND m.conversation_id = s.conversation_id AND m.id = s.id
                  AND s.source = 'cache' AND s.version <= m.version AND s.files = m.files
                  AND (s.version < m.version OR NOT {_REREAD_OVERWRITES_SQL})
                  AND COALESCE(s.attachments, '[]') != '[]' AND s.attachments IS NOT m.attachments
            """)

            # 4. If the source is cache (source == 'cache'), also record into cache_messages
            if source == "cache":
                conn.execute("""
                    INSERT INTO cache_messages (
                        channel, conversation_id, id, time, timestamp, sender_id, sender,
                        is_self, type, text, version, deleted, edited, files, attachments, quoted, last_seen
                    )
                    SELECT
                        s.channel, s.conversation_id, s.id, s.time, s.timestamp, s.sender_id, s.sender,
                        s.is_self, s.type, s.text, s.version, s.deleted, s.edited, s.files, s.attachments, s.quoted, ?
                    FROM _stage_messages s
                    LEFT JOIN cache_messages c ON c.channel = s.channel AND c.conversation_id = s.conversation_id AND c.id = s.id
                    WHERE c.id IS NULL
                """, (now_iso,))

                conn.execute(f"""
                    UPDATE cache_messages AS c
                    SET
                        time = s.time,
                        timestamp = s.timestamp,
                        sender_id = s.sender_id,
                        sender = s.sender,
                        is_self = s.is_self,
                        type = s.type,
                        text = s.text,
                        version = s.version,
                        deleted = s.deleted,
                        edited = s.edited,
                        files = s.files,
                        attachments = CASE
                            WHEN s.files = c.files THEN {_merge_attachments_sql("c.attachments", "s.attachments")}
                            WHEN s.version > c.version THEN s.attachments
                            ELSE c.attachments
                        END,
                        quoted = s.quoted,
                        last_seen = ?
                    FROM _stage_messages s
                    WHERE c.channel = s.channel AND c.conversation_id = s.conversation_id AND c.id = s.id
                      AND s.version >= c.version
                """, (now_iso,))

                conn.execute(f"""
                    UPDATE cache_messages AS c SET attachments = {_merge_attachments_sql("c.attachments", "s.attachments")}
                    FROM _stage_messages s
                    WHERE c.channel = s.channel AND c.conversation_id = s.conversation_id AND c.id = s.id
                      AND s.version < c.version AND s.files = c.files
                      AND COALESCE(s.attachments, '[]') != '[]' AND s.attachments IS NOT c.attachments
                """)

            # 5. Ensure the message's conversation has a basic record in the conversations table, and update the latest message time
            conn.execute("""
                INSERT INTO conversations (
                    channel, id, name, is_group, unread,
                    last_message_time, last_message_timestamp,
                    version, last_seen, sources, last_seen_in_cache
                )
                SELECT
                    s.channel, s.conversation_id, s.conversation_id, 0, NULL,
                    MAX(s.time), MAX(s.timestamp), MAX(s.version), ?, json_array(s.source),
                    CASE WHEN s.source = 'cache' THEN ? ELSE NULL END
                FROM _stage_messages s
                LEFT JOIN conversations c ON c.channel = s.channel AND c.id = s.conversation_id
                WHERE c.id IS NULL
                GROUP BY s.channel, s.conversation_id
            """, (now_iso, now_iso if source == "cache" else None))

            conn.execute("""
                UPDATE conversations
                SET (last_message_time, last_message_timestamp) = (
                    SELECT m.time, m.timestamp FROM messages m
                    WHERE m.channel = conversations.channel AND m.conversation_id = conversations.id
                    ORDER BY m.timestamp DESC, m.id DESC LIMIT 1
                )
                WHERE channel = ? AND id IN (SELECT DISTINCT conversation_id FROM _stage_messages)
            """, (channel,))

            conn.execute("DELETE FROM _stage_messages")
            conn.commit()

        return len(messages)

    def query_messages(
        self,
        channel: str,
        conversation_id: str,
        limit: int | None = 50,
        offset: int = 0,
        start: str | float | None = None,
        end: str | float | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query the messages of a single conversation (oldest to newest).

        Supports a time range (--start/--end) and paging (--limit/--offset).
        Messages with the same timestamp are ordered by id, guaranteeing no duplicates or gaps across pages.
        If source='cache' is given, queries the raw content retained by the cache.
        """
        table = "cache_messages" if source == "cache" else "messages"
        has_sources = table == "messages"

        select_cols = "id, conversation_id, time, timestamp, sender_id, sender, is_self, type, text, version, deleted, edited, files, attachments, quoted"
        if has_sources:
            select_cols += ", sources"

        query = f"SELECT {select_cols} FROM {table} WHERE channel = ? AND conversation_id = ?"
        params: list[Any] = [channel, conversation_id]

        start_ts = parse_time_bound(start, is_end=False)
        end_ts = parse_time_bound(end, is_end=True)
        if start_ts is not None and end_ts is not None and start_ts > end_ts:
            raise ValueError("start must not be later than end")

        if start_ts is not None:
            query += " AND timestamp >= ?"
            params.append(start_ts)

        if end_ts is not None:
            query += " AND timestamp <= ?"
            params.append(end_ts)

        query += " ORDER BY timestamp DESC, id DESC"

        if limit is not None and limit > 0:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset > 0:
            query += " LIMIT -1 OFFSET ?"
            params.append(offset)

        results = []
        with closing(self._connect()) as conn:
            results = [_row_to_message(r, has_sources=has_sources) for r in conn.execute(query, params)]
        results.reverse()
        return results

    def find_messages_by_id(self, channel: str, message_id: str) -> list[dict[str, Any]]:
        """Query message ids of the given channel, keeping each conversation's candidates."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, conversation_id, time, timestamp, sender_id, sender, is_self, type, text, version, deleted, edited, files, attachments, quoted, sources FROM messages WHERE channel = ? AND id = ?",
                (channel, message_id),
            )
            return [_row_to_message(row) for row in rows]

    def get_cache_message(
        self,
        channel: str,
        conversation_id: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        """Get the raw data of the message as last presented by the cache (for reconciliation)."""
        with closing(self._connect()) as conn:
            r = conn.execute("""
                SELECT id, conversation_id, time, timestamp, sender_id, sender, is_self,
                       type, text, version, deleted, edited, files, attachments, quoted
                FROM cache_messages
                WHERE channel = ? AND conversation_id = ? AND id = ?
            """, (channel, conversation_id, message_id)).fetchone()
            if not r:
                return None
            return _row_to_message(r, has_sources=False)

    def search_messages(
        self,
        channel: str,
        keyword: str,
        conversation_id: str | None = None,
        limit: int | None = 20,
        start: str | float | None = None,
        end: str | float | None = None,
    ) -> list[dict[str, Any]]:
        """Search messages by keyword (covers readable text, link targets, attachment file names and quoted content).

        If the hit is in quoted/forwarded content, hit_in_quote=True is set, otherwise False.
        Returns the latest `limit` rows, sorted oldest to newest by time.
        """
        if not keyword:
            return []

        # Escape SQL LIKE wildcards
        escaped_kw = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_kw}%"

        query = """
            SELECT id, conversation_id, time, timestamp, sender_id, sender,
                   is_self, type, text, version, deleted, edited, files, attachments, quoted, sources
            FROM messages
            WHERE channel = ?
              AND deleted = 0
              AND (
                  text LIKE ? ESCAPE '\\'
                  OR json_extract(quoted, '$.text') LIKE ? ESCAPE '\\'
                  OR json_extract(quoted, '$.author') LIKE ? ESCAPE '\\'
                  OR files LIKE ? ESCAPE '\\'
              )
        """
        params: list[Any] = [channel, pattern, pattern, pattern, pattern]

        if conversation_id:
            query += " AND conversation_id = ?"
            params.append(conversation_id)

        start_ts = parse_time_bound(start, is_end=False)
        end_ts = parse_time_bound(end, is_end=True)
        if start_ts is not None and end_ts is not None and start_ts > end_ts:
            raise ValueError("start must not be later than end")

        if start_ts is not None:
            query += " AND timestamp >= ?"
            params.append(start_ts)

        if end_ts is not None:
            query += " AND timestamp <= ?"
            params.append(end_ts)

        query += " ORDER BY timestamp DESC, id DESC"

        if limit is not None and limit > 0:
            query += " LIMIT ?"
            params.append(limit)

        kw_lower = keyword.lower()
        results = []
        with closing(self._connect()) as conn:
            for r in conn.execute(query, params):
                msg = _row_to_message(r, has_sources=True)
                quoted = msg["quoted"]
                hit_in_quote = False
                if quoted and isinstance(quoted, dict):
                    q_text = str(quoted.get("text") or "")
                    q_author = str(quoted.get("author") or "")
                    if kw_lower in q_text.lower() or kw_lower in q_author.lower():
                        hit_in_quote = True
                msg["hit_in_quote"] = hit_in_quote
                results.append(msg)

        results.reverse()
        return results

    def query_recent_messages(
        self,
        channel: str,
        since: str | float,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Query all messages (across all conversations) after a given time (boundary second included).

        Determined by original send time; if the total exceeds limit, truncated to the latest `limit` rows,
        and returns (messages oldest to newest, truncated: bool).
        """
        since_ts = parse_time_bound(since, is_end=False)
        if since_ts is None:
            raise ValueError(f"Invalid time format: '{since}'")

        query = """
            SELECT id, conversation_id, time, timestamp, sender_id, sender,
                   is_self, type, text, version, deleted, edited, files, attachments, quoted, sources
            FROM messages
            WHERE channel = ? AND timestamp >= ?
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
        """
        # Fetch one extra row to determine truncation
        fetch_limit = limit + 1 if limit > 0 else 101

        with closing(self._connect()) as conn:
            rows = conn.execute(query, (channel, since_ts, fetch_limit)).fetchall()

        truncated = len(rows) > limit
        if truncated:
            rows = rows[:limit]

        results = [_row_to_message(r, has_sources=True) for r in rows]
        results.reverse()
        return results, truncated
