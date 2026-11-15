"""One-shot repair of old replacement-character corruption."""

import re
from typing import Any

from readers.archive import REPLACEMENT_CHARACTER, Archive, _load_sources


def repair_fffd(archive: Archive, messages: list[dict[str, Any]], write: bool = False) -> dict[str, Any]:
    fresh: dict[tuple[str, str], str] = {}
    for message in messages:
        key = (message["conversation_id"], message["id"])
        if key in fresh and fresh[key] != message["text"]:
            raise RuntimeError("The cache contains duplicate messages with inconsistent content")
        fresh[key] = message["text"]
    candidates = archive.fffd_candidates("teams")
    skipped = {key: 0 for key in ("sources_not_cache_only", "not_in_cache", "still_fffd", "other_difference")}
    repairs = []
    samples = []
    for row in candidates:
        old = row["text"]
        key = (row["conversation_id"], row["id"])
        if _load_sources(row["sources"]) != ["cache"]:
            skipped["sources_not_cache_only"] += 1
        elif key not in fresh:
            skipped["not_in_cache"] += 1
        elif REPLACEMENT_CHARACTER in fresh[key]:
            skipped["still_fffd"] += 1
        else:
            new = fresh[key]
            chunks = re.split(rf"[\s{REPLACEMENT_CHARACTER}]*{REPLACEMENT_CHARACTER}[\s{REPLACEMENT_CHARACTER}]*", old)
            pattern = "(.*?)".join(re.escape(chunk) for chunk in chunks)
            match = re.fullmatch(pattern, new, flags=re.DOTALL)
            if match is None:
                skipped["other_difference"] += 1
                continue
            repairs.append((*key, old, new))
            # The unchanged prefix ends where the first garbled run starts, in both texts.
            start = max(0, len(chunks[0]) - 20)
            samples.append({"conversation_id": key[0], "id": key[1],
                            "before": old[start:len(chunks[0]) + 21],
                            "after": new[start:len(chunks[0]) + 40]})
    result = {"candidates": len(candidates), "repairable": len(repairs), "skipped": skipped, "samples": samples}
    if write:
        result["updated"] = archive.repair_texts("teams", repairs)
    return result
