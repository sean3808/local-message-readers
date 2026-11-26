"""Seam B: Archive ingest/query interface tests.

Uses purely hand-written normalized data structures, importing no module of any specific messaging app (such as Teams or WeChat).
Verifies cross-channel isolation, the version-winning rule (newest version wins), deleted-state protection, incremental accumulation without loss, and similar properties.
"""

from datetime import datetime
from pathlib import Path

import pytest

from readers.archive import Archive, get_default_archive_path


@pytest.fixture
def temp_archive(tmp_path: Path) -> Archive:
    """Provide an independent Archive instance pointing at a temporary directory."""
    db_file = tmp_path / "test_archive.db"
    return Archive(db_path=db_file)


def test_channel_is_part_of_key(temp_archive: Archive) -> None:
    """Conversations and messages with the same id in different channels must stay isolated and not interfere with each other."""
    shared_conv_id = "conv_common_101"
    shared_msg_id = "msg_common_202"

    temp_archive.ingest_conversations(
        channel="teams",
        conversations=[{
            "id": shared_conv_id,
            "name": "Teams Group",
            "is_group": True,
            "unread": False,
            "last_time": "2026-11-10T10:00:00+00:00",
            "last_timestamp": 1794268800,
            "version": 1.0,
        }],
        source="cache",
    )

    temp_archive.ingest_conversations(
        channel="wechat",
        conversations=[{
            "id": shared_conv_id,
            "name": "WeChat Contact",
            "is_group": False,
            "unread": True,
            "last_time": "2026-11-11T12:00:00+00:00",
            "last_timestamp": 1794362400,
            "version": 2.0,
        }],
        source="cache",
    )

    teams_convs = temp_archive.query_conversations(channel="teams")
    assert len(teams_convs) == 1
    assert teams_convs[0]["id"] == shared_conv_id
    assert teams_convs[0]["name"] == "Teams Group"
    assert teams_convs[0]["is_group"] is True

    wechat_convs = temp_archive.query_conversations(channel="wechat")
    assert len(wechat_convs) == 1
    assert wechat_convs[0]["id"] == shared_conv_id
    assert wechat_convs[0]["name"] == "WeChat Contact"
    assert wechat_convs[0]["is_group"] is False

    # Verify that messages are also strictly isolated by channel
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": shared_conv_id,
            "id": shared_msg_id,
            "timestamp": 1000,
            "text": "Hello Teams",
            "version": 1.0,
        }],
        source="cache",
    )

    temp_archive.ingest_messages(
        channel="wechat",
        messages=[{
            "conversation_id": shared_conv_id,
            "id": shared_msg_id,
            "timestamp": 1000,
            "text": "Hello WeChat",
            "version": 1.0,
        }],
        source="cache",
    )

    teams_msgs, _ = temp_archive.query_messages(channel="teams", conversation_id=shared_conv_id)
    assert len(teams_msgs) == 1
    assert teams_msgs[0]["text"] == "Hello Teams"

    wechat_msgs, _ = temp_archive.query_messages(channel="wechat", conversation_id=shared_conv_id)
    assert len(wechat_msgs) == 1
    assert wechat_msgs[0]["text"] == "Hello WeChat"


def test_conversation_newest_version_wins(temp_archive: Archive) -> None:
    """A newer Conversation version overwrites the old one; an older version does not overwrite the current state."""
    cid = "conv_ver_test"

    temp_archive.ingest_conversations(
        channel="teams",
        conversations=[{
            "id": cid,
            "name": "Initial Name",
            "is_group": False,
            "unread": False,
            "last_time": "2026-11-01T00:00:00+00:00",
            "last_timestamp": 1793500000,
            "version": 10.0,
        }],
        source="cache",
    )

    # Import the newer version (version=20)
    temp_archive.ingest_conversations(
        channel="teams",
        conversations=[{
            "id": cid,
            "name": "Updated Name",
            "is_group": False,
            "unread": True,
            "last_time": "2026-11-02T00:00:00+00:00",
            "last_timestamp": 1793586400,
            "version": 20.0,
        }],
        source="cache",
    )

    res = temp_archive.query_conversations(channel="teams")
    assert len(res) == 1
    assert res[0]["name"] == "Updated Name"
    assert res[0]["unread"] is True

    # Import the older version (version=5); it must not overwrite
    temp_archive.ingest_conversations(
        channel="teams",
        conversations=[{
            "id": cid,
            "name": "Ancient Name",
            "is_group": False,
            "unread": False,
            "last_time": "2026-10-31T00:00:00+00:00",
            "last_timestamp": 1793400000,
            "version": 5.0,
        }],
        source="export",
    )

    res = temp_archive.query_conversations(channel="teams")
    assert len(res) == 1
    assert res[0]["name"] == "Updated Name"
    assert res[0]["unread"] is True


def test_conversation_absent_from_later_ingest_is_not_removed(temp_archive: Archive) -> None:
    """Conversations absent from a later import are not deleted (cumulative archive, ADR-0006)."""
    temp_archive.ingest_conversations(
        channel="teams",
        conversations=[
            {
                "id": "c1",
                "name": "Conv 1",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-01T10:00:00+00:00",
                "last_timestamp": 1000,
            },
            {
                "id": "c2",
                "name": "Conv 2",
                "is_group": True,
                "unread": False,
                "last_time": "2026-11-02T10:00:00+00:00",
                "last_timestamp": 2000,
            },
        ],
        source="cache",
    )

    # The second import has only c3; c1 and c2 must be kept
    temp_archive.ingest_conversations(
        channel="teams",
        conversations=[{
            "id": "c3",
            "name": "Conv 3",
            "is_group": False,
            "unread": True,
            "last_time": "2026-11-03T10:00:00+00:00",
            "last_timestamp": 3000,
        }],
        source="cache",
    )

    res = temp_archive.query_conversations(channel="teams")
    ids = [c["id"] for c in res]
    assert set(ids) == {"c1", "c2", "c3"}
    # and sorted by last_message_timestamp in descending order
    assert ids == ["c3", "c2", "c1"]


