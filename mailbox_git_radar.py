"""Compare local Git worktree changes with this instance's workspace reservations.

Read-only inspection. Only --publish sends project-relative paths and branches;
file contents and absolute local paths are never sent. Reports are advisory.
"""
import argparse
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def valid_mailbox_url(url):
    try:
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        _ = parsed.port
        if parsed.scheme == 'https':
            return True
        return parsed.scheme == 'http' and (parsed.hostname.casefold() == 'localhost' or
                                             ipaddress.ip_address(parsed.hostname).is_loopback)
    except ValueError:
        return False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            check=False, timeout=30)
    if result.returncode:
        raise RuntimeError('Git could not inspect the requested worktree')
    return result.stdout


def changed_paths(root):
    records = git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all').split(b'\0')
    paths, i = [], 0
    while i < len(records):
        record = records[i]
        i += 1
        if len(record) < 4:
            continue
        status = record[:2].decode('ascii', errors='replace')
        path = record[3:].decode('utf-8', errors='surrogateescape').replace('\\', '/')
        paths.append({'path': path, 'status': status})
        if 'R' in status or 'C' in status:
            if i < len(records) and records[i]:
                paths.append({'path': records[i].decode('utf-8', errors='surrogateescape').replace('\\', '/'),
                              'status': 'source'})
                i += 1
    return paths


def overlaps(left, right):
    left, right = left.casefold().rstrip('/'), right.casefold().rstrip('/')
    return left == right or left.startswith(right + '/') or right.startswith(left + '/')


def shareable_path(path):
    blocked = {'.env', '.env.local', '.env.production', 'credentials.json', 'secrets.json',
               'id_rsa', 'id_ed25519', 'authorized_keys'}
    suffixes = ('.pem', '.key', '.p12', '.pfx', '.kdbx')
    parts = path.split('/')
    return (not path.startswith('/') and ':' not in path and
            all(part not in ('', '.', '..') and part.casefold() not in blocked and
                not part.casefold().endswith(suffixes) for part in parts) and
            not any(ord(char) < 32 or ord(char) == 127 for char in path) and len(path) <= 400)


def worktree_roots(root, all_worktrees):
    if not all_worktrees:
        return [Path(git(root, 'rev-parse', '--show-toplevel').decode('utf-8').strip())]
    records = git(root, 'worktree', 'list', '--porcelain', '-z').split(b'\0')
    return [Path(record[len(b'worktree '):].decode('utf-8', errors='surrogateescape'))
            for record in records if record.startswith(b'worktree ')]


def build_report(root, claims, all_worktrees=False, base=None):
    trees = []
    for tree in worktree_roots(root, all_worktrees):
        try:
            branch = git(tree, 'rev-parse', '--abbrev-ref', 'HEAD').decode('utf-8', errors='replace').strip()
            paths = changed_paths(tree)
            tree_info = {'worktree': str(tree), 'branch': branch, 'changes': paths}
            if base:
                try:
                    base_hash = git(tree, 'rev-parse', '--verify', '--end-of-options', base + '^{commit}').decode('ascii').strip()
                    if not re.fullmatch('[0-9a-f]{40,64}', base_hash):
                        raise RuntimeError('Invalid base revision')
                    ancestor = git(tree, 'merge-base', base_hash, 'HEAD').decode('ascii').strip()
                    committed = git(tree, 'diff', '--name-only', '-z', ancestor, 'HEAD', '--').split(b'\0')
                    paths.extend({'path': path.decode('utf-8', errors='surrogateescape').replace('\\', '/'),
                                  'status': 'committed_since_base'} for path in committed if path)
                    tree_info['comparison_base'] = base_hash
                    tree_info['merge_base'] = ancestor
                except (RuntimeError, subprocess.TimeoutExpired):
                    tree_info['comparison_error'] = 'Unable to compare the requested base revision'
            trees.append(tree_info)
        except (RuntimeError, subprocess.TimeoutExpired):
            trees.append({'worktree': str(tree), 'error': 'Unable to inspect worktree', 'changes': []})
    matches, worktree_matches = [], []
    for tree in trees:
        for change in tree['changes']:
            for claim in claims:
                if overlaps(change['path'], claim['path']):
                    matches.append({'worktree': tree['worktree'], 'branch': tree.get('branch'),
                                    'changed_path': change['path'], 'status': change['status'],
                                    'claim': claim})
    for index, tree in enumerate(trees):
        for other in trees[index + 1:]:
            for change in tree['changes']:
                for other_change in other['changes']:
                    if overlaps(change['path'], other_change['path']):
                        worktree_matches.append({'path': change['path'], 'other_path': other_change['path'],
                                                 'worktree': tree['worktree'], 'other_worktree': other['worktree']})
    return {'worktrees': trees, 'reservation_overlaps': matches, 'worktree_overlaps': worktree_matches,
            'advisory': True, 'scope': ('uncommitted and committed changes since merge base, plus reported reservations'
                                         if base else 'uncommitted local changes and reported reservations only')}


def main():
    parser = argparse.ArgumentParser(description='Inspect local Git changes and workspace reservations without uploading local paths.')
    parser.add_argument('--config', required=True, help='Private JSON config of this exact Mailbox instance')
    parser.add_argument('--repo', required=True, help='Local Git checkout to inspect')
    parser.add_argument('--all-worktrees', action='store_true', help='Also inspect other Git worktrees of this repository')
    parser.add_argument('--base', help='Optional Git revision for committed changes since its merge base with each worktree')
    parser.add_argument('--publish', action='store_true', help='Explicitly share changed paths and branch names with this workspace')
    parser.add_argument('--project', help='Shared project label required with --publish; never a local path')
    args = parser.parse_args()
    if args.publish and not args.project:
        parser.error('--publish requires --project')
    try:
        config = json.loads(Path(args.config).expanduser().read_text(encoding='utf-8-sig'))
        url, token = config['url'], config['token']
        if not isinstance(url, str) or not valid_mailbox_url(url) or not isinstance(token, str) or not token:
            raise ValueError('Invalid mailbox config')
        request = Request(url.rstrip('/') + '/context/claims', headers={'Authorization': 'Bearer ' + token,
                          'Accept': 'application/json'})
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            claims = json.load(response)['claims']
        report = build_report(Path(args.repo).expanduser().resolve(), claims, args.all_worktrees, args.base)
        if args.publish:
            if any(tree.get('error') or tree.get('comparison_error') for tree in report['worktrees']):
                raise ValueError('Cannot publish an incomplete inspection')
            changes, skipped = [], 0
            for tree in report['worktrees']:
                for change in tree['changes']:
                    if not shareable_path(change['path']):
                        skipped += 1
                        continue
                    changes.append({'path': change['path'], 'branch': tree.get('branch', ''), 'status': change['status']})
            if len(changes) > 500:
                raise ValueError('Too many changed paths to publish')
            payload = json.dumps({'project': args.project, 'changes': changes}).encode('utf-8')
            request = Request(url.rstrip('/') + '/context/git-snapshots', data=payload, method='POST',
                              headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                                       'Accept': 'application/json'})
            with build_opener(NoRedirect()).open(request, timeout=15) as response:
                report['published'] = json.load(response)
            report['published']['skipped_sensitive_paths'] = skipped
        print(json.dumps(report, ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
        print('Unable to inspect Git or retrieve reservations; check the repository and private configuration.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
