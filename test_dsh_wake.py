"""Focused tests for adapters/dsh_wake.py: wake channel, routing, duplicates.

Run with:  python -m unittest -v test_dsh_wake.py
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADAPTER = Path(__file__).resolve().parent / "adapters" / "dsh_wake.py"
BOUND_AGENT = "11111111-2222-3333-4444-555555555555"
OTHER_AGENT = "99999999-8888-7777-6666-555555555555"
SESSION_ID = "session-6f3b0db7-6c73-4ddc-ba92-a2716c97d097"
TOKEN = "SECRET-TOKEN-DO-NOT-PRINT-0123456789"


def message(**overrides):
    payload = {
        "id": 191,
        "from_agent_id": OTHER_AGENT,
        "to_agent_id": BOUND_AGENT,
        "subject": "Evidencia del APK",
        "text": "Adjunto el resultado del analisis.",
        "thread_id": "thread-wake",
        "request_fingerprint": "fingerprint-wake",
        "message_type": "request",
        "status": "queued",
        "delivery_mode": "wake",
        "wake_backlog": False,
        "created_at": 1792720000,
        "created_at_iso": "2026-10-22T22:40:00Z",
    }
    payload.update(overrides)
    return payload


class DshWakeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.wake_dir = self.root / "wake"
        self.config_path = self.root / "dsh.json"
        self.config_path.write_text(json.dumps({
            "url": "https://mailbox.example.invalid",
            "agent_id": BOUND_AGENT,
            "token": TOKEN,
            "session_id": SESSION_ID,
            "wake_dir": str(self.wake_dir),
        }), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_adapter(self, payload, *extra):
        return subprocess.run(
            [sys.executable, str(ADAPTER), "--config", str(self.config_path), *extra],
            input=json.dumps(payload, ensure_ascii=False),
            text=True, encoding="utf-8", capture_output=True, check=False,
        )

    def recorded_ids(self):
        state = self.wake_dir / "state.json"
        if not state.exists():
            return []
        return json.loads(state.read_text(encoding="utf-8"))["recorded"]

    def test_wake_message_is_recorded_with_channel_marker(self):
        result = self.run_adapter(message())
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["status"], "wake-recorded")
        self.assertEqual(status["session_id"], SESSION_ID)
        self.assertEqual(self.recorded_ids(), [191])

        marker = json.loads((self.wake_dir / "events" / f"{191:020d}.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["id"], 191)
        self.assertEqual(marker["thread_id"], "thread-wake")
        self.assertEqual(marker["request_fingerprint"], "fingerprint-wake")
        self.assertTrue(marker["subject"].startswith("[WAKE] "))
        self.assertEqual(marker["_wake"]["channel"], "delivery_mode=wake")
        self.assertEqual(marker["_wake"]["session_id"], SESSION_ID)

    def test_ordinary_message_never_opens_the_wake_channel(self):
        result = self.run_adapter(message(delivery_mode="inbox"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "not-a-wake")
        self.assertEqual(self.recorded_ids(), [])
        self.assertFalse((self.wake_dir / "events").exists())

    def test_legacy_message_without_delivery_mode_is_not_a_wake(self):
        payload = message()
        payload.pop("delivery_mode")
        result = self.run_adapter(payload)
        self.assertEqual(json.loads(result.stdout)["status"], "not-a-wake")

    def test_other_recipient_is_refused(self):
        result = self.run_adapter(message(to_agent_id=OTHER_AGENT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "rejected-recipient")
        self.assertEqual(self.recorded_ids(), [])
        self.assertFalse((self.wake_dir / "events").exists())

    def test_duplicate_wake_is_not_recorded_twice(self):
        first = self.run_adapter(message())
        self.assertEqual(json.loads(first.stdout)["status"], "wake-recorded")
        marker = self.wake_dir / "events" / f"{191:020d}.json"
        first_bytes = marker.read_bytes()
        second = self.run_adapter(message())
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["status"], "duplicate")
        self.assertEqual(marker.read_bytes(), first_bytes)
        self.assertEqual(self.recorded_ids(), [191])

    def test_dry_run_writes_nothing(self):
        result = self.run_adapter(message(), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["dry_run"])
        self.assertFalse(self.wake_dir.exists())

    def test_output_never_contains_the_token(self):
        for payload, extra in ((message(), ()), (message(), ("--dry-run",)),
                               (message(delivery_mode="inbox"), ())):
            result = self.run_adapter(payload, *extra)
            self.assertNotIn(TOKEN, result.stdout)
            self.assertNotIn(TOKEN, result.stderr)

    def test_wake_marker_keeps_the_body_as_data(self):
        self.run_adapter(message())
        marker = json.loads((self.wake_dir / "events" / f"{191:020d}.json").read_text(encoding="utf-8"))
        self.assertIn("Adjunto el resultado", marker["text"])

    def test_adapter_cannot_ack_reply_or_reach_the_network(self):
        source = ADAPTER.read_text(encoding="utf-8")
        for forbidden in ("urllib", "requests", "socket", "http.client", "subprocess"):
            self.assertNotIn(forbidden, source,
                             f"the wake adapter must stay a local file writer: found {forbidden!r}")


if __name__ == "__main__":
    unittest.main()