def test_conversations_ordered_by_subsecond_time(temp_archive: Archive) -> None:
    """Two conversations within the same second are still ordered by last message time, not falling back to id order."""
    temp_archive.ingest_conversations(
        channel="teams",
        conversations=[
            {"id": "a_earlier", "name": "A", "is_group": False, "unread": False,
             "last_time": "2026-11-01T10:00:00+00:00", "last_timestamp": 1000.2},
            {"id": "b_later", "name": "B", "is_group": False, "unread": False,
             "last_time": "2026-11-01T10:00:00+00:00", "last_timestamp": 1000.7},
        ],
        source="cache",
    )

    res = temp_archive.query_conversations(channel="teams")
    assert [c["id"] for c in res] == ["b_later", "a_earlier"]
    assert all(c["last_timestamp"] == 1000 for c in res)


def test_unknown_unread_stays_null(temp_archive: Archive) -> None:
    """An unknown unread state is emitted as null and must not be converted to False (read)."""
    temp_archive.ingest_conversations(
        channel="teams",
        conversations=[
            {
                "id": "c_unknown",
                "name": "Unknown read state",
                "is_group": False,
                "unread": None,
                "last_time": "2026-11-01T10:00:00+00:00",
                "last_timestamp": 1000,
            },
        ],
        source="export",
    )

    res = temp_archive.query_conversations(channel="teams")
    assert res[0]["unread"] is None


def test_message_newest_version_wins(temp_archive: Archive) -> None:
    """A newer Message version overwrites the old one; an older version does not overwrite the current state."""
    msg_id = "msg_001"
    conv_id = "conv_test"

    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": conv_id,
            "id": msg_id,
            "time": "2026-11-01T10:00:00+00:00",
            "timestamp": 1000,
            "sender_id": "user1",
            "sender": "Alice",
            "is_self": False,
            "type": "text",
            "text": "original message",
            "version": 1.0,
        }],
        source="cache",
    )

    # Edited version (version=2.0)
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": conv_id,
            "id": msg_id,
            "time": "2026-11-01T10:00:00+00:00",
            "timestamp": 1000,
            "sender_id": "user1",
            "sender": "Alice",
            "is_self": False,
            "type": "text",
            "text": "edited message",
            "version": 2.0,
            "edited": True,
        }],
        source="cache",
    )

    msgs, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id)
    assert len(msgs) == 1
    assert msgs[0]["text"] == "edited message"
    assert msgs[0]["edited"] is True

    # Older version (version=0.5) does not overwrite
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": conv_id,
            "id": msg_id,
            "time": "2026-11-01T10:00:00+00:00",
            "timestamp": 1000,
            "sender_id": "user1",
            "sender": "Alice",
            "is_self": False,
            "type": "text",
            "text": "old draft text",
            "version": 0.5,
        }],
        source="export",
    )

    msgs, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id)
    assert len(msgs) == 1
    assert msgs[0]["text"] == "edited message"


def test_batch_duplicate_keeps_highest_version_not_last(temp_archive: Archive) -> None:
    """Duplicate (channel, conversation_id, id) within one batch: keep the highest version, not the last row."""

    def _msg(text: str, version: float) -> dict:
        return {
            "conversation_id": "conv_dup",
            "id": "msg_dup",
            "time": "2026-11-01T10:00:00+00:00",
            "timestamp": 1000,
            "sender_id": "user1",
            "sender": "Alice",
            "is_self": False,
            "type": "text",
            "text": text,
            "version": version,
        }

    temp_archive.ingest_messages(
        channel="teams",
        messages=[_msg("version two", 2.0), _msg("version one", 1.0)],
        source="cache",
    )

    msgs, _ = temp_archive.query_messages(channel="teams", conversation_id="conv_dup")
    assert len(msgs) == 1
    assert msgs[0]["text"] == "version two"


def test_same_source_reread_same_version_updates_without_conflict(temp_archive: Archive) -> None:
    """Re-reading the same version from the same source (re-normalization) updates directly; only the same version with different content from different sources counts as a conflict."""

    def _msg(mid: str, text: str) -> dict:
        return {
            "conversation_id": "conv_rn",
            "id": mid,
            "time": "2026-11-01T10:00:00+00:00",
            "timestamp": 1000,
            "sender_id": "user1",
            "sender": "Alice",
            "is_self": False,
            "type": "text",
            "text": text,
            "version": 5.0,
        }

    temp_archive.ingest_messages("teams", [_msg("a", "old render"), _msg("b", "cache text")], source="cache")
    failures: list[str] = []
    temp_archive.ingest_messages("teams", [_msg("a", "new render")], source="cache", failures=failures)
    assert failures == []
    msgs = {m["id"]: m for m in temp_archive.query_messages(channel="teams", conversation_id="conv_rn")[0]}
    assert msgs["a"]["text"] == "new render"
    assert temp_archive.get_cache_message("teams", "conv_rn", "a")["text"] == "new render"

    failures = []
    temp_archive.ingest_messages("teams", [_msg("b", "export text")], source="export", failures=failures)
    assert len(failures) == 1, failures
    msgs = {m["id"]: m for m in temp_archive.query_messages(channel="teams", conversation_id="conv_rn")[0]}
    assert msgs["b"]["text"] == "cache text"


@pytest.mark.parametrize("source_version,target_version,expected", [(2, 1, "source"), (1, 2, "target")])
def test_merge_keeps_newest_cache_copy(temp_archive: Archive, source_version: int, target_version: int, expected: str) -> None:
    for cid, version, text in (("old", source_version, "source"), ("new", target_version, "target")):
        temp_archive.ingest_conversations("teams", [{"id": cid, "name": cid}], source="cache")
        temp_archive.ingest_messages("teams", [{"conversation_id": cid, "id": "m", "version": version, "text": text}], source="cache")
    temp_archive.merge_conversation_into("teams", "old", "new")
    assert temp_archive.get_cache_message("teams", "new", "m")["text"] == expected


def test_merge_moves_cache_copy_when_target_message_absent(temp_archive: Archive) -> None:
    temp_archive.ingest_conversations("teams", [{"id": "old"}, {"id": "new"}], source="cache")
    temp_archive.ingest_messages("teams", [{"conversation_id": "old", "id": "m", "version": 2, "text": "source"}], source="cache")
    temp_archive.merge_conversation_into("teams", "old", "new")
    assert temp_archive.get_cache_message("teams", "new", "m")["text"] == "source"
    assert temp_archive.get_cache_message("teams", "old", "m") is None


