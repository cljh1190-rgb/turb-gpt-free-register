# -*- coding: utf-8 -*-
"""
浏览器指纹与 HTTP 客户端配置。

这里集中维护同一个“浏览器环境画像”，供三层同时使用：
1. curl_cffi TLS / HTTP 头；
2. Python 端生成 Sentinel 初始 p；
3. Node VM 端运行 sdk.js。

原则：同一 BrowserSession 内稳定，不同 BrowserSession 可自然分散；协议头、JS
navigator/screen/timezone/client hints 不能互相打架。
"""
from __future__ import annotations

from config.env_loader import apply_env_overrides

import random
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def _latest_chrome_major(default: str = "146") -> str:
    """兼容旧模块导入；默认与 CloakBrowser/curl_cffi 的 Chrome 146 对齐。"""
    return default


CHROME_MAJOR = "146"
CHROME_FULL_VERSION = "146.0.7680.177"

SAFARI_VERSION = ""
SAFARI_WEBKIT_VERSION = "537.36"
WINDOWS_NT_UA_VERSION = "10.0"

# ---------- curl_cffi 模拟浏览器 ----------
# curl_cffi、CloakBrowser 运行时和 HTTP/JS 画像统一使用 Chrome 146。
IMPERSONATE = "chrome146"

# TLS 指纹轮换池：全部为 curl_cffi 0.15.0 实际支持的 impersonate 目标
# （已在本地 python -c 逐个验证 Session(impersonate=...) 可创建）。
# 每个 bundle 的 UA / sec-ch-ua / sec-ch-ua-full-version-list 与 impersonate
# 的 Chrome major 自洽；UA 采用 Chrome 110+ 冻结格式 Chrome/<major>.0.0.0，
# full version 取各 major 的稳定版真实版本号。
TLS_IMPRESONATE_POOL = [
    {
        "impersonate": "chrome124",
        "chrome_major": "124",
        "chrome_full_version": "124.0.6367.207",
        "sec_ch_ua": '"Chromium";v="124", "Not-A.Brand";v="24", "Google Chrome";v="124"',
        "sec_ch_ua_full_version_list": '"Chromium";v="124.0.6367.207", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="124.0.6367.207"',
    },
    {
        "impersonate": "chrome131",
        "chrome_major": "131",
        "chrome_full_version": "131.0.6778.86",
        "sec_ch_ua": '"Chromium";v="131", "Not-A.Brand";v="24", "Google Chrome";v="131"',
        "sec_ch_ua_full_version_list": '"Chromium";v="131.0.6778.86", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="131.0.6778.86"',
    },
    {
        "impersonate": "chrome136",
        "chrome_major": "136",
        "chrome_full_version": "136.0.7103.94",
        "sec_ch_ua": '"Chromium";v="136", "Not-A.Brand";v="24", "Google Chrome";v="136"',
        "sec_ch_ua_full_version_list": '"Chromium";v="136.0.7103.94", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="136.0.7103.94"',
    },
    {
        "impersonate": "chrome146",
        "chrome_major": "146",
        "chrome_full_version": "146.0.7680.177",
        "sec_ch_ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec_ch_ua_full_version_list": '"Chromium";v="146.0.7680.177", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.177"',
    },
]

# 是否启用 TLS 指纹轮换：每个 BrowserSession 从池里随机抽一个 bundle。
# 默认开启（跨账号自然分散）；关闭则回到固定的 chrome146 旧行为。
TLS_IMPRESONATE_ROTATION = True

# 真实浏览器对纯 GET（fetch/xhr）不会携带 Content-Type；协议层历史上全带，
# 属于明显的自动化标记。默认关闭（即 GET 不带 content-type）；需要复现旧抓包
# 形态时改为 True。
SEND_CONTENT_TYPE_ON_GET = False

