# -*- coding: utf-8 -*-
"""
号池服务：注册成功账号统一入池，按额度 / token / 手动状态判定可用性，
对外提供分配、无感切号、状态统计与主动巡检。

线程安全约定：
    - 所有持久化读写走 core.db（内部有 _LOCK）；
    - 本模块内只维护“轮询游标”这一内存状态，用 _POOL_LOCK 保护；
    - 所有函数幂等，可被 WebUI 多线程安全调用。
"""
from __future__ import annotations

import json
import logging
import random
import threading
from datetime import datetime

from core import db
from core.quota_check_service import enqueue_account_quota_check

logger = logging.getLogger(__name__)

_POOL_LOCK = threading.RLock()
_RR_INDEX = 0

# 主动巡检线程单例（避免 create_app / 测试重复启动）
_PROBE_THREAD: threading.Thread | None = None
_PROBE_THREAD_LOCK = threading.Lock()
_PROBE_STOP = threading.Event()

# codex_status 里明确表示“账号已废/被封”的状态 → 一律不可用。
# 其它如 success/skipped/missing/failed 只代表 Codex 授权情况，不代表 API 账号死亡，
# 尤其是 ENABLE_CODEX_AUTO=False 的新账号，codex_status 多为空/skipped，不能因此挡在池外。
_DEAD_CODEX_STATUS = {"deactivated"}


# ============================================================
# 配置读取（延迟读 config.pool，热加载后自动看到新值）
# ============================================================

def _pool_cfg(name: str, default):
    try:
        from config import pool as _cfg
        return getattr(_cfg, name, default)
    except Exception:
        return default


def _enabled() -> bool:
    return bool(_pool_cfg("POOL_ENABLED", True))


def _threshold() -> float:
    try:
        return max(0.0, float(_pool_cfg("POOL_QUOTA_THRESHOLD_PERCENT", 20.0)))
    except (TypeError, ValueError):
        return 20.0


def _allow_unknown() -> bool:
    return bool(_pool_cfg("POOL_ALLOW_UNKNOWN_QUOTA", True))


def _strategy() -> str:
    return str(_pool_cfg("POOL_ACQUIRE_STRATEGY", "round_robin")).strip().lower()


def _probe_interval() -> int:
    try:
        return max(0, int(_pool_cfg("POOL_PROBE_INTERVAL_SECONDS", 600)))
    except (TypeError, ValueError):
        return 600


def _probe_stale_seconds() -> int:
    try:
        return max(60, int(_pool_cfg("POOL_PROBE_STALE_SECONDS", 12 * 3600)))
    except (TypeError, ValueError):
        return 12 * 3600


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


# ============================================================
# 可用性判定
# ============================================================

def _has_quota_data(row: dict) -> bool:
    """账号是否已有成功的额度数据。查询失败 / 从没查过都算“未知”。"""
    return (
        row.get("quota_check_status") == "success"
        or row.get("quota_last_success_at") is not None
    )