def test_merge_equal_version_keeps_target_cache_copy(temp_archive: Archive) -> None:
    for cid, content in (("old", "source"), ("new", "target")):
        temp_archive.ingest_conversations("teams", [{"id": cid}], source="cache")
        temp_archive.ingest_messages("teams", [{"conversation_id": cid, "id": "m", "version": 2, "text": content}], source="cache")
    temp_archive.merge_conversation_into("teams", "old", "new")
    assert temp_archive.get_cache_message("teams", "new", "m")["text"] == "target"
    assert temp_archive.get_cache_message("teams", "old", "m") is None


def test_deleted_message_not_resurrected_by_older_version(temp_archive: Archive) -> None:
    """A message already marked deleted cannot be resurrected by older non-deleted data arriving later."""
    msg_id = "msg_del_01"
    conv_id = "conv_del"

    # Import the deleted version first (version=5.0)
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": conv_id,
            "id": msg_id,
            "time": "2026-11-01T10:00:00+00:00",
            "timestamp": 1000,
            "sender_id": "user1",
            "sender": "Alice",
            "is_self": False,
            "type": "text",
            "text": "",
            "version": 5.0,
            "deleted": True,
        }],
        source="cache",
    )

    # Later, import the non-deleted version from an old export (version=2.0)
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": conv_id,
            "id": msg_id,
            "time": "2026-11-01T10:00:00+00:00",
            "timestamp": 1000,
            "sender_id": "user1",
            "sender": "Alice",
            "is_self": False,
            "type": "text",
            "text": "resurrect me",
            "version": 2.0,
            "deleted": False,
        }],
        source="export",
    )

    msgs, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id)
    assert len(msgs) == 1
    assert msgs[0]["deleted"] is True
    assert msgs[0]["text"] == ""


def test_message_absent_from_later_ingest_remains(temp_archive: Archive) -> None:
    """Messages later purged from the cache are not removed from the archive."""
    conv_id = "conv_persist"
    temp_archive.ingest_messages(
        channel="teams",
        messages=[
            {
                "conversation_id": conv_id,
                "id": "m1",
                "timestamp": 100,
                "text": "msg 1",
            },
            {
                "conversation_id": conv_id,
                "id": "m2",
                "timestamp": 200,
                "text": "msg 2",
            },
        ],
        source="cache",
    )

    # Later only m3 is read
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": conv_id,
            "id": "m3",
            "timestamp": 300,
            "text": "msg 3",
        }],
        source="cache",
    )

    msgs, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id)
    assert len(msgs) == 3
    assert [m["id"] for m in msgs] == ["m1", "m2", "m3"]


def test_sources_merged(temp_archive: Archive) -> None:
    """When a message has been seen by multiple sources, sources records the complete source list."""
    conv_id = "conv_src"
    msg_id = "m_multi_src"

    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": conv_id,
            "id": msg_id,
            "timestamp": 1000,
            "text": "hello",
            "version": 1.0,
        }],
        source="cache",
    )

    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": conv_id,
            "id": msg_id,
            "timestamp": 1000,
            "text": "hello",
            "version": 1.0,
        }],
        source="export",
    )

    msgs, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id)
    assert len(msgs) == 1
    assert "cache" in msgs[0]["sources"]
    assert "export" in msgs[0]["sources"]


def test_archive_path_configurable_via_settings(tmp_path: Path, use_settings) -> None:
    """The Archive path can be set by the settings file, so tests can use a temporary path."""
    custom_db = tmp_path / "settings_custom" / "custom_archive.db"
    use_settings(archive_path=custom_db)

    assert get_default_archive_path() == custom_db

    arc = Archive()
    assert arc.db_path == custom_db
    assert custom_db.exists()


def test_last_cache_version_retained(temp_archive: Archive) -> None:
    """Besides the merged latest content, the archive also keeps the version the cache last presented, for reconciliation."""
    conv_id = "conv_reconcile"
    msg_id = "msg_rec_01"

    # 1. The cache imports version 1.0 first
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": conv_id,
            "id": msg_id,
            "time": "2026-11-01T10:00:00+00:00",
            "timestamp": 1000.0,
            "sender_id": "user1",
            "sender": "Alice",
            "is_self": False,
            "type": "text",
            "text": "content in cache",
            "version": 1.0,
            "deleted": False,
            "edited": False,
        }],
        source="cache",
    )

    # 2. Later the export imports the newer version 2.0
    temp_archive.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": conv_id,
            "id": msg_id,
            "time": "2026-11-01T10:05:00+00:00",
            "timestamp": 1000.0,
            "sender_id": "user1",
            "sender": "Alice",
            "is_self": False,
            "type": "text",
            "text": "updated in export",
            "version": 2.0,
            "deleted": False,
            "edited": True,
        }],
        source="export",
    )

    # The main query presents the latest version (from export)
    current_msgs, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id)
    assert len(current_msgs) == 1
    assert current_msgs[0]["text"] == "updated in export"
    assert current_msgs[0]["version"] == 2.0
    assert set(current_msgs[0]["sources"]) == {"cache", "export"}

    # The cache's original version is kept (keeps what the cache last saw)
    cache_msg = temp_archive.get_cache_message(channel="teams", conversation_id=conv_id, message_id=msg_id)
    assert cache_msg is not None
    assert cache_msg["text"] == "content in cache"
    assert cache_msg["version"] == 1.0

    cache_msgs_query, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id, source="cache")
    assert len(cache_msgs_query) == 1
    assert cache_msgs_query[0]["text"] == "content in cache"
    assert cache_msgs_query[0]["version"] == 1.0


