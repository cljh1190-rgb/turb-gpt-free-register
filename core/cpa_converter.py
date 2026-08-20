# -*- coding: utf-8 -*-
"""把已导入的 ChatGPT OAuth 数据转换成 CPA/CLIProxyAPI Codex auth-file。"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

from config.codex import CODEX_CLIENT_ID


def _iso_expiry(value: Any, fallback: str | None = None) -> str:
    if value not in (None, ""):
        try:
            number = float(value)
            if number > 1e12:
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError, OSError):
            text = str(value).strip()
            if text:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    pass
    if fallback:
        try:
            parsed = datetime.fromisoformat(str(fallback).replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    return ""


def _jwt_claims(token: str) -> dict:
    parts = str(token or "").strip().split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}


def _audience_contains(audience: Any, expected: str) -> bool:
    if isinstance(audience, str):
        return audience == expected
    if isinstance(audience, list):
        return expected in {str(item) for item in audience}
    return False


def _validate_codex_credential(candidate: dict, *, source: str) -> dict:
    access_token = str(candidate.get("access_token") or "").strip()
    id_token = str(candidate.get("id_token") or "").strip()
    refresh_token = str(candidate.get("refresh_token") or "").strip()
    missing = [
        name for name, value in (
            ("access_token", access_token),
            ("id_token", id_token),
            ("refresh_token", refresh_token),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"{source} 缺少完整 Codex OAuth 字段: {', '.join(missing)}")

    access_claims = _jwt_claims(access_token)
    id_claims = _jwt_claims(id_token)
    access_client_id = str(access_claims.get("client_id") or candidate.get("client_id") or "").strip()
    if access_client_id != CODEX_CLIENT_ID:
        shown = access_client_id or "未识别"
        raise ValueError(f"{source} access_token 不是 Codex OAuth Token（client_id={shown}）")
    if not _audience_contains(id_claims.get("aud"), CODEX_CLIENT_ID):
        raise ValueError(f"{source} id_token 的 aud 不属于 Codex 客户端")

    auth_claim = access_claims.get("https://api.openai.com/auth")
    auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
    id_auth_claim = id_claims.get("https://api.openai.com/auth")
    id_auth_claim = id_auth_claim if isinstance(id_auth_claim, dict) else {}
    account_id = str(
        candidate.get("account_id")
        or auth_claim.get("chatgpt_account_id")
        or id_auth_claim.get("chatgpt_account_id")
        or ""
    ).strip()
    email = str(candidate.get("email") or id_claims.get("email") or "").strip()
    expired = _iso_expiry(candidate.get("expires_at") or access_claims.get("exp"), candidate.get("expired"))
    last_refresh = str(candidate.get("last_refresh") or "").strip()
    if not last_refresh:
        last_refresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
        "last_refresh": last_refresh,
        "email": email,
        "type": "codex",
        "expired": expired,
    }


def build_cpa_auth_file(account: dict, *, fallback_credential: dict | None = None) -> tuple[dict, dict]:
    """Build a CPA file from one coherent, complete Codex OAuth credential set.

    ChatGPT web access tokens and Codex OAuth refresh/id tokens must never be mixed.
    """
    fallback_credential = fallback_credential or {}
    candidates = []
    if fallback_credential:
        candidates.append(("正常 Codex 授权文件", {
            "access_token": fallback_credential.get("access_token"),
            "id_token": fallback_credential.get("id_token"),
            "refresh_token": fallback_credential.get("refresh_token"),
            "account_id": fallback_credential.get("account_id"),
            "email": fallback_credential.get("email") or account.get("email"),
            "expires_at": fallback_credential.get("expires_at"),
            "expired": fallback_credential.get("expired"),
            "last_refresh": fallback_credential.get("last_refresh"),
            "client_id": fallback_credential.get("client_id"),
        }))
    candidates.append(("导入账号 OAuth", {
        "access_token": account.get("access_token"),
        "id_token": account.get("oauth_id_token"),
        "refresh_token": account.get("oauth_refresh_token"),
        "account_id": account.get("account_id"),
        "email": account.get("email"),
        "expires_at": account.get("oauth_expires_at") or account.get("token_expires_at"),
        "client_id": account.get("oauth_client_id"),
    }))

    auth_file = None
    selected_source = ""
    errors = []
    for source, candidate in candidates:
        try:
            auth_file = _validate_codex_credential(candidate, source=source)
            selected_source = source
            break
        except ValueError as exc:
            errors.append(str(exc))
    if auth_file is None:
        raise ValueError(
            "没有可用的完整 Codex OAuth 凭证，已拒绝生成不可用的 CPA 文件；"
            + "；".join(errors)
        )

    meta = {
        "email": auth_file["email"],
        "account_id": auth_file["account_id"],
        "complete": True,
        "refreshable": True,
        "has_id_token": True,
        "expired": auth_file["expired"],
        "credential_source": selected_source,
        "plan_type": account.get("current_plan_type") or account.get("plan_type"),
    }
    return auth_file, meta


def safe_cpa_filename(email: str, plan_type: str = "plus") -> str:
    safe_email = "".join(ch if ch.isalnum() or ch in ("@", ".", "-", "_") else "_" for ch in str(email or "unknown"))
    plan = "".join(ch for ch in str(plan_type or "").lower() if ch.isalnum() or ch in ("-", "_"))
    return f"codex-{safe_email}-{plan}.json" if plan else f"codex-{safe_email}.json"
