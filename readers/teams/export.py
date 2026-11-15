"""Streaming import module for official Teams export files (.tar or messages.json).

Parses with ijson streaming so that a JSON file of hundreds of MB is never loaded into memory in full.
Reads .tar member streams directly, without extracting to disk.
"""

from __future__ import annotations

import re
import tarfile
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import ijson

from readers.archive import Archive
from readers.teams.identity import resolve_self_id
from readers.teams.normalizer import (
    normalize_teams_export_conversation,
    normalize_teams_export_message,
)

CHUNK_SIZE = 1000
HEAD_BYTES = 64 * 1024
LEGACY_ONE_TO_ONE_PREFIX = "8:"
UNIFIED_ONE_TO_ONE_PREFIX = "19:uni01_"

_USER_ID_RE = re.compile(rb'"userId"\s*:\s*"([^"]+)"')


class _PrefixedStream:
    """Splice the already-read leading bytes back onto a non-seekable stream so ijson can read from the start."""

    def __init__(self, head: bytes, rest: BinaryIO):
        self._head = head
        self._pos = 0
        self._rest = rest

    def read(self, size: int = -1) -> bytes:
        if self._pos < len(self._head):
            if size is None or size < 0:
                chunk = self._head[self._pos :]
                self._pos = len(self._head)
                return chunk + self._rest.read()
            chunk = self._head[self._pos : self._pos + size]
            self._pos += len(chunk)
            if len(chunk) < size:
                chunk += self._rest.read(size - len(chunk))
            return chunk
        return self._rest.read(size)


def prepare_stream(stream: BinaryIO) -> tuple[str | None, Any]:
    """Read the export's userId and return a stream ijson can parse from the start.

    Seekable streams (.json files, tar members) are simply rewound; for non-seekable streams the leading chunk is spliced back.
    If userId is not in the leading chunk, a seekable stream falls back to using ijson to find the first top-level userId.
    """
    seekable = stream.seekable()
    start = stream.tell() if seekable else 0
    head = stream.read(HEAD_BYTES)
    match = _USER_ID_RE.search(head)
    # The export itself is UTF-8 JSON (not a V8 one-byte string, so the normalizer's _v8_str is not used)
    user_id = match.group(1).decode("utf-8", errors="replace") if match else None

    if not seekable:
        return user_id, _PrefixedStream(head, stream)

    if user_id is None:
        stream.seek(start)
        user_id = next(iter(ijson.items(stream, "userId")), None)
        user_id = str(user_id) if user_id else None
    stream.seek(start)
    return user_id, stream


@contextmanager
def open_export_stream(export_path: Path) -> Iterator[BinaryIO]:
    """Context manager: open the export file stream. Accepts only .tar or .json files."""
    if not export_path.exists():
        raise FileNotFoundError(f"Export path not found: '{export_path}'")
    if not export_path.is_file():
        raise ValueError(f"The export path must be a .tar or .json file; directories are not accepted: '{export_path}'")

    suffix = export_path.suffix.lower()
    if suffix == ".tar":
        with tarfile.open(export_path, "r") as tar:
            member = next(
                (m for m in tar if m.isfile() and m.name.endswith("messages.json")),
                None,
            )
            if member is None:
                raise FileNotFoundError(f"messages.json not found in the tar archive: '{export_path}'")
            stream = tar.extractfile(member)
            if stream is None:
                raise RuntimeError(f"Cannot read messages.json in the tar: '{export_path}'")
            yield stream
        return

    if suffix == ".json":
        with open(export_path, "rb") as f:
            yield f
        return

    raise ValueError(f"The export path must be a .tar or .json file: '{export_path}'")


def build_one_to_one_candidates(archive: Archive, user_id: str | None) -> dict[str, list[str]]:
    """Build {counterpart mri (lowercase) -> list of candidate cache conversation ids} from the cache members already recorded in the archive.

    A conversation is a 1:1 candidate for a counterpart only when its members are exactly {self, counterpart}. Conversations with an incomplete member list (missing self) are not listed:
    those can only be matched by shared message id (pick_by_message_overlap), because a wrong merge is hard to undo,
    whereas an unmerged copy can be merged later by a late match.
    """
    if not user_id:
        return {}
    self_mri = user_id.lower()
    candidates: dict[str, list[str]] = {}
    for cid, members in archive.list_conversation_members("teams").items():
        lowered = {x.lower() for x in members}
        others = lowered - {self_mri}
        if self_mri not in lowered or len(others) != 1:
            continue
        candidates.setdefault(next(iter(others)), []).append(cid)
    return candidates


