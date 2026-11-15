from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.test_teams_resilience import _prefill_archive, run_cli


def test_missing_backup_log_is_stale(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    code, out = run_cli("conversations", "--no-refresh", archive_path=archive)
    assert code == 0
    assert out["backup"] == {"last_success": None, "stale": True}


@pytest.mark.parametrize("days,stale", [(7, False), (9, True)])
def test_success_followed_by_failure_and_bad_line(tmp_path: Path, days: int, stale: bool) -> None:
    archive = tmp_path / "archive.db"
    finished = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    entries = [json.dumps({"backup": {"ok": True}, "finished": finished}),
               "bad json", json.dumps({"backup": {"ok": False}, "finished": datetime.now(UTC).isoformat()})]
    (tmp_path / "backup-log.jsonl").write_text("\n".join(entries), encoding="utf-8")
    code, out = run_cli("conversations", "--no-refresh", archive_path=archive)
    assert code == 0
    assert out["backup"] == {"last_success": finished, "stale": stale}


@pytest.mark.parametrize("args", [
    ("conversations",), ("messages", "19:archived_group@thread.v2"),
    ("search", "word"), ("recent", "--since", "2026-01-01"),
])
@pytest.mark.parametrize("skip_refresh", [True, False])
def test_queries_include_backup(tmp_path: Path, args: tuple[str, ...], skip_refresh: bool) -> None:
    archive = tmp_path / "archive.db"
    _prefill_archive(archive)
    options = ("--no-refresh",) if skip_refresh else ()
    code, out = run_cli(*args, *options, archive_path=archive, profile_path=tmp_path / "missing-profile")
    assert code == 0
    assert out["backup"] == {"last_success": None, "stale": True}


def test_undecodable_line_is_skipped_without_hiding_other_successes(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    finished = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    good = json.dumps({"backup": {"ok": True}, "finished": finished}).encode("utf-8")
    (tmp_path / "backup-log.jsonl").write_bytes(good + b"\n\xff\xfe broken\n")
    code, out = run_cli("conversations", "--no-refresh", archive_path=archive)
    assert code == 0
    assert out["backup"] == {"last_success": finished, "stale": False}
    assert "Cannot read the backup log" not in out["failures"]


def test_success_line_with_undecodable_byte_is_not_counted(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    older = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    newer = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    good = json.dumps({"backup": {"ok": True}, "finished": older}).encode("utf-8")
    damaged = (
        b'{"backup": {"ok": true, "path": "backup-\xff.7z"}, "finished": "' + newer.encode("utf-8") + b'"}'
    )
    (tmp_path / "backup-log.jsonl").write_bytes(good + b"\n" + damaged + b"\n")
    code, out = run_cli("conversations", "--no-refresh", archive_path=archive)
    assert code == 0
    assert out["backup"] == {"last_success": older, "stale": True}


def test_unreadable_log_reported_in_failures(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    (tmp_path / "backup-log.jsonl").mkdir()
    code, out = run_cli("conversations", "--no-refresh", archive_path=archive)
    assert code == 0
    assert out["backup"] == {"last_success": None, "stale": True}
    assert "Cannot read the backup log" in out["failures"]