def account_usable(row: dict) -> tuple[bool, str]:
    """
    依据账号字段判断是否可被号池分配。

    判定顺序（任一命中即不可用，reason 为该命中的原因）：
        1. 号池全局开关关闭
        2. 手动禁用（pool_enabled=False / pool_disabled=True）
        3. 缺少 access_token
        4. JWT 层 token 已过期 / 额度查询返回 HTTP 401（token 被吊销）
        5. pool_status=exhausted 且之后没有更新的成功额度查询（无感切号写回）
        6. codex_status 为废号状态
        7. quota_limit_reached / spend_control_reached / credits 超额上限
        8. credits 有余额且 <= 0（已耗尽）
        9. primary_remaining_percent <= POOL_QUOTA_THRESHOLD_PERCENT
        10. 无额度数据：POOL_ALLOW_UNKNOWN_QUOTA=True 视为可用，否则不可用

    返回 (usable, reason)；usable=True 时 reason 为空字符串。
    """
    if not _enabled():
        return False, "号池功能未启用（POOL_ENABLED=False）"
    if row.get("pool_enabled") is False:
        return False, str(row.get("pool_disabled_reason") or "已被管理员禁用")
    if row.get("pool_disabled") is True:
        return False, str(row.get("pool_disabled_reason") or "已被强制禁用")

    token = str(row.get("access_token") or "").strip()
    if not token:
        return False, "缺少 access_token"

    try:
        from core.chatgpt_plan import token_claims
        if token_claims(token).get("token_expired") is True:
            return False, "token 已过期"
    except Exception:
        pass
    if int(row.get("quota_check_http_status") or 0) == 401:
        return False, "token 已失效（HTTP 401）"

    if row.get("pool_status") == "exhausted":
        exhausted_at = _parse_ts(row.get("pool_exhausted_at"))
        refreshed_at = _parse_ts(row.get("quota_last_success_at") or row.get("quota_checked_at"))
        if exhausted_at is None or refreshed_at is None or refreshed_at <= exhausted_at:
            return False, str(row.get("pool_exhausted_reason") or "额度已耗尽")

    codex_status = str(row.get("codex_status") or "").lower()
    if codex_status in _DEAD_CODEX_STATUS:
        return False, "账号已废号（codex deactivated）"

    if row.get("quota_limit_reached") is True:
        return False, "额度已达上限"
    if row.get("spend_control_reached") is True:
        return False, "消费控制已达上限"
    if row.get("credits_overage_limit_reached") is True:
        return False, "credits 超额上限"

    if row.get("credits_has_credits") is True and not bool(row.get("credits_unlimited")):
        try:
            if float(row.get("credits_balance")) <= 0:
                return False, "credits 余额已耗尽"
        except (TypeError, ValueError):
            pass

    remaining = row.get("primary_remaining_percent")
    if remaining is not None:
        try:
            if float(remaining) <= _threshold():
                return False, f"剩余额度 {float(remaining):g}% 低于阈值 {_threshold():g}%"
        except (TypeError, ValueError):
            pass

    if not _has_quota_data(row):
        if _allow_unknown():
            return True, ""
        return False, "缺少额度数据"

    return True, ""


# ============================================================
# 池内账号读取 / 挑选
# ============================================================

def _matches_tags(row: dict, tags) -> bool:
    tags = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]
    if not tags:
        return True
    haystack = " ".join(
        str(row.get(k) or "").lower()
        for k in ("plan_type", "current_plan_type", "email_source", "note")
    )
    return any(tag in haystack for tag in tags)


def _pool_candidates(tags=None) -> list[dict]:
    """返回当前可用、可分配的账号原始行列表（不修改数据）。"""
    rows = []
    for row in db.list_all_accounts():
        if bool(row.get("archived")):
            continue
        if row.get("pool_enabled") is False:
            continue
        if not _matches_tags(row, tags):
            continue
        usable, _reason = account_usable(row)
        if usable:
            rows.append(row)
    return rows


def _pick(rows: list[dict]) -> dict:
    strategy = _strategy()
    if strategy == "random":
        return random.choice(rows)
    # 默认 round_robin：从上次分配位置之后开始选，避免同一账号被连续分配
    global _RR_INDEX
    with _POOL_LOCK:
        idx = _RR_INDEX % len(rows)
        _RR_INDEX += 1
        return rows[idx]


def _quota_snapshot(row: dict) -> dict:
    keys = (
        "quota_plan_type", "quota_allowed", "quota_limit_reached", "quota_limit_reached_type",
        "primary_used_percent", "primary_remaining_percent", "primary_reset_at_iso",
        "secondary_used_percent", "secondary_remaining_percent",
        "credits_has_credits", "credits_unlimited", "credits_balance",
        "spend_control_reached", "quota_check_status", "quota_check_ok",
        "quota_check_http_status", "quota_check_error", "quota_checked_at",
    )
    out: dict = {}
    for k in keys:
        v = row.get(k)
        if v is not None:
            out[k] = v
    return out


