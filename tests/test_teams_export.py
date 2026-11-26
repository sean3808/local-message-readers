"""Seam B & CLI tests for Teams official export import (issue #15).

Verifies:
1. Normalization of official export messages and conversations (field names mirror the real export: originalarrivaltime, messagetype, properties.files, edittime, deletetime, etc.)
2. Cross-source version winner (newest version wins) and merging of sources
3. V1 legacy version rule: rows with a legacy sequence number take the observation time as version after migration, are not overwritten by an older export, deleted messages are not resurrected by an older version; a newer version wins
4. Unorderable conflicts (same version, different content) keep the current state and are recorded in failures
5. .tar and messages.json streaming imports produce no duplicates
6. CLI output conforms to the JSON contract and never leaks message text
"""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path
from typing import Any

import ijson
import pytest

from readers.archive import SCHEMA_VERSION, Archive
from readers.teams.cli import main, resolve_conversation
from readers.teams.export import import_export, prepare_stream
from readers.teams.normalizer import (
    normalize_teams_export_conversation,
    normalize_teams_export_message,
)


def test_import_one_to_one_display_name_shown_without_id_and_counted_unknown(tmp_path: Path, temp_archive: Archive) -> None:
    data = {"userId": "8:self", "conversations": [
        {"id": "19:uni01_test", "MessageList": [
            {"id": "1", "from": "8:self", "displayName": "Self", "content": "a", "version": 1},
            {"id": "2", "displayName": "Only a name", "content": "b", "version": 2},
            {"id": "3", "content": "c", "version": 3},
        ]},
        {"id": "8:other", "MessageList": [
            {"id": "4", "content": "d", "version": 4},
        ]},
        {"id": "19:group", "MessageList": [{"id": "5", "content": "e", "version": 5}]},
    ]}
    path = tmp_path / "messages.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    result = import_export(temp_archive, path)
    messages = {m["id"]: m for m in temp_archive.query_messages("teams", "19:uni01_test", limit=None)[0]}
    assert messages["1"]["sender_id"] == "8:self"
    assert messages["1"]["sender"] == "Self"
    assert messages["1"]["is_self"] is True
    assert messages["2"]["sender_id"] == ""
    assert messages["2"]["sender"] == "Only a name"
    assert messages["2"]["is_self"] is False
    assert messages["3"]["sender_id"] == messages["3"]["sender"] == ""
    assert result["one_to_one_sender_unknown"] == 3


def test_duplicate_across_export_conversations_conflicts_but_reread_updates(tmp_path: Path, temp_archive: Archive) -> None:
    path = tmp_path / "messages.json"
    def write(first: str, second: str) -> None:
        path.write_text(json.dumps({"userId": "8:self", "conversations": [
            {"id": "19:test", "MessageList": [{"id": "1", "version": 1, "content": first}]},
            {"id": "19:test", "MessageList": [{"id": "1", "version": 1, "content": second}]},
        ]}), encoding="utf-8")
    write("first", "second")
    result = import_export(temp_archive, path)
    assert result["unorderable_conflicts"] == 1
    assert temp_archive.query_messages("teams", "19:test")[0][0]["text"] == "first"
    write("updated", "updated")
    result = import_export(temp_archive, path)
    assert result["unorderable_conflicts"] == 0
    assert temp_archive.query_messages("teams", "19:test")[0][0]["text"] == "updated"


@pytest.fixture
def temp_archive(tmp_path: Path) -> Archive:
    """Provide a standalone Archive instance pointing at a temporary directory."""
    db_file = tmp_path / "test_export_archive.db"
    return Archive(db_path=db_file)


def test_export_message_normalisation_mirrors_real_export_field_names() -> None:
    """Normalize hand-written official export records; key names mirror the real export file."""
    cid = "19:test_export_conv@thread.v2"
    user_id = "8:fictional_self"

    # 1. Rich text and links sent by oneself
    raw_self = {
        "id": "1765867388954",
        "displayName": None,
        "originalarrivaltime": "2025-12-16T06:43:08.954Z",
        "messagetype": "RichText/Html",
        "version": 1765867388954,
        "content": "<p>See <a href=\"https://example.com/spec\">documentation</a></p>",
        "conversationid": cid,
        "from": "8:fictional_self",
        "properties": {
            "files": "[]",
        },
    }
    m_self = normalize_teams_export_message(raw_self, cid, user_id=user_id)
    assert m_self["id"] == "1765867388954"
    assert m_self["conversation_id"] == cid
    assert m_self["is_self"] is True
    # When the user's own message has no display name it stays blank; no person's name is hard-coded
    assert m_self["sender"] == ""
    assert m_self["sender_id"] == "8:fictional_self"
    assert m_self["type"] == "text"
    assert m_self["text"] == "See [documentation](https://example.com/spec)"
    assert m_self["version"] == 1765867388954.0
    assert m_self["deleted"] is False
    assert m_self["edited"] is False
    assert m_self["sources"] == ["export"]

    # 2. A message sent by someone else, with a quote, an image and file attachments
    raw_other = {
        "id": "1765867400000",
        "displayName": "Fictional Person #112",
        "originalarrivaltime": "2025-12-16T06:43:20.000Z",
        "messagetype": "RichText/Html",
        "version": 1765867400000,
        "content": (
            "<p>Thanks for the update <img src=\"test.png\" alt=\"chart\"/></p>"
            "<blockquote><strong itemprop=\"mri\">Fictional Self</strong>"
            "<span itemprop=\"time\" itemid=\"1765867388954\"></span>"
            "<p itemprop=\"preview\">See documentation</p></blockquote>"
        ),
        "conversationid": cid,
        "from": "8:fictional_other",
        "properties": {
            "files": json.dumps([{"fileName": "offer_sheet.xlsx"}]),
            "edittime": 1765867405000,
        },
    }
    m_other = normalize_teams_export_message(raw_other, cid, user_id=user_id)
    assert m_other["is_self"] is False
    assert m_other["sender"] == "Fictional Person #112"
    assert m_other["sender_id"] == "8:fictional_other"
    assert "[image]" in m_other["text"]
    assert "> [Fictional Self" in m_other["text"]
    assert m_other["quoted"] is not None
    assert m_other["quoted"]["author"] == "Fictional Self"
    assert m_other["quoted"]["text"] == "See documentation"
    assert m_other["files"] == ["offer_sheet.xlsx"]
    assert m_other["edited"] is True
    assert m_other["deleted"] is False

    # 3. System event
    raw_sys = {
        "id": "1765867500000",
        "displayName": None,
        "originalarrivaltime": "2025-12-16T06:45:00.000Z",
        "messagetype": "ThreadActivity/TopicUpdate",
        "version": 1765867500000,
        "content": "<value>新業務專案</value>",
        "conversationid": cid,
        "from": "8:fictional_self",
        "properties": {},
    }
    m_sys = normalize_teams_export_message(raw_sys, cid, user_id=user_id)
    assert m_sys["type"] == "system"
    assert m_sys["text"] == 'Changed the group name to "新業務專案"'

    # 4. Deleted message
    raw_del = {
        "id": "1765867600000",
        "displayName": "Fictional Person #112",
        "originalarrivaltime": "2025-12-16T06:46:00.000Z",
        "messagetype": "RichText/Html",
        "version": 1765867610000,
        "content": "",
        "conversationid": cid,
        "from": "8:fictional_other",
        "properties": {
            "deletetime": 1765867610000,
        },
    }
    m_del = normalize_teams_export_message(raw_del, cid, user_id=user_id)
    assert m_del["deleted"] is True
    assert m_del["text"] == ""


