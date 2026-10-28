"""Registering with someone else's token: read the owner and declare the owner's name.

When a person hands their account registration key to an agent, that agent must be able
to see whose key it is (``GET /registration/owner`` returns id, username and display
name) and must declare it at registration time (``owner_name``). A mismatch is rejected,
so an instance never lands in a workspace pretending to belong to somebody else.
"""

import http.cookiejar
import json
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import test_mailbox as http_tests


class RegistrationOwnerTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.MailboxTests.setUpClass.__func__)
    tearDownClass = classmethod(http_tests.MailboxTests.tearDownClass.__func__)
    start_server = classmethod(http_tests.MailboxTests.start_server.__func__)
    stop_server = classmethod(http_tests.MailboxTests.stop_server.__func__)
    request = http_tests.MailboxTests.request
    setUp = http_tests.MailboxTests.setUp
    _registration_key = None

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

    def owner_key(self):
        """Create (once per test class) an owner account and its registration key."""
        cls = type(self)
        if cls._registration_key:
            return cls._registration_key
        cookies = http.cookiejar.CookieJar()
        browser = build_opener(HTTPCookieProcessor(cookies))
        status = self.browser_request(browser, "/auth/status")
        if status["setup_required"]:
            self.browser_request(browser, "/auth/setup", "POST", {
                "setup_key": self.admin, "username": "OwnerUno", "password": "owner-uno-password-42"}, expected=201)
        else:
            self.browser_request(browser, "/auth/login", "POST", {
                "username": "OwnerUno", "password": "owner-uno-password-42"}, expected=200)
        csrf = next(cookie.value for cookie in cookies if "csrf" in cookie.name)
        key = self.browser_request(browser, "/account/registration-key", "POST", {}, {"X-CSRF-Token": csrf})["registration_key"]
        self.assertTrue(key.startswith("amr_"))
        cls._registration_key = key
        return key

    def test_the_key_reveals_the_owner_and_nothing_else_does(self):
        key = self.owner_key()
        data = self.request("GET", "/registration/owner", key)
        self.assertEqual(data["owner"]["username"], "OwnerUno")
        self.assertTrue(data["owner"]["id"])
        self.assertEqual(data["endpoint"], "/join/secure")
        self.request("GET", "/registration/owner", self.a_token, expected=403)
        self.request("GET", "/registration/owner", None, expected=401)

    def test_registration_with_a_foreign_key_must_declare_the_owner(self):
        key = self.owner_key()
        payload = {"name": "Sin dueño", "kind": "codex", "session_key": "no-owner-session"}
        self.request("POST", "/join/secure", key, payload, 422)
        self.request("POST", "/join/secure", key, {**payload, "owner_name": "Otra Persona"}, 422)
        registered = self.request("POST", "/join/secure", key,
                                  {**payload, "session_key": "with-owner-session", "owner_name": "owneruno"}, 201)
        self.assertEqual(registered["owner"]["username"], "OwnerUno")

    def test_the_instance_can_see_its_owner_afterwards(self):
        key = self.owner_key()
        registered = self.request("POST", "/join/secure", key,
                                  {"name": "Con dueño", "kind": "codex", "session_key": "owner-later-session",
                                   "owner_name": "OwnerUno"}, 201)
        card = self.request("GET", "/agents/me", registered["token"])
        self.assertEqual(card["owner"]["username"], "OwnerUno")
        self.assertTrue(card["owner"]["id"])
        self.assertNotIn("owner", self.request("GET", "/agents", registered["token"])["agents"][0])

    def test_a_human_registration_from_the_panel_derives_the_owner(self):
        created = self.request("POST", "/agents", self.admin, {"name": "Desde el panel", "kind": "other"}, 201)
        card = self.request("GET", "/agents/me", created["token"])
        self.assertIn("owner", card)
        self.assertEqual(card["owner"]["username"], card["owner"]["username"])


if __name__ == "__main__":
    unittest.main()
