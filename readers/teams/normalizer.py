"""Normalization module for Teams messages and conversation properties.

Normalizes raw HTML content, system events and conversation properties into the domain vocabulary of GLOSSARY.md.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import Any

from readers.teams.identity import resolve_self_id

_UNSET = object()


def _v8_str(val: Any) -> str:
    # V8 one-byte strings are Latin-1 code units; the upstream reader returns bytes.
    return val.decode("latin-1") if isinstance(val, bytes) else str(val)


def _clean_str(val: Any) -> str | None:
    if val is None:
        return None
    if hasattr(val, "__class__") and val.__class__.__name__ == "_Undefined":
        return None
    s = _v8_str(val).strip()
    return s if s else None


def _iso_from_ms(ms_val: float) -> str:
    return datetime.fromtimestamp(ms_val / 1000.0).astimezone().isoformat(timespec="seconds")


def resolve_version(v_raw: Any, ts: float, fallback: float = 0.0) -> float:
    """Get the own version (ms); if missing or unparsable fall back to the send time (ms), then to fallback."""
    if v_raw is not None:
        try:
            return float(v_raw)
        except (ValueError, TypeError):
            pass  # Silent: falls back to the send time; no data lost
    return float(ts * 1000.0) if ts > 0 else float(fallback or 0.0)


def derive_member_mris(conversation_dict: dict[str, Any]) -> list[str] | None:
    """Extract member mris from the cached conversation's members[].id (sorted, de-duplicated); recorded only for non-group conversations, otherwise returns None."""
    if derive_is_group(conversation_dict):
        return None
    members = conversation_dict.get("members")
    if not isinstance(members, list):
        return None
    mris = {
        mri
        for mem in members
        if isinstance(mem, dict)
        for mri in [_clean_str(mem.get("id"))]
        if mri
    }
    return sorted(mris) if mris else None


def derive_conversation_name(conversation_dict: dict[str, Any], conv_id: str) -> str:
    """Derive the display name from the conversation properties."""
    display_name = _clean_str(conversation_dict.get("displayName"))
    if display_name:
        return display_name

    thread_props = conversation_dict.get("threadProperties") or {}
    topic = _clean_str(thread_props.get("topic"))
    if topic:
        return topic

    chat_title = conversation_dict.get("chatTitle")
    if isinstance(chat_title, dict):
        short_title = _clean_str(chat_title.get("shortTitle"))
        if short_title:
            return short_title
        long_title = _clean_str(chat_title.get("longTitle"))
        if long_title:
            return long_title
        avatar_users = chat_title.get("avatarUsersInfo")
        if isinstance(avatar_users, list) and avatar_users:
            first_user = avatar_users[0]
            if isinstance(first_user, dict):
                display_name = _clean_str(first_user.get("displayName"))
                if display_name:
                    return display_name

    if conv_id == "48:notes" or conversation_dict.get("streamType") == "streamofnotes":
        return "Notes"

    stream_type = _clean_str(conversation_dict.get("streamType"))
    if stream_type:
        return stream_type.title()

    return conv_id


def derive_is_group(conversation_dict: dict[str, Any]) -> bool:
    """Decide whether this is a group conversation."""
    thread_props = conversation_dict.get("threadProperties") or {}
    template_type = _clean_str(thread_props.get("templateType"))
    if template_type:
        template_type_lower = template_type.lower()
        if template_type_lower in ("skypeonetoone", "immutableroster"):
            return False
        if template_type_lower == "skypegroup":
            return True

    if conversation_dict.get("type") == "Meeting":
        return True

    topic = _clean_str(thread_props.get("topic"))
    if topic:
        return True

    member_count = thread_props.get("membercount")
    if isinstance(member_count, (int, float)):
        if member_count > 2:
            return True
        if member_count <= 2 and not topic:
            return False

    members = conversation_dict.get("members") or thread_props.get("members")
    if isinstance(members, (str, bytes)):
        try:
            members = json.loads(_v8_str(members))
        except (json.JSONDecodeError, TypeError):
            pass  # Silent: falls back to the membercount / raw-value check; no data lost
    return isinstance(members, list) and len(members) > 2


