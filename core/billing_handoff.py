# -*- coding: utf-8 -*-
"""把新注册账号交接到 ChatGPT 官方 Plus 页面。"""
from __future__ import annotations

import logging
import threading
from urllib.parse import urlparse

from core import account_browser_service, db

logger = logging.getLogger(__name__)


def official_billing_url(value: str) -> str:
    """仅接受 chatgpt.com 官方 HTTPS 页面。"""
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "chatgpt.com":
        raise ValueError("BILLING_HANDOFF_URL 必须是 https://chatgpt.com 官方地址")
    return url


def open_billing_handoff(account_id: int, *, allow_rotated_exit: bool = False) -> dict:
    from config import billing_handoff as cfg

    account = db.get_account(int(account_id))
    if not account:
        return {"accepted": False, "error": "账号不存在"}
    return account_browser_service.open_account_browser(
        account,
        allow_rotated_exit=allow_rotated_exit,
        initial_url=official_billing_url(cfg.BILLING_HANDOFF_URL),
        allow_proxy_fallback=True,
        allow_direct_fallback=True,
        capture_checkout=True,
    )


def enqueue_billing_handoff(account_id: int) -> dict:
    """按配置延迟打开官方结账页；不阻塞注册任务。"""
    from config import billing_handoff as cfg

    if not bool(cfg.ENABLE_BILLING_HANDOFF):
        return {"accepted": False, "skipped": True, "reason": "ENABLE_BILLING_HANDOFF=False"}

    try:
        delay = max(0, min(300, int(cfg.BILLING_HANDOFF_DELAY_SECONDS)))
        official_billing_url(cfg.BILLING_HANDOFF_URL)
    except Exception as exc:
        logger.warning("[Plus交接] 配置无效: %s", exc)
        return {"accepted": False, "error": str(exc)}

    def _open() -> None:
        try:
            result = open_billing_handoff(account_id)
            if result.get("accepted") or result.get("busy"):
                logger.info("[Plus交接] 已打开官方页面: account_id=%s", account_id)
            else:
                logger.warning("[Plus交接] 打开失败: account_id=%s error=%s", account_id, result.get("error"))
        except Exception:
            logger.exception("[Plus交接] 后台打开异常: account_id=%s", account_id)

    timer = threading.Timer(delay, _open)
    timer.daemon = True
    timer.name = f"billing-handoff-{int(account_id)}"
    timer.start()
    return {"accepted": True, "queued": True, "delay_seconds": delay}
