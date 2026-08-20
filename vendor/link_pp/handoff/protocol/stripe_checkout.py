"""ChatGPT Checkout 纯 HTTP 协议。

主流程创建 ``oaics_`` 自定义 Checkout，经 ChatGPT taxes、Stripe
ConfirmationToken 与 Checkout confirm 获取 PayPal 跳转。真正的 ``cpmt_``
支付方式仍使用 custom_payment_method/start。文件后半保留旧版
``cs_live_`` Stripe Hosted 辅助函数，在显式选择 Stripe 提链时由生产网关调用。

移植自 Gpt-Agreement-Payment/CTF-pay/card/_monolith.py 的 Stripe 部分：
  fetch_publishable_key / init_checkout / fetch_elements_session /
  create_paypal_payment_method / confirm_payment / redirect extraction

全程用 curl_cffi（chrome TLS 指纹）走指定代理。ChatGPT Checkout 与 Stripe
接口均通过纯 HTTP 请求完成。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import string
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..security import sanitize_diagnostic_payload, sanitize_message

STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION_BASE = "2025-03-31.basil"
STRIPE_VERSION_FULL = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
PAYPAL_STRIPE_VERSION = (
    "2020-08-27;custom_checkout_beta=v1; "
    "checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
)
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"
CHECKOUT_SESSION_ATTEMPTS_DEFAULT = 3
CHECKOUT_SESSION_ATTEMPTS_MIN = 1
ZERO_AMOUNT_VERIFY_ATTEMPTS = 4
ZERO_AMOUNT_VERIFY_DELAY_SECONDS = 0.8
# Preflight is intentionally aggressive: a slow route is less useful than the
# next proxy in the pool. Real Checkout and confirm requests retain their
# longer protocol-specific timeouts.
PREFLIGHT_RETRY_ATTEMPTS = 1
PREFLIGHT_RETRY_DELAY_SECONDS = 0
PREFLIGHT_HTTP_TIMEOUT_SECONDS = max(
    1,
    min(15, int(os.environ.get("HANDOFF_PREFLIGHT_TIMEOUT", "5"))),
)
PREFLIGHT_CONCURRENCY = max(
    1,
    min(20, int(os.environ.get("HANDOFF_PREFLIGHT_CONCURRENCY", "20"))),
)
_PREFLIGHT_SEMAPHORE = threading.BoundedSemaphore(PREFLIGHT_CONCURRENCY)

OPENAI_IE_COUNTRIES = {
    "AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GB", "GR", "HR", "HU", "IE", "IS", "IT", "LI", "LT", "LU",
    "LV", "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
}

BROWSER_IMPERSONATE = "firefox147"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) "
    "Gecko/20100101 Firefox/147.0"
)
CHATGPT_CLIENT_VERSION = "prod-db390ebea64862bf1899c420a4c736e0cf639747"
CHATGPT_CLIENT_BUILD_NUMBER = "7904904"
IP_CHECK_SOURCES = (
    ("ipinfo", "https://ipinfo.io/json"),
    ("ipapi", "https://ipapi.co/json/"),
    ("ipwho", "https://ipwho.is/"),
    ("myip", "https://api.myip.com/"),
)

OPENAI_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
OPENAI_CHECKOUT_TAXES_URL = f"{OPENAI_CHECKOUT_URL}/taxes"
OPENAI_CHECKOUT_CONFIRM_URL = f"{OPENAI_CHECKOUT_URL}/confirm"
OPENAI_CUSTOM_PAYMENT_START_URL = (
    f"{OPENAI_CHECKOUT_URL}/custom_payment_method/start"
)

_OAI_SESSION_IDS: dict[str, str] = {}
_OAI_SESSION_IDS_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class SentinelCtx:
    device_id: str
    user_agent: str
    proxy: str = ""
    country: str = "US"
    page_url: str = "https://chatgpt.com/"
    cookie_header: str = ""


def _mint_sentinel(
    flow: str,
    context: Optional[SentinelCtx],
    log: Callable[[str], None],
) -> tuple[str, str]:
    if context is None:
        return "", ""
    try:
        from .sentinel import mint_sentinel_sync

        main, so = mint_sentinel_sync(
            flow=flow,
            device_id=context.device_id,
            user_agent=context.user_agent,
            proxy=context.proxy,
            page_url=context.page_url,
            language=_profile(context.country)["browser_language"],
            timezone=_profile(context.country)["browser_timezone"],
            cookie_header=context.cookie_header,
        )
        log(f"[sentinel] {flow} token ok (main={len(main)}, so={len(so)})")
        return main, so
    except Exception as exc:
        log(f"[sentinel] {flow} 生成失败（继续）: {str(exc)[:160]}")
        return "", ""

# OpenAI / OpenAI LLC 的 Stripe publishable key（账户分片 → pk）。
KNOWN_PUBLISHABLE_KEYS = {
    "1Pj377KslHRdbaPg": "pk_live_51Pj377KslHRdbaPgTJYjThzH3f5dt1N1vK7LUp0qh0yNSarhfZ6nfbG7FFlh8KLxVkvdMWN5o6Mc4Vda6NHaSnaV00C2Sbl8Zs",
    "1HOrSwC6h1nxGoI3": "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
}

LOCALE_PROFILES = {
    "US": {"browser_locale": "en-US", "browser_timezone": "America/Chicago", "browser_language": "en-US"},
    "BR": {"browser_locale": "pt-BR", "browser_timezone": "America/Sao_Paulo", "browser_language": "pt-BR"},
    "JP": {"browser_locale": "ja-JP", "browser_timezone": "Asia/Tokyo", "browser_language": "ja-JP"},
    "GB": {"browser_locale": "en-GB", "browser_timezone": "Europe/London", "browser_language": "en-GB"},
    "DE": {"browser_locale": "de-DE", "browser_timezone": "Europe/Berlin", "browser_language": "de-DE"},
    "FR": {"browser_locale": "fr-FR", "browser_timezone": "Europe/Paris", "browser_language": "fr-FR"},
}

class PayPalFundingUnavailableError(RuntimeError):
    """The concrete Checkout Session does not accept PayPal."""

    def __init__(self, session_id: str, payment_method_types: list[str], detail: str = ""):
        self.session_id = session_id
        self.payment_method_types = list(payment_method_types)
        suffix = f"; {detail}" if detail else ""
        super().__init__(
            f"Checkout 不支持 PayPal "
            f"(session={session_id}, pm={self.payment_method_types}){suffix}"
        )


class PayPalRiskDeclinedError(RuntimeError):
    """Stripe Radar rejected the current PayPal payment method."""

    def __init__(
        self,
        *,
        decline_code: str,
        error_code: str = "",
        payment_method_id: str = "",
    ):
        self.decline_code = str(decline_code or "generic_decline")
        self.error_code = str(error_code or "")
        self.payment_method_id = str(payment_method_id or "")
        detail = f"; error_code={self.error_code}" if self.error_code else ""
        super().__init__(
            f"PayPal 风控拒绝: decline_code={self.decline_code}{detail}"
        )


class PromoNotAppliedError(RuntimeError):
    """The checkout was requested with a free promo but still has an amount due."""

    def __init__(self, session_id: str, amount: Any, currency: str, detail: str = ""):
        self.session_id = session_id
        self.amount = amount
        self.currency = str(currency or "").upper()
        suffix = f"（{detail}）" if detail else ""
        super().__init__(
            "免费促销未实际生效 "
            f"(session={session_id}, Checkout due={amount} {self.currency or '?'})；"
            f"已停止后续 PayPal 创建和审批{suffix}"
        )


class OaicsCheckoutRequiredError(RuntimeError):
    """The custom OAICS flow received a hosted Stripe Checkout instead."""

    def __init__(self, session_id: str):
        self.session_id = str(session_id or "")
        super().__init__(
            "上游返回普通 Stripe Checkout "
            f"(session={self.session_id or 'unknown'})，未生成当前流程需要的 oaics_ 链"
        )


class StripeCheckoutRequiredError(RuntimeError):
    """The hosted Stripe flow received an OAICS custom Checkout instead."""

    def __init__(self, session_id: str):
        self.session_id = str(session_id or "")
        super().__init__(
            "上游返回 OAICS Checkout "
            f"(session={self.session_id or 'unknown'})，未生成当前流程需要的 cs_live_ 链"
        )


class OaicsConfirmBlockedError(RuntimeError):
    """OAICS custom-payment confirmation stayed blocked after fresh proofs."""


class CheckoutPreflightError(RuntimeError):
    """A route failed before a real Checkout submission was made."""

    def __init__(self, message: str, *, code: str = "PREFLIGHT_FAILED"):
        self.code = str(code or "PREFLIGHT_FAILED")
        super().__init__(message)


class ChatGPTAuthError(RuntimeError):
    """The supplied access token cannot create a ChatGPT Checkout."""


def oai_session_id_for_device(device_id: str) -> str:
    """Return a stable session UUID that is distinct from the device UUID."""
    key = str(device_id or "").strip()
    if not key:
        return ""
    with _OAI_SESSION_IDS_LOCK:
        value = _OAI_SESSION_IDS.get(key)
        if value:
            return value
        if len(_OAI_SESSION_IDS) >= 4096:
            _OAI_SESSION_IDS.pop(next(iter(_OAI_SESSION_IDS)))
        value = str(uuid.uuid4())
        _OAI_SESSION_IDS[key] = value
        return value


def chatgpt_context_headers(
    *,
    country: str,
    device_id: str,
    referer: str,
) -> dict[str, str]:
    profile = _profile(country)
    browser_language = profile["browser_language"]
    language = browser_language.split("-", 1)[0]
    headers = {
        "Accept-Language": f"{browser_language},{language};q=0.9,en;q=0.8",
        "Referer": referer,
        "User-Agent": BROWSER_UA,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "OAI-Language": browser_language,
        "oai-client-version": CHATGPT_CLIENT_VERSION,
        "oai-client-build-number": CHATGPT_CLIENT_BUILD_NUMBER,
    }
    if device_id:
        headers["oai-device-id"] = device_id
        headers["oai-session-id"] = oai_session_id_for_device(device_id)
    return headers


def _extract_ip_country(source: str, payload: dict[str, Any]) -> tuple[str, str]:
    if source == "ipinfo":
        return str(payload.get("ip") or ""), str(payload.get("country") or "").upper()
    if source == "ipapi":
        return (
            str(payload.get("ip") or ""),
            str(payload.get("country_code") or payload.get("country") or "").upper(),
        )
    if source == "ipwho":
        if payload.get("success") is False:
            return str(payload.get("ip") or ""), ""
        return str(payload.get("ip") or ""), str(payload.get("country_code") or "").upper()
    if source == "myip":
        return str(payload.get("ip") or ""), str(payload.get("cc") or payload.get("country") or "").upper()
    return "", ""


def verify_proxy_exit_country(http, expected_country: str, *, timeout: int = 12) -> dict[str, str]:
    expected = str(expected_country or "").strip().upper()
    failures: list[str] = []
    deadline = time.monotonic() + max(0.1, float(timeout))
    for source, url in IP_CHECK_SOURCES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = http.get(
                url,
                headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
                timeout=remaining,
            )
            status = int(getattr(response, "status_code", 599) or 599)
            if status >= 400:
                failures.append(f"{source} HTTP {status}")
                continue
            payload = response.json() or {}
            if not isinstance(payload, dict):
                failures.append(f"{source} invalid response")
                continue
            ip, country = _extract_ip_country(source, payload)
            if not country:
                failures.append(f"{source} no country")
                continue
            if country != expected:
                raise CheckoutPreflightError(
                    f"代理出口国家 {country}，要求 {expected}",
                    code="PROXY_COUNTRY_MISMATCH",
                )
            return {"ip": ip, "country": country, "source": source}
        except CheckoutPreflightError:
            raise
        except Exception as exc:
            failures.append(f"{source} {type(exc).__name__}: {str(exc)[:80]}")
    raise CheckoutPreflightError(
        "代理出口查询失败：" + "；".join(failures[:4]),
        code="PROXY_UNAVAILABLE",
    )


def proxy_exit_log_label(info: dict[str, str]) -> str:
    digest = hashlib.sha256(str(info.get("ip") or "").encode()).hexdigest()[:10]
    return (
        f"country={info.get('country') or 'UNKNOWN'}, "
        f"ip=exit#{digest}, source={info.get('source') or 'unknown'}"
    )


def _is_cloudflare_challenge(response: Any) -> bool:
    raw_headers = getattr(response, "headers", {}) or {}
    try:
        headers = {str(key).lower(): str(value) for key, value in raw_headers.items()}
    except Exception:
        headers = {}
    text = (getattr(response, "text", "") or "")[:4000].lower()
    return (
        headers.get("cf-mitigated") == "challenge"
        or "enable javascript and cookies to continue" in text
        or "cf-chl-" in text
        or "just a moment" in text
    )


def verify_chatgpt_account(
    http,
    access_token: str,
    *,
    country: str,
    device_id: str,
    log: Callable[[str], None] = lambda _message: None,
) -> None:
    _set_oai_device_cookie(http, device_id)
    _warmup_chatgpt_page(
        http,
        page_url="https://chatgpt.com/",
        country=country,
        device_id=device_id,
        log=log,
        timeout=PREFLIGHT_HTTP_TIMEOUT_SECONDS,
        fail_on_error=True,
    )
    path = "/backend-api/me"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "x-openai-target-path": path,
        "x-openai-target-route": path,
        **chatgpt_context_headers(
            country=country,
            device_id=device_id,
            referer="https://chatgpt.com/",
        ),
    }
    try:
        response = http.get(
            "https://chatgpt.com/backend-api/me",
            headers=headers,
            timeout=PREFLIGHT_HTTP_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _protocol_diagnostic(
            log,
            kind="chatgpt_me",
            method="GET",
            route=path,
            request_payload={
                "country": str(country or "").upper(),
                "device_id_present": bool(device_id),
            },
            error=f"{type(exc).__name__}: {exc}",
        )
        raise CheckoutPreflightError(
            f"ChatGPT /me 连接失败: {type(exc).__name__}: {exc}",
            code="CHATGPT_CONNECTION_FAILED",
        ) from exc
    status = int(getattr(response, "status_code", 0) or 0)
    text = getattr(response, "text", "") or ""
    challenge = _is_cloudflare_challenge(response)
    _protocol_diagnostic(
        log,
        kind="chatgpt_me",
        method="GET",
        route=path,
        status=status,
        request_payload={
            "country": str(country or "").upper(),
            "device_id_present": bool(device_id),
        },
        response_payload={
            "challenge": challenge,
            "content_type": str(
                (getattr(response, "headers", {}) or {}).get("content-type") or ""
            )[:160],
            "body_prefix": text[:240] if challenge else "",
        },
        response=response,
    )
    if status == 401:
        raise ChatGPTAuthError("Access Token 鉴权失败: ChatGPT /me HTTP 401")
    if challenge:
        raise CheckoutPreflightError(
            "ChatGPT /me HTTP 403: Cloudflare Challenge",
            code="CLOUDFLARE_CHALLENGE",
        )
    if status >= 400:
        raise CheckoutPreflightError(
            f"ChatGPT /me 预检失败 HTTP {status}: {text[:200]}",
            code="CHATGPT_PREFLIGHT_FAILED",
        )


def verify_stripe_route(
    http,
    *,
    log: Callable[[str], None] = lambda _message: None,
) -> int:
    route = "/"
    try:
        response = http.get(
            f"{STRIPE_API}{route}",
            headers=_stripe_headers(),
            timeout=PREFLIGHT_HTTP_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _protocol_diagnostic(
            log,
            kind="stripe_preflight",
            method="GET",
            route=route,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise CheckoutPreflightError(
            f"Stripe 连接失败: {type(exc).__name__}: {exc}",
            code="STRIPE_CONNECTION_FAILED",
        ) from exc
    status = int(getattr(response, "status_code", 0) or 0)
    _protocol_diagnostic(
        log,
        kind="stripe_preflight",
        method="GET",
        route=route,
        status=status,
        response_payload={"reachable": status > 0},
        response=response,
    )
    if status <= 0:
        raise CheckoutPreflightError(
            "Stripe 连接失败: 未收到 HTTP 响应",
            code="STRIPE_CONNECTION_FAILED",
        )
    return status


def _profile(country: str) -> dict:
    return LOCALE_PROFILES.get((country or "US").upper(), LOCALE_PROFILES["US"])


def checkout_session_attempts(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        value = CHECKOUT_SESSION_ATTEMPTS_DEFAULT
    return max(CHECKOUT_SESSION_ATTEMPTS_MIN, value)


def _is_retryable_checkout_create_error(error: Optional[str]) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    if re.search(r"\bhttp\s+(?:408|425|429|5\d\d)\b", text):
        return True
    if not text.startswith("request_error:"):
        return False
    if any(
        marker in text
        for marker in ("certificate_verify_failed", "certificate verify failed", "curl: (60)")
    ):
        return False
    return any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "temporarily unavailable",
            "connection",
            "remote disconnected",
            "tls",
            "ssl",
            "curl: (5)",
            "curl: (6)",
            "curl: (7)",
            "curl: (16)",
            "curl: (18)",
            "curl: (28)",
            "curl: (35)",
            "curl: (52)",
            "curl: (55)",
            "curl: (56)",
            "curl: (81)",
            "curl: (92)",
            "curl: (96)",
        )
    )


def _is_checkout_transport_error(error: Optional[str]) -> bool:
    return str(error or "").strip().lower().startswith("request_error:")


def _locale_short(profile: dict) -> str:
    return profile["browser_locale"].split("-")[0]


def _stripe_headers() -> dict:
    return {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }


def _gen_fingerprint() -> tuple[str, str, str]:
    def _id() -> str:
        return str(uuid.uuid4()).replace("-", "") + uuid.uuid4().hex[:6]

    return _id(), _id(), _id()


def _gen_elements_session_id() -> str:
    chars = string.ascii_letters + string.digits
    return "elements_session_" + "".join(random.choices(chars, k=11))


def _elements_options_client_payload() -> dict:
    return {
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
    }


def _stripe_unknown_param(resp) -> str:
    try:
        payload = resp.json()
    except Exception:
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return ""
    if str(err.get("code") or "").lower() != "parameter_unknown":
        return ""
    return str(err.get("param") or "").strip()


def _is_payment_method_types_mismatch(resp) -> bool:
    try:
        payload = resp.json()
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    extra = error.get("extra_fields") if isinstance(error, dict) else None
    reason = extra.get("confirm_error_reason") if isinstance(extra, dict) else ""
    if str(reason or "").lower() == "payment_method_types_mismatch":
        return True
    text = (getattr(resp, "text", "") or "").lower()
    return "payment_method_types_mismatch" in text


def _extract_payment_method_types(payload: dict) -> list[str]:
    pmt = payload.get("payment_method_types")
    if isinstance(pmt, list) and pmt:
        return [pm for pm in pmt if isinstance(pm, str)]
    specs = payload.get("payment_method_specs")
    if isinstance(specs, list):
        out = [s["type"] for s in specs if isinstance(s, dict) and s.get("type")]
        if out:
            return out
    return ["card"]


def _extract_processor_entity(payload: dict) -> str:
    """Extract OpenAI's payment entity from checkout/confirm response fields."""
    if not isinstance(payload, dict):
        return ""
    direct = str(payload.get("processor_entity") or "").strip()
    if direct:
        return direct
    candidates = [
        payload.get("checkout_url"),
        payload.get("url"),
        payload.get("openai_checkout_url"),
        payload.get("success_url"),
        payload.get("cancel_url"),
        payload.get("return_url"),
    ]
    for value in candidates:
        text = str(value or "")
        match = re.search(
            r"/checkout/([^/?]+)/(?:oaics|cs_(?:live|test))_[A-Za-z0-9_-]+",
            text,
        )
        if match:
            return match.group(1)
        match = re.search(r"processor_entity=([A-Za-z0-9_]+)", text)
        if match:
            return match.group(1)
    for key in ("checkout_session", "checkout", "session", "data", "result"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = _extract_processor_entity(nested)
            if found:
                return found
    return ""


def _default_processor_entity(country: str) -> str:
    normalized = (country or "US").upper()
    return "openai_ie" if normalized in OPENAI_IE_COUNTRIES else "openai_llc"


def _set_oai_device_cookie(http, device_id: str) -> None:
    if not device_id:
        return
    cookies = getattr(http, "cookies", None)
    if cookies is None or not hasattr(cookies, "set"):
        return
    try:
        cookies.set("oai-did", device_id, domain="chatgpt.com", path="/")
    except Exception:
        pass


def _session_cookie_header(http) -> str:
    """Return the ChatGPT/OpenAI cookies visible to the active HTTP session."""
    jar = getattr(http, "cookies", None)
    if jar is None:
        return ""
    pairs: list[tuple[str, str]] = []
    try:
        for cookie in jar:
            name = str(getattr(cookie, "name", "") or "").strip()
            value = str(getattr(cookie, "value", "") or "").strip()
            domain = str(getattr(cookie, "domain", "") or "").lower()
            if name and value and (
                not domain or "chatgpt.com" in domain or "openai.com" in domain
            ):
                pairs.append((name, value))
    except Exception:
        try:
            values = jar.get_dict()
        except Exception:
            values = {}
        if isinstance(values, dict):
            pairs.extend(
                (str(name).strip(), str(value).strip())
                for name, value in values.items()
                if str(name).strip() and str(value).strip()
            )
    unique: dict[str, str] = {}
    for name, value in pairs:
        unique[name] = value
    return "; ".join(f"{name}={value}" for name, value in unique.items())


def _warmup_chatgpt_page(
    http,
    *,
    page_url: str,
    country: str,
    device_id: str,
    log: Callable[[str], None],
    timeout: float = 30,
    fail_on_error: bool = False,
) -> None:
    """Warm the real page in the same session so cookies and context stay aligned."""
    # Checkout creation and account preflight share a session.  Avoid a
    # duplicate request for the same page, while still warming a later
    # checkout-specific page in the approval flow.
    try:
        if getattr(http, "_oaics_warmed_page_url", "") == page_url:
            return
    except Exception:
        pass
    try:
        response = http.get(
            page_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "upgrade-insecure-requests": "1",
                **chatgpt_context_headers(
                    country=country,
                    device_id=device_id,
                    referer="https://chatgpt.com/",
                ),
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-origin",
            },
            timeout=timeout,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        _protocol_diagnostic(
            log,
            kind="chatgpt_page_warmup",
            method="GET",
            route="/",
            status=status,
            request_payload={
                "country": str(country or "").upper(),
                "device_id_present": bool(device_id),
            },
            response_payload={
                "challenge": _is_cloudflare_challenge(response),
                "content_type": str(
                    (getattr(response, "headers", {}) or {}).get("content-type") or ""
                )[:160],
            },
            response=response,
        )
        challenge = _is_cloudflare_challenge(response)
        if fail_on_error and challenge:
            raise CheckoutPreflightError(
                "ChatGPT 页面预热 HTTP 403: Cloudflare Challenge",
                code="CLOUDFLARE_CHALLENGE",
            )
        if fail_on_error and status >= 400:
            raise CheckoutPreflightError(
                f"ChatGPT 页面预热失败 HTTP {status}",
                code="CHATGPT_PREFLIGHT_FAILED",
            )
        if status >= 400:
            log(f"[context] 页面预热返回 HTTP {status}（继续）")
        else:
            try:
                setattr(http, "_oaics_warmed_page_url", page_url)
            except Exception:
                pass
            log("[context] 页面与 Cookie 上下文已对齐")
    except CheckoutPreflightError:
        raise
    except Exception as exc:
        _protocol_diagnostic(
            log,
            kind="chatgpt_page_warmup",
            method="GET",
            route="/",
            request_payload={
                "country": str(country or "").upper(),
                "device_id_present": bool(device_id),
            },
            error=f"{type(exc).__name__}: {exc}",
        )
        if fail_on_error:
            raise CheckoutPreflightError(
                f"ChatGPT 页面预热连接失败: {type(exc).__name__}: {exc}",
                code="CHATGPT_CONNECTION_FAILED",
            ) from exc
        log(f"[context] 页面预热失败（继续）: {type(exc).__name__}: {exc}")


def build_http(proxy: Optional[str]):
    """curl_cffi Session（Firefox 147 TLS 指纹），使用指定代理。"""
    from curl_cffi.requests import Session as CffiSession

    http = CffiSession(impersonate=BROWSER_IMPERSONATE)
    try:
        http.trust_env = False
    except Exception:
        pass
    if proxy:
        try:
            http.proxies = {"http": proxy, "https": proxy}
        except Exception:
            pass
    return http


def renew_http_session(http, proxy: Optional[str]):
    """Replace a broken connection pool while preserving the browser cookies."""
    replacement = build_http(proxy)
    try:
        old_cookies = getattr(http, "cookies", None)
        new_cookies = getattr(replacement, "cookies", None)
        if old_cookies is not None and new_cookies is not None:
            new_cookies.update(old_cookies)
    except Exception:
        try:
            replacement.close()
        except Exception:
            pass
        raise
    try:
        http.close()
    except Exception:
        pass
    return replacement


def _diagnostic_headers(headers: Any) -> dict[str, str]:
    allowed = {
        "cf-cache-status",
        "cf-mitigated",
        "cf-ray",
        "content-type",
        "openai-processing-ms",
        "request-id",
        "retry-after",
        "server",
        "x-envoy-upstream-service-time",
        "x-openai-request-id",
        "x-request-id",
    }
    try:
        items = headers.items()
    except Exception:
        return {}
    return {
        str(key).lower(): str(value)[:500]
        for key, value in items
        if str(key).lower() in allowed
    }


def _diagnostic_response_payload(response: Any) -> Any:
    text = str(getattr(response, "text", "") or "")
    try:
        return response.json()
    except Exception:
        try:
            return json.loads(text or "{}")
        except ValueError:
            return {"raw_text": text[:16000], "raw_text_truncated": len(text) > 16000}


def _protocol_diagnostic(
    log: Callable[[str], None],
    *,
    kind: str,
    method: str,
    route: str,
    status: Any = 0,
    request_payload: Any = None,
    response_payload: Any = None,
    response: Any = None,
    error: Any = "",
) -> None:
    """Emit one backend-only record; Job strips it from UI/SSE output."""
    record: dict[str, Any] = {
        "kind": str(kind or "protocol")[:80],
        "method": str(method or "").upper(),
        "route": str(route or ""),
        "http_status": int(status or 0),
    }
    if request_payload is not None:
        record["request"] = sanitize_diagnostic_payload(request_payload)
    if response_payload is not None:
        record["response"] = sanitize_diagnostic_payload(response_payload)
    response_headers = _diagnostic_headers(getattr(response, "headers", {}) or {})
    if response_headers:
        record["response_headers"] = response_headers
    if error:
        record["error"] = sanitize_diagnostic_payload(
            str(error)[:4000],
            field_name="error",
        )
    log(
        "[protocol-diagnostic] "
        + json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    )


# ---------------------------------------------------------------------------
# ChatGPT 下单（纯 HTTP）
# ---------------------------------------------------------------------------

def create_chatgpt_order(
    http,
    access_token: str,
    *,
    country: str = "US",
    currency: str = "USD",
    device_id: str = "",
    sentinel_proxy: str = "",
    checkout_context: Optional[dict[str, str]] = None,
    with_promo: bool = True,
    hosted_checkout: bool = False,
    log: Callable[[str], None] = lambda m: None,
) -> tuple[Optional[str], Optional[str]]:
    """POST Checkout once and return the requested OAICS or hosted session identifier."""
    if checkout_context is not None:
        checkout_context.clear()
    _set_oai_device_cookie(http, device_id)

    # Establish CF/OAI cookies before minting Sentinel so the Node request and
    # the following Checkout POST describe the same HTTP session.
    _warmup_chatgpt_page(
        http,
        page_url="https://chatgpt.com/",
        country=country,
        device_id=device_id,
        log=log,
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://chatgpt.com",
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
        **chatgpt_context_headers(
            country=country,
            device_id=device_id,
            referer="https://chatgpt.com/",
        ),
    }
    if device_id and sentinel_proxy:
        sentinel, sentinel_so = _mint_sentinel(
            "chatgpt_checkout",
            SentinelCtx(
                device_id=device_id,
                user_agent=BROWSER_UA,
                proxy=sentinel_proxy,
                country=country,
                page_url="https://chatgpt.com/",
                cookie_header=_session_cookie_header(http),
            ),
            log,
        )
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        if sentinel_so:
            headers["openai-sentinel-so-token"] = sentinel_so
    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": (country or "US").upper(),
            "currency": (currency or "USD").upper(),
        },
    }
    if not hosted_checkout:
        body["checkout_ui_mode"] = "custom"
    body["check_card_proxy"] = True
    if with_promo:
        body["promo_campaign"] = {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }

    try:
        resp = http.post(OPENAI_CHECKOUT_URL, json=body, headers=headers, timeout=30)
    except Exception as e:
        _protocol_diagnostic(
            log,
            kind="checkout_create",
            method="POST",
            route="/backend-api/payments/checkout",
            request_payload=body,
            error=f"{type(e).__name__}: {e}",
        )
        return None, f"request_error: {type(e).__name__}: {e}"

    status = getattr(resp, "status_code", 0)
    text = getattr(resp, "text", "") or ""
    diagnostic_payload = _diagnostic_response_payload(resp)
    _protocol_diagnostic(
        log,
        kind="checkout_create",
        method="POST",
        route="/backend-api/payments/checkout",
        status=status,
        request_payload=body,
        response_payload=diagnostic_payload,
        response=resp,
    )
    if status != 200:
        return None, f"HTTP {status}: {text[:300]}"
    try:
        data = diagnostic_payload
    except ValueError:
        return None, f"invalid_json: {text[:200]}"
    if not isinstance(data, dict):
        return None, f"invalid_json_object: {text[:200]}"
    raw_sid = str(data.get("checkout_session_id") or "")
    searchable = "\n".join(
        (
            raw_sid,
            str(data.get("checkout_url") or ""),
            str(data.get("url") or ""),
            str(data.get("openai_checkout_url") or ""),
            text,
        )
    )
    patterns = (
        (r"cs_(?:live|test)_[A-Za-z0-9_-]+", r"oaics_[A-Za-z0-9_-]+")
        if hosted_checkout
        else (r"oaics_[A-Za-z0-9_-]+", r"cs_(?:live|test)_[A-Za-z0-9_-]+")
    )
    # A direct checkout_session_id is authoritative. In particular, an OAICS
    # payload can contain an internal cs_live identifier that is not a Hosted
    # Checkout page and must not be selected as the requested Stripe flow.
    direct_match = re.fullmatch(
        r"(?:oaics|cs_(?:live|test))_[A-Za-z0-9_-]+",
        raw_sid,
    )
    match = direct_match or re.search(patterns[0], searchable) or re.search(patterns[1], searchable)
    sid = match.group(0) if match else None
    if sid:
        if checkout_context is not None:
            checkout_context["processor_entity"] = (
                _extract_processor_entity(data) or _default_processor_entity(country)
            )
            checkout_context["checkout_url"] = str(
                data.get("checkout_url")
                or data.get("url")
                or data.get("openai_checkout_url")
                or ""
            )
            raw_pk = (
                data.get("stripe_publishable_key")
                or data.get("publishable_key")
                or data.get("publishableKey")
                or data.get("stripePublishableKey")
                or data.get("key")
                or ""
            )
            pk_match = re.search(r"pk_live_[A-Za-z0-9]+", str(raw_pk))
            if pk_match:
                checkout_context["publishable_key"] = pk_match.group(0)
            checkout_context["checkout_kind"] = (
                "oaics" if sid.startswith("oaics_") else "hosted"
            )
        return sid, None
    return None, f"no_session_id: {text[:200]}"


