"""Teams WebView2 IndexedDB snapshot reading and archive import module.

Read-only: copies only the personal profile (WV2Profile_tfl) into a temporary snapshot directory, never touches the work profile (WV2Profile_tfw),
never writes back to Teams files, and deletes the snapshot when done.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ccl_chromium_reader.ccl_chromium_indexeddb import WrappedIndexDB

from readers.archive import Archive
from readers.settings import get_path_setting
from readers.teams.identity import resolve_self_id
from readers.teams.normalizer import (
    _iso_from_ms,
    derive_conversation_name,
    derive_is_group,
    derive_member_mris,
    derive_unread,
    normalize_teams_message,
)

TEMP_PREFIX = "teams-reader-"
DEFAULT_PROFILE_REL = (
    r"Packages\MSTeams_8wekyb3d8bbwe\LocalCache\Microsoft\MSTeams\EBWebView\WV2Profile_tfl"
)


def get_tfl_profile_dir() -> Path:
    """Get the path of the Teams personal profile (WV2Profile_tfl).

    Reads only the personal account profile; the work/school profile (WV2Profile_tfw) is strictly excluded.
    """
    configured = get_path_setting("profile_dir")
    if configured:
        return configured
    base = os.environ.get("LOCALAPPDATA") or ""
    return Path(base) / DEFAULT_PROFILE_REL


@dataclass
class IngestOutcome:
    success: bool
    failures: list[str] = field(default_factory=list)
    error: str | None = None


class SnapshotError(Exception):
    """The snapshot lacks an expected database or store."""


def _is_empty_placeholder(key: Any, value: Any) -> bool:
    """Empty placeholder in the Teams cache replychains: the key's second segment is -1 and the value is None (#24); this is not a parse failure."""
    return isinstance(key, tuple) and len(key) == 2 and key[1] in (-1, "-1") and value is None


UNKNOWN_KEY = object()
"""Marker added to `failed_keys` when a record failed before its key could be read."""


def _plain_key(key_raw: Any) -> Any:
    if isinstance(key_raw, tuple):
        return tuple(x.value if hasattr(x, "value") else x for x in key_raw)
    return key_raw.value if hasattr(key_raw, "value") else key_raw


def _load_latest_store_records(
    store: Any,
    store_name: str,
    failures: list[str] | None = None,
    failed_keys: set[Any] | None = None,
) -> dict[Any, Any]:
    """Read all records of a store and return {key: latest record} (the one with the largest ldb_seq_no wins)."""
    latest_records: dict[Any, Any] = {}

    def _note_failed_key(key: Any) -> None:
        if failed_keys is None:
            return
        try:
            failed_keys.add(_plain_key(key.value))
        except Exception:  # noqa: BLE001
            failed_keys.add(UNKNOWN_KEY)

    def _bad_record(key: Any, _raw: bytes) -> None:
        k_val = getattr(key, "value", str(key))
        _note_failed_key(key)
        if failures is not None:
            failures.append(f"Could not parse record: store='{store_name}', key='{k_val}': deserialization failed")

    try:
        records_iter = store.iterate_records(bad_deserializer_data_handler=_bad_record)
    except TypeError:
        records_iter = store.iterate_records()

    for r in records_iter:
        try:
            k = _plain_key(r.key.value)
            seq = getattr(r, "ldb_seq_no", None)
            if _is_empty_placeholder(k, r.value):
                continue
            if r.value is None or not isinstance(r.value, dict):
                raise ValueError("Record content is empty or not a valid dict")
            if k not in latest_records or (seq is not None and seq > latest_records[k].ldb_seq_no):
                latest_records[k] = r
        except Exception as e:  # noqa: BLE001
            _note_failed_key(getattr(r, "key", None))
            seq_str = str(getattr(r, "ldb_seq_no", "unknown"))
            k_str = str(getattr(getattr(r, "key", None), "value", "unknown"))
            if failures is not None:
                failures.append(f"Could not parse record: store='{store_name}', key='{k_str}', seq={seq_str}: {e}")

    return latest_records


