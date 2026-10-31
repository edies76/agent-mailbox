"""The profiles route must retain every visible instance, including legacy unprofiled rows."""
import sqlite3
import unittest
import test_mailbox as http_tests


class ProfilesDirectoryTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp

    def test_unprofiled_legacy_instance_is_grouped_and_visible(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute('UPDATE agents SET profile_id=NULL, workspace_id="" WHERE id=?', (self.b_id,))
            db.commit()
        data = self.request('GET', '/dashboard/profiles/data', self.admin)
        group = next(item for item in data['profiles'] if item.get('unprofiled'))
        ids = {item['id'] for item in group['instances']}
        self.assertIn(self.b_id, ids)
        self.assertEqual(group['route'], '/dashboard/profiles?group=unprofiled')
        self.assertEqual(group['instances_count'], 1)

    def test_workspace_agent_is_not_hidden_when_workspace_is_empty(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute('UPDATE agents SET profile_id=NULL, workspace_id="" WHERE id=?', (self.a_id,))
            db.commit()
        data = self.request('GET', '/dashboard/profiles/data', self.admin)
        self.assertTrue(any(self.a_id in {item['id'] for item in group.get('instances', [])}
                            for group in data['profiles'] if group.get('unprofiled')))


if __name__ == '__main__':
    unittest.main()
