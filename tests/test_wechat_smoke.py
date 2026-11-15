"""WeChat reader smoke: runs the real dependencies (local WeChat data + keys), asserting structure only, never content (issue #4).

Run this first after a WeChat upgrade. When the keys are missing it skips explicitly and explains how to fix that, never passing silently.
"""

import glob
import json
import os
import subprocess
import sys
import tempfile

import pytest
from conftest import real_smoke_settings, settings_args

from readers.wechat import TEMP_PREFIX

KEYS = os.path.expanduser("~/.wechat-cli/all_keys.json")

pytestmark = pytest.mark.skipif(
    real_smoke_settings().get("wechat") is not True or not os.path.exists(KEYS),
    reason=f"Set wechat = true under [smoke] in settings.toml and confirm {KEYS} exists: with WeChat open, first run `uv run wechat-cli init --db-dir <xwechat_files\\<wxid>\\db_storage>`",
)

MESSAGE_KEYS = {"id", "conversation_id", "time", "timestamp", "sender_id", "sender", "is_self", "type", "text"}


def run(*args):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    # Always pass --settings pointing at a temporary empty settings file; never read the real settings.toml
    with tempfile.TemporaryDirectory(prefix="wechat-smoke-settings-") as folder:
        settings = settings_args(folder, archive_path=None, profile_dir=None, backup_dest=None)
        p = subprocess.run([sys.executable, "-m", "readers.wechat", *settings, *args],
                           capture_output=True, encoding="utf-8", env=env, check=False)
    return p.returncode, json.loads(p.stdout)


def assert_messages(messages):
    assert messages
    for m in messages:
        assert MESSAGE_KEYS <= m.keys()
        assert m["conversation_id"]
    stamps = [m["timestamp"] for m in messages]
    assert stamps == sorted(stamps)


def test_conversations_messages_search():
    code, out = run("conversations", "--limit", "10")
    assert code == 0, out
    convs = out["conversations"]
    assert convs and all(c["id"] for c in convs)

    code, out = run("messages", convs[0]["id"], "--limit", "20")
    assert code == 0, out
    assert out["conversation"]["id"] == convs[0]["id"]
    assert out["failures"] == []
    assert_messages(out["messages"])

    # U+7684, a very common Chinese character, so a search over real data is likely to hit
    code, out = run("search", "\u7684", "--limit", "5")
    assert code == 0, out
    assert out["failures"] == []
    assert_messages(out["messages"])

    code, out = run("recent", "--since", "2000-01-01", "--limit", "5")
    assert code == 0, out
    assert out["failures"] == [] and out["truncated"] is True
    assert_messages(out["messages"])
    assert len(out["messages"]) == 5

    leftovers = glob.glob(os.path.join(tempfile.gettempdir(), TEMP_PREFIX + "*"))
    assert not leftovers, f"the decrypted cache should be deleted after every run: {leftovers}"


def test_unknown_conversation_is_an_error():
    code, out = run("messages", "nonexistent-conversation-zzz")
    assert code == 1 and "error" in out
