"""Teams reconciliation: raw records of the official export vs the cached versions in the archive.

Read-only: reads only the archive's cache_messages (what the cache itself shows), not the merged messages,
and never writes to the archive (the CLI opens it with a read-only connection). The export is processed per conversation with ijson streaming, never fully loaded.
Output contains only locating information such as ids and times; it never prints message text, sender names or conversation names.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ijson

from readers.archive import Archive
from readers.teams.export import (
    LEGACY_ONE_TO_ONE_PREFIX,
    build_one_to_one_candidates,
    open_export_stream,
    prepare_stream,
    resolve_one_to_one,
)
from readers.teams.identity import resolve_self_id
from readers.teams.normalizer import _iso_from_ms, normalize_teams_export_message

CHANNEL = "teams"
TIME_TOLERANCE_SECONDS = 0.0005

# U+FFFD defect in cached text (#22): a difference against the export's U+00A0 counts as "U+FFFD-only difference"; it still counts as different and is only tallied separately
_FFFD = chr(0xFFFD)
_NBSP = chr(0xA0)
_FFFD_OR_NBSP = re.compile(f"[{_FFFD}{_NBSP}]+")


def _is_fffd_only_difference(cache_text: str, export_text: str) -> bool:
    if _FFFD not in cache_text:
        return False
    return _FFFD_OR_NBSP.sub("\0", cache_text) == _FFFD_OR_NBSP.sub("\0", export_text)


def _sender_key(value: str | None) -> str:
    """Comparison key for sender identity: trimmed and lowercased; for URL forms take the last segment (`.../contacts/8:xxx`)."""
    v = (value or "").strip().lower()
    return v.rsplit("/", 1)[-1] if "/" in v else v


def _iso(ts: float) -> str:
    return _iso_from_ms(ts * 1000)


class _CacheOverlapIndex:
    """Cache only: {message id -> cached non-group conversation id} (None when the same id appears in several conversations), built on first use."""

    def __init__(self, archive: Archive):
        self._archive = archive
        self._index: dict[str, str | None] | None = None

    def count(self, message_ids: list[str], export_id: str) -> dict[str, int]:
        if self._index is None:
            index: dict[str, str | None] = {}
            for mid, cid in self._archive.list_cache_one_to_one_message_ids(CHANNEL):
                index[mid] = cid if index.get(mid, cid) == cid else None
            self._index = index
        counts: dict[str, int] = {}
        for mid in message_ids:
            cid = self._index.get(mid)
            if cid is not None and cid != export_id:
                counts[cid] = counts.get(cid, 0) + 1
        return counts


def _resolve_target(
    archive: Archive,
    export_id: str,
    raw_msgs: list[Any],
    candidates_by_member: dict[str, list[str]],
    overlap_index: _CacheOverlapIndex,
    failures: list[str],
) -> tuple[str, bool]:
    """Return (the conversation id to compare against the cache, whether it is a `8:` 1:1 that was mapped successfully).

    Same rules as import-export: registered aliases first, then member matching (several candidates are decided by shared message ids),
    and shared message ids only as the last fallback; shared message ids look at the cache (cache_messages) only.
    """
    if not export_id.startswith(LEGACY_ONE_TO_ONE_PREFIX):
        return export_id, False
    alias_target = archive.get_alias_target(CHANNEL, export_id)
    if alias_target:
        return alias_target, True
    candidates = candidates_by_member.get(export_id.lower(), [])
    merged = resolve_one_to_one(archive, export_id, candidates, raw_msgs, failures, overlap_index.count)
    return (merged, True) if merged else (export_id, False)


def _locator(conversation_id: str, mid: str, time: str, export_conversation_id: str | None = None) -> dict[str, Any]:
    loc: dict[str, Any] = {"conversation_id": conversation_id, "id": mid, "time": time}
    if export_conversation_id and export_conversation_id != conversation_id:
        loc["export_conversation_id"] = export_conversation_id
    return loc


def _diff_fields(e: dict[str, Any], c: dict[str, Any]) -> tuple[list[str], bool, bool]:
    """Compare one exported message with its cached version. Returns (differing fields, whether the text difference is only U+FFFD, whether the sender is comparable)."""
    fields: list[str] = []
    if abs(e["timestamp"] - c["timestamp"]) > TIME_TOLERANCE_SECONDS:
        fields.append("time")
    e_sender, c_sender = _sender_key(e["sender_id"]), _sender_key(c["sender_id"])
    sender_comparable = bool(e_sender and c_sender)
    if sender_comparable and e_sender != c_sender:
        fields.append("sender")
    text_differs = e["text"] != c["text"]
    if text_differs:
        fields.append("text")
    if bool(e["edited"]) != c["edited"]:
        fields.append("edited")
    if bool(e["deleted"]) != c["deleted"]:
        fields.append("deleted")
    return fields, text_differs and _is_fffd_only_difference(c["text"], e["text"]), sender_comparable


_FIELD_ORDER = ("time", "sender", "text", "edited", "deleted")


@dataclass
class _Retained:
    """One exported message whose time falls within the cache range (it may have several copies; differences are unioned)."""

    timestamp: float
    time: str
    export_id: str
    fields: list[str] = field(default_factory=list)
    text_fffd_only: bool = True  # every copy with a text difference differs only by U+FFFD

    def merge_copy(self, fields: list[str], text_fffd_only: bool) -> None:
        self.fields = [f for f in _FIELD_ORDER if f in self.fields or f in fields]
        if "text" in fields:
            self.text_fffd_only = self.text_fffd_only and text_fffd_only


@dataclass
class _Target:
    """Reconciliation state of one cached conversation. Several exported conversations (e.g. the `19:` and `8:` id kinds) can map to the same one."""

    cache_rows: dict[str, dict[str, Any]]
    cache_min: float
    cache_max: float
    export_ids: set[str] = field(default_factory=set)
    export_min: float = float("inf")
    export_max: float = float("-inf")
    retained: dict[str, _Retained] = field(default_factory=dict)
    outside_cache_range: int = 0


@dataclass
class _Report:
    """Reconciliation result accumulator: three locator lists, counts, and the overlap window of each conversation."""

    limit: int
    in_export_not_cache: list[dict[str, Any]] = field(default_factory=list)
    in_cache_not_export: list[dict[str, Any]] = field(default_factory=list)
    in_both_different: list[dict[str, Any]] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    diff_by_field: Counter[str] = field(default_factory=Counter)
    windows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def unlimited(self) -> bool:
        return self.limit <= 0

    def add(self, bucket: list[dict[str, Any]], loc: dict[str, Any]) -> None:
        if self.unlimited or len(bucket) < self.limit:
            bucket.append(loc)

    def category(self, name: str, bucket: list[dict[str, Any]]) -> dict[str, Any]:
        total = self.counts[name]
        return {"count": total, "locators": bucket, "locators_truncated": len(bucket) < total}


def _compare_target(target_id: str, st: _Target, rep: _Report) -> None:
    """Classify a cached conversation using the overlap window of both sides (messages within the window only), and record that conversation's window."""
    rep.counts["export_messages_outside_window"] += st.outside_cache_range
    has_both = st.export_min <= st.export_max and st.cache_min <= st.cache_max
    lo = max(st.export_min, st.cache_min) if has_both else float("inf")
    hi = min(st.export_max, st.cache_max) if has_both else float("-inf")
    if lo > hi:
        rep.windows.append({"conversation_id": target_id, "start": None, "end": None, "status": "no_overlap"})
        rep.counts["conversations_no_overlap"] += 1
        rep.counts["export_messages_outside_window"] += len(st.retained)
        rep.counts["cache_messages_outside_window"] += sum(1 for r in st.cache_rows.values() if r["timestamp"] > 0)
        return
    rep.windows.append({"conversation_id": target_id, "start": _iso(lo), "end": _iso(hi), "status": "compared"})
    rep.counts["conversations_with_overlap"] += 1

    for mid, r in st.retained.items():
        if not (lo <= r.timestamp <= hi):
            rep.counts["export_messages_outside_window"] += 1
        elif mid not in st.cache_rows:
            rep.counts["in_export_not_cache"] += 1
            rep.add(rep.in_export_not_cache, _locator(target_id, mid, r.time, r.export_id))
        elif not r.fields:
            rep.counts["in_both_same"] += 1
        else:
            rep.counts["in_both_different"] += 1
            for f in r.fields:
                rep.diff_by_field[f] += 1
            loc = _locator(target_id, mid, r.time, r.export_id)
            loc["fields"] = list(r.fields)
            if r.fields == ["text"] and r.text_fffd_only:
                rep.counts["in_both_different_fffd_only"] += 1
                loc["fffd_only"] = True
            rep.add(rep.in_both_different, loc)

    for mid, c in st.cache_rows.items():
        cts = c["timestamp"]
        if cts <= 0:
            continue
        if not (lo <= cts <= hi):
            rep.counts["cache_messages_outside_window"] += 1
        elif mid not in st.export_ids:
            rep.counts["in_cache_not_export"] += 1
            rep.add(rep.in_cache_not_export, _locator(target_id, mid, c["time"]))