def _load_latest_records(
    leveldb_dir: Path,
    blob_dir: Path | None,
    db_marker: str,
    store_name: str,
    required: bool,
    failures: list[str] | None = None,
    failed_keys: set[Any] | None = None,
) -> dict[Any, Any]:
    """Read the given store from the already-copied IndexedDB snapshot and return {key: latest record} (the one with the largest ldb_seq_no wins).

    With required=False, a missing database or store returns an empty dict; otherwise SnapshotError is raised.
    """
    with WrappedIndexDB(leveldb_dir, blob_dir) as db:
        db_id = next((i for i in db.database_ids if f":{db_marker}:" in i.name), None)
        if not db_id:
            if required:
                raise SnapshotError(f"{db_marker} database not found in IndexedDB")
            return {}

        target_db = db[db_id]
        if store_name not in target_db:
            if required:
                raise SnapshotError(f"{store_name} store not found in the {db_marker} database")
            return {}

        store = target_db.get_object_store_by_name(store_name)
        return _load_latest_store_records(store, store_name, failures=failures, failed_keys=failed_keys)


def load_latest_conversation_records(
    leveldb_dir: Path,
    blob_dir: Path | None,
    failures: list[str] | None = None,
) -> dict[Any, Any]:
    """Read the snapshot's conversations store; raises SnapshotError if the database or store is missing."""
    return _load_latest_records(
        leveldb_dir, blob_dir, "conversation-manager", "conversations", required=True, failures=failures
    )


def load_latest_replychain_records(
    leveldb_dir: Path,
    blob_dir: Path | None,
    failures: list[str] | None = None,
    failed_keys: set[Any] | None = None,
) -> dict[Any, Any]:
    """Read the snapshot's replychains store; raises SnapshotError if the database or store is missing."""
    return _load_latest_records(
        leveldb_dir, blob_dir, "replychain-manager", "replychains", required=True,
        failures=failures, failed_keys=failed_keys,
    )


def _copy_cache_snapshot(profile_dir: Path, cache_dir: str) -> tuple[Path, Path | None]:
    leveldb_src = profile_dir / "IndexedDB" / "https_teams.live.com_0.indexeddb.leveldb"
    blob_src = profile_dir / "IndexedDB" / "https_teams.live.com_0.indexeddb.blob"
    if not leveldb_src.exists():
        raise SnapshotError(f"Teams IndexedDB LevelDB not found: {leveldb_src}")
    leveldb, blob = Path(cache_dir) / "leveldb", Path(cache_dir) / "blob"
    try:
        shutil.copytree(leveldb_src, leveldb)
        if blob_src.exists():
            shutil.copytree(blob_src, blob)
    except Exception as e:
        raise SnapshotError(f"Failed to copy the Teams snapshot: {e}") from e
    return leveldb, blob if blob.exists() else None


def _normalize_replychains(
    records: dict[Any, Any], failures: list[str], *, report_degraded: bool = True
) -> list[dict[str, Any]]:
    self_id = resolve_self_id()
    messages = []
    for k, r in records.items():
        seq = getattr(r, "ldb_seq_no", "unknown")
        val = r.value
        conv_id = k[0] if isinstance(k, tuple) else str(val.get("conversationId") or "")
        msg_map = val.get("messageMap")
        if msg_map is None:
            continue
        if not isinstance(msg_map, dict):
            failures.append(f"Could not parse record: store='replychains', key='{conv_id}', seq={seq}: messageMap is not a dict")
            continue
        for mid, m in msg_map.items():
            try:
                if not isinstance(m, dict):
                    raise TypeError("Message record is not a dict")
                degraded: list[str] = []
                messages.append(normalize_teams_message(m, conv_id, float(seq), degraded, self_id=self_id))
                for fld in degraded if report_degraded else ():
                    failures.append(
                        f"Record field could not be parsed (rest of the content kept): store='replychains', "
                        f"key='{conv_id}', id='{mid}', seq={seq}: field {fld}"
                    )
            except Exception as e:  # noqa: BLE001
                failures.append(
                    f"Could not parse record: store='replychains', key='{conv_id}', id='{mid}', seq={seq}: {type(e).__name__}"
                )
    return messages


