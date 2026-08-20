# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import roxy_registration
from core.cloakbrowser_registration import _should_switch_proxy


class RegistrationChallengeTests(unittest.TestCase):
    def test_detects_cloudflare_just_a_moment_page(self):
        self.assertTrue(roxy_registration._is_security_interstitial_state({
            "url": "https://chatgpt.com/auth/login",
            "title": "Just a moment...",
            "inputs": [],
            "actions": [],
        }))

    def test_waits_for_challenge_then_types_email(self):
        driver = Mock()
        driver.current_url = "https://chatgpt.com/auth/login"
        driver._registration_log_prefix = "[Test注册]"
        email_input = object()
        challenge = {
            "url": driver.current_url,
            "title": "Just a moment...",
            "inputs": [],
            "actions": [],
        }
        with patch.object(
            roxy_registration,
            "_find_visible_email_input_js",
            side_effect=[None, email_input],
        ), patch.object(
            roxy_registration,
            "_email_entry_state",
            return_value=challenge,
        ), patch.object(
            roxy_registration,
            "_set_element_value",
        ) as set_value, patch.object(roxy_registration.time, "sleep"):
            roxy_registration._type_email_address(driver, "user@example.com", timeout=1)
        set_value.assert_called_once_with(driver, email_input, "user@example.com")

    def test_stuck_challenge_is_proxy_switchable(self):
        exc = RuntimeError("challenge_stuck: title=Just a moment...")
        self.assertTrue(_should_switch_proxy(exc))


if __name__ == "__main__":
    unittest.main()