OverlapCounter = Callable[[list[str], str], dict[str, int]]


def _count_overlaps(
    archive: Archive,
    export_id: str,
    message_ids: list[str],
    count_overlaps: OverlapCounter | None,
) -> dict[str, int]:
    """Count shared message ids: when no counter is given, read the merged messages."""
    if count_overlaps is None:
        return archive.count_cache_conversation_overlaps("teams", message_ids, exclude_conversation_id=export_id)
    return count_overlaps(message_ids, export_id)


def pick_target(
    archive: Archive,
    export_id: str,
    candidates: list[str],
    message_ids: list[str],
    failures: list[str],
    count_overlaps: OverlapCounter | None = None,
) -> str | None:
    """Pick the merge target. A single candidate is used directly; with several candidates, take the one sharing message ids with the exported conversation (exactly one is required).

    Matches against all message ids of that exported conversation (streamed per conversation, all ids already in hand), favouring correctness.
    count_overlaps defaults to reading the merged messages; reconcile passes a counter that reads only the cached versions.
    """
    if len(candidates) == 1:
        return candidates[0]
    overlaps = _count_overlaps(archive, export_id, message_ids, count_overlaps)
    with_overlap = [cid for cid in candidates if overlaps.get(cid, 0) > 0]
    if len(with_overlap) == 1:
        return with_overlap[0]
    failures.append(
        f"The 1:1 conversation mapping is not unique; not merging: export_id='{export_id}', candidates={len(candidates)}, candidates with shared messages={len(with_overlap)}"
    )
    return None


def pick_by_message_overlap(
    archive: Archive,
    export_id: str,
    message_ids: list[str],
    count_overlaps: OverlapCounter | None = None,
) -> str | None:
    """Fallback when member matching finds no candidate: merge only when exactly one cached non-group conversation shares message ids with the exported conversation."""
    overlaps = _count_overlaps(archive, export_id, message_ids, count_overlaps)
    hits = [cid for cid, n in overlaps.items() if n > 0]
    return hits[0] if len(hits) == 1 else None


def resolve_one_to_one(
    archive: Archive,
    export_id: str,
    candidates: list[str],
    raw_msgs: list[Any],
    failures: list[str],
    count_overlaps: OverlapCounter | None = None,
) -> str | None:
    """Merge target for a `8:` 1:1 exported conversation: use pick_target when member candidates exist, otherwise fall back to shared message ids."""
    message_ids = [str(m["id"]) for m in raw_msgs if isinstance(m, dict) and m.get("id") is not None]
    if candidates:
        return pick_target(archive, export_id, candidates, message_ids, failures, count_overlaps)
    if message_ids:
        return pick_by_message_overlap(archive, export_id, message_ids, count_overlaps)
    return None


