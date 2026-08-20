# -*- coding: utf-8 -*-
import json
import re
import unittest
from unittest.mock import Mock, patch

from curl_cffi.const import CurlOpt

from config import proxy as proxy_cfg
from core.chatgpt_plan import resolve_plan_check_route
from core.session import BrowserSession


class _FakeCookies:
    def __init__(self):
        self.jar = []

    def set(self, *_args, **_kwargs):
        return None


class _FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.cookies = _FakeCookies()
        self.proxies = {}
        self.timeout = None


class ThorDataProxyTests(unittest.TestCase):
    def setUp(self):
        self.pool = list(proxy_cfg.PROXY_POOL)
        self.refreshed_at = proxy_cfg._THORDATA_REFRESHED_AT
        self.meta = dict(proxy_cfg._THORDATA_META)
        self.country_pools = {key: list(value) for key, value in proxy_cfg._THORDATA_COUNTRY_POOLS.items()}
        self.country_refreshed = dict(proxy_cfg._THORDATA_COUNTRY_REFRESHED_AT)
        self.bans = dict(proxy_cfg._TEMP_BANNED)

    def tearDown(self):
        proxy_cfg.PROXY_POOL[:] = self.pool
        proxy_cfg._THORDATA_REFRESHED_AT = self.refreshed_at
        proxy_cfg._THORDATA_META.clear()
        proxy_cfg._THORDATA_META.update(self.meta)
        proxy_cfg._THORDATA_COUNTRY_POOLS.clear()
        proxy_cfg._THORDATA_COUNTRY_POOLS.update(self.country_pools)
        proxy_cfg._THORDATA_COUNTRY_REFRESHED_AT.clear()
        proxy_cfg._THORDATA_COUNTRY_REFRESHED_AT.update(self.country_refreshed)
        proxy_cfg._TEMP_BANNED.clear()
        proxy_cfg._TEMP_BANNED.update(self.bans)

    def test_normalizes_entry_nodes_as_https_proxies(self):
        pool = proxy_cfg._normalize_thordata_entries(
            "43.135.181.9:18155\nhttps://43.135.181.9:11106\ninvalid\n"
        )
        self.assertEqual(pool, [
            "https://43.135.181.9:18155",
            "https://43.135.181.9:11106",
        ])

    def test_https_proxy_uses_proxy_only_tls_exceptions(self):
        options = proxy_cfg.proxy_curl_options("https://43.135.181.9:18155")
        self.assertEqual(options[CurlOpt.PROXY_SSL_VERIFYPEER], 0)
        self.assertEqual(options[CurlOpt.PROXY_SSL_VERIFYHOST], 0)
        self.assertNotIn(CurlOpt.SSL_VERIFYPEER, options)

    def test_fetches_dynamic_pool_without_treating_entries_as_exits(self):
        response = Mock(status_code=200, text="43.135.181.9:18155\n43.135.181.9:11106\n")
        proxy_cfg.PROXY_POOL[:] = []
        proxy_cfg._THORDATA_REFRESHED_AT = 0.0
        with patch.object(proxy_cfg, "THORDATA_CUSTOMER", "Thor-test"), \
             patch("curl_cffi.requests.get", return_value=response) as request_get:
            pool = proxy_cfg._fetch_thordata_pool(force=True)
        self.assertEqual(len(pool), 2)
        self.assertTrue(all(item.startswith("https://") for item in pool))
        called_url = request_get.call_args.args[0]
        self.assertIn("td-customer=Thor-test", called_url)
        self.assertNotIn("ipinfo.thordata.com", called_url)

    def test_jp_country_pool_is_isolated_from_us_registration_pool(self):
        response = Mock(status_code=200, text="43.135.181.9:18155\n43.135.181.9:11106\n")
        registration_pool = ["https://43.135.181.9:19001"]
        proxy_cfg.PROXY_POOL[:] = registration_pool
        proxy_cfg._THORDATA_COUNTRY_POOLS.clear()
        proxy_cfg._THORDATA_COUNTRY_REFRESHED_AT.clear()
        with patch.object(proxy_cfg, "THORDATA_CUSTOMER", "Thor-test"), \
             patch("curl_cffi.requests.get", return_value=response) as request_get:
            pool = proxy_cfg.fetch_thordata_country_pool("JP", 3, force=True)
        self.assertEqual(len(pool), 2)
        self.assertEqual(proxy_cfg.PROXY_POOL, registration_pool)
        called_url = request_get.call_args.args[0]
        self.assertIn("country=JP", called_url)
        self.assertIn("number=3", called_url)
        self.assertIn("sesstype=2", called_url)

    def test_jp_health_probe_validates_jp_instead_of_global_us(self):
        entry = "https://43.135.181.9:18155"
        with patch.object(proxy_cfg, "pick_country_proxy", return_value=entry), \
             patch.object(proxy_cfg, "probe_proxy", return_value={"ok": True, "country": "JP"}) as probe:
            selected = proxy_cfg.pick_healthy_country_proxy("JP", number=3, max_candidates=1)
        self.assertEqual(selected, entry)
        probe.assert_called_once_with(entry, expected_country="JP")

    def test_probe_reports_real_exit_metadata(self):
        payload = {
            "ip": "68.48.90.198",
            "country": "US",
            "region": "Michigan",
            "org": "AS7922 Comcast Cable Communications, LLC",
            "asn": "AS7922",
            "timezone": "America/Detroit",
        }
        response = Mock(status_code=200, text=json.dumps(payload))
        entry = "https://43.135.181.9:18155"
        with patch("curl_cffi.requests.get", return_value=response) as request_get, \
             patch.object(proxy_cfg, "THORDATA_PURITY_CHECK", False):
            result = proxy_cfg.probe_proxy(entry, timeout=5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["ip"], "68.48.90.198")
        self.assertNotEqual(result["ip"], "43.135.181.9")
        self.assertEqual(result["country"], "US")
        kwargs = request_get.call_args.kwargs
        self.assertEqual(kwargs["proxies"]["https"], entry)
        self.assertEqual(kwargs["curl_options"][CurlOpt.PROXY_SSL_VERIFYHOST], 0)

    def test_cliproxy_gateway_ip_is_not_reported_as_verified_exit(self):
        gateway = "107.151.249.77"
        entry = f"socks5h://{gateway}:18657"
        response = Mock(status_code=200, text=json.dumps({
            "ip": gateway,
            "country": "GB",
        }))
        with patch.object(proxy_cfg, "THORDATA_ENABLED", False), \
             patch.object(proxy_cfg, "CLIPROXY_POOL_ENABLED", True), \
             patch.object(proxy_cfg, "cliproxy_pool_enabled", return_value=True), \
             patch("curl_cffi.requests.get", return_value=response):
            result = proxy_cfg.probe_proxy(entry, expected_country="GB")
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified_exit"])
        self.assertEqual(result["gateway_ip"], gateway)
        self.assertEqual(result["entry_port"], 18657)
        self.assertIsNone(result["exit_ip"])
        self.assertIsNone(result["ip"])
        self.assertEqual(result["observed_ip"], gateway)
        self.assertIsNone(result["country"])
        self.assertEqual(result["observed_country"], "GB")
        self.assertEqual(proxy_cfg.get_proxy_metadata(entry)["gateway_ip"], gateway)

    def test_dynamic_probe_requires_expected_country_when_requested(self):
        entry = "socks5h://107.151.249.77:18658"
        response = Mock(status_code=200, text=json.dumps({
            "ip": "81.1.2.3",
            "country": "US",
        }))
        with patch.object(proxy_cfg, "THORDATA_ENABLED", False), \
             patch.object(proxy_cfg, "CLIPROXY_POOL_ENABLED", True), \
             patch.object(proxy_cfg, "cliproxy_pool_enabled", return_value=True), \
             patch("curl_cffi.requests.get", return_value=response):
            result = proxy_cfg.probe_proxy(entry, expected_country="GB")
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_ip"], "81.1.2.3")
        self.assertEqual(result["error"], "出口国家不匹配: US != GB")

    def test_all_failed_thordata_entries_return_empty_instead_of_direct(self):
        entry = "https://43.135.181.9:18155"
        with patch.object(proxy_cfg, "CLIPROXY_POOL_ENABLED", False), \
             patch.object(proxy_cfg, "pick_proxy", side_effect=[entry, RuntimeError("empty")]), \
             patch.object(proxy_cfg, "probe_proxy", return_value={"ok": False, "error": "down"}), \
             patch.object(proxy_cfg, "ensure_reg_proxy_pool", return_value=[]):
            selected = proxy_cfg.pick_healthy_proxy(max_candidates=2)
        self.assertEqual(selected, "")

    def test_cliproxy_normalization_preserves_credentials(self):
        raw = "user-region-GB-sid-test-t-5:secret@sg.cliproxy.io:3010"
        self.assertEqual(
            proxy_cfg._normalize_proxy_url(raw),
            "http://user-region-GB-sid-test-t-5:secret@sg.cliproxy.io:3010",
        )

    def test_cliproxy_whitelist_error_is_not_treated_as_a_proxy(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"45.39.198.9 not added to whitelist"
        opener = Mock()
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "45.39.198.9.*白名单"):
                proxy_cfg._cliproxy_fetch_pool_url("https://api.cliproxy.io/white/api?type=http")

    def test_cliproxy_text_entries_use_configured_socks5_protocol(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"128.1.12.147:16612\n128.1.12.147:16613\n"
        opener = Mock()
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener), \
             patch.object(proxy_cfg, "CLIPROXY_PROXY_SCHEME", "socks5h"):
            pool = proxy_cfg._cliproxy_fetch_pool_url(
                "https://api.cliproxy.io/white/api?region=JP&type=txt&format=n"
            )
        self.assertEqual(pool, [
            "socks5h://128.1.12.147:16612",
            "socks5h://128.1.12.147:16613",
        ])

    def test_banned_static_proxies_are_not_reused(self):
        entry = "http://user:pass@sg.cliproxy.io:3010"
        with patch.object(proxy_cfg, "THORDATA_ENABLED", False), \
             patch.object(proxy_cfg, "CLIPROXY_POOL_ENABLED", False), \
             patch.object(proxy_cfg, "PROXY_POOL", [entry]), \
             patch.object(proxy_cfg, "_active_temp_bans", return_value={entry}):
            self.assertEqual(proxy_cfg.list_proxy_pool(), [])

    def test_cliproxy_mode_rejects_legacy_http_proxy(self):
        with patch.object(proxy_cfg, "THORDATA_ENABLED", False), \
             patch.object(proxy_cfg, "CLIPROXY_POOL_ENABLED", True), \
             patch.object(proxy_cfg, "CLIPROXY_PROXY_SCHEME", "socks5h"):
            self.assertFalse(proxy_cfg.proxy_allowed(
                "http://user:pass@sg.cliproxy.io:3010"
            ))
            self.assertTrue(proxy_cfg.proxy_allowed(
                "socks5h://128.1.12.147:16633"
            ))

    def test_plan_check_replaces_saved_proxy_with_dynamic_cliproxy(self):
        entry = "socks5h://128.1.12.147:16633"
        legacy = "http://user:pass@sg.cliproxy.io:3010"
        with patch.object(proxy_cfg, "THORDATA_ENABLED", False), \
             patch.object(proxy_cfg, "cliproxy_pool_enabled", return_value=True), \
             patch.object(proxy_cfg, "PLAN_CHECK_CLIPROXY_COUNTRY", "JP"), \
             patch.object(proxy_cfg, "new_cliproxy_country_session", return_value=entry), \
             patch.object(proxy_cfg, "probe_proxy", return_value={"ok": True}) as probe, \
             patch.object(proxy_cfg, "get_proxy_metadata", return_value={"ip": "203.0.113.8", "country": "JP"}):
            route = resolve_plan_check_route(legacy)
        self.assertEqual(route["proxy"], entry)
        self.assertEqual(route["network_route"], "proxy")
        self.assertEqual(route["proxy_mode"], "cliproxy_dynamic_enforced")
        self.assertEqual(route["plan_check_cliproxy_country"], "JP")
        self.assertEqual(route["proxy_exit_country"], "JP")
        probe.assert_not_called()

    def test_plan_check_replaces_explicit_direct_with_thordata(self):
        entry = "https://43.135.181.9:18155"
        with patch.object(proxy_cfg, "THORDATA_ENABLED", True), \
             patch.object(proxy_cfg, "pick_healthy_country_proxy", return_value=entry), \
             patch.object(proxy_cfg, "get_proxy_metadata", return_value={"ip": "203.0.113.8", "country": "JP"}):
            route = resolve_plan_check_route("")
        self.assertEqual(route["proxy"], entry)
        self.assertEqual(route["network_route"], "proxy")
        self.assertEqual(route["proxy_mode"], "plan_country_enforced")
        self.assertEqual(route["plan_check_proxy_country"], "JP")
        self.assertEqual(route["proxy_exit_country"], "JP")

    def test_plan_check_replaces_legacy_local_proxy_with_thordata(self):
        entry = "https://43.135.181.9:18155"
        legacy = "socks5h://127.0.0.1:17891"
        with patch.object(proxy_cfg, "THORDATA_ENABLED", True), \
             patch.object(proxy_cfg, "pick_healthy_country_proxy", return_value=entry), \
             patch.object(proxy_cfg, "get_proxy_metadata", return_value={}):
            route = resolve_plan_check_route(legacy)
        self.assertEqual(route["proxy"], entry)
        self.assertEqual(route["network_route"], "proxy")
        self.assertEqual(route["proxy_mode"], "plan_country_enforced")

    @patch("core.session.Session", _FakeSession)
    def test_browser_session_replaces_explicit_direct_with_thordata(self):
        entry = "https://43.135.181.9:18155"
        with patch("core.session.pick_proxy", return_value=entry), \
             patch("config.proxy.proxy_required", return_value=True), \
             patch("config.proxy.proxy_allowed", return_value=False):
            session = BrowserSession(proxy="", detect_exit_geo=False)
        self.assertEqual(session.proxy, entry)
        self.assertEqual(session.session.proxies["https"], entry)
        self.assertEqual(
            session.session.kwargs["curl_options"][CurlOpt.PROXY_SSL_VERIFYPEER],
            0,
        )

    @patch("core.session.Session", _FakeSession)
    def test_browser_session_normalizes_cliproxy_host_port_credentials(self):
        raw = "us.cliproxy.io:3010:test-user-region-GB:test-password"
        with patch.object(proxy_cfg, "THORDATA_ENABLED", False), \
             patch.object(proxy_cfg, "CLIPROXY_POOL_ENABLED", True), \
             patch.object(proxy_cfg, "CLIPROXY_PROXY_SCHEME", "socks5h"):
            session = BrowserSession(proxy=raw, detect_exit_geo=False)
        self.assertRegex(
            session.proxy,
            re.compile(
                r"^socks5h://test-user-region-GB-sid-[A-Za-z0-9]{8}-t-5:"
                r"test-password@us\.cliproxy\.io:3010$"
            ),
        )

    def test_existing_cliproxy_sid_is_preserved(self):
        proxy = "socks5h://user-region-GB-sid-EYM7RyXf-t-5:pass@sg.cliproxy.io:3010"
        self.assertEqual(proxy_cfg.ensure_cliproxy_session(proxy), proxy)


if __name__ == "__main__":
    unittest.main()
