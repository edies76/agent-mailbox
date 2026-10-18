"""Unit tests for host wake and lifecycle-hook adapters."""

import tempfile
import unittest
from pathlib import Path

from adapters.mailbox_hook import filter_new_messages, format_context, hook_response
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


if __name__ == "__main__":
    unittest.main()


