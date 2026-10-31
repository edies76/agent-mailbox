"""Turning a standalone agent into a profile must keep its private inbox intact."""
import unittest
import test_mailbox as http_tests


class ProfileConversionTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp
    send = http_tests.MailboxTests.send

    def test_convert_keeps_instance_identity_token_and_mail(self):
        original = self.send('Conservar mensaje')
        path = f'/dashboard/agents/{self.b_id}/create-profile'
        self.request('POST', path, self.a_token, {'name': 'Codex local'}, 403)
        created = self.request('POST', path, self.admin, {'name': 'Codex local'}, 201)
        self.assertEqual(created['instance']['id'], self.b_id)
        self.assertEqual(created['instance']['profile_id'], created['profile']['id'])
        self.assertEqual(self.request('GET', '/inbox', self.b_token)['messages'][0]['id'], original['id'])
        new_chat = self.request('POST', f"/profiles/{created['profile']['id']}/instances/secure",
                                created['enrollment_token'], {'instance_name':'Otro chat','session_key':'other'},
                                201, {'X-Mailbox-Recovery-Secret':'x'*40})
        self.assertNotEqual(new_chat['agent']['id'], self.b_id)
        self.assertEqual(self.request('GET', '/inbox', new_chat['token'])['messages'], [])
        again = self.request('POST', path, self.admin, {'name': 'Duplicado'}, 201)
        self.assertEqual(again['profile']['id'], created['profile']['id'])


if __name__ == '__main__':
    unittest.main()
