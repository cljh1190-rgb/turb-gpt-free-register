# -*- coding: utf-8 -*-
"""Throwaway.io public temporary inbox client."""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp, looks_like_openai_email

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.throwaway.io/api/ai/v1"
DEFAULT_TIMEOUT = 20
DEFAULT_DOMAIN_CACHE_SECONDS = 300


class ThrowawayMailError(RuntimeError):
    """Throwaway.io API or inbox polling failure."""


@dataclass
class ThrowawayAccount:
    email: str
    domain: str
    expires_at: str = ""
    created_at: float = 0.0


_CONTEXT_CACHE: dict[str, ThrowawayAccount] = {}
_CONTEXT_LOCK = threading.RLock()
_DOMAIN_LOCK = threading.Lock()
_DOMAIN_CACHE: list[str] = []
_DOMAIN_CACHE_UNTIL = 0.0


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _cfg_str(name: str, default: str = "") -> str:
    return str(getattr(_email_cfg, name, default) or default).strip()


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(getattr(_email_cfg, name, default) or default)
    except (TypeError, ValueError):
        return default


def _base_url() -> str:
    return (_cfg_str("THROWAWAY_API_BASE", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")


def _timeout() -> int:
    return max(5, _cfg_int("THROWAWAY_REQUEST_TIMEOUT", DEFAULT_TIMEOUT))


def _request(method: str, path: str, *, json_body: dict | None = None) -> dict:
    route = "/" + str(path or "").lstrip("/")
    try:
        response = requests.request(
            method.upper(),
            _base_url() + route,
            json=json_body,
            headers={
                "Accept": "application/json",
                "User-Agent": "turb-gpt-free-register/throwaway-mail",
            },
            timeout=_timeout(),
        )
    except requests.RequestException as exc:
        raise ThrowawayMailError(
            f"Throwaway.io 请求失败 ({route}): {type(exc).__name__}: {exc}"
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise ThrowawayMailError(
            f"Throwaway.io 响应不是 JSON ({route}): HTTP {response.status_code}"
        ) from exc

    if response.status_code >= 400 or not isinstance(payload, dict) or payload.get("status") is not True:
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error") or "")
        raise ThrowawayMailError(
            f"Throwaway.io 请求失败 ({route}): HTTP {response.status_code}; "
            f"{message or str(payload)[:200]}"
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ThrowawayMailError(f"Throwaway.io 响应缺少 data 对象 ({route})")
    return data


def _normalize_domains(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        domain = str(value or "").strip().lower().lstrip("@")
        if domain and "." in domain and " " not in domain and domain not in seen:
            seen.add(domain)
            out.append(domain)
    return out


def list_domains(*, force_refresh: bool = False) -> list[str]:
    """Return a cached snapshot of currently available public domains."""
    global _DOMAIN_CACHE, _DOMAIN_CACHE_UNTIL
    now = time.monotonic()
    with _DOMAIN_LOCK:
        if not force_refresh and _DOMAIN_CACHE and now < _DOMAIN_CACHE_UNTIL:
            return list(_DOMAIN_CACHE)
        data = _request("GET", "/domains")
        domains = _normalize_domains(data.get("domains"))
        if not domains:
            raise ThrowawayMailError("Throwaway.io 域名接口没有返回可用域名")
        cache_seconds = max(
            30,
            _cfg_int("THROWAWAY_DOMAIN_CACHE_SECONDS", DEFAULT_DOMAIN_CACHE_SECONDS),
        )
        _DOMAIN_CACHE = domains
        _DOMAIN_CACHE_UNTIL = time.monotonic() + cache_seconds
        logger.info("[Throwaway] 已刷新域名列表: %s 个", len(domains))
        return list(domains)


def create_address(domain: str | None = None) -> ThrowawayAccount:
    """Create a public ten-minute inbox on a random current domain."""
    selected = str(domain or "").strip().lower().lstrip("@")
    if not selected:
        selected = secrets.choice(list_domains())
    data = _request("POST", "/addresses", json_body={"domain": selected})
    email = str(data.get("email") or "").strip().lower()
    actual_domain = str(data.get("domain") or selected).strip().lower()
    if not email or "@" not in email:
        raise ThrowawayMailError("Throwaway.io 创建邮箱响应缺少有效 email")
    if email.rsplit("@", 1)[-1] != actual_domain:
        actual_domain = email.rsplit("@", 1)[-1]
    return ThrowawayAccount(
        email=email,
        domain=actual_domain,
        expires_at=str(data.get("expires_at") or ""),
        created_at=time.time(),
    )


def pick_account() -> ThrowawayAccount:
    """Create and cache an independently addressable inbox for one worker."""
    account = create_address()
    with _CONTEXT_LOCK:
        _CONTEXT_CACHE[_cache_key(account.email)] = account
    logger.info("[Throwaway] 已创建临时邮箱: %s (domain=%s)", account.email, account.domain)
    return account


def get_account_context(email: str) -> ThrowawayAccount | None:
    with _CONTEXT_LOCK:
        return _CONTEXT_CACHE.get(_cache_key(email))


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    with _CONTEXT_LOCK:
        _CONTEXT_CACHE.pop(_cache_key(email), None)
    logger.info(
        "[Throwaway] 已释放临时邮箱: %s（status=%s, note=%s）",
        email,
        status,
        note or "",
    )


def _message_timestamp(item: dict) -> float | None:
    for key in ("received_at", "created_at", "timestamp", "date", "time"):
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        if isinstance(raw, (int, float)):
            value = float(raw)
            return value / 1000.0 if value > 1e12 else value
        text = str(raw).strip()
        if not text:
            continue
        try:
            if text.replace(".", "", 1).isdigit():
                value = float(text)
                return value / 1000.0 if value > 1e12 else value
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError):
            continue
    return None


def _otp_item(item: dict) -> dict:
    body = item.get("body") or item.get("text") or item.get("content") or ""
    return {
        "id": item.get("id"),
        "from": item.get("from_email") or item.get("from") or "",
        "fromName": item.get("from") or "",
        "subject": item.get("subject") or "",
        "text": body,
        "html": item.get("html") or body,
    }


def list_messages(email: str) -> list[dict]:
    target = str(email or "").strip().lower()
    if not target or "@" not in target:
        raise ThrowawayMailError("Throwaway.io 收件箱地址无效")
    data = _request("GET", f"/addresses/{quote(target, safe='')}/messages")
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise ThrowawayMailError("Throwaway.io 收件箱响应缺少 messages 数组")
    return [item for item in messages if isinstance(item, dict)]


def get_message(message_id: str) -> dict:
    value = str(message_id or "").strip()
    if not value:
        raise ThrowawayMailError("Throwaway.io 邮件 ID 为空")
    return _request("GET", f"/messages/{quote(value, safe='')}")


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """Poll one Throwaway.io inbox and return its latest OpenAI OTP."""
    target = str(email or "").strip().lower()
    if not target:
        raise ThrowawayMailError("Throwaway.io 取码缺少邮箱地址")

    wait_seconds = int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT)
    interval = max(1, int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL))
    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    deadline = time.monotonic() + max(0, wait_seconds)
    best_otp: str | None = None
    best_timestamp = float("-inf")
    best_message_id = ""
    settle_until: float | None = None
    last_error = "收件箱为空或尚未出现新的 OpenAI 验证码"

    logger.info("[Throwaway] 开始轮询邮箱 %s，最长 %ss", target, wait_seconds)
    while time.monotonic() <= deadline:
        try:
            messages = sorted(
                list_messages(target),
                key=lambda item: _message_timestamp(item) or float("-inf"),
                reverse=True,
            )
            for summary in messages:
                message_time = _message_timestamp(summary)
                if after_ts is not None and message_time is not None and message_time < after_ts - 30:
                    continue
                message_id = str(summary.get("id") or "").strip()
                if not message_id:
                    continue
                detail = get_message(message_id)
                detail_time = _message_timestamp(detail)
                effective_time = detail_time if detail_time is not None else message_time
                if after_ts is not None and effective_time is not None and effective_time < after_ts - 30:
                    continue
                item = _otp_item({**summary, **detail})
                if not looks_like_openai_email(item):
                    continue
                otp = extract_otp(item)
                if not otp:
                    continue
                candidate_time = float("-inf") if effective_time is None else effective_time
                if (
                    best_otp is None
                    or candidate_time > best_timestamp
                    or (candidate_time == best_timestamp and message_id != best_message_id and otp != best_otp)
                ):
                    best_otp = otp
                    best_timestamp = candidate_time
                    best_message_id = message_id
                    settle_until = time.monotonic() + settle
                    logger.info("[Throwaway] 锁定 OTP 候选，等待 %ss 确认", settle)

            now = time.monotonic()
            if best_otp and settle_until is not None and now >= settle_until:
                return best_otp
        except ThrowawayMailError as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    if best_otp:
        return best_otp
    raise ThrowawayMailError(f"等待 Throwaway.io 验证码超时: {target}; {last_error}")
