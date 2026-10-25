"""A2A HTTP+JSON interoperability and instance-routing tests."""

import base64
import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import test_mailbox as http_tests
from a2a_gateway import ROUTING_EXTENSION, mailbox_id_from_task


class A2AGatewayTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp

    def a2a(self, method, path, token=None, body=None, extension=True, expected=200):
        headers = {"Content-Type": "application/a2a+json", "Accept": "application/a2a+json",
                   "A2A-Version": "1.0"}
        if extension:
            headers["A2A-Extensions"] = ROUTING_EXTENSION
        if token:
            headers["Authorization"] = "Bearer " + token
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(self.url + path, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                status, result = response.status, json.load(response)
        except HTTPError as error:
            with error:
                status, result = error.code, json.load(error)
        self.assertEqual(status, expected, result)
        return result

    def send_task(self, message_id="a2a-msg-1", mode="inbox", backlog=False):
        return self.a2a("POST", "/a2a/message:send", self.a_token, {
            "message": {
                "messageId": message_id,
                "role": "ROLE_USER",
                "parts": [{"text": "Inspect the named component and return evidence."}],
                "extensions": [ROUTING_EXTENSION],
            },
            "configuration": {"returnImmediately": True},
            "metadata": {ROUTING_EXTENSION: {
                "recipientInstanceId": self.b_id,
                "subject": "A2A gateway integration",
                "deliveryMode": mode,
                "wakeBacklog": backlog,
            }},
        })

    def test_public_agent_card_advertises_a2a_http_json_and_required_routing_extension(self):
        card = self.request("GET", "/.well-known/agent-card.json")
        interface = card["supportedInterfaces"][0]
        self.assertEqual(interface["protocolBinding"], "HTTP+JSON")
        self.assertEqual(interface["protocolVersion"], "1.0")
        self.assertFalse(card["capabilities"]["streaming"])
        self.assertFalse(card["capabilities"]["pushNotifications"])
        self.assertEqual(card["capabilities"]["extensions"][0]["uri"], ROUTING_EXTENSION)
        self.assertTrue(card["capabilities"]["extensions"][0]["required"])

    def test_send_get_and_reply_map_to_exact_private_mailbox_task(self):
        accepted = self.send_task(mode="wake", backlog=True)
        task = accepted["task"]
        mailbox_message_id = mailbox_id_from_task(task["id"])
        self.assertIsNotNone(mailbox_message_id)
        self.assertEqual(task["status"]["state"], "TASK_STATE_SUBMITTED")
        self.assertEqual(task["metadata"][ROUTING_EXTENSION]["recipientInstanceId"], self.b_id)

        inbox = self.request("GET", "/inbox", self.b_token)["messages"]
        delivered = next(row for row in inbox if row["id"] == mailbox_message_id)
        self.assertEqual(delivered["delivery_mode"], "wake")
        self.assertTrue(delivered["wake_backlog"])

        reply = self.request("POST", f"/messages/{mailbox_message_id}/reply", self.b_token, {
            "request_fingerprint": delivered["request_fingerprint"],
            "text": "I inspected the requested component and verified its route.",
            "status": "completed",
        }, 201)
        updated = self.a2a("GET", f"/a2a/tasks/{task['id']}?historyLength=20", self.a_token)["task"]
        self.assertEqual(updated["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(updated["artifacts"][0]["artifactId"], f"mailbox-reply-{reply['reply']['id']}")
        self.assertIn("verified its route", updated["artifacts"][0]["parts"][0]["text"])
        self.a2a("GET", f"/a2a/tasks/{task['id']}", self.b_token, expected=404)

    def test_task_listing_and_cancellation(self):
        accepted = self.send_task(message_id="a2a-cancel-test")
        task_id = accepted["task"]["id"]
        listing = self.a2a("GET", "/a2a/tasks?pageSize=10", self.a_token)
        self.assertIn(task_id, [task["id"] for task in listing["tasks"]])
        cancelled = self.a2a("POST", f"/a2a/tasks/{task_id}:cancel", self.a_token, {"id": task_id})
        self.assertEqual(cancelled["task"]["status"]["state"], "TASK_STATE_CANCELED")
        repeated = self.a2a("POST", f"/a2a/tasks/{task_id}:cancel", self.a_token, {"id": task_id})
        self.assertEqual(repeated["task"]["status"]["state"], "TASK_STATE_CANCELED")

    def test_a2a_raw_file_part_round_trips_through_mailbox_task_history(self):
        content = b"A2A attachment bytes"
        result = self.a2a("POST", "/a2a/message:send", self.a_token, {
            "message": {
                "messageId": "a2a-file-msg-1",
                "role": "ROLE_USER",
                "parts": [
                    {"text": "Inspect this text file."},
                    {"raw": base64.b64encode(content).decode("ascii"),
                     "filename": "evidence.txt", "mediaType": "text/plain"},
                ],
                "extensions": [ROUTING_EXTENSION],
            },
            "configuration": {"returnImmediately": True},
            "metadata": {ROUTING_EXTENSION: {"recipientInstanceId": self.b_id}},
        })
        task_id = result["task"]["id"]
        task = self.a2a("GET", f"/a2a/tasks/{task_id}?historyLength=10", self.a_token)["task"]
        file_part = next(part for part in task["history"][0]["parts"] if "raw" in part)
        self.assertEqual(file_part["filename"], "evidence.txt")
        self.assertEqual(base64.b64decode(file_part["raw"]), content)

    def test_a2a_requires_version_extension_and_async_request_mode(self):
        error = self.a2a("GET", "/a2a/tasks", self.a_token, extension=False, expected=400)
        self.assertEqual(error["error"]["details"][0]["reason"], "EXTENSION_SUPPORT_REQUIRED")
        body = {"message": {"messageId": "a2a-requires-async", "role": "ROLE_USER",
                            "parts": [{"text": "Inspect the component."}],
                            "extensions": [ROUTING_EXTENSION]},
                "metadata": {ROUTING_EXTENSION: {"recipientInstanceId": self.b_id}}}
        rejected = self.a2a("POST", "/a2a/message:send", self.a_token, body, expected=400)
        self.assertIn("returnImmediately=true", rejected["error"]["message"])


if __name__ == "__main__":
    unittest.main()