def _ingest_export_message(
    e: dict[str, Any], export_id: str, st: _Target, rep: _Report
) -> None:
    """Merge one normalized exported message into the target conversation state (every copy is compared with the cache)."""
    mid = e["id"]
    ets = e["timestamp"]
    c = st.cache_rows.get(mid)
    duplicate = mid in st.export_ids
    if duplicate:
        rep.counts["export_duplicate_messages"] += 1  # the same message appears in several exported conversations mapped to this one
        kept = st.retained.get(mid)
        if kept is not None and c is not None:
            fields, text_fffd, _ = _diff_fields(e, c)
            kept.merge_copy(fields, text_fffd)
        return
    st.export_ids.add(mid)
    if ets <= 0:
        rep.counts["messages_without_time"] += 1
        return
    st.export_min = min(st.export_min, ets)
    st.export_max = max(st.export_max, ets)
    if not (st.cache_min <= ets <= st.cache_max):
        st.outside_cache_range += 1
        return
    kept = _Retained(timestamp=ets, time=e["time"], export_id=export_id)
    if c is not None:
        fields, text_fffd, sender_comparable = _diff_fields(e, c)
        if not sender_comparable:
            rep.counts["sender_not_comparable"] += 1  # one side has no sender identity: cannot compare, not counted as a difference
        kept.merge_copy(fields, text_fffd)
    st.retained[mid] = kept


