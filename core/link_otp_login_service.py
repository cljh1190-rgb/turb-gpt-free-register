# -*- coding: utf-8 -*-
"""Protocol login: mailbox link OTP -> ChatGPT session token -> PLUS query."""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import unquote, urlsplit

from core import db
from core.account_export import fetch_session, follow_oauth_callback
from core.chatgpt_auth import get_csrf_token, get_providers, signin_openai
from core.chatgpt_plan import check_account_plan, token_claims
from core.mail_archive_viewer import fetch_mail_archive, mask_mail_url
from core.openai_auth import (
    EmailOtpInvalidError,
    follow_authorize,
    network_preflight,
    send_email_otp,
    validate_email_otp,
)
from core.otp_utils import extract_otp, looks_like_openai_email
from core.session import BrowserSession

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_LOCK = threading.RLock()
_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="link-otp-login")
_JOBS: dict[str, dict] = {}
_PAUSE_EVENT = threading.Event()
_PAUSE_EVENT.set()
_PAUSED = False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_public_text(value, *secrets) -> str:
    """Remove private URLs and credentials from messages exposed by the jobs API."""
    text = str(value or "")
    for secret in secrets:
        secret_text = str(secret or "").strip()
        if secret_text:
            text = text.replace(secret_text, "[已隐藏]")
    text = re.sub(r"(?:https?|socks5h?)://\S+", "[链接已隐藏]", text, flags=re.IGNORECASE)
    return text


def _close_browser_session(session: BrowserSession | None) -> None:
    if session is None:
        return
    close = getattr(session, "close", None)
    if callable(close):
        close()
        return
    inner = getattr(session, "session", None)
    close = getattr(inner, "close", None)
    if callable(close):
        close()


def _create_preflight_session(*, proxy: str | None, progress) -> tuple[BrowserSession, str, bool]:
    """优先代理预检；ThorData 强制模式下绝不回退本机直连。"""
    session: BrowserSession | None = None
    first_error: Exception | None = None
    try:
        session = BrowserSession(proxy=proxy)
        using_proxy = bool(getattr(session, "proxy", ""))
        progress(
            stage="starting",
            network_mode="proxy" if using_proxy else "direct",
            fallback_used=False,
            message="正在使用配置代理/IP进行网络预检" if using_proxy else "正在使用本机直连进行网络预检",
        )
        network_preflight(session)
        return session, "proxy" if using_proxy else "direct", False
    except Exception as exc:
        first_error = exc
        selected_proxy = str(getattr(session, "proxy", "") or "") if session is not None else ""
        should_fallback = bool(selected_proxy) or (session is None and proxy != "")
        _close_browser_session(session)
        session = None
        try:
            from config import proxy as proxy_cfg
            if bool(proxy_cfg.proxy_required()):
                raise RuntimeError(
                    f"ThorData 代理预检失败，已禁止切换本机直连："
                    f"{type(first_error).__name__}: {_safe_public_text(first_error)[:180]}"
                ) from first_error
        except ImportError:
            pass
        if not should_fallback:
            raise

    progress(
        stage="starting",
        network_mode="direct",
        fallback_used=True,
        message="代理/IP预检失败，已切换本机直连",
    )
    direct_session: BrowserSession | None = None
    try:
        direct_session = BrowserSession(proxy="")
        network_preflight(direct_session)
        return direct_session, "direct", True
    except Exception as direct_error:
        _close_browser_session(direct_session)
        first_message = _safe_public_text(first_error)
        direct_message = _safe_public_text(direct_error)
        raise RuntimeError(
            f"代理/IP预检失败，切换本机直连后仍不可用："
            f"代理={type(first_error).__name__}: {first_message[:120]}；"
            f"直连={type(direct_error).__name__}: {direct_message[:120]}"
        ) from direct_error