def test_export_conversation_normalisation() -> None:
    """Test export conversation dict normalization (name, unread is null, group detection)."""
    raw_conv_1to1 = {
        "id": "19:uni01_test@thread.v2",
        "displayName": "Fictional Person #112",
        "version": 1765867448933,
        "properties": {
            "lastimreceivedtime": "2025-12-11T03:57:54.948Z",
        },
        "threadProperties": {
            "membercount": 2,
            "members": '["Export Owner", "Fictional Person #112"]',
            "topic": None,
        },
    }
    c1 = normalize_teams_export_conversation(raw_conv_1to1)
    assert c1["id"] == "19:uni01_test@thread.v2"
    assert c1["name"] == "Fictional Person #112"
    assert c1["is_group"] is False
    assert c1["unread"] is None
    assert c1["version"] == 1765867448933.0
    assert c1["last_timestamp"] > 0

    raw_conv_group = {
        "id": "19:group_test@thread.skype",
        "displayName": "虛構小組",
        "version": 1765867448999,
        "properties": {},
        "threadProperties": {
            "membercount": 8,
            "topic": "虛構小組",
        },
    }
    cg = normalize_teams_export_conversation(raw_conv_group)
    assert cg["name"] == "虛構小組"
    assert cg["is_group"] is True
    assert cg["unread"] is None


def test_cross_source_version_precedence_and_sources(temp_archive: Archive) -> None:
    """Seam B: when cache and export overlap, newest version wins and sources records both."""
    cid = "19:cross_conv@thread.v2"
    mid = "1765000000000"

    # The cache first writes an older version (1765000000000)
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": cid,
            "id": mid,
            "timestamp": 1765000000.0,
            "time": "2025-12-06T10:00:00+08:00",
            "text": "original cache draft",
            "version": 1765000000000.0,
            "deleted": False,
        }],
        source="cache",
    )

    # The export imports a newer edited version (1765000005000)
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": cid,
            "id": mid,
            "timestamp": 1765000000.0,
            "time": "2025-12-06T10:00:00+08:00",
            "text": "final text in export",
            "version": 1765000005000.0,
            "deleted": False,
            "edited": True,
        }],
        source="export",
    )

    msgs, _ = temp_archive.query_messages(channel="teams", conversation_id=cid)
    assert len(msgs) == 1
    assert msgs[0]["text"] == "final text in export"
    assert msgs[0]["edited"] is True
    assert set(msgs[0]["sources"]) == {"cache", "export"}

    # Reverse: if the cache version is newer (1765000010000), the older export (1765000005000) does not overwrite it
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": cid,
            "id": mid,
            "timestamp": 1765000000.0,
            "time": "2025-12-06T10:00:00+08:00",
            "text": "newer cache update",
            "version": 1765000010000.0,
            "deleted": False,
        }],
        source="cache",
    )
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": cid,
            "id": mid,
            "timestamp": 1765000000.0,
            "time": "2025-12-06T10:00:00+08:00",
            "text": "stale export text",
            "version": 1765000005000.0,
            "deleted": False,
        }],
        source="export",
    )
    msgs_after, _ = temp_archive.query_messages(channel="teams", conversation_id=cid)
    assert len(msgs_after) == 1
    assert msgs_after[0]["text"] == "newer cache update"


