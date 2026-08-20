# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from config import browser as browser_cfg
from core.chatgpt_plan import check_account_plan
from core.cloakbrowser_driver import (
    CloakOpenResult,
    capture_cloak_environment,
    stable_cloak_device_id,
    stable_cloak_fingerprint_seed,
)
from core.session import BrowserSession
from webui.app import _account_plan_check_context


class _FakeCookies:
    def __init__(self):
        self.jar = []
        self.values = []

    def set(self, name, value, **kwargs):
        self.values.append((name, value, kwargs))


class _FakeCurlSession:
    def __init__(self, *args, **kwargs):
        self.cookies = _FakeCookies()
        self.proxies = {}
        self.timeout = None


class FingerprintContinuityTests(unittest.TestCase):
    def test_stable_seed_and_device_are_account_scoped(self):
        email = "Test.User@example.com"
        self.assertEqual(
            stable_cloak_fingerprint_seed(email),
            stable_cloak_fingerprint_seed(" test.user@EXAMPLE.com "),
        )
        self.assertEqual(
            stable_cloak_device_id(email),
            stable_cloak_device_id(" test.user@EXAMPLE.com "),
        )
        self.assertNotEqual(
            stable_cloak_device_id(email),
            stable_cloak_device_id("other@example.com"),
        )

    def test_capture_maps_runtime_profile_and_timezone_sign(self):
        observed = {
            "userAgent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
            ),
            "platform": "Win32",
            "vendor": "Google Inc.",
            "language": "en-US",
            "languages": ["en-US", "en"],
            "webdriver": False,
            "hardwareConcurrency": 8,
            "deviceMemory": 8,
            "screenWidth": 1920,
            "screenHeight": 1080,
            "availWidth": 1920,
            "availHeight": 1040,
            "colorDepth": 24,
            "pixelDepth": 24,
            "devicePixelRatio": 1,
            "timezone": "America/Denver",
            "timezoneOffset": 360,
            "requestIdleCallback": True,
            "uaData": {
                "brands": [
                    {"brand": "Chromium", "version": "146"},
                    {"brand": "Not-A.Brand", "version": "24"},
                    {"brand": "Google Chrome", "version": "146"},
                ],
                "mobile": False,
                "platform": "Windows",
                "high": {
                    "architecture": "x86",
                    "bitness": "64",
                    "model": "",
                    "platformVersion": "19.0.0",
                    "uaFullVersion": "146.0.7680.177",
                    "fullVersionList": [
                        {"brand": "Chromium", "version": "146.0.7680.177"},
                        {"brand": "Google Chrome", "version": "146.0.7680.177"},
                    ],
                },
            },
        }
        page = Mock()
        page.evaluate.return_value = observed
        context = Mock()
        context.cookies.return_value = [{"name": "oai-did", "value": "saved-device"}]
        driver = Mock(page=page, context=context)
        opened = CloakOpenResult(raw={"locale": {"accept_language": "en-US,en;q=0.9"}})

        captured = capture_cloak_environment(driver, opened, fallback_device_id="fallback")
        profile = captured["browser_profile"]

        self.assertEqual(captured["device_id"], "saved-device")
        self.assertEqual(profile["chrome_full_version"], "146.0.7680.177")
        self.assertEqual(profile["browser_os"], "Windows")
        self.assertEqual(profile["timezone_offset_minutes"], -360)
        self.assertEqual(profile["timezone_name"], "Mountain Daylight Time")
        self.assertEqual(profile["screen_width"], 1920)
        self.assertFalse(profile["webdriver"])
        self.assertEqual(browser_cfg.validate_browser_profile(profile), [])

    @patch("core.session.Session", _FakeCurlSession)
    def test_browser_session_reuses_device_and_profile(self):
        profile = browser_cfg.build_browser_environment(
            {"country": "US", "timezone": "America/Denver"},
            base_profile=browser_cfg.HAR_CAPTURE_BASE_PROFILE,
        )
        with patch("config.proxy.proxy_required", return_value=False):
            session = BrowserSession(
                proxy="",
                detect_exit_geo=False,
                device_id="existing-device",
                browser_profile=profile,
            )
        self.assertEqual(session.device_id, "existing-device")
        self.assertEqual(session.browser_profile["user_agent"], profile["user_agent"])
        self.assertEqual(session._get_common_headers()["User-Agent"], profile["user_agent"])
        self.assertEqual(session.js_timezone_offset_min(), -int(profile["timezone_offset_minutes"]))
        self.assertIsNot(session.browser_profile, profile)

    def test_plan_query_forwards_saved_environment(self):
        response = Mock(status_code=401, text="unauthorized", headers={})
        fake_env = Mock()
        fake_env.device_id = "saved-device"
        fake_env.navigator_language.return_value = "en-US"
        fake_env._get_common_headers.return_value = {"User-Agent": "saved-ua"}
        fake_env.session.get.return_value = response
        fake_env.session.close.return_value = None
        profile = {"user_agent": "saved-ua", "navigator_language": "en-US"}
        with patch("core.chatgpt_plan.BrowserSession", return_value=fake_env) as constructor, \
             patch("config.proxy.THORDATA_ENABLED", True), \
             patch("config.proxy.pick_healthy_country_proxy", return_value="https://43.135.181.9:12345"), \
             patch("config.proxy.get_proxy_metadata", return_value={"country": "JP"}):
            result = check_account_plan(
                "opaque-token-value-that-is-long-enough-for-a-request",
                proxy="",
                device_id="saved-device",
                browser_profile=profile,
                max_attempts=1,
            )
        self.assertEqual(result["http_status"], 401)
        constructor.assert_called_once_with(
            proxy="https://43.135.181.9:12345",
            detect_exit_geo=False,
            device_id="saved-device",
            browser_profile=profile,
        )

    def test_webui_account_context_reverses_timezone_offset(self):
        account = {
            "device_id": "db-device",
            "proxy_used": "http://proxy.example:8080",
            "extra_json": (
                '{"device_id":"extra-device","browser_profile":'
                '{"timezone_offset_minutes":-360,"user_agent":"ua"}}'
            ),
        }
        context = _account_plan_check_context(account)
        self.assertEqual(context["device_id"], "db-device")
        self.assertEqual(context["timezone_offset_min"], "360")
        self.assertEqual(context["browser_profile"]["user_agent"], "ua")

    def test_default_protocol_profile_matches_tls_impersonation(self):
        profile = browser_cfg.build_browser_environment(base_profile=browser_cfg.HAR_CAPTURE_BASE_PROFILE)
        self.assertEqual(browser_cfg.IMPERSONATE, "chrome146")
        self.assertEqual(profile["chrome_major"], "146")
        self.assertEqual(profile["browser_os"], "Windows")
        self.assertEqual(profile["navigator_platform"], "Win32")
        self.assertEqual(browser_cfg.validate_browser_profile(profile), [])


if __name__ == "__main__":
    unittest.main()
