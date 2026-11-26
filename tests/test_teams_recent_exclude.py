"""teams-reader CLI tests for teams_exclude_conversations and excluded_messages in recent."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from conftest import settings_args

from readers.archive import Archive


def _seed_archive(arc_path: Path) -> None:
    arc = Archive(db_path=arc_path)
    base_ts = datetime.fromisoformat("2026-11-10T10:00:00+08:00").timestamp()
    arc.ingest_conversations(
        channel="teams",
        conversations=[
            {
                "id": "19:excluded@thread.v2",
                "name": "Noisy Group",
                "is_group": True,
                "unread": False,
                "last_time": "2026-11-10T10:05:00+08:00",
                "last_timestamp": base_ts + 300.0,
                "version": 100.0,
            },
            {
                "id": "8:alice",
                "name": "Alice Example",
                "is_group": False,
                "unread": False,
                "last_time": "2026-11-10T10:04:00+08:00",
                "last_timestamp": base_ts + 240.0,
                "version": 100.0,
            },
        ],
        source="cache",
    )
    arc.ingest_messages(
        channel="teams",
        messages=[
            {
                "conversation_id": "19:excluded@thread.v2",
                "id": "m_old_excluded",
                "timestamp": base_ts - 3600.0,
                "time": "2026-11-10T09:00:00+08:00",
                "text": "Before since",
            },
            {
                "conversation_id": "19:excluded@thread.v2",
                "id": "m_ex_1",
                "timestamp": base_ts + 60.0,
                "time": "2026-11-10T10:01:00+08:00",
                "text": "Excluded 1",
            },
            {
                "conversation_id": "8:alice",
                "id": "m_keep_1",
                "timestamp": base_ts + 120.0,
                "time": "2026-11-10T10:02:00+08:00",
                "text": "Keep 1",
            },
            {
                "conversation_id": "19:excluded@thread.v2",
                "id": "m_ex_2",
                "timestamp": base_ts + 180.0,
                "time": "2026-11-10T10:03:00+08:00",
                "text": "Excluded 2",
            },
            {
                "conversation_id": "8:alice",
                "id": "m_keep_2",
                "timestamp": base_ts + 240.0,
                "time": "2026-11-10T10:04:00+08:00",
                "text": "Keep 2",
            },
            {
                "conversation_id": "19:excluded@thread.v2",
                "id": "m_ex_3",
                "timestamp": base_ts + 300.0,
                "time": "2026-11-10T10:05:00+08:00",
                "text": "Excluded 3",
            },
        ],
        source="cache",
    )


def _run_teams(
    tmp_path: Path,
    arc_path: Path,
    *args: str,
    exclude: list[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    extra = {"teams_exclude_conversations": exclude} if exclude is not None else {}
    settings = settings_args(
        tmp_path,
        archive_path=arc_path,
        profile_dir=tmp_path / "no-profile",
        **extra,
    )
    cmd = [sys.executable, "-m", "readers.teams", *settings, "--no-refresh", *args]
    proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", check=False)
    return proc.returncode, json.loads(proc.stdout)


def test_teams_recent_exclude_unset_and_empty(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_archive(arc)

    for exclude in (None, []):
        code, out = _run_teams(tmp_path, arc, "recent", "--since", "2026-11-10T10:00:00+08:00", exclude=exclude)
        assert code == 0
        assert out["excluded_messages"] == 0
        assert out["truncated"] is False
        assert [m["id"] for m in out["messages"]] == ["m_ex_1", "m_keep_1", "m_ex_2", "m_keep_2", "m_ex_3"]


def test_teams_recent_excludes_before_limit_and_counts_full_range(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_archive(arc)

    # 3 excluded in range + 2 kept in range: --limit 2 returns both kept with truncated=False
    code, out = _run_teams(
        tmp_path,
        arc,
        "recent",
        "--since",
        "2026-11-10T10:00:00+08:00",
        "--limit",
        "2",
        exclude=["19:excluded@thread.v2"],
    )
    assert code == 0
    assert out["excluded_messages"] == 3
    assert out["truncated"] is False
    assert [m["id"] for m in out["messages"]] == ["m_keep_1", "m_keep_2"]

    # Even with --limit 1 (truncating kept messages), excluded_messages still counts all 3 in range
    code_trunc, out_trunc = _run_teams(
        tmp_path,
        arc,
        "recent",
        "--since",
        "2026-11-10T10:00:00+08:00",
        "--limit",
        "1",
        exclude=["19:excluded@thread.v2"],
    )
    assert code_trunc == 0
    assert out_trunc["excluded_messages"] == 3
    assert out_trunc["truncated"] is True
    assert [m["id"] for m in out_trunc["messages"]] == ["m_keep_2"]


def test_teams_recent_unknown_excluded_id_is_not_error(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_archive(arc)

    code, out = _run_teams(
        tmp_path,
        arc,
        "recent",
        "--since",
        "2026-11-10T10:00:00+08:00",
        exclude=["19:does-not-exist@thread.v2"],
    )
    assert code == 0
    assert out["excluded_messages"] == 0
    assert len(out["messages"]) == 5


def test_teams_other_commands_still_return_excluded_conversation(tmp_path: Path) -> None:
    arc = tmp_path / "a.db"
    _seed_archive(arc)
    excluded = ["19:excluded@thread.v2"]

    code_c, out_c = _run_teams(tmp_path, arc, "conversations", exclude=excluded)
    assert code_c == 0
    assert any(c["id"] == "19:excluded@thread.v2" for c in out_c["conversations"])

    code_m, out_m = _run_teams(tmp_path, arc, "messages", "19:excluded@thread.v2", exclude=excluded)
    assert code_m == 0
    assert [m["id"] for m in out_m["messages"]] == ["m_old_excluded", "m_ex_1", "m_ex_2", "m_ex_3"]

    code_s, out_s = _run_teams(tmp_path, arc, "search", "Excluded", exclude=excluded)
    assert code_s == 0
    assert [m["id"] for m in out_s["messages"]] == ["m_ex_1", "m_ex_2", "m_ex_3"]
