# -*- coding: utf-8 -*-
"""查询 ChatGPT/Codex 使用额度（wham/usage）。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from core.chatgpt_plan import normalize_token, now_iso, token_claims
from core.session import BrowserSession


USAGE_PATH = "/backend-api/wham/usage"


def _epoch_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def parse_quota_usage(data: dict) -> dict:
    rate = data.get("rate_limit") or {}
    primary = rate.get("primary_window") or {}
    secondary = rate.get("secondary_window") or {}
    credits = data.get("credits") or {}
    spend = data.get("spend_control") or {}

    primary_used = primary.get("used_percent")
    secondary_used = secondary.get("used_percent")
    try:
        primary_remaining = max(0.0, 100.0 - float(primary_used)) if primary_used is not None else None
    except (TypeError, ValueError):
        primary_remaining = None
    try:
        secondary_remaining = max(0.0, 100.0 - float(secondary_used)) if secondary_used is not None else None
    except (TypeError, ValueError):
        secondary_remaining = None

    return {
        "ok": True,
        "checked_at": now_iso(),
        "email": data.get("email"),
        "user_id": data.get("user_id"),
        "usage_account_id": data.get("account_id"),
        "quota_plan_type": data.get("plan_type"),
        "quota_allowed": bool(rate.get("allowed")),
        "quota_limit_reached": bool(rate.get("limit_reached")),
        "quota_limit_reached_type": data.get("rate_limit_reached_type"),
        "primary_used_percent": primary_used,
        "primary_remaining_percent": primary_remaining,
        "primary_limit_window_seconds": primary.get("limit_window_seconds"),
        "primary_reset_after_seconds": primary.get("reset_after_seconds"),
        "primary_reset_at": primary.get("reset_at"),
        "primary_reset_at_iso": _epoch_iso(primary.get("reset_at")),
        "secondary_used_percent": secondary_used,
        "secondary_remaining_percent": secondary_remaining,
        "secondary_limit_window_seconds": secondary.get("limit_window_seconds"),
        "secondary_reset_after_seconds": secondary.get("reset_after_seconds"),
        "secondary_reset_at": secondary.get("reset_at"),
        "secondary_reset_at_iso": _epoch_iso(secondary.get("reset_at")),
        "credits_has_credits": bool(credits.get("has_credits")),
        "credits_unlimited": bool(credits.get("unlimited")),
        "credits_balance": credits.get("balance"),
        "credits_overage_limit_reached": bool(credits.get("overage_limit_reached")),
        "spend_control_reached": bool(spend.get("reached")),
        "spend_control_individual_limit": spend.get("individual_limit"),
        "raw": data,
    }


def check_account_quota(token: str, *, proxy: str = "", timeout: float = 20.0) -> dict:
    token = normalize_token(token)
    claims = token_claims(token)
    if not token:
        return {"ok": False, "checked_at": now_iso(), "error": "token 为空"}
    if claims.get("token_expired") is True:
        return {"ok": False, "checked_at": now_iso(), "error": "token 已过期", **{k: v for k, v in claims.items() if k != "payload"}}

    env = None
    response = None
    try:
        env = BrowserSession(proxy=str(proxy or ""), detect_exit_geo=False)
        headers = env._get_common_headers()
        headers.update({
            "accept": "application/json",
            "authorization": f"Bearer {token}",
            "oai-device-id": env.device_id,
            "referer": "https://chatgpt.com/",
            "x-openai-target-path": USAGE_PATH,
            "x-openai-target-route": USAGE_PATH,
        })
        # 注意：wham/usage 不能带 chatgpt-account-id，否则部分有效 Token 会返回 token_expired。
        headers.pop("chatgpt-account-id", None)
        response = env.session.get(
            f"https://chatgpt.com{USAGE_PATH}",
            headers=headers,
            timeout=max(3.0, min(60.0, float(timeout or 20.0))),
            allow_redirects=False,
        )
        status = int(response.status_code)
        text = response.text or ""
        try:
            data = response.json()
        except Exception:
            data = json.loads(text) if text.strip().startswith("{") else None
        if not (200 <= status < 300) or not isinstance(data, dict):
            message = None
            if isinstance(data, dict):
                message = ((data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("error"))
            return {
                "ok": False,
                "checked_at": now_iso(),
                "http_status": status,
                "error": str(message or f"HTTP {status}"),
                "response_preview": text[:500],
                **{k: v for k, v in claims.items() if k != "payload"},
            }
        result = parse_quota_usage(data)
        result["http_status"] = status
        result.update({k: v for k, v in claims.items() if k != "payload" and v is not None})
        return result
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": int(response.status_code) if response is not None and getattr(response, "status_code", None) else None,
            "error": f"{type(exc).__name__}: {exc}",
            **{k: v for k, v in claims.items() if k != "payload"},
        }
    finally:
        if env is not None:
            try:
                env.session.close()
            except Exception:
                pass
