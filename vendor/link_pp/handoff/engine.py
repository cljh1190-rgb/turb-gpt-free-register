from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

from .countries import CountryProfile
from .gateway import CheckoutArtifact, ProviderResult, new_device_id
from .proxies import ProxyPool
from .protocol.go_stripe_worker import GoStripeWorkerUnavailableError
from .protocol.stripe_checkout import (
    ChatGPTAuthError,
    CheckoutPreflightError,
    OaicsConfirmBlockedError,
    PayPalFundingUnavailableError,
    PayPalRiskDeclinedError,
)
from .security import TokenProfile, mask_identifier, sanitize_message


DEFAULT_CHECKOUT_ATTEMPTS = 5
DEFAULT_PROVIDER_ATTEMPTS = 10


class CancelledError(RuntimeError):
    pass


class FlowExhaustedError(RuntimeError):
    pass


class Gateway(Protocol):
    def create_checkout(self, **kwargs) -> CheckoutArtifact: ...

    def attempt_provider(self, **kwargs) -> ProviderResult: ...


EmitFn = Callable[[str, str, str], None]


@dataclass(slots=True)
class RunSpec:
    access_token: str
    token_profile: TokenProfile
    proxy_country: CountryProfile
    checkout_country: CountryProfile
    proxies: ProxyPool
    checkout_attempts: int = DEFAULT_CHECKOUT_ATTEMPTS
    provider_attempts: int = DEFAULT_PROVIDER_ATTEMPTS
    stripe_checkout: bool = False
    stripe_engine: str = "python"
    stripe_promo_strategy: str = "post_update"
    device_id: str = ""
    proxy_offset: int = 0

    def clear_secrets(self) -> None:
        self.access_token = ""


@dataclass(frozen=True, slots=True)
class FlowResult:
    session_id: str
    checkout_url: str
    provider_redirect_url: str
    paypal_approve_url: str
    ba_token: str
    proxy_country: str
    country: str
    currency: str
    checkout_attempt: int
    provider_attempt: int
    def to_dict(self) -> dict:
        return asdict(self)


def positive_attempts(raw: object, *, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("重试次数必须是整数") from exc
    if value < 1:
        raise ValueError("重试次数至少为 1")
    return value


def _is_auth_error(exc: BaseException) -> bool:
    if isinstance(exc, ChatGPTAuthError):
        return True
    text = str(exc).lower()
    return (
        ("http 401" in text or "approve 失败: 401" in text)
        and ("chatgpt" in text or "checkout" in text or "approve" in text)
    )


def _requires_new_checkout(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            OaicsConfirmBlockedError,
            PayPalFundingUnavailableError,
            PayPalRiskDeclinedError,
        ),
    ):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "免费促销未实际生效",
            "stripe due=",
            "generic_decline",
            "manual_approval approve blocked",
            "result=blocked",
            "checkout_not_active_session",
            "checkout session is no longer active",
            "oaics paypal confirm blocked",
        )
    )


def _is_terminal_approval_block(exc: BaseException) -> bool:
    """Approval blocks are account/upstream decisions, not retryable transport errors."""
    text = str(exc).lower()
    return "manual_approval approve blocked" in text or "result=blocked" in text