def _reopen_as_unmigrated(db_path: Path) -> Archive:
    """Reset the existing archive's schema version to zero and reopen it, simulating an old archive before the V1 migration."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    return Archive(db_path=db_path)


def _cache_row(cid: str, mid: str, ts: float, version: float, deleted: bool, text: str = "") -> dict[str, Any]:
    return {
        "conversation_id": cid,
        "id": mid,
        "timestamp": ts,
        "time": "2026-08-10T15:51:38+08:00",
        "text": text,
        "version": version,
        "deleted": deleted,
    }


def test_v1_legacy_row_not_overwritten_by_older_export(tmp_path: Path) -> None:
    """V1: a legacy sequence-number row (version is ldb_seq_no) takes the observation time as version after migration; an older export (2025) does not overwrite the 2026 content."""
    db_path = tmp_path / "legacy.db"
    cid = "19:legacy_conv@thread.v2"
    mid = "1786348298039"
    arc = Archive(db_path=db_path)
    arc.ingest_messages(
        channel="teams",
        messages=[_cache_row(cid, mid, 1786348298.039, 6348150.0, False, "2026 cache message with legacy seq_no")],
        source="cache",
    )
    arc = _reopen_as_unmigrated(db_path)

    arc.ingest_messages(
        channel="teams",
        messages=[_cache_row(cid, mid, 1765867388.954, 1765867388954.0, False, "2025 older export message")],
        source="export",
    )
    msgs, _ = arc.query_messages(channel="teams", conversation_id=cid)
    assert len(msgs) == 1
    assert msgs[0]["text"] == "2026 cache message with legacy seq_no"
    assert set(msgs[0]["sources"]) == {"cache", "export"}


def test_v1_legacy_deleted_row_not_resurrected_by_older_export(tmp_path: Path) -> None:
    """V1: a legacy sequence-number deleted row, observed in 2026; an older export copy (2025, not deleted) must not resurrect it."""
    db_path = tmp_path / "legacy_del.db"
    cid = "19:deleted_protect_conv@thread.v2"
    mid = "1786348500000"
    arc = Archive(db_path=db_path)
    arc.ingest_messages(
        channel="teams",
        messages=[_cache_row(cid, mid, 1786348500.0, 7000000.0, True)],
        source="cache",
    )
    arc = _reopen_as_unmigrated(db_path)

    arc.ingest_messages(
        channel="teams",
        messages=[_cache_row(cid, mid, 1765867388.0, 1765867388954.0, False, "resurrected text from export")],
        source="export",
    )
    msgs, _ = arc.query_messages(channel="teams", conversation_id=cid)
    assert len(msgs) == 1
    assert msgs[0]["deleted"] is True
    assert msgs[0]["text"] == ""


def test_v1_row_already_rewritten_to_send_time_gets_observation_version(tmp_path: Path) -> None:
    """V1: a row already migrated to the send time as version (version == send time) is likewise treated as a legacy row, and its version is raised to the observation time."""
    db_path = tmp_path / "send_time.db"
    cid = "19:send_time_conv@thread.v2"
    mid = "1765867388954"
    arc = Archive(db_path=db_path)
    arc.ingest_messages(
        channel="teams",
        messages=[_cache_row(cid, mid, 1765867388.954, 1765867388954.0, True)],
        source="cache",
    )
    arc = _reopen_as_unmigrated(db_path)

    msg = arc.query_messages(channel="teams", conversation_id=cid)[0][0]
    assert msg["version"] > 1765867388954.0
    assert msg["deleted"] is True


def test_v1_legacy_row_newer_export_edit_wins(tmp_path: Path) -> None:
    """V1: an export edit that happened after the legacy row was observed (its own version is newer) wins."""
    db_path = tmp_path / "legacy_edit.db"
    cid = "19:legacy_edit_conv@thread.v2"
    mid = "1786348298039"
    arc = Archive(db_path=db_path)
    arc.ingest_messages(
        channel="teams",
        messages=[_cache_row(cid, mid, 1786348298.039, 6348150.0, False, "before edit")],
        source="cache",
    )
    arc = _reopen_as_unmigrated(db_path)

    far_future_ms = 4102444800000.0  # 2100-01-01, necessarily later than the observation time
    arc.ingest_messages(
        channel="teams",
        messages=[_cache_row(cid, mid, 1786348298.039, far_future_ms, False, "after edit in export")],
        source="export",
    )
    msgs, _ = arc.query_messages(channel="teams", conversation_id=cid)
    assert len(msgs) == 1
    assert msgs[0]["text"] == "after edit in export"
    assert msgs[0]["version"] == far_future_ms


def test_newer_undeleted_version_wins_over_deleted(temp_archive: Archive) -> None:
    """When a deleted row meets a strictly newer non-deleted version, the newer version wins (the sender can undo a deletion)."""
    cid = "19:undelete_conv@thread.v2"
    mid = "1765867388954"
    temp_archive.ingest_messages(
        channel="teams", messages=[_cache_row(cid, mid, 1765867388.954, 1765867400000.0, True)], source="cache"
    )
    temp_archive.ingest_messages(
        channel="teams",
        messages=[_cache_row(cid, mid, 1765867388.954, 1765867500000.0, False, "restored")],
        source="export",
    )
    msgs, _ = temp_archive.query_messages(channel="teams", conversation_id=cid)
    assert len(msgs) == 1
    assert msgs[0]["deleted"] is False
    assert msgs[0]["text"] == "restored"


def test_v1_migration_runs_only_once(tmp_path: Path) -> None:
    """V1: protected by user_version; opening a second time does not change versions again."""
    db_path = tmp_path / "once.db"
    cid = "19:once_conv@thread.v2"
    mid = "1786348298039"
    arc = Archive(db_path=db_path)
    arc.ingest_messages(
        channel="teams", messages=[_cache_row(cid, mid, 1786348298.039, 6348150.0, False, "x")], source="cache"
    )
    arc = _reopen_as_unmigrated(db_path)
    migrated_version = arc.query_messages(channel="teams", conversation_id=cid)[0][0]["version"]
    assert migrated_version > 6348150.0

    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        conn.execute("UPDATE messages SET version = 5.0")
        conn.commit()

    arc2 = Archive(db_path=db_path)
    assert arc2.query_messages(channel="teams", conversation_id=cid)[0][0]["version"] == 5.0


def test_unorderable_conflict_reported_and_keeps_current(temp_archive: Archive) -> None:
    """Seam B: unorderable conflict (same version on both sides but different content): keep the current state and record it in failures."""
    cid = "19:conflict_conv@thread.v2"
    mid = "1765867388954"
    same_version = 1765867388954.0

    # 1. Message already in the archive
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": cid,
            "id": mid,
            "timestamp": 1765867388.954,
            "time": "2025-12-16T06:43:08+08:00",
            "text": "Current archive content",
            "version": same_version,
            "deleted": False,
        }],
        source="cache",
    )

    failures: list[str] = []
    stats: dict[str, int] = {}

    # 2. Import new data with the same version but different text
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": cid,
            "id": mid,
            "timestamp": 1765867388.954,
            "time": "2025-12-16T06:43:08+08:00",
            "text": "Conflicting incoming content",
            "version": same_version,
            "deleted": False,
        }],
        source="export",
        failures=failures,
        stats=stats,
    )

    # Keep the current state
    msgs, _ = temp_archive.query_messages(channel="teams", conversation_id=cid)
    assert len(msgs) == 1
    assert msgs[0]["text"] == "Current archive content"
    assert "cache" in msgs[0]["sources"]
    assert "export" in msgs[0]["sources"]

    # Record the conflict
    assert stats["unorderable_conflicts"] == 1
    assert len(failures) == 1
    assert "Version conflict (cannot be ordered)" in failures[0]
    assert mid in failures[0]


def _build_synthetic_export_json(conv_id: str, msg_count: int = 5) -> dict[str, Any]:
    """Build a hand-written synthetic official export dict structure."""
    msgs = [
        {
            "id": str(1765000000000 + i * 1000),
            "displayName": "User A" if i % 2 == 0 else None,
            "originalarrivaltime": f"2025-12-06T10:0{i}:00.000Z",
            "messagetype": "RichText/Html",
            "version": 1765000000000 + i * 1000,
            "content": f"<p>Synthetic message {i}</p>",
            "conversationid": conv_id,
            "from": "8:user_a" if i % 2 == 0 else "8:fictional_self",
            "properties": {"files": "[]"},
        }
        for i in range(msg_count)
    ]
    return {
        "userId": "8:fictional_self",
        "exportDate": "2025-12-16T09:28",
        "conversations": [
            {
                "id": conv_id,
                "displayName": "Synthetic Export Conv",
                "version": 1765000000000 + msg_count * 1000,
                "properties": {"lastimreceivedtime": f"2025-12-06T10:0{msg_count}:00.000Z"},
                "threadProperties": {"membercount": 2, "topic": None},
                "MessageList": msgs,
            }
        ],
    }


def test_import_export_json_and_tar_streaming_no_duplicates(tmp_path: Path, temp_archive: Archive) -> None:
    """Test importing .tar and messages.json: streaming parse yields no duplicates, and a second import adds 0 rows."""
    conv_id = "19:tar_stream_conv@thread.v2"
    export_data = _build_synthetic_export_json(conv_id, msg_count=5)

    # Write messages.json
    json_path = tmp_path / "messages.json"
    json_bytes = json.dumps(export_data).encode("utf-8")
    json_path.write_bytes(json_bytes)

    # Pack into a .tar
    tar_path = tmp_path / "export.tar"
    with tarfile.open(tar_path, "w") as tar:
        ti = tarfile.TarInfo(name="messages.json")
        ti.size = len(json_bytes)
        tar.addfile(ti, io.BytesIO(json_bytes))

    # 1. Import the .tar
    res1 = import_export(temp_archive, tar_path)
    assert res1["conversations_new"] == 1
    assert res1["messages_new"] == 5
    assert res1["messages_overlapping"] == 0
    assert res1["unorderable_conflicts"] == 0
    assert res1["failures"] == []

    # 2. Import the extracted messages.json a second time (overlapping)
    res2 = import_export(temp_archive, json_path)
    assert res2["conversations_new"] == 0
    assert res2["messages_new"] == 0
    assert res2["messages_overlapping"] == 5
    assert res2["unorderable_conflicts"] == 0
    assert res2["conversations_before"] == res2["conversations_after"]
    assert res2["messages_before"] == res2["messages_after"]

    # Verify queries
    msgs, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id)
    assert len(msgs) == 5
    assert msgs[0]["text"] == "Synthetic message 0"


def test_cli_import_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test the CLI command teams-reader import-export: the output structure is correct and never contains message text."""
    conv_id = "19:cli_conv@thread.v2"
    export_data = _build_synthetic_export_json(conv_id, msg_count=3)

    json_path = tmp_path / "messages.json"
    json_path.write_text(json.dumps(export_data), encoding="utf-8")
    db_path = tmp_path / "cli_archive.db"

    code = main(["import-export", str(json_path), "--archive-path", str(db_path)])
    assert code == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["conversations_new"] == 1
    assert result["messages_new"] == 3
    assert result["unorderable_conflicts"] == 0
    assert result["failures"] == []

    # Key check: the output must never contain message text
    assert "Synthetic message" not in captured.out


