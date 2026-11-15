import json
import urllib.error
from io import BytesIO

import pytest

from readers.archive import Archive
from readers.teams import cli, download


@pytest.fixture(autouse=True)
def mock_graph_opener(monkeypatch):
    class Opener:
        def open(self, request):
            return download.urllib.request.urlopen(request)
    monkeypatch.setattr(download.urllib.request, 'build_opener', lambda handler: Opener())


def test_graph_share_id_and_anonymous_download(monkeypatch, tmp_path):
    requests = []

    def open_request(req):
        requests.append(req)
        if len(requests) == 1:
            return BytesIO(json.dumps({'name': 'a.txt', 'size': 3, '@microsoft.graph.downloadUrl': 'https://download.example/file'}).encode())
        return BytesIO(b'abc')

    monkeypatch.setattr(download, 'get_token', lambda: 'secret')
    monkeypatch.setattr(download.urllib.request, 'urlopen', open_request)
    result = download.download_attachment('https://example.com/a', tmp_path)
    assert requests[0].full_url.endswith('/shares/u!aHR0cHM6Ly9leGFtcGxlLmNvbS9h/driveItem')
    assert requests[1].get_header('Authorization') is None
    assert (tmp_path / 'a.txt').read_bytes() == b'abc'
    assert result['size'] == 3


