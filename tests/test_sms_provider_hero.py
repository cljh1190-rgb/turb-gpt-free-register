# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import sms_provider
from config import codex as codex_config


class _Resp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        return self.responses.pop(0)

    def close(self):
        pass


class HeroSmsProviderTests(unittest.TestCase):
    def test_provider_label_hero(self):
        with patch.object(codex_config, "SMS_PROVIDER", "hero"):
            self.assertEqual(sms_provider._provider_label(), "HeroSMS")

    def test_default_api_base_when_empty(self):
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), patch.object(codex_config, "SMS_API_BASE", ""):
            self.assertEqual(
                sms_provider._sms_api_base(),
                "https://hero-sms.com/stubs/handler_api.php",
            )

    def test_hero_json_bad_key(self):
        http = _Http([_Resp(401, '{"title":"BAD_KEY","details":"Unauthorized"}')])
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), patch.object(
            codex_config, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"
        ), patch.object(codex_config, "SMS_API_KEY", "x"):
            with self.assertRaises(sms_provider.SmsProviderError) as ctx:
                sms_provider._request_activate(http, {"action": "getBalance"})
            self.assertIn("BAD_KEY", str(ctx.exception))

    def test_hero_get_number_success(self):
        http = _Http([_Resp(200, "ACCESS_NUMBER:999:12025550123")])
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), patch.object(
            codex_config, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"
        ), patch.object(codex_config, "SMS_API_KEY", "k"), patch.object(
            codex_config, "SMS_SERVICE", "dr"
        ), patch.object(codex_config, "SMS_COUNTRY", "187"), patch.object(
            codex_config, "SMS_MAX_PRICE", ""
        ):
            aid, phone = sms_provider.acquire_number(http=http)
        self.assertEqual(aid, "999")
        self.assertEqual(phone, "12025550123")
        self.assertEqual(http.calls[0]["params"]["action"], "getNumber")
        self.assertEqual(http.calls[0]["params"]["api_key"], "k")
        self.assertTrue(http.calls[0]["url"].endswith("/stubs/handler_api.php"))

    def test_hero_get_status_ok(self):
        http = _Http([_Resp(200, "STATUS_OK:654321")])
        with patch.object(codex_config, "SMS_PROVIDER", "hero"), patch.object(
            codex_config, "SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"
        ), patch.object(codex_config, "SMS_API_KEY", "k"), patch.object(
            codex_config, "SMS_CODE_WAIT", 1
        ), patch.object(codex_config, "SMS_POLL_INTERVAL", 0):
            code = sms_provider.wait_for_sms_code("999", http=http, max_wait=1, poll_interval=0)
        self.assertEqual(code, "654321")


if __name__ == "__main__":
    unittest.main()