# ---------- 桌面 Chrome 画像 ----------
BROWSER_FAMILY = "chrome"
BROWSER_OS = "Windows"
# OS 相关字段必须和 UA / Client Hints / JS navigator 三方一致。
NAVIGATOR_PLATFORM = "Win32"
NAVIGATOR_VENDOR = "Google Inc."
USER_AGENT_DATA_PLATFORM = "Windows"
USER_AGENT = (
    f"Mozilla/5.0 (Windows NT {WINDOWS_NT_UA_VERSION}; Win64; x64) "
    f"AppleWebKit/{SAFARI_WEBKIT_VERSION} (KHTML, like Gecko) "
    f"Chrome/{CHROME_MAJOR}.0.0.0 Safari/{SAFARI_WEBKIT_VERSION}"
)

SEC_CH_UA = '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"'
SEC_CH_UA_FULL_VERSION_LIST = '"Chromium";v="146.0.7680.177", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.177"'
SEC_CH_UA_PLATFORM = '"Windows"'
SEC_CH_UA_PLATFORM_VERSION = '"19.0.0"'
SEC_CH_UA_MOBILE = "?0"
SEC_CH_UA_ARCH = '"x86"'
SEC_CH_UA_BITNESS = '"64"'
SEC_CH_UA_MODEL = '""'
SEND_CLIENT_HINTS = True
SEND_HIGH_ENTROPY_CLIENT_HINTS = False

# ---------- 语言 / 时区 ----------
BROWSER_LOCALE_PROFILE = "jp"
AUTO_BROWSER_LOCALE_FROM_IP = True
IP_GEO_TIMEOUT = 6.0
IP_GEO_ENDPOINTS = [
    "https://ipinfo.io/json",
    "https://ipapi.co/json",
    "https://ipwho.is/",
]

# 代理出口质量诊断：默认不拦截，只在手动开启时拒绝云厂商/DC ASN。
# 用户可能明确使用固定云出口复现实验抓包，因此默认 False。
REJECT_CLOUD_PROXY = False
CLOUD_PROXY_ORG_KEYWORDS = [
    "amazon", "aws", "google cloud", "google llc", "microsoft", "azure",
    "digitalocean", "linode", "akamai", "ovh", "hetzner", "oracle",
    "tencent", "alibaba", "aliyun", "huawei cloud", "vultr", "contabo",
    "data center", "datacenter", "hosting", "host", "server", "cloud",
]
COUNTRY_LOCALE_PROFILE_MAP = {
    "JP": "jp", "CN": "cn", "HK": "hk", "TW": "tw", "US": "us", "CA": "us",
    "SG": "sg", "GB": "gb", "AU": "gb", "DE": "de", "FR": "fr", "NL": "nl",
}

BROWSER_LOCALE_PROFILES = {
    "jp": {"navigator_language": "ja-JP", "navigator_languages": ["ja-JP"], "accept_language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Tokyo", "timezone_offset_minutes": 9 * 60, "timezone_name": "Japan Standard Time"},
    "cn": {"navigator_language": "zh-CN", "navigator_languages": ["zh-CN"], "accept_language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Shanghai", "timezone_offset_minutes": 8 * 60, "timezone_name": "China Standard Time"},
    "us": {"navigator_language": "en-US", "navigator_languages": ["en-US"], "accept_language": "en-US,en;q=0.9", "timezone_iana": "America/Los_Angeles", "timezone_offset_minutes": -7 * 60, "timezone_name": "Pacific Daylight Time"},
    "sg": {"navigator_language": "en-SG", "navigator_languages": ["en-SG"], "accept_language": "en-SG,en-US;q=0.9,en;q=0.8", "timezone_iana": "Asia/Singapore", "timezone_offset_minutes": 8 * 60, "timezone_name": "Singapore Standard Time"},
    "hk": {"navigator_language": "zh-HK", "navigator_languages": ["zh-HK"], "accept_language": "zh-HK,zh-TW;q=0.9,zh;q=0.8,en-US;q=0.7,en;q=0.6", "timezone_iana": "Asia/Hong_Kong", "timezone_offset_minutes": 8 * 60, "timezone_name": "Hong Kong Standard Time"},
    "tw": {"navigator_language": "zh-TW", "navigator_languages": ["zh-TW"], "accept_language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Asia/Taipei", "timezone_offset_minutes": 8 * 60, "timezone_name": "Taipei Standard Time"},
    "gb": {"navigator_language": "en-GB", "navigator_languages": ["en-GB"], "accept_language": "en-GB,en-US;q=0.9,en;q=0.8", "timezone_iana": "Europe/London", "timezone_offset_minutes": 1 * 60, "timezone_name": "British Summer Time"},
    "de": {"navigator_language": "de-DE", "navigator_languages": ["de-DE"], "accept_language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Europe/Berlin", "timezone_offset_minutes": 2 * 60, "timezone_name": "Central European Summer Time"},
    "fr": {"navigator_language": "fr-FR", "navigator_languages": ["fr-FR"], "accept_language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Europe/Paris", "timezone_offset_minutes": 2 * 60, "timezone_name": "Central European Summer Time"},
    "nl": {"navigator_language": "nl-NL", "navigator_languages": ["nl-NL"], "accept_language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7", "timezone_iana": "Europe/Amsterdam", "timezone_offset_minutes": 2 * 60, "timezone_name": "Central European Summer Time"},
}

