"""Integration tests against a real HTTP process and an on-disk database."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import base64
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class MailboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.directory.name) / "test.sqlite3")
        cls.admin = secrets.token_urlsafe(32)
        cls.join_key = secrets.token_urlsafe(16)
        cls.log = open(Path(cls.directory.name) / "server.log", "w+")
        cls.start_server()

    @classmethod
    def start_server(cls):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.url = f"http://127.0.0.1:{port}"
        env = dict(os.environ, MAILBOX_ADMIN_KEY=cls.admin, MAILBOX_JOIN_KEY=cls.join_key, MAILBOX_DB=cls.db_path,
                   MAILBOX_ONLINE_SECONDS="90")
        cls.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=Path(__file__).parent, env=env, stdout=cls.log, stderr=cls.log)
        for _ in range(200):
            try:
                with urlopen(cls.url + "/health", timeout=.3) as response:
                    if response.status == 200:
                        return
            except (URLError, OSError):
                if cls.process.poll() is not None:
                    break
                time.sleep(.05)
        cls.stop_server()
        cls.log.seek(0)
        raise RuntimeError("Server failed to start: " + cls.log.read())

    @classmethod
    def stop_server(cls):
        # Windows venv python.exe is a launcher with a real interpreter child.
        # Stop both so restart tests actually release the database and log.
        if os.name == "nt" and cls.process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(cls.process.pid), "/T", "/F"],
                           check=True, capture_output=True)
        elif cls.process.poll() is None:
            cls.process.terminate()
        try:
            cls.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    @classmethod
    def tearDownClass(cls):
        cls.stop_server()
        cls.log.close()
        cls.directory.cleanup()

    def request(self, method, path, token=None, body=None, expected=200):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        payload = None if body is None else json.dumps(body).encode()
        request = Request(self.url + path, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                status, data = response.status, json.load(response)
        except HTTPError as error:
            with error:
                status, data = error.code, json.load(error)
        self.assertEqual(status, expected, data)
        return data

    def setUp(self):
        self.a = self.request("POST", "/agents", self.admin,
                              {"name": "Lunar", "kind": "lunar", "project": "demo"}, 201)
        self.b = self.request("POST", "/agents", self.admin,
                              {"name": "Codex web", "kind": "codex"}, 201)
        self.a_id, self.a_token = self.a["agent"]["id"], self.a["token"]
        self.b_id, self.b_token = self.b["agent"]["id"], self.b["token"]

    def send(self, text="Hola", **fields):
        return self.request("POST", "/messages", self.a_token,
                            {"to_agent_id": self.b_id, "text": text, **fields}, 201)

    def test_round_trip_and_explicit_acknowledgement(self):
        message = self.send("¿Qué agentes hay activos? 🌙")
        for _ in range(2):
            inbox = self.request("GET", "/inbox", self.b_token)
            self.assertEqual([m["id"] for m in inbox["messages"]], [message["id"]])
        self.assertEqual(self.request("GET", "/inbox/count", self.b_token), {"total": 1, "pending": 1})
        reply = self.request("POST", "/messages", self.b_token,
                             {"to_agent_id": self.a_id, "text": "Aquí estoy", "in_reply_to": message["id"]}, 201)
        self.assertEqual(self.request("GET", "/inbox", self.a_token)["messages"][0]["id"], reply["id"])
        for _ in range(2):
            self.request("POST", f'/inbox/{message["id"]}/ack', self.b_token)
        self.assertEqual(self.request("GET", "/inbox", self.b_token)["messages"], [])
        self.assertEqual(len(self.request("GET", "/inbox?unread_only=false", self.b_token)["messages"]), 1)

    def test_broadcast_fans_out_private_copies_that_can_be_replied_to(self):
        grok = self.request("POST", "/agents", self.admin,
                            {"name": "Grok", "kind": "grok"}, 201)
        result = self.request("POST", "/broadcast", self.a_token,
                              {"text": "Quien puede revisar esta pregunta?", "kinds": ["GROK"]}, 201)
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["messages"][0]["to_agent_id"], grok["agent"]["id"])
        self.assertEqual(self.request("GET", "/inbox", self.b_token)["messages"], [])
        reply = self.request("POST", "/messages", grok["token"], {
            "to_agent_id": self.a_id, "text": "Yo lo reviso", "in_reply_to": result["messages"][0]["id"],
        }, 201)
        self.assertEqual(reply["in_reply_to"], result["messages"][0]["id"])

    def test_broadcast_rejects_blank_text(self):
        self.request("POST", "/broadcast", self.a_token, {"text": "   "}, 422)

    def test_image_attachment_round_trip_and_access_control(self):
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/nWQAAAAASUVORK5CYII="
        )
        message = self.send("", attachments=[{"filename": "../pixel.png", "media_type": "image/png",
                                               "data_base64": base64.b64encode(image).decode("ascii")}])
        self.assertEqual(message["attachments"][0]["filename"], "pixel.png")
        self.assertEqual(self.request("GET", "/inbox", self.b_token)["messages"][0]["attachments"][0]["size_bytes"], len(image))
        downloaded = self.request("GET", f"/inbox/{message['id']}/attachments/0", self.b_token)
        self.assertEqual(base64.b64decode(downloaded["data_base64"]), image)
        self.request("GET", f"/inbox/{message['id']}/attachments/0", self.a_token, expected=200)
        self.request("GET", f"/inbox/{message['id']}/attachments/0", self.admin, expected=403)
        self.request("POST", "/messages", self.a_token,
                     {"to_agent_id": self.b_id, "attachments": [{"filename": "fake.png",
                      "media_type": "image/png", "data_base64": base64.b64encode(b"not png").decode()}]}, 422)

    def test_identity_and_mailbox_isolation(self):
        message = self.send()
        self.request("GET", "/agents", expected=401)
        self.request("GET", "/inbox", "invalid-token", expected=401)
        self.request("GET", "/inbox", self.admin, expected=403)
        self.request("POST", "/agents", self.a_token, {"name": "fake"}, 403)
        self.request("POST", f'/inbox/{message["id"]}/ack', self.a_token, expected=404)
        self.assertEqual(self.request("GET", "/inbox", self.a_token)["messages"], [])
        self.request("POST", "/messages", self.a_token,
                     {"to_agent_id": self.b_id, "text": "fake", "from_agent_id": self.b_id}, 422)
        self.request("POST", "/messages", self.a_token,
                     {"to_agent_id": self.b_id, "text": "fake reply", "in_reply_to": message["id"]}, 404)
        listing = self.request("GET", "/agents", self.a_token)
        for agent in listing["agents"]:
            self.assertNotIn("token", agent)
            self.assertNotIn("token_hash", agent)

    def test_presence_and_pagination(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            with db:
                db.execute("UPDATE agents SET last_seen = 0 WHERE id = ?", (self.b_id,))
        offline = self.request("GET", "/agents?online=false", self.admin)
        self.assertIn(self.b_id, [a["id"] for a in offline["agents"]])
        self.request("POST", "/heartbeat", self.b_token)
        online = self.request("GET", "/agents?online=true", self.admin)
        self.assertIn(self.b_id, [a["id"] for a in online["agents"]])
        self.assertEqual(self.request("GET", "/agents/me", self.b_token)["id"], self.b_id)
        ids = [self.send(str(i))["id"] for i in range(3)]
        first = self.request("GET", "/inbox?limit=2", self.b_token)
        self.assertTrue(first["has_more"])
        second = self.request("GET", f'/inbox?limit=2&after_id={first["next_after_id"]}', self.b_token)
        self.assertEqual([m["id"] for m in first["messages"] + second["messages"]], ids)
        self.assertFalse(second["has_more"])
        self.request("GET", "/inbox?limit=-1", self.b_token, expected=422)
        self.request("POST", "/messages", self.a_token, {"to_agent_id": "unknown", "text": "hi"}, 404)
        self.request("POST", "/messages", self.a_token, {"to_agent_id": self.b_id, "text": "  "}, 422)
        stats = self.request("GET", "/stats", self.admin)
        self.assertGreaterEqual(stats["agents_total"], 2)
        self.assertGreaterEqual(stats["agents_online"], 2)

    def test_concurrent_senders(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            messages = list(pool.map(lambda n: self.send(f"message {n}"), range(16)))
        self.assertEqual(len({m["id"] for m in messages}), 16)
        self.assertEqual(self.request("GET", "/inbox/count", self.b_token)["pending"], 16)

    def test_invalid_identifiers_are_rejected_before_storage(self):
        self.request("POST", "/messages", self.a_token,
                     {"to_agent_id": "a/b", "text": "invalid path"}, 422)
        self.request("POST", "/messages", self.a_token,
                     {"to_agent_id": self.b_id, "text": "invalid reply", "in_reply_to": 2**100}, 422)
        self.request("GET", f"/inbox?after_id={2**100}", self.b_token, expected=422)
        self.request("POST", f"/inbox/{2**100}/ack", self.b_token, expected=422)
        self.request("POST", "/inbox/-1/ack", self.b_token, expected=422)

    def test_agent_can_unregister_and_admin_can_remove_agent(self):
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/nWQAAAAASUVORK5CYII="
        )
        first = self.send("private conversation", attachments=[{
            "filename": "private.png", "media_type": "image/png",
            "data_base64": base64.b64encode(image).decode("ascii"),
        }])
        self.request("DELETE", "/agents/me", self.a_token)
        self.request("GET", "/agents/me", self.a_token, expected=401)
        self.request("GET", f"/inbox/{first['id']}/attachments/0", self.b_token, expected=404)
        self.assertEqual(self.request("GET", "/inbox/count", self.b_token), {"total": 0, "pending": 0})

        c = self.request("POST", "/agents", self.admin, {"name": "Observer"}, 201)
        c_id, c_token = c["agent"]["id"], c["token"]
        self.request("POST", "/messages", self.b_token,
                     {"to_agent_id": c_id, "text": "private to be removed"}, 201)
        self.request("DELETE", f"/agents/{self.b_id}", self.b_token, expected=403)
        removed = self.request("DELETE", f"/agents/{self.b_id}", self.admin)
        self.assertTrue(removed["deleted"])
        self.request("GET", "/agents/me", self.b_token, expected=401)
        self.assertEqual(self.request("GET", "/inbox/count", c_token), {"total": 0, "pending": 0})
        self.request("DELETE", f"/agents/{self.b_id}", self.admin, expected=404)

    def test_url_discovery_and_shared_key_self_registration(self):
        for path in ("/", "/connect.md", "/connect.js"):
            with urlopen(self.url + path, timeout=10) as response:
                text = response.read().decode()
                self.assertNotIn(self.admin, text)
                self.assertNotIn(self.join_key, text)
                if path != "/connect.js":
                    self.assertIn("/join", text)
        self.request("POST", "/join", body={"name": "self"}, expected=401)
        self.request("POST", "/join", "wrong-key", {"name": "self"}, 401)
        self.request("POST", "/join", self.admin, {"name": "self"}, 401)
        registered = self.request("POST", "/join", self.join_key,
                                  {"name": "Jules self-registration", "kind": "jules"}, 201)
        self.assertEqual(registered["instructions_url"], "/connect.md")
        token = registered["token"]
        identity = self.request("GET", "/agents/me", token)
        self.assertEqual(identity["id"], registered["agent"]["id"])
        self.request("GET", "/inbox", self.join_key, expected=401)
        self.request("POST", "/agents", token, {"name": "another"}, 403)
        self.request("POST", "/agents", self.join_key, {"name": "another"}, 401)
        message = self.request("POST", "/messages", token,
                               {"to_agent_id": self.a_id, "text": "Registrado por mi cuenta"}, 201)
        self.assertEqual(self.request("GET", "/inbox", self.a_token)["messages"][0]["id"], message["id"])

    def test_restart_retains_tokens_messages_and_acknowledgements(self):
        acknowledged = self.send("already handled")
        pending = self.send("read after restart")
        self.request("POST", f'/inbox/{acknowledged["id"]}/ack', self.b_token)
        type(self).stop_server()
        type(self).start_server()
        inbox = self.request("GET", "/inbox", self.b_token)
        self.assertEqual([m["id"] for m in inbox["messages"]], [pending["id"]])
        self.assertEqual(self.request("GET", "/agents/me", self.a_token)["id"], self.a_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)

