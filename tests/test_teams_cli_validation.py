"""teams-reader pagination argument validation: reuses the validate_pagination rules of wechat-reader."""

from __future__ import annotations

from pathlib import Path

import pytest

from readers.archive import Archive
from tests.test_teams_resilience import run_cli


def _seed(arc_path: Path, n: int) -> None:
    arc = Archive(db_path=arc_path)
    arc.ingest_conversations(
        channel="teams",
        conversations=[
            {
                "id": f"19:c{i}@thread.v2",
                "name": f"Conv {i}",
                "is_group": True,
                "unread": False,
                "last_time": "2026-11-10T10:00:00+08:00",
                "last_timestamp": 1794285600.0 + i,
                "version": 100.0,
            }
            for i in range(n)
        ],
        source="cache",
    )


@pytest.mark.parametrize(
    "argv",
    [
        ("conversations", "--limit", "0"),
        ("conversations", "--limit", "-1"),
        ("messages", "Conv 0", "--limit", "0"),
        ("messages", "Conv 0", "--offset", "-1"),
        ("search", "kw", "--limit", "0"),
        ("recent", "--since", "2026-01-01", "--limit", "0"),
    ],
)
def test_invalid_pagination_rejected(tmp_path: Path, argv: tuple[str, ...]) -> None:
    arc = tmp_path / "a.db"
    _seed(arc, 1)
    code, out = run_cli(*argv, "--no-refresh", archive_path=arc)
    assert code != 0
    assert set(out) == {"error"}
    assert "limit must be greater than 0" in out["error"] or "offset must not be less than 0" in out["error"]


def test_conversations_default_limit_matches_wechat(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed(arc, 25)
    code, out = run_cli("conversations", "--no-refresh", archive_path=arc)
    assert code == 0
    assert len(out["conversations"]) == 20
