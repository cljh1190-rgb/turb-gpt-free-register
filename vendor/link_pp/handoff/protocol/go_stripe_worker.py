from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..security import sanitize_diagnostic_payload, sanitize_message
from . import stripe_checkout as stripe


DEFAULT_WORKER_PATH = str(Path(__file__).resolve().parents[2] / "bin" / "stripe-worker")
DEFAULT_WORKER_TIMEOUT = 190


class GoStripeWorkerUnavailableError(RuntimeError):
    pass


def _string_values(value: object) -> list[str]:
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _string_values(nested)]
    if isinstance(value, (list, tuple)):
        return [item for nested in value for item in _string_values(nested)]
    return [value] if isinstance(value, str) and value else []


def run_go_stripe_worker(
    *,
    session_id: str,
    publishable_key: str,
    proxy_url: str,
    access_token: str,
    cookie_header: str,
    device_id: str,
    country: str,
    currency: str,
    browser_locale: str,
    browser_timezone: str,
    processor_entity: str,
    checkout_url: str,
    billing: dict[str, Any],
    approve_headers: dict[str, str],
    apply_promo: bool,
    log: Callable[[str], None],
) -> str:
    binary = os.environ.get("STRIPE_GO_WORKER", DEFAULT_WORKER_PATH)
    payload = {
        "session_id": session_id,
        "publishable_key": publishable_key,
        "proxy_url": proxy_url,
        "access_token": access_token,
        "cookie_header": cookie_header,
        "device_id": device_id,
        "country": country,
        "browser_locale": browser_locale,
        "browser_timezone": browser_timezone,
        "processor_entity": processor_entity,
        "checkout_url": checkout_url,
        "billing": billing,
        "approve_headers": approve_headers,
        "apply_promo": apply_promo,
    }
    try:
        completed = subprocess.run(
            [binary],
            input=json.dumps(payload, ensure_ascii=False).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=DEFAULT_WORKER_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GoStripeWorkerUnavailableError(
            f"Go Stripe Worker 不存在: {binary}"
        ) from exc
    except PermissionError as exc:
        raise GoStripeWorkerUnavailableError(
            f"Go Stripe Worker 无法执行: {binary}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Go Stripe Worker 执行超时") from exc

    secrets = tuple(
        value
        for value in (
            cookie_header,
            proxy_url,
            billing.get("email"),
            billing.get("name"),
            *_string_values(billing.get("address")),
            *approve_headers.values(),
        )
        if isinstance(value, str) and value
    )
    stderr = sanitize_message(
        completed.stderr.decode(errors="replace")[:4000],
        access_token=access_token,
        secrets=secrets,
    )
    if completed.returncode != 0:
        detail = f": {stderr}" if stderr else ""
        raise RuntimeError(f"Go Stripe Worker 异常退出 {completed.returncode}{detail}")
    try:
        result = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        detail = f": {stderr}" if stderr else ""
        raise RuntimeError(f"Go Stripe Worker 返回无效 JSON{detail}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Go Stripe Worker 返回格式无效")

    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, list):
        for raw_record in diagnostics[:100]:
            if not isinstance(raw_record, dict):
                continue
            serialized = json.dumps(raw_record, ensure_ascii=False)
            serialized = sanitize_message(
                serialized,
                access_token=access_token,
                secrets=secrets,
                max_length=None,
            )
            record = sanitize_diagnostic_payload(json.loads(serialized))
            stripe._protocol_diagnostic(
                log,
                kind=str(record.get("kind") or "go_stripe")[:80],
                method=str(record.get("method") or ""),
                route=str(record.get("route") or ""),
                status=record.get("http_status") or 0,
                request_payload=record.get("request"),
                response_payload=record.get("response"),
                error=record.get("error") or "",
            )

    if result.get("ok") is True:
        redirect_url = str(result.get("redirect_url") or "").strip()
        if not redirect_url:
            raise RuntimeError("Go Stripe Worker 未返回 PayPal 跳转地址")
        return redirect_url

    code = str(result.get("code") or "go_stripe_failed")
    message = sanitize_message(
        result.get("message") or code,
        access_token=access_token,
        secrets=secrets,
    )
    if code in {"non_zero_amount", "promo_update_failed"}:
        raise stripe.PromoNotAppliedError(session_id, "non-zero", currency, message)
    if code == "paypal_unavailable":
        raise stripe.PayPalFundingUnavailableError(session_id, [], message)
    raise RuntimeError(f"Go Stripe Worker 失败 ({code}): {message}")
