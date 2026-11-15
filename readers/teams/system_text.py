"""One-shot migration of archived system-event descriptions from the old Chinese text to the English text (#39, #43)."""

import re
from typing import Any

from readers.archive import Archive

# old exact text -> (label, new text). The group-name event with a value is a pattern, handled separately.
# The old texts are Chinese archive data; they are written as \u escapes so the source stays ASCII.
_BY_OLD = {
    "\u8b8a\u66f4\u7fa4\u7d44\u540d\u7a31": ("group_name_unset", "Changed the group name"),
    "\u65b0\u589e\u6210\u54e1": ("member_added", "Added a member"),
    "\u6210\u54e1\u96e2\u958b": ("member_left", "A member left"),
    "\u66f4\u65b0\u7fa4\u7d44\u52a0\u5165\u6b0a\u9650": ("join_permission", "Updated the group join permission"),
    "\u8a9e\u97f3/\u8996\u8a0a\u901a\u8a71": ("call", "Voice/video call"),
    "\u7cfb\u7d71\u4e8b\u4ef6": ("generic", "System event"),
}
_GROUP_NAME = re.compile("\u8b8a\u66f4\u7fa4\u7d44\u540d\u7a31\u70ba\u300c(.*)\u300d", re.DOTALL)


def _translate(text: str) -> tuple[str, str] | None:
    """Return (label, new text) when the text is one of the old system-event descriptions."""
    if text in _BY_OLD:
        return _BY_OLD[text]
    match = _GROUP_NAME.fullmatch(text)
    if match:
        return "group_name", f'Changed the group name to "{match.group(1)}"'
    return None


def migrate_system_text(archive: Archive, write: bool = False) -> dict[str, Any]:
    """Rewrite old Chinese system-event texts (type 'system' only) in messages and their cache mirror.

    Previews by default. Version and every other column stay unchanged; the archive has no derived text index.
    """
    counts = {"group_name": 0, **{label: 0 for label, _ in _BY_OLD.values()}}
    changes = []
    for row in archive.system_messages("teams"):
        translated = _translate(row["text"] or "")
        if translated is None:
            continue
        label, new = translated
        counts[label] += 1
        changes.append((row["conversation_id"], row["id"], row["text"], new))
    result: dict[str, Any] = {
        "candidates": len(changes),
        "by_mapping": counts,
        "message_ids": [{"conversation_id": cid, "id": mid} for cid, mid, _, _ in changes],
    }
    if write:
        # The cache mirror may be absent or already carry newer text; it is rewritten only when it holds the old text.
        result["updated"] = archive.repair_texts(
            "teams", changes, cache_only=False, require_mirror=False, message_type="system"
        )
    return result