class _NonSeekable:
    """A stream that offers only read(), simulating a non-seekable source."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def seekable(self) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)


def _write_json(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _export_msg(mid: int, conv_id: str, sender: str, text: str) -> dict[str, Any]:
    return {
        "id": str(mid),
        "displayName": None,
        "originalarrivaltime": "2025-12-06T10:00:00.000Z",
        "messagetype": "RichText/Html",
        "version": mid,
        "content": f"<p>{text}</p>",
        "conversationid": conv_id,
        "from": sender,
        "properties": {"files": "[]"},
    }


def _one_to_one_export(user_id: str | None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "exportDate": "2025-12-16T09:28",
        "conversations": [
            {
                "id": "8:bob",
                "displayName": "Bob Export",
                "version": 1765000009000,
                "properties": {"lastimreceivedtime": "2025-12-06T10:00:09.000Z"},
                "threadProperties": {"membercount": 2},
                "MessageList": [
                    _export_msg(1765000001000, "8:bob", "8:bob", "shared one"),
                    _export_msg(1765000002000, "8:bob", user_id or "8:me", "shared two"),
                    _export_msg(1765000003000, "8:bob", "8:bob", "export only"),
                ],
            },
            {
                "id": "8:carol",
                "displayName": "Carol Export",
                "version": 1765000009000,
                "properties": {"lastimreceivedtime": "2025-12-06T10:00:09.000Z"},
                "threadProperties": {"membercount": 2},
                "MessageList": [_export_msg(1765000004000, "8:carol", "8:carol", "carol msg")],
            },
        ],
    }
    if user_id:
        data = {"userId": user_id, **data}
    return data


def _seed_cache_one_to_one(arc: Archive, with_messages: bool = True) -> None:
    arc.ingest_conversations(
        "teams",
        [
            {
                "id": "19:uni01_x",
                "name": "Bob Cache",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-01T10:00:00+08:00",
                "last_timestamp": 1793498400.0,
                "version": 1793498400000.0,
                "member_mris": ["8:bob", "8:me"],
            },
            {
                "id": "19:uni01_other",
                "name": "Dave Cache",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-01T10:00:00+08:00",
                "last_timestamp": 1793498400.0,
                "version": 1793498400000.0,
                "member_mris": ["8:dave", "8:me"],
            },
        ],
        source="cache",
    )
    if not with_messages:
        return
    arc.ingest_messages(
        channel="teams",
        messages=[
            {
                "conversation_id": "19:uni01_x",
                "id": mid,
                "timestamp": float(mid) / 1000.0,
                "time": "2025-12-06T10:00:00+00:00",
                "text": txt,
                "version": float(mid),
            }
            for mid, txt in (("1765000001000", "shared one"), ("1765000002000", "shared two"))
        ],
        source="cache",
    )


def test_one_to_one_export_conversation_merges_into_cache_conversation(
    tmp_path: Path, temp_archive: Archive
) -> None:
    """Seam B: the exported `8:bob` 1:1 conversation merges into the cached conversation `19:uni01_x`; shared messages are stored once; `8:carol` cannot be mapped and is kept."""
    _seed_cache_one_to_one(temp_archive)
    export_file = _write_json(tmp_path / "messages.json", _one_to_one_export("8:me"))

    res = import_export(temp_archive, export_file)

    assert res["one_to_one_mapped"] == 1
    assert res["one_to_one_unmapped"] == 1
    assert res["messages_in_mapped_conversations"] == 3
    assert res["failures"] == []

    conv_ids = {c["id"] for c in temp_archive.query_conversations("teams")}
    assert "19:uni01_x" in conv_ids
    assert "8:bob" not in conv_ids
    assert "8:carol" in conv_ids

    msgs, _ = temp_archive.query_messages("teams", "19:uni01_x", limit=None)
    ids = [m["id"] for m in msgs]
    assert sorted(ids) == ["1765000001000", "1765000002000", "1765000003000"]
    by_id = {m["id"]: m for m in msgs}
    assert sorted(by_id["1765000001000"]["sources"]) == ["cache", "export"]
    assert sorted(by_id["1765000002000"]["sources"]) == ["cache", "export"]
    assert by_id["1765000003000"]["sources"] == ["export"]
    assert temp_archive.query_messages("teams", "8:bob")[0] == []
    assert len(temp_archive.query_messages("teams", "8:carol")[0]) == 1

    # The merged conversation can be found by the old id and the old name
    assert temp_archive.get_conversation("teams", "8:bob")["id"] == "19:uni01_x"
    assert temp_archive.get_conversation("teams", "8:carol")["id"] == "8:carol"
    by_name = temp_archive.find_conversations_by_name("teams", "Bob Export")
    assert [c["id"] for c in by_name] == ["19:uni01_x"]
    assert resolve_conversation(temp_archive, "8:bob")["id"] == "19:uni01_x"

    # Import once more: no new rows are added
    res2 = import_export(temp_archive, export_file)
    assert res2["messages_new"] == 0
    assert res2["conversations_new"] == 0
    assert res2["messages_before"] == res2["messages_after"]


def test_one_to_one_not_merged_without_user_id(tmp_path: Path, temp_archive: Archive) -> None:
    """The export has no userId: is_self is not determined; with no member match and no shared message id evidence in the archive, nothing is merged and one failures entry is recorded."""
    _seed_cache_one_to_one(temp_archive, with_messages=False)
    export_file = _write_json(tmp_path / "messages.json", _one_to_one_export(None))

    res = import_export(temp_archive, export_file)

    assert res["one_to_one_mapped"] == 0
    assert res["one_to_one_unmapped"] == 2
    assert any("userId" in f for f in res["failures"])
    msgs, _ = temp_archive.query_messages("teams", "8:bob")
    assert len(msgs) == 3
    assert all(m["is_self"] is False for m in msgs)


def test_self_identity_comes_from_user_id_in_tar_stream(tmp_path: Path, temp_archive: Archive) -> None:
    """is_self is decided only by the export's userId (.tar member stream); no account or name is hard-coded."""
    conv_id = "19:self_tar_conv@thread.v2"
    data = _build_synthetic_export_json(conv_id, msg_count=4)
    data["userId"] = "8:someone_else_entirely"
    for m in data["conversations"][0]["MessageList"]:
        m["displayName"] = None
        m["from"] = "8:someone_else_entirely" if m["from"] == "8:fictional_self" else m["from"]
    payload = json.dumps(data).encode("utf-8")
    tar_path = tmp_path / "export.tar"
    with tarfile.open(tar_path, "w") as tar:
        ti = tarfile.TarInfo(name="export/messages.json")
        ti.size = len(payload)
        tar.addfile(ti, io.BytesIO(payload))

    import_export(temp_archive, tar_path)

    msgs, _ = temp_archive.query_messages("teams", conv_id)
    self_msgs = [m for m in msgs if m["is_self"]]
    assert len(self_msgs) == 2
    assert all(m["sender_id"] == "8:someone_else_entirely" for m in self_msgs)
    assert all(m["sender"] == "" for m in self_msgs)


