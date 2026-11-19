"""Tests for resolving ambiguous Teams conversation names (issue #2)."""

from __future__ import annotations

from pathlib import Path

from readers.archive import Archive
from tests.test_teams_resilience import run_cli


def _seed_convs(arc_path: Path, convs: list[dict], messages: list[dict] | None = None) -> None:
    arc = Archive(db_path=arc_path)
    arc.ingest_conversations(channel="teams", conversations=convs, source="cache")
    if messages:
        arc.ingest_messages(channel="teams", messages=messages, source="cache")


def test_messages_resolves_multiple_non_group_to_latest(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_convs(
        arc,
        [
            {
                "id": "19:example-old@thread.v2",
                "name": "Alice Example",
                "is_group": False,
                "unread": False,
                "last_time": "2026-07-14T11:01:40+08:00",
                "last_timestamp": 1784007700.0,
                "version": 100.0,
            },
            {
                "id": "19:example-new@thread.v2",
                "name": "Alice Example",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-19T13:22:12+08:00",
                "last_timestamp": 1795065732.0,
                "version": 200.0,
            },
        ],
        messages=[
            {
                "id": "m-old",
                "conversation_id": "19:example-old@thread.v2",
                "time": "2026-07-14T11:01:40+08:00",
                "timestamp": 1784007700.0,
                "sender_id": "8:alice",
                "sender": "Alice Example",
                "is_self": False,
                "type": "text",
                "text": "old chat",
                "version": 100.0,
            },
            {
                "id": "m-new",
                "conversation_id": "19:example-new@thread.v2",
                "time": "2026-11-19T13:22:12+08:00",
                "timestamp": 1795065732.0,
                "sender_id": "8:alice",
                "sender": "Alice Example",
                "is_self": False,
                "type": "text",
                "text": "new chat",
                "version": 200.0,
            },
        ],
    )

    code, out = run_cli("messages", "Alice Example", "--no-refresh", archive_path=arc)
    assert code == 0, out
    assert out["conversation"]["id"] == "19:example-new@thread.v2"
    assert [m["text"] for m in out["messages"]] == ["new chat"]
    assert out["alternatives"] == [
        {"id": "19:example-old@thread.v2", "last_time": "2026-07-14T11:01:40+08:00"}
    ]


def test_messages_group_and_non_group_still_ambiguous(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_convs(
        arc,
        [
            {
                "id": "19:example-1to1@thread.v2",
                "name": "Project Sync",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-19T13:22:12+08:00",
                "last_timestamp": 1795065732.0,
                "version": 200.0,
            },
            {
                "id": "19:example-grp@thread.v2",
                "name": "Project Sync",
                "is_group": True,
                "unread": False,
                "last_time": "2026-07-14T11:01:40+08:00",
                "last_timestamp": 1784007700.0,
                "version": 100.0,
            },
        ],
    )

    code, out = run_cli("messages", "Project Sync", "--no-refresh", archive_path=arc)
    assert code != 0
    assert "matches multiple conversations" in out["error"]
    assert "19:example-1to1@thread.v2" in out["error"]
    assert "19:example-grp@thread.v2" in out["error"]


def test_messages_non_group_equal_last_time_still_ambiguous(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_convs(
        arc,
        [
            {
                "id": "19:example-a@thread.v2",
                "name": "Bob Example",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-19T13:22:12+08:00",
                "last_timestamp": 1795065732.0,
                "version": 100.0,
            },
            {
                "id": "19:example-b@thread.v2",
                "name": "Bob Example",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-19T13:22:12+08:00",
                "last_timestamp": 1795065732.0,
                "version": 100.0,
            },
        ],
    )

    code, out = run_cli("messages", "Bob Example", "--no-refresh", archive_path=arc)
    assert code != 0
    assert "matches multiple conversations" in out["error"]


def test_messages_non_group_both_unknown_last_time_still_ambiguous(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_convs(
        arc,
        [
            {
                "id": "19:example-a@thread.v2",
                "name": "Carol Example",
                "is_group": False,
                "unread": False,
                "last_time": "",
                "last_timestamp": 0.0,
                "version": 100.0,
            },
            {
                "id": "19:example-b@thread.v2",
                "name": "Carol Example",
                "is_group": False,
                "unread": False,
                "last_time": "",
                "last_timestamp": 0.0,
                "version": 100.0,
            },
        ],
    )

    code, out = run_cli("messages", "Carol Example", "--no-refresh", archive_path=arc)
    assert code != 0
    assert "matches multiple conversations" in out["error"]


def test_messages_unknown_last_time_sorts_oldest_and_reports_null(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_convs(
        arc,
        [
            {
                "id": "19:example-unknown@thread.v2",
                "name": "Dave Example",
                "is_group": False,
                "unread": False,
                "last_time": "",
                "last_timestamp": 0.0,
                "version": 100.0,
            },
            {
                "id": "19:example-mid@thread.v2",
                "name": "Dave Example",
                "is_group": False,
                "unread": False,
                "last_time": "2026-08-01T09:00:00+08:00",
                "last_timestamp": 1785546000.0,
                "version": 150.0,
            },
            {
                "id": "19:example-newest@thread.v2",
                "name": "Dave Example",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-19T13:22:12+08:00",
                "last_timestamp": 1795065732.0,
                "version": 200.0,
            },
        ],
    )

    code, out = run_cli("messages", "Dave Example", "--no-refresh", archive_path=arc)
    assert code == 0, out
    assert out["conversation"]["id"] == "19:example-newest@thread.v2"
    assert out["alternatives"] == [
        {"id": "19:example-mid@thread.v2", "last_time": "2026-08-01T09:00:00+08:00"},
        {"id": "19:example-unknown@thread.v2", "last_time": None},
    ]


def test_messages_single_match_and_exact_id_omit_alternatives(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_convs(
        arc,
        [
            {
                "id": "19:example-old@thread.v2",
                "name": "Alice Example",
                "is_group": False,
                "unread": False,
                "last_time": "2026-07-14T11:01:40+08:00",
                "last_timestamp": 1784007700.0,
                "version": 100.0,
            },
            {
                "id": "19:example-new@thread.v2",
                "name": "Alice Example",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-19T13:22:12+08:00",
                "last_timestamp": 1795065732.0,
                "version": 200.0,
            },
            {
                "id": "19:example-unique@thread.v2",
                "name": "Unique Person",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-10T10:00:00+08:00",
                "last_timestamp": 1794285600.0,
                "version": 100.0,
            },
        ],
    )

    code_unique, out_unique = run_cli("messages", "Unique Person", "--no-refresh", archive_path=arc)
    assert code_unique == 0, out_unique
    assert "alternatives" not in out_unique

    code_id, out_id = run_cli("messages", "19:example-old@thread.v2", "--no-refresh", archive_path=arc)
    assert code_id == 0, out_id
    assert out_id["conversation"]["id"] == "19:example-old@thread.v2"
    assert "alternatives" not in out_id


def test_search_with_conversation_resolves_latest_non_group(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_convs(
        arc,
        [
            {
                "id": "19:example-old@thread.v2",
                "name": "Alice Example",
                "is_group": False,
                "unread": False,
                "last_time": "2026-07-14T11:01:40+08:00",
                "last_timestamp": 1784007700.0,
                "version": 100.0,
            },
            {
                "id": "19:example-new@thread.v2",
                "name": "Alice Example",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-19T13:22:12+08:00",
                "last_timestamp": 1795065732.0,
                "version": 200.0,
            },
        ],
        messages=[
            {
                "id": "m-old",
                "conversation_id": "19:example-old@thread.v2",
                "time": "2026-07-14T11:01:40+08:00",
                "timestamp": 1784007700.0,
                "sender_id": "8:alice",
                "sender": "Alice Example",
                "is_self": False,
                "type": "text",
                "text": "needle in old",
                "version": 100.0,
            },
            {
                "id": "m-new",
                "conversation_id": "19:example-new@thread.v2",
                "time": "2026-11-19T13:22:12+08:00",
                "timestamp": 1795065732.0,
                "sender_id": "8:alice",
                "sender": "Alice Example",
                "is_self": False,
                "type": "text",
                "text": "needle in new",
                "version": 200.0,
            },
        ],
    )

    code, out = run_cli("search", "needle", "--conversation", "Alice Example", "--no-refresh", archive_path=arc)
    assert code == 0, out
    assert [m["id"] for m in out["messages"]] == ["m-new"]
    assert out["alternatives"] == [
        {"id": "19:example-old@thread.v2", "last_time": "2026-07-14T11:01:40+08:00"}
    ]
