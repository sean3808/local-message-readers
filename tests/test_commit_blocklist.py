import subprocess
import sys
from pathlib import Path

import pytest
from conftest import write_settings

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_commit_blocklist.py"


@pytest.fixture
def context(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    blocklist = tmp_path / "private.txt"
    return tmp_path, {"blocklist": blocklist, "settings": write_settings(tmp_path, commit_blocklist=blocklist)}


def run_checker(context, *args):
    repo, config = context
    return subprocess.run([sys.executable, str(SCRIPT), "--settings", str(config["settings"]), *args], cwd=repo,
                          capture_output=True, text=True, check=False)


def stage(repo, name, content):
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "--", name], cwd=repo, check=True)


def test_missing_configuration_fails(context):
    repo, config = context
    config["settings"] = write_settings(repo, commit_blocklist=None)
    result = run_checker(context)
    assert result.returncode != 0 and "commit_blocklist" in result.stderr


def test_malformed_settings_names_file_and_key(context):
    config = context[1]
    config["settings"].write_text('commit_blocklst = "x"\n', encoding="utf-8")
    result = run_checker(context)
    assert result.returncode != 0
    assert str(config["settings"]) in result.stderr and "commit_blocklst" in result.stderr


def test_undecodable_blocklist_fails_without_quoting_its_bytes(context):
    config = context[1]
    config["blocklist"].write_bytes(b"secret-\xff\xfe-entry\n")
    result = run_checker(context)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "secret" not in output and "0xff" not in output and "\\xff" not in output


def test_missing_blocklist_file_fails(context):
    assert run_checker(context).returncode != 0


def test_staged_content_is_checked_without_revealing_matches(context):
    repo, config = context
    config["blocklist"].write_text("hidden-example\n", encoding="utf-8")
    stage(repo, "file.txt", "safe\nHIDDEN-example\n")
    (repo / "file.txt").write_text("safe\n", encoding="utf-8")
    result = run_checker(context)
    assert result.returncode != 0 and "file.txt:2" in result.stdout
    assert "hidden-example" not in (result.stdout + result.stderr).lower()


def test_clean_staged_content_passes(context):
    repo, config = context
    config["blocklist"].write_text("hidden-example\n", encoding="utf-8")
    stage(repo, "file.txt", "safe\n")
    assert run_checker(context).returncode == 0


def test_staged_path_match_is_reported(context):
    repo, config = context
    config["blocklist"].write_text("private-name\n", encoding="utf-8")
    stage(repo, "a-safe.txt", "safe\n")
    stage(repo, "private-name.txt", "safe\nprivate-name again\n")
    result = run_checker(context)
    assert result.returncode != 0
    # The matching path is reported by position only (2nd staged file), never by name (#38)
    assert result.stdout.splitlines() == ["<file #2, name withheld>:path", "<file #2, name withheld>:2"]
    assert "private-name" not in (result.stdout + result.stderr).lower()


def test_hash_prefixed_entries_and_escaped_comment_prefix(context):
    repo, config = context
    config["blocklist"].write_text("# comment\n#needle\n\\# spaced\n", encoding="utf-8")
    stage(repo, "file.txt", "#needle\n# spaced\n# comment\n")
    result = run_checker(context)
    assert result.returncode != 0
    assert result.stdout.splitlines() == ["file.txt:1", "file.txt:2"]


def test_head_scans_committed_content_and_paths(context):
    repo, config = context
    config["blocklist"].write_text("blocked\n", encoding="utf-8")
    stage(repo, "blocked.txt", "safe\nblocked\n")
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], cwd=repo, check=True)
    stage(repo, "blocked.txt", "safe\n")
    result = run_checker(context, "--head")
    assert result.returncode != 0
    assert result.stdout.splitlines() == ["<file #1, name withheld>:path", "<file #1, name withheld>:2"]
