"""Focused tests for adapters/dsh_notice.py: exact routing, duplicates, dry run.

Run with:  python -m unittest -v test_dsh_notice.py
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADAPTER = Path(__file__).resolve().parent / "adapters" / "dsh_notice.py"
BOUND_AGENT = "11111111-2222-3333-4444-555555555555"
OTHER_AGENT = "99999999-8888-7777-6666-555555555555"
SESSION_ID = "session-6f3b0db7-6c73-4ddc-ba92-a2716c97d097"
TOKEN = "SECRET-TOKEN-DO-NOT-PRINT-0123456789"


def message(**overrides):
    payload = {
        "id": 42,
        "from_agent_id": OTHER_AGENT,
        "to_agent_id": BOUND_AGENT,
        "subject": "Permisos del componente Copilot",
        "text": "Investiga el orden de los controles bajo el input y devuelve evidencia.",
        "thread_id": "thread-abc",
        "request_fingerprint": "fingerprint-xyz",
        "message_type": "request",
        "status": "queued",
        "created_at": 1792720000,
        "created_at_iso": "2026-10-22T20:00:00Z",
        "attachments": [],
    }
    payload.update(overrides)
    return payload


class DshNoticeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.notify_dir = self.root / "notices"
        self.config_path = self.root / "dsh.json"
        self.config_path.write_text(json.dumps({
            "url": "https://mailbox.example.invalid",
            "agent_id": BOUND_AGENT,
            "token": TOKEN,
            "session_id": SESSION_ID,
            "notify_dir": str(self.notify_dir),
        }), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_adapter(self, payload, *extra):
        completed = subprocess.run(
            [sys.executable, str(ADAPTER), "--config", str(self.config_path), *extra],
            input=json.dumps(payload, ensure_ascii=False),
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        return completed

    def delivered_ids(self):
        state = self.notify_dir / "state.json"
        if not state.exists():
            return []
        return json.loads(state.read_text(encoding="utf-8"))["delivered"]

    def test_exact_routing_delivers_notice_for_bound_instance(self):
        result = self.run_adapter(message())
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["status"], "delivered")
        self.assertEqual(status["session_id"], SESSION_ID)
        self.assertEqual(self.delivered_ids(), [42])

        event = self.notify_dir / "events" / f"{42:020d}.json"
        record = json.loads(event.read_text(encoding="utf-8"))
        self.assertEqual(record["id"], 42)
        self.assertEqual(record["thread_id"], "thread-abc")
        self.assertEqual(record["request_fingerprint"], "fingerprint-xyz")
        self.assertEqual(record["subject"], "Permisos del componente Copilot")
        self.assertIn("Investiga el orden", record["text"])
        self.assertEqual(record["_notice"]["session_id"], SESSION_ID)
        self.assertEqual(record["_notice"]["instance_id"], BOUND_AGENT)

    def test_other_recipient_is_refused_and_leaves_no_notice(self):
        result = self.run_adapter(message(to_agent_id=OTHER_AGENT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "rejected-recipient")
        self.assertEqual(self.delivered_ids(), [])
        self.assertFalse((self.notify_dir / "events").exists())
        self.assertFalse((self.notify_dir / "PENDING.md").exists())

    def test_duplicate_message_is_not_recorded_twice(self):
        first = self.run_adapter(message())
        self.assertEqual(json.loads(first.stdout)["status"], "delivered")
        event = self.notify_dir / "events" / f"{42:020d}.json"
        first_bytes = event.read_bytes()

        second = self.run_adapter(message(subject="Permisos del componente Copilot"))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["status"], "duplicate")
        self.assertEqual(event.read_bytes(), first_bytes)
        self.assertEqual(self.delivered_ids(), [42])
        index = json.loads((self.notify_dir / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(index["entries"]), 1)

    def test_dry_run_writes_nothing(self):
        result = self.run_adapter(message(), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertTrue(status["dry_run"])
        self.assertFalse(self.notify_dir.exists())

    def test_output_never_contains_the_token(self):
        for payload, extra in ((message(), ()), (message(), ("--dry-run",)),
                               (message(to_agent_id=OTHER_AGENT), ())):
            result = self.run_adapter(payload, *extra)
            self.assertNotIn(TOKEN, result.stdout)
            self.assertNotIn(TOKEN, result.stderr)

    def test_notice_index_carries_no_body(self):
        self.run_adapter(message())
        index = (self.notify_dir / "PENDING.md").read_text(encoding="utf-8")
        self.assertIn("message 42", index)
        self.assertNotIn("Investiga el orden", index)

    def test_message_without_id_is_refused(self):
        result = self.run_adapter(message(id=None))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.notify_dir / "events").exists())

    def test_adapter_cannot_ack_or_reply(self):
        source = ADAPTER.read_text(encoding="utf-8")
        for forbidden in ("urllib", "requests", "socket", "http.client", "subprocess"):
            self.assertNotIn(forbidden, source,
                             f"the adapter must stay a local file writer: found {forbidden!r}")


if __name__ == "__main__":
    unittest.main()