def reconcile_export(
    archive: Archive,
    export_path: str | Path,
    failures: list[str] | None = None,
    locator_limit: int | None = None,
) -> dict[str, Any]:
    """Reconciliation report: export vs the archive's cached versions, limited to the overlap window where both sides have data (decided per conversation).

    locator_limit: maximum number of locators printed per kind; None or <=0 means all (default). Counts are always complete.
    """
    if failures is None:
        failures = []
    path = Path(export_path)
    rep = _Report(limit=locator_limit or 0)
    targets: dict[str, _Target] = {}
    overlap_index = _CacheOverlapIndex(archive)

    with open_export_stream(path) as raw_stream:
        user_id, stream = prepare_stream(raw_stream)
        user_id = resolve_self_id(user_id)
        if not user_id:
            failures.append("Export is missing userId: cannot determine is_self; 1:1 conversations can only be mapped by shared message id")
        candidates_by_member = build_one_to_one_candidates(archive, user_id)

        for conv_dict in ijson.items(stream, "conversations.item"):
            rep.counts["conversations_in_export"] += 1
            export_id = "unknown"
            try:
                export_id = str(conv_dict.get("id") or "")
                raw_msgs = conv_dict.pop("MessageList", None) or []
                if not export_id:
                    failures.append(
                        f"Could not compare export conversation: id=<missing>, position={rep.counts['conversations_in_export'] - 1}, "
                        f"skipped {len(raw_msgs)} messages"
                    )
                    continue
                target_id, mapped = _resolve_target(
                    archive, export_id, raw_msgs, candidates_by_member, overlap_index, failures
                )
                if mapped:
                    rep.counts["one_to_one_mapped"] += 1
                elif export_id.startswith(LEGACY_ONE_TO_ONE_PREFIX):
                    rep.counts["one_to_one_unmapped"] += 1

                state = targets.get(target_id)
                if state is None:
                    cache_rows = archive.get_cache_messages_for_reconcile(CHANNEL, target_id)
                    if not cache_rows:
                        rep.counts["conversations_export_only"] += 1
                        rep.counts["export_messages_in_conversations_without_cache"] += len(raw_msgs)
                        continue
                    cache_times = [r["timestamp"] for r in cache_rows.values() if r["timestamp"] > 0]
                    rep.counts["messages_without_time"] += len(cache_rows) - len(cache_times)
                    state = _Target(
                        cache_rows=cache_rows,
                        cache_min=min(cache_times) if cache_times else float("inf"),
                        cache_max=max(cache_times) if cache_times else float("-inf"),
                    )
                    targets[target_id] = state

                for m in raw_msgs:
                    try:
                        msg_degraded: list[str] = []
                        e = normalize_teams_export_message(m, target_id, user_id=user_id, degraded=msg_degraded)
                        for fld in msg_degraded:
                            failures.append(
                                f"Export message field could not be parsed (rest of the content kept): conv='{target_id}', "
                                f"id='{m.get('id', 'unknown')}': field {fld}"
                            )
                    except Exception as ex:  # noqa: BLE001
                        mid = m.get("id", "unknown") if isinstance(m, dict) else "unknown"
                        failures.append(f"Could not parse export message: conv='{target_id}', id='{mid}': {type(ex).__name__}")
                        continue
                    _ingest_export_message(e, export_id, state, rep)
            except Exception as e:  # noqa: BLE001
                failures.append(f"Could not compare export conversation: id='{export_id}': {type(e).__name__}")

    for target_id, st in targets.items():
        _compare_target(target_id, st, rep)

    cache_only = [cid for cid in archive.list_cache_conversation_ids(CHANNEL) if cid not in targets]
    ok_windows = [w for w in rep.windows if w["status"] == "compared"]
    if ok_windows:
        window: dict[str, Any] = {
            "start": min(w["start"] for w in ok_windows),
            "end": max(w["end"] for w in ok_windows),
        }
        statement = (
            f"The overlap window is decided per conversation (the time range where both sides have data; see conversation_windows); "
            f"{rep.counts['conversations_with_overlap']} cached conversations overlap, "
            f"overall covering {window['start']} to {window['end']}. The three kinds of difference count only messages inside each conversation's overlap window."
        )
    else:
        window = {"start": None, "end": None}
        statement = "The export and the cache have no overlapping period, so reconciliation is impossible; this does not mean the two sides agree."

    return {
        "overlap": {
            "scope": "per_conversation",
            "window": window,
            "statement": statement,
            "conversation_windows": rep.windows,
            "conversations_in_export": rep.counts["conversations_in_export"],
            "cache_conversations_compared": len(targets),
            "conversations_with_overlap": rep.counts["conversations_with_overlap"],
            "conversations_no_overlap": rep.counts["conversations_no_overlap"],
            "conversations_export_only": rep.counts["conversations_export_only"],
            "conversations_cache_only": len(cache_only),
        },
        "one_to_one_mapped": rep.counts["one_to_one_mapped"],
        "one_to_one_unmapped": rep.counts["one_to_one_unmapped"],
        "counts": {
            "in_export_not_cache": rep.counts["in_export_not_cache"],
            "in_cache_not_export": rep.counts["in_cache_not_export"],
            "in_both_different": rep.counts["in_both_different"],
            "in_both_different_fffd_only": rep.counts["in_both_different_fffd_only"],
            "in_both_same": rep.counts["in_both_same"],
            "different_by_field": dict(sorted(rep.diff_by_field.items())),
            "sender_not_comparable": rep.counts["sender_not_comparable"],
            "export_duplicate_messages": rep.counts["export_duplicate_messages"],
            "export_messages_outside_window": rep.counts["export_messages_outside_window"],
            "cache_messages_outside_window": rep.counts["cache_messages_outside_window"],
            "export_messages_in_conversations_without_cache": rep.counts["export_messages_in_conversations_without_cache"],
            "messages_without_time": rep.counts["messages_without_time"],
        },
        "in_export_not_cache": rep.category("in_export_not_cache", rep.in_export_not_cache),
        "in_cache_not_export": rep.category("in_cache_not_export", rep.in_cache_not_export),
        "in_both_different": rep.category("in_both_different", rep.in_both_different),
        "failures": failures,
    }
