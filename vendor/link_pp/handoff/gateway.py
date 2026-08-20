from __future__ import annotations

import html
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlsplit

from .countries import CountryProfile, install_protocol_profiles
from .protocol import stripe_checkout as stripe
from .protocol.go_stripe_worker import run_go_stripe_worker
from .proxies import ProxyEndpoint


install_protocol_profiles(stripe)

LogFn = Callable[[str], None]
_PAYPAL_URL_RE = re.compile(
    r"https?://(?:www\.)?paypal\.com/agreements/approve\?[^\s<>\"']+",
    re.IGNORECASE,
)
_BA_TOKEN_RE = re.compile(r"\bBA-[A-Z0-9-]+\b", re.IGNORECASE)


@dataclass(slots=True)
class CheckoutTransport:
    http: Any
    proxy_url: str
    claimed: bool = False

    def claim(self, proxy_url: str):
        if self.claimed or str(proxy_url or "") != self.proxy_url:
            return None
        self.claimed = True
        return self.http

    def close(self) -> None:
        self.claimed = True
        try:
            self.http.close()
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class CheckoutArtifact:
    session_id: str
    processor_entity: str
    country: str
    currency: str
    checkout_url: str
    amount: int | None = None
    stripe_engine: str = "python"
    promo_on_create: bool = True
    publishable_key: str = field(default="", repr=False)
    transport: CheckoutTransport | None = field(default=None, repr=False, compare=False)

    def close_transport(self) -> None:
        if self.transport is not None:
            self.transport.close()


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider_redirect_url: str
    paypal_approve_url: str
    ba_token: str


def new_device_id() -> str:
    return str(uuid.uuid4())


def preflight_checkout_route(
    *,
    http,
    proxy_country: str,
    access_token: str,
    device_id: str,
    log: LogFn,
    require_stripe: bool = False,
    renew_http: Callable[[Any], Any] | None = None,
) -> None:
    last_error: stripe.CheckoutPreflightError | None = None
    with stripe._PREFLIGHT_SEMAPHORE:
        for attempt in range(1, stripe.PREFLIGHT_RETRY_ATTEMPTS + 1):
            try:
                checkout_info = stripe.verify_proxy_exit_country(
                    http,
                    proxy_country,
                    timeout=stripe.PREFLIGHT_HTTP_TIMEOUT_SECONDS,
                )
                log(f"Checkout 出口预检通过：{stripe.proxy_exit_log_label(checkout_info)}")
                if require_stripe:
                    stripe_status = stripe.verify_stripe_route(http, log=log)
                    log(f"Stripe 出口预检通过：HTTP {stripe_status}")
                # verify_chatgpt_account warms the page in this same session
                # before making the protected /me request.
                stripe.verify_chatgpt_account(
                    http,
                    access_token,
                    country=proxy_country,
                    device_id=device_id,
                    log=log,
                )
                log("ChatGPT /me 账号与连接预检通过")
                return
            except stripe.CheckoutPreflightError as exc:
                last_error = exc
                if attempt >= stripe.PREFLIGHT_RETRY_ATTEMPTS:
                    raise
                log(
                    f"预检瞬时失败（{exc.code}），同代理重试 "
                    f"{attempt + 1}/{stripe.PREFLIGHT_RETRY_ATTEMPTS}"
                )
                # A curl 56/connection failure leaves the pool unusable.  A new
                # session also gives a challenged route a clean TLS connection;
                # cookies are copied by renew_http_session.
                if renew_http is not None:
                    try:
                        http = renew_http(http)
                        log("预检已重建同代理 HTTP 会话")
                    except Exception as renew_exc:
                        log(f"预检会话重建失败（继续重试）: {type(renew_exc).__name__}: {renew_exc}")
                time.sleep(stripe.PREFLIGHT_RETRY_DELAY_SECONDS)
    if last_error is not None:
        raise last_error


def _ba_from_url(url: str) -> str:
    try:
        token = (parse_qs(urlsplit(url).query).get("ba_token") or [""])[0]
    except Exception:
        token = ""
    if token:
        return token
    match = _BA_TOKEN_RE.search(url)
    return match.group(0) if match else ""


