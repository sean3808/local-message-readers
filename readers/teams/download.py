"""Download shared OneDrive attachments through Microsoft Graph."""

import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import msal
from msal_extensions import FilePersistenceWithDataProtection, PersistedTokenCache

from readers.archive import Archive, get_default_data_dir


def get_token() -> str:
    path = get_default_data_dir() / 'graph-token-cache.bin'
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = PersistedTokenCache(FilePersistenceWithDataProtection(str(path)))
    # Microsoft Graph Command Line Tools is a public client supporting personal accounts and device code without app registration.
    app = msal.PublicClientApplication('14d82eec-204b-4c2f-b7e8-296a70dab67e', authority='https://login.microsoftonline.com/consumers', token_cache=cache)
    scopes = ['Files.Read.All']
    for account in app.get_accounts():
        result = app.acquire_token_silent(scopes, account=account)
        if result and result.get('access_token'):
            return result['access_token']
    flow = app.initiate_device_flow(scopes=scopes)
    if not flow.get('user_code') or not flow.get('verification_uri'):
        raise RuntimeError('Could not start Microsoft device-code sign-in')
    print(f"Go to {flow['verification_uri']} and enter the code {flow['user_code']}", file=sys.stderr, flush=True)
    result = app.acquire_token_by_device_flow(flow)
    if not result.get('access_token'):
        raise RuntimeError('Microsoft sign-in was cancelled, expired or failed')
    return result['access_token']


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib forwards the Authorization header to the redirect target host, which would leak the
    # Bearer token; pre-authenticated OneDrive download URLs also answer 401 when sent a Bearer.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _resolve_url(endpoint: str, headers: dict, item: dict) -> str:
    url = item.get('@microsoft.graph.downloadUrl')
    if url:
        return url
    try:
        response = urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(endpoint + '/content', headers=headers))
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308) and exc.headers.get('Location'):
            return exc.headers['Location']
        raise
    with response:
        pass
    raise RuntimeError('Graph did not provide a download redirect')


def _write_unique(directory: Path, name: str, content: bytes) -> dict:
    name = Path(name.replace('\\', '/')).name
    if name in ('', '.', '..'):
        raise RuntimeError('Invalid attachment file name')
    directory.mkdir(parents=True, exist_ok=True)
    original = Path(name)
    number = 0
    while True:
        name = original.name if number == 0 else f'{original.stem} ({number}){original.suffix}'
        path = directory / name
        try:
            with path.open('xb') as output:
                output.write(content)
            return {'path': str(path), 'size': len(content)}
        except FileExistsError:
            number += 1


def download_attachment(share_url: str, directory: Path, token: str | None = None) -> dict:
    share_id = 'u!' + base64.urlsafe_b64encode(share_url.encode('utf-8')).decode('ascii').rstrip('=')
    # Do not add $select here: Graph then omits @microsoft.graph.downloadUrl.
    endpoint = f'https://graph.microsoft.com/v1.0/shares/{share_id}/driveItem'
    headers = {'Authorization': f'Bearer {token if token is not None else get_token()}'}
    try:
        try:
            with urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(endpoint, headers=headers)) as response:
                item = json.load(response)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise RuntimeError(f'Graph metadata request redirected unexpectedly: HTTP {exc.code}') from exc
            raise
        if item.get('size') is None:
            raise RuntimeError('Graph driveItem has no size; cannot verify the download size')
        url = _resolve_url(endpoint, headers, item)
        with urllib.request.urlopen(urllib.request.Request(url)) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError(f'HTTP {exc.code}: insufficient permission or the file is not shared with you') from exc
        if exc.code == 404:
            raise RuntimeError('HTTP 404: the file was deleted or the link is no longer valid') from exc
        raise RuntimeError(f'Download failed: HTTP {exc.code}') from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError('Network connection failed or timed out; check your network and retry') from exc
    if len(content) != item['size']:
        raise RuntimeError('Downloaded size does not match Graph driveItem.size')
    return _write_unique(directory, item['name'], content)


def download_message(archive: Archive, message_id: str, directory: Path, conversation_id: str | None, name_filter: str | None) -> dict:
    rows = archive.find_messages_by_id('teams', message_id)
    if conversation_id:
        rows = [row for row in rows if row['conversation_id'] == conversation_id]
    if not rows:
        raise ValueError(f'Message not found: {message_id}')
    if len(rows) > 1:
        raise ValueError(f"The message id matches multiple conversations; specify one with --conversation: {', '.join(row['conversation_id'] for row in rows)}")
    attachments = rows[0]['attachments']
    if not attachments:
        raise ValueError('The message has no attachments')
    if name_filter is not None:
        attachments = [a for a in attachments if a.get('name') == name_filter]
        if not attachments:
            raise ValueError(f'Attachment not found: {name_filter}')
    token = None
    token_error = None
    if any(a.get('url') for a in attachments):
        try:
            token = get_token()
        except (urllib.error.URLError, TimeoutError):
            token_error = 'Network connection failed or timed out; check your network and retry'
        except Exception as exc:  # noqa: BLE001 - report login failure for each attachment
            token_error = str(exc)
    results = []
    for attachment in attachments:
        name, url = attachment.get('name'), attachment.get('url')
        if not url:
            results.append({'name': name, 'status': 'skipped', 'reason': 'The attachment has no link'})
        elif token_error is not None:
            results.append({'name': name, 'status': 'error', 'reason': token_error})
        else:
            try:
                results.append({'name': name, 'status': 'downloaded', **download_attachment(url, directory, token)})
            except Exception as exc:  # noqa: BLE001 - report failure without losing other attachments
                results.append({'name': name, 'status': 'error', 'reason': str(exc)})
    result = {'message_id': message_id, 'attachments': results}
    if any(item['status'] != 'downloaded' for item in results):
        result['error'] = 'Some or all attachments could not be downloaded'
    return result