def test_cli_errors_and_collision(monkeypatch, tmp_path, capsys):
    archive_path = tmp_path / 'archive.db'
    archive = Archive(archive_path)
    archive.ingest_messages('teams', [
        {'id': 'empty', 'conversation_id': 'c', 'version': 1},
        {'id': 'missing', 'conversation_id': 'c', 'version': 1, 'attachments': [{'name': 'a', 'url': None}]},
        {'id': 'ok', 'conversation_id': 'c', 'version': 1, 'attachments': [{'name': 'a', 'url': 'https://example.com/a'}]},
    ], 'cache')
    monkeypatch.setattr(download, 'get_token', lambda: 'secret')
    def open_request(req):
        if req.full_url.startswith('https://graph.microsoft.com'):
            return BytesIO(json.dumps({'name': 'a.txt', 'size': 3, '@microsoft.graph.downloadUrl': 'https://download.example/file'}).encode())
        return BytesIO(b'abc')
    monkeypatch.setattr(download.urllib.request, 'urlopen', open_request)
    args = ['--archive-path', str(archive_path), '--no-refresh', 'download']
    for message, expected in [('absent', 'Message not found'), ('empty', 'no attachments'), ('missing', 'could not be downloaded')]:
        assert cli.main([*args, message, '--to', str(tmp_path)]) == 1
        output = json.loads(capsys.readouterr().out)
        assert expected in output['error']
        if message == 'missing':
            assert output['attachments'][0]['reason'] == 'The attachment has no link'
    (tmp_path / 'a.txt').write_text('original')
    assert cli.main([*args, 'ok', '--to', str(tmp_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['attachments'][0]['path'].endswith('a (1).txt')
    assert (tmp_path / 'a.txt').read_text() == 'original'
    assert (tmp_path / 'a (1).txt').read_bytes() == b'abc'


@pytest.mark.parametrize('status,expected', [(401, 'permission'), (403, 'permission'), (404, 'deleted')])
def test_cli_http_error_mapping(monkeypatch, tmp_path, capsys, status, expected):
    path = tmp_path / 'archive.db'
    Archive(path).ingest_messages('teams', [{'id': '1', 'conversation_id': 'c', 'version': 1, 'attachments': [{'name': 'x', 'url': 'https://example.com'}]}], 'cache')
    monkeypatch.setattr(download, 'get_token', lambda: 'secret')
    def fail(req):
        raise urllib.error.HTTPError(req.full_url, status, 'failure', {}, None)
    monkeypatch.setattr(download.urllib.request, 'urlopen', fail)
    assert cli.main(['--archive-path', str(path), '--no-refresh', 'download', '1', '--to', str(tmp_path)]) == 1
    assert expected in json.loads(capsys.readouterr().out)['attachments'][0]['reason']


def test_content_fallback_redirect_is_anonymous(monkeypatch, tmp_path):
    requests = []
    class Opener:
        def open(self, request):
            requests.append(request)
            if not request.full_url.endswith('/content'):
                return BytesIO(json.dumps({'name': 'x', 'size': 3}).encode())
            raise urllib.error.HTTPError(request.full_url, 302, 'redirect', {'Location': 'https://download.example/file'}, None)
    def open_request(request):
        requests.append(request)
        if request.full_url.startswith('https://graph.microsoft.com'):
            return BytesIO(json.dumps({'name': 'x', 'size': 3}).encode())
        return BytesIO(b'abc')
    monkeypatch.setattr(download, 'get_token', lambda: 'secret')
    def build_opener(handler):
        assert isinstance(handler, download.NoRedirect)
        return Opener()
    monkeypatch.setattr(download.urllib.request, 'build_opener', build_opener)
    monkeypatch.setattr(download.urllib.request, 'urlopen', open_request)
    download.download_attachment('https://example.com/a', tmp_path)
    assert requests[1].full_url.endswith('/driveItem/content')
    assert requests[1].get_header('Authorization') == 'Bearer secret'
    assert requests[2].get_header('Authorization') is None


def test_size_mismatch_does_not_save(monkeypatch, tmp_path):
    monkeypatch.setattr(download, 'get_token', lambda: 'secret')
    def open_request(request):
        return BytesIO(json.dumps({'name': 'x', 'size': 4, '@microsoft.graph.downloadUrl': 'https://download.example/file'}).encode()) if 'graph.microsoft.com' in request.full_url else BytesIO(b'abc')
    monkeypatch.setattr(download.urllib.request, 'urlopen', open_request)
    with pytest.raises(RuntimeError, match='size'):
        download.download_attachment('https://example.com', tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('failure', [urllib.error.URLError('offline'), TimeoutError()])
def test_network_failure_is_clear(monkeypatch, tmp_path, failure):
    monkeypatch.setattr(download, 'get_token', lambda: 'secret')
    def fail(request):
        raise failure
    monkeypatch.setattr(download.urllib.request, 'urlopen', fail)
    with pytest.raises(RuntimeError, match='Network'):
        download.download_attachment('https://example.com', tmp_path)


def test_name_filter_and_conversation_disambiguation(monkeypatch, tmp_path, capsys):
    path = tmp_path / 'archive.db'
    archive = Archive(path)
    archive.ingest_messages('teams', [
        {'id': '1', 'conversation_id': 'a', 'version': 1, 'attachments': [{'name': 'first', 'url': 'https://example.com/first'}, {'name': 'second', 'url': 'https://example.com/second'}]},
        {'id': '1', 'conversation_id': 'b', 'version': 1, 'attachments': [{'name': 'other', 'url': 'https://example.com/other'}]},
    ], 'cache')
    tokens = []
    monkeypatch.setattr(download, 'get_token', lambda: tokens.append(1) or 'secret')
    def open_request(request):
        return BytesIO(json.dumps({'name': 'file', 'size': 3, '@microsoft.graph.downloadUrl': 'https://download.example/file'}).encode()) if 'graph.microsoft.com' in request.full_url else BytesIO(b'abc')
    monkeypatch.setattr(download.urllib.request, 'urlopen', open_request)
    args = ['--archive-path', str(path), '--no-refresh', 'download', '1', '--to', str(tmp_path)]
    assert cli.main(args) == 1
    assert 'multiple conversations' in json.loads(capsys.readouterr().out)['error']
    assert cli.main([*args, '--conversation', 'a', '--name', 'second']) == 0
    assert [x['name'] for x in json.loads(capsys.readouterr().out)['attachments']] == ['second']
    assert tokens == [1]
    assert cli.main([*args, '--conversation', 'a']) == 0
    assert len(json.loads(capsys.readouterr().out)['attachments']) == 2
    assert tokens == [1, 1]


def test_login_failure_is_reported_once(monkeypatch, tmp_path, capsys):
    path = tmp_path / 'archive.db'
    Archive(path).ingest_messages('teams', [{'id': '1', 'conversation_id': 'a', 'version': 1, 'attachments': [{'name': 'x', 'url': 'https://example.com/x'}, {'name': 'y', 'url': 'https://example.com/y'}]}], 'cache')
    calls = []
    def fail():
        calls.append(1)
        raise RuntimeError('Microsoft sign-in was cancelled, expired or failed')
    monkeypatch.setattr(download, 'get_token', fail)
    assert cli.main(['--archive-path', str(path), '--no-refresh', 'download', '1', '--to', str(tmp_path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert calls == [1]
    assert all('cancelled' in item['reason'] for item in result['attachments'])


def test_device_code_login_expired(monkeypatch, tmp_path, capsys):
    class App:
        def __init__(self, *args, **kwargs): pass
        def get_accounts(self): return []
        def initiate_device_flow(self, scopes): return {'user_code': 'CODE', 'verification_uri': 'https://example.com'}
        def acquire_token_by_device_flow(self, flow): return {'error': 'expired_token'}
    monkeypatch.setattr(download, 'get_default_data_dir', lambda: tmp_path)
    monkeypatch.setattr(download, 'FilePersistenceWithDataProtection', lambda path: object())
    monkeypatch.setattr(download, 'PersistedTokenCache', lambda persistence: object())
    monkeypatch.setattr(download.msal, 'PublicClientApplication', App)
    with pytest.raises(RuntimeError, match='expired'):
        download.get_token()
    assert 'CODE' in capsys.readouterr().err


def test_login_network_failure_is_clear(monkeypatch, tmp_path, capsys):
    path = tmp_path / 'archive.db'
    Archive(path).ingest_messages('teams', [{'id': '1', 'conversation_id': 'a', 'version': 1, 'attachments': [{'name': 'x', 'url': 'https://example.com/x'}]}], 'cache')
    def fail():
        raise TimeoutError()
    monkeypatch.setattr(download, 'get_token', fail)
    assert cli.main(['--archive-path', str(path), '--no-refresh', 'download', '1', '--to', str(tmp_path)]) == 1
    assert 'Network' in json.loads(capsys.readouterr().out)['attachments'][0]['reason']


def test_metadata_redirect_does_not_forward_authorization(monkeypatch, tmp_path):
    requests = []
    class Opener:
        def open(self, request):
            requests.append(request)
            raise urllib.error.HTTPError(request.full_url, 302, 'redirect', {'Location': 'https://other.example/file'}, None)
    def build_opener(handler):
        assert isinstance(handler, download.NoRedirect)
        return Opener()
    monkeypatch.setattr(download.urllib.request, 'build_opener', build_opener)
    monkeypatch.setattr(download.urllib.request, 'urlopen', lambda request: requests.append(request))
    with pytest.raises(RuntimeError, match='redirect'):
        download.download_attachment('https://example.com/a', tmp_path, 'secret')
    assert len(requests) == 1
    assert all(request.host == 'graph.microsoft.com' for request in requests if request.has_header('Authorization'))
    assert list(tmp_path.iterdir()) == []


def test_missing_size_does_not_save(monkeypatch, tmp_path):
    def open_request(request):
        return BytesIO(json.dumps({'name': 'x', '@microsoft.graph.downloadUrl': 'https://download.example/file'}).encode()) if 'graph.microsoft.com' in request.full_url else BytesIO(b'abc')
    monkeypatch.setattr(download.urllib.request, 'urlopen', open_request)
    with pytest.raises(RuntimeError, match='size'):
        download.download_attachment('https://example.com', tmp_path, 'secret')
    assert list(tmp_path.iterdir()) == []
