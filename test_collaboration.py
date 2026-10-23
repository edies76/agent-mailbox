"""Real HTTP tests for separate native chats, bound replies and shared metadata."""

import base64
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import test_mailbox as http_tests
from adapters.mailbox_hook import bind_session, format_context, handle_hook_event, hook_response


class CollaborationTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp

    def profile(self):
        return self.request("POST", "/profiles", self.a_token,
                            {"name": "ZCode", "kind": "zcode", "project": "Lunar"}, 201)

    def enroll(self, profile, key):
        return self.request("POST", f"/profiles/{profile['profile']['id']}/instances",
                            profile["enrollment_token"], {"instance_name": key, "session_key": key}, 201)

    def test_retry_keys_replay_conflict_and_deleted_tombstone(self):
        body = {"to_instance_id": self.b_id, "text": "Exact payload", "idempotency_key": "retry-1"}
        first = self.request("POST", "/messages", self.a_token, body, 201)
        replay = self.request("POST", "/messages", self.a_token, body, 201)
        self.assertEqual(first["id"], replay["id"])
        self.request("POST", "/messages", self.a_token, {**body, "text": "Changed"}, 409)
        reply = {"text": "Evidence", "request_fingerprint": first["request_fingerprint"],
                 "idempotency_key": "answer-1"}
        answer = self.request("POST", f"/messages/{first['id']}/reply", self.b_token, reply, 201)
        again = self.request("POST", f"/messages/{first['id']}/reply", self.b_token, reply, 201)
        self.assertEqual(answer["reply"]["id"], again["reply"]["id"])
        self.request("DELETE", f"/messages/{first['id']}", self.a_token)
        self.request("POST", "/messages", self.a_token, body, 410)

    def test_named_api_keys_authenticate_http_and_mcp_and_can_be_revoked(self):
        created = self.request("POST", "/agents/me/api-keys", self.a_token,
                               {"name": "ZCode MCP"}, 201)
        token, key_id = created["token"], created["key"]["id"]
        self.assertTrue(token.startswith("amb_live_"))
        self.assertNotIn("token_hash", json.dumps(created))
        summary = self.request("GET", "/inbox/summary", token)
        self.assertEqual(summary["instance"]["id"], self.a_id)
        listed = self.request("GET", "/agents/me/api-keys", token)
        self.assertFalse(listed["can_manage"])
        self.assertNotIn(token, json.dumps(listed))
        self.request("POST", "/agents/me/api-keys", token, {"name": "Escalation"}, 403)
        self.request("DELETE", f"/agents/me/api-keys/{key_id}", self.b_token, expected=404)

        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "api-key-test", "version": "1"}}}
        request = Request(self.url + "/mcp", data=json.dumps(payload).encode(), method="POST", headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        with urlopen(request, timeout=15) as response:
            self.assertEqual(json.load(response)["result"]["serverInfo"]["name"], "Agent Mailbox")

        self.request("DELETE", f"/agents/me/api-keys/{key_id}", self.a_token)
        self.request("GET", "/inbox/summary", token, expected=401)
        self.assertTrue(self.request("GET", "/agents/me/api-keys", self.a_token)["keys"][0]["revoked_at"])

    def test_empty_native_chat_still_receives_its_private_identity(self):
        config = {"agent_id": "native-chat-A", "bound_config_path": "private-instance.json",
                  "client_script": "mailbox_client.py", "token": "DO-NOT-INJECT"}
        for event in ("SessionStart", "UserPromptSubmit"):
            result = hook_response("zcode", event, {}, [], config)
            context = result["hookSpecificOutput"]["additionalContext"]
            self.assertIn("native-chat-A", context)
            self.assertIn("private-instance.json", context)
            self.assertIn("Never call a global MCP token to check identity", context)
            self.assertNotIn("DO-NOT-INJECT", context)
        self.assertIsNone(hook_response("zcode", "Stop", {}, [], config))

    def test_private_wait_wakes_on_own_message_without_acknowledging(self):
        import time
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(self.request, "GET", "/inbox/wait?timeout=3", self.b_token)
            time.sleep(.15)
            self.assertFalse(waiting.done())
            sent = self.request("POST", "/messages", self.a_token,
                                {"to_instance_id": self.b_id, "text": "Wake this inbox"}, 201)
            result = waiting.result(timeout=8)
        self.assertTrue(result["changed"])
        self.assertEqual(result["message_id"], sent["id"])
        self.assertEqual(self.request("GET", "/inbox/count", self.b_token)["pending"], 1)
        self.assertFalse(self.request("GET", "/inbox/wait?timeout=1", self.a_token)["changed"])
        self.request("GET", "/inbox/wait?timeout=1", expected=401)

    def test_profile_enrollment_resumes_native_chat_without_sharing_inbox(self):
        profile = self.profile()
        second = self.enroll(profile, "chat-native-2")
        same = self.enroll(profile, "chat-native-2")
        third = self.enroll(profile, "chat-native-3")
        self.assertEqual(second["agent"]["id"], same["agent"]["id"])
        self.assertEqual(second["token"], same["token"])
        self.assertNotEqual(second["agent"]["id"], third["agent"]["id"])
        self.request("GET", "/inbox", profile["enrollment_token"], expected=401)
        self.request("POST", f"/profiles/{profile['profile']['id']}/instances", self.b_token,
                     {"instance_name": "intruder"}, 401)
        self.assertEqual(len(self.request("GET", f"/profiles/{profile['profile']['id']}/instances",
                                         self.b_token)["instances"]), 3)
        self.request("DELETE", f"/profiles/{profile['profile']['id']}", profile["enrollment_token"], expected=409)
        for instance in (self.a_id, second["agent"]["id"], third["agent"]["id"]):
            self.request("DELETE", f"/profiles/{profile['profile']['id']}/instances/{instance}", profile["enrollment_token"])
        self.request("DELETE", f"/profiles/{profile['profile']['id']}", profile["enrollment_token"])

    def test_codex_session_start_auto_binds_one_independent_instance_per_chat(self):
        config = {"url": "https://mail.example", "profile_id": "profile-codex",
                  "enrollment_token": "private-enrollment-token", "instance_prefix": "Codex local"}
        identities = {}

        def bind(_config, config_path, host, session_id, workspace):
            identities.setdefault(session_id, "instance-" + session_id)
            return {**config, "agent_id": identities[session_id], "bound_config_path": "private/" + session_id + ".json",
                    "client_script": "private/mailbox_client.py"}

        def event(session_id):
            return {"hook_event_name": "SessionStart", "session_id": session_id, "source": "startup",
                    "cwd": "C:/private/project"}

        with (patch("adapters.mailbox_hook.read_config", return_value=config),
              patch("adapters.mailbox_hook.bind_session", side_effect=bind),
              patch("adapters.mailbox_hook.get_pending", return_value=[]),
              patch("adapters.mailbox_hook.filter_new_messages", return_value=[])):
            first = handle_hook_event("codex", event("chat-A"), "private/codex-profile.json")
            resumed = handle_hook_event("codex", event("chat-A"), "private/codex-profile.json")
            second = handle_hook_event("codex", event("chat-B"), "private/codex-profile.json")
        first_context = first["hookSpecificOutput"]["additionalContext"]
        resumed_context = resumed["hookSpecificOutput"]["additionalContext"]
        second_context = second["hookSpecificOutput"]["additionalContext"]
        self.assertIn("instance-chat-A", first_context)
        self.assertIn("private/chat-A.json", first_context)
        self.assertEqual(first_context, resumed_context)
        self.assertIn("instance-chat-B", second_context)
        self.assertNotEqual(first_context, second_context)

    def test_codex_hook_refuses_shared_static_token_fallback(self):
        with patch("adapters.mailbox_hook.read_config", return_value={
                "url": "https://mail.example", "agent_id": "shared", "token": "global-token"}):
            with self.assertRaisesRegex(ValueError, "refusing shared static-token fallback"):
                handle_hook_event("codex", {"hook_event_name": "SessionStart", "session_id": "new-chat"},
                                  "private/shared.json")

    def test_codex_plugin_marketplace_has_reviewable_session_start_hook(self):
        manifest = json.loads((Path(__file__).parent / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads((Path(__file__).parent / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
        hooks = json.loads((Path(__file__).parent / "hooks/hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "agent-mailbox")
        self.assertEqual(marketplace["plugins"][0]["source"]["path"], "./")
        handler = hooks["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertIn("mailbox_hook.py", handler["command"])
        self.assertIn("mailbox_hook.py", handler["commandWindows"])

    def test_shared_counts_and_opt_in_subject_never_expose_body_or_files(self):
        profile = self.profile()
        sibling = self.enroll(profile, "chat-B")
        for share, subject in ((False, "PRIVATE SUBJECT"), (True, "Shared topic")):
            self.request("POST", "/messages", self.b_token,
                         {"to_instance_id": self.a_id, "text": "PRIVATE BODY", "subject": subject,
                          "share_subject": share, "attachments": [{"filename": "PRIVATE-FILE.txt",
                          "media_type": "text/plain", "data_base64": base64.b64encode(b"private bytes").decode()}]}, 201)
        activity = self.request("GET", f"/profiles/{profile['profile']['id']}/activity", sibling["token"])
        encoded = json.dumps(activity)
        self.assertEqual(activity["pending_total"], 2)
        self.assertIn("Shared topic", encoded)
        for secret in ("PRIVATE SUBJECT", "PRIVATE BODY", "PRIVATE-FILE", "data_base64", "token"):
            self.assertNotIn(secret, encoded)
        self.request("GET", f"/profiles/{profile['profile']['id']}/activity", self.b_token, expected=403)
        self.assertEqual(self.request("GET", "/inbox", sibling["token"])["messages"], [])

    def test_two_zcode_requests_to_codex_cannot_cross_replies_or_hide_pending_work(self):
        profile = self.profile()
        sibling = self.enroll(profile, "zcode-fork-lunar")
        request_a = self.request("POST", "/messages", self.a_token,
                                 {"to_instance_id": self.b_id, "subject": "Copilot controls below input",
                                  "text": "Extract six controls, order, visibility, measurements, actions, APK assets"}, 201)
        request_b = self.request("POST", "/messages", sibling["token"],
                                 {"to_instance_id": self.b_id, "subject": "Loading list rows",
                                  "text": "Inspect LoadingMessagesItem behavior"}, 201)
        self.request("POST", f"/messages/{request_a['id']}/reply", self.b_token,
                     {"text": "WRONG CONTEXT", "request_fingerprint": request_b["request_fingerprint"]}, 409)
        self.assertEqual(self.request("GET", "/inbox/summary", self.b_token)["pending"], 2)
        response_b = self.request("POST", f"/messages/{request_b['id']}/reply", self.b_token,
                                  {"text": "Rows: evidence from source", "request_fingerprint": request_b["request_fingerprint"]}, 201)
        self.assertEqual(response_b["reply"]["to_agent_id"], sibling["agent"]["id"])
        self.assertEqual(response_b["reply"]["thread_id"], request_b["thread_id"])
        self.assertEqual(response_b["reply"]["message_type"], "reply")
        self.assertEqual(response_b["reply"]["status"], "completed")
        remaining = self.request("GET", "/inbox", self.b_token)["messages"]
        self.assertEqual([row["id"] for row in remaining], [request_a["id"]])
        self.assertEqual(self.request("GET", "/inbox", self.a_token)["messages"], [])
        self.request("POST", f"/messages/{request_a['id']}/reply", sibling["token"],
                     {"text": "wrong instance", "request_fingerprint": request_a["request_fingerprint"]}, 404)
        response_a = self.request("POST", f"/messages/{request_a['id']}/reply", self.b_token,
                                  {"text": "Controls in order, observed dimensions and APK resources",
                                   "request_fingerprint": request_a["request_fingerprint"]}, 201)
        self.assertEqual(response_a["reply"]["to_agent_id"], self.a_id)
        self.assertEqual(len(self.request("GET", f"/threads/{request_a['id']}", self.b_token)["messages"]), 2)
        self.request("GET", f"/threads/{request_a['id']}", sibling["token"], expected=404)
        self.request("POST", f"/messages/{request_a['id']}/reply", self.b_token,
                     {"text": "duplicate", "request_fingerprint": request_a["request_fingerprint"]}, 409)

    def test_blocked_progress_and_filtered_mail_keep_request_visible(self):
        request = self.request("POST", "/messages", self.a_token,
                               {"to_instance_id": self.b_id, "subject": "APK resources", "text": "Inspect APK"}, 201)
        self.request("PATCH", f"/messages/{request['id']}/status", self.a_token,
                     {"status": "completed"}, 404)
        self.request("PATCH", f"/messages/{request['id']}/status", self.b_token,
                     {"status": "in_progress", "note": "Inspecting source"})
        result = self.request("POST", f"/messages/{request['id']}/reply", self.b_token,
                              {"status": "blocked", "text": "REA unavailable; source inspected; APK missing",
                               "request_fingerprint": request["request_fingerprint"]}, 201)
        self.assertFalse(result["acknowledged"])
        self.assertEqual(self.request("GET", "/inbox/summary", self.b_token)["pending"], 1)
        blocked = self.request("GET", "/inbox?status=blocked", self.b_token)["messages"]
        self.assertEqual(blocked[0]["subject"], "APK resources")
        self.assertTrue(blocked[0]["created_at_iso"].endswith("+00:00"))
        self.assertEqual(self.request("GET", "/outbox?status=blocked", self.a_token)["messages"][0]["id"], request["id"])

    def test_native_hook_config_binds_two_chats_and_keeps_credentials_out_of_context(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {"url": "https://mail.example", "profile_id": "p1", "enrollment_token": "private-key",
                      "mailbox_bank_dir": str(Path(directory) / "bank"),
                      "on_message_command": ["python", "mailbox_spool.py", "--bank-dir", "private-bank"]}
            path = Path(directory) / "zcode.json"
            def response(request, **kwargs):
                key = json.loads(request.data)["session_key"]
                return io.BytesIO(json.dumps({"agent": {"id": key}, "token": "private-token"}).encode())
            with patch("adapters.mailbox_hook.urlopen", side_effect=response) as mocked:
                first = bind_session(config, path, "zcode", "native-1")
                same = bind_session(config, path, "zcode", "native-1")
                other = bind_session(config, path, "zcode", "native-2")
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(first["agent_id"], same["agent_id"])
            self.assertNotEqual(first["agent_id"], other["agent_id"])
            saved = json.loads(Path(first["bound_config_path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["on_message_command"], config["on_message_command"])
            self.assertEqual(saved["mailbox_bank_dir"], config["mailbox_bank_dir"])
            context = format_context([], first)
            self.assertIn(first["agent_id"], context)
            self.assertIn("MASTER_PROMPT.md", context)
            self.assertNotIn("private-token", context)
            self.assertNotIn("private-key", context)

    def test_only_one_concurrent_final_reply_is_committed(self):
        message = self.request("POST", "/messages", self.a_token,
                               {"to_instance_id": self.b_id, "subject": "Exactly one final", "text": "Inspect this"}, 201)
        payload = json.dumps({"text": "Evidence-based result", "request_fingerprint": message["request_fingerprint"]}).encode()
        def send(_):
            req = Request(self.url + f"/messages/{message['id']}/reply", data=payload, method="POST",
                          headers={"Authorization": "Bearer " + self.b_token, "Content-Type": "application/json"})
            try:
                with urlopen(req, timeout=15) as response:
                    return response.status
            except HTTPError as error:
                code = error.code
                error.close()
                return code
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(send, range(4)))
        self.assertEqual(sorted(statuses), [201, 409, 409, 409])
        self.assertEqual(len(self.request("GET", "/inbox", self.a_token)["messages"]), 1)

    def test_remote_mcp_checks_and_replies_through_same_instance(self):
        def rpc(method, params=None):
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
            req = Request(self.url + "/mcp", data=json.dumps(payload).encode(), method="POST", headers={
                "Authorization": "Bearer " + self.b_token, "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"})
            with urlopen(req, timeout=15) as response:
                return json.load(response)
        initialized = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "mailbox-test", "version": "1"}})
        self.assertIn("instructions", initialized["result"])
        self.assertIn("REQUIRED FIRST STEP", initialized["result"]["instructions"])
        self.assertIn("/agent-setup.md", initialized["result"]["instructions"])
        tools = rpc("tools/list")["result"]["tools"]
        self.assertIn("mailbox_reply", [tool["name"] for tool in tools])
        check = rpc("tools/call", {"name": "mailbox_check", "arguments": {}})["result"]
        self.assertFalse(check.get("isError", False), check)
        checked = check.get("structuredContent") or json.loads(check["content"][0]["text"])
        self.assertEqual(checked["instance"]["id"], self.b_id)
        message = self.request("POST", "/messages", self.a_token,
                               {"to_instance_id": self.b_id, "subject": "MCP test", "text": "Investigate"}, 201)
        unbound = rpc("tools/call", {"name": "mailbox_send", "arguments": {
            "to_agent_id": self.a_id, "text": "unbound answer", "in_reply_to": message["id"]}})["result"]
        self.assertTrue(unbound.get("isError"), unbound)
        answer = rpc("tools/call", {"name": "mailbox_reply", "arguments": {
            "message_id": message["id"], "request_fingerprint": message["request_fingerprint"],
            "text": "Observed result", "status": "completed"}})["result"]
        self.assertFalse(answer.get("isError", False), answer)
        result = answer.get("structuredContent") or json.loads(answer["content"][0]["text"])
        self.assertEqual(result["reply"]["to_agent_id"], self.a_id)

    def test_human_guides_render_and_preserve_public_docs(self):
        for path in ("/workspace", "/guide/operations", "/guide/skill", "/guide/prompt", "/guide/mcp", "/guide/accounts", "/guide/local", "/guide/overview"):
            with urlopen(self.url + path, timeout=10) as response:
                text = response.read().decode("utf-8")
                self.assertIn("text/html", response.headers["Content-Type"])
                self.assertNotIn(self.admin, text)
                self.assertNotIn(self.b_token, text)
                self.assertIn("/app.css", text)


if __name__ == "__main__":
    unittest.main()
