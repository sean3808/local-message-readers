"""Teams reader package."""

from readers.teams.cli import main, resolve_conversation
from readers.teams.normalizer import (
    _clean_str,
    _iso_from_ms,
    derive_conversation_name,
    derive_is_group,
    derive_unread,
    normalize_html_content,
    normalize_system_event,
    normalize_teams_message,
)
from readers.teams.snapshot import (
    DEFAULT_PROFILE_REL,
    TEMP_PREFIX,
    IngestOutcome,
    SnapshotError,
    get_tfl_profile_dir,
    ingest_cache,
    load_latest_conversation_records,
    load_latest_replychain_records,
)

__all__ = [
    "DEFAULT_PROFILE_REL",
    "TEMP_PREFIX",
    "IngestOutcome",
    "SnapshotError",
    "_clean_str",
    "_iso_from_ms",
    "derive_conversation_name",
    "derive_is_group",
    "derive_unread",
    "get_tfl_profile_dir",
    "ingest_cache",
    "load_latest_conversation_records",
    "load_latest_replychain_records",
    "main",
    "normalize_html_content",
    "normalize_system_event",
    "normalize_teams_message",
    "resolve_conversation",
]
