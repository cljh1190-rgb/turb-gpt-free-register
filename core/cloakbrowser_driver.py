# -*- coding: utf-8 -*-
"""CloakBrowser 的 Selenium 风格轻量适配层。"""
from __future__ import annotations

import logging
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from config import cloakbrowser as _cfg

logger = logging.getLogger(__name__)


def stable_cloak_fingerprint_seed(identity: str) -> str:
    """Return a stable numeric Cloak fingerprint seed for one account identity."""
    normalized = str(identity or "").strip().lower()
    digest = hashlib.sha256(f"cloak-fingerprint:{normalized}".encode("utf-8")).digest()
    return str(int.from_bytes(digest[:8], "big") % 2_000_000_000 + 1)


def stable_cloak_device_id(identity: str) -> str:
    """Keep oai-did stable when the same account retries on another proxy."""
    normalized = str(identity or "").strip().lower()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cloak-oai-device:{normalized}"))


def prime_cloak_device_id(driver: "CloakSeleniumDriver", device_id: str) -> None:
    """Seed the browser cookie jar before the first ChatGPT/Auth navigation."""
    value = str(device_id or "").strip()
    if not value:
        return
    context = driver.context or getattr(driver.page, "context", None)
    if context is None:
        return
    context.add_cookies([
        {"name": "oai-did", "value": value, "domain": ".chatgpt.com", "path": "/", "secure": True, "sameSite": "Lax"},
        {"name": "oai-did", "value": value, "domain": ".auth.openai.com", "path": "/", "secure": True, "sameSite": "Lax"},
        {"name": "oai-did", "value": value, "domain": ".sentinel.openai.com", "path": "/", "secure": True, "sameSite": "Lax"},
    ])


