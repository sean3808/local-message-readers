import json
import os
import sqlite3
import subprocess
import sys

import pytest
from conftest import settings_args

from readers.archive import Archive
from readers.teams.normalizer import normalize_teams_message


def test_attachment_share_url_and_files():
    raw = {'id': '1', 'properties': {'files': [{'fileName': 'report.xlsx', 'fileInfo': {'shareUrl': 'https://1drv.ms/a'}}]}}
    message = normalize_teams_message(raw, 'c')
    assert message['files'] == ['report.xlsx']
    assert message['attachments'] == [{'name': 'report.xlsx', 'url': 'https://1drv.ms/a'}]


@pytest.mark.parametrize(('file', 'url'), [
    ({'fileName': 'x', 'objectUrl': 'object', 'fileInfo': {'fileUrl': 'file'}}, 'object'),
    ({'fileName': 'x', 'fileInfo': {'fileUrl': 'file'}}, 'file'),
    ({'fileName': 'x'}, None),
])
def test_url_fallbacks(file, url):
    message = normalize_teams_message({'id': '1', 'properties': {'files': [file]}}, 'c')
    assert message['attachments'] == [{'name': 'x', 'url': url}]


def test_corrupt_files_degrades_without_losing_message():
    degraded = []
    message = normalize_teams_message({'id': '1', 'properties': {'files': '{bad'}}, 'c', degraded=degraded)
    assert message['attachments'] == []
    assert message['files'] == []
    assert 'files' in degraded


def test_archive_migration_preserves_existing_data(tmp_path):
    path = tmp_path / 'archive.db'
    archive = Archive(path)
    archive.ingest_messages('teams', [{'id': '1', 'conversation_id': 'c', 'text': 'hello', 'version': 42}], 'cache')
    before = archive.query_messages('teams', 'c')[0]
    with sqlite3.connect(path) as conn:
        for table in ('messages', 'cache_messages'):
            conn.execute(f'ALTER TABLE {table} DROP COLUMN attachments')
        conn.execute('PRAGMA user_version = 1')
    migrated = Archive(path).query_messages('teams', 'c')[0]
    assert migrated == before


def test_cache_backfills_same_version_without_conflict(tmp_path):
    archive = Archive(tmp_path / 'archive.db')
    raw = {'id': '1', 'version': 100, 'properties': {'files': [{'fileName': 'x', 'fileInfo': {'shareUrl': 'url'}}]}}
    message = normalize_teams_message(raw, 'c')
    old = dict(message, attachments=[])
    archive.ingest_messages('teams', [old], 'cache')
    archive.ingest_messages('teams', [old], 'export')
    failures = []
    archive.ingest_messages('teams', [message], 'cache', failures=failures)
    result = archive.query_messages('teams', 'c')[0]
    assert result['attachments'] == [{'name': 'x', 'url': 'url'}]
    assert result['version'] == message['version']
    assert failures == []


