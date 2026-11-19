"""Tests for teams-reader refresh throttling, --refresh, --no-refresh reason, and settings integration."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import settings_args

from readers.archive import Archive
from readers.teams import cli
from tests.test_teams_refresh import CONV_ID, _fake_wrapped, _profile

TZ = timezone(timedelta(hours=8))


def _run_main(
    *argv: str,
    settings_file: Path,
    archive_path: Path,
    profile_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict]:
    full_argv = [
        "--settings",
        str(settings_file),
        "--archive-path",
        str(archive_path),
        "--profile-dir",
        str(profile_dir),
        *argv,
    ]
    code = cli.main(full_argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


def _seed_sync_and_settings(
    tmp_path: Path, interval: int, sync_time: datetime
) -> tuple[Path, Path, Path]:
    arc_path = tmp_path / "arc.db"
    profile = _profile(tmp_path)
    settings = tmp_path / "settings.toml"
    settings.write_text(f"refresh_min_interval_seconds = {interval}\n", encoding="utf-8")
    arc = Archive(db_path=arc_path)
    arc.record_sync("teams", source="cache", status="ok", sync_time=sync_time.isoformat(timespec="seconds"))
    return arc_path, profile, settings


def test_second_query_within_interval_skips_without_reading_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arc_path = tmp_path / "arc.db"
    profile = _profile(tmp_path)
    settings = tmp_path / "settings.toml"
    settings.write_text("refresh_min_interval_seconds = 120\n", encoding="utf-8")

    t0 = datetime.now(TZ)
    t1 = t0 + timedelta(seconds=60)

    with (
        patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(with_replychains=True)),
        patch("readers.teams.cli._now", return_value=t0),
    ):
        code1, out1 = _run_main(
            "conversations",
            settings_file=settings,
            archive_path=arc_path,
            profile_dir=profile,
            capsys=capsys,
        )

    assert code1 == 0
    assert out1["refresh"]["status"] == "ok"
    assert [c["id"] for c in out1["conversations"]] == [CONV_ID]

    with (
        patch("readers.teams.cli.ingest_cache", side_effect=AssertionError("should not read cache")),
        patch("readers.teams.cli._now", return_value=t1),
    ):
        code2, out2 = _run_main(
            "conversations",
            settings_file=settings,
            archive_path=arc_path,
            profile_dir=profile,
            capsys=capsys,
        )

    assert code2 == 0
    assert out2["refresh"]["status"] == "skipped"
    assert "reason" in out2["refresh"] and out2["refresh"]["reason"]
    assert out2["refresh"]["current_up_to"] == out1["refresh"]["current_up_to"]
    assert [c["id"] for c in out2["conversations"]] == [CONV_ID]


def test_query_after_interval_refreshes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    t0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=TZ)
    arc_path, profile, settings = _seed_sync_and_settings(tmp_path, 120, t0)

    with (
        patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(with_replychains=True)),
        patch("readers.teams.cli._now", return_value=t0 + timedelta(seconds=120)),
    ):
        code, out = _run_main(
            "conversations",
            settings_file=settings,
            archive_path=arc_path,
            profile_dir=profile,
            capsys=capsys,
        )

    assert code == 0
    assert out["refresh"]["status"] == "ok"


def test_zero_interval_always_refreshes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    t0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=TZ)
    arc_path, profile, settings = _seed_sync_and_settings(tmp_path, 0, t0)

    with (
        patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(with_replychains=True)),
        patch("readers.teams.cli._now", return_value=t0 + timedelta(seconds=1)),
    ):
        code, out = _run_main(
            "conversations",
            settings_file=settings,
            archive_path=arc_path,
            profile_dir=profile,
            capsys=capsys,
        )

    assert code == 0
    assert out["refresh"]["status"] == "ok"


@pytest.mark.parametrize("flag_position", ["before", "after"])
def test_refresh_flag_forces_refresh_within_interval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag_position: str
) -> None:
    t0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=TZ)
    arc_path, profile, settings = _seed_sync_and_settings(tmp_path, 120, t0)

    argv = ("--refresh", "conversations") if flag_position == "before" else ("conversations", "--refresh")
    with (
        patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(with_replychains=True)),
        patch("readers.teams.cli._now", return_value=t0 + timedelta(seconds=10)),
    ):
        code, out = _run_main(
            *argv,
            settings_file=settings,
            archive_path=arc_path,
            profile_dir=profile,
            capsys=capsys,
        )

    assert code == 0
    assert out["refresh"]["status"] == "ok"


def test_no_refresh_includes_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arc_path = tmp_path / "arc.db"
    Archive(db_path=arc_path)
    settings = tmp_path / "settings.toml"
    settings.write_text("", encoding="utf-8")

    code, out = _run_main(
        "conversations",
        "--no-refresh",
        settings_file=settings,
        archive_path=arc_path,
        profile_dir=tmp_path / "absent-profile",
        capsys=capsys,
    )

    assert code == 0
    assert out["refresh"]["status"] == "skipped"
    assert "--no-refresh" in out["refresh"]["reason"]


@pytest.mark.parametrize(
    "flags",
    [
        ("--refresh", "--no-refresh", "conversations"),
        ("conversations", "--refresh", "--no-refresh"),
        ("--refresh", "conversations", "--no-refresh"),
        ("--no-refresh", "conversations", "--refresh"),
    ],
)
def test_refresh_and_no_refresh_together_is_usage_error(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "readers.teams", *settings_args(tmp_path), *flags],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode != 0
    assert "--refresh" in proc.stderr and "--no-refresh" in proc.stderr


def test_failed_refresh_does_not_suppress_next_query_within_interval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    t0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=TZ)
    earlier_ok = t0.isoformat(timespec="seconds")
    arc_path, profile, settings = _seed_sync_and_settings(tmp_path, 120, t0)

    # First query forces a refresh that fails (missing replychains)
    with (
        patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(with_replychains=False)),
        patch("readers.teams.cli._now", return_value=t0 + timedelta(seconds=10)),
    ):
        code1, out1 = _run_main(
            "conversations",
            "--refresh",
            settings_file=settings,
            archive_path=arc_path,
            profile_dir=profile,
            capsys=capsys,
        )

    assert code1 == 0
    assert out1["refresh"]["status"] == "failed"
    assert out1["refresh"]["current_up_to"] == earlier_ok

    # Second query is still within 120s of t0, but must refresh because the previous attempt failed
    with (
        patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(with_replychains=True)),
        patch("readers.teams.cli._now", return_value=t0 + timedelta(seconds=20)),
    ):
        code2, out2 = _run_main(
            "conversations",
            settings_file=settings,
            archive_path=arc_path,
            profile_dir=profile,
            capsys=capsys,
        )

    assert code2 == 0
    assert out2["refresh"]["status"] == "ok"


def test_refresh_that_raises_does_not_count_as_recent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exception escaping ingest_cache (e.g. the temp snapshot cannot be created) must not leave the attempt marked ok."""
    t0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=TZ)
    arc_path, profile, settings = _seed_sync_and_settings(tmp_path, 120, t0)
    Archive(db_path=arc_path).record_sync("teams", source="cache_attempt", status="ok")

    with (
        patch("readers.teams.cli.ingest_cache", side_effect=OSError("cannot create temp snapshot")),
        patch("readers.teams.cli._now", return_value=t0 + timedelta(seconds=10)),
    ):
        code1, out1 = _run_main(
            "conversations", "--refresh",
            settings_file=settings, archive_path=arc_path, profile_dir=profile, capsys=capsys,
        )
    assert code1 != 0
    assert "cannot create temp snapshot" in out1["error"]
    assert Archive(db_path=arc_path).get_sync_status("teams", source="cache_attempt") == "failed"