def capture_cloak_environment(
    driver: "CloakSeleniumDriver",
    opened: CloakOpenResult | None = None,
    *,
    fallback_device_id: str = "",
) -> dict:
    """Capture the real secure-page fingerprint used by CloakBrowser.

    The returned browser_profile follows config.browser's key names so later
    protocol requests can reuse the same UA, Client Hints, locale and timezone.
    """
    observed = driver.page.evaluate(
        """async () => {
          const uaData = navigator.userAgentData;
          let high = {};
          if (uaData && uaData.getHighEntropyValues) {
            try {
              high = await uaData.getHighEntropyValues([
                'architecture', 'bitness', 'model', 'platformVersion',
                'uaFullVersion', 'fullVersionList'
              ]);
            } catch (_) {}
          }
          return {
            userAgent: navigator.userAgent,
            platform: navigator.platform,
            vendor: navigator.vendor,
            language: navigator.language,
            languages: Array.from(navigator.languages || []),
            webdriver: navigator.webdriver,
            hardwareConcurrency: navigator.hardwareConcurrency,
            deviceMemory: navigator.deviceMemory || null,
            screenWidth: screen.width,
            screenHeight: screen.height,
            availWidth: screen.availWidth,
            availHeight: screen.availHeight,
            colorDepth: screen.colorDepth,
            pixelDepth: screen.pixelDepth,
            devicePixelRatio: window.devicePixelRatio,
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            timezoneOffset: new Date().getTimezoneOffset(),
            requestIdleCallback: typeof window.requestIdleCallback === 'function',
            uaData: uaData ? {
              brands: Array.from(uaData.brands || []),
              mobile: !!uaData.mobile,
              platform: uaData.platform || '',
              high,
            } : null,
          };
        }"""
    ) or {}
    ua = str(observed.get("userAgent") or "")
    version_match = re.search(r"(?:Chrome|CriOS)/(\d+(?:\.\d+){0,3})", ua)
    full_version = version_match.group(1) if version_match else ""
    major = full_version.split(".", 1)[0] if full_version else ""
    ua_data = observed.get("uaData") or {}
    high = ua_data.get("high") or {}
    if high.get("uaFullVersion"):
        full_version = str(high.get("uaFullVersion"))
        major = full_version.split(".", 1)[0]

    def _brand_list(values) -> str:
        return ", ".join(
            f'"{str(item.get("brand") or "")}";v="{str(item.get("version") or "")}"'
            for item in (values or [])
            if isinstance(item, dict) and item.get("brand")
        )

    locale_meta = dict(((opened.raw or {}).get("locale") if opened else {}) or {})
    geo = dict(locale_meta.get("geo") or {})
    platform_name = str(ua_data.get("platform") or "")
    if not platform_name:
        platform_name = "Windows" if "Windows" in ua else "macOS" if "Macintosh" in ua else ""
    language = str(observed.get("language") or locale_meta.get("locale") or "en-US")
    languages = list(observed.get("languages") or [language])
    accept_language = str(locale_meta.get("accept_language") or language)
    if "," not in accept_language:
        accept_language = f"{language},{language.split('-', 1)[0]};q=0.9"
    timezone_iana = str(observed.get("timezone") or locale_meta.get("timezone") or "UTC")
    timezone_offset = -int(observed.get("timezoneOffset") or 0)
    try:
        from config.browser import TIMEZONE_NAME_BY_IANA
        timezone_name = str(TIMEZONE_NAME_BY_IANA.get(timezone_iana) or timezone_iana)
    except Exception:
        timezone_name = timezone_iana
    brands = _brand_list(ua_data.get("brands"))
    full_brands = _brand_list(high.get("fullVersionList")) or brands
    profile = {
        "source": "cloak_runtime",
        "browser_family": "chrome",
        "browser_os": platform_name,
        "user_agent": ua,
        "chrome_major": major,
        "chrome_full_version": full_version,
        "navigator_platform": str(observed.get("platform") or ""),
        "navigator_vendor": str(observed.get("vendor") or "Google Inc."),
        "navigator_language": language,
        "navigator_languages": languages,
        "accept_language": accept_language,
        "user_agent_data_platform": platform_name,
        "send_client_hints": bool(ua_data),
        "sec_ch_ua": brands,
        "sec_ch_ua_mobile": "?1" if ua_data.get("mobile") else "?0",
        "sec_ch_ua_platform": f'"{platform_name}"' if platform_name else "",
        "sec_ch_ua_platform_version": f'"{str(high.get("platformVersion") or "")}"',
        "sec_ch_ua_arch": f'"{str(high.get("architecture") or "")}"',
        "sec_ch_ua_bitness": f'"{str(high.get("bitness") or "")}"',
        "sec_ch_ua_model": f'"{str(high.get("model") or "")}"',
        "sec_ch_ua_full_version_list": full_brands,
        "screen_width": int(observed.get("screenWidth") or 0),
        "screen_height": int(observed.get("screenHeight") or 0),
        "screen_avail_width": int(observed.get("availWidth") or 0),
        "screen_avail_height": int(observed.get("availHeight") or 0),
        "color_depth": int(observed.get("colorDepth") or 24),
        "pixel_depth": int(observed.get("pixelDepth") or 24),
        "device_pixel_ratio": float(observed.get("devicePixelRatio") or 1),
        "hardware_concurrency": int(observed.get("hardwareConcurrency") or 4),
        "device_memory": int(observed.get("deviceMemory") or 8),
        "timezone_iana": timezone_iana,
        "timezone_offset_minutes": timezone_offset,
        "timezone_name": timezone_name,
        "geo": geo,
        "window_feature_flags": {"requestIdleCallback": 1 if observed.get("requestIdleCallback") else 0},
        "webdriver": bool(observed.get("webdriver")),
    }

    device_id = str(fallback_device_id or "").strip()
    try:
        context = driver.context or getattr(driver.page, "context", None)
        for cookie in (context.cookies() if context is not None else []):
            if cookie.get("name") == "oai-did" and cookie.get("value"):
                device_id = str(cookie.get("value"))
                break
    except Exception:
        pass
    return {
        "device_id": device_id,
        "browser_profile": profile,
        "summary": (
            f"{platform_name or '?'} Chrome/{full_version or '?'} "
            f"{language} {profile['timezone_iana']} "
            f"{profile['screen_width']}x{profile['screen_height']}"
        ),
    }