def _parse_horizon_ts(val: Any) -> int:
    s = _clean_str(val)
    if not s:
        return 0
    parts = s.split(";")
    try:
        return int(parts[0])
    except (ValueError, IndexError):
        return 0  # Silent: an unparsable bookmark counts as unset and falls back to the other horizon


def derive_unread(conversation_dict: dict[str, Any]) -> bool:
    """Determine the unread state from the consumption-horizon bookmark and lastMessageTimeUtc."""
    last_msg_time = conversation_dict.get("lastMessageTimeUtc")
    if last_msg_time is None or not isinstance(last_msg_time, (int, float)) or last_msg_time <= 0:
        return False

    props = conversation_dict.get("properties") or {}
    bookmark_ts = _parse_horizon_ts(props.get("consumptionHorizonBookmark"))
    horizon_ts = _parse_horizon_ts(props.get("consumptionhorizon"))

    read_ts = bookmark_ts if bookmark_ts > 0 else horizon_ts
    return float(last_msg_time) > float(read_ts)


def _html_to_text(content: str) -> str:
    """Convert an HTML fragment to readable text (image placeholder, mentions, links kept as [text](url), line breaks, whitespace normalization)."""
    # 2. Image placeholder [image]
    content = re.sub(r"<img[^>]*>", " [image] ", content, flags=re.IGNORECASE)

    # 3. Mentions
    def _replace_mention(m: re.Match[str]) -> str:
        inner = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if not inner:
            return ""
        if not inner.startswith("@"):
            return f"@{inner}"
        return inner

    content = re.sub(
        r'<span[^>]*itemtype="http://schema\.skype\.com/Mention"[^>]*>(.*?)</span>',
        _replace_mention,
        content,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 4. Links ([text](href) or a bare href)
    def _replace_link(m: re.Match[str]) -> str:
        href = m.group(1).strip()
        link_text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not link_text or link_text == href:
            return href
        return f"[{link_text}]({href})"

    content = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', _replace_link, content, flags=re.DOTALL | re.IGNORECASE)

    # 5. Line breaks and paragraphs
    content = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"</p>\s*<p[^>]*>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"</?p[^>]*>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"</?div[^>]*>", "\n", content, flags=re.IGNORECASE)

    # 6. Strip remaining tags and decode entities
    content = re.sub(r"<[^>]+>", "", content)
    content = html.unescape(content)

    # 7. Whitespace normalization
    content = re.sub(r"\r\n", "\n", content)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in content.split("\n")]
    content = "\n".join(lines)
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    return content


