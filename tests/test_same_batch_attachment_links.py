import pytest

from readers.archive import Archive


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("source", ["cache", "export"])
def test_same_batch_same_version_merges_attachment_links(tmp_path, reverse, source):
    archive = Archive(tmp_path / "archive.db")
    base = {"id": "1", "conversation_id": "c", "version": 100,
            "text": "first", "files": ["report.xlsx"]}
    missing = {**base, "attachments": [{"name": "report.xlsx", "url": None}]}
    linked = {**base, "attachments": [{"name": "report.xlsx", "url": "https://1drv.ms/link"}]}
    rows = [linked, missing] if reverse else [missing, linked]
    failures, stats = [], {}
    archive.ingest_messages("teams", rows, source=source, failures=failures, stats=stats)
    message = archive.query_messages("teams", "c")[0]
    assert message["attachments"] == linked["attachments"]
    assert message["version"] == 100
    if source == "cache":
        assert archive.get_cache_message("teams", "c", "1")["attachments"] == linked["attachments"]
    assert failures == []
    assert stats.get("unorderable_conflicts", 0) == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_same_batch_newest_version_wins(tmp_path, reverse):
    archive = Archive(tmp_path / "archive.db")
    old = {"id": "1", "conversation_id": "c", "version": 100, "text": "old",
           "files": ["old"], "attachments": [{"name": "old", "url": "old-url"}]}
    new = {**old, "version": 200, "text": "new", "files": ["new"],
           "attachments": [{"name": "new", "url": "new-url"}]}
    failures, stats = [], {}
    archive.ingest_messages("teams", [new, old] if reverse else [old, new], failures=failures, stats=stats)
    for message in (archive.query_messages("teams", "c")[0], archive.get_cache_message("teams", "c", "1")):
        for field in ("version", "text", "files", "attachments"):
            assert message[field] == new[field]
    assert failures == []
    assert stats.get("unorderable_conflicts", 0) == 0


def test_same_batch_different_files_keep_first_and_record_conflict(tmp_path):
    archive = Archive(tmp_path / "archive.db")
    first = {"id": "1", "conversation_id": "c", "version": 100, "files": ["a"],
             "attachments": [{"name": "a", "url": None}]}
    second = {**first, "files": ["b"], "attachments": [{"name": "b", "url": "b-url"}]}
    failures, stats = [], {}
    archive.ingest_messages("teams", [first, second], failures=failures, stats=stats)
    for message in (archive.query_messages("teams", "c")[0], archive.get_cache_message("teams", "c", "1")):
        assert message["files"] == first["files"]
        assert message["attachments"] == first["attachments"]
    assert len(failures) == 1
    assert "Version conflict (cannot be ordered)" in failures[0]
    assert stats.get("unorderable_conflicts", 0) == 1
