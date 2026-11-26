"""teams-reader CLI tests for multi-keyword search, hit enrichment, and truncated reporting."""

from __future__ import annotations

from pathlib import Path

from readers.archive import Archive
from tests.test_teams_resilience import run_cli


def _seed_archive(arc_path: Path) -> None:
    arc = Archive(db_path=arc_path)
    arc.ingest_conversations(
        channel="teams",
        conversations=[
            {
                "id": "19:grp@thread.v2",
                "name": "Logistics Team",
                "is_group": True,
                "unread": False,
                "last_time": "2026-11-10T10:05:00+08:00",
                "last_timestamp": 5000.0,
                "version": 100.0,
            },
            {
                "id": "8:alice",
                "name": "Alice Example",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-10T10:04:00+08:00",
                "last_timestamp": 4000.0,
                "version": 100.0,
            },
        ],
        source="cache",
    )
    arc.ingest_messages(
        channel="teams",
        messages=[
            {
                "conversation_id": "19:grp@thread.v2",
                "id": "m1",
                "timestamp": 1000.0,
                "time": "2026-11-10T10:01:00+08:00",
                "text": "Need bill of lading",
            },
            {
                "conversation_id": "8:alice",
                "id": "m2",
                "timestamp": 2000.0,
                "time": "2026-11-10T10:02:00+08:00",
                "text": "Telex release approved",
            },
            {
                "conversation_id": "19:grp@thread.v2",
                "id": "m3",
                "timestamp": 3000.0,
                "time": "2026-11-10T10:03:00+08:00",
                "text": "Bill of lading and telex release attached",
            },
            {
                "conversation_id": "8:alice",
                "id": "m4",
                "timestamp": 4000.0,
                "time": "2026-11-10T10:04:00+08:00",
                "text": "Another telex release note",
                "quoted": {"author": "Bob", "text": "bill of lading draft"},
            },
        ],
        source="cache",
    )


def test_teams_search_multi_keyword_or_and_enrichment(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_archive(arc)

    code, out = run_cli(
        "search",
        "telex release",
        "bill of lading",
        "telex release",
        "--limit",
        "3",
        "--no-refresh",
        archive_path=arc,
    )
    assert code == 0
    assert out["keywords"] == ["telex release", "bill of lading"]
    assert "keyword" not in out
    assert out["truncated"] is True
    assert [m["id"] for m in out["messages"]] == ["m2", "m3", "m4"]

    assert out["messages"][0]["conversation_name"] == "Alice Example"
    assert out["messages"][0]["is_group"] is False
    assert out["messages"][0]["matched_keywords"] == ["telex release"]
    assert out["messages"][0]["hit_in_quote"] is False

    assert out["messages"][1]["conversation_name"] == "Logistics Team"
    assert out["messages"][1]["is_group"] is True
    assert out["messages"][1]["matched_keywords"] == ["telex release", "bill of lading"]
    assert out["messages"][1]["hit_in_quote"] is False

    assert out["messages"][2]["matched_keywords"] == ["telex release", "bill of lading"]
    assert out["messages"][2]["hit_in_quote"] is True


def test_teams_search_single_keyword_backward_compat_and_exact_limit(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_archive(arc)

    code, out = run_cli("search", "telex release", "--limit", "3", "--no-refresh", archive_path=arc)
    assert code == 0
    assert out["keyword"] == "telex release"
    assert out["keywords"] == ["telex release"]
    assert out["truncated"] is False
    assert [m["id"] for m in out["messages"]] == ["m2", "m3", "m4"]


def test_teams_search_rejects_blank_keywords(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_archive(arc)

    for blank in ("", "   "):
        code, out = run_cli("search", blank, "--no-refresh", archive_path=arc)
        assert code != 0
        assert set(out) == {"error"}


def test_teams_messages_reports_truncated(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_archive(arc)

    code, out_trunc = run_cli("messages", "Logistics Team", "--limit", "1", "--no-refresh", archive_path=arc)
    assert code == 0
    assert out_trunc["truncated"] is True
    assert [m["id"] for m in out_trunc["messages"]] == ["m3"]

    code, out_eq = run_cli("messages", "Logistics Team", "--limit", "2", "--no-refresh", archive_path=arc)
    assert code == 0
    assert out_eq["truncated"] is False
    assert [m["id"] for m in out_eq["messages"]] == ["m1", "m3"]

    code, out_off = run_cli("messages", "Logistics Team", "--limit", "1", "--offset", "1", "--no-refresh", archive_path=arc)
    assert code == 0
    assert out_off["truncated"] is False
    assert [m["id"] for m in out_off["messages"]] == ["m1"]
