"""Unit tests for host wake and lifecycle-hook adapters."""

import tempfile
import unittest
import io
import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from adapters.mailbox_hook import filter_new_messages, format_context, hook_response, get_pending
from adapters.mailbox_wake import build_command, build_prompt


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.message = {
            "id": 42,
            "from_agent_id": "sender-1",
            "text": "Please inspect this change",
            "attachments": [],
        }

    def test_mailbox_prompt_labels_external_content_as_untrusted(self):
        prompt = build_prompt(self.message)
        self.assertIn("untrusted user content", prompt)
        self.assertIn('"id":42', prompt)
        self.assertIn("in_reply_to", prompt)

    def test_native_session_commands_use_session_specific_queues(self):
        wake = {"enabled": True, "provider": "codex", "session_id": "thread-abc"}
        command = build_command(wake, self.message)
        self.assertEqual(command[:4], ["codex", "queue", "--thread", "thread-abc"])
        self.assertEqual(command[4], "--message")

    def test_cli_resume_adapters_keep_the_host_session(self):
        claude = build_command({"enabled": True, "provider": "claude", "session_id": "claude-session"}, self.message)
        grok = build_command({"enabled": True, "provider": "grok", "session_id": "grok-session",
                              "cwd": "C:/work"}, self.message)
        self.assertIn("claude-session", claude)
        self.assertIn("grok-session", grok)
        self.assertNotIn("--dangerously-skip-permissions", claude)
        self.assertNotIn("--always-approve", grok)

    def test_claude_and_grok_can_resume_the_latest_session_in_a_workspace(self):
        claude = build_command({"enabled": True, "provider": "claude", "cwd": "C:/work"}, self.message)
        grok = build_command({"enabled": True, "provider": "grok", "cwd": "C:/work"}, self.message)
        self.assertIn("--continue", claude)
        self.assertIn("-c", grok)

    def test_wake_requires_explicit_opt_in_and_a_session(self):
        with self.assertRaises(ValueError):
            build_command({"provider": "codex", "session_id": "thread-abc"}, self.message)
        with self.assertRaises(ValueError):
            build_command({"enabled": True, "provider": "codex"}, self.message)

    def test_hook_injects_at_prompt_and_continues_only_when_enabled(self):
        output = hook_response("zcode", "UserPromptSubmit", {}, [self.message], {})
        self.assertIn("additionalContext", output["hookSpecificOutput"])
        self.assertIsNone(hook_response("claude", "Stop", {}, [self.message], {}))
        self.assertIsNone(hook_response("claude", "Stop", {"stop_hook_active": True}, [self.message],
                                       {"auto_continue_on_stop": True}))
        output = hook_response("claude", "Stop", {}, [self.message], {"auto_continue_on_stop": True})
        self.assertEqual(output["decision"], "block")

    def test_hook_delivery_ledger_suppresses_duplicates_until_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg_path = Path(directory) / "agent.json"
            cfg_path.write_text("{}", encoding="utf-8")
            config = {}
            self.assertEqual(filter_new_messages(config, cfg_path, "session-1", [self.message]), [self.message])
            self.assertEqual(filter_new_messages(config, cfg_path, "session-1", [self.message]), [])
            self.assertEqual(filter_new_messages(config, cfg_path, "session-2", [self.message]), [self.message])
            self.assertEqual(filter_new_messages(config, cfg_path, "session-1", []), [])
            self.assertEqual(filter_new_messages(config, cfg_path, "session-1", [self.message]), [self.message])
            self.assertEqual(filter_new_messages(config, cfg_path, "session-1", [self.message], reset=True), [self.message])

    def test_hook_reads_all_pages_even_when_old_requests_remain_blocked(self):
        requests = []
        def response(request, **kwargs):
            cursor = int(parse_qs(urlsplit(request.full_url).query)["after_id"][0])
            requests.append(cursor)
            page = ({"messages": [{"id": i, "status": "blocked"} for i in range(1, 21)],
                     "has_more": True, "next_after_id": 20} if cursor == 0 else
                    {"messages": [{"id": 21, "status": "queued"}], "has_more": False, "next_after_id": 21})
            return io.BytesIO(json.dumps(page).encode())
        with patch("adapters.mailbox_hook.urlopen", side_effect=response):
            pending = get_pending({"url": "https://mail.example", "token": "private"})
        self.assertEqual(requests, [0, 20])
        self.assertEqual([item["id"] for item in pending], list(range(1, 22)))

    def test_bound_hook_uses_short_preview_and_requires_reading_every_page(self):
        messages = [{"id": i, "from_agent_id": "sender", "text": "x" * 10000, "attachments": []}
                    for i in range(1, 101)]
        context = format_context(messages, {"agent_id": "instance", "bound_config_path": "private.json",
                                            "client_script": "client.py"})
        self.assertIn("100 pending messages", context)
        self.assertIn('"id":100', context)
        self.assertIn("read EVERY page", context)
        self.assertNotIn("x" * 1000, context)
        self.assertLess(len(context), 8000)


if __name__ == "__main__":
    unittest.main()


