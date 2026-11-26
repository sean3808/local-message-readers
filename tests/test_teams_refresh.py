"""Teams cache refresh (ingest_cache) tests: every refresh imports messages; a missing replychains means the refresh failed.

A fake WrappedIndexDB replaces the real snapshot; only a temporary archive and a temporary profile directory are used.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from readers.archive import Archive
from readers.teams.snapshot import ingest_cache

CONV_ID = "19:refresh_conv@thread.v2"


def _record(key: str, seq: int, value: dict) -> MagicMock:
    rec = MagicMock()
    rec.key.value = key
    rec.ldb_seq_no = seq
    rec.value = value
    return rec


def _store_db(store_name: str, records: list[MagicMock]) -> MagicMock:
    store = MagicMock()
    store.iterate_records.return_value = records
    db = MagicMock()
    db.get_object_store_by_name.return_value = store
    db.__contains__.side_effect = lambda name: name == store_name
    return db


def _msg(mid: str | None, content: str, **extra: object) -> dict:
    m: dict = {
        "originalArrivalTime": 1794268800000,
        "creator": "8:alice",
        "imDisplayName": "Alice",
        "messageType": "RichText/Html",
        "content": content,
        **extra,
    }
    if mid is not None:
        m["id"] = mid
    return m


def _fake_wrapped(with_replychains: bool, message_map: dict | None = None) -> MagicMock:
    conv_rec = _record(CONV_ID, 10, {
        "id": CONV_ID,
        "threadProperties": {"topic": "Refresh Group"},
        "lastMessageTimeUtc": 1794268800000,
    })
    chain_rec = _record(CONV_ID, 11, {
        "conversationId": CONV_ID,
        "messageMap": message_map
        if message_map is not None
        else {"m1": _msg("m1", "<p>hello from cache</p>")},
    })
    dbs = {"conversation-manager": _store_db("conversations", [conv_rec])}
    if with_replychains:
        dbs["replychain-manager"] = _store_db("replychains", [chain_rec])

    ids = []
    by_id = {}
    for marker, db in dbs.items():
        db_id = MagicMock()
        db_id.name = f"https_teams.live.com_0:{marker}:"
        ids.append(db_id)
        by_id[id(db_id)] = db

    fake = MagicMock()
    fake.database_ids = ids
    fake.__getitem__.side_effect = lambda db_id: by_id[id(db_id)]
    wrapped = MagicMock()
    wrapped.return_value.__enter__.return_value = fake
    return wrapped


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "profile"
    (profile / "IndexedDB" / "https_teams.live.com_0.indexeddb.leveldb").mkdir(parents=True)
    return profile


def test_refresh_ingests_messages(tmp_path: Path) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    with patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(with_replychains=True)):
        outcome = ingest_cache(arc, _profile(tmp_path))

    assert outcome.success, outcome
    msgs, _ = arc.query_messages(channel="teams", conversation_id=CONV_ID)
    assert [m["id"] for m in msgs] == ["m1"]
    assert arc.get_data_current_up_to("teams") is not None


def test_missing_replychains_fails_refresh_but_keeps_conversations(tmp_path: Path) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    arc.record_sync("teams", source="cache", status="ok", sync_time="2026-01-01T00:00:00+08:00")
    with patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(with_replychains=False)):
        outcome = ingest_cache(arc, _profile(tmp_path))

    assert not outcome.success
    assert outcome.error and "replychain" in outcome.error
    assert any("replychain" in f for f in outcome.failures)
    # Already-imported conversations are kept, and the sync state does not advance
    assert [c["id"] for c in arc.query_conversations("teams", limit=10)] == [CONV_ID]
    assert arc.get_data_current_up_to("teams") == "2026-01-01T00:00:00+08:00"


def test_cache_conflict_reported_in_failures(tmp_path: Path) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    mm = {
        "a": _msg("m1", "<p>first text</p>", version=5),
        "b": _msg("m1", "<p>other text</p>", version=5),
    }
    with patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(True, mm)):
        outcome = ingest_cache(arc, _profile(tmp_path))

    assert outcome.success, outcome
    assert len(outcome.failures) == 1, outcome.failures
    assert "m1" in outcome.failures[0]


def test_cache_record_without_id_is_skipped_and_reported(tmp_path: Path) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    mm = {
        "x": _msg(None, "<p>no id one</p>"),
        "y": _msg("", "<p>no id two</p>"),
        "m1": _msg("m1", "<p>kept</p>"),
    }
    with patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(True, mm)):
        outcome = ingest_cache(arc, _profile(tmp_path))

    assert [m["id"] for m in arc.query_messages(channel="teams", conversation_id=CONV_ID)[0]] == ["m1"]
    missing = [f for f in outcome.failures if CONV_ID in f and ("id='x'" in f or "id='y'" in f)]
    assert len(missing) == 2, outcome.failures


def test_cache_non_dict_message_entry_is_reported(tmp_path: Path) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    mm = {"bad": "not-a-dict", "m1": _msg("m1", "<p>kept</p>")}
    with patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(True, mm)):
        outcome = ingest_cache(arc, _profile(tmp_path))

    assert outcome.success
    assert [m["id"] for m in arc.query_messages(channel="teams", conversation_id=CONV_ID)[0]] == ["m1"]
    bad = [f for f in outcome.failures if CONV_ID in f and "id='bad'" in f]
    assert len(bad) == 1, outcome.failures
    assert "not-a-dict" not in bad[0]


def test_quoted_preview_keeps_link_target_and_is_searchable(tmp_path: Path) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    quote = (
        '<blockquote itemtype="http://schema.skype.com/Reply"><strong itemprop="mri">Bob</strong>'
        '<p itemprop="preview">see <a href="https://example.com/x">label</a></p></blockquote>'
        "<p>reply body</p>"
    )
    mm = {"m1": _msg("m1", quote)}
    with patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(True, mm)):
        ingest_cache(arc, _profile(tmp_path))

    msgs, _ = arc.query_messages(channel="teams", conversation_id=CONV_ID)
    assert "https://example.com/x" in msgs[0]["quoted"]["text"]
    hits, _ = arc.search_messages("teams", "example.com/x")
    assert [h["id"] for h in hits] == ["m1"]
    assert hits[0]["hit_in_quote"] is True


def test_cache_undecodable_files_property_is_kept_and_reported(tmp_path: Path) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    mm = {"m1": _msg("m1", "<p>kept body</p>", properties={"files": "{bad"})}
    with patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(True, mm)):
        outcome = ingest_cache(arc, _profile(tmp_path))

    assert outcome.success, outcome
    msgs, _ = arc.query_messages(channel="teams", conversation_id=CONV_ID)
    assert [m["id"] for m in msgs] == ["m1"]
    assert msgs[0]["files"] == []
    hits = [f for f in outcome.failures if "files" in f and "id='m1'" in f]
    assert len(hits) == 1, outcome.failures
    assert "{bad" not in hits[0]


def test_cache_files_valid_json_but_not_list_is_reported(tmp_path: Path) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    mm = {"m1": _msg("m1", "<p>kept body</p>", properties={"files": '{"fileName": "a.pdf"}'})}
    with patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(True, mm)):
        outcome = ingest_cache(arc, _profile(tmp_path))

    msgs, _ = arc.query_messages(channel="teams", conversation_id=CONV_ID)
    assert [m["id"] for m in msgs] == ["m1"]
    assert msgs[0]["files"] == []
    hits = [f for f in outcome.failures if "files" in f and "id='m1'" in f]
    assert len(hits) == 1, outcome.failures


def test_cache_bad_properties_and_missing_time_are_reported(tmp_path: Path) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    bad_props = _msg("m1", "<p>kept</p>")
    bad_props["properties"] = "not-a-dict"
    no_time = _msg("m2", "<p>kept too</p>")
    no_time.pop("originalArrivalTime", None)
    no_time.pop("clientArrivalTime", None)
    no_time.pop("sequenceId", None)
    with patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(True, {"m1": bad_props, "m2": no_time})):
        outcome = ingest_cache(arc, _profile(tmp_path))

    assert sorted(m["id"] for m in arc.query_messages(channel="teams", conversation_id=CONV_ID)[0]) == ["m1", "m2"]
    assert len([f for f in outcome.failures if "properties" in f and "id='m1'" in f]) == 1, outcome.failures
    assert len([f for f in outcome.failures if "time" in f and "id='m2'" in f]) == 1, outcome.failures


def test_cache_well_formed_record_reports_nothing(tmp_path: Path) -> None:
    arc = Archive(db_path=tmp_path / "arc.db")
    mm = {"m1": _msg("m1", "<p>fine</p>", properties={"files": '[{"fileName": "a.txt"}]'})}
    with patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(True, mm)):
        outcome = ingest_cache(arc, _profile(tmp_path))

    assert outcome.success, outcome
    assert outcome.failures == []


def test_normalizer_degraded_channel_empty_for_well_formed_record() -> None:
    from readers.teams.normalizer import normalize_teams_message

    degraded: list[str] = []
    normalize_teams_message(_msg("m1", "<p>ok</p>"), CONV_ID, 1.0, degraded)
    assert degraded == []