def create_chatgpt_order_with_retry(
    http,
    access_token: str,
    *,
    country: str = "US",
    currency: str = "USD",
    device_id: str = "",
    sentinel_proxy: str = "",
    checkout_context: Optional[dict[str, str]] = None,
    with_promo: bool = True,
    hosted_checkout: bool = False,
    max_attempts: Any = CHECKOUT_SESSION_ATTEMPTS_DEFAULT,
    is_cancelled: Optional[Callable[[], bool]] = None,
    renew_http: Optional[Callable[[Any], Any]] = None,
    log: Callable[[str], None] = lambda m: None,
) -> tuple[Optional[str], Optional[str]]:
    """Create one Checkout Session, retrying only transient creation failures."""
    attempts = checkout_session_attempts(max_attempts)
    last_error: Optional[str] = None

    for attempt in range(1, attempts + 1):
        if is_cancelled and is_cancelled():
            raise RuntimeError("已取消")
        session_id, error = create_chatgpt_order(
            http,
            access_token,
            country=country,
            currency=currency,
            device_id=device_id,
            sentinel_proxy=sentinel_proxy,
            checkout_context=checkout_context,
            with_promo=with_promo,
            hosted_checkout=hosted_checkout,
            log=log,
        )
        if session_id:
            return session_id, None
        last_error = error
        if attempt >= attempts or not _is_retryable_checkout_create_error(error):
            break

        if _is_checkout_transport_error(error):
            log(
                "[order] Checkout 传输错误（已脱敏）: "
                + sanitize_message(
                    error,
                    access_token=access_token,
                    max_length=320,
                )
            )
            if renew_http is not None:
                try:
                    http = renew_http(http)
                    log("[order] 已保留 Cookie 并重建同代理 HTTP 会话")
                except Exception as exc:
                    log(
                        "[order] HTTP 会话重建失败（继续重试）: "
                        + sanitize_message(
                            f"{type(exc).__name__}: {exc}",
                            access_token=access_token,
                            max_length=220,
                        )
                    )

        delay = min(1.5 * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5), 6.0)
        status_match = re.search(r"\bHTTP\s+(\d{3})\b", str(error or ""), re.IGNORECASE)
        reason = f"HTTP {status_match.group(1)}" if status_match else "网络异常"
        log(
            f"[order] Checkout 账单生成临时失败，"
            f"{delay:.1f}s 后重试 {attempt + 1}/{attempts}（{reason}）"
        )
        time.sleep(delay)

    return None, last_error