def resolve_paypal_approval_url(
    http,
    redirect_url: str,
    *,
    max_hops: int = 6,
    log: LogFn = lambda _message: None,
) -> tuple[str, str]:
    current = html.unescape(str(redirect_url or "").strip())
    if not current:
        raise RuntimeError("Checkout 未返回 PayPal 跳转地址")

    for _hop in range(max(1, max_hops) + 1):
        if "paypal.com/agreements/approve" in current.lower():
            token = _ba_from_url(current)
            if token:
                stripe._protocol_diagnostic(
                    log,
                    kind="paypal_redirect",
                    method="GET",
                    route="paypal/agreements/approve",
                    request_payload={"hop": _hop, "url": current},
                    response_payload={"ba_token_found": True},
                )
                return current, token
        response = http.get(
            current,
            allow_redirects=False,
            headers={
                "User-Agent": stripe.BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=30,
        )
        location = str((getattr(response, "headers", {}) or {}).get("location") or "")
        body = html.unescape(str(getattr(response, "text", "") or ""))
        match = _PAYPAL_URL_RE.search(body)
        stripe._protocol_diagnostic(
            log,
            kind="paypal_redirect",
            method="GET",
            route=urlsplit(current).path or "/",
            status=getattr(response, "status_code", 0),
            request_payload={"hop": _hop, "url": current},
            response_payload={
                "location": location,
                "paypal_url_in_body": bool(match),
                "ba_token_in_body": bool(_BA_TOKEN_RE.search(body)),
            },
            response=response,
        )
        if match:
            approval = match.group(0).replace("\\u0026", "&").replace("\\/", "/")
            token = _ba_from_url(approval)
            if token:
                return approval, token
        token_match = _BA_TOKEN_RE.search(body)
        if token_match and "paypal" in body.lower():
            token = token_match.group(0)
            return f"https://www.paypal.com/agreements/approve?ba_token={token}", token
        if not location:
            break
        current = urljoin(current, html.unescape(location))

    raise RuntimeError("未能从 Checkout 跳转解析 PayPal BA 链接")


class LiveProtocolGateway:
    def create_checkout(
        self,
        *,
        access_token: str,
        proxy_country: CountryProfile,
        checkout_country: CountryProfile,
        billing: dict,
        proxy: ProxyEndpoint,
        device_id: str,
        stripe_checkout: bool = False,
        stripe_engine: str = "python",
        stripe_promo_strategy: str = "post_update",
        checkout_attempt: int = 1,
        log: LogFn,
    ) -> CheckoutArtifact:
        http = stripe.build_http(proxy.url)
        keep_checkout_http = False
        context: dict[str, str] = {}

        def renew_checkout_http(current):
            nonlocal http
            http = stripe.renew_http_session(current, proxy.url)
            return http

        try:
            preflight_checkout_route(
                http=http,
                proxy_country=proxy_country.code,
                access_token=access_token,
                device_id=device_id,
                log=log,
                require_stripe=stripe_checkout,
                renew_http=renew_checkout_http,
            )
            promo_on_create = True
            if stripe_checkout:
                promo_on_create = stripe_promo_strategy == "upfront" or (
                    stripe_promo_strategy == "mixed" and checkout_attempt % 2 == 1
                )
            session_id, error = stripe.create_chatgpt_order_with_retry(
                http,
                access_token,
                country=checkout_country.code,
                currency=checkout_country.currency,
                device_id=device_id,
                sentinel_proxy=proxy.url,
                checkout_context=context,
                with_promo=promo_on_create,
                hosted_checkout=stripe_checkout,
                max_attempts=3,
                renew_http=renew_checkout_http,
                log=log,
            )
            if not session_id:
                raise RuntimeError(f"创建 Checkout 失败: {error or '没有 session id'}")
            if stripe_checkout:
                if not session_id.startswith(("cs_live_", "cs_test_")):
                    raise stripe.StripeCheckoutRequiredError(session_id)
                processor_entity = (
                    context.get("processor_entity") or checkout_country.processor_entity
                )
                publishable_key = context.get("publishable_key") or stripe.verify_pk(
                    http,
                    session_id,
                    log,
                )
                canonical_checkout_url = (
                    f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"
                )
                response_checkout_url = context.get("checkout_url") or ""
                checkout_url = (
                    response_checkout_url
                    if session_id in response_checkout_url
                    else canonical_checkout_url
                )
                promo_label = "前置优惠" if promo_on_create else "后置 update 优惠"
                log(
                    f"Stripe 提链引擎：{'Go' if stripe_engine == 'go' else 'Python'}；"
                    f"本轮策略：{promo_label}"
                )
                transport = CheckoutTransport(http=http, proxy_url=proxy.url)
                keep_checkout_http = True
                return CheckoutArtifact(
                    session_id=session_id,
                    processor_entity=processor_entity,
                    country=checkout_country.code,
                    currency=checkout_country.currency,
                    checkout_url=checkout_url,
                    amount=None,
                    stripe_engine=stripe_engine,
                    promo_on_create=promo_on_create,
                    publishable_key=publishable_key,
                    transport=transport,
                )
            if not session_id.startswith("oaics_"):
                raise stripe.OaicsCheckoutRequiredError(session_id)
            processor_entity = (
                context.get("processor_entity") or checkout_country.processor_entity
            )
            state = stripe.fetch_oaics_checkout_session(
                http,
                access_token,
                session_id,
                processor_entity,
                country=checkout_country.code,
                device_id=device_id,
                log=log,
            )
            tax_state = stripe.submit_oaics_checkout_taxes(
                http,
                access_token,
                session_id,
                processor_entity,
                billing=billing,
                country=checkout_country.code,
                currency=checkout_country.currency,
                device_id=device_id,
                log=log,
            )
            state = stripe.wait_for_oaics_zero(
                http,
                access_token,
                session_id,
                processor_entity,
                country=checkout_country.code,
                currency=checkout_country.currency,
                device_id=device_id,
                initial_payload=tax_state or state,
                log=log,
            )
            detected_currency = stripe.oaics_checkout_currency(state)
            if detected_currency and detected_currency != checkout_country.currency:
                raise RuntimeError(
                    "OAICS 币种与账单国家不一致: "
                    f"expected={checkout_country.currency}, actual={detected_currency}"
                )
            canonical_checkout_url = (
                f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"
            )
            response_checkout_url = context.get("checkout_url") or ""
            checkout_url = (
                response_checkout_url
                if session_id in response_checkout_url
                else canonical_checkout_url
            )
            transport = CheckoutTransport(http=http, proxy_url=proxy.url)
            keep_checkout_http = True
            return CheckoutArtifact(
                session_id=session_id,
                processor_entity=processor_entity,
                country=checkout_country.code,
                currency=detected_currency or checkout_country.currency,
                checkout_url=checkout_url,
                amount=0,
                stripe_engine="python",
                promo_on_create=True,
                transport=transport,
            )
        finally:
            if not keep_checkout_http:
                try:
                    http.close()
                except Exception:
                    pass

    def attempt_provider(
        self,
        *,
        artifact: CheckoutArtifact,
        access_token: str,
        proxy_country: CountryProfile,
        checkout_country: CountryProfile,
        billing: dict,
        proxy: ProxyEndpoint,
        device_id: str,
        check_cancelled: Callable[[], None] = lambda: None,
        log: LogFn,
    ) -> ProviderResult:
        http = artifact.transport.claim(proxy.url) if artifact.transport is not None else None
        reused_checkout_transport = http is not None
        if http is None:
            http = stripe.build_http(proxy.url)

        def renew_provider_http(current):
            nonlocal http
            http = stripe.renew_http_session(current, proxy.url)
            return http

        try:
            check_cancelled()
            if not reused_checkout_transport:
                preflight_checkout_route(
                    http=http,
                    proxy_country=proxy_country.code,
                    access_token=access_token,
                    device_id=device_id,
                    log=log,
                    require_stripe=artifact.session_id.startswith(("cs_live_", "cs_test_")),
                    renew_http=renew_provider_http,
                )
            else:
                log("首次提链复用 Checkout 会话")
            if artifact.session_id.startswith(("cs_live_", "cs_test_")):
                if artifact.stripe_engine == "go":
                    log("开始 Go Stripe 严格 0 元校验与 PayPal 提链")
                    approve_headers = stripe.prepare_approve_sentinel_headers(
                        http,
                        artifact.session_id,
                        artifact.processor_entity,
                        log,
                        device_id=device_id,
                        sentinel_proxy=proxy.url,
                        country=checkout_country.code,
                    )
                    redirect_url = run_go_stripe_worker(
                        session_id=artifact.session_id,
                        publishable_key=artifact.publishable_key,
                        proxy_url=proxy.url,
                        access_token=access_token,
                        cookie_header=stripe._session_cookie_header(http),
                        device_id=device_id,
                        country=checkout_country.code,
                        currency=artifact.currency,
                        browser_locale=checkout_country.locale,
                        browser_timezone=checkout_country.timezone,
                        processor_entity=artifact.processor_entity,
                        checkout_url=artifact.checkout_url,
                        billing=billing,
                        approve_headers=approve_headers,
                        apply_promo=not artifact.promo_on_create,
                        log=log,
                    )
                else:
                    log("开始 Python Stripe 严格 0 元校验与 PayPal 提链")
                    apply_promo_callback = None
                    if not artifact.promo_on_create:
                        apply_promo_callback = lambda processor_entity: stripe.update_checkout_promo(
                            http,
                            access_token,
                            artifact.session_id,
                            processor_entity,
                            device_id=device_id,
                            country=checkout_country.code,
                            log=lambda raw: log(raw.removeprefix("[stripe] ")),
                        )
                    redirect_url, _publishable_key, _context = stripe.stripe_to_paypal_redirect(
                        http,
                        artifact.session_id,
                        billing=billing,
                        country=checkout_country.code,
                        processor_entity=artifact.processor_entity,
                        publishable_key=artifact.publishable_key,
                        require_zero_amount=True,
                        chatgpt_http=http,
                        access_token=access_token,
                        device_id=device_id,
                        sentinel_proxy=proxy.url,
                        apply_promo_callback=apply_promo_callback,
                        log=lambda raw: log(raw.removeprefix("[stripe] ")),
                    )
            else:
                redirect_url, _context = stripe.oaics_to_paypal_redirect(
                    http,
                    artifact.session_id,
                    access_token=access_token,
                    processor_entity=artifact.processor_entity,
                    billing=billing,
                    country=checkout_country.code,
                    currency=artifact.currency,
                    device_id=device_id,
                    sentinel_proxy=proxy.url,
                    log=lambda raw: log(raw.removeprefix("[oaics] ")),
                )
            check_cancelled()
            approval_url, ba_token = resolve_paypal_approval_url(
                http,
                redirect_url,
                log=log,
            )
            return ProviderResult(
                provider_redirect_url=redirect_url,
                paypal_approve_url=approval_url,
                ba_token=ba_token,
            )
        finally:
            try:
                http.close()
            except Exception:
                pass
