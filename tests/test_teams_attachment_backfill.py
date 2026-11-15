import sqlite3

import pytest

from readers.archive import Archive


@pytest.mark.parametrize("same_files", [True, False])
def test_older_cache_backfills_only_matching_files(tmp_path, same_files):
    path = tmp_path / "archive.db"
    archive = Archive(path)
    saved = {"id": "1", "conversation_id": "c", "version": 200,
             "text": "saved", "files": ["report.xlsx"], "attachments": []}
    archive.ingest_messages("teams", [saved], source="cache")
    with sqlite3.connect(path) as conn:
        for table in ("messages", "cache_messages"):
            conn.execute(f"UPDATE {table} SET attachments = NULL")

    before = archive.query_messages("teams", "c")[0]
    cache_before = archive.get_cache_message("teams", "c", "1")
    incoming_name = "report.xlsx" if same_files else "different.xlsx"
    incoming = {"id": "1", "conversation_id": "c", "version": 100,
                "text": "older", "files": [incoming_name],
                "attachments": [{"name": incoming_name, "url": "https://1drv.ms/link"}]}
    failures = []
    stats = {}
    archive.ingest_messages("teams", [incoming], source="cache", failures=failures, stats=stats)

    expected = incoming["attachments"] if same_files else []
    after = archive.query_messages("teams", "c")[0]
    cache_after = archive.get_cache_message("teams", "c", "1")
    assert after == {**before, "attachments": expected}
    assert cache_after == {**cache_before, "attachments": expected}
    assert failures == []
    assert stats.get("unorderable_conflicts", 0) == 0


def test_same_version_changed_files_do_not_backfill_old_attachment(tmp_path):
    archive = Archive(tmp_path / "archive.db")
    base = {"id": "1", "conversation_id": "c", "version": 200}
    old_attachment = [{"name": "old", "url": "old-url"}]
    archive.ingest_messages("teams", [{**base, "files": ["old"], "attachments": old_attachment}], source="cache")
    archive.ingest_messages("teams", [{**base, "files": ["new"], "attachments": [{"name": "new", "url": "new-url"}]}], source="cache")
    assert archive.query_messages("teams", "c")[0]["attachments"] == old_attachment
    assert archive.get_cache_message("teams", "c", "1")["attachments"] == old_attachment


def test_newer_version_without_url_keeps_saved_link(tmp_path):
    archive = Archive(tmp_path / "archive.db")
    saved = [{"name": "x", "url": "saved-url"}]
    archive.ingest_messages("teams", [{"id": "1", "conversation_id": "c", "version": 100,
                                       "files": ["x"], "attachments": saved}], source="cache")
    archive.ingest_messages("teams", [{"id": "1", "conversation_id": "c", "version": 200, "text": "edited",
                                       "files": ["x"], "attachments": [{"name": "x", "url": None}]}], source="cache")
    after = archive.query_messages("teams", "c")[0]
    assert after["text"] == "edited"
    assert after["attachments"] == saved
    assert archive.get_cache_message("teams", "c", "1")["attachments"] == saved


def test_alias_merge_with_different_files_keeps_target_attachments(tmp_path):
    archive = Archive(tmp_path / "archive.db")
    target = [{"name": "a", "url": "a-url"}]
    archive.ingest_messages("teams", [{"id": "1", "conversation_id": "new", "version": 100,
                                       "files": ["a"], "attachments": target}], source="cache")
    archive.ingest_messages("teams", [{"id": "1", "conversation_id": "old", "version": 100,
                                       "files": ["b"], "attachments": [{"name": "b", "url": "b-url"}]}], source="cache")
    archive.merge_conversation_into("teams", "old", "new")
    assert (archive.query_messages("teams", "new")[0]["files"],
            archive.query_messages("teams", "new")[0]["attachments"]) == (["b"], [{"name": "b", "url": "b-url"}])
    assert archive.get_cache_message("teams", "new", "1")["attachments"] == target