# ---------------------------------------------------------------------------
# OAICS custom Checkout（纯 HTTP）
# ---------------------------------------------------------------------------

_OAICS_WRAPPER_KEYS = (
    "checkout_session",
    "checkoutSession",
    "session",
    "checkout",
    "data",
    "result",
    "payload",
    "response",
    "checkout_state",
    "checkoutState",
    "checkout_snapshot",
    "checkoutSnapshot",
)

_OAICS_AMOUNT_PATHS = (
    ("checkout_amount_minor",),
    ("total_summary", "due"),
    ("totalSummary", "due"),
    ("invoice", "amount_due"),
    ("invoice", "amountDue"),
    ("amount_due",),
    ("amountDue",),
    ("amount_total",),
    ("amountTotal",),
    ("total", "total"),
    ("total", "due"),
    ("total", "taxInclusive"),
    ("total", "taxInclusiveAmount"),
)


def _response_json(response: Any, stage: str) -> dict[str, Any]:
    text = str(getattr(response, "text", "") or "")
    try:
        payload = response.json()
    except Exception:
        try:
            payload = json.loads(text or "{}")
        except ValueError as exc:
            raise RuntimeError(f"{stage} 返回非 JSON：{text[:300]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{stage} 返回无效 JSON 对象")
    return payload


def _oaics_headers(
    access_token: str,
    *,
    country: str,
    device_id: str,
    referer: str,
    route: str = "",
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        **chatgpt_context_headers(
            country=country,
            device_id=device_id,
            referer=referer,
        ),
    }
    if route:
        headers["x-openai-target-path"] = route
        headers["x-openai-target-route"] = route
    return headers


