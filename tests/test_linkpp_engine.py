from __future__ import annotations

import unittest

from vendor.link_pp.handoff.countries import get_country
from vendor.link_pp.handoff.engine import HandoffEngine, RunSpec
from vendor.link_pp.handoff.gateway import CheckoutArtifact
from vendor.link_pp.handoff.proxies import ProxyPool, parse_proxy_lines
from vendor.link_pp.handoff.security import TokenProfile


class _BlockedGateway:
    def __init__(self):
        self.checkout_calls = 0
        self.provider_calls = 0

    def create_checkout(self, **kwargs):
        self.checkout_calls += 1
        return CheckoutArtifact(
            session_id="cs_live_test",
            processor_entity="openai_llc",
            country="GB",
            currency="GBP",
            checkout_url="https://chatgpt.com/checkout/openai_llc/cs_live_test",
        )

    def attempt_provider(self, **kwargs):
        self.provider_calls += 1
        raise RuntimeError("manual_approval approve blocked: result=blocked")


class LinkppEngineTests(unittest.TestCase):
    def test_approval_block_stops_repeated_checkouts(self):
        gateway = _BlockedGateway()
        spec = RunSpec(
            access_token="test-token",
            token_profile=TokenProfile("owner@example.com", "Owner", "acct"),
            proxy_country=get_country("GB"),
            checkout_country=get_country("GB"),
            proxies=ProxyPool(parse_proxy_lines("proxy.example:1080")),
            checkout_attempts=3,
            provider_attempts=5,
            stripe_checkout=True,
            stripe_engine="go",
            stripe_promo_strategy="mixed",
        )

        with self.assertRaisesRegex(RuntimeError, "审批被拒绝"):
            HandoffEngine(gateway).run(
                spec, emit=lambda *_args: None, is_cancelled=lambda: False
            )

        self.assertEqual(gateway.checkout_calls, 1)
        self.assertEqual(gateway.provider_calls, 1)


if __name__ == "__main__":
    unittest.main()