@dataclass
class CloakOpenResult:
    profile_id: str = "cloakbrowser"
    raw: dict | None = None


class CloakElement:
    def __init__(self, page, locator=None, handle=None):
        self.page = page
        self.locator = locator
        self.handle = handle

    def _handle(self):
        if self.handle is not None:
            return self.handle
        return self.locator.element_handle(timeout=5000)

    def _eval(self, expression: str, arg: Any = None) -> Any:
        if self.locator is not None:
            try:
                return self.locator.evaluate(expression, arg, timeout=3000)
            except TypeError:
                return self.locator.evaluate(expression, arg)
        return self.handle.evaluate(expression, arg)

    def _eval_handle(self, expression: str, arg: Any = None) -> Any:
        h = self._handle()
        return h.evaluate_handle(expression, arg)

    def is_displayed(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_visible(timeout=800))
            return bool(self.handle.evaluate("el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'"))
        except Exception:
            return False

    def is_enabled(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_enabled(timeout=800))
            return bool(self.handle.evaluate("el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'"))
        except Exception:
            return False

    def click(self) -> None:
        if self.locator is not None:
            self.locator.click(timeout=10000)
        else:
            self.handle.click(timeout=10000)

    def clear(self) -> None:
        try:
            if self.locator is not None:
                self.locator.fill("", timeout=10000)
            else:
                self.handle.fill("", timeout=10000)
        except Exception:
            # 部分非 input 元素不支持 fill，回退键盘清空。
            self.click()
            self.page.keyboard.press("Meta+A")
            self.page.keyboard.press("Backspace")

    @property
    def tag_name(self) -> str:
        try:
            return str(self._eval("el => el.tagName.toLowerCase()") or "")
        except Exception:
            return ""

    def send_keys(self, *values: str) -> None:
        # 兼容 Selenium: el.send_keys(Keys.COMMAND, 'a')。
        text = "".join(str(v or "") for v in values)
        lower = text.lower()
        try:
            self.click()
        except Exception:
            pass
        if "\ue03d" in text or "\ue009" in text or "command" in lower or "control" in lower:
            # Selenium Keys.CONTROL/COMMAND 编码可能传入私有区字符；这里按全选处理。
            try:
                self.page.keyboard.press("Meta+A")
            except Exception:
                self.page.keyboard.press("Control+A")
            return
        try:
            if self.locator is not None:
                self.locator.fill(text, timeout=10000)
            else:
                self.handle.fill(text, timeout=10000)
        except Exception:
            self.page.keyboard.type(text, delay=35)

    def get_attribute(self, name: str) -> str | None:
        try:
            if self.locator is not None:
                return self.locator.get_attribute(name, timeout=1000)
            return self.handle.get_attribute(name)
        except Exception:
            return None

    @property
    def text(self) -> str:
        """兼容 Selenium WebElement.text；Cloak/Playwright 元素本身没有 .text。"""
        try:
            val = self._eval(
                "el => String((el && (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '')) || '').trim()"
            )
            return str(val or "")
        except Exception:
            try:
                return str(self.get_attribute("value") or self.get_attribute("aria-label") or "")
            except Exception:
                return ""


class _SwitchTo:
    def __init__(self, driver: "CloakSeleniumDriver"):
        self._driver = driver

    def window(self, handle: str) -> None:
        self._driver._switch_window(handle)


