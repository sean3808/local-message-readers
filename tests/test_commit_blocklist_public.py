import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import write_settings

from readers.settings import SettingsError, load_settings

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_commit_blocklist.py"


def make_context(tmp_path, entries, public):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    blocklist = tmp_path / "private.txt"
    blocklist.write_text("\n".join(entries) + "\n", encoding="utf-8")
    settings = write_settings(tmp_path, commit_blocklist=blocklist)
    if public is not None:
        with settings.open("a", encoding="utf-8") as stream:
            stream.write(f"commit_blocklist_public = {json.dumps(public)}\n")
    return tmp_path, settings


def run_checker(context):
    repo, settings = context
    return subprocess.run([sys.executable, str(SCRIPT), "--settings", str(settings)], cwd=repo,
                          capture_output=True, text=True, check=False)


def stage(repo, name, content):
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "--", name], cwd=repo, check=True)


def test_entry_inside_public_string_does_not_hit(tmp_path):
    context = make_context(tmp_path, ["example", "other-secret"], ["example-owner"])
    stage(tmp_path, "file.txt", "copyright example-owner 2026\n")
    result = run_checker(context)
    assert result.returncode == 0 and result.stdout == ""


def test_same_entry_standalone_on_another_line_still_hits(tmp_path):
    context = make_context(tmp_path, ["example"], ["example-owner"])
    stage(tmp_path, "file.txt", "by example-owner\nplain example here\n")
    result = run_checker(context)
    assert result.returncode != 0
    assert result.stdout.split() == ["file.txt:2"]


def test_line_with_public_string_and_standalone_entry_hits(tmp_path):
    context = make_context(tmp_path, ["example"], ["example-owner"])
    stage(tmp_path, "file.txt", "example-owner and example\n")
    result = run_checker(context)
    assert result.returncode != 0 and result.stdout.split() == ["file.txt:1"]


def test_public_string_is_masked_case_insensitively(tmp_path):
    context = make_context(tmp_path, ["example"], ["Example-Owner"])
    stage(tmp_path, "file.txt", "EXAMPLE-OWNER\nexample-OWNER\n")
    assert run_checker(context).returncode == 0


def test_masking_does_not_join_text_around_the_public_string(tmp_path):
    # "ab" + "cd" must not match entry "abcd" once "-public-" is removed from between them
    context = make_context(tmp_path, ["abcd"], ["-public-"])
    stage(tmp_path, "file.txt", "ab-public-cd\n")
    assert run_checker(context).returncode == 0


def test_masking_applies_to_paths(tmp_path):
    context = make_context(tmp_path, ["example"], ["example-owner"])
    stage(tmp_path, "example-owner.txt", "safe\n")
    assert run_checker(context).returncode == 0


def test_path_with_standalone_entry_still_hits_and_is_withheld(tmp_path):
    context = make_context(tmp_path, ["example"], ["example-owner"])
    stage(tmp_path, "example-owner-example.txt", "safe\n")
    result = run_checker(context)
    assert result.returncode != 0
    assert "<file #1, name withheld>:path" in result.stdout
    assert "example" not in result.stdout.replace("name withheld", "")


def test_longer_public_string_is_masked_first(tmp_path):
    context = make_context(tmp_path, ["owner"], ["example", "example-owner"])
    stage(tmp_path, "file.txt", "example-owner\n")
    assert run_checker(context).returncode == 0


def test_without_key_behaviour_is_unchanged(tmp_path):
    context = make_context(tmp_path, ["example"], None)
    stage(tmp_path, "file.txt", "example-owner\n")
    result = run_checker(context)
    assert result.returncode != 0 and result.stdout.split() == ["file.txt:1"]


def test_output_never_contains_public_string_or_entry(tmp_path):
    context = make_context(tmp_path, ["example"], ["example-owner"])
    stage(tmp_path, "file.txt", "example-owner example\n")
    result = run_checker(context)
    output = (result.stdout + result.stderr).lower()
    assert "example" not in output


@pytest.mark.parametrize("order", [["abc", "bcd"], ["bcd", "abc"]])
def test_overlapping_public_occurrences_are_all_masked(tmp_path, order):
    # "d" lies inside the public occurrence "bcd", which overlaps "abc"; masking must not depend on order
    context = make_context(tmp_path, ["d", "a"], order)
    stage(tmp_path, "file.txt", "abcd\n")
    assert run_checker(context).returncode == 0


def test_public_string_with_surrounding_whitespace_is_rejected_not_trimmed(tmp_path):
    # A trimmed "ann smith " would mask a standalone "Ann Smith" that the owner never declared public
    context = make_context(tmp_path, ["ann smith"], None)
    with context[1].open("a", encoding="utf-8") as stream:
        stream.write('commit_blocklist_public = ["Ann Smith "]\n')
    stage(tmp_path, "file.txt", "Ann Smith\n")
    result = run_checker(context)
    assert result.returncode != 0 and "commit_blocklist_public" in result.stderr


@pytest.mark.parametrize("value", ['"example-owner"', "[1]", '["ok", 2]', '[""]', '["ok", "   "]', '[" lead"]', '["trail "]'])
def test_invalid_public_values_name_file_and_key(tmp_path, value):
    path = tmp_path / "settings.toml"
    path.write_text(f"commit_blocklist_public = {value}\n", encoding="utf-8")
    with pytest.raises(SettingsError) as error:
        load_settings(path)
    assert str(path) in str(error.value) and "commit_blocklist_public" in str(error.value)


def test_valid_public_array_loads(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('commit_blocklist_public = ["example-owner", "x"]\n', encoding="utf-8")
    assert load_settings(path)["commit_blocklist_public"] == ["example-owner", "x"]


def test_checker_reports_invalid_public_setting(tmp_path):
    context = make_context(tmp_path, ["example"], None)
    with context[1].open("a", encoding="utf-8") as stream:
        stream.write('commit_blocklist_public = ["ok", ""]\n')
    result = run_checker(context)
    assert result.returncode != 0 and "commit_blocklist_public" in result.stderr