def normalize_html_content(
    content_raw: str | bytes | None,
    degraded: list[str] | None = None,
) -> tuple[str, dict[str, str] | None]:
    """Normalize Teams HTML content to plain text and extract the quote/forward structure.

    degraded: when a field cannot be decoded and falls back to a default, the field name is appended (never the record content).
    """
    if content_raw is None:
        return "", None
    content = _v8_str(content_raw)

    content = content.strip()
    if not content:
        return "", None

    quoted_dict = None

    # 1. Handle blockquote (quote/forward)
    bq_match = re.search(r"<blockquote([^>]*)>(.*?)</blockquote>", content, flags=re.DOTALL | re.IGNORECASE)
    if bq_match:
        bq_inner = bq_match.group(2)

        # Extract the original author (never inferred, and never the literal "Display Name")
        author = ""
        strong_match = re.search(
            r'<strong[^>]*itemprop="mri"[^>]*>(.*?)</strong>', bq_inner, flags=re.DOTALL | re.IGNORECASE
        )
        if strong_match:
            cand = re.sub(r"<[^>]+>", "", strong_match.group(1)).strip()
            if cand and cand != "Display Name":
                author = cand

        # Extract the original time (itemprop="time" or span itemid)
        time_iso = ""
        time_match = re.search(r'<span[^>]*itemprop="time"[^>]*itemid="([^"]+)"', bq_inner, flags=re.IGNORECASE)
        if not time_match:
            time_match = re.search(r'<span[^>]*itemid="([^"]+)"', bq_inner, flags=re.IGNORECASE)
        if time_match:
            raw_time = time_match.group(1).strip()
            try:
                ms = float(raw_time)
                if ms > 0:
                    time_iso = _iso_from_ms(ms)
            except ValueError:
                if degraded is not None:
                    degraded.append("quoted.time")

        # Extract the quoted content (preview or body)
        quote_text = ""
        preview_match = re.search(
            r'<p[^>]*itemprop="preview"[^>]*>(.*?)</p>', bq_inner, flags=re.DOTALL | re.IGNORECASE
        )
        if preview_match:
            raw_preview = preview_match.group(1)
            quote_text = _html_to_text(raw_preview)
        else:
            rem = re.sub(r'<strong[^>]*itemprop="mri"[^>]*>.*?</strong>', "", bq_inner, flags=re.DOTALL | re.IGNORECASE)
            rem = re.sub(r'<span[^>]*itemprop="time"[^>]*>.*?</span>', "", rem, flags=re.DOTALL | re.IGNORECASE)
            rem = re.sub(r'<span[^>]*itemid="[^"]*"[^>]*>.*?</span>', "", rem, flags=re.DOTALL | re.IGNORECASE)
            quote_text = _html_to_text(rem)

        quoted_dict = {
            "author": author,
            "time": time_iso,
            "text": quote_text,
        }

        hdr = []
        if author:
            hdr.append(author)
        if time_iso:
            hdr.append(time_iso)
        if hdr:
            quote_repr = f"> [{', '.join(hdr)}]: {quote_text}\n\n"
        else:
            quote_repr = f"> {quote_text}\n\n" if quote_text else ""

        content = content[: bq_match.start()] + quote_repr + content[bq_match.end() :]

    content = _html_to_text(content)

    return content, quoted_dict


def normalize_system_event(msg_type: str, content_raw: str | bytes | None) -> str:
    """Normalize a system event (ThreadActivity/TopicUpdate etc.) into a readable description."""
    c = _v8_str(content_raw or "")
    if msg_type == "ThreadActivity/TopicUpdate":
        m = re.search(r"<value>(.*?)</value>", c, flags=re.IGNORECASE)
        val = m.group(1).strip() if m else ""
        return f'Changed the group name to "{val}"' if val else "Changed the group name"
    if msg_type == "ThreadActivity/AddMember":
        return "Added a member"
    if msg_type == "ThreadActivity/DeleteMember":
        return "A member left"
    if msg_type == "ThreadActivity/JoiningEnabledUpdate":
        return "Updated the group join permission"
    if "Call" in msg_type:
        return "Voice/video call"

    # Generic: strip XML/HTML tags
    clean = re.sub(r"<[^>]+>", " ", c).strip()
    return clean if clean else "System event"


def _is_valid(val: Any) -> bool:
    if val is None or val is False:
        return False
    if hasattr(val, "__class__") and val.__class__.__name__ == "_Undefined":
        return False
    return bool(val)


