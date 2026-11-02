"""Read-only comparison of real Git worktrees and shared claims."""
import subprocess
import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailbox_git_radar import build_report, main


class GitRadarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.command('init', '-q')
        self.command('config', 'user.email', 'test@example.test')
        self.command('config', 'user.name', 'Test')
        (self.root / 'same.txt').write_text('base', encoding='utf-8')
        self.command('add', 'same.txt')
        self.command('commit', '-qm', 'base')
        self.base = self.command('rev-parse', 'HEAD').strip()

    def command(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True, stderr=subprocess.PIPE)

    def test_detects_dirty_worktrees_and_claim_overlaps(self):
        other = Path(self.temp.name) / 'other'
        self.command('worktree', 'add', '-qb', 'other', str(other))
        (self.root / 'same.txt').write_text('local', encoding='utf-8')
        (other / 'same.txt').write_text('other', encoding='utf-8')
        report = build_report(self.root, [{'path':'same.txt', 'owner_id':'agent:other'}], True)
        self.assertEqual(len(report['worktrees']), 2)
        self.assertEqual(len(report['reservation_overlaps']), 2)
        self.assertEqual(len(report['worktree_overlaps']), 1)
        self.assertTrue(report['advisory'])

    def test_detects_committed_change_against_merge_base(self):
        (self.root / 'new.txt').write_text('new', encoding='utf-8')
        self.command('add', 'new.txt')
        self.command('commit', '-qm', 'change')
        report = build_report(self.root, [{'path':'new.txt'}], base=self.base)
        self.assertEqual(report['reservation_overlaps'][0]['status'], 'committed_since_base')
        self.assertEqual(report['worktrees'][0]['merge_base'], self.base)

    def test_invalid_base_keeps_dirty_results(self):
        (self.root / 'same.txt').write_text('dirty', encoding='utf-8')
        report = build_report(self.root, [{'path':'same.txt'}], base='does-not-exist')
        self.assertTrue(report['reservation_overlaps'])
        self.assertIn('comparison_error', report['worktrees'][0])

    def test_publish_is_explicit_and_does_not_send_absolute_paths_or_file_content(self):
        (self.root / 'same.txt').write_text('PRIVATE CONTENT', encoding='utf-8')
        (self.root / '.env').write_text('SECRET', encoding='utf-8')
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps({'url':'https://mailbox.example', 'token':'private-token'}), encoding='utf-8')
        class Opener:
            def __init__(self): self.requests = []
            def open(self, request, timeout):
                self.requests.append(request)
                return io.BytesIO(b'{"claims": []}' if request.get_method() == 'GET' else b'{"count": 1}')
        opener = Opener()
        args = ['radar', '--config', str(config), '--repo', str(self.root)]
        with patch('sys.argv', args), patch('mailbox_git_radar.build_opener', return_value=opener), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(), 0)
        self.assertEqual(len(opener.requests), 1)
        with patch('sys.argv', args + ['--publish', '--project', 'Auton']), patch('mailbox_git_radar.build_opener', return_value=opener), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(), 0)
        published = opener.requests[-1].data.decode('utf-8')
        self.assertIn('same.txt', published)
        self.assertNotIn('.env', published)
        self.assertNotIn('PRIVATE CONTENT', published)
        self.assertNotIn(str(self.root), published)


if __name__ == '__main__':
    unittest.main()
