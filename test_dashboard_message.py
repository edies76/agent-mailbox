"""The per-message dashboard route: access, scoping and the honest wake state.

The dashboard shows, for every interaction, whether it was a normal message or an
explicit wake request, and — when it was a wake — what the server can actually prove:
nothing yet, an acknowledgement, or a reply. These tests pin that vocabulary so the UI
never claims a wake that nobody observed.
"""

import unittest
from urllib.request import urlopen

import test_mailbox as http_tests


class DashboardMessageTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp
    send = http_tests.MailboxTests.send

    def route(self, message_id):
        return self.request("GET", f"/dashboard/messages/{message_id}", self.admin)

    def test_requires_a_dashboard_session(self):
        message = self.send("contenido privado")
        self.request("GET", f"/dashboard/messages/{message['id']}", None, expected=401)

    def test_page_is_served_without_embedding_the_session(self):
        with urlopen(self.url + "/dashboard/message/1", timeout=10) as response:
            self.assertEqual(response.status, 200)
            page = response.read().decode("utf-8")
        self.assertIn("/message.js", page)
        self.assertIn("Agent Mailbox", page)

    def test_normal_message_reports_that_no_wake_was_requested(self):
        message = self.send("¿Qué agentes hay activos?")
        data = self.route(message["id"])
        self.assertEqual(data["message"]["id"], message["id"])
        self.assertEqual(data["wake"]["mode"], "inbox")
        self.assertEqual(data["wake"]["state"], "sin_despertar")
        self.assertIn("no pedía despertar", data["wake"]["label"])
        self.assertEqual(data["thread_count"], 1)
        self.assertEqual(data["replies"], [])
        self.assertTrue(data["content_visible"])

    def test_wake_is_requested_and_attended_after_the_recipient_acknowledges(self):
        wake = self.send("despierta y responde", delivery_mode="wake")
        before = self.route(wake["id"])["wake"]
        self.assertEqual((before["mode"], before["state"], before["evidence"]), ("wake", "solicitado", None))
        self.request("POST", f"/inbox/{wake['id']}/ack", self.b_token)
        after = self.route(wake["id"])["wake"]
        self.assertEqual(after["state"], "atendido")
        self.assertEqual(after["evidence"], "acuse")
        self.assertIsNotNone(after["at"])

    def test_a_reply_is_the_strongest_wake_evidence(self):
        wake = self.send("despierta", delivery_mode="wake")
        self.request("POST", "/messages", self.b_token,
                     {"to_agent_id": self.a_id, "text": "despierto", "in_reply_to": wake["id"]}, 201)
        data = self.route(wake["id"])
        self.assertEqual(data["wake"]["evidence"], "respondido")
        self.assertEqual(len(data["replies"]), 1)
        self.assertEqual(data["thread_count"], 2)
        self.assertEqual([entry["id"] for entry in data["thread"]][0], wake["id"])

    def test_unknown_message_is_not_found(self):
        self.request("GET", "/dashboard/messages/999999", self.admin, expected=404)

    def test_wake_backlog_is_surfaced_for_the_operator(self):
        message = self.send("lee lo pendiente", delivery_mode="wake", wake_backlog=True)
        data = self.route(message["id"])
        self.assertTrue(data["message"]["wake_backlog"])


if __name__ == "__main__":
    unittest.main()
