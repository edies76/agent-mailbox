"""Security boundaries for named control keys, and local file-bank isolation."""

import base64
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_mailbox as http_tests
from adapters.mailbox_spool import initialize_bank, mirror
from mailbox_listener import WindowsJob, drain_output, load_manifest


class ControlAccessTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp

    def key(self, role):
        return self.request("POST", "/dashboard/users", self.admin,
                            {"name": "Test " + role, "role": role}, 201)

    def test_control_roles_redact_deny_messaging_and_revoke(self):
        sent = self.request("POST", "/messages", self.a_token,
                            {"to_instance_id": self.b_id, "subject": "PRIVATE SUBJECT", "text": "PRIVATE BODY",
                             "attachments": [{"filename": "PRIVATE-FILE.txt", "media_type": "text/plain",
                                              "data_base64": base64.b64encode(b"private bytes").decode()}]}, 201)
        observer, operator = self.key("observer"), self.key("operator")
        viewer = observer["access_key"]
        operator_key = operator["access_key"]
        observed = self.request("GET", "/dashboard/data", viewer)
        from urllib.request import Request, urlopen
        with urlopen(Request(self.url + "/dashboard/data", headers={"Authorization": "Bearer " + viewer}), timeout=10) as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        encoded = json.dumps(observed)
        for secret in ("PRIVATE SUBJECT", "PRIVATE BODY", "PRIVATE-FILE", "private bytes"):
            self.assertNotIn(secret, encoded)
        self.assertFalse(observed["permissions"]["can_manage_agents"])
        operated = self.request("GET", "/dashboard/data", operator_key)
        self.assertIn("PRIVATE BODY", json.dumps(operated))
        self.assertFalse(operated["permissions"]["can_delete_messages"])
        for token in (viewer, operator_key):
            for path in ("/agents/me", "/inbox", f"/messages/{sent['id']}"):
                self.request("GET", path, token, expected=403)
            self.request("GET", "/dashboard/users", token, expected=403)
            self.request("GET", "/dashboard/audit", token, expected=403)
            self.request("POST", "/messages", token, {"to_instance_id": self.a_id, "text": "no"}, 403)
            self.request("DELETE", f"/messages/{sent['id']}", token, expected=404)
        self.request("DELETE", f"/agents/{self.a_id}", viewer, expected=403)
        self.request("POST", "/agents", viewer, {"name": "denied"}, 403)
        extra = self.request("POST", "/agents", operator_key, {"name": "operator fixture"}, 201)
        self.request("DELETE", f"/agents/{extra['agent']['id']}", operator_key)
        rows = self.request("GET", "/dashboard/users", self.admin)
        encoded = json.dumps(rows)
        self.assertNotIn(viewer, encoded)
        self.assertNotIn(operator_key, encoded)
        self.assertNotIn("token_hash", encoded)
        self.request("DELETE", f"/dashboard/users/{observer['user']['id']}", self.admin)
        self.request("GET", "/dashboard/data", viewer, expected=401)
        audit = self.request("GET", "/dashboard/audit", self.admin)["events"]
        self.assertTrue(any(row["action"] == "user_key_revoked" for row in audit))
        self.request("DELETE", f"/dashboard/users/{operator['user']['id']}", self.admin)

    def test_control_key_and_revocation_survive_restart(self):
        user = self.key("observer")
        self.stop_server()
        self.start_server()
        self.request("GET", "/dashboard/data", user["access_key"])
        self.request("DELETE", f"/dashboard/users/{user['user']['id']}", self.admin)
        self.stop_server()
        self.start_server()
        self.request("GET", "/dashboard/data", user["access_key"], expected=401)

    def test_workflow_delivers_master_and_guides_have_real_assets(self):
        workflow = self.request("GET", "/workflow")
        self.assertIn("Take useful initiative", workflow["master_prompt"])
        from urllib.request import urlopen
        for path in ("/", "/en", "/guide/start", "/guide/security", "/guide/brand", "/brand-symbol.svg", "/social-card.png"):
            with urlopen(self.url + path, timeout=10) as response:
                content = response.read()
                self.assertEqual(response.status, 200)
                self.assertNotIn(b"{{PUBLIC_ORIGIN}}", content)

    def test_public_origin_overrides_the_proxy_internal_scheme(self):
        from urllib.request import urlopen
        self.stop_server()
        try:
            with patch.dict(os.environ, {"MAILBOX_PUBLIC_URL": "https://mail.example"}):
                self.start_server()
            with urlopen(self.url, timeout=10) as response:
                content = response.read().decode("utf-8")
            self.assertIn('href="https://mail.example/"', content)
            self.assertIn('content="https://mail.example/social-card.png"', content)
        finally:
            self.stop_server()
            self.start_server()


