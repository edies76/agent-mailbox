"""Listener presence must not be confused with API activity or verified wake."""
import sqlite3
import time
import unittest
from unittest.mock import patch
from io import StringIO
from types import SimpleNamespace
import test_mailbox as http_tests
import mailbox_watch


class ListenerPresenceTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp

    def test_report_is_instance_scoped_and_expires(self):
        initial = self.request('GET', '/agents/me', self.a_token)
        self.assertFalse(initial['listener']['active'])
        self.assertIsNone(initial['wake']['configured'])
        reported = self.request('POST', '/agents/me/listener', self.a_token,
                                {'interval_seconds': 15, 'wake_configured': True})
        self.assertTrue(reported['listener']['active'])
        self.assertTrue(reported['wake']['configured'])
        self.assertFalse(reported['wake']['verified'])
        self.assertFalse(self.request('GET', '/agents/me', self.b_token)['listener']['active'])
        db = sqlite3.connect(self.db_path)
        try:
            db.execute('UPDATE agents SET listener_seen_at=? WHERE id=?', (time.time() - 1000, self.a_id))
            db.commit()
        finally:
            db.close()
        expired = self.request('GET', '/agents/me', self.a_token)
        self.assertFalse(expired['listener']['active'])
        self.assertIsNone(expired['wake']['configured'])

    def test_authentication_and_interval_limits(self):
        self.request('POST', '/agents/me/listener', None, {'interval_seconds': 15}, 401)
        self.request('POST', '/agents/me/listener', self.a_token, {'interval_seconds': 301}, 422)


class WakeReportingTests(unittest.TestCase):
    def test_uncertain_callback_never_claims_queued(self):
        message = {'id': 42, 'from_agent_id': 'sender', 'delivery_mode': 'wake'}
        output = StringIO()
        with patch('mailbox_watch.subprocess.run', return_value=SimpleNamespace(returncode=3)), patch('sys.stdout', output):
            mailbox_watch.process_message(message, 'token', {'on_wake_command': ['wake']})
        self.assertIn('mailbox_wake_uncertain', output.getvalue())
        self.assertNotIn('mailbox_wake_queued', output.getvalue())


if __name__ == '__main__':
    unittest.main()