def parse_link_login_input(text: str, *, max_records: int = 50) -> dict:
    records: list[dict] = []
    errors: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line_no, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        url_match = re.search(r"https?://\S+", line, re.IGNORECASE)
        if not url_match:
            errors.append({"line_no": line_no, "reason": "未找到邮箱查看链接"})
            continue
        mail_url = url_match.group(0).strip().rstrip(",;")
        parsed_url = urlsplit(mail_url)
        if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
            errors.append({"line_no": line_no, "reason": "邮箱查看链接无效"})
            continue
        prefix = line[:url_match.start()]
        email_match = _EMAIL_RE.search(prefix)
        password = ""
        if email_match:
            email = email_match.group(0)
            for delimiter in ("----", "===="):
                if delimiter in prefix:
                    prefix_parts = [part.strip() for part in prefix.split(delimiter)]
                    if len(prefix_parts) >= 3 and prefix_parts[0].lower() == email.lower():
                        password = prefix_parts[1]
                    elif len(prefix_parts) == 2 and prefix_parts[0].lower() == email.lower():
                        # Compatibility with email----password---https://...
                        password = re.sub(r"(?:---|===)$", "", prefix_parts[1]).strip()
                    break
        else:
            tail = unquote(parsed_url.path.rstrip("/").rsplit("/", 1)[-1])
            tail_match = _EMAIL_RE.search(tail)
            email = tail_match.group(0) if tail_match else ""
        if not email:
            errors.append({"line_no": line_no, "reason": "无法从该行或链接末尾识别邮箱地址"})
            continue
        key = (email.lower(), mail_url)
        if key in seen:
            continue
        if len(records) >= max_records:
            errors.append({"line_no": line_no, "reason": f"单次最多提交 {max_records} 个账号"})
            break
        seen.add(key)
        records.append({"line_no": line_no, "email": email, "password": password, "mail_url": mail_url})
    return {"records": records, "errors": errors, "count": len(records)}


def _public_job(job: dict) -> dict:
    public = {
        key: value for key, value in job.items()
        if key not in {"mail_url", "access_token", "proxy", "_pause_snapshot"}
    }
    secrets = (job.get("mail_url"), job.get("proxy"), job.get("access_token"))
    for key in ("message", "error"):
        if key in public:
            public[key] = _safe_public_text(public[key], *secrets)
    return public


def _update_job(job_id: str, **updates) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = _now()


def _pause_checkpoint(job_id: str) -> None:
    """在协议步骤之间协作式暂停；当前网络请求完成后进入等待。"""
    if _PAUSE_EVENT.is_set():
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job or job.get("status") in {"success", "failed"}:
            return
        if not isinstance(job.get("_pause_snapshot"), dict):
            job["_pause_snapshot"] = {
                "status": "running" if job.get("started_at") else "queued",
                "message": job.get("message") or "正在处理",
            }
        job["status"] = "paused"
        job["message"] = "任务已暂停，点击继续后从当前阶段恢复"
        job["updated_at"] = _now()

    while not _PAUSE_EVENT.wait(timeout=0.5):
        with _LOCK:
            if job_id not in _JOBS:
                return

    with _LOCK:
        job = _JOBS.get(job_id)
        if not job or job.get("status") in {"success", "failed"}:
            return
        snapshot = job.pop("_pause_snapshot", {})
        job["status"] = "running" if job.get("started_at") else str(snapshot.get("status") or "queued")
        job["message"] = str(snapshot.get("message") or "继续执行")
        job["updated_at"] = _now()


def _latest_openai_otp(mail_url: str) -> tuple[str | None, dict]:
    archive = fetch_mail_archive(mail_url, timeout=18)
    if not archive.get("ok"):
        return None, archive
    for message in archive.get("messages") or []:
        item = {
            "subject": message.get("subject") or "",
            "from": message.get("from") or "",
            "text": message.get("body") or "",
            "content": message.get("body") or "",
        }
        if not looks_like_openai_email(item):
            continue
        code = extract_otp(item)
        if code:
            return code, archive
    return None, archive


def _wait_for_new_otp(
    mail_url: str,
    *,
    baseline: str | None,
    max_wait: int = 150,
    poll_interval: int = 3,
    control=None,
) -> str:
    control = control or (lambda: None)
    deadline = time.time() + max_wait
    last_error = ""
    while time.time() < deadline:
        pause_started = time.time()
        control()
        deadline += max(0.0, time.time() - pause_started)
        code, archive = _latest_openai_otp(mail_url)
        if code and code != baseline:
            return code
        if not archive.get("ok"):
            last_error = str(archive.get("error") or "邮箱链接读取失败")
        pause_started = time.time()
        control()
        deadline += max(0.0, time.time() - pause_started)
        time.sleep(poll_interval)
    suffix = f"；{last_error}" if last_error else ""
    raise TimeoutError(f"等待邮箱链接出现新的 OpenAI 验证码超时{suffix}")


def _finish_oauth_session(session: BrowserSession, continue_url: str, email: str, *, control=None) -> tuple[dict, str]:
    control = control or (lambda: None)
    if not continue_url:
        raise RuntimeError("验证码验证成功，但响应缺少 OAuth 回调地址")
    last_error: Exception | None = None
    for attempt in range(1, 6):
        control()
        try:
            follow_oauth_callback(
                session,
                continue_url,
                referer="https://auth.openai.com/email-verification",
            )
            session_info = fetch_session(session)
            token = str(session_info.get("accessToken") or "").strip()
            if not token:
                raise RuntimeError("ChatGPT session 未返回 accessToken")
            return session_info, token
        except Exception as exc:
            last_error = exc
            if attempt < 5:
                control()
                time.sleep(min(8, 2 ** (attempt - 1)))
    raise RuntimeError(f"OAuth 回调后获取登录 Token 失败：{type(last_error).__name__}: {str(last_error)[:160]}")