def _normalize_body(
    m: dict[str, Any],
    skypeeditedid: Any,
    msg_type_raw: str,
    degraded: list[str] | None = None,
) -> dict[str, Any]:
    """Content determination shared by both sources: deleted/edited flags, attachment names, type, and text / quote."""
    props = m.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
        if degraded is not None:
            degraded.append("properties")

    has_edittime = _is_valid(props.get("edittime"))
    has_deletetime = _is_valid(props.get("deletetime"))
    has_del_info = _is_valid(m.get("deletionInfo"))
    has_skypeeditedid = _is_valid(skypeeditedid)

    raw_content = m.get("content")
    raw_content_str = _v8_str(raw_content).strip() if raw_content is not None else ""

    # Determine the deleted and edited flags
    deleted = has_deletetime or has_del_info or (has_skypeeditedid and not has_edittime and not raw_content_str)
    edited = has_edittime

    # Extract the list of file attachment names
    files: list[str] = []
    attachments: list[dict[str, str | None]] = []
    files_prop = props.get("files")
    if files_prop:
        try:
            parsed_files = json.loads(_v8_str(files_prop)) if isinstance(files_prop, (str, bytes)) else files_prop
            if not isinstance(parsed_files, list):
                raise TypeError("files is not a list")
            for f in parsed_files:
                if isinstance(f, dict):
                    fn = f.get("fileName") or f.get("title") or f.get("name")
                    if fn:
                        files.append(str(fn))
                        info = f.get("fileInfo")
                        info = info if isinstance(info, dict) else {}
                        url = info.get("shareUrl") or f.get("objectUrl") or info.get("fileUrl")
                        attachments.append({"name": str(fn), "url": str(url) if url else None})
        except (json.JSONDecodeError, TypeError):
            files = []
            attachments = []
            if degraded is not None:
                degraded.append("files")

    # Message type: system event vs text message
    m_type_raw = str(m.get("type") or "")
    is_system = (
        msg_type_raw.startswith(("ThreadActivity/", "Event/"))
        or m_type_raw == "Event"
    )

    if deleted:
        text = ""
        quoted = None
    elif is_system:
        text = normalize_system_event(msg_type_raw, raw_content)
        quoted = None
    else:
        text, quoted = normalize_html_content(raw_content, degraded)

    return {
        "type": "system" if is_system else "text",
        "text": text,
        "deleted": deleted,
        "edited": edited,
        "files": files,
        "attachments": attachments,
        "quoted": quoted,
    }


def normalize_teams_message(
    m: dict[str, Any],
    conv_id: str,
    ldb_seq_no: float = 0.0,
    degraded: list[str] | None = None,
    *,
    self_id: str | None | object = _UNSET,
) -> dict[str, Any]:
    """Normalize a raw Teams message dict into a message dict conforming to GLOSSARY.md and the spec.

    degraded: when a field cannot be decoded and falls back to a default, the field name is appended; the record is still returned.
    """
    mid = str(m.get("id") or "").strip()
    if not mid:
        raise ValueError("Message is missing id")
    orig_time = m.get("originalArrivalTime") or m.get("clientArrivalTime") or m.get("sequenceId")
    if isinstance(orig_time, (int, float)) and float(orig_time) > 0:
        ts = float(orig_time) / 1000.0
        time_str = _iso_from_ms(float(orig_time))
    elif isinstance(orig_time, str) and orig_time:
        try:
            val_f = float(orig_time)
            ts = val_f / 1000.0 if val_f > 1e11 else val_f
            time_str = _iso_from_ms(val_f if val_f > 1e11 else val_f * 1000.0)
        except ValueError:
            time_str = orig_time
            ts = 0.0
            if degraded is not None:
                degraded.append("time")
    else:
        ts = 0.0
        time_str = ""
        if degraded is not None:
            degraded.append("time")

    sender_id = _clean_str(m.get("creator")) or _clean_str(m.get("from")) or ""
    sender = _clean_str(m.get("imDisplayName")) or _clean_str(m.get("displayName")) or ""
    if sender == "Display Name":
        sender = ""
    flag = m.get("isSentByCurrentUser")
    configured_id = resolve_self_id() if self_id is _UNSET else self_id
    is_self = flag if isinstance(flag, bool) else bool(
        isinstance(configured_id, str) and sender_id and sender_id.casefold() == configured_id.casefold()
    )

    body = _normalize_body(m, m.get("skypeeditedid"), str(m.get("messageType") or ""), degraded)

    version = resolve_version(m.get("version"), ts, ldb_seq_no)

    return {
        "id": mid,
        "conversation_id": conv_id,
        "time": time_str,
        "timestamp": ts,
        "sender_id": sender_id,
        "sender": sender,
        "is_self": is_self,
        "type": body["type"],
        "text": body["text"],
        "version": version,
        "deleted": body["deleted"],
        "edited": body["edited"],
        "files": body["files"],
        "attachments": body["attachments"],
        "quoted": body["quoted"],
        "sources": ["cache"],
    }


