"""Seam B: teams-reader reconcile (official export vs cached versions).

All records are hand-written, with field names mirroring the real export file; no real data is touched.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from readers.archive import Archive
from readers.teams.cli import main
from readers.teams.export import import_export
from readers.teams.reconcile import reconcile_export

ME = "8:me"


@pytest.fixture
def arc(tmp_path: Path) -> Archive:
    return Archive(db_path=tmp_path / "reconcile_archive.db")


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _exp(
    ms: int,
    text: str = "hello",
    sender: str = "8:bob",
    edited: bool = False,
    deleted: bool = False,
) -> dict[str, Any]:
    props: dict[str, Any] = {"files": "[]"}
    if edited:
        props["edittime"] = str(ms + 500)
    if deleted:
        props["deletetime"] = str(ms + 500)
    return {
        "id": str(ms),
        "displayName": None,
        "originalarrivaltime": _iso(ms),
        "messagetype": "RichText/Html",
        "version": ms,
        "content": "" if deleted else f"<p>{text}</p>",
        "from": sender,
        "properties": props,
    }


def _export_file(tmp_path: Path, conversations: list[tuple[str, list[dict[str, Any]]]]) -> Path:
    data = {
        "userId": ME,
        "exportDate": "2025-12-16T09:28",
        "conversations": [
            {
                "id": cid,
                "displayName": f"ConvTitle-{cid}",
                "version": 1765000009000,
                "properties": {"lastimreceivedtime": "2025-12-06T10:00:09.000Z"},
                "threadProperties": {"membercount": 2},
                "MessageList": msgs,
            }
            for cid, msgs in conversations
        ],
    }
    p = tmp_path / "messages.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _cache(
    cid: str,
    ms: int,
    text: str = "hello",
    sender: str = "8:bob",
    edited: bool = False,
    deleted: bool = False,
) -> dict[str, Any]:
    return {
        "conversation_id": cid,
        "id": str(ms),
        "timestamp": ms / 1000.0,
        "time": _iso(ms),
        "sender_id": sender,
        "sender": "x",
        "text": "" if deleted else text,
        "version": float(ms),
        "edited": edited,
        "deleted": deleted,
    }


def _seed_conv(arc: Archive, cid: str, members: list[str] | None = None, is_group: bool = True) -> None:
    conv: dict[str, Any] = {
        "id": cid,
        "name": f"CacheTitle-{cid}",
        "is_group": is_group,
        "unread": False,
        "last_time": "2026-11-01T10:00:00+08:00",
        "last_timestamp": 1793498400.0,
        "version": 1793498400000.0,
    }
    if members:
        conv["member_mris"] = members
    arc.ingest_conversations("teams", [conv], source="cache")


def _ids(locators: dict[str, Any]) -> list[str]:
    return sorted(loc["id"] for loc in locators["locators"])


T = 1765000000000


def test_three_categories_limited_to_overlap_window(tmp_path: Path, arc: Archive) -> None:
    cid = "19:group@thread.v2"
    _seed_conv(arc, cid)
    arc.ingest_messages(
        "teams",
        [
            _cache(cid, T + 1000),  # before window
            _cache(cid, T + 2000),  # before window
            _cache(cid, T + 3000),  # same
            _cache(cid, T + 3500, "old text"),  # text differs
            _cache(cid, T + 3600),  # edited differs
            _cache(cid, T + 3700),  # deleted differs
            _cache(cid, T + 3800, sender="8:alice"),  # sender differs
            _cache(cid, T + 5000),  # in cache not export
        ],
        source="cache",
    )
    export = _export_file(
        tmp_path,
        [
            (
                cid,
                [
                    _exp(T + 3000),
                    _exp(T + 3500, "new text"),
                    _exp(T + 3600, edited=True),
                    _exp(T + 3700, deleted=True),
                    _exp(T + 3800, sender="8:bob"),
                    _exp(T + 4500),  # in export not cache
                    _exp(T + 6000),  # after window
                ],
            )
        ],
    )

    res = reconcile_export(arc, export)

    assert res["failures"] == []
    c = res["counts"]
    assert c["in_export_not_cache"] == 1
    assert c["in_cache_not_export"] == 1
    assert c["in_both_different"] == 4
    assert c["in_both_same"] == 1
    assert c["in_both_different_fffd_only"] == 0
    assert c["export_messages_outside_window"] == 1
    assert c["cache_messages_outside_window"] == 2
    assert _ids(res["in_export_not_cache"]) == [str(T + 4500)]
    assert _ids(res["in_cache_not_export"]) == [str(T + 5000)]
    fields = {loc["id"]: loc["fields"] for loc in res["in_both_different"]["locators"]}
    assert fields == {
        str(T + 3500): ["text"],
        str(T + 3600): ["edited"],
        str(T + 3700): ["text", "deleted"],
        str(T + 3800): ["sender"],
    }
    assert c["different_by_field"] == {"deleted": 1, "edited": 1, "sender": 1, "text": 2}

    overlap = res["overlap"]
    assert overlap["scope"] == "per_conversation"
    assert overlap["conversations_with_overlap"] == 1
    assert overlap["window"]["start"] == datetime.fromtimestamp((T + 3000) / 1000).astimezone().isoformat(timespec="seconds")
    assert overlap["window"]["end"] == datetime.fromtimestamp((T + 5000) / 1000).astimezone().isoformat(timespec="seconds")


def test_fffd_only_text_difference_is_counted_separately_but_still_different(tmp_path: Path, arc: Archive) -> None:
    cid = "19:g@thread.v2"
    _seed_conv(arc, cid)
    arc.ingest_messages(
        "teams",
        [_cache(cid, T + 1000), _cache(cid, T + 2000, "a�b"), _cache(cid, T + 3000, "x�y"), _cache(cid, T + 4000)],
        source="cache",
    )
    export = _export_file(
        tmp_path,
        [(cid, [_exp(T + 1000), _exp(T + 2000, "a\xa0b"), _exp(T + 3000, "something else"), _exp(T + 4000)])],
    )

    res = reconcile_export(arc, export)

    assert res["counts"]["in_both_different"] == 2
    assert res["counts"]["in_both_different_fffd_only"] == 1
    flagged = {loc["id"]: loc.get("fffd_only", False) for loc in res["in_both_different"]["locators"]}
    assert flagged == {str(T + 2000): True, str(T + 3000): False}


def test_one_to_one_export_id_maps_to_cache_conversation(tmp_path: Path, arc: Archive) -> None:
    cache_id = "19:uni01_x@thread.v2"
    _seed_conv(arc, cache_id, members=["8:bob", ME], is_group=False)
    arc.ingest_messages("teams", [_cache(cache_id, T + 1000), _cache(cache_id, T + 3000)], source="cache")
    export = _export_file(tmp_path, [("8:bob", [_exp(T + 1000), _exp(T + 2000), _exp(T + 3000)])])

    res = reconcile_export(arc, export)

    assert res["one_to_one_mapped"] == 1
    assert res["one_to_one_unmapped"] == 0
    assert res["counts"]["in_both_same"] == 2
    # The same 1:1 message is not split into "in export, not in cache" and "in cache, not in export"
    assert res["counts"]["in_cache_not_export"] == 0
    assert res["counts"]["in_export_not_cache"] == 1
    loc = res["in_export_not_cache"]["locators"][0]
    assert {k: v for k, v in loc.items() if k != "time"} == {"conversation_id": cache_id, "id": str(T + 2000), "export_conversation_id": "8:bob"}
    assert datetime.fromisoformat(loc["time"]).timestamp() == (T + 2000) / 1000
    assert res["overlap"]["conversations_export_only"] == 0
    assert res["overlap"]["conversations_cache_only"] == 0


def test_unmapped_one_to_one_is_reported_as_export_only(tmp_path: Path, arc: Archive) -> None:
    cache_id = "19:uni01_x@thread.v2"
    _seed_conv(arc, cache_id, members=["8:bob", ME], is_group=False)
    arc.ingest_messages("teams", [_cache(cache_id, T + 1000)], source="cache")
    export = _export_file(tmp_path, [("8:stranger", [_exp(T + 1500, sender="8:stranger")])])

    res = reconcile_export(arc, export)

    assert res["one_to_one_unmapped"] == 1
    assert res["overlap"]["conversations_export_only"] == 1
    assert res["overlap"]["conversations_cache_only"] == 1
    assert res["counts"]["in_export_not_cache"] == 0
    assert res["counts"]["export_messages_in_conversations_without_cache"] == 1


def test_export_conversation_without_id_is_reported(tmp_path: Path, arc: Archive) -> None:
    cache_id = "19:uni01_x@thread.v2"
    _seed_conv(arc, cache_id, members=["8:bob", ME], is_group=False)
    arc.ingest_messages("teams", [_cache(cache_id, T + 1000)], source="cache")
    export = _export_file(tmp_path, [("", [_exp(T + 1500, sender="8:bob")])])

    res = reconcile_export(arc, export)

    hits = [f for f in res["failures"] if "Could not compare export conversation" in f and "skipped 1 messages" in f]
    assert len(hits) == 1, res["failures"]
    assert res["overlap"]["conversations_export_only"] == 0


def test_message_present_only_via_export_import_does_not_count_as_in_cache(tmp_path: Path, arc: Archive) -> None:
    cache_id = "19:uni01_x@thread.v2"
    _seed_conv(arc, cache_id, members=["8:bob", ME], is_group=False)
    arc.ingest_messages("teams", [_cache(cache_id, T + 1000), _cache(cache_id, T + 3000)], source="cache")
    export = _export_file(tmp_path, [("8:bob", [_exp(T + 1000), _exp(T + 2000), _exp(T + 3000)])])

    import_export(arc, export)  # the merged messages now contain T+2000 (its sources are export only)
    merged_ids = {m["id"] for m in arc.query_messages("teams", cache_id, limit=None)}
    assert str(T + 2000) in merged_ids

    res = reconcile_export(arc, export)

    assert res["counts"]["in_export_not_cache"] == 1
    assert _ids(res["in_export_not_cache"]) == [str(T + 2000)]
    assert res["counts"]["in_both_same"] == 2
    assert res["one_to_one_mapped"] == 1  # the registered alias


def test_ambiguous_candidates_resolved_by_cache_messages_not_merged_ones(tmp_path: Path, arc: Archive) -> None:
    for cid in ("19:uni01_a", "19:uni01_b"):
        _seed_conv(arc, cid, members=["8:bob", ME], is_group=False)
    arc.ingest_messages("teams", [_cache("19:uni01_a", T + 1000)], source="cache")
    # b has merged content from the export source only (not in cache_messages)
    arc.ingest_messages("teams", [_cache("19:uni01_b", T + 2000)], source="export")
    export = _export_file(tmp_path, [("8:bob", [_exp(T + 1000), _exp(T + 2000)])])

    res = reconcile_export(arc, export)

    assert res["failures"] == []
    assert res["one_to_one_mapped"] == 1
    assert res["counts"]["in_both_same"] == 1
    assert res["overlap"]["conversations_cache_only"] == 0


def test_no_overlap_says_so_instead_of_claiming_a_match(tmp_path: Path, arc: Archive) -> None:
    cid = "19:g@thread.v2"
    _seed_conv(arc, cid)
    arc.ingest_messages("teams", [_cache(cid, T + 1000), _cache(cid, T + 2000)], source="cache")
    export = _export_file(tmp_path, [(cid, [_exp(T + 8000), _exp(T + 9000)])])

    res = reconcile_export(arc, export)

    assert res["overlap"]["window"] == {"start": None, "end": None}
    assert res["overlap"]["conversations_with_overlap"] == 0
    assert res["overlap"]["conversations_no_overlap"] == 1
    assert "no overlapping period" in res["overlap"]["statement"]
    c = res["counts"]
    assert (c["in_export_not_cache"], c["in_cache_not_export"], c["in_both_different"], c["in_both_same"]) == (0, 0, 0, 0)
    assert c["export_messages_outside_window"] == 2
    assert c["cache_messages_outside_window"] == 2


def _fingerprint(db: Path) -> tuple[str, dict[str, int]]:
    import sqlite3
    from contextlib import closing

    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    with closing(sqlite3.connect(str(db))) as conn:
        rows = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("conversations", "messages", "cache_messages", "conversation_aliases", "sync_state")
        }
    return digest, rows


def test_cli_reconcile_is_read_only_and_prints_no_message_text_or_names(
    tmp_path: Path, arc: Archive, capsys: pytest.CaptureFixture[str]
) -> None:
    cache_id = "19:uni01_x@thread.v2"
    _seed_conv(arc, cache_id, members=["8:bob", ME], is_group=False)
    arc.ingest_messages(
        "teams",
        [_cache(cache_id, T + 1000, "SECRETCACHE"), _cache(cache_id, T + 3000, "SECRETBOTH")],
        source="cache",
    )
    export = _export_file(
        tmp_path,
        [("8:bob", [_exp(T + 1000, "SECRETEXPORT"), _exp(T + 2000, "SECRETONLY"), _exp(T + 3000, "SECRETBOTH")])],
    )
    before = _fingerprint(arc.db_path)

    code = main(["--archive-path", str(arc.db_path), "reconcile", str(export)])

    out = capsys.readouterr().out
    assert code == 0
    assert _fingerprint(arc.db_path) == before
    for leaked in ("SECRET", "ConvTitle", "CacheTitle", "displayName"):
        assert leaked not in out
    payload = json.loads(out)
    assert set(payload) >= {"overlap", "counts", "in_export_not_cache", "in_cache_not_export", "in_both_different", "failures"}
    assert payload["counts"]["in_both_different"] == 1
    assert payload["in_both_different"]["locators"][0]["fields"] == ["text"]
    assert payload["failures"] == []


def test_locator_limit_truncates_locators_but_not_counts(tmp_path: Path, arc: Archive) -> None:
    cid = "19:g@thread.v2"
    _seed_conv(arc, cid)
    arc.ingest_messages("teams", [_cache(cid, T + 1000), _cache(cid, T + 9000)], source="cache")
    export = _export_file(tmp_path, [(cid, [_exp(T + 1000)] + [_exp(T + 2000 + i * 100) for i in range(5)] + [_exp(T + 9000)])])

    limited = reconcile_export(arc, export, locator_limit=2)
    full = reconcile_export(arc, export, locator_limit=0)

    assert limited["counts"]["in_export_not_cache"] == 5
    assert len(limited["in_export_not_cache"]["locators"]) == 2
    assert limited["in_export_not_cache"]["locators_truncated"] is True
    assert len(full["in_export_not_cache"]["locators"]) == 5
    assert full["in_export_not_cache"]["locators_truncated"] is False


def test_two_export_conversations_for_one_cache_conversation_are_merged_before_comparing(
    tmp_path: Path, arc: Archive
) -> None:
    """The old `8:` id and the new `19:` id of the same person in the export both map to the same cached conversation: messages are unioned before comparing."""
    cache_id = "19:uni01_x@thread.v2"
    _seed_conv(arc, cache_id, members=["8:bob", ME], is_group=False)
    arc.ingest_messages(
        "teams",
        [_cache(cache_id, T + 1000), _cache(cache_id, T + 2000), _cache(cache_id, T + 3000)],
        source="cache",
    )
    export = _export_file(
        tmp_path,
        [
            (cache_id, [_exp(T + 1000), _exp(T + 2000)]),
            ("8:bob", [_exp(T + 2000), _exp(T + 3000)]),
        ],
    )

    res = reconcile_export(arc, export)

    assert res["failures"] == []
    assert res["counts"]["in_cache_not_export"] == 0
    assert res["counts"]["in_both_same"] == 3
    assert res["counts"]["export_duplicate_messages"] == 1
    assert res["overlap"]["cache_conversations_compared"] == 1
    assert res["overlap"]["conversations_in_export"] == 2


def test_missing_sender_on_either_side_is_not_a_difference_but_is_counted(tmp_path: Path, arc: Archive) -> None:
    cid = "19:g@thread.v2"
    _seed_conv(arc, cid)
    arc.ingest_messages("teams", [_cache(cid, T + 1000), _cache(cid, T + 2000)], source="cache")
    blank = _exp(T + 2000)
    blank["from"] = ""
    export = _export_file(tmp_path, [(cid, [_exp(T + 1000), blank])])

    res = reconcile_export(arc, export)

    assert res["counts"]["in_both_same"] == 2
    assert res["counts"]["in_both_different"] == 0
    assert res["counts"]["sender_not_comparable"] == 1


def test_sender_url_form_matches_bare_mri(tmp_path: Path, arc: Archive) -> None:
    cid = "19:g@thread.v2"
    _seed_conv(arc, cid)
    arc.ingest_messages("teams", [_cache(cid, T + 1000), _cache(cid, T + 2000)], source="cache")
    url = _exp(T + 2000, sender="https://example.invalid/v1/users/ME/contacts/8:BOB")
    export = _export_file(tmp_path, [(cid, [_exp(T + 1000), url])])

    res = reconcile_export(arc, export)

    assert res["counts"]["in_both_same"] == 2


def test_difference_only_in_second_export_copy_is_reported(tmp_path: Path, arc: Archive) -> None:
    """The same message appears in two exported conversations mapped to the same cached conversation: a difference in either copy counts as different (fields are unioned)."""
    cache_id = "19:uni01_x@thread.v2"
    _seed_conv(arc, cache_id, members=["8:bob", ME], is_group=False)
    arc.ingest_messages(
        "teams",
        [_cache(cache_id, T + 1000), _cache(cache_id, T + 2000, "same")],
        source="cache",
    )
    export = _export_file(
        tmp_path,
        [
            (cache_id, [_exp(T + 1000), _exp(T + 2000, "same")]),
            ("8:bob", [_exp(T + 1000, "changed in second copy"), _exp(T + 2000, "same", edited=True)]),
        ],
    )

    res = reconcile_export(arc, export)

    assert res["counts"]["export_duplicate_messages"] == 2
    assert res["counts"]["in_both_same"] == 0
    assert res["counts"]["in_both_different"] == 2
    fields = {loc["id"]: loc["fields"] for loc in res["in_both_different"]["locators"]}
    assert fields == {str(T + 1000): ["text"], str(T + 2000): ["edited"]}


def test_conversation_windows_are_listed_per_conversation(tmp_path: Path, arc: Archive) -> None:
    for cid in ("19:a@thread.v2", "19:b@thread.v2"):
        _seed_conv(arc, cid)
    arc.ingest_messages("teams", [_cache("19:a@thread.v2", T + 1000), _cache("19:a@thread.v2", T + 3000)], source="cache")
    arc.ingest_messages("teams", [_cache("19:b@thread.v2", T + 1000)], source="cache")
    export = _export_file(
        tmp_path,
        [("19:a@thread.v2", [_exp(T + 2000), _exp(T + 4000)]), ("19:b@thread.v2", [_exp(T + 9000)])],
    )

    res = reconcile_export(arc, export)

    wins = {w["conversation_id"]: w for w in res["overlap"]["conversation_windows"]}
    assert wins["19:a@thread.v2"]["status"] == "compared"
    assert datetime.fromisoformat(wins["19:a@thread.v2"]["start"]).timestamp() == (T + 2000) / 1000
    assert datetime.fromisoformat(wins["19:a@thread.v2"]["end"]).timestamp() == (T + 3000) / 1000
    assert wins["19:b@thread.v2"] == {"conversation_id": "19:b@thread.v2", "start": None, "end": None, "status": "no_overlap"}


def test_default_outputs_all_locators(tmp_path: Path, arc: Archive, capsys: pytest.CaptureFixture[str]) -> None:
    cid = "19:g@thread.v2"
    _seed_conv(arc, cid)
    arc.ingest_messages("teams", [_cache(cid, T + 1000), _cache(cid, T + 9000)], source="cache")
    export = _export_file(tmp_path, [(cid, [_exp(T + 1000)] + [_exp(T + 2000 + i * 10) for i in range(250)] + [_exp(T + 9000)])])

    assert main(["--archive-path", str(arc.db_path), "reconcile", str(export)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["in_export_not_cache"]["count"] == 250
    assert len(payload["in_export_not_cache"]["locators"]) == 250
    assert payload["in_export_not_cache"]["locators_truncated"] is False


def test_reconcile_refuses_missing_archive_without_creating_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    export = _export_file(tmp_path, [("19:g@thread.v2", [_exp(T + 1000)])])
    missing = tmp_path / "nope" / "archive.db"

    code = main(["--archive-path", str(missing), "reconcile", str(export)])

    assert code == 1
    assert "error" in json.loads(capsys.readouterr().out)
    assert not missing.exists()
    assert not missing.parent.exists()


def test_reconcile_refuses_unmigrated_archive_and_leaves_it_untouched(
    tmp_path: Path, arc: Archive, capsys: pytest.CaptureFixture[str]
) -> None:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(str(arc.db_path))) as conn:
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    export = _export_file(tmp_path, [("19:g@thread.v2", [_exp(T + 1000)])])
    before = hashlib.sha256(arc.db_path.read_bytes()).hexdigest()
    def files() -> list[str]:  # -wal/-shm are side files of the reader in WAL mode, not data changes
        return sorted(p.name for p in arc.db_path.parent.iterdir() if not p.name.endswith(("-wal", "-shm")))

    siblings_before = files()

    code = main(["--archive-path", str(arc.db_path), "reconcile", str(export)])

    assert code == 1
    assert "error" in json.loads(capsys.readouterr().out)
    assert hashlib.sha256(arc.db_path.read_bytes()).hexdigest() == before
    assert files() == siblings_before
