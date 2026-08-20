# -*- coding: utf-8 -*-
"""GoneBox (gonebox.email) 一次性临时邮箱 client。

鉴权：GoneBox REST API 目前对匿名调用开放（无需 X-API-Key 即可创建/收信），
但官方文档声明付费套餐才提供 API 访问；实践中匿名创建收件箱返回 201。
若配置了 GONEBOX_API_KEY 则自动附带 X-API-Key 请求头（用于更高配额）。

端点（base = GONEBOX_API_BASE，默认 https://api.gonebox.email/api/v1）：
    POST /inboxes                          → 创建收件箱
    GET  /inboxes/{address}/messages       → 列出消息（按 address 定位，不是 id）
    GET  /messages/{message_id}            → 取单封邮件全文
    DELETE /inboxes/{address}              → 删除收件箱
响应统一包裹为 {"success": true, "data": {...}}。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp, looks_like_openai_email

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.gonebox.email/api/v1"
DEFAULT_TIMEOUT = 20
DEFAULT_DOMAIN_CACHE_SECONDS = 300

# GoneBox 固定提供的三个收件域名（无公开域名列表接口）。
KNOWN_DOMAINS = ("gonebox.email", "sumiu.email", "nemexiste.email")


class GoneboxMailError(RuntimeError):
    """GoneBox API 或收件箱轮询失败。"""


@dataclass
class GoneboxAccount:
    email: str
    domain: str
    inbox_id: str = ""
    expires_at: str = ""
    created_at: float = 0.0


_CONTEXT_CACHE: dict[str, GoneboxAccount] = {}
_CONTEXT_LOCK = threading.RLock()
_ACTIVE_EMAILS: set[str] = set()
_ISSUED_EMAILS: set[str] = set()
_CREATE_RETRIES = 5
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
    return (_cfg_str("GONEBOX_API_BASE", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")


def _timeout() -> int:
    return max(5, _cfg_int("GONEBOX_REQUEST_TIMEOUT", DEFAULT_TIMEOUT))


def _format_expires(raw) -> str:
    """把 expiresAt（秒级 epoch）归一化成 ISO 字符串，兼容其他格式。"""
    if raw is None or raw == "":
        return ""
    if isinstance(raw, (int, float)) or (
        isinstance(raw, str) and str(raw).strip().isdigit()
    ):
        try:
            value = float(raw)
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        except (OverflowError, ValueError, OSError):
            return str(raw)
    return str(raw)


def _request(method: str, path: str, *, json_body: dict | None = None) -> dict:
    route = "/" + str(path or "").lstrip("/")
    headers = {
        "Accept": "application/json",
        "User-Agent": "turb-gpt-free-register/gonebox-mail",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    api_key = _cfg_str("GONEBOX_API_KEY", "")
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        response = requests.request(
            method.upper(),
            _base_url() + route,
            json=json_body,
            headers=headers,
            timeout=_timeout(),
        )
    except requests.RequestException as exc:
        raise GoneboxMailError(
            f"GoneBox 请求失败 ({route}): {type(exc).__name__}: {exc}"
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise GoneboxMailError(
            f"GoneBox 响应不是 JSON ({route}): HTTP {response.status_code}"
        ) from exc

    if (
        response.status_code >= 400
        or not isinstance(payload, dict)
        or payload.get("success") is not True
    ):
        message = ""
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                message = str(err.get("message") or err.get("code") or "")
            message = message or str(payload.get("message") or payload.get("error") or "")
        raise GoneboxMailError(
            f"GoneBox 请求失败 ({route}): HTTP {response.status_code}; "
            f"{message or str(payload)[:200]}"
        )

    data = payload.get("data")
    if not isinstance(data, dict):
        raise GoneboxMailError(f"GoneBox 响应缺少 data 对象 ({route})")
    return data


def list_domains(*, force_refresh: bool = False) -> list[str]:
    """返回可用收件域名快照（GoneBox 域名为固定三项，无远程接口，本地缓存）。"""
    global _DOMAIN_CACHE, _DOMAIN_CACHE_UNTIL
    default_domain = _cfg_str("GONEBOX_DEFAULT_DOMAIN", "") or KNOWN_DOMAINS[0]
    domains = [default_domain] + [d for d in KNOWN_DOMAINS if d != default_domain]
    now = time.monotonic()
    with _DOMAIN_LOCK:
        if not force_refresh and _DOMAIN_CACHE and now < _DOMAIN_CACHE_UNTIL:
            return list(_DOMAIN_CACHE)
        cache_seconds = max(
            30,
            _cfg_int("GONEBOX_DOMAIN_CACHE_SECONDS", DEFAULT_DOMAIN_CACHE_SECONDS),
        )
        _DOMAIN_CACHE = domains
        _DOMAIN_CACHE_UNTIL = time.monotonic() + cache_seconds
        logger.info("[GoneBox] 域名列表: %s", ", ".join(domains))
        return list(domains)


def create_address(domain: str | None = None) -> GoneboxAccount:
    """创建一次性收件箱，默认使用 GONEBOX_DEFAULT_DOMAIN（或 gonebox.email）。"""
    selected = str(domain or "").strip().lower().lstrip("@")
    if not selected:
        selected = _cfg_str("GONEBOX_DEFAULT_DOMAIN", "") or KNOWN_DOMAINS[0]
    data = _request("POST", "/inboxes", json_body={"domain": selected})
    # The API returns existing=true when it reuses an address. Reusing an
    # inbox is unsafe for concurrent registrations because OTPs can cross.
    if data.get("existing") is True:
        raise GoneboxMailError("GoneBox 服务端复用了已有邮箱")
    email = str(data.get("address") or data.get("email") or "").strip().lower()
    actual_domain = str(data.get("domain") or selected).strip().lower().lstrip("@")
    if not email or "@" not in email:
        raise GoneboxMailError("GoneBox 创建邮箱响应缺少有效 address")
    if email.rsplit("@", 1)[-1] != actual_domain:
        actual_domain = email.rsplit("@", 1)[-1]
    return GoneboxAccount(
        email=email,
        domain=actual_domain,
        inbox_id=str(data.get("id") or ""),
        expires_at=_format_expires(data.get("expiresAt")),
        created_at=time.time(),
    )


def pick_account() -> GoneboxAccount:
    """创建并缓存一个可独立寻址的收件箱。"""
    last_error: Exception | None = None
    for attempt in range(1, _CREATE_RETRIES + 1):
        try:
            account = create_address()
            key = _cache_key(account.email)
            if not key or "@" not in key:
                raise GoneboxMailError("GoneBox 返回了无效邮箱地址")
            with _CONTEXT_LOCK:
                if key in _ISSUED_EMAILS or key in _ACTIVE_EMAILS or key in _CONTEXT_CACHE:
                    raise GoneboxMailError(f"GoneBox 返回重复邮箱: {account.email}")
                _ACTIVE_EMAILS.add(key)
                _ISSUED_EMAILS.add(key)
                _CONTEXT_CACHE[key] = account
            logger.info("[GoneBox] 已创建临时邮箱: %s (domain=%s)", account.email, account.domain)
            return account
        except Exception as exc:
            last_error = exc
            logger.warning("[GoneBox] 创建邮箱失败，第 %s/%s 次: %s", attempt, _CREATE_RETRIES, exc)
    raise GoneboxMailError(f"GoneBox 创建独立邮箱失败: {last_error}")


def get_account_context(email: str) -> GoneboxAccount | None:
    with _CONTEXT_LOCK:
        return _CONTEXT_CACHE.get(_cache_key(email))


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    with _CONTEXT_LOCK:
        key = _cache_key(email)
        _CONTEXT_CACHE.pop(key, None)
        _ACTIVE_EMAILS.discard(key)
    logger.info(
        "[GoneBox] 已释放临时邮箱: %s（status=%s, note=%s）",
        email,
        status,
        note or "",
    )


def _message_timestamp(item: dict) -> float | None:
    for key in ("received_at", "receivedAt", "created_at", "timestamp", "date", "time"):
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
    # GoneBox uses camelCase fields in real message details (bodyText/bodyHtml).
    # Keep snake_case and legacy aliases for mocked/older API responses.
    body = (
        item.get("bodyText")
        or item.get("body_text")
        or item.get("textBody")
        or item.get("text")
        or ""
    )
    html = (
        item.get("bodyHtml")
        or item.get("body_html")
        or item.get("htmlBody")
        or item.get("html")
        or body
    )
    sender = item.get("from_address") or item.get("from") or item.get("sender") or ""
    return {
        "id": item.get("id"),
        "from": sender,
        "fromName": sender,
        "subject": item.get("subject") or "",
        "text": body,
        "html": html,
    }


def list_messages(email: str) -> list[dict]:
    target = str(email or "").strip().lower()
    if not target or "@" not in target:
        raise GoneboxMailError("GoneBox 收件箱地址无效")
    data = _request("GET", f"/inboxes/{quote(target, safe='')}/messages")
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise GoneboxMailError("GoneBox 收件箱响应缺少 messages 数组")
    return [item for item in messages if isinstance(item, dict)]


def get_message(message_id: str) -> dict:
    value = str(message_id or "").strip()
    if not value:
        raise GoneboxMailError("GoneBox 邮件 ID 为空")
    return _request("GET", f"/messages/{quote(value, safe='')}")


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """轮询一个 GoneBox 收件箱，返回最新 OpenAI OTP（6 位数字）。"""
    target = str(email or "").strip().lower()
    if not target:
        raise GoneboxMailError("GoneBox 取码缺少邮箱地址")

    wait_seconds = int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT)
    interval = max(1, int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL))
    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    deadline = time.monotonic() + max(0, wait_seconds)
    best_otp: str | None = None
    best_timestamp = float("-inf")
    best_message_id = ""
    settle_until: float | None = None
    last_error = "收件箱为空或尚未出现新的 OpenAI 验证码"

    logger.info("[GoneBox] 开始轮询邮箱 %s，最长 %ss", target, wait_seconds)
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
                    logger.info("[GoneBox] 锁定 OTP 候选，等待 %ss 确认", settle)

            now = time.monotonic()
            if best_otp and settle_until is not None and now >= settle_until:
                return best_otp
        except GoneboxMailError as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    if best_otp:
        return best_otp
    raise GoneboxMailError(f"等待 GoneBox 验证码超时: {target}; {last_error}")