def normalize_teams_export_conversation(
    c: dict[str, Any],
    degraded: list[str] | None = None,
) -> dict[str, Any]:
    """Normalize an official-export conversation dict into a conversation dict conforming to GLOSSARY.md."""
    cid = str(c.get("id") or "")
    if not cid:
        # A conversation without id would be merged with other id-less conversations under an empty id; the caller reports it and skips it
        raise ValueError("Conversation is missing id")
    name = derive_conversation_name(c, cid)
    is_grp = derive_is_group(c)
    unread = None  # The official export's unread state is unknown; fixed to None per the spec

    props = c.get("properties") or {}
    last_im_time = props.get("lastimreceivedtime")
    if last_im_time:
        try:
            dt = datetime.fromisoformat(str(last_im_time))
            last_time = dt.astimezone().isoformat(timespec="seconds")
            last_ts = dt.timestamp()
        except ValueError:
            last_time = str(last_im_time)
            last_ts = 0.0
            if degraded is not None:
                degraded.append("last_time")
    else:
        last_time = ""
        last_ts = 0.0

    version = resolve_version(c.get("version"), last_ts)

    return {
        "id": cid,
        "name": name,
        "is_group": is_grp,
        "unread": unread,
        "last_time": last_time,
        "last_timestamp": last_ts,
        "version": version,
    }


def normalize_teams_export_message(
    m: dict[str, Any],
    conv_id: str,
    user_id: str | None = None,
    degraded: list[str] | None = None,
) -> dict[str, Any]:
    """Normalize a raw official-export message dict into a message dict conforming to GLOSSARY.md."""
    mid = str(m.get("id") or "").strip()
    if not mid:
        raise ValueError("Message is missing id")
    orig_time = m.get("originalarrivaltime") or m.get("clientarrivaltime") or m.get("originalArrivalTime")
    if isinstance(orig_time, str) and orig_time:
        try:
            dt = datetime.fromisoformat(orig_time)
            ts = dt.timestamp()
            time_str = dt.astimezone().isoformat(timespec="seconds")
        except ValueError:
            time_str = orig_time
            ts = 0.0
            if degraded is not None:
                degraded.append("time")
    elif isinstance(orig_time, (int, float)) and float(orig_time) > 0:
        val_f = float(orig_time)
        ts = val_f / 1000.0 if val_f > 1e11 else val_f
        time_str = _iso_from_ms(val_f if val_f > 1e11 else val_f * 1000.0)
    else:
        ts = 0.0
        time_str = ""
        if degraded is not None:
            degraded.append("time")

    version = resolve_version(m.get("version"), ts)

    from_raw = _clean_str(m.get("from")) or ""
    sender_id = from_raw
    # Own identity comes only from the export's userId
    is_self = bool(user_id and from_raw and from_raw.casefold() == user_id.casefold())

    dn = _clean_str(m.get("displayName")) or _clean_str(m.get("imdisplayname")) or ""
    if dn == "Display Name":
        dn = ""
    if dn:
        sender = dn
    elif is_self:
        sender = ""
    elif from_raw:
        sender = from_raw.replace("8:", "")
    else:
        sender = ""

    props = m.get("properties")
    props = props if isinstance(props, dict) else {}
    body = _normalize_body(
        m,
        props.get("skypeeditedid") or m.get("skypeeditedid"),
        str(m.get("messagetype") or m.get("messageType") or ""),
        degraded,
    )

    return {
        "id": mid,
        "conversation_id": conv_id,
        "time": time_str,
        "timestamp": ts,
        "sender_id": sender_id,
        "sender": sender,
        "is_self": is_self,
        "type": body["type"],
        "text": body["text"],
        "version": version,
        "deleted": body["deleted"],
        "edited": body["edited"],
        "files": body["files"],
        "attachments": [],
        "quoted": body["quoted"],
        "sources": ["export"],
    }
