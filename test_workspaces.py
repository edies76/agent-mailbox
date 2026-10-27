"""Workspace invitations, isolation, and controlled cross-workspace messaging."""

import http.cookiejar
import json
import secrets
import unittest
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
from urllib.error import HTTPError
from unittest.mock import Mock
from types import SimpleNamespace

import test_mailbox as http_tests
from stores import FirestoreStore


class FirestoreSafetyTests(unittest.TestCase):
    def fake_store(self):
        store = FirestoreStore.__new__(FirestoreStore)
        store.client = Mock()
        store.agents = Mock()
        store.firestore = SimpleNamespace(transactional=lambda function: function)
        return store, store.client.transaction.return_value

    def test_rotation_physically_revokes_old_keys_and_replay_is_idempotent(self):
        store, transaction = self.fake_store()
        agent_ref = store.agents.document.return_value
        agent_ref.get.return_value.to_dict.return_value = {"token_hash": "old", "credential_epoch": 0, "api_key_revision": 2}
        key_doc = Mock()
        key_doc.to_dict.return_value = {"revoked_at": None}
        query = Mock()
        query.stream.return_value = [key_doc]
        store.where = Mock(return_value=query)
        self.assertTrue(store.rotate_agent_token("agent", "new", "recovery", 0, 123.0))
        transaction.update.assert_any_call(key_doc.reference, {"revoked_at": 123.0})
        transaction.update.assert_any_call(agent_ref, {"token_hash": "new", "credential_epoch": 1,
                                                      "credential_rotated_at": 123.0, "recovery_hash": "recovery",
                                                      "last_rotation_hash": "recovery",
                                                      "api_key_revision": 3})
        agent_ref.get.return_value.to_dict.return_value = {"token_hash": "new", "recovery_hash": "recovery",
                                                          "last_rotation_hash": "recovery", "credential_epoch": 1}
        transaction.reset_mock()
        self.assertFalse(store.rotate_agent_token("agent", "new", "recovery", 0, 124.0))
        transaction.update.assert_not_called()

    def test_invite_revocation_reads_consumption_inside_transaction(self):
        store, transaction = self.fake_store()
        ref = store.client.collection.return_value.document.return_value
        ref.get.return_value.exists = True
        ref.get.return_value.to_dict.return_value = {"workspace_id": "owner", "used_at": 10, "revoked_at": None}
        self.assertFalse(store.revoke_workspace_invite("owner", "invite", 20))
        ref.get.assert_called_once_with(transaction=transaction)
        transaction.update.assert_not_called()


class WorkspaceTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp

    def invite(self):
        return self.request("POST", "/dashboard/workspace-invitations", self.admin,
                            {"expires_in_hours": 24}, 201)

    def create_workspace(self, invitation, workspace_name):
        cookies = http.cookiejar.CookieJar()
        client = build_opener(HTTPCookieProcessor(cookies))
        body = {"invite_code": invitation["code"], "workspace_name": workspace_name,
                "username": "user_" + secrets.token_hex(4), "password": secrets.token_urlsafe(16)}
        request = Request(self.url + "/auth/register", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with client.open(request, timeout=10) as response:
            self.assertEqual(response.status, 201)
            result = json.loads(response.read())
        csrf_cookie = next(cookie.value for cookie in cookies if cookie.name.endswith("csrf"))

        def call(method, path, payload=None, expected=200):
            headers = {}
            data = None
            if payload is not None:
                data = json.dumps(payload).encode()
                headers["Content-Type"] = "application/json"
            if method not in ("GET", "HEAD"):
                headers["X-CSRF-Token"] = csrf_cookie
            req = Request(self.url + path, data=data, headers=headers, method=method)
            try:
                response = client.open(req, timeout=10)
            except HTTPError as error:
                payload_text = error.read().decode(errors="replace")
                self.assertEqual(error.code, expected, payload_text)
                return json.loads(payload_text) if error.headers.get("Content-Type", "").startswith("application/json") else None
            with response:
                self.assertEqual(response.status, expected)
                return json.loads(response.read())

        result["workspace"] = call("GET", "/dashboard/workspace")
        result["create_agent"] = lambda name: call("POST", "/agents", {
            "name": name, "kind": "test", "project": workspace_name,
            "session_key": secrets.token_urlsafe(16)
        }, 201)
        result["get"] = lambda path: call("GET", path)
        result["post"] = lambda path, payload, expected=200: call("POST", path, payload, expected)
        return result

    def test_invite_creates_private_workspaces_with_explicit_peer_messaging(self):
        first_invite, second_invite = self.invite(), self.invite()
        first = self.create_workspace(first_invite, "Equipo uno")
        second = self.create_workspace(second_invite, "Equipo dos")
        first_registration = first["create_agent"]("Agente uno")
        second_registration = second["create_agent"]("Agente dos")
        first_agent = first_registration["agent"]
        second_agent = second_registration["agent"]

        # Each workspace sees itself and the inviter, but sibling invitees stay private.
        first_directory = first["get"]("/agents")
        first_ids = {agent["id"] for agent in first_directory["agents"]}
        self.assertIn(first_agent["id"], first_ids)
        self.assertIn(self.a_id, first_ids)
        self.assertNotIn(second_agent["id"], first_ids)
        self.request("DELETE", f"/agents/{second_agent['id']}", self.admin, expected=404)
        self.request("POST", f"/dashboard/agents/{second_agent['id']}/rotate-token", self.admin, {}, 404)
        first_dashboard = first["get"]("/dashboard/data")
        self.assertEqual(first_dashboard["stats"]["agents_total"], 1)

        # Invited agents may communicate with the host's agents, but not another invitee.
        sent = self.request("POST", "/messages", first_registration["token"],
                            {"to_agent_id": self.a_id, "subject": "Hello", "text": "workspace one"}, 201)
        self.assertEqual(self.request("GET", "/inbox?unread_only=true", self.a_token)["messages"][0]["id"], sent["id"])
        self.request("POST", "/messages", first_registration["token"],
                     {"to_agent_id": second_agent["id"], "text": "cross invitees"}, 403)
        second_dashboard = second["get"]("/dashboard/data")
        self.assertNotIn("workspace one", json.dumps(second_dashboard))

        # Revoking the connection removes directory visibility and blocks new cross-space sends.
        peer_workspace_id = first["workspace"]["workspace_id"]
        self.request("DELETE", f"/dashboard/workspace-connections/{peer_workspace_id}", self.admin)
        self.assertNotIn(self.a_id, {agent["id"] for agent in first["get"]("/agents")["agents"]})
        self.request("POST", "/messages", first_registration["token"],
                     {"to_agent_id": self.a_id, "text": "after disconnect"}, 403)
        reconnect_invite = self.invite()
        first["post"]("/dashboard/workspace-connections", {"invite_code": reconnect_invite["code"]}, 201)
        self.assertIn(self.a_id, {agent["id"] for agent in first["get"]("/agents")["agents"]})
        self.assertTrue(first_invite["code"].startswith("amw_"))

    def test_profile_activity_is_private_to_its_workspace_even_for_server_owner(self):
        friend = self.create_workspace(self.invite(), "Espacio privado")
        agent = friend["create_agent"]("Agente privado")
        profile = self.request("POST", "/profiles", agent["token"],
                               {"name": "Perfil privado", "kind": "test"}, 201)
        path = f"/profiles/{profile['profile']['id']}/activity"
        self.request("GET", path, self.admin, expected=404)
        self.assertEqual(friend["get"](path)["instances"][0]["instance_id"], agent["agent"]["id"])
        self.request("DELETE", f"/dashboard/workspace-connections/{friend['workspace']['workspace_id']}", self.admin)
        self.request("GET", path, self.admin, expected=404)


if __name__ == "__main__":
    unittest.main()
