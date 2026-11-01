"""A chat name belongs to an instance and is independent of profile and instance labels."""
import unittest
import test_mailbox as http_tests


class ChatNameTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp

    def test_chat_name_is_nonempty_editable_and_separate(self):
        original = self.request('GET', '/agents/me', self.a_token)
        self.assertTrue(original['chat_name'])
        changed = self.request('PATCH', '/agents/me', self.a_token, {'chat_name': 'Auton · chat de revisión'})
        self.assertEqual(changed['chat_name'], 'Auton · chat de revisión')
        self.assertEqual(changed['name'], original['name'])
        self.assertEqual(changed['profile_id'], original['profile_id'])
        self.request('PATCH', '/agents/me', self.a_token, {'chat_name': '  '}, 422)
        self.request('PATCH', '/agents/me', self.a_token, {'chat_name': None}, 422)
        self.request('PATCH', f'/dashboard/agents/{self.a_id}/chat-name', self.b_token,
                     {'name': 'Impostor'}, 403)
        updated = self.request('PATCH', f'/dashboard/agents/{self.a_id}/chat-name', self.admin,
                               {'name': 'Auton · UI'})
        self.assertEqual(updated['chat_name'], 'Auton · UI')
        self.assertEqual(self.request('GET', '/agents/me', self.a_token)['name'], original['name'])
        self.assertNotEqual(self.request('GET', '/agents/me', self.b_token)['chat_name'], 'Auton · UI')

    def test_registration_requires_nonempty_value_when_supplied(self):
        self.request('POST', '/agents/secure', self.admin, {'name': 'Agente', 'chat_name': ''}, 422)
        result = self.request('POST', '/agents/secure', self.admin, {
            'name': 'Agente lógico', 'chat_name': 'Ventana de investigación'}, 201)
        self.assertEqual(result['agent']['chat_name'], 'Ventana de investigación')


if __name__ == '__main__':
    unittest.main()
