# -*- coding: utf-8 -*-
"""套餐/Plus 资格查询后台队列。"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import proxy as proxy_cfg
from core import db
from core.chatgpt_plan import check_account_plan

logger = logging.getLogger(__name__)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(proxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(proxy_cfg, name, default) or 0.0)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("PLAN_CHECK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("PLAN_CHECK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="plan-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _wait_for_rate_slot() -> None:
    """为所有查询线程分配错开的请求启动时间。"""
    global _NEXT_REQUEST_AT
    min_interval = _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0)
    jitter = _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _registration_recheck_delay() -> float:
    return _float_setting("PLAN_CHECK_REGISTRATION_RECHECK_DELAY", 2.0, 0.0, 30.0)


def _auto_delete_invalid_account(*, account_id: int, email: str, result: dict) -> bool:
    """删除已被 ChatGPT 明确判定失效的本地账号记录。

    仅接受 account_validity=invalid；代理封禁、403、超时等不确定结果必须保留。
    """
    if str(result.get("account_validity") or "").strip().lower() != "invalid":
        return False
    try:
        deleted = bool(db.delete_account(acc_id=account_id))
    except Exception as exc:
        logger.warning("[Plan] 失效账号自动删除失败: id=%s email=%s: %s", account_id, email, exc)
        return False
    if deleted:
        logger.warning(
            "[Plan] 检测到账号已失效，已自动删除本地记录: id=%s email=%s error=%s",
            account_id,
            email,
            result.get("error") or "token invalid",
        )
    return deleted


def _env_flag(name: str, default: bool = True) -> bool:
    import os
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _maybe_auto_extract_after_plan(
    *,
    account_id: int,
    email: str,
    access_token: str,
    plan_result: dict,
    trigger: str,
) -> None:
    """套餐查询确认 free+可试用后，自动入队提链（BurstPro UPI 等）。

    开关：ENABLE_EXTRACT_AUTO（默认 False）。工作台仅需要 API Base；其他 provider 还需要 CDK。
    """
    if not _env_flag("ENABLE_EXTRACT_AUTO", False):
        return
    if not bool(plan_result.get("plus_trial_eligible")):
        return
    plan = str(plan_result.get("current_plan_type") or plan_result.get("plan_type") or "").lower()
    if plan and plan != "free":
        return
    token = str(access_token or "").strip()
    if not token:
        return
    try:
        from core import extract_link_service
        if not extract_link_service.extraction_enabled():
            return
        # 配置不齐时静默跳过，不打断套餐查询主流程
        try:
            api_base = extract_link_service._api_base()
            if extract_link_service._provider(api_base) not in {"workbench", "linkpp"}:
                extract_link_service._cdk()
        except Exception as cfg_exc:
            logger.info("[提链] 自动提链跳过（未配置）: %s, %s", email, cfg_exc)
            return
        queued = extract_link_service.enqueue_account_extract(
            account_id=int(account_id),
            email=email or "",
            access_token=token,
            trigger=f"plan_auto:{trigger}",
            link_type=None,  # 走 EXTRACT_LINK_TYPE
        )
        if queued.get("accepted"):
            logger.info("[提链] 套餐确认可试用，已自动入队: id=%s email=%s", account_id, email)
        elif queued.get("busy"):
            logger.info("[提链] 账号已在提链中，跳过自动入队: id=%s email=%s", account_id, email)
        else:
            logger.warning(
                "[提链] 自动入队未接受: id=%s email=%s err=%s",
                account_id, email, queued.get("error"),
            )
    except Exception as exc:
        logger.warning(
            "[提链] 自动入队异常（不影响套餐结果）: %s, %s: %s",
            email, type(exc).__name__, str(exc)[:180],
        )


def _run_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None,
    timezone_offset_min: str,
    device_id: str | None,
    browser_profile: dict | None,
) -> dict:
    try:
        if not db.mark_account_plan_check_running(account_id):
            return {"ok": False, "error": "账号已删除或套餐查询状态已被重置"}

        _wait_for_rate_slot()
        result = check_account_plan(
            access_token,
            proxy=proxy,
            timezone_offset_min=timezone_offset_min,
            device_id=device_id,
            browser_profile=browser_profile,
            include_plus_trial=not str(trigger or "").startswith("validity"),
        )

        recheck_delay = _registration_recheck_delay()
        should_recheck = (
            trigger == "registration_auto"
            and recheck_delay > 0
            and bool(result.get("ok"))
            and str(result.get("current_plan_type") or "").lower() == "free"
            and not bool(result.get("plus_trial_eligible"))
        )
        if should_recheck:
            logger.info("[Plan] 新账号暂未发现 Plus 试用资格，%.1fs 后复查一次: %s", recheck_delay, email)
            time.sleep(recheck_delay)
            _wait_for_rate_slot()
            recheck_result = check_account_plan(
                access_token,
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
                device_id=device_id,
                browser_profile=browser_profile,
                max_attempts=1,
                include_plus_trial=not str(trigger or "").startswith("validity"),
            )
            if recheck_result.get("ok"):
                result = recheck_result
            else:
                logger.warning(
                    "[Plan] 新账号资格复查失败，保留首次成功结果: %s, %s",
                    email,
                    recheck_result.get("error") or "未知错误",
                )

        db.update_account_plan_check(acc_id=account_id, result=result)
        _auto_delete_invalid_account(account_id=account_id, email=email, result=result)
        if result.get("ok"):
            logger.info(
                "[Plan] 后台查询成功: %s, plan=%s, plus_trial=%s, trigger=%s",
                email,
                result.get("current_plan_type") or "unknown",
                bool(result.get("plus_trial_eligible")),
                trigger,
            )
            # free + 可 Plus 试用 → 自动入队 BurstPro/提链（需已配置 API BASE + CDK）
            # 独立 PLUS 查询页只负责识别套餐，不应因为查到试用资格而自动提链。
            if not str(trigger or "").startswith("plus_query_"):
                _maybe_auto_extract_after_plan(
                    account_id=account_id,
                    email=email,
                    access_token=access_token,
                    plan_result=result,
                    trigger=trigger,
                )
        else:
            logger.warning(
                "[Plan] 后台查询失败: %s, trigger=%s, error=%s",
                email,
                trigger,
                result.get("error") or "未知错误",
            )
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        try:
            db.update_account_plan_check(acc_id=account_id, result=result)
        except Exception:
            logger.exception("[Plan] 写入后台查询异常状态失败: account_id=%s", account_id)
        logger.exception("[Plan] 后台查询异常: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None = None,
    timezone_offset_min: str = "-",
    device_id: str | None = None,
    browser_profile: dict | None = None,
) -> dict:
    """把查询放入统一线程池；重复查询或队列满时不提交。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "套餐查询队列已满，请稍后重试"}

    if not db.claim_account_plan_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询套餐"}

    try:
        _EXECUTOR.submit(
            _run_plan_check,
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=str(trigger or "manual"),
            proxy=proxy,
            timezone_offset_min=str(timezone_offset_min or "-"),
            device_id=str(device_id or "").strip() or None,
            browser_profile=dict(browser_profile) if isinstance(browser_profile, dict) else None,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"套餐查询入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_plan_check(acc_id=account_id, result=result)
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "min_interval": _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0),
        "jitter": _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0),
    }
