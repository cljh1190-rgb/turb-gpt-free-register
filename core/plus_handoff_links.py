# -*- coding: utf-8 -*-
"""Temporary local short links for the official ChatGPT Plus page."""
from __future__ import annotations

import secrets
import threading
import time

from core.billing_handoff import official_billing_url

_LOCK = threading.RLock()
_LINKS: dict[str, dict] = {}
_TTL_SECONDS = 30 * 60


def _cleanup(now: float) -> None:
    expired = [code for code, item in _LINKS.items() if float(item.get("expires_at") or 0) <= now]
    for code in expired:
        _LINKS.pop(code, None)


def create_short_link(*, account_id: int, target_url: str) -> dict:
    target = official_billing_url(target_url)
    now = time.time()
    with _LOCK:
        _cleanup(now)
        code = secrets.token_urlsafe(5).replace("-", "").replace("_", "")[:7]
        while not code or code in _LINKS:
            code = secrets.token_urlsafe(5).replace("-", "").replace("_", "")[:7]
        expires_at = now + _TTL_SECONDS
        _LINKS[code] = {
            "account_id": int(account_id),
            "target_url": target,
            "created_at": now,
            "expires_at": expires_at,
        }
    return {"code": code, "expires_at": expires_at}


def resolve_short_link(code: str) -> dict | None:
    now = time.time()
    with _LOCK:
        _cleanup(now)
        item = _LINKS.get(str(code or "").strip())
        return dict(item) if item else None