def _plus_status(plan: dict) -> str:
    if not plan.get("ok"):
        return "failed"
    value = str(plan.get("current_plan_type") or "").lower()
    if "plus" in value:
        return "opened"
    if any(name in value for name in ("pro", "team", "go", "enterprise")):
        return "other_paid"
    return "not_opened"


def login_and_query_by_mail_link(
    *,
    email: str,
    mail_url: str,
    proxy: str | None = None,
    progress=None,
    control=None,
) -> dict:
    """Perform a login-only OpenAI protocol flow and query the account plan."""
    progress = progress or (lambda **_kwargs: None)
    control = control or (lambda: None)
    control()
    baseline_otp, _archive = _latest_openai_otp(mail_url)
    session: BrowserSession | None = None
    try:
        control()
        session, network_mode, fallback_used = _create_preflight_session(proxy=proxy, progress=progress)
        control()
        get_providers(session)
        control()
        csrf_token = get_csrf_token(session)
        control()
        authorize_url = signin_openai(session, csrf_token, email)

        control()
        progress(stage="sending_otp", message="提交邮箱并触发登录验证码")
        follow_authorize(session, authorize_url)

        validate_result = None
        current_baseline = baseline_otp
        for attempt in range(1, 4):
            control()
            progress(stage="waiting_otp", message=f"等待邮箱链接返回新验证码（{attempt}/3）")
            otp = _wait_for_new_otp(mail_url, baseline=current_baseline, control=control)
            control()
            progress(stage="validating_otp", message=f"提交邮箱验证码（{attempt}/3）")
            try:
                validate_result = validate_email_otp(session, otp)
                break
            except EmailOtpInvalidError:
                if attempt >= 3:
                    raise
                current_baseline = otp
                control()
                send_email_otp(session)
        if validate_result is None:
            raise RuntimeError("邮箱验证码验证未完成")

        page = validate_result.get("page") if isinstance(validate_result, dict) else {}
        page = page if isinstance(page, dict) else {}
        page_type = str(page.get("type") or "")
        continue_url = (
            validate_result.get("continue_url")
            or validate_result.get("external_url")
            or validate_result.get("url")
            or page.get("continue_url")
            or page.get("external_url")
            or page.get("url")
        )
        if page_type in {"about_you", "about-you"} or "about-you" in str(continue_url or ""):
            raise RuntimeError("该邮箱尚未完成 ChatGPT 账号创建，当前协议仅登录查询，不会自动注册")
        if not continue_url:
            raise RuntimeError(f"验证码后未获得登录回调地址：page_type={page_type or 'unknown'}")

        control()
        progress(stage="logging_in", message="完成 OAuth 回调并获取 OpenAI Token")
        _session_info, access_token = _finish_oauth_session(session, str(continue_url), email, control=control)

        control()
        progress(stage="querying_plan", message="登录成功，正在查询 PLUS 套餐")
        plan = check_account_plan(access_token, proxy="", timezone_offset_min="-")
        control()
        claims = token_claims(access_token)
        saved = db.upsert_plus_check_accounts([{
            "email": email,
            "access_token": access_token,
            "user_name": claims.get("user_name") or "",
            "user_id": claims.get("user_id"),
            "account_id": claims.get("account_id"),
            "plan_type": claims.get("claim_plan_type"),
            "detected_format": "email_link_protocol_login",
            "synthetic_email": False,
        }])
        account_id = int((saved[0] if saved else {}).get("id") or 0)
        if account_id:
            db.update_account_plan_check(acc_id=account_id, result=plan)
        if not plan.get("ok"):
            raise RuntimeError(f"登录成功但套餐查询失败：{plan.get('error') or '未知错误'}")
        return {
            "ok": True,
            "email": email,
            "account_id": account_id,
            "plan_type": plan.get("current_plan_type") or claims.get("claim_plan_type") or "unknown",
            "plus_status": _plus_status(plan),
            "has_active_subscription": bool(plan.get("has_active_subscription")),
            "plus_trial_eligible": bool(plan.get("plus_trial_eligible")),
            "checked_at": plan.get("checked_at") or _now(),
            "network_mode": network_mode,
            "fallback_used": fallback_used,
        }
    finally:
        try:
            _close_browser_session(session)
        except Exception:
            pass


