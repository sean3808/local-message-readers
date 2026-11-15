import hashlib
import sqlite3

import pytest

from readers.archive import Archive
from readers.teams.cli import _parser, _run
from readers.teams.system_text import migrate_system_text

OLD_TO_NEW = [
    ('變更群組名稱為「Example Team」', 'Changed the group name to "Example Team"'),
    ("變更群組名稱", "Changed the group name"),
    ("新增成員", "Added a member"),
    ("成員離開", "A member left"),
    ("更新群組加入權限", "Updated the group join permission"),
    ("語音/視訊通話", "Voice/video call"),
    ("系統事件", "System event"),
]


def _seed(path):
    archive = Archive(path)
    messages = [
        {"conversation_id": "c", "id": str(i), "text": old, "type": "system", "version": 900}
        for i, (old, _) in enumerate(OLD_TO_NEW)
    ]
    messages.append({"conversation_id": "c", "id": "text1", "text": "新增成員", "type": "text", "version": 900})
    messages.append({"conversation_id": "c", "id": "sys-new", "text": "Added a member", "type": "system", "version": 900})
    messages.append({"conversation_id": "c", "id": "sys-other", "text": "新增成員了嗎", "type": "system", "version": 900})
    archive.ingest_messages("teams", messages, source="cache")
    return archive


def _texts(path, table="messages"):
    with sqlite3.connect(path) as conn:
        return dict(conn.execute(f"SELECT id, text FROM {table}").fetchall())


def _args(path, *extra):
    return _parser().parse_args(["migrate-system-text", "--archive-path", str(path), *extra])


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setattr("readers.teams.cli.backup_status", lambda _: {"stale": False})


def test_is_preview_by_default():
    assert _parser().parse_args(["migrate-system-text"]).write is False


def test_preview_lists_ids_and_changes_nothing(tmp_path, use_settings, guarded):
    path = tmp_path / "archive.db"
    use_settings(archive_path=path)
    _seed(path)
    digest = hashlib.sha256(path.read_bytes()).digest()
    result = _run(_args(path))
    assert hashlib.sha256(path.read_bytes()).digest() == digest
    assert result["candidates"] == 7
    assert result["by_mapping"] == {"group_name": 1, "group_name_unset": 1, "member_added": 1, "member_left": 1,
                                    "join_permission": 1, "call": 1, "generic": 1}
    assert sorted(m["id"] for m in result["message_ids"]) == [str(i) for i in range(7)]
    assert "updated" not in result


def test_write_rewrites_each_mapping_and_mirror_then_is_idempotent(tmp_path, use_settings, guarded):
    path = tmp_path / "archive.db"
    use_settings(archive_path=path)
    _seed(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE cache_messages SET text = 'newer' WHERE id = '2'")
        before = conn.execute("SELECT id, time, timestamp, version, sources, type FROM messages ORDER BY id").fetchall()
    assert _run(_args(path, "--write"))["updated"] == 7
    for table in ("messages", "cache_messages"):
        texts = _texts(path, table)
        for i, (_, new) in enumerate(OLD_TO_NEW):
            if table == "cache_messages" and i == 2:
                assert texts["2"] == "newer"  # mirror with different text is left alone
            else:
                assert texts[str(i)] == new
        assert texts["text1"] == "新增成員"
        assert texts["sys-new"] == "Added a member"
        assert texts["sys-other"] == "新增成員了嗎"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT id, time, timestamp, version, sources, type FROM messages ORDER BY id").fetchall() == before
    second = _run(_args(path, "--write"))
    assert second["candidates"] == 0 and second["updated"] == 0


def test_stale_backup_refuses_write(tmp_path, monkeypatch, use_settings):
    path = tmp_path / "archive.db"
    use_settings(archive_path=path)
    _seed(path)
    monkeypatch.setattr("readers.teams.cli.backup_status", lambda _: {"stale": True})
    with pytest.raises(RuntimeError, match="backup"):
        _run(_args(path, "--write"))
    assert _texts(path)["2"] == "新增成員"


def test_write_rejects_non_default_archive_but_preview_allowed(tmp_path, use_settings, guarded):
    default_path = tmp_path / "default.db"
    other_path = tmp_path / "other.db"
    use_settings(archive_path=default_path)
    _seed(other_path)
    assert _run(_args(other_path))["candidates"] == 7
    with pytest.raises(RuntimeError, match="default archive"):
        _run(_args(other_path, "--write"))
    assert _texts(other_path)["2"] == "新增成員"


def test_failed_write_rolls_back(tmp_path):
    path = tmp_path / "archive.db"
    archive = _seed(path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TRIGGER block BEFORE UPDATE ON messages WHEN OLD.id = '3' BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(sqlite3.DatabaseError):
        migrate_system_text(archive, write=True)
    assert _texts(path)["2"] == "新增成員"