def test_prepare_stream_reads_user_id_from_non_seekable_stream() -> None:
    """Non-seekable stream: userId can still be read, and ijson can still parse everything from the start (the leading chunk is spliced back)."""
    data = _build_synthetic_export_json("19:pad@thread.v2", msg_count=3)
    data["userId"] = "8:nonseek"
    pad = "x" * 100_000  # make the content longer than the leading read length
    data["conversations"][0]["MessageList"][0]["content"] = f"<p>{pad}</p>"
    payload = json.dumps({"userId": data["userId"], **{k: v for k, v in data.items() if k != "userId"}}).encode()

    user_id, stream = prepare_stream(_NonSeekable(payload))  # type: ignore[arg-type]

    assert user_id == "8:nonseek"
    convs = list(ijson.items(stream, "conversations.item"))
    assert len(convs) == 1
    assert len(convs[0]["MessageList"]) == 3


def test_import_export_rejects_directory_and_unknown_suffix(tmp_path: Path, temp_archive: Archive) -> None:
    """import-export accepts only .tar or .json files: directories and other extensions raise a clear error."""
    inner = tmp_path / "export_dir"
    inner.mkdir()
    _write_json(inner / "messages.json", _build_synthetic_export_json("19:dir@thread.v2"))
    with pytest.raises(ValueError):
        import_export(temp_archive, inner)

    other = tmp_path / "messages.txt"
    other.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        import_export(temp_archive, other)

    assert main(["import-export", str(inner), "--archive-path", str(tmp_path / "x.db")]) == 1


