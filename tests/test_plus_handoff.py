# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core.plus_handoff_links import create_short_link, resolve_short_link
from webui.app import create_app


class PlusHandoffLinkTests(unittest.TestCase):
    def test_local_short_link_resolves_only_to_official_chatgpt(self):
        created = create_short_link(account_id=7, target_url="https://chatgpt.com/#pricing")
        item = resolve_short_link(created["code"])
        self.assertEqual(item["account_id"], 7)
        self.assertEqual(item["target_url"], "https://chatgpt.com/#pricing")

    def test_rejects_non_official_target(self):
        with self.assertRaises(ValueError):
            create_short_link(account_id=7, target_url="https://example.com/pay")


class PlusHandoffApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def test_page_is_available(self):
        response = self.client.get("/plus-handoff", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Plus 结账助手", response.get_data(as_text=True))

    def test_create_and_follow_short_link(self):
        with patch("webui.app.db.get_account", return_value={"id": 9, "email": "user@example.com"}):
            response = self.client.post("/api/accounts/9/billing-short-link", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        url = response.get_json()["url"]
        path = url.split("http://localhost", 1)[-1]
        followed = self.client.get(path)
        self.assertEqual(followed.status_code, 302)
        self.assertEqual(followed.headers["Location"], "https://chatgpt.com/#pricing")


if __name__ == "__main__":
    unittest.main()