class FileBankTests(unittest.TestCase):
    def test_file_bank_callback_reads_utf8_over_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            message = {"id": 19, "to_agent_id": "chat-unicode", "from_agent_id": "peer",
                       "text": "Prueba — “Android” 日本語"}
            result = subprocess.run([sys.executable, "adapters/mailbox_spool.py", "--bank-dir", directory],
                                    input=json.dumps(message, ensure_ascii=False), text=True,
                                    encoding="utf-8", capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            stored = json.loads((Path(directory) / "chat-unicode/events/00000000000000000019.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["text"], message["text"])

    @unittest.skipUnless(os.name == "nt", "Windows job ownership")
    def test_windows_job_closes_its_owned_worker(self):
        process = subprocess.Popen([sys._base_executable, "-c", "import time; time.sleep(60)"],
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        job = WindowsJob()
        try:
            job.assign(process)
            job.close()
            self.assertIsNotNone(process.wait(timeout=5))
        finally:
            job.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_mirror_separates_instances_and_does_not_copy_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            bank = Path(directory)
            initialize_bank(bank)
            for recipient in ("chat-A", "chat-B"):
                result = mirror(bank, {"id": 17, "to_agent_id": recipient, "from_agent_id": "peer",
                                       "text": "UNTRUSTED BODY", "token": "DO-NOT-COPY",
                                       "attachments": [{"filename": "scope.txt", "data_base64": "DO-NOT-COPY"}]})
                self.assertEqual(result["message_id"], 17)
                stored = (bank / recipient / "events/00000000000000000017.json").read_text(encoding="utf-8")
                self.assertIn("UNTRUSTED BODY", stored)
                self.assertNotIn("DO-NOT-COPY", stored)
                self.assertNotIn("UNTRUSTED BODY", (bank / recipient / "INDEX.md").read_text(encoding="utf-8"))
            self.assertTrue((bank / "MASTER_PROMPT.md").exists())

    def test_supervisor_redacts_message_and_arbitrary_adapter_output(self):
        class Process:
            stdout = io.StringIO('\n'.join([
                json.dumps({"type": "mailbox_message", "message": {"id": 7, "text": "SECRET", "subject": "SECRET"}}),
                json.dumps({"type": "adapter_custom", "text": "SECRET", "token": "SECRET"}),
                "SECRET non-json output",
            ]))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            drain_output(Process(), {"name": "test", "agent_id": "chat-A"})
        self.assertNotIn("SECRET", stream.getvalue())
        self.assertIn('"message_id": 7', stream.getvalue())

    def test_manifest_rejects_duplicate_mailbox_and_has_independent_cursors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, identifier in (("a", "chat-A"), ("b", "chat-B")):
                (root / f"{name}.json").write_text(json.dumps({"url": "https://example.invalid", "agent_id": identifier, "token": "private"}))
            manifest = root / "listener.json"
            manifest.write_text(json.dumps({"version": 1, "instances": [{"config": "a.json"}, {"config": "b.json"}]}))
            specs = load_manifest(manifest)
            self.assertEqual(len({str(row["state"]) for row in specs.values()}), 2)
            (root / "b.json").write_text((root / "a.json").read_text())
            with self.assertRaises(ValueError):
                load_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
