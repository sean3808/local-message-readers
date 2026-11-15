"""Teams reader smoke: runs against real local data (WV2Profile_tfl), asserting structure only and never private content (issue #11).

When the profile does not exist the tests skip explicitly with a reason, never passing silently.
During tests the archive points at tmp_path so the default user archive is not polluted.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from conftest import real_smoke_settings, settings_args

from readers.teams import (
    TEMP_PREFIX,
    _clean_str,
    get_tfl_profile_dir,
    load_latest_conversation_records,
    normalize_teams_message,
)

PROFILE_DIR = get_tfl_profile_dir()
SMOKE = real_smoke_settings()
SMOKE_KEYS = (
    "teams_group_id", "teams_second_id", "teams_group_name",
    "teams_ambiguous_name", "teams_ambiguous_id_1", "teams_ambiguous_id_2",
)

real_data = pytest.mark.skipif(
    not PROFILE_DIR.exists() or not all(SMOKE.get(name) for name in SMOKE_KEYS),
    reason=f"Fill in {', '.join(SMOKE_KEYS)} under [smoke] in settings.toml and confirm the profile: {PROFILE_DIR} (profile_dir can be set)",
)

REQUIRED_CONVERSATION_KEYS = {"id", "name", "is_group", "unread", "last_time", "last_timestamp"}
REQUIRED_MESSAGE_KEYS = {
    "id",
    "conversation_id",
    "time",
    "timestamp",
    "sender_id",
    "sender",
    "is_self",
    "type",
    "text",
    "edited",
    "deleted",
    "files",
    "quoted",
    "sources",
}
FIXTURES_PATH = Path(__file__).parent / "fixtures" / "teams" / "raw_messages.json"


def run_cli(*args: str, archive_path: Path | None = None) -> tuple[int, dict[str, Any]]:
    """Run the teams-reader CLI and return the exit code and JSON output."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    assert archive_path is not None, "run_cli needs an archive path to place its isolated settings file"
    # Always pass --settings: the archive lives in a temporary directory, the profile points at the real profile (read-only), and the real settings.toml is never read
    cmd = [sys.executable, "-m", "readers.teams",
           *settings_args(archive_path.parent, archive_path=archive_path, profile_dir=PROFILE_DIR),
           "--archive-path", str(archive_path)]
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