def _short_reason(exc: BaseException, access_token: str) -> str:
    text = sanitize_message(exc, access_token=access_token)
    lower = text.lower()
    if "checkout_not_active_session" in lower or "checkout session is no longer active" in lower:
        return "Checkout 已失效"
    if "普通 stripe checkout" in lower or "oaics_ 链" in lower:
        return "上游返回普通 Stripe Checkout，未生成 oaics_ 链"
    if "未生成当前流程需要的 cs_live_ 链" in lower:
        return "上游返回 OAICS Checkout，未生成 cs_live_ 链"
    if (
        "manual_approval approve blocked" in lower
        or "result=blocked" in lower
        or "oaics paypal confirm blocked" in lower
    ):
        return "审批被拒绝"
    if "paypal 风控拒绝" in lower or "generic_decline" in lower:
        return "风控拒绝（generic_decline）"
    if "resource_missing" in lower and "publishable_key" in lower:
        return "Stripe 分片 key 不匹配"
    if "payment_method_types_mismatch" in lower or "不支持 paypal" in lower:
        return "未开放 PayPal"
    if (
        "免费促销未实际生效" in text
        or "stripe due=" in lower
        or "checkout due=" in lower
    ):
        if re.search(r"session=cs_(?:live|test)_", lower):
            return "已生成 Stripe Checkout，但前置优惠未生效（应付金额非 0）"
        return "已生成 OAICS，但前置优惠未生效（应付金额非 0）"
    if "ba 链接" in lower or "ba_token" in lower:
        return "没有解析到 BA 链接"
    if "confirm 未返回 paypal 跳转地址" in lower:
        return "未取得 PayPal 跳转地址"
    if "bad_decrypt" in lower:
        return "代理 TLS 解密异常（BAD_DECRYPT）"
    if "general socks server failure" in lower:
        return "代理无法连接 Stripe（SOCKS server failure）"
    if "connection closed abruptly" in lower or re.search(r"curl\s*:\s*\(56\)", lower):
        return "代理连接被上游中断（curl 56）"
    network_markers = (
        "timeout",
        "timed out",
        "connection error",
        "connectionerror",
        "connection reset",
        "connection refused",
        "failed to connect",
        "proxy error",
        "proxyerror",
        "proxy connect",
        "tls error",
        "tls handshake",
        "ssl error",
        "curl:",
    )
    if isinstance(exc, (TimeoutError, ConnectionError)) or any(
        marker in lower for marker in network_markers
    ):
        return "代理连接失败"
    if _is_auth_error(exc):
        return "AT 已失效或无权限"
    return text[:100] or type(exc).__name__