def _build_acquire_result(row: dict, *, acquired: bool) -> dict:
    acc_id = int(row.get("id") or 0)
    pool_state = {
        "enabled": row.get("pool_enabled") is not False,
        "status": row.get("pool_status") or "available",
        "last_acquired_at": row.get("pool_last_acquired_at"),
        "acquire_count": int(row.get("pool_acquire_count") or 0),
    }
    if acquired:
        updated = db.mark_account_pool_acquired(acc_id)
        if updated:
            pool_state["last_acquired_at"] = updated.get("pool_last_acquired_at") or pool_state["last_acquired_at"]
            pool_state["acquire_count"] = int(updated.get("pool_acquire_count") or pool_state["acquire_count"])
    return {
        "ok": True,
        "account_id": acc_id,
        "email": row.get("email"),
        "access_token": row.get("access_token"),
        "user_id": row.get("user_id"),
        "user_name": row.get("user_name"),
        "plan_type": row.get("current_plan_type") or row.get("plan_type"),
        "quota": _quota_snapshot(row),
        "pool_status": pool_state,
    }


# ============================================================
# 对外 API：分配 / 切号 / 统计 / 开关 / 巡检
# ============================================================

def acquire(prefer_email: str | None = None, tags: list[str] | None = None) -> dict:
    """
    从号池分配一个可用账号。

    prefer_email：优先分配指定邮箱账号（不满足可用性时自动回退到其它可用账号）。
    tags：可选过滤标签，按 plan_type / current_plan_type / email_source / note 子串匹配。

    返回 {ok, account_id, email, access_token, user_id, plan_type, quota, pool_status}；
    无可用账号时返回 {ok: False, error: "号池暂无可用账号"}。
    """
    if not _enabled():
        return {"ok": False, "error": "号池功能未启用（POOL_ENABLED=False）"}
    rows = _pool_candidates(tags=tags)
    if not rows:
        return {"ok": False, "error": "号池暂无可用账号"}
    picked = None
    prefer = str(prefer_email or "").strip().lower()
    if prefer:
        picked = next((r for r in rows if (r.get("email") or "").lower() == prefer), None)
    if picked is None:
        picked = _pick(rows)
    return _build_acquire_result(picked, acquired=True)


def _most_recently_acquired() -> dict | None:
    best = None
    best_ts = None
    for row in db.list_all_accounts():
        ts = _parse_ts(row.get("pool_last_acquired_at"))
        if ts is None:
            continue
        if best_ts is None or ts > best_ts:
            best, best_ts = row, ts
    return best


def switch(
    current_email: str | None = None,
    next_prefer_email: str | None = None,
    reason: str | None = None,
) -> dict:
    """
    无感切号：
        1) 把当前使用中的账号标记为“额度耗尽/受限”（写回 pool_status=exhausted 与原因）；
        2) acquire() 分配下一个可用账号并返回新凭证。

    current_email：当前已耗尽/受限的账号；缺省时自动选取最近一次被分配的账号。
    next_prefer_email：希望优先切到的目标账号（仍须满足可用性判定）。

    返回新账号凭证 + switched_from（旧账号摘要）+ 切换原因；无可用账号时 error 提示。
    """
    if not _enabled():
        return {"ok": False, "error": "号池功能未启用（POOL_ENABLED=False）"}
    switched_from = None
    mark_reason = str(reason or "调用方触发无感切号")[:200]

    if current_email:
        acc = db.get_account_by_email(str(current_email).strip())
        if acc is not None:
            db.mark_account_pool_exhausted(int(acc.get("id") or 0), mark_reason)
            switched_from = {"account_id": acc.get("id"), "email": acc.get("email")}
    else:
        last = _most_recently_acquired()
        if last is not None:
            db.mark_account_pool_exhausted(int(last.get("id") or 0), mark_reason)
            switched_from = {"account_id": last.get("id"), "email": last.get("email")}

    result = acquire(prefer_email=next_prefer_email)
    result["switched_from"] = switched_from
    if not result.get("ok"):
        return result
    result["switched"] = True
    result["switch_reason"] = mark_reason
    return result