def test_cache_conversation_member_mris_persist_and_backfill(temp_archive: Archive) -> None:
    """Cache import records the member mris of 1:1 conversations; existing rows are filled in on the next refresh."""
    from readers.teams.normalizer import derive_member_mris

    one_to_one = {
        "id": "19:uni01_m",
        "threadProperties": {"templateType": "skypeonetoone"},
        "members": [{"id": "8:me"}, {"id": "8:bob"}],
    }
    group = {
        "id": "19:g",
        "threadProperties": {"topic": "Group topic"},
        "members": [{"id": "8:me"}, {"id": "8:bob"}, {"id": "8:x"}],
    }
    assert derive_member_mris(one_to_one) == ["8:bob", "8:me"]
    assert derive_member_mris(group) is None

    base = {
        "id": "19:uni01_m",
        "name": "Bob",
        "is_group": False,
        "unread": False,
        "last_time": "",
        "last_timestamp": 0.0,
        "version": 10.0,
    }
    temp_archive.ingest_conversations("teams", [base], source="cache")
    assert temp_archive.list_conversation_members("teams") == {}
    temp_archive.ingest_conversations(
        "teams", [{**base, "version": 11.0, "member_mris": ["8:bob", "8:me"]}], source="cache"
    )
    assert temp_archive.list_conversation_members("teams") == {"19:uni01_m": ["8:bob", "8:me"]}
    # A later update without member info does not clear the members already recorded
    temp_archive.ingest_conversations("teams", [{**base, "version": 12.0}], source="cache")
    assert temp_archive.list_conversation_members("teams") == {"19:uni01_m": ["8:bob", "8:me"]}


def _cache_conv(cid: str, members: list[str]) -> dict[str, Any]:
    return {
        "id": cid,
        "name": f"Cache {cid}",
        "is_group": False,
        "unread": False,
        "last_time": "2026-11-01T10:00:00+08:00",
        "last_timestamp": 1793498400.0,
        "version": 1793498400000.0,
        "member_mris": members,
    }


def _cache_msg(cid: str, mid: int) -> dict[str, Any]:
    return {
        "conversation_id": cid,
        "id": str(mid),
        "timestamp": mid / 1000.0,
        "time": "2025-12-06T10:00:00+00:00",
        "text": f"m{mid}",
        "version": float(mid),
    }


def _bob_only_export(tmp_path: Path) -> Path:
    data = {
        "userId": "8:me",
        "conversations": [
            {
                "id": "8:bob",
                "displayName": "Bob Export",
                "version": 1765000009000,
                "properties": {"lastimreceivedtime": "2025-12-06T10:00:09.000Z"},
                "threadProperties": {"membercount": 2},
                "MessageList": [
                    _export_msg(1765000001000, "8:bob", "8:bob", "a"),
                    _export_msg(1765000002000, "8:bob", "8:bob", "b"),
                ],
            }
        ],
    }
    return _write_json(tmp_path / "messages.json", data)


def test_ambiguous_candidates_resolved_by_shared_message_ids(tmp_path: Path, temp_archive: Archive) -> None:
    """The same counterpart has two cached 1:1 conversations: merge into the one that shares message ids with the export."""
    temp_archive.ingest_conversations(
        "teams",
        [_cache_conv("19:uni01_a", ["8:bob", "8:me"]), _cache_conv("19:uni01_b", ["8:bob", "8:me"])],
        source="cache",
    )
    shared = {**_cache_msg("19:uni01_b", 1765000001000), "text": "a"}
    temp_archive.ingest_messages("teams", [shared], source="cache")
    temp_archive.ingest_messages("teams", [_cache_msg("19:uni01_a", 1765000099000)], source="cache")

    res = import_export(temp_archive, _bob_only_export(tmp_path))

    assert res["one_to_one_mapped"] == 1
    assert res["one_to_one_unmapped"] == 0
    assert res["failures"] == []
    ids_b = sorted(m["id"] for m in temp_archive.query_messages("teams", "19:uni01_b", limit=None)[0])
    assert ids_b == ["1765000001000", "1765000002000"]
    assert [m["id"] for m in temp_archive.query_messages("teams", "19:uni01_a", limit=None)[0]] == ["1765000099000"]
    assert temp_archive.get_conversation("teams", "8:bob")["id"] == "19:uni01_b"


