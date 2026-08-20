# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock

from core.chatgpt_plan import (
    PLUS_TRIAL_COUPON_ID,
    PLUS_TRIAL_COUPON_PATH,
    _check_plus_trial_coupon,
    _merge_plus_trial_coupon,
    parse_accounts_check,
    parse_plus_trial_coupon,
    _retryable_plan_error,
)


class ChatGPTPlanTrialTests(unittest.TestCase):
    def test_proxy_edge_auth_statuses_are_retryable(self):
        self.assertTrue(_retryable_plan_error(401))
        self.assertTrue(_retryable_plan_error(403))
        self.assertTrue(_retryable_plan_error(429))
        self.assertFalse(_retryable_plan_error(400))
        self.assertFalse(_retryable_plan_error(401, '"code":"token_revoked"'))
        self.assertFalse(_retryable_plan_error(401, '"code":"token_invalidated"'))
        self.assertFalse(_retryable_plan_error(401, 'Your authentication token has been invalidated.'))

    def test_coupon_eligible_is_detected(self):
        result = parse_plus_trial_coupon({
            "state": "eligible",
            "redemption": {
                "redeemed": False,
                "promotion_length_days": 31,
            },
        })
        self.assertTrue(result["plus_trial_coupon_eligible"])
        self.assertEqual(result["plus_trial_coupon_promotion_length_days"], 31)

    def test_coupon_not_eligible_is_not_misreported(self):
        result = parse_plus_trial_coupon({
            "state": "not_eligible",
            "redemption": {"redeemed": False},
        })
        self.assertFalse(result["plus_trial_coupon_eligible"])

    def test_coupon_result_upgrades_free_account_trial_flag(self):
        plan = {"current_plan_type": "free", "plus_trial_eligible": False}
        coupon = {
            "plus_trial_coupon_checked": True,
            "plus_trial_coupon_eligible": True,
            "plus_trial_coupon_state": "eligible",
            "plus_trial_coupon_promotion_length_days": 31,
        }
        merged = _merge_plus_trial_coupon(plan, coupon)
        self.assertTrue(merged["plus_trial_eligible"])
        self.assertEqual(merged["plus_trial_campaign_id"], PLUS_TRIAL_COUPON_ID)
        self.assertEqual(merged["plus_trial_duration_num_periods"], 31)
        self.assertEqual(merged["plus_trial_duration_period"], "day")

    def test_coupon_check_uses_dedicated_endpoint(self):
        response = Mock(status_code=200, text="")
        response.json.return_value = {
            "state": "eligible",
            "redemption": {"redeemed": False},
        }
        env = Mock()
        env.device_id = "device-id"
        env.navigator_language.return_value = "en-US"
        env._get_common_headers.return_value = {"User-Agent": "ua"}
        env.session.get.return_value = response
        result = _check_plus_trial_coupon(env, "token-value", 12)
        self.assertTrue(result["plus_trial_coupon_eligible"])
        call = env.session.get.call_args
        self.assertEqual(call.args[0], f"https://chatgpt.com{PLUS_TRIAL_COUPON_PATH}")
        self.assertEqual(call.kwargs["params"]["coupon"], PLUS_TRIAL_COUPON_ID)
        self.assertEqual(call.kwargs["headers"]["x-openai-target-route"], PLUS_TRIAL_COUPON_PATH)

    def test_accounts_check_keeps_yearly_new_user_separate_from_free_trial(self):
        data = {
            "accounts": {
                "default": {
                    "account": {"account_id": "acc", "plan_type": "free"},
                    "entitlement": {"subscription_plan": "chatgptfreeplan"},
                    "eligible_promo_campaigns": {},
                    "eligible_offers": {"offers": [{"id": "chatgptplusplan"}]},
                    "is_eligible_for_yearly_plus_new_user_subscription": True,
                }
            }
        }
        result = parse_accounts_check(data)
        self.assertTrue(result["plus_yearly_new_user_eligible"])
        self.assertFalse(result["plus_trial_eligible"])


if __name__ == "__main__":
    unittest.main()