class CloakSeleniumDriver:
    """只实现本项目 Roxy Selenium 流程实际用到的 WebDriver 子集。"""

    def __init__(self, browser: Any, context: Any | None, page: Any):
        self.browser = browser
        self.context = context
        self.page = page
        self._page_load_timeout_ms = int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90) * 1000
        self.switch_to = _SwitchTo(self)

    @property
    def current_url(self) -> str:
        return str(getattr(self.page, "url", "") or "")

    @property
    def window_handles(self) -> list[str]:
        pages = self._pages()
        return [str(i) for i in range(len(pages))]

    def _pages(self) -> list[Any]:
        try:
            if self.context is not None:
                return list(self.context.pages)
        except Exception:
            pass
        try:
            contexts = list(getattr(self.browser, "contexts", []) or [])
            pages = []
            for ctx in contexts:
                pages.extend(list(getattr(ctx, "pages", []) or []))
            return pages or [self.page]
        except Exception:
            return [self.page]

    def _switch_window(self, handle: str) -> None:
        pages = self._pages()
        idx = int(handle)
        self.page = pages[idx]
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def set_page_load_timeout(self, seconds: int) -> None:
        self._page_load_timeout_ms = int(seconds) * 1000
        try:
            self.page.set_default_navigation_timeout(self._page_load_timeout_ms)
            self.page.set_default_timeout(self._page_load_timeout_ms)
        except Exception:
            pass

    def get(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def back(self) -> None:
        self.page.go_back(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def refresh(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def quit(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass

    def find_elements(self, by: Any, selector: str) -> list[CloakElement]:
        loc = self._locator(by, selector)
        try:
            count = min(int(loc.count()), 200)
        except Exception:
            count = 0
        return [CloakElement(self.page, loc.nth(i)) for i in range(count)]

    def find_element(self, by: Any, selector: str) -> CloakElement:
        els = self.find_elements(by, selector)
        if not els:
            raise RuntimeError(f"找不到页面元素: {selector}")
        return els[0]

    def _locator(self, by: Any, selector: str):
        by_s = str(by or "").lower()
        if "xpath" in by_s or str(selector).startswith("//"):
            return self.page.locator(f"xpath={selector}")
        return self.page.locator(selector)

    def execute_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=False)

    def execute_async_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=True)

    def execute_cdp_cmd(self, cmd: str, params: dict | None = None) -> Any:
        params = params or {}
        try:
            client = self.context.new_cdp_session(self.page) if self.context is not None else self.page.context.new_cdp_session(self.page)
            return client.send(cmd, params)
        except Exception as exc:
            logger.debug("[Cloak] CDP 命令失败 %s: %s", cmd, exc)
            return None

    def _serialize_args(self, args: tuple[Any, ...]) -> tuple[CloakElement | None, list[Any]]:
        """拆分 Selenium 脚本参数。

        Playwright 的 JSHandle/ElementHandle 不能可靠地嵌在 dict/list payload 中跨
        page.evaluate 传递；Selenium 脚本最常见模式是 `arguments[0]` 为元素，
        因此这里把第一个 CloakElement 作为真实 DOM `el` 传入，其它参数保持
        JSON 可序列化。
        """
        first_el = args[0] if args and isinstance(args[0], CloakElement) else None
        rest = list(args[1:] if first_el else args)
        cleaned = []
        for item in rest:
            if isinstance(item, CloakElement):
                # 极少数脚本会传多个元素；用真实 handle 直接会在嵌套 payload 中失效，
                # 这里退化为 None，比把错误对象传进 JS 更安全。
                cleaned.append(None)
            else:
                cleaned.append(item)
        return first_el, cleaned

    @staticmethod
    def _unwrap_js_result(page, handle: Any) -> Any:
        try:
            element = handle.as_element()
        except Exception:
            element = None
        if element is not None:
            return CloakElement(page, handle=element)
        try:
            return handle.json_value()
        except Exception as exc:
            msg = str(exc)
            if "Execution context was destroyed" in msg or "navigation" in msg.lower():
                logger.info("[Cloak] JS 执行后页面发生跳转，忽略返回值读取失败：%s", msg[:160])
                return {"ok": True, "reason": "navigation_after_script"}
            raise
        finally:
            try:
                handle.dispose()
            except Exception:
                pass

    def _evaluate(self, script: str, args: tuple[Any, ...], async_mode: bool) -> Any:
        first_el, serial_args = self._serialize_args(args)
        if async_mode:
            wrapper = """async ({script, args}) => {
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            element_wrapper = """async (el, payload) => {
              const args = [el, ...payload.args];
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', payload.script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            if first_el is not None:
                result = first_el._eval(element_wrapper, {"script": script, "args": serial_args})
            else:
                result = self.page.evaluate(wrapper, {"script": script, "args": serial_args})
            if isinstance(result, dict) and result.get("__cloak_timeout"):
                raise TimeoutError("execute_async_script timeout")
            return result

        # Selenium 脚本经常以 `return ...` 为主体；用 Function 保持语义。
        wrapper = """({script, args}) => {
          const fn = new Function(...args.map((_, i) => 'a' + i), script);
          return fn(...args);
        }"""
        element_wrapper = """(el, payload) => {
          const args = [el, ...payload.args];
          const fn = new Function(...args.map((_, i) => 'a' + i), payload.script);
          return fn(...args);
        }"""
        if first_el is not None:
            handle = first_el._eval_handle(element_wrapper, {"script": script, "args": serial_args})
        else:
            handle = self.page.evaluate_handle(wrapper, {"script": script, "args": serial_args})
        return self._unwrap_js_result(self.page, handle)


def _normalize_proxy(proxy: str | None) -> str | None:
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    return proxy.replace("socks5h://", "socks5://")


def _detect_cloak_exit_geo(proxy_url: str | None = None) -> dict:
    """按当前/代理出口检测地理信息，供 Cloak 显式 locale/timezone 使用。"""
    try:
        from curl_cffi import requests as creq
        from config import browser as _browser_cfg
        from config import proxy as _proxy_cfg
        endpoints = list(getattr(_browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
        timeout = float(getattr(_browser_cfg, "IP_GEO_TIMEOUT", 6) or 6)
    except Exception:
        return {}
    if proxy_url and bool(getattr(_proxy_cfg, "THORDATA_ENABLED", False)):
        thor_endpoint = str(getattr(_proxy_cfg, "THORDATA_IPINFO_URL", "") or "").strip()
        if thor_endpoint:
            endpoints = [thor_endpoint, *[url for url in endpoints if url != thor_endpoint]]
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for url in endpoints:
        try:
            resp = creq.get(
                url,
                headers=headers,
                proxies=proxies,
                timeout=timeout,
                impersonate="chrome146",
                curl_options=_proxy_cfg.proxy_curl_options(proxy_url),
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            timezone = data.get("timezone")
            if isinstance(timezone, dict):
                timezone = timezone.get("id") or timezone.get("name")
            geo = {
                "ip": data.get("ip") or data.get("query"),
                "country": (data.get("country") or data.get("country_code") or data.get("countryCode") or "").upper(),
                "region": data.get("region") or data.get("regionName"),
                "city": data.get("city"),
                "timezone": timezone or "",
                "org": data.get("org") or data.get("isp") or (data.get("connection") or {}).get("org"),
            }
            if geo.get("country") or geo.get("timezone"):
                logger.info(
                    "[Cloak] 出口IP地理信息：ip=%s country=%s city=%s timezone=%s",
                    geo.get("ip") or "?", geo.get("country") or "?", geo.get("city") or "?", geo.get("timezone") or "?",
                )
                return geo
        except Exception as exc:
            logger.debug("[Cloak] 出口 IP 地理检测失败 endpoint=%s: %s: %s", url, type(exc).__name__, exc)
    return {}


def _build_cloak_locale_options(proxy_url: str | None = None, browser_profile: dict | None = None) -> dict:
    """生成 Cloak/Playwright 双层语言时区配置。"""
    explicit_locale = str(getattr(_cfg, "CLOAK_LOCALE", "") or "").strip()
    explicit_timezone = str(getattr(_cfg, "CLOAK_TIMEZONE", "") or "").strip()
    out = {}
    if isinstance(browser_profile, dict):
        saved_locale = str(browser_profile.get("navigator_language") or "").strip()
        saved_timezone = str(browser_profile.get("timezone_iana") or "").strip()
        saved_accept_language = str(browser_profile.get("accept_language") or "").strip()
        if saved_locale:
            out["locale"] = saved_locale
        if saved_timezone:
            out["timezone"] = saved_timezone
        if saved_accept_language:
            out["accept_language"] = saved_accept_language
        if isinstance(browser_profile.get("geo"), dict):
            out["geo"] = dict(browser_profile.get("geo") or {})
    if explicit_locale:
        out["locale"] = explicit_locale
        # Accept-Language 用 config.browser 自动推断更完整；显式时给一个保守值。
        out["accept_language"] = f"{explicit_locale},{explicit_locale.split('-')[0]};q=0.9,en-US;q=0.8,en;q=0.7"
    if explicit_timezone:
        out["timezone"] = explicit_timezone
    if explicit_locale and explicit_timezone:
        return out
    if not bool(getattr(_cfg, "CLOAK_GEOIP", True)):
        return out
    try:
        from config.browser import build_browser_environment
        geo = _detect_cloak_exit_geo(proxy_url)
        profile = build_browser_environment(geo)
        out.setdefault("locale", str(profile.get("navigator_language") or ""))
        out.setdefault("timezone", str(profile.get("timezone_iana") or ""))
        out.setdefault("accept_language", str(profile.get("accept_language") or ""))
        out["geo"] = geo
    except Exception as exc:
        logger.debug("[Cloak] 构建自动语言/时区失败：%s: %s", type(exc).__name__, exc)
    return {k: v for k, v in out.items() if v}


def build_cloak_driver(
    proxy: str | None = None,
    *,
    fingerprint_seed: str | None = None,
    headless: bool | None = None,
    user_data_dir: str | None = None,
    browser_profile: dict | None = None,
    force_direct: bool = False,
) -> tuple[CloakSeleniumDriver, CloakOpenResult]:
    """启动 CloakBrowser 并返回 Selenium 风格 driver。

    proxy=None  时按 config.proxy.PROXY_POOL 随机抽取；
    proxy=""    时显式禁用代理；
    proxy="..." 时使用指定代理。
    """
    from config import proxy as proxy_cfg
    proxy_required = bool(getattr(proxy_cfg, "proxy_required", lambda: False)())
    if force_direct:
        proxy = None
    elif bool(getattr(_cfg, "CLOAK_USE_PROXY", True)) and (
        proxy is None
        or (proxy_required and not bool(getattr(proxy_cfg, "proxy_allowed", lambda value: bool(value))(proxy)))
    ):
        # 优先健康探测选出口；ThorData 全挂时必须中止，禁止浏览器直连。
        proxy = proxy_cfg.pick_healthy_proxy(probe=True)
    if proxy_required and not force_direct and not str(proxy or "").strip():
        raise RuntimeError("ThorData 没有健康 HTTPS 入口，Cloak 注册已禁止使用本机直连 IP")
    try:
        from cloakbrowser import launch, launch_persistent_context
    except ImportError as exc:
        raise RuntimeError("未安装 cloakbrowser，请执行：pip install cloakbrowser") from exc

    launch_args = list(getattr(_cfg, "CLOAK_EXTRA_ARGS", []) or [])
    seed = str(
        fingerprint_seed
        if fingerprint_seed is not None
        else (getattr(_cfg, "CLOAK_FINGERPRINT_SEED", "") or "")
    ).strip()
    if seed:
        launch_args.append(f"--fingerprint={seed}")

    proxy_url = (
        _normalize_proxy(proxy)
        if bool(getattr(_cfg, "CLOAK_USE_PROXY", True)) and not force_direct
        else None
    )
    https_proxy = str(proxy_url or "").lower().startswith("https://")
    if https_proxy and "--ignore-certificate-errors" not in launch_args:
        launch_args.append("--ignore-certificate-errors")
    locale_opts = _build_cloak_locale_options(proxy_url, browser_profile=browser_profile)
    # geoip=True 交给 CloakBrowser 根据当前出口 IP 自动匹配 timezone/locale/WebRTC。
    # 之前只有显式 proxy_url 时才开启；如果用户走系统代理/VPN/透明代理，代码层面
    # 看不到 proxy_url，会误关 geoip，导致语言/时区不跟随出口。这里改为完全尊重配置。
    opts = {
        "headless": bool(getattr(_cfg, "CLOAK_HEADLESS", False)) if headless is None else bool(headless),
        "humanize": bool(getattr(_cfg, "CLOAK_HUMANIZE", True)),
        # Cloak 0.5.2 的内置 GeoIP 不支持裸 IP HTTPS 代理的 proxy-insecure；
        # ThorData 时由上面的 curl_cffi 探测真实出口并显式设置 locale/timezone。
        "geoip": bool(getattr(_cfg, "CLOAK_GEOIP", True)) and not https_proxy,
    }
    if locale_opts.get("locale"):
        opts["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        opts["timezone"] = locale_opts["timezone"]
    if proxy_url:
        opts["proxy"] = proxy_url
    if launch_args:
        opts["args"] = launch_args
    license_key = str(getattr(_cfg, "CLOAK_LICENSE_KEY", "") or "").strip()
    if license_key:
        opts["license_key"] = license_key

    if user_data_dir is None:
        user_data_dir = str(getattr(_cfg, "CLOAK_USER_DATA_DIR", "") or "").strip()
    else:
        user_data_dir = str(user_data_dir or "").strip()
    logger.info(
        "[Cloak] 启动 CloakBrowser：headless=%s humanize=%s geoip=%s proxy=%s locale=%s timezone=%s accept_language=%s persistent=%s",
        opts.get("headless"), opts.get("humanize"), opts.get("geoip"),
        proxy_cfg._mask_proxy(proxy_url) if proxy_url else "无", opts.get("locale") or "自动/默认", opts.get("timezone") or "自动/默认",
        locale_opts.get("accept_language") or "自动/默认", bool(user_data_dir),
    )
    context_kwargs = {}
    if https_proxy:
        context_kwargs["ignore_https_errors"] = True
    if locale_opts.get("locale"):
        context_kwargs["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        context_kwargs["timezone_id"] = locale_opts["timezone"]
    if locale_opts.get("accept_language"):
        context_kwargs["extra_http_headers"] = {"Accept-Language": locale_opts["accept_language"]}

    if user_data_dir:
        persistent_opts = dict(opts)
        if https_proxy:
            persistent_opts["ignore_https_errors"] = True
        context = launch_persistent_context(user_data_dir, **persistent_opts)
        page = context.new_page()
        browser = getattr(context, "browser", None) or context
        # persistent context 的 locale/timezone 已通过 launch_persistent_context 参数传入。
    else:
        browser = launch(**opts)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

    driver = CloakSeleniumDriver(browser=browser, context=context, page=page)
    # Roxy/Cloak 共用部分页面操作函数；给共享函数一个显式日志前缀，
    # 避免 Cloak 注册流程里出现 `[Roxy注册]`。
    driver._registration_log_prefix = "[Cloak注册]"
    driver.set_page_load_timeout(int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90))
    return driver, CloakOpenResult(
        profile_id=f"cloak-{seed}" if seed else "cloakbrowser",
        raw={
            "driver": "cloakbrowser",
            "proxy": proxy_url,
            "locale": locale_opts,
            "fingerprint_seed": seed,
            "options": {k: v for k, v in opts.items() if k != "license_key"},
        },
    )