def _get_latest_conversations(profile_path: Path) -> dict[str, dict[str, Any]]:
    """Only "copy" the profile's IndexedDB snapshot (never open it in place) and use the production read function to get the latest value of each conversation."""
    leveldb_src = profile_path / "IndexedDB" / "https_teams.live.com_0.indexeddb.leveldb"
    if not leveldb_src.exists():
        return {}

    tmp_dir = tempfile.mkdtemp(prefix="teams-test-bookmark-")
    try:
        tmp_leveldb = Path(tmp_dir) / "leveldb"
        shutil.copytree(leveldb_src, tmp_leveldb)
        records = load_latest_conversation_records(tmp_leveldb, None)
        return {k: r.value for k, r in records.items() if isinstance(r.value, dict)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _get_sample_conversation_bookmark(profile_path: Path) -> tuple[str, str] | None:
    """Get a conversation id that has a consumptionHorizonBookmark, with its bookmark (latest seq_no)."""
    for cid, c in _get_latest_conversations(profile_path).items():
        bk = _clean_str((c.get("properties") or {}).get("consumptionHorizonBookmark"))
        if bk:
            return str(c.get("id") or cid), bk
    return None


def _get_bookmark_for_id(profile_path: Path, target_id: str) -> str | None:
    """Get the latest bookmark of a specific conversation id."""
    for cid, c in _get_latest_conversations(profile_path).items():
        if str(c.get("id") or cid) == target_id:
            return _clean_str((c.get("properties") or {}).get("consumptionHorizonBookmark")) or ""
    return None


@pytest.fixture(scope="module")
def shared_teams_archive(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    """Module-scoped fixture: runs the real local cache import only once (including replychains and conversations)."""
    shared_dir = tmp_path_factory.mktemp("teams_smoke_shared")
    db_path = shared_dir / "teams_shared.db"
    code, out = run_cli("messages", SAMPLE_CONV_ID, "--limit", "1", archive_path=db_path)
    assert code == 0, f"Shared cache import failed: {out}"
    return db_path, out


@real_data
def test_conversations_smoke(shared_teams_archive: tuple[Path, dict[str, Any]]) -> None:
    """Seam A: conversations structure and basic contract checks."""
    arc_path, init_out = shared_teams_archive
    # Verify the refresh status of the real cache import is successful
    assert init_out.get("refresh", {}).get("status") == "ok"
    assert init_out.get("refresh", {}).get("current_up_to") is not None
    assert "error" not in init_out.get("refresh", {})

    code, out = run_cli("conversations", "--limit", "100000", "--no-refresh", archive_path=arc_path)

    assert code == 0, f"Run failed: {out}"
    assert "conversations" in out, f"Missing conversations field: {out}"

    convs = out["conversations"]
    assert len(convs) > 0, "Should return at least one conversation"

    ids = [c["id"] for c in convs]
    assert len(ids) == len(set(ids)), f"Duplicate conversation ids are not allowed: {len(ids)} vs {len(set(ids))}"

    for c in convs:
        assert REQUIRED_CONVERSATION_KEYS <= c.keys(), f"Missing required fields: {c.keys()}"
        assert isinstance(c["id"], str) and c["id"]
        assert isinstance(c["name"], str) and c["name"]
        assert isinstance(c["is_group"], bool)
        assert isinstance(c["unread"], bool), f"unread must be a bool: {c['unread']}"
        assert isinstance(c["last_time"], str)
        assert isinstance(c["last_timestamp"], int)

    # Ordering: newest to oldest by last_timestamp
    timestamps = [c["last_timestamp"] for c in convs]
    assert timestamps == sorted(timestamps, reverse=True)

    # Verify the temporary snapshot was removed after the run
    leftovers = glob.glob(os.path.join(tempfile.gettempdir(), TEMP_PREFIX + "*"))
    assert not leftovers, f"The decrypted temporary snapshot should be deleted after the run: {leftovers}"


@real_data
def test_conversations_limit_option(shared_teams_archive: tuple[Path, dict[str, Any]]) -> None:
    """Verify the --limit argument truncates the result count correctly."""
    arc_path, _ = shared_teams_archive
    limit = 5
    code, out = run_cli("conversations", "--limit", str(limit), "--no-refresh", archive_path=arc_path)

    assert code == 0, out
    assert len(out["conversations"]) == limit


@real_data
def test_seam_a_read_state_preserved(tmp_path: Path) -> None:
    """Seam A read-only guarantee: the conversation unread bookmark stays exactly the same before and after running the reader."""
    sample = _get_sample_conversation_bookmark(PROFILE_DIR)
    assert sample is not None, "Could not find a conversation with a bookmark in the local Teams cache"
    conv_id, bookmark_before = sample

    arc_path = tmp_path / "read_state_archive.db"
    code, out = run_cli("conversations", archive_path=arc_path)
    assert code == 0, out

    bookmark_after = _get_bookmark_for_id(PROFILE_DIR, conv_id)
    assert bookmark_after == bookmark_before, (
        f"Unread bookmark was altered: before={bookmark_before!r}, after={bookmark_after!r}"
    )


# ---------------------------------------------------------------------------
# Normalisation fixture tests (hand-written fixtures mirroring real cache field names)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def raw_fixtures() -> dict[str, Any]:
    with open(FIXTURES_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_normalisation_plain_text_and_links(raw_fixtures: dict[str, Any]) -> None:
    """HTML is converted to readable text, keeping link targets."""
    raw = raw_fixtures["plain_text_and_links"]
    m = normalize_teams_message(raw, raw["conversationId"])
    assert m["type"] == "text"
    assert m["text"] == "Please check [the documentation](https://example.com/spec)."
    assert m["files"] == []
    assert m["quoted"] is None
    assert m["deleted"] is False
    assert m["edited"] is False


def test_normalisation_mentions(raw_fixtures: dict[str, Any]) -> None:
    """Mentions are extracted and formatted correctly."""
    raw = raw_fixtures["mentions"]
    m = normalize_teams_message(raw, raw["conversationId"])
    assert m["type"] == "text"
    assert m["text"] == "Hello @Alice Smith welcome!"


def test_normalisation_image_placeholder(raw_fixtures: dict[str, Any]) -> None:
    """Images are represented by the [image] placeholder; mixed image and text stays readable."""
    raw = raw_fixtures["image_placeholder"]
    m = normalize_teams_message(raw, raw["conversationId"])
    assert m["type"] == "text"
    assert m["text"] == "Here is the diagram\n[image]"


def test_normalisation_quoted_attribution(raw_fixtures: dict[str, Any]) -> None:
    """A quoted message extracts the original author, original time and preview text."""
    raw = raw_fixtures["quoted_attribution"]
    m = normalize_teams_message(raw, raw["conversationId"])
    assert m["type"] == "text"
    assert m["quoted"] is not None
    assert m["quoted"]["author"] == "Alice Smith"
    assert m["quoted"]["text"] == "Please check the documentation"
    assert "2026-" in m["quoted"]["time"]
    assert "> [Alice Smith" in m["text"]
    assert "Looks great!" in m["text"]


def test_normalisation_quoted_display_name_filtered(raw_fixtures: dict[str, Any]) -> None:
    """A quoted author that is the literal 'Display Name' is never adopted and stays an empty string."""
    raw = raw_fixtures["quoted_display_name_filtered"]
    m = normalize_teams_message(raw, raw["conversationId"])
    assert m["type"] == "text"
    assert m["quoted"] is not None
    assert m["quoted"]["author"] == ""
    assert "Display Name" not in m["text"]
    assert "Thank you" in m["text"]


def test_normalisation_forwarded_attribution(raw_fixtures: dict[str, Any]) -> None:
    """A forwarded message extracts the blockquote content."""
    raw = raw_fixtures["forwarded_attribution"]
    m = normalize_teams_message(raw, raw["conversationId"])
    assert m["type"] == "text"
    assert m["quoted"] is not None
    assert m["quoted"]["text"] == "Important announcement from partner"
    assert "> Important announcement from partner" in m["text"]


def test_normalisation_file_attachments(raw_fixtures: dict[str, Any]) -> None:
    """The attachment file list extracts the file names."""
    raw = raw_fixtures["file_attachments"]
    m = normalize_teams_message(raw, raw["conversationId"])
    assert m["files"] == ["Invoice_202611.pdf"]


def test_normalisation_edited_vs_skypeeditedid_only(raw_fixtures: dict[str, Any]) -> None:
    """Only edittime marks a message as edited; skypeeditedid alone does not count as edited."""
    edited_raw = raw_fixtures["edited_message"]
    m_edited = normalize_teams_message(edited_raw, edited_raw["conversationId"])
    assert m_edited["edited"] is True
    assert m_edited["deleted"] is False

    skype_only = raw_fixtures["skypeeditedid_only_not_edited"]
    m_skype = normalize_teams_message(skype_only, skype_only["conversationId"])
    assert m_skype["edited"] is False
    assert m_skype["deleted"] is False


def test_normalisation_deleted_residue(raw_fixtures: dict[str, Any]) -> None:
    """Deleted messages (deletetime, deletionInfo, or an empty skypeeditedid remnant) are marked deleted with empty content."""
    m1 = normalize_teams_message(raw_fixtures["deleted_via_deletetime"], "c1")
    assert m1["deleted"] is True
    assert m1["text"] == ""

    m2 = normalize_teams_message(raw_fixtures["deleted_via_deletion_info"], "c1")
    assert m2["deleted"] is True
    assert m2["text"] == ""

    m3 = normalize_teams_message(raw_fixtures["deleted_residue_skypeeditedid_empty"], "c1")
    assert m3["deleted"] is True
    assert m3["text"] == ""


def test_normalisation_system_events(raw_fixtures: dict[str, Any]) -> None:
    """System events (topic update, member added, etc.) are marked system and carry readable text."""
    m_topic = normalize_teams_message(raw_fixtures["system_event_topic"], "c1")
    assert m_topic["type"] == "system"
    assert "New Team Topic" in m_topic["text"]

    m_member = normalize_teams_message(raw_fixtures["system_event_add_member"], "c1")
    assert m_member["type"] == "system"
    assert "Added a member" in m_member["text"]


# ---------------------------------------------------------------------------
# Seam A messages CLI smoke tests (real local data)
# ---------------------------------------------------------------------------

SAMPLE_CONV_ID = SMOKE.get("teams_group_id", "")
SECOND_CONV_ID = SMOKE.get("teams_second_id", "")


@real_data
def test_messages_smoke(shared_teams_archive: tuple[Path, dict[str, Any]]) -> None:
    """Seam A: messages command basic contract and field checks."""
    arc_path, _ = shared_teams_archive
    code, out = run_cli("messages", SAMPLE_CONV_ID, "--limit", "5", "--no-refresh", archive_path=arc_path)

    assert code == 0, f"Run failed: {out}"
    assert "conversation" in out, f"Missing conversation field: {out}"
    assert "messages" in out, f"Missing messages field: {out}"
    assert "failures" in out, f"Missing failures field: {out}"

    assert out["conversation"]["id"] == SAMPLE_CONV_ID
    assert out["conversation"]["is_group"] is True

    msgs = out["messages"]
    assert len(msgs) > 0, "Should return at least one message"
    assert len(msgs) <= 5

    m_ids = [m["id"] for m in msgs]
    assert len(m_ids) == len(set(m_ids)), f"Duplicate message ids are not allowed: {m_ids}"

    for m in msgs:
        assert REQUIRED_MESSAGE_KEYS <= m.keys(), f"Missing required fields: {m.keys()}"
        assert isinstance(m["id"], str) and m["id"]
        assert m["conversation_id"] == SAMPLE_CONV_ID
        assert isinstance(m["time"], str)
        assert isinstance(m["timestamp"], (int, float))
        assert isinstance(m["sender_id"], str)
        assert isinstance(m["sender"], str)
        assert isinstance(m["is_self"], bool)
        assert m["type"] in ("text", "system")
        assert isinstance(m["text"], str)
        assert isinstance(m["edited"], bool)
        assert isinstance(m["deleted"], bool)
        assert isinstance(m["files"], list)
        assert m["quoted"] is None or isinstance(m["quoted"], dict)
        assert isinstance(m["sources"], list) and "cache" in m["sources"]

    # Ordering: oldest to newest
    timestamps = [(m["timestamp"], m["id"]) for m in msgs]
    assert timestamps == sorted(timestamps)

    # Seam A: messages <id> --limit 5 returns the latest messages (the last message time matches the conversations last_time within the same second)
    code_c, out_c = run_cli("conversations", "--limit", "100000", "--no-refresh", archive_path=arc_path)
    assert code_c == 0, out_c
    sample_c = next(c for c in out_c["conversations"] if c["id"] == SAMPLE_CONV_ID)
    assert msgs[-1]["time"] == sample_c["last_time"]

    # Verify the temporary snapshot was removed after the run
    leftovers = glob.glob(os.path.join(tempfile.gettempdir(), TEMP_PREFIX + "*"))
    assert not leftovers, f"The decrypted temporary snapshot should be deleted after the run: {leftovers}"


@real_data
def test_messages_paging_no_overlap(shared_teams_archive: tuple[Path, dict[str, Any]]) -> None:
    """Verify pagination limit and offset are correct, with no duplicate ids across pages."""
    arc_path, _ = shared_teams_archive
    code1, out1 = run_cli("messages", SAMPLE_CONV_ID, "--limit", "5", "--offset", "0", "--no-refresh", archive_path=arc_path)
    assert code1 == 0, out1
    page1_ids = [m["id"] for m in out1["messages"]]

    code2, out2 = run_cli("messages", SAMPLE_CONV_ID, "--limit", "5", "--offset", "5", "--no-refresh", archive_path=arc_path)
    assert code2 == 0, out2
    page2_ids = [m["id"] for m in out2["messages"]]

    assert len(page1_ids) == 5
    assert len(page2_ids) == 5
    overlap = set(page1_ids) & set(page2_ids)
    assert not overlap, f"Duplicate message ids across pages are not allowed: {overlap}"


@real_data
def test_conversation_name_resolution(shared_teams_archive: tuple[Path, dict[str, Any]]) -> None:
    """Conversation name resolution: exact name, ambiguous name errors, unknown name errors."""
    arc_path, _ = shared_teams_archive

    # 1. Unique display name (group)
    code_exact, out_exact = run_cli("messages", SMOKE["teams_group_name"], "--limit", "1", "--no-refresh", archive_path=arc_path)
    assert code_exact == 0, out_exact
    assert out_exact["conversation"]["id"] == SAMPLE_CONV_ID

    # 2. Ambiguous display name (conversations sharing a name; two 1:1 with the same name)
    code_ambig, out_ambig = run_cli("messages", SMOKE["teams_ambiguous_name"], "--limit", "1", "--no-refresh", archive_path=arc_path)
    assert code_ambig != 0
    assert "error" in out_ambig
    assert "matches multiple conversations" in out_ambig["error"]
    assert SMOKE["teams_ambiguous_id_1"] in out_ambig["error"]
    assert SMOKE["teams_ambiguous_id_2"] in out_ambig["error"]

    # 3. Unknown name
    code_unknown, out_unknown = run_cli("messages", "NoSuchConversation_XYZ", "--limit", "1", "--no-refresh", archive_path=arc_path)
    assert code_unknown != 0
    assert "error" in out_unknown
    assert "Conversation not found" in out_unknown["error"]


@real_data
def test_spot_check_image_and_quote_attribution(shared_teams_archive: tuple[Path, dict[str, Any]]) -> None:
    """Spot check against the screen (the two conversations of #7: the group and the second 1:1): quote/forward attribution and mixed image and text messages."""
    arc_path, _ = shared_teams_archive

    # Group
    code, out = run_cli("messages", SAMPLE_CONV_ID, "--limit", "100", "--no-refresh", archive_path=arc_path)
    assert code == 0, out
    msgs = out["messages"]

    # Check 1: mixed image and text messages (containing [image] and having text)
    img_text_msgs = [m for m in msgs if "[image]" in m["text"] and len(m["text"].strip()) > len("[image]")]
    assert len(img_text_msgs) > 0, "Could not find a mixed image and text message"
    for m in img_text_msgs:
        assert "[image]" in m["text"]

    # The second 1:1 conversation
    code_second, out_second = run_cli("messages", SECOND_CONV_ID, "--limit", "100", "--no-refresh", archive_path=arc_path)
    assert code_second == 0, out_second
    second_msgs = out_second["messages"]

    # Check 2: quote or forward attribution
    quoted_msgs = [m for m in msgs + second_msgs if m["quoted"] is not None]
    assert len(quoted_msgs) > 0, "Could not find a quote/forward message"
    for qm in quoted_msgs:
        q = qm["quoted"]
        assert isinstance(q, dict)
        assert "author" in q and "time" in q and "text" in q
        assert q["author"] != "Display Name", f"A quote author must never be the literal Display Name: {q}"


@real_data
def test_messages_cli_time_filtering_error_handling(shared_teams_archive: tuple[Path, dict[str, Any]]) -> None:
    """Test that the CLI reports an error and a non-zero exit code when the time format is invalid or start > end."""
    arc_path, _ = shared_teams_archive

    # 1. Invalid time format
    code_inv, out_inv = run_cli("messages", SAMPLE_CONV_ID, "--start", "invalid-date", "--no-refresh", archive_path=arc_path)
    assert code_inv != 0
    assert "error" in out_inv
    assert "Invalid time format" in out_inv["error"]

    # 2. start > end
    code_range, out_range = run_cli(
        "messages", SAMPLE_CONV_ID, "--start", "2026-12-01", "--end", "2026-01-01", "--no-refresh", archive_path=arc_path
    )
    assert code_range != 0
    assert "error" in out_range
    assert "start must not be later than end" in out_range["error"]


def test_quote_author_no_fallback_to_arbitrary_bold() -> None:
    """Make sure any bold text inside a quote block (not itemprop='mri') is not wrongly inferred as the author."""
    from readers.teams.normalizer import normalize_html_content

    html_content = (
        '<blockquote><p><strong>Warning:</strong> System will reboot soon.</p></blockquote>'
        '<p>Please save your work.</p>'
    )
    _text, quoted = normalize_html_content(html_content)
    assert quoted is not None
    # Never infer "Warning:" as the author
    assert quoted["author"] == ""
    assert "Warning:" in quoted["text"]


@real_data
def test_search_smoke(shared_teams_archive: tuple[Path, dict[str, Any]]) -> None:
    """Seam A: search command structure contract, fields, hit_in_quote and disambiguation checks."""
    arc_path, _ = shared_teams_archive
    # Search a common string
    code, out = run_cli("search", "http", "--limit", "5", "--no-refresh", archive_path=arc_path)

    assert code == 0, f"Run failed: {out}"
    assert "keyword" in out and out["keyword"] == "http"
    assert "messages" in out
    assert "refresh" in out
    assert "failures" in out
    assert out["refresh"]["status"] == "skipped"

    msgs = out["messages"]
    assert len(msgs) > 0, "Messages containing http should be found in the real data"
    assert len(msgs) <= 5

    search_required_keys = REQUIRED_MESSAGE_KEYS | {"hit_in_quote"}
    for m in msgs:
        assert search_required_keys <= m.keys(), f"Missing required fields: {m.keys()}"
        assert isinstance(m["hit_in_quote"], bool)
        assert isinstance(m["id"], str) and m["id"]
        assert isinstance(m["time"], str)
        assert isinstance(m["timestamp"], (int, float))

    # Ordering: oldest to newest
    timestamps = [(m["timestamp"], m["id"]) for m in msgs]
    assert timestamps == sorted(timestamps)

    # Test --conversation restricting the search to one conversation
    code_c, out_c = run_cli(
        "search", "http", "--conversation", SAMPLE_CONV_ID, "--limit", "5", "--no-refresh", archive_path=arc_path
    )
    assert code_c == 0, out_c
    for m in out_c["messages"]:
        assert m["conversation_id"] == SAMPLE_CONV_ID


@real_data
def test_recent_smoke(shared_teams_archive: tuple[Path, dict[str, Any]]) -> None:
    """Seam A: recent command structure contract, truncated flag and ordering checks."""
    arc_path, _ = shared_teams_archive
    # Query messages after 2026-01-01
    since_str = "2026-01-01"
    code, out = run_cli("recent", "--since", since_str, "--limit", "5", "--no-refresh", archive_path=arc_path)

    assert code == 0, f"Run failed: {out}"
    assert "since" in out and out["since"] == since_str
    assert "truncated" in out
    assert isinstance(out["truncated"], bool)
    assert "messages" in out
    assert "refresh" in out
    assert "failures" in out
    assert out["refresh"]["status"] == "skipped"

    msgs = out["messages"]
    assert len(msgs) > 0, "Should return at least one recent message"
    assert len(msgs) <= 5

    for m in msgs:
        assert REQUIRED_MESSAGE_KEYS <= m.keys(), f"Missing required fields: {m.keys()}"
        assert isinstance(m["id"], str) and m["id"]
        assert isinstance(m["time"], str)
        assert isinstance(m["timestamp"], (int, float))

    # Ordering: oldest to newest
    timestamps = [(m["timestamp"], m["id"]) for m in msgs]
    assert timestamps == sorted(timestamps)