def test_messages_same_timestamp_ordered_by_id_paging(temp_archive: Archive) -> None:
    """Messages with the same timestamp are ordered by id, guaranteeing no duplicates or gaps across pages."""
    conv_id = "conv_paging"
    # All 5 messages carry the same timestamp
    same_ts = 1780000000.0
    raw_msgs = [
        {"conversation_id": conv_id, "id": f"msg_{i:02d}", "timestamp": same_ts, "text": f"text {i}"}
        for i in [3, 1, 5, 2, 4]  # deliberately supplied out of order
    ]
    temp_archive.ingest_messages(channel="teams", messages=raw_msgs, source="cache")

    # Paged query: 2 per page
    page1, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id, limit=2, offset=0)
    page2, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id, limit=2, offset=2)
    page3, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id, limit=2, offset=4)

    p1_ids = [m["id"] for m in page1]
    p2_ids = [m["id"] for m in page2]
    p3_ids = [m["id"] for m in page3]

    assert p1_ids == ["msg_04", "msg_05"]
    assert p2_ids == ["msg_02", "msg_03"]
    assert p3_ids == ["msg_01"]

    # No duplicates and no gaps across pages
    all_seen = p3_ids + p2_ids + p1_ids
    assert all_seen == ["msg_01", "msg_02", "msg_03", "msg_04", "msg_05"]
    assert len(set(all_seen)) == 5


def test_messages_pagination_newest_first_direction(temp_archive: Archive) -> None:
    """Seam B: --limit 3 returns the latest 3 (ordered oldest to newest), --limit 3 --offset 3 returns the 3 before them,

    and messages with the same timestamp at a page boundary are neither duplicated nor missed (Carried-over fix 1).
    """
    conv_id = "conv_pagination_direction"
    # 6 messages, including a same-timestamp-across-pages scenario:
    # msg_01: ts=1000.0
    # msg_02: ts=2000.0
    # msg_03: ts=2000.0  <- page boundary
    # msg_04: ts=2000.0  <- page boundary
    # msg_05: ts=2000.0
    # msg_06: ts=3000.0
    raw_msgs = [
        {"conversation_id": conv_id, "id": "msg_01", "timestamp": 1000.0, "text": "t1"},
        {"conversation_id": conv_id, "id": "msg_02", "timestamp": 2000.0, "text": "t2"},
        {"conversation_id": conv_id, "id": "msg_03", "timestamp": 2000.0, "text": "t3"},
        {"conversation_id": conv_id, "id": "msg_04", "timestamp": 2000.0, "text": "t4"},
        {"conversation_id": conv_id, "id": "msg_05", "timestamp": 2000.0, "text": "t5"},
        {"conversation_id": conv_id, "id": "msg_06", "timestamp": 3000.0, "text": "t6"},
    ]
    temp_archive.ingest_messages(channel="teams", messages=raw_msgs, source="cache")

    # Page 1: the latest 3 (offset=0)
    page1, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id, limit=3, offset=0)
    # Page 2: the 3 before the latest 3 (offset=3)
    page2, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id, limit=3, offset=3)

    p1_ids = [m["id"] for m in page1]
    p2_ids = [m["id"] for m in page2]

    # The latest 3, oldest to newest, should be: msg_04, msg_05, msg_06
    assert p1_ids == ["msg_04", "msg_05", "msg_06"]
    # The 3 before them, oldest to newest, should be: msg_01, msg_02, msg_03
    assert p2_ids == ["msg_01", "msg_02", "msg_03"]

    # Neither duplicated nor missed across pages
    all_ids = p2_ids + p1_ids
    assert all_ids == ["msg_01", "msg_02", "msg_03", "msg_04", "msg_05", "msg_06"]
    assert len(set(p1_ids) & set(p2_ids)) == 0



def test_messages_time_bounds(temp_archive: Archive) -> None:
    """--start and --end time boundary filtering."""
    conv_id = "conv_bounds"
    msgs = [
        {"conversation_id": conv_id, "id": "m1", "timestamp": 1000.0, "time": "2026-11-01T10:00:00+00:00", "text": "t1"},
        {"conversation_id": conv_id, "id": "m2", "timestamp": 2000.0, "time": "2026-11-01T11:00:00+00:00", "text": "t2"},
        {"conversation_id": conv_id, "id": "m3", "timestamp": 3000.0, "time": "2026-11-01T12:00:00+00:00", "text": "t3"},
        {"conversation_id": conv_id, "id": "m4", "timestamp": 4000.0, "time": "2026-11-01T13:00:00+00:00", "text": "t4"},
    ]
    temp_archive.ingest_messages(channel="teams", messages=msgs, source="cache")

    # Numeric timestamp range
    res_middle, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id, start=2000.0, end=3000.0)
    assert [m["id"] for m in res_middle] == ["m2", "m3"]

    # start only
    res_start, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id, start=2500.0)
    assert [m["id"] for m in res_start] == ["m3", "m4"]

    # end only
    res_end, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id, end=2500.0)
    assert [m["id"] for m in res_end] == ["m1", "m2"]

    # start > end must raise ValueError
    with pytest.raises(ValueError, match="start must not be later than end"):
        temp_archive.query_messages(channel="teams", conversation_id=conv_id, start=3000.0, end=2000.0)

    # An invalid time format must raise ValueError
    with pytest.raises(ValueError, match="Invalid time format"):
        temp_archive.query_messages(channel="teams", conversation_id=conv_id, start="not-a-date")

    with pytest.raises(ValueError, match="Invalid time format"):
        temp_archive.query_messages(channel="teams", conversation_id=conv_id, end="invalid-time")


def test_messages_date_only_end_bound(temp_archive: Archive) -> None:
    """Test that a date boundary (YYYY-MM-DD) used as end includes all messages of that day, including milliseconds."""
    conv_id = "conv_subsecond"
    # 2026-11-01 23:59:59.500 on that day
    dt_end_subsec = datetime(2026, 11, 1, 23, 59, 59, 500000).astimezone()
    ts_subsec = dt_end_subsec.timestamp()
    msgs = [
        {"conversation_id": conv_id, "id": "m_day", "timestamp": ts_subsec, "time": dt_end_subsec.isoformat(), "text": "day msg"},
        {"conversation_id": conv_id, "id": "m_next_day", "timestamp": ts_subsec + 1.0, "time": "2026-11-02T00:00:00+00:00", "text": "next day"},
    ]
    temp_archive.ingest_messages(channel="teams", messages=msgs, source="cache")

    res, _ = temp_archive.query_messages(channel="teams", conversation_id=conv_id, end="2026-11-01")
    assert [m["id"] for m in res] == ["m_day"]


