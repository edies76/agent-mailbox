"""Security boundaries for named control keys, and local file-bank isolation."""

import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
import shutil
import sqlite3
import sys
import http.cookiejar
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

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

    def test_operator_cannot_recover_or_rotate_an_existing_instance_by_session_id(self):
        body = {"name": "Owner instance", "session_key": "known-native-session"}
        owner_instance = self.request("POST", "/agents", self.admin, body, 201)
        operator = self.key("operator")["access_key"]
        self.request("POST", "/agents", operator, body, 409)
        self.request("POST", f"/dashboard/agents/{owner_instance['agent']['id']}/rotate-token",
                     operator, {}, 403, extra_headers={"X-Mailbox-Recovery-Secret": "r" * 43, "X-Mailbox-Expected-Epoch": "0"})
        resumed = self.request("POST", "/agents", self.admin, body, 201,
                               extra_headers={"X-Mailbox-Instance-Token": owner_instance["token"]})
        self.assertEqual(resumed["agent"]["id"], owner_instance["agent"]["id"])

    def test_rotation_rejects_reusing_registration_recovery_secret(self):
        secret = "r" * 43
        body = {"name": "Registered securely", "session_key": "registration-secret-session"}
        agent = self.request("POST", "/agents/secure", self.admin, body, 201,
                             extra_headers={"X-Mailbox-Recovery-Secret": secret})
        key = self.request("POST", "/agents/me/api-keys", agent["token"], {"name": "client"}, 201)["token"]
        self.request("POST", f"/dashboard/agents/{agent['agent']['id']}/rotate-token", self.admin, {}, 409,
                     extra_headers={"X-Mailbox-Recovery-Secret": secret, "X-Mailbox-Expected-Epoch": "0"})
        self.request("GET", "/inbox", key)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell is not installed")
    def test_registration_script_saves_and_reuses_private_recovery_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "register-agent.ps1"
            shutil.copy2(Path(__file__).parent / "register-agent.ps1", script)
            environment = dict(os.environ, MAILBOX_ADMIN_KEY=self.admin)
            command = [shutil.which("pwsh"), "-NoProfile", "-NonInteractive"]
            if os.name == "nt":
                command += ["-ExecutionPolicy", "Bypass"]
            first = subprocess.run(command + ["-File", str(script), "-Name", "Script agent", "-BaseUrl", self.url,
                                                "-SessionId", "script-native-session"], cwd=directory, env=environment,
                                   text=True, capture_output=True, timeout=45)
            self.assertEqual(first.returncode, 0, first.stderr)
            files = list((Path(directory) / ".local" / "agents").glob("*.json"))
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text(encoding="utf-8-sig"))
            self.assertTrue(saved.get("recovery_secret"))
            self.assertNotIn(saved["token"], first.stdout)
            second = subprocess.run(command + ["-File", str(script), "-Name", "Script agent", "-ExistingConfig", str(files[0])],
                                    cwd=directory, env=environment, text=True, capture_output=True, timeout=45)
            self.assertEqual(second.returncode, 0, second.stderr)
            resumed = json.loads(files[0].read_text(encoding="utf-8-sig"))
            self.assertEqual(resumed["agent_id"], saved["agent_id"])
            self.assertEqual(resumed["token"], saved["token"])

    def test_workflow_delivers_master_and_guides_have_real_assets(self):
        workflow = self.request("GET", "/workflow")
        self.assertIn("Take useful initiative", workflow["master_prompt"])
        self.assertNotIn("agent_setup_guide", workflow)  # Optional guide bodies are not fetched before consent.
        self.assertEqual(workflow["onboarding"]["guide_url"], "/agent-mailbox.md")
        self.assertEqual(workflow["setup_guide_url"], "/agent-setup.md")
        self.assertEqual(workflow["adapter_prompt_url"], "/adapter-build-prompt.md")
        dashboard = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        dashboard_js = (Path(__file__).parent / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("Siguiente paso obligatorio", dashboard)
        self.assertIn("new-agent-onboarding", dashboard)
        self.assertIn("result.onboarding.prompt", dashboard_js)
        self.assertIn("copy-agent-onboarding", dashboard_js)
        from urllib.request import urlopen
        for path in ("/", "/en", "/guide/start", "/guide/security", "/guide/brand", "/guide/agent-setup", "/guide/adapter-prompt", "/brand-symbol.svg", "/social-card.png", "/agent-setup.md", "/adapter-build-prompt.md", "/mailbox-listener.py", "/mailbox-watch.py", "/mailbox-hook.py", "/mailbox-wake.py", "/mailbox-spool.py"):
            with urlopen(self.url + path, timeout=10) as response:
                content = response.read()
                self.assertEqual(response.status, 200)
                self.assertNotIn(b"{{PUBLIC_ORIGIN}}", content)

    def test_public_pages_explain_mcp_requirements_without_private_chat_examples(self):
        from urllib.request import urlopen
        for path in ("/", "/en", "/use-cases", "/guide/mcp"):
            with urlopen(self.url + path, timeout=10) as response:
                page = response.read().decode("utf-8")
            self.assertIn("Streamable HTTP", page)
            self.assertIn("Bearer", page)
            self.assertNotIn("Herramientas debajo del input", page)
            self.assertNotIn("Comportamiento de las filas de carga", page)
        with urlopen(self.url + "/", timeout=10) as response:
            home = response.read().decode("utf-8")
        self.assertIn("ChatGPT web requiere OAuth", home)
        self.assertIn("Conectar un cliente MCP", home)

    def test_dashboard_auth_screen_covers_viewport_and_locks_background_scroll(self):
        from urllib.request import urlopen
        with urlopen(self.url + "/dashboard", timeout=10) as response:
            page = response.read().decode("utf-8")
        with urlopen(self.url + "/app.css", timeout=10) as response:
            css = response.read().decode("utf-8")
        with urlopen(self.url + "/dashboard.js", timeout=10) as response:
            script = response.read().decode("utf-8")
        self.assertIn('class="login-overlay" id="login" hidden', page)
        self.assertIn("body.auth-locked{overflow:hidden}", css)
        self.assertIn(".login-overlay{position:fixed;inset:0", css)
        self.assertIn("classList.add('auth-locked')", script)
        self.assertIn("classList.remove('auth-locked')", script)

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
            source = Path(__file__).parent
            for filename in ("agent-mailbox.md", "SKILL.md", "AGENT-INSTRUCTIONS.md"):
                self.assertTrue((bank / filename).exists())
            self.assertEqual((bank / "agent-mailbox.md").read_text(encoding="utf-8"),
                             (source / "agent-mailbox.md").read_text(encoding="utf-8-sig"))

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


class AccountSessionTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    setUp = http_tests.MailboxTests.setUp
    request = http_tests.MailboxTests.request

    def browser_request(self, opener, path, method="GET", body=None, headers=None, expected=200):
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            with opener.open(request, timeout=20) as response:
                status, data = response.status, json.load(response)
        except HTTPError as error:
            with error:
                status, data = error.code, json.load(error)
        self.assertEqual(status, expected, data)
        return data

    def test_owner_setup_cookie_sessions_csrf_and_agent_handoff(self):
        cookies = http.cookiejar.CookieJar()
        browser = build_opener(HTTPCookieProcessor(cookies))
        status = self.browser_request(browser, "/auth/status")
        self.assertTrue(status["setup_required"])
        self.assertFalse(status["local_setup_available"])
        self.browser_request(browser, "/auth/setup", "POST", {
            "username": "MailboxOwner", "password": "local-password-that-is-long-42"}, expected=401)
        setup = self.browser_request(browser, "/auth/setup", "POST", {
            "setup_key": self.admin, "username": "MailboxOwner", "password": "local-password-that-is-long-42"}, expected=201)
        self.assertEqual(setup["user"]["username"], "MailboxOwner")
        self.browser_request(browser, "/auth/setup", "POST", {
            "setup_key": self.admin, "username": "SecondOwner", "password": "another-password-long-enough-44"}, expected=409)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            stored_password = db.execute("SELECT password_hash FROM account_users WHERE id = ?",
                                         (setup["user"]["id"],)).fetchone()[0]
        self.assertTrue(stored_password.startswith("argon2id$v=19$m=19456,t=2,p=1$"))
        self.assertNotIn("local-password-that-is-long-42", stored_password)
        raw_session = next(cookie.value for cookie in cookies if "session" in cookie.name)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            stored_session = db.execute("SELECT token_hash FROM account_sessions WHERE user_id = ?",
                                        (setup["user"]["id"],)).fetchone()[0]
        self.assertNotEqual(raw_session, stored_session)
        self.assertEqual(hashlib.sha256(raw_session.encode()).hexdigest(), stored_session)
        self.assertFalse(self.browser_request(browser, "/auth/status")["setup_required"])
        session = self.browser_request(browser, "/auth/session")
        self.assertEqual(session["user"]["username"], "MailboxOwner")
        self.assertNotIn("token", json.dumps(session))
        logged_in = self.browser_request(browser, "/auth/login", "POST", {
            "username": "mailboxowner", "password": "local-password-that-is-long-42"})
        self.assertEqual(logged_in["user"]["username"], "MailboxOwner")

        self.browser_request(browser, "/dashboard/accounts", "POST", {
            "username": "no-csrf", "password": "a-long-valid-password-42", "role": "observer"}, expected=403)
        csrf = next(cookie.value for cookie in cookies if "csrf" in cookie.name)
        headers = {"X-CSRF-Token": csrf}
        operator = self.browser_request(browser, "/dashboard/accounts", "POST", {
            "username": "OperatorOne", "password": "operator-password-long-42", "role": "operator"}, headers=headers, expected=201)
        self.assertEqual(operator["role"], "operator")
        agent = self.browser_request(browser, "/agents", "POST", {
            "name": "HTTP agent", "kind": "codex", "project": "test"}, headers=headers, expected=201)
        self.assertTrue(agent["token"])
        self.assertEqual(self.request("GET", "/inbox/summary", agent["token"])["instance"]["id"], agent["agent"]["id"])

        operator_cookies = http.cookiejar.CookieJar()
        operator_browser = build_opener(HTTPCookieProcessor(operator_cookies))
        self.browser_request(operator_browser, "/auth/login", "POST", {
            "username": "operatorone", "password": "operator-password-long-42"})
        operator_dashboard = self.browser_request(operator_browser, "/dashboard/data")
        self.assertTrue(operator_dashboard["permissions"]["can_manage_agents"])
        self.assertFalse(operator_dashboard["permissions"]["can_manage_users"])
        self.browser_request(operator_browser, "/dashboard/accounts", expected=403)
        self.browser_request(browser, f"/dashboard/accounts/{operator['id']}", "DELETE", headers=headers)
        self.browser_request(operator_browser, "/dashboard/data", expected=401)

        self.browser_request(browser, "/auth/password", "POST", {
            "current_password": "local-password-that-is-long-42",
            "new_password": "owner-password-rotated-and-long-43"}, headers=headers)
        headers = {"X-CSRF-Token": next(cookie.value for cookie in cookies if "csrf" in cookie.name)}
        rejected_browser = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.browser_request(rejected_browser, "/auth/login", "POST", {
            "username": "MailboxOwner", "password": "local-password-that-is-long-42"}, expected=401)
        for _ in range(6):
            self.browser_request(rejected_browser, "/auth/login", "POST", {
                "username": "unknown-login", "password": "definitely-the-wrong-password"}, expected=401)
        self.browser_request(rejected_browser, "/auth/login", "POST", {
            "username": "unknown-login", "password": "another-wrong-password"}, expected=429)

        self.browser_request(browser, "/auth/logout", "POST", {}, headers=headers)
        self.browser_request(browser, "/auth/session", expected=401)
        self.request("POST", "/auth/recover", self.admin,
                     {"username": "MailboxOwner", "new_password": "owner-password-recovered-and-long-44"})
        recovered_browser = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        recovered = self.browser_request(recovered_browser, "/auth/login", "POST", {
            "username": "MailboxOwner", "password": "owner-password-recovered-and-long-44"})
        self.assertEqual(recovered["user"]["role"], "owner")
        self.request("GET", "/dashboard/data", self.admin)

class AccountRegistrationKeyTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    setUp = http_tests.MailboxTests.setUp
    request = http_tests.MailboxTests.request

    def browser_request(self, opener, path, method="GET", body=None, headers=None, expected=200):
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            with opener.open(request, timeout=20) as response:
                status, data = response.status, json.load(response)
        except HTTPError as error:
            with error:
                status, data = error.code, json.load(error)
        self.assertEqual(status, expected, data)
        return data

    def test_account_profile_registration_key_is_scoped_and_rotatable(self):
        cookies = http.cookiejar.CookieJar()
        browser = build_opener(HTTPCookieProcessor(cookies))
        self.browser_request(browser, "/auth/setup", "POST", {
            "setup_key": self.admin, "username": "ProfileOwner", "password": "profile-owner-password-42"}, expected=201)
        csrf = next(cookie.value for cookie in cookies if "csrf" in cookie.name)
        headers = {"X-CSRF-Token": csrf}
        profile = self.browser_request(browser, "/account/profile")
        self.assertIsNone(profile["registration_key"])
        created = self.browser_request(browser, "/account/registration-key", "POST", {}, headers=headers)
        registration_key = created["registration_key"]
        self.assertTrue(registration_key.startswith("amr_"))
        self.assertNotIn(registration_key, json.dumps(self.browser_request(browser, "/account/profile")))
        agent = self.request("POST", "/join/secure", registration_key,
                            {"name": "Profile agent", "kind": "codex", "session_key": "profile-session"}, 201)
        directory = self.request("GET", "/agents", registration_key)
        self.assertIn(agent["agent"]["id"], {row["id"] for row in directory["agents"]})
        old_key = registration_key
        rotated = self.browser_request(browser, "/account/registration-key", "POST", {}, headers=headers)["registration_key"]
        self.assertNotEqual(old_key, rotated)
        self.request("POST", "/join/secure", old_key,
                     {"name": "Should fail", "kind": "other", "session_key": "old-key-session"}, 401)

    def test_public_signup_creates_a_private_workspace_without_install_key(self):
        owner_cookies = http.cookiejar.CookieJar()
        owner = build_opener(HTTPCookieProcessor(owner_cookies))
        status = self.browser_request(owner, "/auth/status")
        if status["setup_required"]:
            self.browser_request(owner, "/auth/setup", "POST", {
                "setup_key": self.admin, "username": "SignupOwner", "password": "signup-owner-password-42"}, expected=201)
        else:
            self.browser_request(owner, "/auth/login", "POST", {
                "username": "ProfileOwner", "password": "profile-owner-password-42"}, expected=200)
        signup_cookies = http.cookiejar.CookieJar()
        signup = build_opener(HTTPCookieProcessor(signup_cookies))
        created = self.browser_request(signup, "/auth/signup", "POST", {
            "username": "NewWorkspaceUser", "password": "new-workspace-password-42", "workspace_name": "Independent Agents"}, expected=201)
        self.assertEqual(created["user"]["role"], "workspace_owner")
        profile = self.browser_request(signup, "/account/profile")
        self.assertEqual(profile["account"]["username"], "NewWorkspaceUser")
        self.assertEqual(profile["workspace"]["name"], "Independent Agents")


class LocalBootstrapTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request

    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"MAILBOX_STORAGE": "sqlite", "MAILBOX_LOCAL_FIRST_RUN": "1"}):
            http_tests.MailboxTests.setUpClass.__func__(cls)

    def setup_request(self, body, origin=None, expected=201):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = Request(self.url + "/auth/setup", data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=20) as response:
                status, result = response.status, json.load(response)
        except HTTPError as error:
            with error:
                status, result = error.code, json.load(error)
        self.assertEqual(status, expected, result)
        return result

    def browser_request(self, opener, path, method="GET", body=None, headers=None, expected=200):
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            with opener.open(request, timeout=20) as response:
                status, data = response.status, json.load(response)
        except HTTPError as error:
            with error:
                status, data = error.code, json.load(error)
        self.assertEqual(status, expected, data)
        return data

    def test_only_a_matching_loopback_origin_can_claim_the_local_owner(self):
        body = {"username": "LocalOwner", "password": "local-only-password-long-43"}
        with urlopen(self.url + "/auth/status", timeout=10) as response:
            status = json.load(response)
        self.assertTrue(status["setup_required"])
        self.assertTrue(status["local_setup_available"])
        self.setup_request(body, origin="https://remote.example", expected=401)
        self.setup_request(body, expected=401)
        self.assertTrue(self.setup_request(body, origin=self.url)["user"]["username"] == "LocalOwner")

    def test_previous_control_key_can_exchange_for_a_revocable_browser_session(self):
        legacy = self.request("POST", "/dashboard/users", self.admin,
                              {"name": "Old observer", "role": "observer"}, 201)
        cookies = http.cookiejar.CookieJar()
        browser = build_opener(HTTPCookieProcessor(cookies))
        result = self.browser_request(browser, "/auth/key-login", "POST",
                                      {"access_key": legacy["access_key"]})
        self.assertEqual(result["user"]["role"], "observer")
        self.assertEqual(result["auth_method"], "access_key")
        self.assertNotIn(legacy["access_key"], json.dumps(result))
        dashboard = self.browser_request(browser, "/dashboard/data")
        self.assertFalse(dashboard["permissions"]["can_read_message_content"])
        self.assertFalse(dashboard["permissions"]["can_manage_agents"])

        self.request("DELETE", f"/dashboard/users/{legacy['user']['id']}", self.admin)
        self.browser_request(browser, "/dashboard/data", expected=401)

        owner_session = self.browser_request(browser, "/auth/key-login", "POST",
                                             {"access_key": self.admin})
        self.assertEqual(owner_session["user"]["role"], "owner")
        self.assertNotIn(self.admin, json.dumps(owner_session))
        owner_dashboard = self.browser_request(browser, "/dashboard/data")
        self.assertTrue(owner_dashboard["permissions"]["can_manage_users"])


if __name__ == "__main__":
    unittest.main()