def test_ambiguous_candidates_without_shared_ids_not_merged(tmp_path: Path, temp_archive: Archive) -> None:
    """Neither candidate shares message ids: do not merge, keep the 8: id and record failures."""
    temp_archive.ingest_conversations(
        "teams",
        [_cache_conv("19:uni01_a", ["8:bob", "8:me"]), _cache_conv("19:uni01_b", ["8:bob", "8:me"])],
        source="cache",
    )
    temp_archive.ingest_messages("teams", [_cache_msg("19:uni01_a", 1765000099000)], source="cache")

    res = import_export(temp_archive, _bob_only_export(tmp_path))

    assert res["one_to_one_mapped"] == 0
    assert res["one_to_one_unmapped"] == 1
    assert len(res["failures"]) == 1
    assert len(temp_archive.query_messages("teams", "8:bob", limit=None)[0]) == 2


def test_member_list_without_self_needs_shared_ids_and_self_only_never(tmp_path: Path, temp_archive: Archive) -> None:
    """Member list lacks self (counterpart only): without shared message ids nothing is merged; a conversation with only self maps to nobody."""
    temp_archive.ingest_conversations(
        "teams",
        [_cache_conv("19:uni01_x", ["8:bob"]), _cache_conv("19:uni01_self", ["8:me"])],
        source="cache",
    )

    res = import_export(temp_archive, _bob_only_export(tmp_path))

    assert res["one_to_one_mapped"] == 0
    assert res["one_to_one_unmapped"] == 1
    assert temp_archive.query_messages("teams", "19:uni01_x", limit=None)[0] == []
    assert temp_archive.query_messages("teams", "19:uni01_self")[0] == []
    assert len(temp_archive.query_messages("teams", "8:bob", limit=None)[0]) == 2


def test_member_list_without_self_with_shared_ids_merges(tmp_path: Path, temp_archive: Archive) -> None:
    """Member list lacks self, but shares message ids with the export: merge by shared messages."""
    temp_archive.ingest_conversations("teams", [_cache_conv("19:uni01_x", ["8:bob"])], source="cache")
    shared = {**_cache_msg("19:uni01_x", 1765000001000), "text": "a"}
    temp_archive.ingest_messages("teams", [shared], source="cache")

    res = import_export(temp_archive, _bob_only_export(tmp_path))

    assert res["one_to_one_mapped"] == 1
    assert temp_archive.get_conversation("teams", "8:bob")["id"] == "19:uni01_x"
    ids = sorted(m["id"] for m in temp_archive.query_messages("teams", "19:uni01_x", limit=None)[0])
    assert ids == ["1765000001000", "1765000002000"]


def test_zero_member_candidates_fall_back_to_shared_message_ids(tmp_path: Path, temp_archive: Archive) -> None:
    """When member matching finds no candidate, look at message ids: exactly one cached non-group conversation sharing ids is merged into."""
    temp_archive.ingest_conversations("teams", [_cache_conv("19:uni01_z", ["8:me"])], source="cache")
    shared = {**_cache_msg("19:uni01_z", 1765000001000), "text": "a"}
    temp_archive.ingest_messages("teams", [shared], source="cache")

    res = import_export(temp_archive, _bob_only_export(tmp_path))

    assert res["one_to_one_mapped"] == 1
    assert res["one_to_one_unmapped"] == 0
    assert res["failures"] == []
    ids = sorted(m["id"] for m in temp_archive.query_messages("teams", "19:uni01_z", limit=None)[0])
    assert ids == ["1765000001000", "1765000002000"]
    assert temp_archive.get_conversation("teams", "8:bob")["id"] == "19:uni01_z"
    assert "8:bob" not in {c["id"] for c in temp_archive.query_conversations("teams")}


def test_zero_member_candidates_without_shared_ids_stay_unmapped(tmp_path: Path, temp_archive: Archive) -> None:
    """No member-match candidate and no shared message ids: keep the 8: id."""
    temp_archive.ingest_conversations("teams", [_cache_conv("19:uni01_z", ["8:me"])], source="cache")
    temp_archive.ingest_messages("teams", [_cache_msg("19:uni01_z", 1765000099000)], source="cache")

    res = import_export(temp_archive, _bob_only_export(tmp_path))

    assert res["one_to_one_mapped"] == 0
    assert res["one_to_one_unmapped"] == 1
    assert len(temp_archive.query_messages("teams", "8:bob", limit=None)[0]) == 2
    assert [m["id"] for m in temp_archive.query_messages("teams", "19:uni01_z", limit=None)[0]] == ["1765000099000"]


def test_existing_alias_is_reused_even_if_member_data_changed(tmp_path: Path, temp_archive: Archive) -> None:
    """An exported conversation with a registered alias reuses the existing target and does not re-match members."""
    temp_archive.ingest_conversations("teams", [_cache_conv("19:uni01_x", ["8:bob", "8:me"])], source="cache")
    export_file = _bob_only_export(tmp_path)
    assert import_export(temp_archive, export_file)["one_to_one_mapped"] == 1

    # After the member data changes the original candidate no longer holds, and another, seemingly better, candidate appears
    temp_archive.ingest_conversations(
        "teams",
        [
            {**_cache_conv("19:uni01_x", ["8:zed", "8:me"]), "version": 1793498500000.0},
            _cache_conv("19:uni01_y", ["8:bob", "8:me"]),
        ],
        source="cache",
    )
    res = import_export(temp_archive, export_file)

    assert res["one_to_one_mapped"] == 1
    assert res["messages_new"] == 0
    assert temp_archive.get_alias_target("teams", "8:bob") == "19:uni01_x"
    assert temp_archive.query_messages("teams", "19:uni01_y")[0] == []