TIMEZONE_NAME_BY_IANA = {
    "Asia/Tokyo": "Japan Standard Time",
    "Asia/Shanghai": "China Standard Time",
    "Asia/Singapore": "Singapore Standard Time",
    "Asia/Hong_Kong": "Hong Kong Standard Time",
    "Asia/Taipei": "Taipei Standard Time",
    "America/Los_Angeles": "Pacific Daylight Time",
    "America/New_York": "Eastern Daylight Time",
    "America/Chicago": "Central Daylight Time",
    "America/Denver": "Mountain Daylight Time",
    "Europe/London": "British Summer Time",
    "Europe/Berlin": "Central European Summer Time",
    "Europe/Paris": "Central European Summer Time",
    "Europe/Amsterdam": "Central European Summer Time",
}


def _offset_minutes_for_timezone(tz_name: str, default: int) -> int:
    try:
        offset = datetime.now(ZoneInfo(tz_name)).utcoffset()
        if offset is not None:
            return int(offset.total_seconds() // 60)
    except Exception:
        pass
    return int(default)


def _locale_profile_key_from_geo(geo: dict | None) -> str:
    if not geo or not AUTO_BROWSER_LOCALE_FROM_IP:
        return BROWSER_LOCALE_PROFILE
    country = str(geo.get("country") or geo.get("country_code") or "").upper()
    return COUNTRY_LOCALE_PROFILE_MAP.get(country, BROWSER_LOCALE_PROFILE)


def _build_locale_from_geo(geo: dict | None) -> dict:
    key = _locale_profile_key_from_geo(geo)
    locale = dict(BROWSER_LOCALE_PROFILES.get(key, BROWSER_LOCALE_PROFILES[BROWSER_LOCALE_PROFILE]))
    if geo and AUTO_BROWSER_LOCALE_FROM_IP:
        tz = str(geo.get("timezone") or "").strip()
        if tz:
            locale["timezone_iana"] = tz
            locale["timezone_offset_minutes"] = _offset_minutes_for_timezone(tz, int(locale["timezone_offset_minutes"]))
            locale["timezone_name"] = TIMEZONE_NAME_BY_IANA.get(tz, locale.get("timezone_name", ""))
    locale["locale_profile"] = key
    return locale


_LOCALE = BROWSER_LOCALE_PROFILES.get(BROWSER_LOCALE_PROFILE, BROWSER_LOCALE_PROFILES["jp"])
NAVIGATOR_LANGUAGE = _LOCALE["navigator_language"]
NAVIGATOR_LANGUAGES = list(_LOCALE["navigator_languages"])
ACCEPT_LANGUAGE = _LOCALE["accept_language"]
TIMEZONE_IANA = _LOCALE["timezone_iana"]
TIMEZONE_OFFSET_MINUTES = int(_LOCALE["timezone_offset_minutes"])
TIMEZONE_NAME = _LOCALE["timezone_name"]

# ---------- Sentinel / JS VM 环境 ----------
SCREEN_WIDTH = 1920
SCREEN_HEIGHT = 1080
HARDWARE_CONCURRENCY = 8
JS_HEAP_SIZE_LIMIT = 4395630592
DEVICE_MEMORY = 8

# 这些列表必须与 sentinel/sentinel-runner.js 的 createBrowserContext 保持一致。
NAVIGATOR_PROTO_SAMPLES = [
    "createAuctionNonce−function createAuctionNonce() { [native code] }",
    "clearOriginJoinedAdInterestGroups−function clearOriginJoinedAdInterestGroups() { [native code] }",
    "updateAdInterestGroups−function updateAdInterestGroups() { [native code] }",
    "canLoadAdAuctionFencedFrame−function canLoadAdAuctionFencedFrame() { [native code] }",
    "gpu−[object GPU]",
    "getBattery−function getBattery() { [native code] }",
    "getGamepads−function getGamepads() { [native code] }",
    "javaEnabled−function javaEnabled() { [native code] }",
    "sendBeacon−function sendBeacon() { [native code] }",
    "vibrate−function vibrate() { [native code] }",
    "login−[object NavigatorLogin]",
]
DOCUMENT_KEY_SAMPLES = [
    "currentScript", "scripts", "cookie", "URL", "documentURI", "referrer",
    "title", "characterSet", "charset", "compatMode", "contentType", "readyState",
    "visibilityState", "hidden", "hasFocus", "documentElement", "body",
    "addEventListener", "removeEventListener", "querySelector", "querySelectorAll",
    "getElementById", "getElementsByTagName", "createElement",
]
WINDOW_KEY_SAMPLES = [
    "window", "self", "top", "parent", "frames", "navigator", "screen", "location",
    "localStorage", "sessionStorage", "history", "innerWidth", "innerHeight",
    "outerWidth", "outerHeight", "devicePixelRatio", "chrome", "performance", "crypto",
    "TextEncoder", "URL", "URLSearchParams", "AbortController",
    "locationbar", "scrollX", "scrollY", "ondevicemotion",
    "requestAnimationFrame", "queueMicrotask", "onfocus", "onblur", "onpageshow",
]

SCRIPT_SRC_SAMPLES = [
    "https://accounts.google.com/gsi/client",
    "https://chatgpt.com/cdn-cgi/challenge-platform/scripts/jsd/api.js?onload=jsdOnload",
    "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
]

WINDOW_FEATURE_FLAGS = {
    "ai": 0,
    "InstallTrigger": 0,
    "cache": 0,
    "data": 0,
    "solana": 0,
    "dump": 0,
    # HAR 样本 p[24] 为 0；默认不暴露，必要时由画像开关启用。
    "requestIdleCallback": 0,
}

# ---------- HTTP 超时 ----------
REQUEST_TIMEOUT = 30

# HAR 参考画像：Default-all-domains-1784468371563.json 解码 p[0]/p[2]/p[16] 得出。
HAR_CAPTURE_BASE_PROFILE = {"screen_width": 1920, "screen_height": 1080, "hardware_concurrency": 8, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 1}

# 常见 Windows Chrome 桌面画像池。同一 session 内保持不变；不同 session 随机分散。
# HAR_CAPTURE_BASE_PROFILE 只是候选之一，不全局固定。
BROWSER_PROFILE_POOL = [
    HAR_CAPTURE_BASE_PROFILE,
    {"screen_width": 1366, "screen_height": 768,  "hardware_concurrency": 4,  "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 1},
    {"screen_width": 1536, "screen_height": 864,  "hardware_concurrency": 8,  "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 1.25},
    {"screen_width": 1600, "screen_height": 900,  "hardware_concurrency": 8,  "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 1},
    {"screen_width": 1920, "screen_height": 1080, "hardware_concurrency": 12, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 1},
    {"screen_width": 2560, "screen_height": 1440, "hardware_concurrency": 16, "device_memory": 8, "js_heap_size_limit": 4294967296, "device_pixel_ratio": 1},
]


def build_browser_environment(geo: dict | None = None, base_profile: dict | None = None) -> dict:
    """构建完整浏览器环境画像，作为所有指纹字段的单一数据源。"""
    locale = _build_locale_from_geo(geo)
    profile = dict(base_profile or random.choice(BROWSER_PROFILE_POOL))
    profile.update({
        "locale_profile": locale.get("locale_profile", BROWSER_LOCALE_PROFILE),
        "geo": dict(geo or {}),
        "timezone_iana": locale["timezone_iana"],
        "timezone_offset_minutes": int(locale["timezone_offset_minutes"]),
        "timezone_name": locale["timezone_name"],
        "navigator_language": locale["navigator_language"],
        "navigator_languages": list(locale["navigator_languages"]),
        "accept_language": locale["accept_language"],
        "browser_family": BROWSER_FAMILY,
        "browser_os": BROWSER_OS,
        "navigator_platform": NAVIGATOR_PLATFORM,
        "navigator_vendor": NAVIGATOR_VENDOR,
        "user_agent_data_platform": USER_AGENT_DATA_PLATFORM,
        "safari_version": SAFARI_VERSION,
        "safari_webkit_version": SAFARI_WEBKIT_VERSION,
        "chrome_major": CHROME_MAJOR,
        "chrome_full_version": CHROME_FULL_VERSION,
        "user_agent": USER_AGENT,
        "send_client_hints": SEND_CLIENT_HINTS,
        "sec_ch_ua": SEC_CH_UA,
        "sec_ch_ua_platform": SEC_CH_UA_PLATFORM,
        "sec_ch_ua_platform_version": SEC_CH_UA_PLATFORM_VERSION,
        "sec_ch_ua_arch": SEC_CH_UA_ARCH,
        "sec_ch_ua_bitness": SEC_CH_UA_BITNESS,
        "sec_ch_ua_model": SEC_CH_UA_MODEL,
        "sec_ch_ua_full_version_list": SEC_CH_UA_FULL_VERSION_LIST,
        "sec_ch_ua_mobile": SEC_CH_UA_MOBILE,
        "navigator_proto_samples": list(NAVIGATOR_PROTO_SAMPLES),
        "document_key_samples": list(DOCUMENT_KEY_SAMPLES),
        "window_key_samples": list(WINDOW_KEY_SAMPLES),
        "script_src_samples": list(SCRIPT_SRC_SAMPLES),
        "window_feature_flags": dict(WINDOW_FEATURE_FLAGS),
        "build_id": __import__("config.openai_protocol", fromlist=["OPENAI_BUILD_ID"]).OPENAI_BUILD_ID,
    })
    return profile


def _tls_bundle_profile(bundle: dict) -> dict:
    """把 TLS bundle 的版本字段转成浏览器画像覆盖项。"""
    major = str(bundle.get("chrome_major") or CHROME_MAJOR)
    ua = (
        f"Mozilla/5.0 (Windows NT {WINDOWS_NT_UA_VERSION}; Win64; x64) "
        f"AppleWebKit/{SAFARI_WEBKIT_VERSION} (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/{SAFARI_WEBKIT_VERSION}"
    )
    return {
        "impersonate": str(bundle.get("impersonate") or IMPERSONATE),
        "chrome_major": major,
        "chrome_full_version": str(bundle.get("chrome_full_version") or CHROME_FULL_VERSION),
        "user_agent": ua,
        "sec_ch_ua": str(bundle.get("sec_ch_ua") or SEC_CH_UA),
        "sec_ch_ua_full_version_list": str(bundle.get("sec_ch_ua_full_version_list") or SEC_CH_UA_FULL_VERSION_LIST),
    }


def pick_tls_bundle() -> dict | None:
    """
    按 TLS_IMPRESONATE_ROTATION 从池里随机抽一个 TLS bundle。
    返回 None 表示保持旧行为（模块级 chrome146 默认值）。
    """
    if not TLS_IMPRESONATE_ROTATION:
        return None
    if not TLS_IMPRESONATE_POOL:
        return None
    return dict(random.choice(TLS_IMPRESONATE_POOL))


def impersonate_for_profile(profile: dict | None) -> str:
    """
    根据画像里的 chrome_major 推导对应的 curl_cffi impersonate 目标。
    供复用已保存画像的会话（套餐查询/登录态复用等）保持 TLS 与画像版本一致。
    """
    if not isinstance(profile, dict):
        return IMPERSONATE
    major = str(profile.get("chrome_major") or "").strip()
    if major:
        for bundle in TLS_IMPRESONATE_POOL:
            if str(bundle.get("chrome_major") or "") == major:
                return str(bundle.get("impersonate") or IMPERSONATE)
    return IMPERSONATE


def pick_browser_profile(geo: dict | None = None, tls_bundle: dict | None = None) -> dict:
    """为一个 BrowserSession 随机挑选稳定桌面画像；HAR 尺寸只是候选之一。

    tls_bundle 为空时按 TLS_IMPRESONATE_ROTATION 自动抽取；bundle 的版本字段
    会覆盖进画像，保证 UA / sec-ch-ua / client hints / curl_cffi impersonate 自洽。
    """
    if tls_bundle is None:
        tls_bundle = pick_tls_bundle()
    profile = build_browser_environment(geo)
    if tls_bundle:
        profile.update(_tls_bundle_profile(tls_bundle))
    return profile


def validate_browser_profile(profile: dict) -> list[str]:
    """返回画像内部矛盾点，主要用于日志/自测。"""
    issues: list[str] = []
    ua = str(profile.get("user_agent") or "")
    family = str(profile.get("browser_family") or BROWSER_FAMILY)
    if family == "safari":
        if "Version/" not in ua or "Safari/" not in ua or "Chrome/" in ua or "Chromium/" in ua:
            issues.append("Safari UA 不一致")
        if profile.get("send_client_hints"):
            issues.append("Safari 不应发送 Chromium Client Hints")
    else:
        ua_match = re.search(r"Chrome/(\d+)(?:\.\d+){0,3}", ua)
        expected_major = str(profile.get("chrome_major") or "")
        full_major = str(profile.get("chrome_full_version") or "").split(".", 1)[0]
        if not ua_match or (expected_major and ua_match.group(1) != expected_major):
            issues.append("UA 与 chrome_major 不一致")
        if expected_major and full_major and expected_major != full_major:
            issues.append("chrome_major 与 chrome_full_version 不一致")
        sec_ch_ua = str(profile.get("sec_ch_ua") or "")
        sec_major_match = re.search(r'"Google Chrome";v="(\d+)"', sec_ch_ua) if "Google Chrome" in sec_ch_ua else None
        if expected_major and sec_major_match and sec_major_match.group(1) != expected_major:
            issues.append("sec-ch-ua 与 chrome_major 不一致")
    if profile.get("browser_os") == "macOS":
        if "Macintosh; Intel Mac OS X" not in ua:
            issues.append("macOS 画像但 UA 不是 Macintosh")
        if str(profile.get("navigator_platform") or "") != "MacIntel":
            issues.append("macOS 画像但 navigator.platform 不是 MacIntel")
        if "macOS" not in str(profile.get("sec_ch_ua_platform") or ""):
            issues.append("macOS 画像但 sec-ch-ua-platform 不是 macOS")
    elif profile.get("browser_os") == "Windows":
        if "Windows NT" not in ua:
            issues.append("Windows 画像但 UA 不是 Windows")
        if str(profile.get("navigator_platform") or "") != "Win32":
            issues.append("Windows 画像但 navigator.platform 不是 Win32")
        if "Windows" not in str(profile.get("sec_ch_ua_platform") or ""):
            issues.append("Windows 画像但 sec-ch-ua-platform 不是 Windows")
    if not profile.get("navigator_language"):
        issues.append("navigator_language 为空")
    languages = profile.get("navigator_languages") or []
    if profile.get("navigator_language") and profile.get("navigator_language") not in languages:
        issues.append("navigator.language 不在 navigator.languages 中")
    # requestIdleCallback 是否暴露由画像决定。
    return issues

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'BROWSER_LOCALE_PROFILE': 'str', 'AUTO_BROWSER_LOCALE_FROM_IP': 'bool', 'IP_GEO_TIMEOUT': 'float', 'REJECT_CLOUD_PROXY': 'bool', 'TLS_IMPRESONATE_ROTATION': 'bool', 'SEND_CONTENT_TYPE_ON_GET': 'bool'})