def set_account_pool_state(acc_id, enabled: bool, reason: str | None = None) -> dict:
    """管理员手动启用/禁用池内账号。enabled=False 时 reason 记录踢出原因。"""
    try:
        acc_id = int(acc_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "acc_id 必须是数字"}
    ok = db.update_account_pool_state(acc_id, enabled=bool(enabled), reason=reason)
    if not ok:
        return {"ok": False, "error": "账号不存在"}
    return {"ok": True, "account_id": acc_id, "enabled": bool(enabled), "reason": str(reason or "")}


def pool_summary() -> dict:
    """号池统计：总数、可用、不可用（按 reason 分桶）、未知、禁用、最近分配时间等。"""
    rows = db.list_all_accounts()
    total = 0
    in_pool = 0
    available = 0
    unknown = 0
    disabled = 0
    exhausted = 0
    reasons: dict[str, int] = {}
    last_acquired_at: datetime | None = None

    for row in rows:
        total += 1
        if bool(row.get("archived")):
            continue
        if row.get("pool_enabled") is False:
            disabled += 1
            reason = str(row.get("pool_disabled_reason") or "手动禁用")
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        in_pool += 1
        if row.get("pool_status") == "exhausted":
            exhausted += 1
        if not _has_quota_data(row):
            unknown += 1
        usable, reason = account_usable(row)
        if usable:
            available += 1
        else:
            reasons[reason or "未知原因"] = reasons.get(reason or "未知原因", 0) + 1
        ts = _parse_ts(row.get("pool_last_acquired_at"))
        if ts is not None and (last_acquired_at is None or ts > last_acquired_at):
            last_acquired_at = ts

    return {
        "ok": True,
        "enabled": _enabled(),
        "total": total,
        "in_pool": in_pool,
        "available": available,
        "unavailable": in_pool - available,
        "unknown": unknown,
        "disabled": disabled,
        "exhausted": exhausted,
        "reasons": dict(sorted(reasons.items(), key=lambda x: -x[1])),
        "last_acquired_at": (
            last_acquired_at.isoformat(timespec="seconds") if last_acquired_at is not None else None
        ),
        "config": {
            "pool_enabled": _enabled(),
            "quota_threshold_percent": _threshold(),
            "allow_unknown_quota": _allow_unknown(),
            "probe_interval_seconds": _probe_interval(),
            "acquire_strategy": _strategy(),
        },
    }


def list_pool_accounts(
    limit: int = 500,
    q: str | None = None,
    status: str | None = None,
) -> dict:
    """号池账号列表（带可用性判定与原因），供 WebUI 展示。默认不含敏感字段。"""
    items: list[dict] = []
    for row in db.list_all_accounts():
        item = {
            "id": row.get("id"),
            "email": row.get("email"),
            "plan_type": row.get("current_plan_type") or row.get("plan_type"),
            "pool_enabled": row.get("pool_enabled") is not False,
            "pool_status": row.get("pool_status") or "available",
            "pool_disabled_reason": row.get("pool_disabled_reason"),
            "pool_exhausted_reason": row.get("pool_exhausted_reason"),
            "pool_last_acquired_at": row.get("pool_last_acquired_at"),
            "pool_acquire_count": int(row.get("pool_acquire_count") or 0),
            "archived": bool(row.get("archived")),
            "codex_status": row.get("codex_status"),
            "quota_check_status": row.get("quota_check_status"),
            "quota_checked_at": row.get("quota_checked_at"),
            "quota": _quota_snapshot(row),
        }
        usable, reason = account_usable(row)
        item["usable"] = usable
        item["unusable_reason"] = "" if usable else reason
        items.append(item)

    if q:
        ql = str(q).strip().lower()
        items = [
            i for i in items
            if ql in json.dumps(i, ensure_ascii=False).lower()
        ]
    if status:
        s = str(status).strip().lower()
        if s in ("available", "usable"):
            items = [i for i in items if i.get("usable")]
        elif s in ("unavailable", "unusable"):
            items = [i for i in items if not i.get("usable") and i.get("pool_enabled")]
        elif s == "disabled":
            items = [i for i in items if not i.get("pool_enabled")]
        elif s == "unknown":
            items = [i for i in items if i.get("pool_enabled") and i.get("quota_check_status") != "success"]

    return {
        "ok": True,
        "total": len(items),
        "items": items[:max(1, min(2000, int(limit or 500)))],
    }


