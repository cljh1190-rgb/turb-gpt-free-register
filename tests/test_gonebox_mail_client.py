# -*- coding: utf-8 -*-
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from core import email_provider
from core import gonebox_mail_client as client


def _response(payload: dict, status_code: int = 200) -> Mock:
    response = Mock(status_code=status_code)
    response.json.return_value = payload
    return response


class GoneboxMailClientTests(unittest.TestCase):
    def setUp(self):
        with client._CONTEXT_LOCK:
            client._CONTEXT_CACHE.clear()
        with client._DOMAIN_LOCK:
            client._DOMAIN_CACHE = []
            client._DOMAIN_CACHE_UNTIL = 0.0

    @patch("core.gonebox_mail_client.requests.request")
    def test_create_address_parses_address(self, request_mock):
        request_mock.return_value = _response({
            "success": True,
            "data": {
                "id": "CHEO6le5fXYL",
                "address": "wksqvzd1@gonebox.email",
                "domain": "gonebox.email",
                "expiresAt": 1787232490,
                "ttl": 3600,
                "existing": False,
            },
        }, status_code=201)

        account = client.create_address()

        self.assertEqual(account.email, "wksqvzd1@gonebox.email")
        self.assertEqual(account.domain, "gonebox.email")
        self.assertEqual(account.inbox_id, "CHEO6le5fXYL")
        self.assertTrue(account.expires_at.startswith("2026-"))
        self.assertEqual(request_mock.call_count, 1)
        self.assertEqual(request_mock.call_args_list[0].kwargs["json"], {"domain": "gonebox.email"})
        self.assertTrue(request_mock.call_args_list[0].args[1].endswith("/inboxes"))

    @patch("core.gonebox_mail_client.requests.request")
    def test_pick_account_caches_context(self, request_mock):
        request_mock.return_value = _response({
            "success": True,
            "data": {
                "id": "abc",
                "address": "random1@gonebox.email",
                "domain": "gonebox.email",
                "expiresAt": 1787232490,
                "ttl": 3600,
            },
        }, status_code=201)

        account = client.pick_account()

        self.assertEqual(account.email, "random1@gonebox.email")
        self.assertIs(client.get_account_context(account.email), account)

    @patch("core.gonebox_mail_client.requests.request")
    def test_auth_header_added_when_api_key_set(self, request_mock):
        request_mock.return_value = _response({
            "success": True,
            "data": {
                "id": "abc",
                "address": "x@gonebox.email",
                "domain": "gonebox.email",
                "expiresAt": 1787232490,
            },
        }, status_code=201)

        with patch("config.email.GONEBOX_API_KEY", "sk-test-123"):
            client.create_address()

        headers = request_mock.call_args_list[0].kwargs["headers"]
        self.assertEqual(headers.get("X-API-Key"), "sk-test-123")

    @patch("core.gonebox_mail_client.requests.request")
    def test_auth_header_absent_without_key(self, request_mock):
        request_mock.return_value = _response({
            "success": True,
            "data": {
                "id": "abc",
                "address": "x@gonebox.email",
                "domain": "gonebox.email",
                "expiresAt": 1787232490,
            },
        }, status_code=201)

        with patch("config.email.GONEBOX_API_KEY", ""):
            client.create_address()

        headers = request_mock.call_args_list[0].kwargs["headers"]
        self.assertNotIn("X-API-Key", headers)

    @patch("core.gonebox_mail_client.requests.request")
    def test_list_messages_uses_address_path(self, request_mock):
        request_mock.return_value = _response({
            "success": True,
            "data": {
                "address": "wksqvzd1@gonebox.email",
                "expiresAt": 1787232490,
                "count": 1,
                "messages": [
                    {"id": "msg-1", "subject": "Verify", "from_address": "noreply@openai.com"}
                ],
            },
        })

        messages = client.list_messages("wksqvzd1@gonebox.email")

        self.assertEqual(len(messages), 1)
        url = request_mock.call_args_list[0].args[1]
        self.assertTrue(url.endswith("/inboxes/wksqvzd1%40gonebox.email/messages"))

    @patch("core.gonebox_mail_client.requests.request")
    def test_get_message_uses_global_path(self, request_mock):
        request_mock.return_value = _response({
            "success": True,
            "data": {
                "id": "msg-1",
                "from_address": "noreply@openai.com",
                "subject": "Verify",
                "body_text": "Your code is 654321",
                "received_at": "2026-08-18T00:00:10Z",
            },
        })

        detail = client.get_message("msg-1")

        self.assertEqual(detail["id"], "msg-1")
        url = request_mock.call_args_list[0].args[1]
        self.assertTrue(url.endswith("/messages/msg-1"))

    def test_fetch_latest_otp_reads_message_detail(self):
        summary = {
            "id": "msg-1",
            "subject": "Verify your email address",
            "from_address": "noreply@openai.com",
            "received_at": "2026-08-18T00:00:10Z",
        }
        detail = {
            **summary,
            "body_text": "Your verification code is 654321",
            "to": "worker@gonebox.email",
        }
        with patch.object(client, "list_messages", return_value=[summary]), \
             patch.object(client, "get_message", return_value=detail):
            code = client.fetch_latest_otp(
                "worker@gonebox.email",
                after_ts=0,
                max_wait=1,
                poll_interval=1,
                settle_seconds=0,
            )

        self.assertEqual(code, "654321")

    def test_fetch_latest_otp_reads_gonebox_camel_case_body_fields(self):
        summary = {
            "id": "msg-camel",
            "subject": "Your temporary ChatGPT verification code",
            "from": "noreply@tm.openai.com",
            "receivedAt": 1787233741,
        }
        detail = {
            **summary,
            "bodyHtml": "<p>Enter this code: <strong>366319</strong></p>",
            "bodyText": None,
        }
        with patch.object(client, "list_messages", return_value=[summary]), \
             patch.object(client, "get_message", return_value=detail):
            code = client.fetch_latest_otp(
                "jrivkjs4@gonebox.email",
                after_ts=0,
                max_wait=1,
                poll_interval=1,
                settle_seconds=0,
            )

        self.assertEqual(code, "366319")

    def test_message_timestamp_normalizes_iso_and_epoch(self):
        expected_iso = datetime.fromisoformat("2026-08-18T00:00:10+00:00").timestamp()
        self.assertAlmostEqual(
            client._message_timestamp({"received_at": "2026-08-18T00:00:10Z"}),
            expected_iso,
            places=1,
        )
        # 秒级 epoch 原样返回
        self.assertAlmostEqual(
            client._message_timestamp({"received_at": 1787040010}),
            1787040010.0,
            places=1,
        )
        # 毫秒 epoch → 秒
        self.assertAlmostEqual(
            client._message_timestamp({"receivedAt": 1787040010123}),
            1787040010.123,
            places=1,
        )
        self.assertIsNone(client._message_timestamp({}))

    def test_email_provider_routes_gonebox_context(self):
        account = client.GoneboxAccount(
            email="worker@gonebox.email",
            domain="gonebox.email",
        )
        with patch.object(client, "pick_account", return_value=account), \
             patch("config.email.EMAIL_SOURCE", "gonebox"):
            with client._CONTEXT_LOCK:
                client._CONTEXT_CACHE[account.email] = account
            self.assertEqual(email_provider.acquire_email(), account.email)
            self.assertEqual(email_provider.resolve_email_source(account.email), "gonebox")

    def test_release_clears_context(self):
        account = client.GoneboxAccount(email="worker@gonebox.email", domain="gonebox.email")
        with client._CONTEXT_LOCK:
            client._CONTEXT_CACHE[account.email] = account
        client.release_account(account.email, status="used")
        self.assertIsNone(client.get_account_context(account.email))


if __name__ == "__main__":
    unittest.main()
