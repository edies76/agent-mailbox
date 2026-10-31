"""Human chat uses a private per-account mailbox and preserves agent routing."""
import base64
import http.cookiejar
import unittest
from urllib.request import HTTPCookieProcessor, build_opener

import test_mailbox as http_tests
from test_control_access import AccountSessionTests


class DashboardChatTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp
    browser_request = AccountSessionTests.browser_request

    def test_account_chat_reply_attachments_and_isolation(self):
        cookies = http.cookiejar.CookieJar()
        owner = build_opener(HTTPCookieProcessor(cookies))
        created = self.browser_request(owner, '/auth/setup', 'POST', {
            'setup_key': self.admin, 'username': 'ChatOwner', 'display_name': 'Owner Name',
            'password': 'workspace-owner-password-42'}, expected=201)
        csrf = next(c.value for c in cookies if 'csrf' in c.name)
        headers = {'X-CSRF-Token': csrf}
        invite = self.browser_request(owner, '/dashboard/member-invitations', 'POST',
                                      {'expires_in_hours': 24, 'mode': 'member'}, headers, 201)
        member_cookies = http.cookiejar.CookieJar()
        member = build_opener(HTTPCookieProcessor(member_cookies))
        self.browser_request(member, '/auth/register-member', 'POST', {
            'invite_code': invite['code'], 'username': 'ChatMember',
            'password': 'member-password-long-42'}, expected=201)
        payload = {'to_agent_id': self.a_id, 'text': 'Analiza esto',
                   'delivery_mode': 'wake', 'attachments': [
                       {'filename': 'prueba.txt', 'media_type': 'text/plain',
                        'data_base64': base64.b64encode(b'hola').decode()}]}
        self.browser_request(owner, '/dashboard/chat/messages', 'POST', payload, expected=403)
        sent = self.browser_request(owner, '/dashboard/chat/messages', 'POST', payload, headers, 201)
        self.assertIn('ChatOwner', sent['text'])
        self.assertIn(created['user']['id'], sent['text'])
        self.assertEqual(sent['to_agent_id'], self.a_id)
        received = self.request('GET', '/inbox', self.a_token)['messages']
        self.assertEqual(received[0]['id'], sent['id'])
        self.request('POST', f"/messages/{sent['id']}/reply", self.a_token, {
            'text': 'Hecho', 'request_fingerprint': received[0]['request_fingerprint']}, 201)
        conversation = self.browser_request(owner, '/dashboard/chat/data')
        self.assertEqual([m['text'] for m in conversation['messages']][1], 'Hecho')
        self.assertEqual(self.browser_request(member, '/dashboard/chat/data')['messages'], [])
        self.browser_request(member, f"/dashboard/chat/attachments/{sent['id']}/0", expected=404)
        file = self.browser_request(owner, f"/dashboard/chat/attachments/{sent['id']}/0")
        self.assertEqual(base64.b64decode(file['data_base64']), b'hola')


if __name__ == '__main__':
    unittest.main()
