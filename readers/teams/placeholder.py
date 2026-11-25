"""One-shot cleanup of archived Teams cache placeholder messages (id '-1')."""

from __future__ import annotations

from typing import Any

from readers.archive import Archive, _load_sources


def clean_placeholder_messages(archive: Archive, write: bool = False) -> dict[str, Any]:
    """Remove cache-only placeholder messages (id '-1') from messages and cache_messages.

    Previews by default. Non-cache-only rows are reported as skipped and left untouched.
    Output contains counts and conversation locators only (no message text).
    """
    candidates: list[str] = []
    skipped_cids: list[str] = []
    for row in archive.placeholder_candidates("teams"):
        if _load_sources(row["sources"]) == ["cache"]:
            candidates.append(row["conversation_id"])
        else:
            skipped_cids.append(row["conversation_id"])

    result: dict[str, Any] = {
        "candidates": len(candidates),
        "skipped": len(skipped_cids),
        "conversation_ids": candidates,
        "skipped_conversation_ids": skipped_cids,
    }
    if write:
        result["deleted"] = archive.delete_placeholder_messages("teams", candidates)
    return result
