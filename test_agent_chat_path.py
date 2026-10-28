"""The recommended ``chat_path`` field: which chat this is and where it lives.

It is not required, but it is what lets a peer place an instance without asking. The
agent declares it about itself (``PATCH /agents/me`` / ``mailbox_update_card``), the
dashboard can also set it at registration, and it shows up in the directory and in the
route of every message.
"""

import unittest

import test_mailbox as http_tests


class AgentChatPathTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp
    send = http_tests.MailboxTests.send

    def test_a_new_instance_declares_no_path_until_it_says_so(self):
        card = self.request("GET", "/agents/me", self.a_token)
        self.assertEqual(card["chat_path"], "")

    def test_the_agent_declares_its_chat_and_the_directory_shows_it(self):
        path = "Lunar Mobile · ZCode (bifurcación) — C:\\Nueva-carpeta\\LunarMovilReplica"
        updated = self.request("PATCH", "/agents/me", self.a_token, {"chat_path": path})
        self.assertEqual(updated["chat_path"], path)
        self.assertEqual(self.request("GET", "/agents/me", self.a_token)["chat_path"], path)
        directory = self.request("GET", "/dashboard/data", self.admin)["agents"]
        card = next(row for row in directory if row["id"] == self.a_id)
        self.assertEqual(card["chat_path"], path)
        self.assertEqual(updated["instance_name"], self.request("GET", "/agents/me", self.a_token)["instance_name"])

    def test_registration_can_declare_it_for_manual_instances(self):
        created = self.request("POST", "/agents", self.admin,
                               {"name": "Instancia con ruta", "kind": "codex",
                                "chat_path": "Revisión de seguridad — C:\\tmp\\seguridad"}, 201)
        self.assertIn("seguridad", created["agent"]["chat_path"])
        self.assertEqual(self.request("GET", "/agents/me", created["token"])["chat_path"],
                         created["agent"]["chat_path"])

    def test_an_empty_patch_is_still_rejected(self):
        self.request("PATCH", "/agents/me", self.a_token, {}, 422)

    def test_the_message_route_carries_both_paths(self):
        self.request("PATCH", "/agents/me", self.a_token, {"chat_path": "Origen — C:\\tmp\\origen"})
        self.request("PATCH", "/agents/me", self.b_token, {"chat_path": "Destino — C:\\tmp\\destino"})
        message = self.send("hola")
        data = self.request("GET", f"/dashboard/messages/{message['id']}", self.admin)
        self.assertIn("origen", data["participants"]["from"]["chat_path"])
        self.assertIn("destino", data["participants"]["to"]["chat_path"])

    def test_a_peer_can_read_the_path_from_the_directory(self):
        self.request("PATCH", "/agents/me", self.a_token, {"chat_path": "Visible para pares — C:\\tmp\\pares"})
        agents = self.request("GET", "/agents", self.b_token)
        rows = agents["agents"] if isinstance(agents, dict) else agents
        card = next(row for row in rows if row["id"] == self.a_id)
        self.assertIn("pares", card["chat_path"])


if __name__ == "__main__":
    unittest.main()
