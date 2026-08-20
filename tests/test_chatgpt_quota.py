# -*- coding: utf-8 -*-
import unittest

from core.chatgpt_quota import parse_quota_usage


class ChatgptQuotaTests(unittest.TestCase):
    def test_parse_primary_window_and_credits(self):
        result = parse_quota_usage({
            "email": "plus@example.com",
            "plan_type": "plus",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 6,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 500,
                    "reset_at": 1786258888,
                },
                "secondary_window": None,
            },
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            "spend_control": {"reached": False, "individual_limit": None},
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["primary_used_percent"], 6)
        self.assertEqual(result["primary_remaining_percent"], 94)
        self.assertEqual(result["primary_limit_window_seconds"], 604800)
        self.assertTrue(result["primary_reset_at_iso"].endswith("Z"))
        self.assertFalse(result["credits_has_credits"])


if __name__ == "__main__":
    unittest.main()