def test_archive_get_and_find_conversations(temp_archive: Archive) -> None:
    """Test Archive.get_conversation and Archive.find_conversations_by_name."""
    convs = [
        {"id": "19:conv1@thread.v2", "name": "Project A", "is_group": True, "unread": False},
        {"id": "19:conv2@thread.v2", "name": "Project A", "is_group": False, "unread": True},
        {"id": "19:conv3@thread.v2", "name": "Project B", "is_group": False, "unread": None},
    ]
    temp_archive.ingest_conversations(channel="teams", conversations=convs, source="cache")

    # get_conversation by id
    c1 = temp_archive.get_conversation("teams", "19:conv1@thread.v2")
    assert c1 is not None
    assert c1["name"] == "Project A"
    assert c1["is_group"] is True
    assert c1["unread"] is False

    assert temp_archive.get_conversation("teams", "nonexistent") is None

    # find_conversations_by_name
    matches_a = temp_archive.find_conversations_by_name("teams", "Project A")
    assert len(matches_a) == 2
    assert {c["id"] for c in matches_a} == {"19:conv1@thread.v2", "19:conv2@thread.v2"}

    matches_none = temp_archive.find_conversations_by_name("teams", "Nonexistent Name")
    assert matches_none == []


def test_conversations_not_in_latest_cache_retain_history_with_null_unread(temp_archive: Archive) -> None:
    """Seam B: A conversation no longer in the cache but present in the archive is still listed, with unread null, keeping last_seen_in_cache and the last message time."""
    c_retained_id = "19:conv_retained@thread.v2"
    c_pruned_id = "19:conv_pruned@thread.v2"

    # Run 1: the cache contains two conversations
    temp_archive.ingest_conversations(
        channel="teams",
        conversations=[
            {
                "id": c_retained_id,
                "name": "Retained Group",
                "is_group": True,
                "unread": False,
                "last_time": "2026-11-10T10:00:00+00:00",
                "last_timestamp": 1794268800,
                "version": 1.0,
            },
            {
                "id": c_pruned_id,
                "name": "Pruned Group",
                "is_group": True,
                "unread": True,
                "last_time": "2026-11-09T08:00:00+00:00",
                "last_timestamp": 1794175200,
                "version": 1.0,
            },
        ],
        source="cache",
    )

    convs_r1 = {c["id"]: c for c in temp_archive.query_conversations("teams")}
    assert convs_r1[c_retained_id]["unread"] is False
    assert convs_r1[c_pruned_id]["unread"] is True
    pruned_seen_r1 = convs_r1[c_pruned_id]["last_seen_in_cache"]
    assert pruned_seen_r1 is not None

    # Run 2: the cache rolls over; c_pruned is no longer in the cache (only c_retained)
    temp_archive.ingest_conversations(
        channel="teams",
        conversations=[
            {
                "id": c_retained_id,
                "name": "Retained Group",
                "is_group": True,
                "unread": True,
                "last_time": "2026-11-11T12:00:00+00:00",
                "last_timestamp": 1794362400,
                "version": 2.0,
            },
        ],
        source="cache",
    )

    convs_r2 = {c["id"]: c for c in temp_archive.query_conversations("teams")}
    assert len(convs_r2) == 2, "conversations missing from the cache should still be kept in the archive list"

    # c_retained: updated to the latest unread state and time
    assert convs_r2[c_retained_id]["unread"] is True
    assert convs_r2[c_retained_id]["last_time"] == "2026-11-11T12:00:00+00:00"

    # c_pruned: unread must be null (unknown, a stale value must not be kept); last_time and last_seen_in_cache fully retained
    pruned_conv = convs_r2[c_pruned_id]
    assert pruned_conv["unread"] is None, f"conversation is no longer in the cache, unread must be null: {pruned_conv['unread']}"
    assert pruned_conv["last_time"] == "2026-11-09T08:00:00+00:00"
    assert pruned_conv["last_seen_in_cache"] == pruned_seen_r1


def test_conversations_populated_from_messages_have_null_unread(temp_archive: Archive) -> None:
    """Seam B: If a conversation has only archived messages (such as added by an import or after a cache rollover), it automatically appears in conversations with unread null."""
    c_export_id = "19:conv_export_only@thread.v2"
    temp_archive.ingest_messages(
        channel="teams",
        messages=[
            {
                "conversation_id": c_export_id,
                "id": "msg_exp_1",
                "time": "2025-06-01T10:00:00+00:00",
                "timestamp": 1748772000.0,
                "sender_id": "user1",
                "sender": "Alice",
                "text": "Hello from export",
            }
        ],
        source="export",
    )

    convs = {c["id"]: c for c in temp_archive.query_conversations("teams")}
    assert c_export_id in convs
    exp_conv = convs[c_export_id]
    assert exp_conv["unread"] is None
    assert exp_conv["last_time"] == "2025-06-01T10:00:00+00:00"
    assert exp_conv["last_seen_in_cache"] is None