class HandoffEngine:
    def __init__(self, gateway: Gateway):
        self.gateway = gateway

    def run(
        self,
        spec: RunSpec,
        *,
        emit: EmitFn,
        is_cancelled: Callable[[], bool],
    ) -> FlowResult:
        token = spec.access_token
        fixed_device_id = spec.device_id
        billing = spec.checkout_country.billing_dict(
            name=spec.token_profile.name,
            email=spec.token_profile.email,
        )
        last_error: BaseException | None = None
        last_provider_error: BaseException | None = None
        active_artifact: CheckoutArtifact | None = None

        def close_active_artifact() -> None:
            nonlocal active_artifact
            if active_artifact is not None:
                active_artifact.close_transport()
                active_artifact = None

        def check_cancelled() -> None:
            if is_cancelled():
                close_active_artifact()
                raise CancelledError("任务已停止")

        checkout_attempt = 0
        candidate_sequence = 0
        consecutive_preflight_failures = 0
        preflight_scan_limit = max(len(spec.proxies), 1)

        while checkout_attempt < spec.checkout_attempts:
            check_cancelled()
            close_active_artifact()
            candidate_sequence += 1
            device_id = fixed_device_id or new_device_id()
            checkout_proxy = spec.proxies.pick(candidate_sequence + spec.proxy_offset)
            displayed_attempt = checkout_attempt + 1
            emit(
                "info",
                "checkout",
                f"生成 Checkout {displayed_attempt}/{spec.checkout_attempts}"
                f"（{spec.proxy_country.code} 出口 · "
                f"{spec.checkout_country.code}/{spec.checkout_country.currency} 账单）",
            )
            try:
                artifact = self.gateway.create_checkout(
                    access_token=token,
                    proxy_country=spec.proxy_country,
                    checkout_country=spec.checkout_country,
                    billing=billing,
                    proxy=checkout_proxy,
                    device_id=device_id,
                    stripe_checkout=spec.stripe_checkout,
                    stripe_engine=spec.stripe_engine,
                    stripe_promo_strategy=spec.stripe_promo_strategy,
                    checkout_attempt=displayed_attempt,
                    log=lambda message: emit(
                        "info",
                        "checkout",
                        sanitize_message(
                            message,
                            access_token=token,
                            max_length=None,
                        ),
                    ),
                )
                active_artifact = artifact
            except CancelledError:
                raise
            except CheckoutPreflightError as exc:
                last_error = exc
                consecutive_preflight_failures += 1
                emit(
                    "warn",
                    "checkout",
                    f"预检失败：{_short_reason(exc, token)}；未消耗 Checkout 次数，换下一组代理",
                )
                if consecutive_preflight_failures >= preflight_scan_limit:
                    emit("error", "checkout", "候选代理已完成一轮预检，未找到可用链路")
                    break
                continue
            except Exception as exc:
                checkout_attempt += 1
                last_error = exc
                reason = _short_reason(exc, token)
                emit("error" if _is_auth_error(exc) else "warn", "checkout", f"生成失败：{reason}")
                if _is_auth_error(exc):
                    raise RuntimeError(reason) from exc
                continue

            checkout_attempt += 1
            consecutive_preflight_failures = 0
            if artifact.session_id.startswith(("cs_live_", "cs_test_")):
                checkout_status = "Stripe Checkout 已生成"
            else:
                checkout_status = "0 元已确认"
            emit(
                "success",
                "checkout",
                f"{checkout_status} · {mask_identifier(artifact.session_id)}",
            )

            for provider_attempt in range(1, spec.provider_attempts + 1):
                check_cancelled()
                provider_proxy = (
                    checkout_proxy
                    if provider_attempt == 1
                    else spec.proxies.pick(
                        candidate_sequence + provider_attempt - 1 + spec.proxy_offset
                    )
                )
                emit(
                    "info",
                    "extract",
                    f"提取 PayPal 链接 {provider_attempt}/{spec.provider_attempts} · "
                    f"Checkout {checkout_attempt}/{spec.checkout_attempts}",
                )
                try:
                    result = self.gateway.attempt_provider(
                        artifact=artifact,
                        access_token=token,
                        proxy_country=spec.proxy_country,
                        checkout_country=spec.checkout_country,
                        billing=billing,
                        proxy=provider_proxy,
                        device_id=device_id,
                        check_cancelled=check_cancelled,
                        log=lambda message: emit(
                            "info",
                            "extract",
                            sanitize_message(
                                message,
                                access_token=token,
                                max_length=None,
                            ),
                        ),
                    )
                    if not result.paypal_approve_url or not result.ba_token:
                        raise RuntimeError("未能从 Checkout 跳转解析 PayPal BA 链接")
                except CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    if isinstance(exc, GoStripeWorkerUnavailableError):
                        reason = _short_reason(exc, token)
                        last_provider_error = exc
                        emit("error", "extract", reason)
                        close_active_artifact()
                        raise RuntimeError(reason) from exc
                    raw_error = str(exc).lower()
                    is_transport_noise = (
                        _short_reason(exc, token) in {"代理连接失败", "代理连接被上游中断（curl 56）"}
                        or any(
                            marker in raw_error
                            for marker in ("bad_decrypt", "connection closed abruptly")
                        )
                        or bool(re.search(r"curl\s*:\s*\(56\)", raw_error))
                    )
                    if not is_transport_noise:
                        last_provider_error = exc
                    reason = _short_reason(exc, token)
                    if _is_auth_error(exc):
                        close_active_artifact()
                        raise RuntimeError(reason) from exc
                    if _is_terminal_approval_block(exc):
                        emit("error", "extract", "上游支付审批拒绝，停止当前 AT 的重复 Checkout")
                        close_active_artifact()
                        raise RuntimeError(reason) from exc
                    if _requires_new_checkout(exc):
                        emit("warn", "extract", f"{reason}，当前 Checkout 不再重试")
                        break
                    if provider_attempt < spec.provider_attempts:
                        emit("warn", "extract", f"{reason}，更换代理出口")
                    else:
                        emit("warn", "extract", f"{reason}，本轮代理出口已用完")
                    continue

                emit("success", "extract", "已取得 PayPal BA 链接")
                close_active_artifact()
                return FlowResult(
                    session_id=artifact.session_id,
                    checkout_url=artifact.checkout_url,
                    provider_redirect_url=result.provider_redirect_url,
                    paypal_approve_url=result.paypal_approve_url,
                    ba_token=result.ba_token,
                    proxy_country=spec.proxy_country.code,
                    country=spec.checkout_country.code,
                    currency=artifact.currency,
                    checkout_attempt=checkout_attempt,
                    provider_attempt=provider_attempt,
                )

            if checkout_attempt < spec.checkout_attempts:
                emit("info", "checkout", "未取得 PayPal 链接，重新生成 Checkout")

        final_error = last_provider_error or last_error
        reason = _short_reason(final_error, token) if final_error else "没有可用结果"
        close_active_artifact()
        raise FlowExhaustedError(
            f"已完成 {checkout_attempt} 次 Checkout，仍未取得 PayPal 链接：{reason}"
        )
