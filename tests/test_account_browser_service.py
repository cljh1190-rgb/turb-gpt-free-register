# -*- coding: utf-8 -*-
import time
import unittest
from unittest.mock import Mock, patch

from core import account_browser_service as service
from core import cloakbrowser_registration as registration
from core.cloakbrowser_driver import CloakOpenResult, _build_cloak_locale_options
from webui.app import create_app


def wait_status(account_id: int, expected: set[str], timeout: float = 2.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        status = service.account_browser_status(account_id)
        if status.get("status") in expected:
            return status
        time.sleep(0.01)
    return service.account_browser_status(account_id)


class _FakePage:
    def is_closed(self):
        return False


class _FakeDriver:
    def __init__(self):
        self.page = _FakePage()
        self.urls = []
        self.closed = False
        self.current_url = ""

    def get(self, url):
        self.urls.append(url)
        self.current_url = url

    def execute_script(self, _script):
        return {"clicked": False}

    def execute_async_script(self, _script):
        return True

    def quit(self):
        self.closed = True


class AccountBrowserServiceTests(unittest.TestCase):
    def tearDown(self):
        with service._LOCK:
            entries = list(service._SESSIONS.values())
            service._SESSIONS.clear()
        for entry in entries:
            event = entry.get("close_event")
            if event is not None:
                event.set()

    def test_saved_browser_profile_controls_locale_and_timezone(self):
        profile = {
            "navigator_language": "en-US",
            "accept_language": "en-US,en;q=0.9",
            "timezone_iana": "America/New_York",
            "geo": {"ip": "1.1.1.1", "country": "US"},
        }
        with patch("config.cloakbrowser.CLOAK_LOCALE", ""), \
             patch("config.cloakbrowser.CLOAK_TIMEZONE", ""), \
             patch("config.cloakbrowser.CLOAK_GEOIP", False):
            result = _build_cloak_locale_options("https://proxy.example:443", profile)
        self.assertEqual(result["locale"], "en-US")
        self.assertEqual(result["timezone"], "America/New_York")
        self.assertEqual(result["geo"]["ip"], "1.1.1.1")

    def test_registration_and_manual_open_share_persistent_profile_directory(self):
        email = "User@example.com"
        self.assertEqual(
            registration._account_browser_profile_dir(email),
            service._profile_dir({"email": email}),
        )

    def test_refuses_rotated_exit_until_user_confirms(self):
        account_id = 9901
        account = {
            "id": account_id,
            "email": "user@example.com",
            "proxy_used": "https://proxy.example:443",
            "extra_json": {"browser_profile": {"geo": {"ip": "1.1.1.1"}}},
        }
        with patch("config.proxy.probe_proxy", return_value={"ok": True, "ip": "2.2.2.2"}):
            result = service.open_account_browser(account)
            self.assertTrue(result["accepted"])
            status = wait_status(account_id, {"failed"})
        self.assertEqual(status["error_code"], "exit_ip_changed")
        self.assertEqual(status["registered_exit_ip"], "1.1.1.1")
        self.assertEqual(status["current_exit_ip"], "2.2.2.2")

    def test_confirmed_rotated_exit_opens_visible_persistent_browser(self):
        account_id = 9902
        account = {
            "id": account_id,
            "email": "user2@example.com",
            "proxy_used": "https://proxy.example:443",
            "device_id": "saved-device",
            "extra_json": {
                "browser_profile": {"geo": {"ip": "1.1.1.1"}},
                "cloakbrowser": {"fingerprint_seed": "123456"},
            },
        }
        fake_driver = _FakeDriver()
        fake_dir = Mock()
        fake_dir.__str__ = Mock(return_value="E:/project/accounts/session")
        with patch("config.proxy.probe_proxy", return_value={"ok": True, "ip": "2.2.2.2"}), \
             patch.object(service, "_profile_dir", return_value=fake_dir), \
             patch.object(service, "build_cloak_driver", return_value=(fake_driver, CloakOpenResult(profile_id="cloak-123456", raw={}))) as build, \
             patch.object(service, "prime_cloak_device_id") as prime, \
             patch.object(service.time, "sleep"):
            result = service.open_account_browser(account, allow_rotated_exit=True)
            self.assertTrue(result["accepted"])
            status = wait_status(account_id, {"ready"})
            self.assertEqual(status["status"], "ready")
            self.assertTrue(status["login_restored"])
            self.assertIn("https://chatgpt.com/", fake_driver.urls)
            build.assert_called_once()
            self.assertFalse(build.call_args.kwargs["headless"])
            self.assertEqual(build.call_args.kwargs["fingerprint_seed"], "123456")
            prime.assert_called_once_with(fake_driver, "saved-device")
            service.close_account_browser(account_id)
            closed = wait_status(account_id, {"closed"})
            self.assertEqual(closed["status"], "closed")
            self.assertTrue(fake_driver.closed)

    def test_official_billing_handoff_opens_requested_chatgpt_url(self):
        account_id = 9903
        account = {
            "id": account_id,
            "email": "billing@example.com",
            "proxy_used": "https://proxy.example:443",
        }
        fake_driver = _FakeDriver()
        fake_dir = Mock()
        fake_dir.__str__ = Mock(return_value="E:/project/accounts/billing-session")
        with patch("config.proxy.probe_proxy", return_value={"ok": True, "ip": "1.1.1.1"}), \
             patch.object(service, "_profile_dir", return_value=fake_dir), \
             patch.object(service, "build_cloak_driver", return_value=(fake_driver, CloakOpenResult(profile_id="billing", raw={}))), \
             patch.object(service, "prime_cloak_device_id"), \
             patch.object(service.time, "sleep"):
            result = service.open_account_browser(account, initial_url="https://chatgpt.com/#pricing")
            self.assertTrue(result["accepted"])
            status = wait_status(account_id, {"ready"})
            self.assertEqual(status["status"], "ready")
            self.assertEqual(fake_driver.urls[-1], "https://chatgpt.com/#pricing")
            service.close_account_browser(account_id)

    def test_rejects_non_chatgpt_initial_url(self):
        result = service.open_account_browser(
            {"id": 9904, "email": "bad@example.com", "proxy_used": "https://proxy.example:443"},
            initial_url="https://example.com/pay",
        )
        self.assertFalse(result["accepted"])
        self.assertIn("chatgpt.com", result["error"])

    def test_billing_handoff_can_fallback_when_saved_proxy_is_dead(self):
        account_id = 9905
        account = {
            "id": account_id,
            "email": "fallback@example.com",
            "proxy_used": "https://dead-proxy.example:443",
        }
        fake_driver = _FakeDriver()
        fake_dir = Mock()
        fake_dir.__str__ = Mock(return_value="E:/project/accounts/fallback-session")
        probes = {
            "https://dead-proxy.example:443": {"ok": False, "error": "timeout"},
            "https://healthy-proxy.example:443": {"ok": True, "ip": "2.2.2.2"},
        }
        with patch("config.proxy.probe_proxy", side_effect=lambda proxy, **_: probes[proxy]), \
             patch("config.proxy.pick_proxy", return_value="https://healthy-proxy.example:443"), \
             patch("config.proxy.ban_proxy"), \
             patch.object(service, "_profile_dir", return_value=fake_dir), \
             patch.object(service, "build_cloak_driver", return_value=(fake_driver, CloakOpenResult(profile_id="fallback", raw={}))) as build, \
             patch.object(service, "prime_cloak_device_id"), \
             patch.object(service.time, "sleep"):
            result = service.open_account_browser(
                account,
                initial_url="https://chatgpt.com/#pricing",
                allow_proxy_fallback=True,
            )
            self.assertTrue(result["accepted"])
            status = wait_status(account_id, {"ready"})
            self.assertEqual(status["status"], "ready")
            self.assertTrue(status["proxy_fallback_used"])
            self.assertIn("健康代理", status["warning"])
            self.assertEqual(build.call_args.kwargs["proxy"], "https://healthy-proxy.example:443")
            service.close_account_browser(account_id)

    def test_billing_handoff_can_use_direct_network_when_all_proxies_are_dead(self):
        account_id = 9906
        account = {
            "id": account_id,
            "email": "direct@example.com",
            "proxy_used": "https://dead-proxy.example:443",
        }
        fake_driver = _FakeDriver()
        fake_dir = Mock()
        fake_dir.__str__ = Mock(return_value="E:/project/accounts/direct-session")
        with patch("config.proxy.probe_proxy", return_value={"ok": False, "error": "timeout"}), \
             patch("config.proxy.pick_proxy", return_value=""), \
             patch.object(service, "_profile_dir", return_value=fake_dir), \
             patch.object(service, "build_cloak_driver", return_value=(fake_driver, CloakOpenResult(profile_id="direct", raw={}))) as build, \
             patch.object(service, "prime_cloak_device_id"), \
             patch.object(service.time, "sleep"):
            result = service.open_account_browser(
                account,
                initial_url="https://chatgpt.com/#pricing",
                allow_proxy_fallback=True,
                allow_direct_fallback=True,
            )
            self.assertTrue(result["accepted"])
            status = wait_status(account_id, {"ready"})
            self.assertEqual(status["status"], "ready")
            self.assertTrue(status["direct_fallback_used"])
            self.assertIn("本机网络", status["warning"])
            self.assertIsNone(build.call_args.kwargs["proxy"])
            self.assertTrue(build.call_args.kwargs["force_direct"])
            service.close_account_browser(account_id)

    def test_official_checkout_url_allowlist(self):
        self.assertEqual(
            service._official_checkout_url("https://checkout.stripe.com/c/pay/cs_test_123"),
            "https://checkout.stripe.com/c/pay/cs_test_123",
        )
        self.assertEqual(
            service._official_checkout_url("https://chatgpt.com/backend-api/payments/checkout?id=1"),
            "https://chatgpt.com/backend-api/payments/checkout?id=1",
        )
        self.assertEqual(service._official_checkout_url("https://example.com/checkout"), "")

class AccountBrowserApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()

    def test_open_browser_route_forwards_rotated_exit_confirmation(self):
        account = {"id": 12, "email": "user@example.com", "proxy_used": "https://proxy.example:443"}
        accepted = {"accepted": True, "status": "queued", "active": True}
        with patch("webui.app.db.get_account", return_value=account), \
             patch("webui.app.account_browser_service.open_account_browser", return_value=accepted) as opener:
            response = self.client.post(
                "/api/accounts/12/open-browser",
                headers={"X-Auth-Code": "test-auth"},
                json={"allow_rotated_exit": True},
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])
        opener.assert_called_once_with(account, allow_rotated_exit=True)

    def test_open_billing_route_uses_official_handoff(self):
        account = {"id": 12, "email": "user@example.com", "proxy_used": "https://proxy.example:443"}
        accepted = {"accepted": True, "status": "queued", "active": True}
        with patch("webui.app.db.get_account", return_value=account), \
             patch("core.billing_handoff.open_billing_handoff", return_value=accepted) as opener:
            response = self.client.post(
                "/api/accounts/12/open-billing",
                headers={"X-Auth-Code": "test-auth"},
                json={"allow_rotated_exit": True},
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])
        opener.assert_called_once_with(12, allow_rotated_exit=True)


if __name__ == "__main__":
    unittest.main()
