# -*- coding: utf-8 -*-
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from core import email_provider
from core import throwaway_mail_client as client


def _response(payload: dict, status_code: int = 200) -> Mock:
    response = Mock(status_code=status_code)
    response.json.return_value = payload
    return response


class ThrowawayMailClientTests(unittest.TestCase):
    def setUp(self):
        with client._CONTEXT_LOCK:
            client._CONTEXT_CACHE.clear()
        with client._DOMAIN_LOCK:
            client._DOMAIN_CACHE = []
            client._DOMAIN_CACHE_UNTIL = 0.0

    @patch("core.throwaway_mail_client.requests.request")
    def test_pick_account_randomly_selects_cached_domain(self, request_mock):
        request_mock.side_effect = [
            _response({"status": True, "data": {"domains": ["a.example", "b.example"]}}),
            _response({
                "status": True,
                "data": {
                    "email": "random123@b.example",
                    "domain": "b.example",
                    "expires_at": "2026-08-18T00:10:00Z",
                },
            }),
        ]

        with patch.object(client.secrets, "choice", return_value="b.example"):
            account = client.pick_account()

        self.assertEqual(account.email, "random123@b.example")
        self.assertEqual(account.domain, "b.example")
        self.assertIs(client.get_account_context(account.email), account)
        self.assertEqual(request_mock.call_count, 2)
        self.assertEqual(request_mock.call_args_list[1].kwargs["json"], {"domain": "b.example"})

    @patch("core.throwaway_mail_client.requests.request")
    def test_concurrent_pick_uses_one_domain_refresh_and_isolated_inboxes(self, request_mock):
        lock = threading.Lock()
        counters = {"domains": 0, "addresses": 0}
        domains = ["a.example", "b.example", "c.example"]

        def request(method, url, **kwargs):
            if url.endswith("/domains"):
                with lock:
                    counters["domains"] += 1
                return _response({"status": True, "data": {"domains": domains}})
            selected = kwargs["json"]["domain"]
            with lock:
                counters["addresses"] += 1
                number = counters["addresses"]
            return _response({
                "status": True,
                "data": {
                    "email": f"worker{number}@{selected}",
                    "domain": selected,
                    "expires_at": "2026-08-18T00:10:00Z",
                },
            })

        request_mock.side_effect = request
        with ThreadPoolExecutor(max_workers=8) as executor:
            accounts = list(executor.map(lambda _: client.pick_account(), range(16)))

        self.assertEqual(counters["domains"], 1)
        self.assertEqual(counters["addresses"], 16)
        self.assertEqual(len({account.email for account in accounts}), 16)
        self.assertTrue(all(account.domain in domains for account in accounts))
        self.assertTrue(all(client.get_account_context(account.email) is account for account in accounts))

    def test_fetch_latest_otp_reads_message_detail(self):
        summary = {
            "id": "msg-1",
            "subject": "Verify your email address",
            "from_email": "noreply@openai.com",
            "received_at": "2026-08-18T00:00:10Z",
        }
        detail = {
            **summary,
            "body": "Your verification code is 654321",
            "to": "worker@a.example",
        }
        with patch.object(client, "list_messages", return_value=[summary]), \
             patch.object(client, "get_message", return_value=detail):
            code = client.fetch_latest_otp(
                "worker@a.example",
                after_ts=0,
                max_wait=1,
                poll_interval=1,
                settle_seconds=0,
            )

        self.assertEqual(code, "654321")

    def test_email_provider_routes_throwaway_context(self):
        account = client.ThrowawayAccount(
            email="worker@a.example",
            domain="a.example",
        )
        with patch.object(client, "pick_account", return_value=account), \
             patch("config.email.EMAIL_SOURCE", "throwaway"):
            with client._CONTEXT_LOCK:
                client._CONTEXT_CACHE[account.email] = account
            self.assertEqual(email_provider.acquire_email(), account.email)
            self.assertEqual(email_provider.resolve_email_source(account.email), "throwaway")

    def test_release_clears_context(self):
        account = client.ThrowawayAccount(email="worker@a.example", domain="a.example")
        with client._CONTEXT_LOCK:
            client._CONTEXT_CACHE[account.email] = account
        client.release_account(account.email, status="used")
        self.assertIsNone(client.get_account_context(account.email))


if __name__ == "__main__":
    unittest.main()