def fetch_oaics_checkout_session(
    http,
    access_token: str,
    session_id: str,
    processor_entity: str,
    *,
    country: str,
    device_id: str,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    if not str(session_id or "").startswith("oaics_"):
        raise OaicsCheckoutRequiredError(session_id)
    checkout_url = f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"
    route = "/backend-api/payments/checkout/{processor_entity}/{checkout_session_id}"
    request_summary = {
        "checkout_session_id": session_id,
        "processor_entity": processor_entity,
        "country": str(country or "").upper(),
    }
    try:
        response = http.get(
            f"{OPENAI_CHECKOUT_URL}/{processor_entity}/{session_id}",
            headers=_oaics_headers(
                access_token,
                country=country,
                device_id=device_id,
                referer=checkout_url,
                route=route,
            ),
            timeout=45,
        )
    except Exception as exc:
        _protocol_diagnostic(
            log,
            kind="checkout_state",
            method="GET",
            route=route,
            request_payload=request_summary,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    status = int(getattr(response, "status_code", 0) or 0)
    text = str(getattr(response, "text", "") or "")
    response_payload = _diagnostic_response_payload(response)
    _protocol_diagnostic(
        log,
        kind="checkout_state",
        method="GET",
        route=route,
        status=status,
        request_payload=request_summary,
        response_payload=response_payload,
        response=response,
    )
    if status == 401:
        raise ChatGPTAuthError("读取 OAICS Checkout 失败 HTTP 401")
    if status != 200:
        raise RuntimeError(f"读取 OAICS Checkout 失败 HTTP {status}: {text[:300]}")
    return _response_json(response, "读取 OAICS Checkout")


def submit_oaics_checkout_taxes(
    http,
    access_token: str,
    session_id: str,
    processor_entity: str,
    *,
    billing: dict[str, Any],
    country: str,
    currency: str,
    device_id: str,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    normalized_country = str(country or "").upper()
    normalized_currency = str(currency or "").upper()
    source_address = billing.get("address") if isinstance(billing, dict) else {}
    source_address = source_address if isinstance(source_address, dict) else {}
    address = {
        "country": normalized_country,
        "line1": str(source_address.get("line1") or ""),
        "line2": str(source_address.get("line2") or ""),
        "city": str(source_address.get("city") or ""),
        "state": str(source_address.get("state") or ""),
        "postal_code": str(source_address.get("postal_code") or ""),
    }
    checkout_url = f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"
    route = "/backend-api/payments/checkout/taxes"
    body = {
        "checkout_session_id": session_id,
        "checkout_email": str(billing.get("email") or ""),
        "billing_country": normalized_country,
        "billing_name": str(billing.get("name") or ""),
        "currency": normalized_currency,
        "tax_id": str(billing.get("tax_id") or "") or None,
        "processor_entity": processor_entity,
        "billing_address": address,
    }
    try:
        response = http.post(
            OPENAI_CHECKOUT_TAXES_URL,
            json=body,
            headers={
                **_oaics_headers(
                    access_token,
                    country=normalized_country,
                    device_id=device_id,
                    referer=checkout_url,
                    route=route,
                ),
                "Content-Type": "application/json",
            },
            timeout=50,
        )
    except Exception as exc:
        _protocol_diagnostic(
            log,
            kind="checkout_taxes",
            method="POST",
            route=route,
            request_payload=body,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    status = int(getattr(response, "status_code", 0) or 0)
    text = str(getattr(response, "text", "") or "")
    _protocol_diagnostic(
        log,
        kind="checkout_taxes",
        method="POST",
        route=route,
        status=status,
        request_payload=body,
        response_payload=_diagnostic_response_payload(response),
        response=response,
    )
    if status == 401:
        raise ChatGPTAuthError("提交 OAICS 账单失败 HTTP 401")
    if status != 200:
        raise RuntimeError(f"提交 OAICS 账单失败 HTTP {status}: {text[:300]}")
    return _response_json(response, "提交 OAICS 账单")


def _nested_value(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _oaics_money_minor(value: Any) -> Optional[int]:
    if isinstance(value, dict):
        for key in ("minorUnitsAmount", "minor_units_amount", "amount"):
            if value.get(key) is not None:
                return _oaics_money_minor(value.get(key))
        return None
    return _minor_amount(value)


def oaics_amount_observations(payload: Any) -> list[tuple[str, int]]:
    """Return payable amount fields without treating undiscounted unit prices as due."""
    observations: list[tuple[str, int]] = []
    visited: set[int] = set()

    def visit(value: Any, prefix: str = "") -> None:
        if not isinstance(value, dict) or id(value) in visited:
            return
        visited.add(id(value))
        for path in _OAICS_AMOUNT_PATHS:
            raw = _nested_value(value, path)
            amount = _oaics_money_minor(raw)
            if amount is not None:
                label = ".".join(path)
                observations.append((f"{prefix}{label}", amount))
        for key in _OAICS_WRAPPER_KEYS:
            nested = value.get(key)
            if isinstance(nested, dict):
                visit(nested, f"{prefix}{key}.")

    visit(payload)
    return list(dict.fromkeys(observations))


def oaics_checkout_currency(payload: Any) -> str:
    visited: set[int] = set()

    def find(value: Any) -> str:
        if not isinstance(value, dict) or id(value) in visited:
            return ""
        visited.add(id(value))
        for key in ("currency", "currency_code", "currencyCode"):
            candidate = str(value.get(key) or "").strip().upper()
            if re.fullmatch(r"[A-Z]{3}", candidate):
                return candidate
        for key in (*_OAICS_WRAPPER_KEYS, "total", "total_summary", "totalSummary"):
            candidate = find(value.get(key))
            if candidate:
                return candidate
        return ""

    return find(payload)


def verify_oaics_zero_snapshot(
    payload: Any,
    *,
    session_id: str,
    currency: str,
) -> int:
    observations = oaics_amount_observations(payload)
    if not observations:
        raise PromoNotAppliedError(
            session_id,
            "unknown",
            oaics_checkout_currency(payload) or currency,
            "OAICS 未返回可核验的应付金额",
        )
    nonzero = [(label, amount) for label, amount in observations if amount != 0]
    if nonzero:
        detail = ", ".join(f"{label}={amount}" for label, amount in nonzero)
        raise PromoNotAppliedError(
            session_id,
            nonzero[0][1],
            oaics_checkout_currency(payload) or currency,
            detail,
        )
    return 0


def wait_for_oaics_zero(
    http,
    access_token: str,
    session_id: str,
    processor_entity: str,
    *,
    country: str,
    currency: str,
    device_id: str,
    initial_payload: Optional[dict[str, Any]] = None,
    attempts: int = ZERO_AMOUNT_VERIFY_ATTEMPTS,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    payload = initial_payload or {}
    last_error: PromoNotAppliedError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        if not payload or attempt > 1:
            payload = fetch_oaics_checkout_session(
                http,
                access_token,
                session_id,
                processor_entity,
                country=country,
                device_id=device_id,
                log=log,
            )
        try:
            verify_oaics_zero_snapshot(
                payload,
                session_id=session_id,
                currency=currency,
            )
            log(f"[oaics] 0 元订单已确认（session={session_id}）")
            return payload
        except PromoNotAppliedError as exc:
            last_error = exc
            if attempt < max(1, attempts):
                log(
                    f"[oaics] 等待 0 元状态同步 "
                    f"{attempt + 1}/{max(1, attempts)}"
                )
                time.sleep(ZERO_AMOUNT_VERIFY_DELAY_SECONDS)
    assert last_error is not None
    raise last_error


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_dicts(nested)


def oaics_custom_payment_methods(payload: Any) -> list[dict[str, Any]]:
    methods: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _walk_dicts(payload):
        candidates = item.get("custom_payment_methods")
        if candidates is None:
            candidates = item.get("customPaymentMethods")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            method_id = str(candidate.get("id") or "").strip()
            if not method_id.startswith("cpmt_") or method_id in seen:
                continue
            seen.add(method_id)
            methods.append(candidate)
    methods.sort(
        key=lambda item: (
            0
            if "paypal" in json.dumps(item, ensure_ascii=True).lower()
            else 1
        )
    )
    return methods


def oaics_payment_method_types(payload: Any) -> list[str]:
    methods: list[str] = []
    seen: set[str] = set()
    for item in _walk_dicts(payload):
        candidates = item.get("payment_method_types")
        if candidates is None:
            candidates = item.get("paymentMethodTypes")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if isinstance(candidate, dict):
                candidate = candidate.get("type")
            method = str(candidate or "").strip().lower()
            if method and method not in seen:
                seen.add(method)
                methods.append(method)
    return methods


def _oaics_find_string(
    payload: Any,
    names: tuple[str, ...],
    *,
    prefixes: tuple[str, ...] = (),
) -> str:
    for item in _walk_dicts(payload):
        for name in names:
            candidate = item.get(name)
            if not isinstance(candidate, str):
                continue
            value = candidate.strip()
            if value and (not prefixes or value.startswith(prefixes)):
                return value
    return ""


def create_oaics_elements_session(
    http,
    state: dict[str, Any],
    *,
    country: str,
    currency: str,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    publishable_key = str(state.get("publishable_key") or "").strip()
    customer_secret = str(state.get("customer_session_client_secret") or "").strip()
    if not publishable_key.startswith(("pk_live_", "pk_test_")):
        raise RuntimeError("OAICS PayPal 缺少 Stripe publishable_key")
    if not customer_secret:
        raise RuntimeError("OAICS PayPal 缺少 customer_session_client_secret")
    methods = oaics_payment_method_types(state)
    stripe_js_id = str(uuid.uuid4())
    params = {
        "customer_session_client_secret": customer_secret,
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": "0",
        "deferred_intent[currency]": str(currency or "").lower(),
        "deferred_intent[setup_future_usage]": "off_session",
        "currency": str(currency or "").lower(),
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION_FULL,
        "elements_init_source": "stripe.elements",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": stripe_js_id,
        "locale": _profile(country)["browser_locale"],
        "type": "deferred_intent",
    }
    for index, method in enumerate(methods):
        params[f"deferred_intent[payment_method_types][{index}]"] = method
    route = "/v1/elements/sessions"
    try:
        response = http.get(
            f"{STRIPE_API}{route}",
            params=params,
            headers=_stripe_headers(),
            timeout=40,
        )
    except Exception as exc:
        _protocol_diagnostic(
            log,
            kind="stripe_elements_session",
            method="GET",
            route=route,
            request_payload=params,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    status = int(getattr(response, "status_code", 0) or 0)
    response_payload = _diagnostic_response_payload(response)
    _protocol_diagnostic(
        log,
        kind="stripe_elements_session",
        method="GET",
        route=route,
        status=status,
        request_payload=params,
        response_payload=response_payload,
        response=response,
    )
    if status != 200:
        raise RuntimeError(f"OAICS PayPal Elements Session 失败 HTTP {status}")
    payload = _response_json(response, "初始化 OAICS PayPal Elements Session")
    payload["_oaics_publishable_key"] = publishable_key
    payload["_oaics_stripe_js_id"] = stripe_js_id
    payload["_oaics_payment_method_types"] = methods
    return payload


def create_oaics_paypal_confirmation_token(
    http,
    state: dict[str, Any],
    elements: dict[str, Any],
    *,
    billing: dict[str, Any],
    currency: str,
    log: Callable[[str], None] = lambda _message: None,
) -> str:
    publishable_key = str(elements.get("_oaics_publishable_key") or "").strip()
    stripe_js_id = str(elements.get("_oaics_stripe_js_id") or uuid.uuid4())
    elements_session_id = _oaics_find_string(
        elements,
        ("session_id", "sessionId", "id"),
        prefixes=("elements_session_",),
    )
    elements_config_id = _oaics_find_string(
        elements,
        ("config_id", "elements_session_config_id", "elementsSessionConfigId"),
    )
    customer = _oaics_find_string(
        elements,
        ("customer", "customer_id", "customerId"),
        prefixes=("cus_",),
    )
    methods = list(elements.get("_oaics_payment_method_types") or [])
    address = billing.get("address") if isinstance(billing, dict) else {}
    address = address if isinstance(address, dict) else {}
    guid, muid, sid = _gen_fingerprint()
    runtime_version = DEFAULT_STRIPE_RUNTIME_VERSION
    body: dict[str, Any] = {
        "payment_method_data[type]": "paypal",
        "payment_method_data[billing_details][name]": str(billing.get("name") or ""),
        "payment_method_data[billing_details][email]": str(billing.get("email") or ""),
        "payment_method_data[guid]": guid,
        "payment_method_data[muid]": muid,
        "payment_method_data[sid]": sid,
        "payment_method_data[payment_user_agent]": (
            f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
            "payment-element; deferred-intent"
        ),
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(random.randint(25000, 55000)),
        "setup_future_usage": "off_session",
        "set_as_default_payment_method": "false",
        "mandate_data[customer_acceptance][type]": "online",
        "mandate_data[customer_acceptance][online][infer_from_client]": "true",
        "client_context[currency]": str(currency or "").lower(),
        "client_context[mode]": "subscription",
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
        "key": publishable_key,
    }
    for field in ("line1", "line2", "city", "state", "postal_code", "country"):
        value = str(address.get(field) or "")
        if value:
            body[f"payment_method_data[billing_details][address][{field}]"] = value
    for index, method in enumerate(methods):
        body[f"client_context[payment_method_types][{index}]"] = method
    if customer:
        body["client_context[customer]"] = customer
    for prefix in (
        "client_attribution_metadata",
        "payment_method_data[client_attribution_metadata]",
    ):
        if elements_session_id:
            body[f"{prefix}[elements_session_id]"] = elements_session_id
        if elements_config_id:
            body[f"{prefix}[elements_session_config_id]"] = elements_config_id
    route = "/v1/confirmation_tokens"
    headers = {
        **_stripe_headers(),
        # A customer ephemeral key can only reference existing saved methods.
        # Creating a new PayPal payment_method_data token uses the merchant PK.
        "Authorization": f"Bearer {publishable_key}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Stripe-Version": STRIPE_VERSION_FULL,
    }
    try:
        response = http.post(
            f"{STRIPE_API}{route}",
            data=body,
            headers=headers,
            timeout=40,
        )
    except Exception as exc:
        _protocol_diagnostic(
            log,
            kind="stripe_confirmation_token",
            method="POST",
            route=route,
            request_payload=body,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    status = int(getattr(response, "status_code", 0) or 0)
    response_payload = _diagnostic_response_payload(response)
    _protocol_diagnostic(
        log,
        kind="stripe_confirmation_token",
        method="POST",
        route=route,
        status=status,
        request_payload=body,
        response_payload=response_payload,
        response=response,
    )
    if status != 200:
        error = response_payload.get("error") if isinstance(response_payload, dict) else {}
        error = error if isinstance(error, dict) else {}
        detail = str(error.get("code") or error.get("type") or "").strip()
        suffix = f" ({detail})" if detail else ""
        raise RuntimeError(f"OAICS PayPal ConfirmationToken 失败 HTTP {status}{suffix}")
    payload = _response_json(response, "创建 OAICS PayPal ConfirmationToken")
    confirmation_token = _oaics_find_string(
        payload,
        ("id", "confirmation_token", "confirmationToken"),
        prefixes=("ctoken_", "ct_"),
    )
    if not confirmation_token:
        raise RuntimeError("OAICS PayPal ConfirmationToken 响应缺少 token")
    return confirmation_token


def confirm_oaics_standard_paypal(
    http,
    access_token: str,
    session_id: str,
    processor_entity: str,
    confirmation_token: str,
    *,
    country: str,
    device_id: str,
    sentinel_proxy: str,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    checkout_url = f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"
    route = "/backend-api/payments/checkout/confirm"
    headers = {
        **_oaics_headers(
            access_token,
            country=country,
            device_id=device_id,
            referer=checkout_url,
            route=route,
        ),
        "Content-Type": "application/json",
    }
    sentinel, sentinel_so = _mint_sentinel(
        "checkout_session_approval",
        SentinelCtx(
            device_id=device_id,
            user_agent=BROWSER_UA,
            proxy=sentinel_proxy,
            country=country,
            page_url=checkout_url,
            cookie_header=_session_cookie_header(http),
        ),
        log,
    )
    if sentinel:
        headers["OpenAI-Sentinel-Token"] = sentinel
    if sentinel_so:
        headers["OpenAI-Sentinel-SO-Token"] = sentinel_so
    body = {
        "checkout_session_id": session_id,
        "confirm_token": confirmation_token,
        "selected_payment_method_type": "paypal",
    }
    try:
        response = http.post(
            OPENAI_CHECKOUT_CONFIRM_URL,
            json=body,
            headers=headers,
            timeout=50,
        )
    except Exception as exc:
        _protocol_diagnostic(
            log,
            kind="checkout_confirm",
            method="POST",
            route=route,
            request_payload=body,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    status_code = int(getattr(response, "status_code", 0) or 0)
    response_payload = _diagnostic_response_payload(response)
    _protocol_diagnostic(
        log,
        kind="checkout_confirm",
        method="POST",
        route=route,
        status=status_code,
        request_payload=body,
        response_payload=response_payload,
        response=response,
    )
    if status_code == 401:
        raise ChatGPTAuthError("OAICS PayPal confirm 失败 HTTP 401")
    if status_code != 200:
        raise RuntimeError(f"OAICS PayPal confirm 失败 HTTP {status_code}")
    payload = _response_json(response, "确认 OAICS PayPal")
    status = str(payload.get("status") or "").strip().lower()
    if status == "blocked":
        raise OaicsConfirmBlockedError("OAICS PayPal confirm blocked")
    if not status:
        raise RuntimeError("OAICS PayPal confirm 响应缺少 status")
    if status in {"declined", "failed", "error", "requires_action"}:
        raise RuntimeError(f"OAICS PayPal confirm 失败 status={status}")
    return payload


def confirm_oaics_paypal_intent(
    http,
    confirmation_token: str,
    app_confirm: dict[str, Any],
    elements: dict[str, Any],
    *,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    publishable_key = str(elements.get("_oaics_publishable_key") or "").strip()
    intent_type = str(app_confirm.get("type") or "").strip().lower()
    client_secret = str(app_confirm.get("client_secret") or "").strip()
    if "_secret_" not in client_secret:
        raise RuntimeError("OAICS PayPal confirm 未返回 Intent client_secret")
    intent_id = client_secret.split("_secret_", 1)[0]
    if intent_id.startswith("pi_"):
        expected_type, collection = "payment_intent", "payment_intents"
    elif intent_id.startswith("seti_"):
        expected_type, collection = "setup_intent", "setup_intents"
    else:
        raise RuntimeError("OAICS PayPal confirm 返回了未知 Intent")
    if intent_type and intent_type != expected_type:
        raise RuntimeError("OAICS PayPal confirm 返回的 Intent 类型不一致")
    body = {
        "confirmation_token": confirmation_token,
        "client_secret": client_secret,
        "use_stripe_sdk": "true",
        "key": publishable_key,
    }
    return_url = str(app_confirm.get("confirm_return_url") or "").strip()
    if return_url:
        body["return_url"] = return_url
    route = f"/v1/{collection}/{intent_id}/confirm"
    headers = {
        **_stripe_headers(),
        "Authorization": f"Bearer {publishable_key}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Stripe-Version": STRIPE_VERSION_FULL,
    }
    try:
        response = http.post(
            f"{STRIPE_API}{route}",
            data=body,
            headers=headers,
            timeout=50,
        )
    except Exception as exc:
        _protocol_diagnostic(
            log,
            kind="stripe_intent_confirm",
            method="POST",
            route=route,
            request_payload=body,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    status_code = int(getattr(response, "status_code", 0) or 0)
    response_payload = _diagnostic_response_payload(response)
    _protocol_diagnostic(
        log,
        kind="stripe_intent_confirm",
        method="POST",
        route=route,
        status=status_code,
        request_payload=body,
        response_payload=response_payload,
        response=response,
    )
    if isinstance(response_payload, dict):
        _raise_for_current_paypal_risk_decline(response_payload, "")
    if status_code != 200:
        error = response_payload.get("error") if isinstance(response_payload, dict) else {}
        error = error if isinstance(error, dict) else {}
        code = str(error.get("code") or error.get("type") or "").strip()
        param = str(error.get("param") or "").strip()
        detail = "/".join(item for item in (code, param) if item)
        suffix = f" ({detail})" if detail else ""
        raise RuntimeError(f"OAICS PayPal Intent confirm 失败 HTTP {status_code}{suffix}")
    return _response_json(response, "确认 OAICS PayPal Intent")


def oaics_standard_paypal_redirect(
    http,
    state: dict[str, Any],
    *,
    access_token: str,
    session_id: str,
    processor_entity: str,
    billing: dict[str, Any],
    country: str,
    currency: str,
    device_id: str,
    sentinel_proxy: str,
    log: Callable[[str], None] = lambda _message: None,
) -> tuple[str, dict[str, Any]]:
    elements = create_oaics_elements_session(
        http,
        state,
        country=country,
        currency=currency,
        log=log,
    )
    confirmation_token = create_oaics_paypal_confirmation_token(
        http,
        state,
        elements,
        billing=billing,
        currency=currency,
        log=log,
    )
    try:
        app_confirm = confirm_oaics_standard_paypal(
            http,
            access_token,
            session_id,
            processor_entity,
            confirmation_token,
            country=country,
            device_id=device_id,
            sentinel_proxy=sentinel_proxy,
            log=log,
        )
    except OaicsConfirmBlockedError:
        log("[oaics] 标准 PayPal confirm 首次 blocked，刷新 SEN/SO 后重试")
        app_confirm = confirm_oaics_standard_paypal(
            http,
            access_token,
            session_id,
            processor_entity,
            confirmation_token,
            country=country,
            device_id=device_id,
            sentinel_proxy=sentinel_proxy,
            log=log,
        )
    redirect_url = extract_redirect_url(app_confirm)
    intent_confirm: dict[str, Any] = {}
    if not redirect_url:
        intent_confirm = confirm_oaics_paypal_intent(
            http,
            confirmation_token,
            app_confirm,
            elements,
            log=log,
        )
        redirect_url = extract_redirect_url(intent_confirm)
    if not redirect_url:
        raise RuntimeError("OAICS PayPal Intent confirm 未返回 PayPal 跳转地址")
    return redirect_url, {
        "checkout_state": state,
        "elements": elements,
        "app_confirm": app_confirm,
        "intent_confirm": intent_confirm,
        "selected_payment_method_type": "paypal",
    }


def confirm_oaics_custom_payment_method(
    http,
    access_token: str,
    session_id: str,
    processor_entity: str,
    custom_payment_method_id: str,
    *,
    country: str,
    device_id: str,
    sentinel_proxy: str,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    checkout_url = f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"
    headers = {
        **_oaics_headers(
            access_token,
            country=country,
            device_id=device_id,
            referer=checkout_url,
            route="/backend-api/payments/checkout/confirm",
        ),
        "Content-Type": "application/json",
    }
    sentinel, sentinel_so = _mint_sentinel(
        "checkout_session_approval",
        SentinelCtx(
            device_id=device_id,
            user_agent=BROWSER_UA,
            proxy=sentinel_proxy,
            country=country,
            page_url=checkout_url,
            cookie_header=_session_cookie_header(http),
        ),
        log,
    )
    if sentinel:
        headers["OpenAI-Sentinel-Token"] = sentinel
    if sentinel_so:
        headers["OpenAI-Sentinel-SO-Token"] = sentinel_so
    route = "/backend-api/payments/checkout/confirm"
    body = {
        "checkout_session_id": session_id,
        "processor_entity": processor_entity,
        "selected_payment_method_type": custom_payment_method_id,
    }
    try:
        response = http.post(
            OPENAI_CHECKOUT_CONFIRM_URL,
            json=body,
            headers=headers,
            timeout=50,
        )
    except Exception as exc:
        _protocol_diagnostic(
            log,
            kind="checkout_confirm",
            method="POST",
            route=route,
            request_payload=body,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    status_code = int(getattr(response, "status_code", 0) or 0)
    text = str(getattr(response, "text", "") or "")
    response_payload = _diagnostic_response_payload(response)
    _protocol_diagnostic(
        log,
        kind="checkout_confirm",
        method="POST",
        route=route,
        status=status_code,
        request_payload=body,
        response_payload=response_payload,
        response=response,
    )
    try:
        payload = _response_json(response, "确认 OAICS PayPal 支付方式")
    except RuntimeError:
        if status_code != 200:
            raise RuntimeError(
                f"确认 OAICS PayPal 支付方式失败 HTTP {status_code}: {text[:300]}"
            )
        raise
    status = str(payload.get("status") or "").strip().lower()
    if status_code == 401:
        raise ChatGPTAuthError("确认 OAICS PayPal 支付方式失败 HTTP 401")
    if status == "blocked" or "blocked" in text.lower():
        raise OaicsConfirmBlockedError("OAICS PayPal confirm blocked")
    if status_code != 200:
        raise RuntimeError(
            f"确认 OAICS PayPal 支付方式失败 HTTP {status_code}: {text[:300]}"
        )
    if status != "success":
        raise RuntimeError(
            f"确认 OAICS PayPal 支付方式失败 status={status or 'unknown'}: {text[:300]}"
        )
    return payload


def start_oaics_custom_payment_method(
    http,
    access_token: str,
    session_id: str,
    processor_entity: str,
    custom_payment_method_id: str,
    *,
    country: str,
    device_id: str,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    checkout_url = f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"
    route = "/backend-api/payments/checkout/custom_payment_method/start"
    body = {
        "checkout_session_id": session_id,
        "processor_entity": processor_entity,
        "custom_payment_method_type_id": custom_payment_method_id,
    }
    try:
        response = http.post(
            OPENAI_CUSTOM_PAYMENT_START_URL,
            json=body,
            headers={
                **_oaics_headers(
                    access_token,
                    country=country,
                    device_id=device_id,
                    referer=checkout_url,
                    route=route,
                ),
                "Content-Type": "application/json",
            },
            timeout=60,
        )
    except Exception as exc:
        _protocol_diagnostic(
            log,
            kind="custom_payment_start",
            method="POST",
            route=route,
            request_payload=body,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    status_code = int(getattr(response, "status_code", 0) or 0)
    text = str(getattr(response, "text", "") or "")
    _protocol_diagnostic(
        log,
        kind="custom_payment_start",
        method="POST",
        route=route,
        status=status_code,
        request_payload=body,
        response_payload=_diagnostic_response_payload(response),
        response=response,
    )
    if status_code == 401:
        raise ChatGPTAuthError("启动 OAICS PayPal 支付失败 HTTP 401")
    if status_code != 200:
        raise RuntimeError(f"启动 OAICS PayPal 支付失败 HTTP {status_code}: {text[:300]}")
    payload = _response_json(response, "启动 OAICS PayPal 支付")
    action = payload.get("next_action")
    action = action if isinstance(action, dict) else {}
    action_url = str(action.get("url") or "").strip()
    if str(payload.get("status") or "").lower() != "requires_action" or not action_url:
        raise RuntimeError(f"OAICS PayPal start 未返回跳转地址: {text[:300]}")
    return payload


def oaics_to_paypal_redirect(
    http,
    session_id: str,
    *,
    access_token: str,
    processor_entity: str,
    billing: dict[str, Any],
    country: str,
    currency: str,
    device_id: str,
    sentinel_proxy: str,
    log: Callable[[str], None] = lambda _message: None,
) -> tuple[str, dict[str, Any]]:
    state: dict[str, Any] = {}
    methods: list[dict[str, Any]] = []
    for attempt in range(1, 4):
        state = fetch_oaics_checkout_session(
            http,
            access_token,
            session_id,
            processor_entity,
            country=country,
            device_id=device_id,
            log=log,
        )
        verify_oaics_zero_snapshot(
            state,
            session_id=session_id,
            currency=currency,
        )
        payment_method_types = oaics_payment_method_types(state)
        if "paypal" in payment_method_types:
            log("[oaics] Checkout 已开放标准 PayPal，进入 ConfirmationToken 流程")
            return oaics_standard_paypal_redirect(
                http,
                state,
                access_token=access_token,
                session_id=session_id,
                processor_entity=processor_entity,
                billing=billing,
                country=country,
                currency=currency,
                device_id=device_id,
                sentinel_proxy=sentinel_proxy,
                log=log,
            )
        methods = oaics_custom_payment_methods(state)
        if methods:
            break
        if payment_method_types:
            raise PayPalFundingUnavailableError(
                session_id,
                payment_method_types,
                "OAICS payment_method_types 未包含 paypal",
            )
        if attempt < 3:
            log(f"[oaics] 等待 Checkout 支付方式同步 {attempt + 1}/3")
            time.sleep(0.8 * attempt)
    if not methods:
        raise RuntimeError(
            "OAICS Checkout 未返回可判定的 payment_method_types 或 cpmt_ 支付方式"
        )

    last_blocked: OaicsConfirmBlockedError | None = None
    for method in methods:
        method_id = str(method.get("id") or "")
        try:
            confirmed = confirm_oaics_custom_payment_method(
                http,
                access_token,
                session_id,
                processor_entity,
                method_id,
                country=country,
                device_id=device_id,
                sentinel_proxy=sentinel_proxy,
                log=log,
            )
        except OaicsConfirmBlockedError as exc:
            last_blocked = exc
            log("[oaics] PayPal confirm 首次 blocked，刷新 SEN/SO 后重试")
            confirmed = confirm_oaics_custom_payment_method(
                http,
                access_token,
                session_id,
                processor_entity,
                method_id,
                country=country,
                device_id=device_id,
                sentinel_proxy=sentinel_proxy,
                log=log,
            )
        started = start_oaics_custom_payment_method(
            http,
            access_token,
            session_id,
            processor_entity,
            method_id,
            country=country,
            device_id=device_id,
            log=log,
        )
        action = started.get("next_action")
        action = action if isinstance(action, dict) else {}
        redirect_url = str(action.get("url") or "").strip()
        payment_type = str(
            action.get("paymentMethodType")
            or action.get("payment_method_type")
            or ""
        ).lower()
        if "paypal" in payment_type or "paypal" in redirect_url.lower():
            return redirect_url, {
                "checkout_state": state,
                "confirmed": confirmed,
                "started": started,
                "custom_payment_method_id": method_id,
            }
        log(f"[oaics] 自定义支付 {method_id[:14]} 不是 PayPal，继续检查")
    if last_blocked is not None:
        raise last_blocked
    raise PayPalFundingUnavailableError(
        session_id,
        [str(item.get("id") or "") for item in methods],
        "OAICS 自定义支付方式均未返回 PayPal 跳转",
    )


# ---------------------------------------------------------------------------
# Stripe Hosted Checkout（纯 HTTP）
# ---------------------------------------------------------------------------

def verify_pk(http, session_id: str, log: Callable[[str], None]) -> str:
    """用已知 OpenAI Stripe pk 探测 /init，返回有效 pk。

    代理偶发 TLS 握手错误(curl 35 / OPENSSL_internal)时对每个 pk 做几次重试；
    若探测整体因网络抖动失败，回退到首个已知 pk 让下游继续（下游会再校验）。
    """
    last = ""
    tls_flaky = False
    keys = list(KNOWN_PUBLISHABLE_KEYS.values())
    for pk in keys:
        for attempt in range(3):
            try:
                resp = http.post(
                    f"{STRIPE_API}/v1/payment_pages/{session_id}/init",
                    data={"key": pk, "_stripe_version": STRIPE_VERSION_BASE, "browser_locale": "en-US"},
                    headers=_stripe_headers(),
                    timeout=15,
                )
                if getattr(resp, "status_code", 0) == 200:
                    log(f"[stripe] publishable_key OK: {pk[:24]}…")
                    return pk
                last = f"{getattr(resp, 'status_code', '?')}: {(getattr(resp, 'text', '') or '')[:120]}"
                break  # 非网络错误（如 400）无需重试该 pk
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                low = str(e).lower()
                if any(k in low for k in ("tls", "ssl", "curl: (35)", "curl: (56)", "curl: (28)", "timed out", "connection")):
                    tls_flaky = True
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break  # 非网络错误，换下一个 pk
    if tls_flaky and keys:
        log(f"[stripe] pk 探测网络抖动，回退首个已知 pk（last={last[:80]}）")
        return keys[0]
    raise RuntimeError(f"无法确认 Stripe publishable_key（{last}）")


def init_checkout(http, session_id: str, pk: str, profile: dict, log) -> tuple[dict, str, dict]:
    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/init"
    stripe_js_id = str(uuid.uuid4())
    elements_session_id = _gen_elements_session_id()
    elements_options = _elements_options_client_payload()

    for version in (STRIPE_VERSION_BASE, STRIPE_VERSION_FULL):
        data = {
            "browser_locale": profile["browser_locale"],
            "browser_timezone": profile["browser_timezone"],
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": stripe_js_id,
            "elements_session_client[locale]": profile["browser_locale"],
            "elements_session_client[is_aggregation_expected]": "false",
            "key": pk,
            "_stripe_version": version,
        }
        data.update(elements_options)
        if version == STRIPE_VERSION_FULL:
            data["elements_session_client[client_betas][0]"] = "custom_checkout_server_updates_1"
            data["elements_session_client[client_betas][1]"] = "custom_checkout_manual_approval_1"

        resp = http.post(url, data=data, headers=_stripe_headers(), timeout=30)
        dropped: set[str] = set()
        while getattr(resp, "status_code", 0) == 400:
            param = _stripe_unknown_param(resp)
            if not param or param not in data or param in dropped:
                break
            dropped.add(param)
            data.pop(param, None)
            elements_options.pop(param, None)
            resp = http.post(url, data=data, headers=_stripe_headers(), timeout=30)

        sc = getattr(resp, "status_code", 0)
        if sc == 200:
            init_data = resp.json()
            total = init_data.get("total_summary") or {}
            amount = total.get("due")
            if amount is None:
                amount = (init_data.get("invoice") or {}).get("amount_due")
            ctx = {
                "stripe_js_id": stripe_js_id,
                "client_session_id": str(uuid.uuid4()),
                "elements_session_id": elements_session_id,
                "elements_options_client": dict(elements_options),
                "browser_locale": profile["browser_locale"],
                "locale": init_data.get("locale") or _locale_short(profile),
                "currency": (init_data.get("currency") or "usd").lower(),
                "checkout_amount": amount,
                "payment_method_types": _extract_payment_method_types(init_data),
                "config_id": init_data.get("config_id", ""),
                "init_checksum": init_data.get("init_checksum", ""),
                "return_url": init_data.get("return_url") or "",
                "stripe_hosted_url": init_data.get("stripe_hosted_url") or "",
            }
            log(f"[stripe] init ok version={version[:24]} amount={amount} currency={ctx['currency']} pm={ctx['payment_method_types']}")
            return init_data, version, ctx
        if sc == 400 and "beta" in (getattr(resp, "text", "") or "").lower():
            continue
        raise RuntimeError(f"init 失败 [{sc}]: {(getattr(resp, 'text', '') or '')[:300]}")
    raise RuntimeError("init 失败：所有 Stripe 版本均不可用")


_ZERO_AMOUNT_PATHS = (
    ("total_summary", "due", "total_summary.due"),
    ("total_summary", "total", "total_summary.total"),
    ("invoice", "amount_due", "invoice.amount_due"),
    ("invoice", "total", "invoice.total"),
    ("elements_options", "amount", "elements_options.amount"),
)


def _minor_amount(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?\d+(?:\.0+)?", text):
        return None
    return int(text.split(".", 1)[0])


def verify_zero_amount_snapshot(init_data: Any, ctx: Optional[dict] = None) -> tuple[bool, str, Any]:
    """Accept a checkout only when every exposed amount field is zero."""
    if not isinstance(init_data, dict):
        return False, "Stripe init 返回无效", "unknown"

    values: list[tuple[str, int]] = []
    invalid: list[str] = []
    for parent_key, child_key, label in _ZERO_AMOUNT_PATHS:
        parent = init_data.get(parent_key)
        if not isinstance(parent, dict) or child_key not in parent:
            continue
        amount = _minor_amount(parent.get(child_key))
        if amount is None:
            invalid.append(label)
        else:
            values.append((label, amount))

    payment_intent = init_data.get("payment_intent")
    if isinstance(payment_intent, dict) and "amount" in payment_intent:
        amount = _minor_amount(payment_intent.get("amount"))
        if amount is None:
            invalid.append("payment_intent.amount")
        else:
            values.append(("payment_intent.amount", amount))

    if not values and isinstance(ctx, dict) and "checkout_amount" in ctx:
        amount = _minor_amount(ctx.get("checkout_amount"))
        if amount is None:
            invalid.append("checkout_amount")
        else:
            values.append(("checkout_amount", amount))

    if invalid:
        return False, f"金额字段无效: {','.join(invalid)}", "unknown"
    if not values:
        return False, "Stripe 未返回可核验的金额", "unknown"
    nonzero = [(label, amount) for label, amount in values if amount != 0]
    if nonzero:
        detail = ", ".join(f"{label}={amount}" for label, amount in nonzero)
        return False, detail, nonzero[0][1]
    return True, ", ".join(f"{label}=0" for label, _ in values), 0


def verify_checkout_zero(
    http,
    session_id: str,
    *,
    country: str,
    publishable_key: str = "",
    log: Callable[[str], None],
    attempts: int = ZERO_AMOUNT_VERIFY_ATTEMPTS,
) -> None:
    """Require a verifiable zero amount before any PayPal operation."""
    pk = str(publishable_key or "").strip() or verify_pk(http, session_id, log)
    last_amount: Any = "unknown"
    last_detail = ""
    for attempt in range(1, max(1, attempts) + 1):
        init_data, _version, ctx = init_checkout(http, session_id, pk, _profile(country), log)
        ok, detail, amount = verify_zero_amount_snapshot(init_data, ctx)
        if ok:
            log(f"[checkout] 0 元订单已确认（session={session_id}）")
            return
        last_amount, last_detail = amount, detail
        if attempt < max(1, attempts):
            log(
                f"[checkout] 应付金额尚未变为 0，保留同一订单重查 "
                f"{attempt + 1}/{max(1, attempts)}（{detail}）"
            )
            time.sleep(ZERO_AMOUNT_VERIFY_DELAY_SECONDS)
    raise PromoNotAppliedError(session_id, last_amount, "", last_detail)


def fetch_elements_session(http, pk: str, session_id: str, ctx: dict, version: str, profile: dict, log) -> dict:
    locale_short = ctx.get("locale") or _locale_short(profile)
    currency = (ctx.get("currency") or "usd").lower()
    amount = ctx.get("checkout_amount")
    amount = 0 if amount is None else int(amount)
    pmt = ctx.get("payment_method_types") or ["card"]
    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": currency,
        "deferred_intent[setup_future_usage]": "off_session",
        "currency": currency,
        "key": pk,
        "_stripe_version": version,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": ctx.get("stripe_js_id", str(uuid.uuid4())),
        "locale": locale_short,
        "type": "deferred_intent",
        "checkout_session_id": session_id,
    }
    for idx, pm in enumerate(pmt):
        params[f"deferred_intent[payment_method_types][{idx}]"] = pm
    try:
        resp = http.get(f"{STRIPE_API}/v1/elements/sessions", params=params, headers=_stripe_headers(), timeout=30)
    except Exception as e:
        log(f"[stripe] elements/sessions 异常（忽略）: {e}")
        return {}
    if getattr(resp, "status_code", 0) != 200:
        log(f"[stripe] elements/sessions [{getattr(resp, 'status_code', '?')}]（忽略，用本地 id）")
        return {}
    data = resp.json()
    real = data.get("session_id") or data.get("id")
    if real:
        ctx["elements_session_id"] = real
    if data.get("config_id"):
        ctx["elements_session_config_id"] = data["config_id"]
    types = [s["type"] for s in data.get("payment_method_specs", []) if isinstance(s, dict) and s.get("type")]
    if types:
        ctx["payment_method_types"] = types
    return data


def update_tax_region(
    http,
    session_id: str,
    pk: str,
    version: str,
    ctx: dict,
    billing: dict,
    profile: dict,
    log,
) -> dict:
    """Push the buyer address before confirm so Stripe can finish tax calculation."""
    address = billing.get("address") or {}
    data = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": ctx.get("stripe_js_id", str(uuid.uuid4())),
        "elements_session_client[session_id]": ctx.get(
            "elements_session_id", _gen_elements_session_id()
        ),
        "elements_session_client[locale]": ctx.get("locale") or _locale_short(profile),
        "elements_session_client[is_aggregation_expected]": "false",
        "key": pk,
        "_stripe_version": version,
    }
    for field, value in (
        ("country", address.get("country")),
        ("line1", address.get("line1")),
        ("city", address.get("city")),
        ("postal_code", address.get("postal_code")),
        ("state", address.get("state")),
    ):
        if value:
            data[f"tax_region[{field}]"] = value
    data.update(ctx.get("elements_options_client") or _elements_options_client_payload())
    try:
        response = http.post(
            f"{STRIPE_API}/v1/payment_pages/{session_id}",
            data=data,
            headers=_stripe_headers(),
            timeout=30,
        )
    except Exception as exc:
        log(f"[stripe] tax_region 更新异常（忽略）: {exc}")
        return {}
    if getattr(response, "status_code", 0) != 200:
        log(
            f"[stripe] tax_region 更新 [{getattr(response, 'status_code', '?')}]（忽略）: "
            f"{(getattr(response, 'text', '') or '')[:160]}"
        )
        return {}
    try:
        payload = response.json()
    except Exception:
        return {}
    total = payload.get("total_summary") or {}
    amount = total.get("total")
    if amount is None:
        amount = total.get("due")
    if amount is not None:
        ctx["checkout_amount"] = amount
    log(
        f"[stripe] tax_region 更新 ok amount={ctx.get('checkout_amount')} "
        f"tax_status={(payload.get('tax') or {}).get('status')}"
    )
    return payload


def update_checkout_promo(
    http,
    access_token: str,
    session_id: str,
    processor_entity: str,
    *,
    device_id: str = "",
    country: str = "US",
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    route = "/backend-api/payments/checkout/update"
    payload = {
        "checkout_session_id": session_id,
        "processor_entity": processor_entity,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "discount_code": None,
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
        "accept": "*/*",
        "origin": "https://chatgpt.com",
        "x-openai-target-path": route,
        "x-openai-target-route": route,
        **chatgpt_context_headers(
            country=country,
            device_id=device_id,
            referer=f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
        ),
    }
    try:
        response = http.post(
            f"https://chatgpt.com{route}",
            json=payload,
            headers=headers,
            timeout=45,
        )
    except Exception as exc:
        _protocol_diagnostic(
            log,
            kind="checkout_promo_update",
            method="POST",
            route=route,
            request_payload=payload,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    status = int(getattr(response, "status_code", 0) or 0)
    response_payload = _diagnostic_response_payload(response)
    _protocol_diagnostic(
        log,
        kind="checkout_promo_update",
        method="POST",
        route=route,
        status=status,
        request_payload=payload,
        response_payload=response_payload,
        response=response,
    )
    if status != 200:
        raise RuntimeError(f"Stripe 后置优惠更新失败 HTTP {status}")
    return response_payload if isinstance(response_payload, dict) else {}


def snapshot_billing(
    chatgpt_http,
    access_token: str,
    session_id: str,
    processor_entity: str,
    billing: dict,
    log,
    *,
    device_id: str = "",
) -> None:
    """Snapshot billing details for ChatGPT's manual approval state machine."""
    if chatgpt_http is None or not access_token:
        return
    address = billing.get("address") or {}
    snapshot_address = {
        "line1": address.get("line1", ""),
        "city": address.get("city", ""),
        "country": address.get("country", "US"),
        "postal_code": address.get("postal_code", ""),
    }
    if address.get("state"):
        snapshot_address["state"] = address["state"]
    payload = {
        "snapshot": {
            "billing_address": {
                "name": billing.get("name", ""),
                "address": snapshot_address,
            }
        }
    }
    headers = {
        "content-type": "application/json",
        "accept": "*/*",
        "authorization": f"Bearer {access_token}",
        "origin": "https://chatgpt.com",
        **chatgpt_context_headers(
            country=str(address.get("country") or "US"),
            device_id=device_id,
            referer=f"https://chatgpt.com/checkout/{processor_entity}/{session_id}",
        ),
    }
    try:
        response = chatgpt_http.post(
            "https://chatgpt.com/backend-api/payments/checkout/snapshot",
            json=payload,
            headers=headers,
            timeout=20,
        )
        log(f"[stripe] snapshot billing: {getattr(response, 'status_code', '?')}")
    except Exception as exc:
        log(f"[stripe] snapshot billing 异常（忽略）: {exc}")


def create_paypal_payment_method(http, pk: str, billing: dict, session_id: str, version: str, ctx: dict, log) -> str:
    guid, muid, sid = _gen_fingerprint()
    guid = ctx.get("guid") or guid
    muid = ctx.get("muid") or muid
    sid = ctx.get("sid") or sid
    ctx["guid"], ctx["muid"], ctx["sid"] = guid, muid, sid
    addr = billing.get("address", {}) or {}
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))
    elements_session_id = ctx.get("elements_session_id", _gen_elements_session_id())
    elements_session_config_id = ctx.get("elements_session_config_id") or str(uuid.uuid4())
    checkout_config_id = ctx.get("config_id") or ""
    runtime_version = ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION

    data = {
        "type": "paypal",
        "billing_details[name]": billing.get("name", ""),
        "billing_details[email]": billing.get("email", ""),
        "billing_details[address][country]": addr.get("country", "US"),
        "billing_details[address][line1]": addr.get("line1", ""),
        "billing_details[address][city]": addr.get("city", ""),
        "billing_details[address][postal_code]": addr.get("postal_code", ""),
        "billing_details[address][state]": addr.get("state", ""),
        "payment_user_agent": (
            f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
            "payment-element; deferred-intent"
        ),
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": session_id,
        "client_attribution_metadata[checkout_config_id]": checkout_config_id,
        "client_attribution_metadata[elements_session_id]": elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": elements_session_config_id,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": pk,
        "_stripe_version": STRIPE_VERSION_BASE,
    }
    resp = http.post(f"{STRIPE_API}/v1/payment_methods", data=data, headers=_stripe_headers(), timeout=30)
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"创建 PayPal payment_method 失败 [{getattr(resp, 'status_code', '?')}]: {(getattr(resp, 'text', '') or '')[:300]}")
    pm_id = resp.json()["id"]
    log(f"[stripe] paypal payment_method: {pm_id}")
    return pm_id


def _paypal_return_url(session_id: str, init_resp: dict, ctx: dict) -> str:
    """Build Stripe Custom Checkout's PayPal return URL."""
    hosted = str(
        ctx.get("stripe_hosted_url") or init_resp.get("stripe_hosted_url") or ""
    ).strip()
    if hosted.startswith("https://pay.openai.com"):
        hosted = "https://checkout.stripe.com" + hosted[len("https://pay.openai.com"):]
    if not hosted:
        hosted = f"https://checkout.stripe.com/c/pay/{session_id}"
    parsed = urlsplit(hosted)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["redirect_pm_type"] = "paypal"
    query["lid"] = str(uuid.uuid4())
    query["ui_mode"] = "custom"
    return urlunsplit(
        (
            parsed.scheme or "https",
            parsed.netloc or "checkout.stripe.com",
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


def confirm_payment(
    http,
    pk: str,
    session_id: str,
    pm_id: str,
    init_resp: dict,
    version: str,
    ctx: dict,
    profile: dict,
    log,
    *,
    expected_payment_method_type: str = "paypal",
) -> dict:
    guid, muid, sid = ctx.get("guid"), ctx.get("muid"), ctx.get("sid")
    if not (guid and muid and sid):
        guid, muid, sid = _gen_fingerprint()
    ctx["guid"], ctx["muid"], ctx["sid"] = guid, muid, sid
    runtime_version = ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION
    top_checkout_config_id = ctx.get("config_id", "")
    init_checksum = init_resp.get("init_checksum", "")

    total = init_resp.get("total_summary") or {}
    expected_amount = "0"
    if ctx.get("checkout_amount") is not None:
        expected_amount = str(ctx["checkout_amount"])
    elif total.get("due") is not None:
        expected_amount = str(total["due"])
    elif (init_resp.get("invoice") or {}).get("amount_due") is not None:
        expected_amount = str(init_resp["invoice"]["amount_due"])

    if expected_payment_method_type != "paypal" or not str(pm_id).startswith("pm_"):
        raise RuntimeError("PayPal compact confirm 缺少有效 pm_*")
    client_session_id = ctx.get("client_session_id") or str(uuid.uuid4())
    ctx["client_session_id"] = client_session_id
    data = {
        "eid": "NA",
        "payment_method": pm_id,
        "guid": guid, "muid": muid, "sid": sid,
        "expected_amount": expected_amount,
        "expected_payment_method_type": "paypal",
        "key": pk,
        "_stripe_version": PAYPAL_STRIPE_VERSION,
        "init_checksum": init_checksum,
        "version": runtime_version,
        "return_url": _paypal_return_url(session_id, init_resp, ctx),
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": session_id,
        "client_attribution_metadata[checkout_config_id]": top_checkout_config_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "link_brand": "link",
    }

    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/confirm"
    resp = http.post(url, data=data, headers=_stripe_headers(), timeout=30)
    if (
        getattr(resp, "status_code", 0) == 400
        and "consent[terms_of_service]" not in data
        and "terms of service" in (getattr(resp, "text", "") or "").lower()
    ):
        data["consent[terms_of_service]"] = "accepted"
        resp = http.post(url, data=data, headers=_stripe_headers(), timeout=30)
    if getattr(resp, "status_code", 0) != 200:
        if _is_payment_method_types_mismatch(resp):
            raise PayPalFundingUnavailableError(
                session_id,
                list(ctx.get("payment_method_types") or []),
                "confirm 返回 payment_method_types_mismatch",
            )
        raise RuntimeError(f"confirm 失败 [{getattr(resp, 'status_code', '?')}]: {(getattr(resp, 'text', '') or '')[:400]}")
    return resp.json()


def _find_next_action(confirm_data: dict) -> dict:
    na = confirm_data.get("next_action")
    if isinstance(na, dict) and na:
        return na
    for key in ("payment_intent", "setup_intent"):
        obj = confirm_data.get(key)
        if isinstance(obj, dict):
            na = obj.get("next_action")
            if isinstance(na, dict) and na:
                return na
    pmo = confirm_data.get("payment_method_object")
    if isinstance(pmo, dict):
        si = pmo.get("setup_intent")
        if isinstance(si, dict):
            na = si.get("next_action")
            if isinstance(na, dict) and na:
                return na
    return {}


def extract_redirect_url(confirm_data: dict) -> str:
    na = _find_next_action(confirm_data)
    rt = na.get("redirect_to_url") or {}
    url = str(rt.get("url") or "")
    if url:
        return url
    # 兜底：整个 confirm JSON 里正则找 PayPal/pm-redirects 跳转地址
    raw = json.dumps(confirm_data)
    m = re.search(r"https?://pm-redirects\.stripe\.com/authorize/[^\s\"'<>\\]+", raw)
    if m:
        return m.group(0).replace("\\u0026", "&").replace("\\/", "/")
    m = re.search(r"https?://(?:www\.)?paypal\.com/agreements/approve\?[^\s\"'<>\\]+", raw)
    if m:
        return m.group(0).replace("\\u0026", "&").replace("\\/", "/")
    return ""


def _find_submission_attempt(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        submission = payload.get("submission_attempt")
        if isinstance(submission, dict):
            return submission
        for value in payload.values():
            found = _find_submission_attempt(value)
            if found:
                return found
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found = _find_submission_attempt(value)
            if found:
                return found
    return {}


def _raise_for_current_paypal_risk_decline(
    payload: Any,
    payment_method_id: str,
) -> None:
    """Raise only when generic_decline belongs to this provider attempt's PM."""
    if isinstance(payload, dict):
        decline_code = str(payload.get("decline_code") or "").strip().lower()
        if decline_code == "generic_decline":
            error_payment_method = payload.get("payment_method")
            declined_pm_id = (
                str(error_payment_method.get("id") or "").strip()
                if isinstance(error_payment_method, dict)
                else ""
            )
            if not payment_method_id or not declined_pm_id or declined_pm_id == payment_method_id:
                raise PayPalRiskDeclinedError(
                    decline_code=decline_code,
                    error_code=str(payload.get("code") or ""),
                    payment_method_id=declined_pm_id or payment_method_id,
                )
        for value in payload.values():
            _raise_for_current_paypal_risk_decline(value, payment_method_id)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            _raise_for_current_paypal_risk_decline(value, payment_method_id)


def prepare_approve_sentinel_headers(
    chatgpt_http,
    session_id: str,
    processor_entity: str,
    log,
    *,
    device_id: str,
    sentinel_proxy: str,
    country: str,
) -> dict[str, str]:
    """Mint approval headers before Stripe confirm in the active ChatGPT session."""
    if not device_id or not sentinel_proxy:
        raise RuntimeError("approve Sentinel 缺少 device_id 或代理")
    _set_oai_device_cookie(chatgpt_http, device_id)
    checkout_page_url = f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"
    _warmup_chatgpt_page(
        chatgpt_http,
        page_url=checkout_page_url,
        country=country,
        device_id=device_id,
        log=log,
    )
    sentinel, sentinel_so = _mint_sentinel(
        "checkout_session_approval",
        SentinelCtx(
            device_id=device_id,
            user_agent=BROWSER_UA,
            proxy=sentinel_proxy,
            country=country,
            page_url=checkout_page_url,
            cookie_header=_session_cookie_header(chatgpt_http),
        ),
        log,
    )
    if not sentinel:
        raise RuntimeError("approve Sentinel 生成失败，未发送审批请求")
    headers = {
        "OpenAI-Sentinel-Token": sentinel,
        "OAI-Telemetry": "[1,null]",
    }
    if sentinel_so:
        headers["OpenAI-Sentinel-So-Token"] = sentinel_so
    try:
        payload = json.loads(sentinel)
    except ValueError:
        payload = {}
    log(
        "[sentinel] checkout_session_approval 已预取"
        f" (pow={len(str(payload.get('p') or ''))}, "
        f"turnstile={len(str(payload.get('t') or ''))}, so={bool(sentinel_so)})"
    )
    return headers


def approve_submission(
    chatgpt_http,
    access_token: str,
    session_id: str,
    processor_entity: str,
    log,
    *,
    device_id: str = "",
    sentinel_proxy: str = "",
    country: str = "US",
    prepared_headers: Optional[dict[str, str]] = None,
) -> dict:
    """manual_approval beta：调 ChatGPT /backend-api/payments/checkout/approve 批准提交。"""
    _set_oai_device_cookie(chatgpt_http, device_id)
    checkout_page_url = f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"
    if prepared_headers is None:
        prepared_headers = prepare_approve_sentinel_headers(
            chatgpt_http,
            session_id,
            processor_entity,
            log,
            device_id=device_id,
            sentinel_proxy=sentinel_proxy,
            country=country,
        )
    headers = {
        "content-type": "application/json",
        "accept": "*/*",
        "authorization": f"Bearer {access_token}",
        "origin": "https://chatgpt.com",
        "x-openai-target-path": "/backend-api/payments/checkout/approve",
        "x-openai-target-route": "/backend-api/payments/checkout/approve",
        **chatgpt_context_headers(
            country=country,
            device_id=device_id,
            referer=checkout_page_url,
        ),
    }
    headers.update(prepared_headers)
    body = {"checkout_session_id": session_id, "processor_entity": processor_entity}
    ar = chatgpt_http.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json=body, headers=headers, timeout=20,
    )
    sc_code = getattr(ar, "status_code", 0)
    text = getattr(ar, "text", "") or ""
    log(f"[stripe] manual_approval approve: {sc_code} {text[:160]}")
    if sc_code != 200:
        raise RuntimeError(f"ChatGPT approve 失败: {sc_code} {text[:200]}")
    try:
        payload = ar.json() or {}
    except Exception:
        payload = {}
    result = str(payload.get("result") or "").lower()
    if result not in {"approved", "blocked"}:
        raise RuntimeError(
            f"manual_approval approve 返回异常: result={result or 'empty'}"
        )
    return payload


def poll_redirect_after_approve(
    http,
    pk: str,
    session_id: str,
    log,
    max_attempts: int = 15,
    *,
    phase: str = "approve 后",
    payment_method_id: str = "",
) -> str:
    """GET /payment_pages/<id> 轮询，等待 next_action.redirect_to_url。"""
    params = {
        "key": pk,
        "_stripe_version": STRIPE_VERSION_FULL,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
    }
    for i in range(max_attempts):
        try:
            gr = http.get(f"{STRIPE_API}/v1/payment_pages/{session_id}", params=params, headers=_stripe_headers(), timeout=20)
        except Exception as e:
            log(f"[stripe] {phase} GET 异常: {e}")
            time.sleep(1)
            continue
        if getattr(gr, "status_code", 0) != 200:
            time.sleep(1)
            continue
        try:
            gj = gr.json()
        except Exception:
            time.sleep(1)
            continue
        url = extract_redirect_url(gj)
        if url:
            return url
        _raise_for_current_paypal_risk_decline(gj, payment_method_id)
        sa = (gj.get("submission_attempt") or {}).get("state")
        log(f"[stripe] {phase} poll {i + 1}: sub_state={sa}")
        time.sleep(1)
    return ""


def _processor_entity(
    confirm_data: dict,
    *,
    country: str = "US",
    checkout_entity: str = "",
) -> str:
    return (
        str(checkout_entity or "").strip()
        or _extract_processor_entity(confirm_data)
        or _default_processor_entity(country)
    )


def stripe_to_paypal_redirect(
    http,
    session_id: str,
    *,
    billing: dict,
    country: str = "US",
    processor_entity: str = "",
    publishable_key: str = "",
    require_zero_amount: bool = False,
    chatgpt_http=None,
    access_token: str = "",
    device_id: str = "",
    sentinel_proxy: str = "",
    apply_promo_callback: Optional[Callable[[str], Any]] = None,
    log: Callable[[str], None] = lambda m: None,
) -> tuple[str, str, dict]:
    """Run one Stripe PayPal submission without creating a second confirm."""
    profile = _profile(country)
    pk = str(publishable_key or "").strip() or verify_pk(http, session_id, log)
    init_data, version, ctx = init_checkout(http, session_id, pk, profile, log)
    pe = processor_entity or _default_processor_entity(country)
    ctx["processor_entity"] = pe

    def require_paypal(payload: dict, context: dict) -> None:
        methods = [str(value).lower() for value in context.get("payment_method_types") or []]
        explicit = isinstance(payload.get("payment_method_types"), list) or isinstance(
            payload.get("payment_method_specs"), list
        )
        if explicit and "paypal" not in methods:
            raise PayPalFundingUnavailableError(
                session_id,
                methods,
                "Stripe init 明确未提供 PayPal",
            )

    require_paypal(init_data, ctx)
    elements_data = fetch_elements_session(
        http, pk, session_id, ctx, version, profile, log
    )
    require_paypal(
        {"payment_method_specs": elements_data.get("payment_method_specs")}, ctx
    )

    is_zero, amount_detail, amount = verify_zero_amount_snapshot(init_data, ctx)
    if not is_zero and apply_promo_callback is not None:
        apply_promo_callback(pe)
        log("[stripe] 后置优惠已提交，等待 Stripe 金额同步")
        for sync_attempt in range(6):
            time.sleep(0.8 if sync_attempt == 0 else 1.5)
            init_data, version, ctx = init_checkout(
                http, session_id, pk, profile, log
            )
            ctx["processor_entity"] = pe
            require_paypal(init_data, ctx)
            is_zero, amount_detail, amount = verify_zero_amount_snapshot(
                init_data, ctx
            )
            if is_zero:
                break
        if is_zero:
            elements_data = fetch_elements_session(
                http, pk, session_id, ctx, version, profile, log
            )
            require_paypal(
                {"payment_method_specs": elements_data.get("payment_method_specs")},
                ctx,
            )
    if require_zero_amount and not is_zero:
        raise PromoNotAppliedError(
            session_id,
            amount if amount is not None else "unknown",
            str(ctx.get("currency") or ""),
            amount_detail,
        )

    ctx["billing"] = billing
    update_tax_region(http, session_id, pk, version, ctx, billing, profile, log)
    if require_zero_amount:
        updated_amount = _minor_amount(ctx.get("checkout_amount"))
        if updated_amount is None or updated_amount != 0:
            raise PromoNotAppliedError(
                session_id,
                updated_amount if updated_amount is not None else "unknown",
                str(ctx.get("currency") or ""),
                "tax_region 更新后金额不是 0",
            )
    snapshot_billing(
        chatgpt_http,
        access_token,
        session_id,
        pe,
        billing,
        log,
        device_id=device_id,
    )

    prepared_approve_headers: dict[str, str] = {}
    if (
        chatgpt_http is not None
        and access_token
        and device_id
        and sentinel_proxy
    ):
        prepared_approve_headers = prepare_approve_sentinel_headers(
            chatgpt_http,
            session_id,
            pe,
            log,
            device_id=device_id,
            sentinel_proxy=sentinel_proxy,
            country=country,
        )

    pm_id = create_paypal_payment_method(
        http, pk, billing, session_id, version, ctx, log
    )
    log("[stripe] PayPal confirm 使用已创建的 pm_*")
    confirm_data = confirm_payment(
        http, pk, session_id, pm_id, init_data, version, ctx, profile, log
    )
    log(
        "[stripe] confirm 完整响应（已脱敏）: "
        + json.dumps(
            sanitize_diagnostic_payload(confirm_data),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    _raise_for_current_paypal_risk_decline(confirm_data, pm_id)
    redirect_url = extract_redirect_url(confirm_data)

    if not redirect_url:
        sub = _find_submission_attempt(confirm_data)
        should_approve = str(sub.get("state") or "").lower() == "requires_approval"
        if should_approve and chatgpt_http is not None and access_token:
            approve_entity = _processor_entity(
                confirm_data,
                country=country,
                checkout_entity=processor_entity,
            )
            log(
                "[stripe] manual_approval（requires_approval）→ 调 ChatGPT approve"
                f"（processor_entity={approve_entity}）…"
            )
            approval = approve_submission(
                chatgpt_http,
                access_token,
                session_id,
                approve_entity,
                log,
                device_id=device_id,
                sentinel_proxy=sentinel_proxy,
                country=country,
                prepared_headers=prepared_approve_headers,
            )
            approval_result = str((approval or {}).get("result") or "").strip().lower()
            if approval_result == "blocked":
                log("[stripe] approve 返回 blocked，当前 Checkout 被拒")
                raise RuntimeError(
                    "manual_approval approve blocked: result=blocked"
                )
            log("[stripe] approve 已通过，只轮询原 submission")
            redirect_url = poll_redirect_after_approve(
                http,
                pk,
                session_id,
                log,
                max_attempts=5,
                phase="approve 后",
                payment_method_id=pm_id,
            )
        elif not should_approve:
            log("[stripe] confirm 已接受但暂无跳转，等待 Stripe 生成 PayPal redirect")
            redirect_url = poll_redirect_after_approve(
                http,
                pk,
                session_id,
                log,
                max_attempts=5,
                phase="confirm 后",
                payment_method_id=pm_id,
            )

    if not redirect_url:
        raise RuntimeError(f"confirm 未返回 PayPal 跳转地址: {json.dumps(confirm_data)[:300]}")
    log(f"[stripe] PayPal 跳转: {redirect_url[:120]}")
    return redirect_url, pk, ctx