def test_archive_schema_migration_last_seen_in_cache(tmp_path: Path) -> None:
    """Verify the legacy-schema migration: existing data is preserved losslessly and last_seen_in_cache and sync_state are added automatically."""
    import sqlite3

    db_path = tmp_path / "legacy_archive.db"
    # Create the legacy schema (without last_seen_in_cache and sync_state)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE conversations (
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
            CREATE TABLE messages (
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
                sources TEXT,
                first_seen TEXT,
                last_seen TEXT,
                PRIMARY KEY (channel, conversation_id, id)
            )
        """)
        # Write 2 legacy conversations
        conn.execute("""
            INSERT INTO conversations VALUES
            ('teams', 'c1', 'Conv 1', 0, 1, '2026-11-01', 1000, 1.0, '2026-11-01T00:00:00+00:00', '["cache"]'),
            ('teams', 'c2', 'Conv 2', 1, 0, '2026-11-02', 2000, 1.0, '2026-11-02T00:00:00+00:00', '["export"]')
        """)
        conn.commit()

    # Opening with Archive triggers the automatic migration
    arc = Archive(db_path=db_path)

    # Verify zero rows lost
    convs = {c["id"]: c for c in arc.query_conversations("teams")}
    assert len(convs) == 2
    assert convs["c1"]["last_seen_in_cache"] == "2026-11-01T00:00:00+00:00"
    assert convs["c2"]["last_seen_in_cache"] is None


def test_archive_sync_state_and_data_current_up_to(temp_archive: Archive) -> None:
    """Test sync-state recording and the latest-data-time query."""
    # Initially no data at all
    assert temp_archive.get_data_current_up_to("teams") is None

    temp_archive.ingest_conversations("teams", [{"id": "export-only", "name": "test"}], source="export")
    assert temp_archive.get_data_current_up_to("teams") is None

    # Record a successful sync
    sync_ts = "2026-11-11T12:00:00+08:00"
    temp_archive.record_sync("teams", source="cache", status="ok", sync_time=sync_ts)
    assert temp_archive.get_data_current_up_to("teams") == sync_ts


def test_search_messages_covers_text_links_files_quoted_and_marks_quote_hit(temp_archive: Archive) -> None:
    """Seam B: search covers readable text, link targets, attachment file names and quoted content, and a hit inside a quote is flagged hit_in_quote."""
    conv_id = "conv_search_coverage"
    msgs = [
        {
            "conversation_id": conv_id,
            "id": "msg_txt",
            "timestamp": 1000.0,
            "time": "2026-11-10T10:00:00+08:00",
            "text": "Normal message with keyword apple and nothing else",
            "quoted": None,
            "files": [],
        },
        {
            "conversation_id": conv_id,
            "id": "msg_link",
            "timestamp": 2000.0,
            "time": "2026-11-10T10:01:00+08:00",
            "text": "See [documentation](https://example.com/banana_guide)",
            "quoted": None,
            "files": [],
        },
        {
            "conversation_id": conv_id,
            "id": "msg_files",
            "timestamp": 3000.0,
            "time": "2026-11-10T10:02:00+08:00",
            "text": "Sending documents today",
            "quoted": None,
            "files": ["cherry_report.pdf"],
        },
        {
            "conversation_id": conv_id,
            "id": "msg_quote",
            "timestamp": 4000.0,
            "time": "2026-11-10T10:03:00+08:00",
            "text": "> [Alice Smith, 2026-11-10]: original date proposal\n\nThanks for date",
            "quoted": {
                "author": "Alice Smith",
                "time": "2026-11-10T09:00:00+08:00",
                "text": "original date proposal",
            },
            "files": [],
        },
        {
            "conversation_id": conv_id,
            "id": "msg_del",
            "timestamp": 5000.0,
            "time": "2026-11-10T10:04:00+08:00",
            "text": "",
            "deleted": True,
            "quoted": None,
            "files": [],
        },
        {
            "conversation_id": conv_id,
            "id": "msg_wildcard",
            "timestamp": 6000.0,
            "time": "2026-11-10T10:05:00+08:00",
            "text": "Special 100% discount with path_test_item",
            "quoted": None,
            "files": [],
        },
    ]
    temp_archive.ingest_messages(channel="teams", messages=msgs, source="cache")

    # 1. Search hits plain text
    r_txt, _ = temp_archive.search_messages(channel="teams", keyword="apple")
    assert len(r_txt) == 1
    assert r_txt[0]["id"] == "msg_txt"
    assert r_txt[0]["hit_in_quote"] is False

    # 2. Search hits a link target (URL)
    r_link, _ = temp_archive.search_messages(channel="teams", keyword="banana_guide")
    assert len(r_link) == 1
    assert r_link[0]["id"] == "msg_link"
    assert r_link[0]["hit_in_quote"] is False

    # 3. Search hits an attachment file name
    r_files, _ = temp_archive.search_messages(channel="teams", keyword="cherry_report")
    assert len(r_files) == 1
    assert r_files[0]["id"] == "msg_files"
    assert r_files[0]["hit_in_quote"] is False

    # 4. Search hits quoted content (hit_in_quote flag is True)
    r_q_text, _ = temp_archive.search_messages(channel="teams", keyword="proposal")
    assert len(r_q_text) == 1
    assert r_q_text[0]["id"] == "msg_quote"
    assert r_q_text[0]["hit_in_quote"] is True

    # Search hits the quote author (hit_in_quote flag is True)
    r_q_author, _ = temp_archive.search_messages(channel="teams", keyword="Alice Smith")
    assert len(r_q_author) == 1
    assert r_q_author[0]["id"] == "msg_quote"
    assert r_q_author[0]["hit_in_quote"] is True

    # Search for a word that appears only in the reply body of a message that carries a quote (hit_in_quote flag is False)
    r_body, _ = temp_archive.search_messages(channel="teams", keyword="Thanks")
    assert len(r_body) == 1
    assert r_body[0]["id"] == "msg_quote"
    assert r_body[0]["hit_in_quote"] is False

    # 5. Deleted messages cannot be found by search
    r_del, _ = temp_archive.search_messages(channel="teams", keyword="msg_del")
    assert len(r_del) == 0

    # 6. Wildcards (% and _) are matched literally, not expanded
    r_pct, _ = temp_archive.search_messages(channel="teams", keyword="100%")
    assert len(r_pct) == 1
    assert r_pct[0]["id"] == "msg_wildcard"

    r_under, _ = temp_archive.search_messages(channel="teams", keyword="path_test")
    assert len(r_under) == 1
    assert r_under[0]["id"] == "msg_wildcard"

    # 7. Verify that JSON structure key names (such as "author") produce no false-positive hits
    r_key, _ = temp_archive.search_messages(channel="teams", keyword="author")
    assert len(r_key) == 0


def test_search_messages_conversation_filter_and_time_bounds(temp_archive: Archive) -> None:
    """Seam B: search supports filtering by a single conversation and a time range limit."""
    c1, c2 = "conv_search_1", "conv_search_2"
    msgs = [
        {"conversation_id": c1, "id": "m1", "timestamp": 1000.0, "time": "2026-11-10T10:00:00+08:00", "text": "target message"},
        {"conversation_id": c1, "id": "m2", "timestamp": 3000.0, "time": "2026-11-10T10:30:00+08:00", "text": "target message"},
        {"conversation_id": c2, "id": "m3", "timestamp": 2000.0, "time": "2026-11-10T10:15:00+08:00", "text": "target message"},
    ]
    temp_archive.ingest_messages(channel="teams", messages=msgs, source="cache")

    # No conversation specified: across all conversations
    all_res, _ = temp_archive.search_messages(channel="teams", keyword="target")
    assert len(all_res) == 3
    # Sorted oldest to newest by time
    assert [m["id"] for m in all_res] == ["m1", "m3", "m2"]

    # Specify conversation_id=c1
    c1_res, _ = temp_archive.search_messages(channel="teams", keyword="target", conversation_id=c1)
    assert [m["id"] for m in c1_res] == ["m1", "m2"]

    # Time range filter
    time_res, _ = temp_archive.search_messages(
        channel="teams", keyword="target", start=1500.0, end=2500.0
    )
    assert len(time_res) == 1
    assert time_res[0]["id"] == "m3"


def test_search_and_recent_across_multiple_ingests(temp_archive: Archive) -> None:
    """Seam B: search and recent correctly cover all records accumulated across multiple ingests."""
    conv_id = "conv_multi_ingest"
    # Ingest 1: old records
    ts1 = 1793508000.0  # 2026-11-01 10:00:00
    ts2 = 1793511600.0  # 2026-11-01 11:00:00
    ingest1 = [
        {"conversation_id": conv_id, "id": "m_old_1", "timestamp": ts1, "time": "2026-11-01T10:00:00+08:00", "text": "quote invoice"},
        {"conversation_id": conv_id, "id": "m_old_2", "timestamp": ts2, "time": "2026-11-01T11:00:00+08:00", "text": "quote agreement"},
    ]
    temp_archive.ingest_messages(channel="teams", messages=ingest1, source="cache")

    # Ingest 2: new records
    ts3 = 1794285600.0  # 2026-11-10 10:00:00
    ts4 = 1794289200.0  # 2026-11-10 11:00:00
    ingest2 = [
        {"conversation_id": conv_id, "id": "m_new_1", "timestamp": ts3, "time": "2026-11-10T10:00:00+08:00", "text": "quote shipment"},
        {"conversation_id": conv_id, "id": "m_new_2", "timestamp": ts4, "time": "2026-11-10T11:00:00+08:00", "text": "quote delivery"},
    ]
    temp_archive.ingest_messages(channel="teams", messages=ingest2, source="cache")

    # search across both ingests
    results, _ = temp_archive.search_messages(channel="teams", keyword="quote")
    assert len(results) == 4
    assert [m["id"] for m in results] == ["m_old_1", "m_old_2", "m_new_1", "m_new_2"]

    # recent across both ingests (with 10:30, between ts1 and ts2, as the boundary)
    recent_msgs, truncated, excluded_count = temp_archive.query_recent_messages(channel="teams", since=ts1 + 1800.0)
    assert truncated is False
    assert excluded_count == 0
    assert [m["id"] for m in recent_msgs] == ["m_old_2", "m_new_1", "m_new_2"]


def test_recent_messages_inclusive_boundary_and_original_send_time(temp_archive: Archive) -> None:
    """Seam B: the recent --since boundary second is inclusive; judged by original send time, later edited or deleted ones before the boundary do not appear."""
    conv_id = "conv_recent_rules"
    boundary_ts = 1794285600.0  # 2026-11-10 10:00:00+08:00

    msgs = [
        # 1. Sent exactly at the boundary second
        {
            "conversation_id": conv_id,
            "id": "m_exact_boundary",
            "timestamp": boundary_ts,
            "time": "2026-11-10T10:00:00+08:00",
            "text": "Exact boundary message",
        },
        # 2. Sent after the boundary
        {
            "conversation_id": conv_id,
            "id": "m_after_boundary",
            "timestamp": boundary_ts + 60.0,
            "time": "2026-11-10T10:01:00+08:00",
            "text": "After boundary message",
        },
        # 3. Sent before the boundary (09:00:00) but edited after it (version/last_seen later than since)
        {
            "conversation_id": conv_id,
            "id": "m_edited_later",
            "timestamp": boundary_ts - 3600.0,
            "time": "2026-11-10T09:00:00+08:00",
            "text": "Edited later content",
            "version": boundary_ts + 7200.0,
            "edited": True,
        },
        # 4. Sent before the boundary (09:30:00) but deleted after it
        {
            "conversation_id": conv_id,
            "id": "m_deleted_later",
            "timestamp": boundary_ts - 1800.0,
            "time": "2026-11-10T09:30:00+08:00",
            "text": "",
            "version": boundary_ts + 7200.0,
            "deleted": True,
        },
        # 5. Sent after the boundary (10:30:00) and deleted afterwards -> should appear and be marked deleted
        {
            "conversation_id": conv_id,
            "id": "m_sent_after_and_deleted",
            "timestamp": boundary_ts + 1800.0,
            "time": "2026-11-10T10:30:00+08:00",
            "text": "",
            "deleted": True,
        },
    ]
    temp_archive.ingest_messages(channel="teams", messages=msgs, source="cache")

    recent_msgs, truncated, excluded_count = temp_archive.query_recent_messages(channel="teams", since=boundary_ts)
    assert truncated is False
    assert excluded_count == 0

    r_ids = [m["id"] for m in recent_msgs]
    # m_exact_boundary must be included (boundary second inclusive)
    assert "m_exact_boundary" in r_ids
    assert "m_after_boundary" in r_ids
    assert "m_sent_after_and_deleted" in r_ids

    # Ones sent before the boundary but edited or deleted later: the original send time is before since, so they must never appear
    assert "m_edited_later" not in r_ids
    assert "m_deleted_later" not in r_ids

    # Sent after the boundary and deleted: marked as deleted
    del_msg = next(m for m in recent_msgs if m["id"] == "m_sent_after_and_deleted")
    assert del_msg["deleted"] is True


def test_recent_messages_truncation(temp_archive: Archive) -> None:
    """Seam B: recent truncation: when over limit, truncated is True and the latest `limit` messages are kept (oldest to newest)."""
    conv_id = "conv_truncation"
    msgs = [
        {"conversation_id": conv_id, "id": f"m_{i}", "timestamp": float(i * 1000), "time": f"2026-11-10T10:0{i}:00+08:00", "text": f"text {i}"}
        for i in range(1, 6)
    ]
    temp_archive.ingest_messages(channel="teams", messages=msgs, source="cache")

    # 1. limit=3 < total 5 -> truncated=True, keep the latest 3 (m_3, m_4, m_5) oldest to newest
    res_trunc, is_trunc, excluded_trunc = temp_archive.query_recent_messages(channel="teams", since=0.0, limit=3)
    assert is_trunc is True
    assert excluded_trunc == 0
    assert len(res_trunc) == 3
    assert [m["id"] for m in res_trunc] == ["m_3", "m_4", "m_5"]

    # 2. limit=10 >= total 5 -> truncated=False, keep all 5 oldest to newest
    res_all, is_all_trunc, excluded_all = temp_archive.query_recent_messages(channel="teams", since=0.0, limit=10)
    assert is_all_trunc is False
    assert excluded_all == 0
    assert len(res_all) == 5
    assert [m["id"] for m in res_all] == ["m_1", "m_2", "m_3", "m_4", "m_5"]





def test_empty_successful_cache_snapshot_clears_unread(temp_archive: Archive) -> None:
    """A successful cache snapshot with zero conversations: unread of existing archived conversations must be cleared to null, leaving no stale value."""
    temp_archive.ingest_conversations(
        channel="teams",
        conversations=[{"id": "c1", "name": "C1", "is_group": False, "unread": True,
                        "last_time": "2026-11-10T10:00:00+08:00", "last_timestamp": 1.0, "version": 1.0}],
        source="cache",
    )
    assert temp_archive.query_conversations("teams")[0]["unread"] is True
    temp_archive.ingest_conversations(channel="teams", conversations=[], source="cache")
    assert temp_archive.query_conversations("teams")[0]["unread"] is None


def test_query_messages_truncation_with_offset(temp_archive: Archive) -> None:
    """Seam B: query_messages returns (messages, truncated) respecting limit and offset."""
    conv_id = "conv_msg_trunc"
    msgs = [
        {"conversation_id": conv_id, "id": f"m_{i}", "timestamp": float(i * 1000), "text": f"msg {i}"}
        for i in range(1, 6)
    ]
    temp_archive.ingest_messages(channel="teams", messages=msgs, source="cache")

    # 1. limit=3 < total 5 -> truncated=True, newest 3 oldest-to-newest
    res, trunc = temp_archive.query_messages(channel="teams", conversation_id=conv_id, limit=3, offset=0)
    assert trunc is True
    assert [m["id"] for m in res] == ["m_3", "m_4", "m_5"]

    # 2. limit=5 == total 5 -> truncated=False
    res_eq, trunc_eq = temp_archive.query_messages(channel="teams", conversation_id=conv_id, limit=5, offset=0)
    assert trunc_eq is False
    assert [m["id"] for m in res_eq] == ["m_1", "m_2", "m_3", "m_4", "m_5"]

    # 3. offset=2, limit=2 -> 1 older row left -> truncated=True
    res_off, trunc_off = temp_archive.query_messages(channel="teams", conversation_id=conv_id, limit=2, offset=2)
    assert trunc_off is True
    assert [m["id"] for m in res_off] == ["m_2", "m_3"]

    # 4. offset=3, limit=2 -> exact end -> truncated=False
    res_end, trunc_end = temp_archive.query_messages(channel="teams", conversation_id=conv_id, limit=2, offset=3)
    assert trunc_end is False
    assert [m["id"] for m in res_end] == ["m_1", "m_2"]


def test_search_messages_multi_keyword_enrichment_and_truncation(temp_archive: Archive) -> None:
    """Seam B: search_messages supports multi-keyword OR, deduplication, conversation enrichment, and truncated."""
    temp_archive.ingest_conversations(
        channel="teams",
        conversations=[
            {"id": "c_group", "name": "Ops Group", "is_group": True, "unread": False,
             "last_time": "2026-11-10T10:00:00+08:00", "last_timestamp": 5000.0, "version": 1.0},
            {"id": "c_dm", "name": "Bob Example", "is_group": False, "unread": False,
             "last_time": "2026-11-10T10:00:00+08:00", "last_timestamp": 5000.0, "version": 1.0},
        ],
        source="cache",
    )
    msgs = [
        {"conversation_id": "c_group", "id": "m1", "timestamp": 1000.0, "text": "alpha only"},
        {"conversation_id": "c_dm", "id": "m2", "timestamp": 2000.0, "text": "beta only"},
        {"conversation_id": "c_group", "id": "m3", "timestamp": 3000.0, "text": "alpha and beta together"},
        {"conversation_id": "c_missing", "id": "m4", "timestamp": 4000.0, "text": "alpha in missing conv",
         "quoted": {"author": "Carol", "text": "beta in quote"}},
    ]
    temp_archive.ingest_messages(channel="teams", messages=msgs, source="cache")

    # 1. Multi-keyword OR with duplicate input keyword, limit=3 < 4 -> truncated=True
    hits, trunc = temp_archive.search_messages(channel="teams", keyword=["beta", "alpha", "beta"], limit=3)
    assert trunc is True
    assert [m["id"] for m in hits] == ["m2", "m3", "m4"]
    assert hits[0]["conversation_name"] == "Bob Example"
    assert hits[0]["is_group"] is False
    assert hits[0]["matched_keywords"] == ["beta"]
    assert hits[0]["hit_in_quote"] is False

    assert hits[1]["conversation_name"] == "Ops Group"
    assert hits[1]["is_group"] is True
    assert hits[1]["matched_keywords"] == ["beta", "alpha"]
    assert hits[1]["hit_in_quote"] is False

    # Missing conversation falls back to conversation_id and is_group=False; hit_in_quote is True if any keyword hits quote
    assert hits[2]["conversation_name"] == "c_missing"
    assert hits[2]["is_group"] is False
    assert hits[2]["matched_keywords"] == ["beta", "alpha"]
    assert hits[2]["hit_in_quote"] is True

    # 2. Exact equality limit=4 == 4 -> truncated=False
    all_hits, trunc_all = temp_archive.search_messages(channel="teams", keyword=["alpha", "beta"], limit=4)
    assert trunc_all is False
    assert [m["id"] for m in all_hits] == ["m1", "m2", "m3", "m4"]

