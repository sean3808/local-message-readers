"""Teams reader resilience tests (Issue #14).

Verify that when the Teams cache cannot be read (missing profile, copy failure, decode failure),
the reader still answers queries from the archive and reports the source error and the data-currency time;
verify that a single unparsable record is recorded in failures by locator while the remaining records import normally.
Everything is simulated through a --settings file pointing at a temporary directory; the real Teams folder is never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from conftest import settings_args

from readers.archive import Archive
from readers.teams.snapshot import load_latest_conversation_records


def run_cli(
    *args: str,
    archive_path: Path | None = None,
    profile_path: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run the teams-reader CLI and return the exit code and JSON output."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    assert archive_path is not None, "run_cli needs an archive path to place its isolated settings file"
    # Always pass --settings: archive and profile both point to temporary directories; the real settings.toml is never read
    settings = settings_args(
        archive_path.parent,
        archive_path=archive_path,
        profile_dir=profile_path if profile_path is not None else archive_path.parent / "no-profile",
    )
    cmd = [sys.executable, "-m", "readers.teams", *settings, "--archive-path", str(archive_path)]
    cmd.extend(args)

    p = subprocess.run(
        cmd,
        capture_output=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        data = {"raw_stdout": p.stdout, "raw_stderr": p.stderr}
    return p.returncode, data


def _prefill_archive(arc_path: Path) -> None:
    """Pre-populate the archive database with one conversation and message, as the answer source when the cache is unavailable."""
    arc = Archive(db_path=arc_path)
    arc.ingest_conversations(
        channel="teams",
        conversations=[{
            "id": "19:archived_group@thread.v2",
            "name": "Archived Test Group",
            "is_group": True,
            "unread": False,
            "last_time": "2026-11-10T10:00:00+08:00",
            "last_timestamp": 1794285600.0,
            "version": 100.0,
        }],
        source="cache",
    )
    arc.ingest_messages(
        channel="teams",
        messages=[{
            "conversation_id": "19:archived_group@thread.v2",
            "id": "msg_prefilled_1",
            "time": "2026-11-10T10:00:00+08:00",
            "timestamp": 1794285600.0,
            "sender_id": "user1",
            "sender": "Alice",
            "is_self": False,
            "type": "text",
            "text": "Previously archived message",
        }],
        source="cache",
    )
    # Record an initial sync state
    arc.record_sync("teams", source="cache", status="ok", sync_time="2026-11-10T10:05:00+08:00")


def test_missing_profile_reported_as_source_error_returns_archived_data(tmp_path: Path) -> None:
    """Missing profile: point at a non-existent profile directory; the command is still answered from the archive, returning exit 0, the source error and the data-currency time."""
    arc_path = tmp_path / "missing_prof_arc.db"
    _prefill_archive(arc_path)
    non_existent_profile = tmp_path / "non_existent_teams_profile"

    # 1. conversations command
    code_c, out_c = run_cli("conversations", archive_path=arc_path, profile_path=non_existent_profile)
    assert code_c == 0, f"Non-zero exit code: {code_c}, output={out_c}"
    assert "conversations" in out_c
    assert len(out_c["conversations"]) == 1
    assert out_c["conversations"][0]["id"] == "19:archived_group@thread.v2"

    # Verify refresh and failures contain the source error and the data-currency time
    assert "refresh" in out_c
    assert out_c["refresh"]["status"] == "failed"
    assert "Teams personal profile not found" in out_c["refresh"]["error"]
    assert out_c["refresh"]["current_up_to"] == "2026-11-10T10:05:00+08:00"
    assert any("Teams personal profile not found" in f for f in out_c.get("failures", []))

    # 2. messages command
    code_m, out_m = run_cli("messages", "19:archived_group@thread.v2", archive_path=arc_path, profile_path=non_existent_profile)
    assert code_m == 0, f"Non-zero exit code: {code_m}, output={out_m}"
    assert "messages" in out_m
    assert len(out_m["messages"]) == 1
    assert out_m["messages"][0]["id"] == "msg_prefilled_1"
    assert out_m["refresh"]["status"] == "failed"
    assert "Teams personal profile not found" in out_m["refresh"]["error"]
    assert out_m["refresh"]["current_up_to"] == "2026-11-10T10:05:00+08:00"


def test_export_only_archive_reports_unknown_freshness(tmp_path: Path) -> None:
    arc_path = tmp_path / "export_only.db"
    arc = Archive(db_path=arc_path)
    arc.ingest_conversations("teams", [{"id": "19:test", "name": "test"}], source="export")
    arc.record_sync("teams", source="export", status="ok")
    code, out = run_cli("conversations", archive_path=arc_path, profile_path=tmp_path / "missing")
    assert code == 0
    assert out["refresh"]["current_up_to"] is None
    assert "never been synced successfully" in out["refresh"]["current_up_to_reason"]


def test_copy_failure_reported_returns_archived_data(tmp_path: Path) -> None:
    """Copy failure: simulate a snapshot copy failure (e.g. the LevelDB path is a file rather than a directory so copytree fails), returning exit 0, the error description and archive data."""
    arc_path = tmp_path / "copy_fail_arc.db"
    _prefill_archive(arc_path)

    # Build a fake profile directory whose leveldb source is a regular file (making shutil.copytree raise NotADirectoryError)
    crafted_profile = tmp_path / "crafted_profile_copy_fail"
    idb_dir = crafted_profile / "IndexedDB"
    idb_dir.mkdir(parents=True)
    leveldb_file = idb_dir / "https_teams.live.com_0.indexeddb.leveldb"
    leveldb_file.write_text("not a directory", encoding="utf-8")

    code, out = run_cli("conversations", archive_path=arc_path, profile_path=crafted_profile)
    assert code == 0, f"Non-zero exit code: {code}, output={out}"
    assert len(out["conversations"]) == 1
    assert out["conversations"][0]["id"] == "19:archived_group@thread.v2"

    assert out["refresh"]["status"] == "failed"
    assert "Failed to copy the Teams snapshot" in out["refresh"]["error"]
    assert out["refresh"]["current_up_to"] == "2026-11-10T10:05:00+08:00"
    assert any("Failed to copy the Teams snapshot" in f for f in out["failures"])


def test_decode_failure_reported_returns_archived_data(tmp_path: Path) -> None:
    """Decode failure: simulate a snapshot decode failure (corrupt LevelDB content cannot be parsed), returning exit 0, the parse-failure error and archive data."""
    arc_path = tmp_path / "decode_fail_arc.db"
    _prefill_archive(arc_path)

    # Build a fake profile directory whose leveldb directory is empty or holds corrupt files
    crafted_profile = tmp_path / "crafted_profile_decode_fail"
    leveldb_dir = crafted_profile / "IndexedDB" / "https_teams.live.com_0.indexeddb.leveldb"
    leveldb_dir.mkdir(parents=True)
    (leveldb_dir / "CURRENT").write_text("corrupt_manifest\n", encoding="utf-8")

    code, out = run_cli("conversations", archive_path=arc_path, profile_path=crafted_profile)
    assert code == 0, f"Non-zero exit code: {code}, output={out}"
    assert len(out["conversations"]) == 1
    assert out["conversations"][0]["id"] == "19:archived_group@thread.v2"

    assert out["refresh"]["status"] == "failed"
    assert "Failed to parse the Teams snapshot" in out["refresh"]["error"]
    assert out["refresh"]["current_up_to"] == "2026-11-10T10:05:00+08:00"
    assert any("Failed to parse the Teams snapshot" in f for f in out["failures"])


def test_missing_profile_with_empty_archive_reports_source_error(tmp_path: Path) -> None:
    """When the personal profile is missing and the archive has no data, report a source error with exit code 1 and never return an empty result."""
    empty_arc_path = tmp_path / "empty_archive.db"
    non_existent_profile = tmp_path / "non_existent_profile_empty"

    code, out = run_cli("conversations", archive_path=empty_arc_path, profile_path=non_existent_profile)
    assert code != 0, f"An empty archive with a missing profile should report a non-zero exit code: {code}"
    assert "error" in out
    assert "Teams personal profile not found" in out["error"]
    assert "conversations" not in out, "Must never return an empty conversations list as if there were 'no messages / no conversations'"


def test_undecodable_record_locator_and_rest_ingested(tmp_path: Path) -> None:
    """Undecodable record: inject a single corrupt record and verify failures contains a locator (store + key + seq) while the remaining records still import."""
    # Build simulated records: 2 good records, 1 corrupt record
    good_rec_1 = MagicMock()
    good_rec_1.key.value = "19:good_conv_1@thread.v2"
    good_rec_1.ldb_seq_no = 101
    good_rec_1.value = {
        "id": "19:good_conv_1@thread.v2",
        "threadProperties": {"topic": "Good Group 1"},
        "lastMessageTimeUtc": 1794268800000,
    }

    bad_rec = MagicMock()
    bad_rec.key.value = "19:bad_conv_corrupted@thread.v2"
    bad_rec.ldb_seq_no = 102
    # Simulate an exception raised when reading .value (e.g. V8/Blink deserialization failure)
    type(bad_rec).value = property(lambda self: (_ for _ in ()).throw(ValueError("deserialization bytes corrupt")))

    good_rec_2 = MagicMock()
    good_rec_2.key.value = "19:good_conv_2@thread.v2"
    good_rec_2.ldb_seq_no = 103
    good_rec_2.value = {
        "id": "19:good_conv_2@thread.v2",
        "threadProperties": {"topic": "Good Group 2"},
        "lastMessageTimeUtc": 1794355200000,
    }

    # Simulate store.iterate_records returning an iterator that includes bad_rec
    fake_store = MagicMock()
    fake_store.iterate_records.return_value = [good_rec_1, bad_rec, good_rec_2]

    fake_conv_db = MagicMock()
    fake_conv_db.get_object_store_by_name.return_value = fake_store
    fake_conv_db.__contains__.side_effect = lambda name: name == "conversations"

    fake_db = MagicMock()
    db_id = MagicMock()
    db_id.name = "https_teams.live.com_0:conversation-manager:"
    fake_db.database_ids = [db_id]
    fake_db.__getitem__.return_value = fake_conv_db

    failures: list[str] = []
    from unittest.mock import patch
    with patch("readers.teams.snapshot.WrappedIndexDB") as mock_wrapped_db:
        mock_wrapped_db.return_value.__enter__.return_value = fake_db
        records = load_latest_conversation_records(Path("dummy_ldb"), None, failures=failures)

    # Verify failures contains an entry with the locator
    assert len(failures) == 1
    failure_msg = failures[0]
    assert "store='conversations'" in failure_msg
    assert "19:bad_conv_corrupted@thread.v2" in failure_msg
    assert "seq=102" in failure_msg
    assert "deserialization bytes corrupt" in failure_msg

    # Verify the remaining 2 good records are still read
    assert len(records) == 2
    assert "19:good_conv_1@thread.v2" in records
    assert "19:good_conv_2@thread.v2" in records


def test_shared_options_positions_before_and_after_subcommand(tmp_path: Path) -> None:
    """Verify --archive-path and --no-refresh take effect both before and after the subcommand (Carried-over fix 2)."""
    arc_a = tmp_path / "arc_a.db"
    arc_b = tmp_path / "arc_b.db"

    # Create different conversations in arc_a and arc_b
    a = Archive(db_path=arc_a)
    a.ingest_conversations(
        channel="teams",
        conversations=[{
            "id": "19:conv_a@thread.v2",
            "name": "Archive A Conv",
            "is_group": True,
            "last_time": "2026-11-10T10:00:00+08:00",
            "last_timestamp": 1794285600.0,
        }],
        source="cache",
    )
    b = Archive(db_path=arc_b)
    b.ingest_conversations(
        channel="teams",
        conversations=[{
            "id": "19:conv_b@thread.v2",
            "name": "Archive B Conv",
            "is_group": False,
            "last_time": "2026-11-10T11:00:00+08:00",
            "last_timestamp": 1794289200.0,
        }],
        source="cache",
    )

    # The settings file does not specify archive_path: only --archive-path decides which archive is used
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    isolated = settings_args(tmp_path, archive_path=None)

    def _exec(*args: str) -> dict[str, Any]:
        p = subprocess.run(
            [sys.executable, "-m", "readers.teams", *isolated, *args],
            capture_output=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        assert p.returncode == 0, f"Run failed: code={p.returncode}, stderr={p.stderr}, stdout={p.stdout}"
        return json.loads(p.stdout)

    # 1. --archive-path and --no-refresh before the subcommand
    out1 = _exec("--archive-path", str(arc_a), "--no-refresh", "conversations")
    assert out1["conversations"][0]["id"] == "19:conv_a@thread.v2"
    assert out1["refresh"]["status"] == "skipped"

    # 2. --archive-path and --no-refresh after the subcommand
    out2 = _exec("conversations", "--archive-path", str(arc_a), "--no-refresh")
    assert out2["conversations"][0]["id"] == "19:conv_a@thread.v2"
    assert out2["refresh"]["status"] == "skipped"

    # 3. Test pointing at arc_b before the subcommand
    out3 = _exec("--archive-path", str(arc_b), "--no-refresh", "conversations")
    assert out3["conversations"][0]["id"] == "19:conv_b@thread.v2"
    assert out3["refresh"]["status"] == "skipped"

    # 4. Test pointing at arc_b after the subcommand
    out4 = _exec("conversations", "--archive-path", str(arc_b), "--no-refresh")
    assert out4["conversations"][0]["id"] == "19:conv_b@thread.v2"
    assert out4["refresh"]["status"] == "skipped"


def test_missing_profile_search_and_recent_returns_archived_data(tmp_path: Path) -> None:
    """Missing profile: search and recent are still answered from the archive when the profile is missing, carrying the refresh source error."""
    arc_path = tmp_path / "missing_prof_search_arc.db"
    _prefill_archive(arc_path)
    non_existent_profile = tmp_path / "non_existent_teams_profile_sr"

    # 1. search command
    code_s, out_s = run_cli("search", "archived", archive_path=arc_path, profile_path=non_existent_profile)
    assert code_s == 0, f"Non-zero exit code: {code_s}, output={out_s}"
    assert "messages" in out_s
    assert len(out_s["messages"]) == 1
    assert out_s["messages"][0]["id"] == "msg_prefilled_1"
    assert out_s["messages"][0]["hit_in_quote"] is False
    assert out_s["refresh"]["status"] == "failed"
    assert "Teams personal profile not found" in out_s["refresh"]["error"]
    assert out_s["refresh"]["current_up_to"] == "2026-11-10T10:05:00+08:00"

    # 2. recent command
    code_r, out_r = run_cli("recent", "--since", "2026-11-01", archive_path=arc_path, profile_path=non_existent_profile)
    assert code_r == 0, f"Non-zero exit code: {code_r}, output={out_r}"
    assert "messages" in out_r
    assert len(out_r["messages"]) == 1
    assert out_r["messages"][0]["id"] == "msg_prefilled_1"
    assert "truncated" in out_r
    assert out_r["truncated"] is False
    assert out_r["refresh"]["status"] == "failed"
    assert "Teams personal profile not found" in out_r["refresh"]["error"]
    assert out_r["refresh"]["current_up_to"] == "2026-11-10T10:05:00+08:00"




def test_missing_profile_with_empty_archive_errors_for_all_query_commands(tmp_path: Path) -> None:
    """Missing profile + empty archive: messages / search (with and without --conversation) / recent all return a source error, not an empty result."""
    arc = tmp_path / "empty_all.db"
    missing = tmp_path / "no_profile"
    for argv in (
        ("conversations",),
        ("messages", "anything"),
        ("search", "kw"),
        ("search", "kw", "--conversation", "anything"),
        ("recent", "--since", "2026-01-01"),
    ):
        code, out = run_cli(*argv, archive_path=arc, profile_path=missing)
        assert code != 0, argv
        assert "Teams personal profile not found" in out.get("error", ""), (argv, out)
        assert "messages" not in out


def test_missing_profile_with_nonempty_archive_still_answers_all_query_commands(tmp_path: Path) -> None:
    """Missing profile + archive has data: search / recent still answer and mark refresh.status=failed."""
    arc = tmp_path / "full.db"
    _prefill_archive(arc)
    missing = tmp_path / "no_profile"
    for argv in (("search", "archived"), ("recent", "--since", "2026-01-01"), ("messages", "Archived Test Group")):
        code, out = run_cli(*argv, archive_path=arc, profile_path=missing)
        assert code == 0, (argv, out)
        assert out["refresh"]["status"] == "failed"
        assert len(out["messages"]) == 1


def test_replychain_empty_placeholder_is_skipped_but_other_invalid_values_fail() -> None:
    """Only a null value at a -1 key is a known empty placeholder."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from readers.teams.snapshot import load_latest_replychain_records

    def record(key: tuple[str, str], value: object, seq: int) -> SimpleNamespace:
        return SimpleNamespace(key=SimpleNamespace(value=key), value=value, ldb_seq_no=seq)

    store = MagicMock()
    store.iterate_records.return_value = [
        record(("placeholder", "-1"), None, 1),
        record(("nonempty", "-1"), "invalid", 2),
        record(("other", "0"), None, 3),
        record(("empty_string", "-1"), "", 4),
        record(("valid", "-1"), {"messageMap": {}}, 5),
    ]
    target = MagicMock()
    target.__contains__.side_effect = lambda name: name == "replychains"
    target.get_object_store_by_name.return_value = store
    db = MagicMock()
    db.database_ids = [SimpleNamespace(name="https_teams.live.com_0:replychain-manager:")]
    db.__getitem__.return_value = target
    failures: list[str] = []
    with patch("readers.teams.snapshot.WrappedIndexDB") as wrapped:
        wrapped.return_value.__enter__.return_value = db
        records = load_latest_replychain_records(Path("dummy"), None, failures)

    assert set(records) == {("valid", "-1")}
    assert len(failures) == 3
    assert all(any(name in failure for failure in failures) for name in ("nonempty", "other", "empty_string"))