def test_refresh_that_raises_after_concurrent_success_still_records_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A concurrent refresh writing "ok" while this one runs must not hide this run's exception."""
    t0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=TZ)
    arc_path, profile, settings = _seed_sync_and_settings(tmp_path, 120, t0)

    def concurrent_ok_then_raise(archive: Archive, _profile_dir: Path):
        archive.record_sync("teams", source="cache_attempt", status="ok")
        raise OSError("cannot create temp snapshot")

    with (
        patch("readers.teams.cli.ingest_cache", side_effect=concurrent_ok_then_raise),
        patch("readers.teams.cli._now", return_value=t0 + timedelta(seconds=10)),
    ):
        code, _ = _run_main(
            "conversations", "--refresh",
            settings_file=settings, archive_path=arc_path, profile_dir=profile, capsys=capsys,
        )
    assert code != 0
    assert Archive(db_path=arc_path).get_sync_status("teams", source="cache_attempt") == "failed"

    with (
        patch("readers.teams.snapshot.WrappedIndexDB", _fake_wrapped(with_replychains=True)),
        patch("readers.teams.cli._now", return_value=t0 + timedelta(seconds=20)),
    ):
        code2, out2 = _run_main(
            "conversations",
            settings_file=settings, archive_path=arc_path, profile_dir=profile, capsys=capsys,
        )
    assert code2 == 0
    assert out2["refresh"]["status"] == "ok"