def import_export(
    archive: Archive,
    export_path: str | Path,
    failures: list[str] | None = None,
) -> dict[str, Any]:
    """Stream-import an official Teams export file into the archive.

    Follows the principle of not loading everything into memory: conversations are parsed one at a time and written in batches.
    A `8:<user>` 1:1 conversation in the export is replaced with the cached conversation id in the archive whose members are {self, `8:<user>`};
    the old id and name are kept as aliases; conversations that cannot be mapped keep the `8:` id and are counted.
    """
    path = Path(export_path)
    if failures is None:
        failures = []

    convs_before = len(archive.query_conversations("teams"))
    msgs_before = archive.count_messages("teams")

    stats: Counter[str] = Counter(
        {
            "conversations_new": 0,
            "conversations_overlapping": 0,
            "messages_new": 0,
            "messages_overlapping": 0,
            "unorderable_conflicts": 0,
        }
    )
    mapped_convs = 0
    unmapped_convs = 0
    msgs_in_mapped = 0
    msgs_moved = 0
    one_to_one_sender_unknown = 0
    seen_this_import: set[tuple[str, str]] = set()

    def flush(chunk: list[dict[str, Any]]) -> None:
        archive.ingest_messages(
            channel="teams",
            messages=chunk,
            source="export",
            failures=failures,
            stats=stats,
            seen_this_import=seen_this_import,
        )

    with open_export_stream(path) as raw_stream:
        user_id, stream = prepare_stream(raw_stream)
        user_id = resolve_self_id(user_id)
        if not user_id:
            failures.append("Export is missing userId: cannot determine is_self; 1:1 conversations can only be merged by shared message id")
        one_to_one_candidates = build_one_to_one_candidates(archive, user_id)

        for conv_dict in ijson.items(stream, "conversations.item"):
            try:
                conv_degraded: list[str] = []
                norm_conv = normalize_teams_export_conversation(conv_dict, conv_degraded)
                export_id = norm_conv["id"]
                for fld in conv_degraded:
                    failures.append(f"Export conversation field could not be parsed (rest of the content kept): id='{export_id}': field {fld}")
                is_legacy_one_to_one = export_id.startswith(LEGACY_ONE_TO_ONE_PREFIX)
                target_id = export_id
                raw_msgs = conv_dict.pop("MessageList", None) or []
                if is_legacy_one_to_one:
                    # Conversations with a registered alias reuse the existing mapping; no re-evaluation
                    target_id = archive.get_alias_target("teams", export_id) or ""
                    if not target_id:
                        candidates = one_to_one_candidates.get(export_id.lower(), [])
                        target_id = (
                            resolve_one_to_one(archive, export_id, candidates, raw_msgs, failures) or export_id
                        )
                    if target_id != export_id:
                        mapped_convs += 1
                    else:
                        unmapped_convs += 1

                norm_conv["id"] = target_id
                archive.ingest_conversations("teams", [norm_conv], source="export", stats=stats)
                if target_id != export_id:
                    # Merge messages previously stored under the `8:` id (late-arriving mapping) into the target to avoid duplicates
                    msgs_moved += archive.merge_conversation_into(
                        "teams", export_id, target_id, failures=failures, stats=stats
                    )
                    archive.add_conversation_alias(
                        "teams", export_id, target_id, alias_name=norm_conv["name"]
                    )

                chunk: list[dict[str, Any]] = []
                is_one_to_one = is_legacy_one_to_one or export_id.startswith(UNIFIED_ONE_TO_ONE_PREFIX)
                for pos, m in enumerate(raw_msgs):
                    try:
                        msg_degraded: list[str] = []
                        normalized = normalize_teams_export_message(m, target_id, user_id=user_id, degraded=msg_degraded)
                        chunk.append(normalized)
                        if is_one_to_one and not normalized["sender_id"]:
                            one_to_one_sender_unknown += 1
                        for fld in msg_degraded:
                            failures.append(
                                f"Export message field could not be parsed (rest of the content kept): conv='{target_id}', position={pos}, "
                                f"id='{m.get('id', 'unknown')}': field {fld}"
                            )
                        if target_id != export_id:
                            msgs_in_mapped += 1
                        if len(chunk) >= CHUNK_SIZE:
                            flush(chunk)
                            chunk = []
                    except Exception as e:  # noqa: BLE001
                        mid = m.get("id", "unknown") if isinstance(m, dict) else "unknown"
                        failures.append(
                            f"Could not parse export message: conv='{target_id}', position={pos}, id='{mid}': {type(e).__name__}"
                        )
                if chunk:
                    flush(chunk)

            except Exception as e:  # noqa: BLE001
                cid = conv_dict.get("id", "unknown") if isinstance(conv_dict, dict) else "unknown"
                failures.append(f"Could not parse export conversation: id='{cid}': {e}")

    convs_after = len(archive.query_conversations("teams"))
    msgs_after = archive.count_messages("teams")

    archive.record_sync("teams", source="export", status="ok")

    return {
        "conversations_before": convs_before,
        "conversations_after": convs_after,
        "conversations_new": stats["conversations_new"],
        "messages_before": msgs_before,
        "messages_after": msgs_after,
        "messages_new": stats["messages_new"],
        "messages_overlapping": stats["messages_overlapping"],
        "unorderable_conflicts": stats["unorderable_conflicts"],
        "one_to_one_mapped": mapped_convs,
        "one_to_one_unmapped": unmapped_convs,
        "messages_in_mapped_conversations": msgs_in_mapped,
        "messages_moved_from_legacy_ids": msgs_moved,
        "one_to_one_sender_unknown": one_to_one_sender_unknown,
        "failures": failures,
    }
