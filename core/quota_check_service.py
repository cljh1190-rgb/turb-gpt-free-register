# -*- coding: utf-8 -*-
"""ChatGPT/Codex 额度查询后台队列。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from core import db
from core.chatgpt_quota import check_account_quota

logger = logging.getLogger(__name__)

_WORKERS = 4
_QUEUE_LIMIT = 500
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="quota-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def _run(*, account_id: int, email: str, access_token: str) -> dict:
    try:
        if not db.mark_account_quota_check_running(account_id):
            return {"ok": False, "error": "账号已删除或额度查询状态已重置"}
        result = check_account_quota(access_token, proxy="")
        db.update_account_quota_check(account_id, result)
        if result.get("ok"):
            logger.info("[Quota] 查询成功: %s used=%s%%", email, result.get("primary_used_percent"))
        else:
            logger.warning("[Quota] 查询失败: %s error=%s", email, result.get("error"))
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        try:
            db.update_account_quota_check(account_id, result)
        except Exception:
            logger.exception("[Quota] 写入异常状态失败: %s", account_id)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_quota_check(*, account_id: int, email: str, access_token: str) -> dict:
    token = str(access_token or "").strip()
    if not token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "额度查询队列已满"}
    if not db.claim_account_quota_check(account_id):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询额度"}
    try:
        _EXECUTOR.submit(_run, account_id=int(account_id), email=str(email or ""), access_token=token)
    except Exception as exc:
        _QUEUE_SLOTS.release()
        db.update_account_quota_check(account_id, {"ok": False, "error": f"额度查询入队失败: {exc}"})
        return {"accepted": False, "busy": False, "error": f"额度查询入队失败: {exc}"}
    return {"accepted": True, "busy": False, "account_id": int(account_id), "status": "queued"}


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
