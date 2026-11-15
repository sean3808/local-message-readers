from readers.archive import Archive


def test_merge_same_source_same_version_prefers_source(tmp_path):
    archive = Archive(tmp_path / 'archive.db')
    for cid, text in [('old', 'source'), ('new', 'target')]:
        archive.ingest_messages('teams', [{'id': '1', 'conversation_id': cid, 'version': 2, 'text': text}], source='cache')
    failures = []
    stats = {}
    archive.merge_conversation_into('teams', 'old', 'new', failures, stats)
    assert archive.query_messages('teams', 'new')[0]['text'] == 'source'
    assert failures == []
    assert stats.get('unorderable_conflicts', 0) == 0


def test_merge_different_sources_conflicts(tmp_path):
    archive = Archive(tmp_path / 'archive.db')
    for cid, text, origin in [('old', 'source', 'export'), ('new', 'target', 'cache')]:
        archive.ingest_messages('teams', [{'id': '1', 'conversation_id': cid, 'version': 2, 'text': text}], source=origin)
    failures = []
    stats = {}
    archive.merge_conversation_into('teams', 'old', 'new', failures, stats)
    assert archive.query_messages('teams', 'new')[0]['text'] == 'target'
    assert len(failures) == 1
    assert stats['unorderable_conflicts'] == 1


def test_merge_older_source_backfills_only_matching_files(tmp_path):
    for same_files in (True, False):
        archive = Archive(tmp_path / f'{same_files}.db')
        target = {'id': '1', 'conversation_id': 'new', 'version': 2, 'files': ['a'],
                  'attachments': [{'name': 'a', 'url': None}]}
        name = 'a' if same_files else 'b'
        source = {'id': '1', 'conversation_id': 'old', 'version': 1, 'files': [name],
                  'attachments': [{'name': name, 'url': 'link'}]}
        archive.ingest_messages('teams', [target, source], source='cache')
        failures = []
        stats = {}
        archive.merge_conversation_into('teams', 'old', 'new', failures, stats)
        for message in (archive.query_messages('teams', 'new')[0], archive.get_cache_message('teams', 'new', '1')):
            assert message['attachments'] == [{'name': 'a', 'url': 'link' if same_files else None}]
            assert message['version'] == 2
        assert failures == []
        assert stats.get('unorderable_conflicts', 0) == 0


def test_merge_newer_source_backfills_cache_from_target(tmp_path):
    archive = Archive(tmp_path / 'archive.db')
    for cid, version, url in [('old', 2, None), ('new', 1, 'link')]:
        archive.ingest_messages('teams', [{'id': '1', 'conversation_id': cid, 'version': version,
                                         'files': ['a'], 'attachments': [{'name': 'a', 'url': url}]}], source='cache')
    archive.merge_conversation_into('teams', 'old', 'new')
    for message in (archive.query_messages('teams', 'new')[0], archive.get_cache_message('teams', 'new', '1')):
        assert message['attachments'] == [{'name': 'a', 'url': 'link'}]
        assert message['version'] == 2


def test_merge_same_source_same_version_different_files_keeps_source_row(tmp_path):
    archive = Archive(tmp_path / 'archive.db')
    for cid, name, url in [('old', 'b', 'b-url'), ('new', 'a', 'a-url')]:
        archive.ingest_messages('teams', [{'id': '1', 'conversation_id': cid, 'version': 2,
                                         'files': [name], 'attachments': [{'name': name, 'url': url}]}], source='cache')
    failures = []
    stats = {}
    archive.merge_conversation_into('teams', 'old', 'new', failures, stats)
    message = archive.query_messages('teams', 'new')[0]
    assert message['files'] == ['b']
    assert message['attachments'] == [{'name': 'b', 'url': 'b-url'}]
    assert failures == []
    assert stats.get('unorderable_conflicts', 0) == 0