def read_cache_messages(profile_dir: Path, failures: list[str]) -> list[dict[str, Any]]:
    """Read a fresh snapshot without ingesting or changing archive state.

    Records that cannot be parsed are appended to `failures`. A failed record may be a newer
    version of a key whose older version still loads, so every conversation with a failed record is
    dropped from the result entirely: its messages read as absent, never as stale text. A failure
    whose key cannot be identified makes the whole read untrustworthy and raises SnapshotError.
    """
    failed_keys: set[Any] = set()
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as temp_dir:
        leveldb, blob = _copy_cache_snapshot(profile_dir, temp_dir)
        records = load_latest_replychain_records(leveldb, blob, failures, failed_keys)
        messages = _normalize_replychains(records, failures, report_degraded=False)
    if UNKNOWN_KEY in failed_keys:
        raise SnapshotError("The cache has corrupt records of unidentifiable origin, so it cannot be determined which messages are trustworthy: " + "; ".join(failures))
    tainted = {k[0] if isinstance(k, tuple) else k for k in failed_keys}
    return [m for m in messages if m.get("conversation_id") not in tainted]


def ingest_cache(
    archive: Archive,
    profile_dir: Path,
) -> IngestOutcome:
    """Copy the WV2Profile_tfl\\IndexedDB snapshot to a temp area, parse and import it into the archive, and return an IngestOutcome."""
    failures: list[str] = []
    cache_dir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    try:
        try:
            temp_leveldb, temp_blob = _copy_cache_snapshot(profile_dir, cache_dir)
        except SnapshotError as e:
            err = str(e)
            return IngestOutcome(success=False, failures=[err], error=err)

        # 1. Import conversations
        try:
            latest_conv_records = load_latest_conversation_records(
                temp_leveldb, temp_blob, failures=failures
            )
        except Exception as e:  # noqa: BLE001
            err = f"Failed to parse the Teams snapshot: {e}"
            return IngestOutcome(success=False, failures=[err], error=err)

        normalised_conversations = []
        observed_ms = time.time() * 1000.0
        for k, r in latest_conv_records.items():
            seq = getattr(r, "ldb_seq_no", "unknown")
            try:
                c = r.value
                cid = str(c.get("id") or k)
                name = derive_conversation_name(c, cid)
                is_grp = derive_is_group(c)
                unread = derive_unread(c)
                last_msg_time = c.get("lastMessageTimeUtc")
                if isinstance(last_msg_time, (int, float)) and last_msg_time > 0:
                    last_time = _iso_from_ms(float(last_msg_time))
                    last_ts = float(last_msg_time) / 1000.0
                else:
                    last_time = ""
                    last_ts = 0

                normalised_conversations.append({
                    "id": cid,
                    "name": name,
                    "is_group": is_grp,
                    "unread": unread,
                    "last_time": last_time,
                    "last_timestamp": last_ts,
                    # Cached conversation records have no millisecond version comparable across sources: use the observation time as the version so every cache refresh is the latest
                    "version": observed_ms,
                    "member_mris": derive_member_mris(c),
                })
            except Exception as e:  # noqa: BLE001
                failures.append(f"Could not parse record: store='conversations', key='{k}', seq={seq}: {e}")

        archive.ingest_conversations(
            channel="teams",
            conversations=normalised_conversations,
            source="cache",
        )

        # 2. Import messages: missing or unreadable replychains = refresh failure (already imported conversations are kept)
        try:
            latest_replychains = load_latest_replychain_records(
                temp_leveldb, temp_blob, failures=failures
            )
        except Exception as e:  # noqa: BLE001
            err = f"Failed to parse the Teams replychains snapshot: {e}"
            failures.append(err)
            return IngestOutcome(success=False, failures=failures, error=err)

        all_norm_messages = _normalize_replychains(latest_replychains, failures)

        if all_norm_messages:
            archive.ingest_messages(
                channel="teams",
                messages=all_norm_messages,
                source="cache",
                failures=failures,
            )

        archive.record_sync(channel="teams", source="cache", status="ok")
        return IngestOutcome(success=True, failures=failures)

    except Exception as e:  # noqa: BLE001
        err = f"Failed to parse the Teams snapshot: {e}"
        failures.append(err)
        return IngestOutcome(success=False, failures=failures, error=err)
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)
        if os.path.exists(cache_dir):
            failures.append(f"Temporary snapshot could not be deleted: {cache_dir}")