def test_late_mapping_moves_earlier_legacy_copy_without_duplicates(tmp_path: Path, temp_archive: Archive) -> None:
    """Messages previously stored under `8:bob` because of no mapping are merged once a mapped cache conversation appears, leaving no duplicates."""
    export_file = _bob_only_export(tmp_path)
    res1 = import_export(temp_archive, export_file)
    assert res1["one_to_one_unmapped"] == 1
    assert len(temp_archive.query_messages("teams", "8:bob", limit=None)[0]) == 2

    temp_archive.ingest_conversations("teams", [_cache_conv("19:uni01_x", ["8:bob", "8:me"])], source="cache")
    temp_archive.ingest_messages(
        "teams", [{**_cache_msg("19:uni01_x", 1765000001000), "text": "a"}], source="cache"
    )

    res2 = import_export(temp_archive, export_file)

    assert res2["one_to_one_mapped"] == 1
    assert res2["messages_moved_from_legacy_ids"] == 2
    assert res2["failures"] == []
    assert "8:bob" not in {c["id"] for c in temp_archive.query_conversations("teams")}
    msgs, _ = temp_archive.query_messages("teams", "19:uni01_x", limit=None)
    assert sorted(m["id"] for m in msgs) == ["1765000001000", "1765000002000"]
    by_id = {m["id"]: m for m in msgs}
    assert sorted(by_id["1765000001000"]["sources"]) == ["cache", "export"]
    assert by_id["1765000002000"]["sources"] == ["export"]
    assert temp_archive.get_conversation("teams", "8:bob")["id"] == "19:uni01_x"
    with closing(sqlite3.connect(str(temp_archive.db_path))) as conn:
        total = conn.execute("SELECT COUNT(*) FROM messages WHERE channel = 'teams'").fetchone()[0]
        legacy = conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = '8:bob'").fetchone()[0]
    assert (total, legacy) == (2, 0)

    res3 = import_export(temp_archive, export_file)
    assert res3["messages_new"] == 0
    assert res3["messages_moved_from_legacy_ids"] == 0
    assert res3["messages_before"] == res3["messages_after"] == 2


def test_in_batch_equal_version_conflict_reported_once_first_row_kept(temp_archive: Archive) -> None:
    """Same id and same version but different content within one batch: record one unorderable conflict and keep the first."""
    first = {**_cache_msg("19:c", 1765000001000), "text": "first"}
    second = {**first, "text": "second"}
    failures: list[str] = []
    stats: dict[str, int] = {}

    temp_archive.ingest_messages("teams", [first, second], source="cache", failures=failures, stats=stats)

    assert stats["unorderable_conflicts"] == 1
    assert len(failures) == 1
    assert "first" not in failures[0] and "second" not in failures[0]
    msgs, _ = temp_archive.query_messages("teams", "19:c", limit=None)
    assert [m["text"] for m in msgs] == ["first"]

    # A higher version still wins silently
    failures2: list[str] = []
    higher = {**first, "text": "third", "version": first["version"] + 1}
    temp_archive.ingest_messages("teams", [first, higher], source="cache", failures=failures2, stats={})
    assert failures2 == []
    assert [m["text"] for m in temp_archive.query_messages("teams", "19:c", limit=None)[0]] == ["third"]


def test_export_messages_without_id_skipped_and_reported(tmp_path: Path, temp_archive: Archive) -> None:
    no_id1 = _export_msg(1, "19:noid", "8:bob", "lost one")
    no_id2 = _export_msg(2, "19:noid", "8:bob", "lost two")
    del no_id1["id"]
    no_id2["id"] = ""
    data = {
        "conversations": [
            {
                "id": "19:noid",
                "displayName": "No Id",
                "version": 1,
                "properties": {"lastimreceivedtime": "2025-12-06T10:00:09.000Z"},
                "threadProperties": {"membercount": 3},
                "MessageList": [no_id1, no_id2, _export_msg(3, "19:noid", "8:bob", "kept")],
            }
        ],
    }
    failures: list[str] = []
    import_export(temp_archive, _write_json(tmp_path / "messages.json", data), failures=failures)

    assert [m["id"] for m in temp_archive.query_messages("teams", "19:noid")[0]] == ["3"]
    assert len([f for f in failures if "19:noid" in f]) == 2, failures


def test_export_message_with_unparseable_time_is_kept_and_reported(
    tmp_path: Path, temp_archive: Archive
) -> None:
    bad = _export_msg(1, "19:badtime", "8:bob", "odd time")
    bad["originalarrivaltime"] = "not-a-time"
    data = {
        "conversations": [
            {
                "id": "19:badtime",
                "displayName": "Bad Time",
                "version": 1,
                "properties": {"lastimreceivedtime": "2025-12-06T10:00:09.000Z"},
                "threadProperties": {"membercount": 3},
                "MessageList": [_export_msg(2, "19:badtime", "8:bob", "fine"), bad],
            }
        ],
    }
    failures: list[str] = []
    import_export(temp_archive, _write_json(tmp_path / "messages.json", data), failures=failures)

    assert sorted(m["id"] for m in temp_archive.query_messages("teams", "19:badtime")[0]) == ["1", "2"]
    hits = [f for f in failures if "time" in f and "position=1" in f and "id='1'" in f]
    assert len(hits) == 1, failures
    assert "not-a-time" not in hits[0]


def test_export_conversations_without_id_are_reported_not_merged(
    tmp_path: Path, temp_archive: Archive
) -> None:
    data = {
        "conversations": [
            {"displayName": "No Id A", "MessageList": [_export_msg(1, "", "8:bob", "a")]},
            {"id": "", "displayName": "No Id B", "MessageList": [_export_msg(2, "", "8:bob", "b")]},
            {
                "id": "19:ok",
                "displayName": "Ok",
                "threadProperties": {"membercount": 3},
                "MessageList": [_export_msg(3, "19:ok", "8:bob", "c")],
            },
        ],
    }
    failures: list[str] = []
    import_export(temp_archive, _write_json(tmp_path / "messages.json", data), failures=failures)

    assert [c["id"] for c in temp_archive.query_conversations("teams")] == ["19:ok"]
    assert len([f for f in failures if "Could not parse export conversation" in f]) == 2, failures
