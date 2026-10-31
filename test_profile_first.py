"""Registration creates one logical profile around the first native chat."""
import unittest
import test_mailbox as http_tests


class ProfileFirstRegistrationTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp

    def test_admin_registration_returns_profile_and_first_instance(self):
        result = self.request('POST', '/agents/secure', self.admin, {
            'name': 'New Codex chat', 'kind': 'codex', 'project': 'profile-first',
            'session_key': 'profile-first-native-session'}, 201)
        self.assertTrue(result['profile_first'])
        self.assertEqual(result['agent']['profile_id'], result['profile']['id'])
        self.assertTrue(result['profile_enrollment_token'])
        profiles = self.request('GET', '/profiles', self.a_token)['profiles']
        self.assertIn(result['profile']['id'], [item['id'] for item in profiles])

    def test_join_registration_is_also_profile_first(self):
        result = self.request('POST', '/join/secure', self.join_key, {
            'name': 'Remote chat', 'kind': 'other', 'session_key': 'remote-profile-first'}, 201)
        self.assertTrue(result['profile_first'])
        self.assertEqual(result['agent']['profile_id'], result['profile']['id'])


if __name__ == '__main__':
    unittest.main()
