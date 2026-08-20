# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import extract_link_service as service


class ExtractLinkLinkppTests(unittest.TestCase):
    def test_linkpp_provider_is_detected(self):
        with patch.object(service, "_runtime_setting", side_effect=lambda name, default=None: {
            "EXTRACT_LINK_PROVIDER": "linkpp",
        }.get(name, default)):
            self.assertEqual(service._provider("http://127.0.0.1:5572"), "linkpp")

    def test_create_linkpp_job_uses_country_proxy_and_billing_country(self):
        values = {
            "EXTRACT_LINK_LINKPP_COUNTRY": "BR",
            "EXTRACT_LINK_LINKPP_BILLING_COUNTRY": "DE",
            "EXTRACT_LINK_LINKPP_CHECKOUT_ATTEMPTS": "3",
            "EXTRACT_LINK_LINKPP_PROVIDER_ATTEMPTS": "5",
            "EXTRACT_LINK_LINKPP_STRIPE_CHECKOUT": "False",
            "EXTRACT_LINK_LINKPP_STRIPE_ENGINE": "python",
            "EXTRACT_LINK_LINKPP_STRIPE_PROMO_STRATEGY": "post_update",
        }
        with patch.object(service, "_runtime_setting", side_effect=lambda name, default=None: values.get(name, default)), \
             patch.object(service, "_api_base", return_value="http://127.0.0.1:5572"), \
             patch.object(service, "_is_linkpp", return_value=True), \
             patch.object(service, "_linkpp_proxy_pool", return_value=["socks5h://user:pass@proxy:3010"]), \
             patch.object(service, "_http_json", return_value=(202, {"job_id": "pp-1", "status": "queued"})) as http:
            result = service._create_extract_job(
                token="opaque-token", link_type="paypal", cdk="", email="user@example.com"
            )
        self.assertEqual(result["provider"], "linkpp")
        payload = http.call_args.kwargs["payload"]
        self.assertEqual(payload["country"], "BR")
        self.assertEqual(payload["billing_country"], "DE")
        self.assertEqual(payload["proxies"], ["socks5h://user:pass@proxy:3010"])
        self.assertFalse(payload["stripe_checkout"])

    def test_poll_linkpp_maps_paypal_ba_url(self):
        responses = iter([
            (200, {"status": "running"}),
            (200, {
                "status": "success",
                "result": {
                    "paypal_approve_url": "https://www.paypal.com/agreements/approve?ba_token=BA-TEST",
                    "ba_token": "BA-TEST",
                },
            }),
        ])
        with patch.object(service, "_api_base", return_value="http://127.0.0.1:5572"), \
             patch.object(service, "_http_json", side_effect=lambda *args, **kwargs: next(responses)), \
             patch.object(service.time, "sleep"), \
             patch.object(service.db, "update_account_extract"):
            result = service._poll_linkpp_task(task_id="pp-1", account_id=7, link_type="paypal")
        self.assertTrue(result["ok"])
        self.assertEqual(result["link_type"], "paypal")
        self.assertEqual(result["result"]["payment_method"], "paypal")
        self.assertIn("BA-TEST", result["result"]["long_url"])

    def test_create_linkpp_job_defaults_to_hosted_go_flow(self):
        with patch.object(service, "_runtime_setting", side_effect=lambda _name, default=None: default), \
             patch.object(service, "_api_base", return_value="http://127.0.0.1:5572"), \
             patch.object(service, "_is_linkpp", return_value=True), \
             patch.object(service, "_linkpp_proxy_pool", return_value=["socks5h://user:pass@proxy:3010"]), \
             patch.object(service, "_http_json", return_value=(202, {"job_id": "pp-2"})) as http:
            service._create_extract_job(
                token="opaque-token", link_type="paypal", cdk="", email="user@example.com"
            )

        payload = http.call_args.kwargs["payload"]
        self.assertTrue(payload["stripe_checkout"])
        self.assertEqual(payload["stripe_engine"], "go")
        self.assertEqual(payload["stripe_promo_strategy"], "mixed")


if __name__ == "__main__":
    unittest.main()
