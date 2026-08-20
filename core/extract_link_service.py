# -*- coding: utf-8 -*-
"""Plus 试用提链后台队列。

BurstPro UPI 官方契约（文档页 https://upi.burstpro-ai.online/cdk-api/ ）:
  BASE URL = https://upi.burstpro-ai.online   ← 注意：不含 /cdk-api
  公开端点均在 /api/* ：
    GET  /api/health
    POST /api/check-cdk          body: {cdk}
    POST /api/activate           body: {cdk, access_token, email?}  → HTTP 202 + task_id/read_token
    GET  /api/tasks/{task_id}    header: X-Task-Token: read_token
    POST /api/tasks/{id}/events-ticket
    GET  /api/tasks/{id}/events?ticket=...
    POST /api/tasks/{id}/regenerate
    POST /api/qr

/cdk-api/ 只是静态文档站，不是 API 前缀。若用户把 BASE 配成 .../cdk-api 会自动剥掉。
另兼容旧版 legacy：GET /api/cdk + POST /api/extract + SSE /api/jobs/{id}/events。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

from config import extract_link as cfg
from core import db

logger = logging.getLogger(__name__)


def _runtime_setting(name: str, default=None):
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    raw = os.getenv(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return getattr(cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def extraction_enabled() -> bool:
    """提链总开关；运行时读取 .env，关闭后绝不再向远端创建任务。"""
    value = str(_runtime_setting("EXTRACT_LINK_ENABLED", "False") or "").strip().lower()
    return value in {"1", "true", "yes", "on", "y"}


SUPPORTED_LINK_TYPES = {"pix", "upi", "kakao_pay", "ideal", "paypal"}

# BurstPro 官方错误码（文档 ERROR REFERENCE）
_BURSTPRO_ERROR_HINTS = {
    "access_token_invalid": "Access Token / Session JSON 格式不正确",
    "cdk_invalid": "CDK 不存在/无效",
    "cdk_unavailable": "CDK 停用、到期或无可用次数",
    "task_not_found": "任务不存在，或 X-Task-Token 不正确",
    "active_task": "原任务仍在排队/执行，不能重新生成",
    "account_no_discount": "该账号已确认无优惠，请换号",
    "credential_expired": "30 分钟重新生成凭证已失效",
    "rate_limited": "请求过于频繁",
    "queue_full": "远端队列已满",
    "service_unavailable": "UPI/代理/队列暂时未就绪",
    "queue_failed": "远端任务队列故障",
    "upi_generation_failed": "远端 UPI 生成失败（账号/优惠/上游问题，非本机路径错误）",
}


def _link_type(value: str | None = None) -> str:
    t = str(value or _runtime_setting("EXTRACT_LINK_TYPE", "pix") or "pix").strip().lower()
    if t not in SUPPORTED_LINK_TYPES:
        raise ValueError("提链类型无效，仅支持 pix / upi / kakao_pay / ideal / paypal")
    return t


def _normalize_api_base(raw: str) -> str:
    """规范化 BASE。官方文档页是 /cdk-api/，真正 API 在根域 /api/*。"""
    base = str(raw or "").strip().rstrip("/")
    if not base:
        return base
    # 用户常把文档 URL 当成 API BASE，这里自动纠正
    lower = base.lower()
    for suffix in ("/cdk-api", "/cdk-api/", "/docs", "/api-docs", "/swagger"):
        if lower.endswith(suffix.rstrip("/")) or lower.endswith(suffix):
            # strip once
            cut = len(suffix.rstrip("/"))
            base = base[: -cut].rstrip("/")
            lower = base.lower()
            break
    # 若误写成 .../api 作为 base，也剥掉（我们会自己拼 /api/xxx）
    if lower.endswith("/api"):
        base = base[:-4].rstrip("/")
    return base


def _api_base() -> str:
    base = _normalize_api_base(_runtime_setting("EXTRACT_LINK_API_BASE", "") or "")
    if not base:
        raise ValueError(
            "EXTRACT_LINK_API_BASE 为空。"
            "BurstPro 填 https://upi.burstpro-ai.online；"
            "Link Atelier 填 https://www.1k50.xyz/extract"
        )
    return base


def _cdk(value: str | None = None) -> str:
    cdk = str(value or _runtime_setting("EXTRACT_LINK_CDK", "") or "").strip()
    if not cdk:
        raise ValueError("EXTRACT_LINK_CDK/CDK 为空")
    return cdk


def _is_burstpro(base: str | None = None) -> bool:
    """BurstPro 系：/api/check-cdk + /api/activate + /api/tasks/{id}。"""
    flag = str(_runtime_setting("EXTRACT_LINK_PROVIDER", "") or "").strip().lower()
    if flag in ("burstpro", "burst", "upi-burstpro", "upi"):
        return True
    if flag in ("legacy", "classic", "old"):
        return False
    b = (base or _api_base()).lower()
    host = urlparse(b if "://" in b else f"https://{b}").netloc.lower()
    return (
        ("burstpro" in b)
        or ("burstpro" in host)
        or host.endswith("burstpro-ai.online")
        or host.startswith("upi.")
        or ("upi." in b and "burst" in b)
    )


def _is_workbench(base: str | None = None) -> bool:
    """Link Atelier 工作台 provider（https://www.1k50.xyz/extract/）。"""
    flag = str(_runtime_setting("EXTRACT_LINK_PROVIDER", "auto") or "").strip().lower()
    if flag in {"workbench", "link_atelier", "1k50", "extract_workbench"}:
        return True
    if flag in {"linkpp", "link-pp", "paypal", "burstpro", "burst", "upi-burstpro", "upi", "legacy", "classic", "old"}:
        return False
    b = (base or _runtime_setting("EXTRACT_LINK_API_BASE", "") or "").lower()
    return "1k50.xyz" in b or "linkatelier" in b


def _is_linkpp(base: str | None = None) -> bool:
    """eatWhitePorridge/link-pp PayPal 提链服务。"""
    flag = str(_runtime_setting("EXTRACT_LINK_PROVIDER", "auto") or "").strip().lower()
    if flag in {"linkpp", "link-pp", "paypal"}:
        return True
    if flag not in {"", "auto"}:
        return False
    b = (base or _runtime_setting("EXTRACT_LINK_API_BASE", "") or "").lower()
    host = urlparse(b if "://" in b else f"http://{b}").netloc.lower()
    return "link-pp" in b or host in {"127.0.0.1:5572", "localhost:5572", "127.0.0.1:5000", "localhost:5000"}


def _provider(base: str | None = None) -> str:
    """Return the active provider name without making a network request."""
    flag = str(_runtime_setting("EXTRACT_LINK_PROVIDER", "auto") or "").strip().lower()
    if flag in {"workbench", "link_atelier", "1k50", "extract_workbench"}:
        return "workbench"
    if _is_linkpp(base):
        return "linkpp"
    if _is_burstpro(base):
        return "burstpro"
    return "legacy"


def _linkpp_proxy_pool(country: str) -> list[str]:
    """为 link-pp 生成目标国家的新 Cliproxy 会话，失败时回退配置代理池。"""
    count = max(
        1,
        min(
            20,
            _int_setting("EXTRACT_LINK_LINKPP_CHECKOUT_ATTEMPTS", 3, 1, 20)
            + _int_setting("EXTRACT_LINK_LINKPP_PROVIDER_ATTEMPTS", 5, 1, 20),
        ),
    )
    generated: list[str] = []
    try:
        from config import proxy as proxy_cfg
        if bool(getattr(proxy_cfg, "cliproxy_pool_enabled", lambda: False)()):
            for _ in range(count):
                value = str(proxy_cfg.new_cliproxy_country_session(country) or "").strip()
                if value and value not in generated:
                    generated.append(value)
    except Exception as exc:
        logger.warning("[link-pp] 生成 %s Cliproxy 会话失败: %s", country, exc)
    if generated:
        return generated
    return _workbench_proxy_pool("EXTRACT_LINK_CHECKOUT_PROXY_POOL")


def _normalize_workbench_proxy(value: str | None) -> str:
    """Match the external UI's host:port:user:password -> HTTP URL conversion."""
    raw = str(value or "").strip()
    if not raw or raw.startswith("#"):
        return ""
    if "://" in raw:
        return raw
    parts = raw.split(":", 3)
    if len(parts) >= 4 and parts[1].isdigit():
        host, port, username, password = parts
        return (
            f"http://{quote(username, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}"
        )
    if len(parts) == 2 and parts[1].isdigit():
        return f"http://{raw}"
    return raw


def _workbench_proxy_pool(name: str) -> list[str]:
    raw = _runtime_setting(name, []) or []
    if isinstance(raw, str):
        values = raw.splitlines()
    else:
        values = list(raw)
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        proxy = _normalize_workbench_proxy(value)
        if proxy and proxy not in seen:
            seen.add(proxy)
            out.append(proxy)
        if len(out) >= 500:
            break
    return out


_WORKBENCH_PROXY_CURSOR_LOCK = threading.Lock()
_WORKBENCH_PROXY_CURSORS = {"checkout": 0, "update": 0}


def _next_workbench_proxy(kind: str, pool: list[str]) -> str:
    if not pool:
        return ""
    cursor_key = "update" if kind == "update" else "checkout"
    with _WORKBENCH_PROXY_CURSOR_LOCK:
        cursor = _WORKBENCH_PROXY_CURSORS[cursor_key]
        _WORKBENCH_PROXY_CURSORS[cursor_key] = (cursor + 1) % len(pool)
    return pool[cursor % len(pool)]


def _workbench_retryable_error(exc: Exception) -> bool:
    message = str(exc or "").strip().lower()
    return any(marker in message for marker in (
        "unusual activity",
        "checkout create failed",
        "task not found",
        "proxy",
        "network",
        "connection",
        "timeout",
        "timed out",
        "failed to perform",
        "curl:",
    ))


def _workbench_window_id() -> str:
    configured = str(_runtime_setting("EXTRACT_LINK_WORKBENCH_WINDOW_ID", "") or "").strip()
    return configured or f"local-{uuid.uuid4().hex}"


def _normalize_access_token(token: str) -> str:
    """官方接受 JWT / Bearer Token / Session JSON。去掉多余 Bearer 前缀。"""
    t = str(token or "").strip()
    if not t:
        return t
    # 已是 Session JSON
    if t.startswith("{") and t.endswith("}"):
        return t
    low = t.lower()
    if low.startswith("bearer "):
        t = t[7:].strip()
    return t


_WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="extract-link")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def _session():
    if curl_requests is None:
        return None
    return curl_requests.Session()


def _fmt_remote_error(payload: dict | None, status: int | None = None) -> str:
    data = payload if isinstance(payload, dict) else {}
    code = str(data.get("error") or data.get("code") or data.get("reason") or "").strip()
    msg = str(data.get("message") or data.get("detail") or data.get("msg") or "").strip()
    hint = _BURSTPRO_ERROR_HINTS.get(code) or _BURSTPRO_ERROR_HINTS.get(code.lower())
    parts = []
    if code:
        parts.append(code)
    if msg and msg != code:
        parts.append(msg)
    if hint:
        parts.append(hint)
    if not parts and status:
        parts.append(f"HTTP {status}")
    if not parts:
        parts.append(json.dumps(data, ensure_ascii=False)[:300] if data else "unknown error")
    return " | ".join(parts)[:500]


def _http_json(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    headers: dict | None = None,
    timeout: int = 30,
    retries: int = 0,
    retry_on_timeout: bool = True,
):
    """发 JSON 请求。对连接超时/瞬时网络错误可重试（不重试 4xx 业务错误）。"""
    h = {"Accept": "application/json", "User-Agent": "turb-gpt-free-register/extract-link"}
    if headers:
        h.update(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        h["Content-Type"] = "application/json"

    attempts = max(1, int(retries) + 1)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        s = _session()
        try:
            if s is None:
                req = Request(url, data=body, headers=h, method=method.upper())
                with urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace") or "{}"
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = {"raw": raw[:500]}
                    return int(getattr(resp, "status", 200) or 200), data if isinstance(data, dict) else {"data": data}
            resp = s.request(
                method.upper(),
                url,
                json=payload if payload is not None else None,
                headers=h,
                timeout=timeout,
            )
            try:
                data = resp.json()
            except Exception:
                data = {"raw": (resp.text or "")[:500]}
            if not isinstance(data, dict):
                data = {"data": data}
            return int(resp.status_code), data
        except Exception as exc:
            last_exc = exc
            name = type(exc).__name__
            msg = str(exc)
            is_timeout = ("Timeout" in name) or ("timeout" in msg.lower()) or ("timed out" in msg.lower()) or ("curl: (28)" in msg)
            is_conn = is_timeout or ("Connection" in name) or ("curl: (7)" in msg) or ("curl: (35)" in msg)
            if (not retry_on_timeout) or (not is_conn) or attempt >= attempts - 1:
                raise
            wait = min(8.0, 1.5 * (attempt + 1))
            logger.warning(
                "[提链] HTTP %s %s 第%d/%d次失败(%s)，%.1fs 后重试",
                method.upper(), url, attempt + 1, attempts, name, wait,
            )
            time.sleep(wait)
        finally:
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
    if last_exc:
        raise last_exc
    raise RuntimeError(f"HTTP {method} {url} 失败")


def query_cdk(*, cdk: str | None = None) -> dict:
    base = _api_base()
    if _is_linkpp(base):
        health = health_check()
        return {
            "ok": bool(health.get("ok", True)),
            "provider": "linkpp",
            "api_base": base,
            "cdk_required": False,
            "message": "link-pp 本地服务不需要 CDK",
            **health,
        }
    if _is_workbench(base):
        # Link Atelier does not use CDK; keep the existing WebUI endpoint
        # useful by returning provider health/configuration instead of asking
        # the workbench for a BurstPro-only code.
        health = health_check()
        return {
            "ok": bool(health.get("ok", True)),
            "provider": "workbench",
            "api_base": base,
            "cdk_required": False,
            "message": "Link Atelier 工作台不需要 CDK",
            **health,
        }
    code = _cdk(cdk)
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)

    if _is_burstpro(base):
        # 官方: POST /api/check-cdk  {cdk}  不扣次
        status, payload = _http_json(
            "POST",
            f"{base}/api/check-cdk",
            payload={"cdk": code},
            timeout=timeout,
            retries=2,
        )
        if status < 200 or status >= 300:
            raise RuntimeError(_fmt_remote_error(payload, status))
        out = dict(payload)
        if "remaining" in payload and "cdk_remaining" not in out:
            out["cdk_remaining"] = payload.get("remaining")
        if "valid" in payload and "ok" not in out:
            out["ok"] = bool(payload.get("valid"))
        out["api_base"] = base
        out["docs_url"] = f"{base}/cdk-api/"
        out["provider"] = "burstpro"
        return out

    # legacy: GET /api/cdk?code=
    status, payload = _http_json("GET", f"{base}/api/cdk?{urlencode({'code': code})}", timeout=timeout, retries=1)
    if status < 200 or status >= 300:
        raise RuntimeError(payload.get("error") or payload.get("message") or f"HTTP {status}")
    return payload


def health_check() -> dict:
    """探测 BurstPro /api/health；非 BurstPro 返回 provider 信息。"""
    base = _api_base()
    if _is_linkpp(base):
        timeout = min(15, _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300))
        status, payload = _http_json("GET", f"{base}/api/meta", timeout=timeout, retries=1)
        if status < 200 or status >= 300:
            raise RuntimeError(_fmt_remote_error(payload, status))
        return {
            "ok": True,
            "provider": "linkpp",
            "api_base": base,
            "cdk_required": False,
            "service": "eatWhitePorridge/link-pp",
            "meta": payload,
        }
    if _is_workbench(base):
        timeout = min(15, _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300))
        status, payload = _http_json("GET", f"{base}/api/health", timeout=timeout, retries=1)
        if status < 200 or status >= 300:
            raise RuntimeError(_fmt_remote_error(payload, status))
        out = dict(payload) if isinstance(payload, dict) else {"raw": payload}
        out.update({"api_base": base, "provider": "workbench", "cdk_required": False})
        return out
    if not _is_burstpro(base):
        return {"ok": True, "provider": "legacy", "api_base": base}
    timeout = min(15, _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300))
    status, payload = _http_json("GET", f"{base}/api/health", timeout=timeout, retries=1)
    if status < 200 or status >= 300:
        raise RuntimeError(_fmt_remote_error(payload, status))
    out = dict(payload) if isinstance(payload, dict) else {"raw": payload}
    out["api_base"] = base
    out["docs_url"] = f"{base}/cdk-api/"
    out["provider"] = "burstpro"
    return out


def _create_extract_job(*, token: str, link_type: str, cdk: str, email: str = "") -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)

    if _is_linkpp(base):
        country = str(_runtime_setting("EXTRACT_LINK_LINKPP_COUNTRY", "GB") or "GB").strip().upper()
        billing_country = str(
            _runtime_setting("EXTRACT_LINK_LINKPP_BILLING_COUNTRY", "GB") or "GB"
        ).strip().upper()
        proxies = _linkpp_proxy_pool(country)
        if not proxies:
            raise RuntimeError(f"link-pp 未找到可用的 {country} 代理池")
        stripe_checkout = str(
            _runtime_setting("EXTRACT_LINK_LINKPP_STRIPE_CHECKOUT", "True") or "True"
        ).strip().lower() in {"1", "true", "yes", "on", "y"}
        payload = {
            "access_token": _normalize_access_token(token),
            "country": country,
            "billing_country": billing_country,
            "proxies": proxies,
            "proxy_scheme": "socks5h",
            "checkout_attempts": _int_setting("EXTRACT_LINK_LINKPP_CHECKOUT_ATTEMPTS", 3, 1, 20),
            "provider_attempts": _int_setting("EXTRACT_LINK_LINKPP_PROVIDER_ATTEMPTS", 5, 1, 20),
            "stripe_checkout": stripe_checkout,
            "stripe_engine": str(
                _runtime_setting("EXTRACT_LINK_LINKPP_STRIPE_ENGINE", "go") or "go"
            ).strip().lower(),
            "stripe_promo_strategy": str(
                _runtime_setting("EXTRACT_LINK_LINKPP_STRIPE_PROMO_STRATEGY", "mixed")
                or "mixed"
            ).strip().lower(),
        }
        status, data = _http_json(
            "POST", f"{base}/api/jobs", payload=payload, timeout=timeout, retries=1
        )
        if status < 200 or status >= 300:
            raise RuntimeError(_fmt_remote_error(data, status))
        job_id = str(data.get("job_id") or data.get("id") or "")
        if not job_id:
            raise RuntimeError(f"link-pp 未返回 job_id: {data}")
        return {
            "provider": "linkpp",
            "job_id": job_id,
            "status": data.get("status") or "queued",
            "http_status": status,
            "api_base": base,
            "country": country,
            "billing_country": billing_country,
        }

    if _is_workbench(base):
        checkout_pool = _workbench_proxy_pool("EXTRACT_LINK_CHECKOUT_PROXY_POOL")
        update_pool = _workbench_proxy_pool("EXTRACT_LINK_UPDATE_PROXY_POOL")
        apply_update = str(
            _runtime_setting("EXTRACT_LINK_WORKBENCH_APPLY_UPDATE", "True") or "True"
        ).strip().lower() in {"1", "true", "yes", "on", "y"}
        if not checkout_pool:
            raise RuntimeError("Link Atelier 未配置 Checkout 代理池")
        if apply_update and not update_pool:
            raise RuntimeError("Link Atelier 已开启 Checkout Update，但未配置 Update 代理池")

        visitor_id = _workbench_window_id()
        payload = {
            "access_token": _normalize_access_token(token),
            "checkout_proxy": _next_workbench_proxy("checkout", checkout_pool),
            "update_proxy": _next_workbench_proxy("update", update_pool) if update_pool else "",
            "checkout_proxy_pool": checkout_pool,
            "update_proxy_pool": update_pool,
            "apply_checkout_update": apply_update,
            "oaics_only": str(
                _runtime_setting("EXTRACT_LINK_WORKBENCH_OAICS_ONLY", "False") or "False"
            ).strip().lower() in {"1", "true", "yes", "on", "y"},
            "window_id": visitor_id,
            "window_concurrency": 1,
        }
        country = str(_runtime_setting("EXTRACT_LINK_WORKBENCH_COUNTRY", "") or "").strip()
        payment_method = str(
            _runtime_setting("EXTRACT_LINK_WORKBENCH_PAYMENT_METHOD", "") or ""
        ).strip()
        if country:
            payload["country"] = country
        if payment_method:
            payload["payment_method"] = payment_method
        if email:
            payload["account_email"] = email

        status, data = _http_json(
            "POST",
            f"{base}/api/tasks",
            payload=payload,
            headers={"X-Workbench-Visitor": visitor_id},
            timeout=timeout,
            retries=1,
        )
        if status < 200 or status >= 300:
            raise RuntimeError(_fmt_remote_error(data, status))
        task_id = str(data.get("task_id") or data.get("job_id") or data.get("id") or "")
        if not task_id:
            raise RuntimeError(f"Link Atelier 未返回 task_id: {data}")
        return {
            "provider": "workbench",
            "job_id": task_id,
            "task_id": task_id,
            "status": data.get("status") or "queued",
            "http_status": status,
            "api_base": base,
            "visitor_id": visitor_id,
            "raw": data,
            "payload": payload,
        }

    if _is_burstpro(base):
        # 官方: POST /api/activate → 202 + task_id/read_token（原子锁定一次 CDK）
        access_token = _normalize_access_token(token)
        if not access_token:
            raise RuntimeError("access_token 为空")
        payload = {
            "cdk": _cdk(cdk),
            "access_token": access_token,
            "email": email or "",
        }
        status, data = _http_json(
            "POST",
            f"{base}/api/activate",
            payload=payload,
            timeout=timeout,
            retries=2,  # 连接超时常见，业务 4xx 不会重试（由 raise 前判断）
        )
        # 官方成功是 202；2xx 都接受
        if status < 200 or status >= 300:
            raise RuntimeError(_fmt_remote_error(data, status))
        if data.get("ok") is False:
            raise RuntimeError(_fmt_remote_error(data, status))
        task_id = str(data.get("task_id") or data.get("job_id") or "")
        read_token = str(data.get("read_token") or "")
        if not task_id or not read_token:
            raise RuntimeError(f"BurstPro 未返回 task_id/read_token: {data}")
        cdk_info = data.get("cdk") if isinstance(data.get("cdk"), dict) else {}
        # 把 read_token 写进账号记录，便于超时后续查 / 重新生成
        try:
            if email:
                acc = db.get_account_by_email(email) or {}
                aid = acc.get("id")
                if aid:
                    db.update_account_extract(int(aid), {
                        "ok": False,
                        "status": "running",
                        "job_id": task_id,
                        "link_type": _link_type(link_type),
                        "message": f"BurstPro 任务已创建 task_id={task_id} (HTTP {status})",
                        "cdk_remaining": cdk_info.get("remaining"),
                        "result": {
                            "read_token": read_token,
                            "task_id": task_id,
                            "provider": "burstpro",
                            "api_base": base,
                        },
                    })
        except Exception:
            logger.exception("[提链] 保存 BurstPro read_token 失败")
        return {
            "provider": "burstpro",
            "job_id": task_id,
            "task_id": task_id,
            "read_token": read_token,
            "status": data.get("status") or "queued",
            "queue_position": data.get("queue_position"),
            "cdk_remaining": cdk_info.get("remaining"),
            "http_status": status,
            "api_base": base,
            "raw": data,
        }

    # legacy
    payload = {"link_type": _link_type(link_type), "cdk": _cdk(cdk), "token": token}
    status, data = _http_json("POST", f"{base}/api/extract", payload=payload, timeout=timeout, retries=1)
    if status < 200 or status >= 300:
        raise RuntimeError(data.get("error") or data.get("message") or f"HTTP {status}")
    if not data.get("job_id"):
        raise RuntimeError(f"提链服务未返回 job_id: {data}")
    data = dict(data)
    data["provider"] = "legacy"
    return data


def _iter_sse_events(*, job_id: str, cdk: str):
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 900)
    url = f"{base}/api/jobs/{quote(job_id, safe='')}/events?{urlencode({'cdk': _cdk(cdk)})}"
    s = _session()
    try:
        if s is None:
            req = Request(url, headers={"Accept": "text/event-stream"})
            with urlopen(req, timeout=timeout) as resp:
                event = "message"
                data_lines: list[str] = []
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line == "":
                        if data_lines:
                            text = "\n".join(data_lines)
                            try:
                                data = json.loads(text)
                            except Exception:
                                data = {"raw": text}
                            yield event, data
                        event = "message"
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip() or "message"
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].lstrip())
                if data_lines:
                    text = "\n".join(data_lines)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text}
                    yield event, data
            return
        resp = s.get(url, timeout=timeout, stream=True)
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(f"监听提链事件失败 HTTP {resp.status_code}: {(resp.text or '')[:300]}")
        event = "message"
        data_lines: list[str] = []
        for raw in resp.iter_lines():
            if raw is None:
                continue
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", "replace")
            else:
                line = str(raw)
            line = line.rstrip("\r")
            if line == "":
                if data_lines:
                    text = "\n".join(data_lines)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text}
                    yield event, data
                event = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        if data_lines:
            text = "\n".join(data_lines)
            try:
                data = json.loads(text)
            except Exception:
                data = {"raw": text}
            yield event, data
    finally:
        try:
            s.close()
        except Exception:
            pass


def _workbench_result(task: dict, envelope: dict | None = None) -> dict:
    """Normalize Link Atelier task results to the local extract schema."""
    source = task if isinstance(task, dict) else {}
    envelope = envelope if isinstance(envelope, dict) else {}
    result = source.get("result") if isinstance(source.get("result"), dict) else None
    if result is None and isinstance(envelope.get("result"), dict):
        result = envelope["result"]
    result = dict(result or {})
    for key in (
        "result_url", "payment_url", "checkout_url", "long_url", "url",
        "link", "payment_link", "copy_paste",
    ):
        value = source.get(key) or envelope.get(key)
        if value and not result.get(key):
            result[key] = value
    link = str(
        result.get("long_url")
        or result.get("payment_link")
        or result.get("payment_url")
        or result.get("checkout_url")
        or result.get("result_url")
        or result.get("url")
        or result.get("link")
        or result.get("copy_paste")
        or ""
    ).strip()
    if link:
        result.setdefault("url", link)
        result.setdefault("long_url", link)
        result.setdefault("link", link)
    return result


def _poll_workbench_task(
    *,
    task_id: str,
    visitor_id: str,
    account_id: int,
    link_type: str,
) -> dict:
    """Poll Link Atelier ``GET /api/tasks/{id}`` until a link is available."""
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    max_wait = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 900, 120, 1800)
    deadline = time.time() + max_wait
    last: dict | None = None
    while time.time() < deadline:
        status, data = _http_json(
            "GET",
            f"{base}/api/tasks/{quote(task_id, safe='')}",
            headers={"X-Workbench-Visitor": visitor_id},
            timeout=timeout,
            retries=1,
        )
        if status < 200 or status >= 300:
            raise RuntimeError(_fmt_remote_error(data, status))
        task = data.get("task") if isinstance(data.get("task"), dict) else data
        task = task if isinstance(task, dict) else {}
        last = task
        state = str(task.get("status") or data.get("status") or "").strip().lower()
        stage = str(task.get("stage") or data.get("stage") or "")
        progress = task.get("progress", data.get("progress"))
        result = _workbench_result(task, data)
        has_link = bool(result.get("url") or result.get("long_url") or result.get("link"))
        message = f"Link Atelier {state or 'running'}/{stage or 'processing'} progress={progress}"
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "running",
            "job_id": task_id,
            "link_type": link_type,
            "message": message[:300],
        })
        if state in {"failed", "error", "cancelled", "canceled"}:
            raise RuntimeError(_fmt_remote_error(task, status) or f"Link Atelier 任务失败: {state}")
        if state in {"succeeded", "success", "completed", "complete", "done"} or has_link:
            if not has_link:
                raise RuntimeError(f"Link Atelier 任务完成但未返回支付链接: {task}")
            return {
                "ok": True,
                "status": "success",
                "job_id": task_id,
                "link_type": link_type,
                "result": {
                    **result,
                    "provider": "workbench",
                    "task": task,
                },
                "message": f"提链成功: {str(result.get('url') or '')[:120]}",
                "cdk_remaining": None,
            }
        time.sleep(3)
    raise RuntimeError(f"Link Atelier 提链超时({max_wait}s): {last}")


def _poll_linkpp_task(*, task_id: str, account_id: int, link_type: str) -> dict:
    """轮询 link-pp 任务并把 PayPal BA 链映射到本地统一结果。"""
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    max_wait = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 900, 120, 1800)
    deadline = time.time() + max_wait
    last: dict | None = None
    while time.time() < deadline:
        status, data = _http_json(
            "GET",
            f"{base}/api/jobs/{quote(task_id, safe='')}",
            timeout=timeout,
            retries=1,
        )
        if status < 200 or status >= 300:
            raise RuntimeError(_fmt_remote_error(data, status))
        last = data if isinstance(data, dict) else {}
        state = str(last.get("status") or "").strip().lower()
        remote_result = last.get("result") if isinstance(last.get("result"), dict) else {}
        failure = str(last.get("failure_reason") or last.get("error") or "").strip()
        message = f"link-pp {state or 'running'}"
        if failure:
            message = f"{message}: {failure}"
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "running",
            "job_id": task_id,
            "link_type": link_type,
            "message": message[:300],
        })
        if state in {"failed", "error", "cancelled", "canceled"}:
            raise RuntimeError(failure or f"link-pp 任务失败: {state}")
        url = str(
            remote_result.get("paypal_approve_url")
            or remote_result.get("provider_redirect_url")
            or remote_result.get("checkout_url")
            or ""
        ).strip()
        if state in {"success", "succeeded", "completed", "done"}:
            if not url:
                raise RuntimeError(f"link-pp 任务成功但未返回 PayPal 链: {last}")
            normalized = {
                **remote_result,
                "provider": "linkpp",
                "payment_method": "paypal",
                "payment_link_type": "paypal_billing_agreement",
                "url": url,
                "long_url": url,
                "link": url,
            }
            return {
                "ok": True,
                "status": "success",
                "job_id": task_id,
                "link_type": "paypal",
                "result": normalized,
                "message": f"PayPal 提链成功: {url[:120]}",
                "cdk_remaining": None,
            }
        time.sleep(3)
    raise RuntimeError(f"link-pp 提链超时({max_wait}s): {last}")


def _poll_burstpro_task(*, task_id: str, read_token: str, account_id: int, link_type: str) -> dict:
    """官方建议每 2–5 秒轮询 GET /api/tasks/{id}，Header: X-Task-Token。"""
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    # BurstPro 单任务经常 3-8 分钟，最多 6 次 attempt；默认至少等 15 分钟
    max_wait = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 900, 120, 1800)
    deadline = time.time() + max_wait
    last = None
    consecutive_net_err = 0
    while time.time() < deadline:
        try:
            status, data = _http_json(
                "GET",
                f"{base}/api/tasks/{quote(task_id, safe='')}",
                headers={"X-Task-Token": read_token},
                timeout=timeout,
                retries=1,
            )
            consecutive_net_err = 0
        except Exception as exc:
            consecutive_net_err += 1
            msg = f"BurstPro 轮询网络异常({consecutive_net_err}): {type(exc).__name__}: {exc}"
            logger.warning("[提链] %s", msg)
            db.update_account_extract(account_id, {
                "ok": False,
                "status": "running",
                "job_id": task_id,
                "link_type": link_type,
                "message": msg[:300],
            })
            if consecutive_net_err >= 8:
                raise RuntimeError(f"BurstPro 轮询连续网络失败: {exc}")
            time.sleep(min(15, 2 * consecutive_net_err))
            continue

        if status < 200 or status >= 300:
            raise RuntimeError(_fmt_remote_error(data, status))
        task = data.get("task") if isinstance(data.get("task"), dict) else data
        last = task if isinstance(task, dict) else {"raw": task}
        st = str(last.get("status") or "").strip().lower()
        stage = str(last.get("stage") or "")
        progress = last.get("progress")
        attempt = last.get("attempt")
        max_attempts = last.get("max_attempts")
        err_hint = str(last.get("error") or "").strip()
        msg = f"BurstPro {st}/{stage} attempt={attempt}/{max_attempts} progress={progress}"
        if err_hint:
            hint = _BURSTPRO_ERROR_HINTS.get(err_hint) or _BURSTPRO_ERROR_HINTS.get(err_hint.lower())
            msg = f"{msg} err={err_hint}" + (f"({hint})" if hint else "")
            msg = msg[:300]
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "running",
            "job_id": task_id,
            "link_type": link_type,
            "message": msg,
        })
        # 成功：completed（官方主状态）；兼容 success/done
        result_url = str(last.get("result_url") or last.get("url") or last.get("payment_url") or "").strip()
        if st == "completed" or (result_url and st not in ("failed", "cancelled", "error", "queued", "running", "pending")):
            if not result_url:
                raise RuntimeError(f"BurstPro 完成但无 result_url: {last}")
            return {
                "ok": True,
                "status": "success",
                "job_id": task_id,
                "link_type": link_type,
                "result": {
                    "url": result_url,
                    "link": result_url,
                    "long_url": result_url,
                    "result_url": result_url,
                    "expires_at": last.get("expires_at"),
                    "email": last.get("email"),
                    "provider": "burstpro",
                    "can_regenerate": last.get("can_regenerate"),
                    "task": last,
                    "read_token": read_token,
                },
                "message": f"提链成功: {result_url[:120]}",
                "cdk_remaining": None,
            }
        if st in ("failed", "cancelled", "error"):
            err = str(last.get("error") or st)
            hint = _BURSTPRO_ERROR_HINTS.get(err) or _BURSTPRO_ERROR_HINTS.get(err.lower())
            detail = f"{err}" + (f" | {hint}" if hint else "")
            raise RuntimeError(f"BurstPro 提链失败: {detail}")
        # 官方建议 2–5 秒；queued 稍慢
        time.sleep(4 if st in ("queued", "pending", "waiting") else 2)
    raise RuntimeError(f"BurstPro 提链超时({max_wait}s): {last}")


def _extract_error_message(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return str(data)
    err = data.get("error")
    if isinstance(err, dict):
        for key in ("message", "detail", "reason", "error", "msg", "description"):
            value = err.get(key)
            if value:
                return str(value).strip()
        return json.dumps(err, ensure_ascii=False)[:500]
    if err:
        code = str(err).strip()
        hint = _BURSTPRO_ERROR_HINTS.get(code) or _BURSTPRO_ERROR_HINTS.get(code.lower())
        if hint:
            return f"{code} | {hint}"
        return code
    for key in ("message", "detail", "reason", "msg", "description", "raw"):
        value = data.get(key)
        if value:
            return str(value).strip()
    return json.dumps(data, ensure_ascii=False)[:500]


def _format_failure_reason(exc: Exception, logs: list[str] | None = None, last_event: dict | None = None) -> str:
    reason = f"{type(exc).__name__}: {str(exc)}".strip()
    if (not str(exc).strip()) and logs:
        reason = str(logs[-1])
    if last_event and "提链事件流结束但未返回 result" in reason:
        extracted = _extract_error_message(last_event.get("data"))
        if extracted:
            reason = f"提链事件流结束但未返回 result；最后事件 {last_event.get('event')}: {extracted}"
    return reason[:500]


def _run_extract(*, account_id: int, email: str, access_token: str, link_type: str, cdk: str, trigger: str, release_slot: bool = True) -> dict:
    logs: list[str] = []
    last_event = None
    try:
        if not extraction_enabled():
            result = {
                "ok": False,
                "status": "stopped",
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "error": "提链功能已关闭",
                "message": "提链功能已关闭，任务未执行",
            }
            db.update_account_extract(account_id, result)
            return result
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        job = _create_extract_job(token=access_token, link_type=link_type, cdk=cdk, email=email)
        job_id = str(job.get("job_id") or job.get("task_id") or "")
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "running",
            "job_id": job_id,
            "link_type": link_type,
            "message": f"提链任务已创建，等待结果 provider={job.get('provider')} base={job.get('api_base') or _api_base()}",
            "cdk_remaining": job.get("cdk_remaining"),
        })

        if job.get("provider") == "linkpp":
            final = _poll_linkpp_task(
                task_id=job_id,
                account_id=account_id,
                link_type="paypal",
            )
            db.update_account_extract(account_id, final)
            logger.info("[提链] 成功(link-pp/PayPal): %s job=%s", email, job_id)
            return final

        if job.get("provider") == "workbench":
            attempt_limit = max(1, min(10, len(
                _workbench_proxy_pool("EXTRACT_LINK_CHECKOUT_PROXY_POOL")
            )))
            attempt = 1
            while True:
                try:
                    final = _poll_workbench_task(
                        task_id=job_id,
                        visitor_id=str(job.get("visitor_id") or ""),
                        account_id=account_id,
                        link_type=link_type,
                    )
                    db.update_account_extract(account_id, final)
                    logger.info(
                        "[提链] 成功(Link Atelier): %s type=%s job=%s attempt=%s/%s",
                        email, link_type, job_id, attempt, attempt_limit,
                    )
                    return final
                except Exception as workbench_exc:
                    if attempt >= attempt_limit or not _workbench_retryable_error(workbench_exc):
                        raise
                    attempt += 1
                    logger.warning(
                        "[提链] 工作台线路失败，切换下一条代理重试: %s attempt=%s/%s error=%s",
                        email, attempt, attempt_limit, str(workbench_exc)[:220],
                    )
                    job = _create_extract_job(
                        token=access_token,
                        link_type=link_type,
                        cdk=cdk,
                        email=email,
                    )
                    job_id = str(job.get("job_id") or job.get("task_id") or "")
                    db.update_account_extract(account_id, {
                        "ok": False,
                        "status": "running",
                        "job_id": job_id,
                        "link_type": link_type,
                        "message": f"Link Atelier 已切换代理，重试 {attempt}/{attempt_limit}",
                    })

        # BurstPro: poll GET /api/tasks/{id}
        if job.get("provider") == "burstpro" or job.get("read_token"):
            final = _poll_burstpro_task(
                task_id=job_id,
                read_token=str(job.get("read_token") or ""),
                account_id=account_id,
                link_type=link_type,
            )
            if job.get("cdk_remaining") is not None:
                final["cdk_remaining"] = job.get("cdk_remaining")
            db.update_account_extract(account_id, final)
            logger.info("[提链] 成功(BurstPro): %s type=%s job=%s", email, link_type, job_id)
            return final

        # legacy SSE
        for event, data in _iter_sse_events(job_id=job_id, cdk=cdk):
            last_event = {"event": event, "data": data}
            if event == "log":
                msg = str((data or {}).get("message") or "")[:300]
                if msg:
                    logs.append(msg)
                    db.update_account_extract(account_id, {
                        "ok": False,
                        "status": "running",
                        "job_id": job_id,
                        "link_type": link_type,
                        "message": msg,
                    })
            elif event == "result":
                result = (data or {}).get("result") if isinstance(data, dict) else None
                if not isinstance(result, dict):
                    result = {}
                final = {"ok": True, "status": "success", "job_id": job_id, "link_type": link_type, "result": result, "logs": logs}
                db.update_account_extract(account_id, final)
                logger.info("[提链] 成功: %s type=%s job=%s", email, link_type, job_id)
                return final
            elif event == "error":
                msg = _extract_error_message(data)
                raise RuntimeError(msg or "提链任务失败")
            elif event == "done":
                break
        raise RuntimeError(f"提链事件流结束但未返回 result: {last_event}")
    except Exception as exc:
        reason = _format_failure_reason(exc, logs=logs, last_event=last_event)
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason,
            "message": reason,
        }
        try:
            db.update_account_extract(account_id, result)
        except Exception:
            logger.exception("[提链] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[提链] 失败: %s", email)
        return result
    finally:
        if release_slot:
            try:
                _QUEUE_SLOTS.release()
            except ValueError:
                pass


def enqueue_account_extract(*, account_id: int, email: str, access_token: str, trigger: str = "manual", link_type: str | None = None, cdk: str | None = None) -> dict:
    if not extraction_enabled():
        return {"accepted": False, "busy": False, "disabled": True, "error": "提链功能已关闭"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    try:
        lt = _link_type(link_type)
        code = ""
        if _provider() not in {"workbench", "linkpp"}:
            code = _cdk(cdk)
        if not db.claim_account_extract(account_id, trigger=trigger, link_type=lt):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        fut = _EXECUTOR.submit(
            _run_extract,
            account_id=account_id,
            email=email,
            access_token=access_token,
            link_type=lt,
            cdk=code,
            trigger=trigger,
            release_slot=True,
        )
        return {"accepted": True, "busy": False, "future": fut, "link_type": lt}
    except Exception:
        try:
            _QUEUE_SLOTS.release()
        except ValueError:
            pass
        raise
