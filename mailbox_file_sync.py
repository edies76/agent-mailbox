"""Publish periodic, versioned snapshots of one explicitly selected local file.

The source file is private conversation data. The operator must explicitly acknowledge that
it will be copied into the selected Agent Mailbox workspace before this process starts.
"""
import argparse
import base64
import hashlib
import ipaddress
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BYTES = 5 * 1024 * 1024


def private_config(path):
    path = Path(path).expanduser().resolve()
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise ValueError('Instance config permissions must be private (0600)')
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    url = data.get('url', '')
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    _ = parsed.port
    local = parsed.scheme == 'http' and (host.casefold() == 'localhost' or
            (host and ipaddress.ip_address(host).is_loopback))
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment or not (parsed.scheme == 'https' or local):
        raise ValueError('Mailbox URL must use HTTPS or localhost HTTP')
    if not isinstance(data.get('token'), str) or not data['token']:
        raise ValueError('Private instance token is missing')
    return data


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(config, path, method='GET', body=None):
    payload = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    req = Request(config['url'].rstrip('/') + path, data=payload, method=method,
                  headers={'Authorization':'Bearer '+config['token'], 'Accept':'application/json',
                           **({'Content-Type':'application/json'} if payload is not None else {})})
    with build_opener(NoRedirect()).open(req, timeout=30) as response:
        return json.load(response)


def stable_file(path):
    before = path.stat()
    if before.st_size > MAX_BYTES:
        raise ValueError('File exceeds the 5 MiB version limit')
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != after.st_size:
        raise RuntimeError('File changed while reading; retry on the next interval')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Private config for this exact instance')
    parser.add_argument('--source', required=True, help='One explicitly selected file inside allowed_roots')
    parser.add_argument('--title', help='Resource title visible to workspace members (defaults to filename)')
    parser.add_argument('--interval', type=int, default=300, help='Check interval, 60–3600 seconds')
    parser.add_argument('--state', help='Private local state file; defaults beside source')
    parser.add_argument('--confirm-upload-file', '--confirm-upload-private-chat-log', dest='confirm_upload_file',
                        action='store_true', help='Acknowledge that the full selected file is copied to this workspace')
    args = parser.parse_args()
    if not 60 <= args.interval <= 3600:
        parser.error('--interval must be 60–3600 seconds')
    if not args.confirm_upload_file:
        parser.error('The full selected file is copied to this workspace; pass --confirm-upload-file after reviewing its contents and destination')
    try:
        config = private_config(args.config)
        source = Path(args.source).expanduser().resolve(strict=True)
        roots = [Path(root).expanduser().resolve() for root in config.get('allowed_roots', [])]
        if not source.is_file() or not any(source == root or root in source.parents for root in roots):
            raise ValueError('Source must be a file inside this instance config allowed_roots')
        state_path = Path(args.state).expanduser() if args.state else source.with_suffix(source.suffix + '.mailbox-state.json')
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        item_id, revision = state.get('item_id'), state.get('revision', 0)
        previous_hash = state.get('sha256', '')
        print(f"Watching one selected file for this instance; interval={args.interval}s. Ctrl+C stops.", flush=True)
        while True:
            try:
                data = stable_file(source)
                digest = hashlib.sha256(data).hexdigest()
                if digest != previous_hash:
                    media_type = mimetypes.guess_type(source.name)[0] or 'application/octet-stream'
                    payload = {'title':args.title or source.name,
                               'path':f"shared-files/{config['agent_id']}/{source.name}",
                               'content_type':media_type, 'data_base64':base64.b64encode(data).decode('ascii')}
                    if item_id:
                        result = request(config, f'/context/items/{item_id}/versions', 'POST',
                                         {'expected_revision':revision, **{k:v for k,v in payload.items() if k in ('content_type','data_base64')}})
                    else:
                        result = request(config, '/context/items', 'POST', payload)
                        item_id = result['item']['id']
                    item = result['item']
                    revision, previous_hash = item['revision'], digest
                    state = {'item_id':item_id, 'revision':revision, 'sha256':digest,
                             'source':str(source), 'workspace_id':item.get('workspace_id'), 'updated_at':time.time()}
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = state_path.with_suffix(state_path.suffix+'.tmp')
                    temporary.write_text(json.dumps(state), encoding='utf-8')
                    if os.name != 'nt': temporary.chmod(0o600)
                    os.replace(temporary, state_path)
                    print(f"Published version {revision} ({len(data)} bytes).", flush=True)
            except (OSError, ValueError, RuntimeError, HTTPError, URLError, TimeoutError, KeyError) as error:
                print(f"Sync deferred: {type(error).__name__}; will retry next interval.", file=sys.stderr, flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"Cannot start JSONL sync: {error}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