def _run_job(job_id: str) -> None:
    with _LOCK:
        job = dict(_JOBS.get(job_id) or {})
    if not job:
        return
    try:
        _update_job(job_id, status="running", started_at=_now())
        _pause_checkpoint(job_id)
        result = login_and_query_by_mail_link(
            email=job["email"],
            mail_url=job["mail_url"],
            proxy=job.get("proxy"),
            progress=lambda **updates: _update_job(job_id, status="running", **updates),
            control=lambda: _pause_checkpoint(job_id),
        )
        _update_job(
            job_id,
            status="success",
            stage="completed",
            message="登录并查询完成",
            completed_at=_now(),
            **result,
        )
    except Exception as exc:
        safe_error = _safe_public_text(exc, job.get("mail_url"), job.get("proxy"))
        logger.warning("[LinkOTPLogin] failed email=%s error=%s: %s", job.get("email"), type(exc).__name__, safe_error[:180])
        _update_job(
            job_id,
            status="failed",
            stage="failed",
            message="登录查询失败",
            error=f"{type(exc).__name__}: {safe_error[:220]}",
            completed_at=_now(),
        )


def enqueue_link_login_queries(text: str, *, proxy: str | None = None) -> dict:
    parsed = parse_link_login_input(text)
    accepted: list[dict] = []
    for record in parsed.get("records") or []:
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "email": record["email"],
            "mail_url": record["mail_url"],
            "mail_url_masked": mask_mail_url(record["mail_url"]),
            "proxy": proxy,
            "status": "queued",
            "stage": "queued",
            "message": "等待执行",
            "error": "",
            "created_at": _now(),
            "updated_at": _now(),
            "completed_at": None,
        }
        with _LOCK:
            _JOBS[job_id] = job
        accepted.append(_public_job(job))
        _EXECUTOR.submit(_run_job, job_id)
    return {
        "ok": bool(accepted),
        "accepted": accepted,
        "accepted_count": len(accepted),
        "errors": parsed.get("errors") or [],
    }


def list_link_login_jobs() -> list[dict]:
    with _LOCK:
        jobs = sorted(_JOBS.values(), key=lambda item: item.get("created_at") or "", reverse=True)
        return [_public_job(dict(job)) for job in jobs]


def link_login_pause_state() -> dict:
    with _LOCK:
        counts = {"queued": 0, "running": 0, "pausing": 0, "paused": 0}
        for job in _JOBS.values():
            status = str(job.get("status") or "")
            if status in counts:
                counts[status] += 1
        return {"paused": bool(_PAUSED), "counts": counts}


def pause_link_login_jobs() -> dict:
    """暂停队列；运行任务会在当前网络请求结束后的下一个检查点停住。"""
    global _PAUSED
    with _LOCK:
        _PAUSED = True
        _PAUSE_EVENT.clear()
        affected = 0
        for job in _JOBS.values():
            if job.get("status") != "running":
                continue
            if not isinstance(job.get("_pause_snapshot"), dict):
                job["_pause_snapshot"] = {
                    "status": "running",
                    "message": job.get("message") or "正在处理",
                }
            job["status"] = "pausing"
            job["message"] = "暂停请求已发送，等待当前网络请求结束"
            job["updated_at"] = _now()
            affected += 1
        queued = sum(1 for job in _JOBS.values() if job.get("status") == "queued")
    return {"ok": True, "paused": True, "affected_running": affected, "queued": queued}


def resume_link_login_jobs() -> dict:
    """继续所有暂停或等待中的协议登录任务。"""
    global _PAUSED
    with _LOCK:
        _PAUSED = False
        resumed = 0
        for job in _JOBS.values():
            if job.get("status") not in {"paused", "pausing"}:
                continue
            if job.get("status") == "pausing":
                # 尚在网络请求中、还没进入等待点：直接撤销暂停请求并恢复快照。
                snapshot = job.pop("_pause_snapshot", {})
                job["status"] = "running" if job.get("started_at") else str(snapshot.get("status") or "queued")
                job["message"] = str(snapshot.get("message") or "继续执行")
                job["updated_at"] = _now()
            # 已经 paused 的任务由等待线程在 Event 唤醒后恢复，不能提前清除快照。
            resumed += 1
        _PAUSE_EVENT.set()
    return {"ok": True, "paused": False, "resumed": resumed}


def clear_link_login_jobs(*, completed_only: bool = True) -> int:
    with _LOCK:
        removable = [
            job_id for job_id, job in _JOBS.items()
            if not completed_only or job.get("status") in {"success", "failed"}
        ]
        for job_id in removable:
            _JOBS.pop(job_id, None)
        return len(removable)