def enqueue_pool_probe() -> dict:
    """
    主动巡检：对池内“长时间未成功查额度”的账号批量入队额度检查（复用 quota_check_service）。
    返回 {ok, scanned, queued, skipped_busy, skipped_fresh, skipped_invalid}。
    """
    if not _enabled():
        return {"ok": False, "error": "号池功能未启用（POOL_ENABLED=False）"}
    stale_after = _probe_stale_seconds()
    now = datetime.now()
    scanned = queued = busy = fresh = invalid = 0
    results: list[dict] = []
    for row in db.list_all_accounts():
        if bool(row.get("archived")):
            continue
        if row.get("pool_enabled") is False:
            continue
        acc_id = int(row.get("id") or 0)
        email = str(row.get("email") or "")
        token = str(row.get("access_token") or "").strip()
        if not email or not token:
            invalid += 1
            continue
        status = str(row.get("quota_check_status") or "")
        if status in ("queued", "running"):
            busy += 1
            continue
        last_ok_ts = _parse_ts(row.get("quota_last_success_at") or row.get("quota_checked_at"))
        if last_ok_ts is not None and (now - last_ok_ts).total_seconds() < stale_after:
            fresh += 1
            continue
        scanned += 1
        queued += 1
        results.append(
            enqueue_account_quota_check(account_id=acc_id, email=email, access_token=token)
        )
    return {
        "ok": True,
        "scanned": scanned,
        "queued": queued,
        "skipped_busy": busy,
        "skipped_fresh": fresh,
        "skipped_invalid": invalid,
        "results": results,
    }


# ============================================================
# 主动巡检后台线程
# ============================================================

def start_probe_loop() -> threading.Thread | None:
    """启动号池主动巡检 daemon 线程（幂等，重复调用只启动一次）。"""
    global _PROBE_THREAD
    with _PROBE_THREAD_LOCK:
        if _PROBE_THREAD is not None and _PROBE_THREAD.is_alive():
            return _PROBE_THREAD
        _PROBE_STOP.clear()
        thread = threading.Thread(target=_probe_loop, name="pool-probe", daemon=True)
        _PROBE_THREAD = thread
        thread.start()
        return thread


def stop_probe_loop() -> None:
    """停止巡检线程（测试用）。"""
    _PROBE_STOP.set()


def _probe_loop() -> None:
    interval = _probe_interval()
    if interval <= 0:
        logger.info("[Pool] 号池巡检间隔为 0，巡检线程不执行")
        return
    logger.info("[Pool] 号池主动巡检线程启动 interval=%ss", interval)
    while not _PROBE_STOP.wait(interval):
        # 每次醒来重新读配置，支持热更新间隔
        interval = _probe_interval()
        if interval <= 0:
            continue
        try:
            result = enqueue_pool_probe()
            if result.get("queued"):
                logger.info("[Pool] 巡检完成 queued=%s", result.get("queued"))
        except Exception:
            logger.exception("[Pool] 号池巡检异常")