@pytest.mark.parametrize('command', ['messages', 'search', 'recent'])
def test_cli_outputs_archived_attachment(tmp_path, command):
    path = tmp_path / 'archive.db'
    archive = Archive(path)
    raw = {'id': '1', 'version': 100, 'originalArrivalTime': 1700000000000, 'content': 'needle', 'properties': {'files': [{'fileName': 'x', 'fileInfo': {'shareUrl': 'https://1drv.ms/x'}}]}}
    archive.ingest_messages('teams', [normalize_teams_message(raw, 'c')], 'cache')
    args = {'messages': ['messages', 'c'], 'search': ['search', 'needle'], 'recent': ['recent', '--since', '2020-01-01']}[command]
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    proc = subprocess.run([sys.executable, '-m', 'readers.teams', *settings_args(tmp_path), '--archive-path', str(path), '--no-refresh', *args], capture_output=True, text=True, encoding='utf-8', env=env, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    message = json.loads(proc.stdout)['messages'][0]
    assert message['files'] == ['x']
    assert message['attachments'] == [{'name': 'x', 'url': 'https://1drv.ms/x'}]


def test_same_version_missing_url_preserves_saved_link(tmp_path):
    archive = Archive(tmp_path / 'archive.db')
    base = {'id': '1', 'conversation_id': 'c', 'version': 2, 'files': ['x']}
    archive.ingest_messages('teams', [dict(base, attachments=[{'name': 'x', 'url': 'link'}])], 'cache')
    archive.ingest_messages('teams', [dict(base, attachments=[{'name': 'x', 'url': None}])], 'cache')
    assert archive.query_messages('teams', 'c')[0]['attachments'] == [{'name': 'x', 'url': 'link'}]


def test_alias_merge_preserves_same_version_attachment(tmp_path):
    archive = Archive(tmp_path / 'archive.db')
    for cid, attachments in [('old', [{'name': 'x', 'url': 'link'}]), ('new', [])]:
        archive.ingest_messages('teams', [{'id': '1', 'conversation_id': cid, 'version': 2, 'files': ['x'], 'attachments': attachments}], 'cache')
    failures = []
    archive.merge_conversation_into('teams', 'old', 'new', failures=failures)
    assert archive.query_messages('teams', 'new')[0]['attachments'] == [{'name': 'x', 'url': 'link'}]
    assert failures == []


def test_same_named_attachments_keep_distinct_urls(tmp_path):
    archive = Archive(tmp_path / 'archive.db')
    base = {'id': '1', 'conversation_id': 'c', 'version': 2, 'files': ['x', 'x']}
    archive.ingest_messages('teams', [dict(base, attachments=[{'name': 'x', 'url': 'a'}, {'name': 'x', 'url': 'b'}])], 'cache')
    archive.ingest_messages('teams', [dict(base, attachments=[{'name': 'x', 'url': None}, {'name': 'x', 'url': None}])], 'cache')
    assert archive.query_messages('teams', 'c')[0]['attachments'] == [{'name': 'x', 'url': 'a'}, {'name': 'x', 'url': 'b'}]

def test_reordered_same_named_attachments_use_same_index(tmp_path):
    archive = Archive(tmp_path / 'archive.db')
    base = {'id': '1', 'conversation_id': 'c', 'version': 2, 'files': ['x', 'x']}
    archive.ingest_messages('teams', [dict(base, attachments=[{'name': 'x', 'url': 'a'}, {'name': 'x', 'url': 'b'}])], 'cache')
    archive.ingest_messages('teams', [dict(base, attachments=[{'name': 'x', 'url': 'b'}, {'name': 'x', 'url': None}])], 'cache')
    assert archive.query_messages('teams', 'c')[0]['attachments'] == [{'name': 'x', 'url': 'b'}, {'name': 'x', 'url': 'b'}]


@pytest.mark.parametrize(('old', 'incoming', 'expected'), [
    ([{'name': 'a', 'url': 'object'}], [{'name': 'a', 'url': 'share'}], [{'name': 'a', 'url': 'share'}]),
    ([{'name': 'a', 'url': 'old'}], [{'name': 'b', 'url': None}], [{'name': 'b', 'url': None}]),
    ([{'name': 'a', 'url': 'a-url'}, {'name': 'b', 'url': 'b-url'}],
     [{'name': 'b', 'url': None}], [{'name': 'b', 'url': 'b-url'}]),
    ([{'name': 'a', 'url': 'a1'}, {'name': 'a', 'url': 'a2'}],
     [{'name': 'b', 'url': None}, {'name': 'b', 'url': None}, {'name': 'a', 'url': None}],
     [{'name': 'b', 'url': None}, {'name': 'b', 'url': None}, {'name': 'a', 'url': None}]),
])
def test_same_version_attachment_merge_uses_incoming_shape(tmp_path, old, incoming, expected):
    archive = Archive(tmp_path / 'archive.db')
    base = {'id': '1', 'conversation_id': 'c', 'version': 2}
    archive.ingest_messages('teams', [dict(base, attachments=old)], 'cache')
    archive.ingest_messages('teams', [dict(base, attachments=incoming)], 'cache')
    attachments = archive.query_messages('teams', 'c')[0]['attachments']
    assert attachments == expected
    assert len(attachments) == len(incoming)


def test_v1_read_only_rejected_then_normal_open_migrates(tmp_path):
    path = tmp_path / 'archive.db'
    Archive(path)
    with sqlite3.connect(path) as conn:
        for table in ('messages', 'cache_messages'):
            conn.execute(f'ALTER TABLE {table} DROP COLUMN attachments')
        conn.execute('PRAGMA user_version = 1')
    with pytest.raises(RuntimeError, match='user_version=1'):
        Archive.open_read_only(path)
    Archive(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
    Archive.open_read_only(path)
