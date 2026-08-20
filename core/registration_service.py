# -*- coding: utf-8 -*-
"""
注册任务服务层：
    - 线程池并发执行 run_registration
    - 每个任务在 data/registration_jobs.json 里有一条记录
    - 每个任务的日志写到 data/logs/<job_uuid>.log，便于 Web UI 实时尾巴

使用：
    submit_registration(email_source="outlook", count=5)
    → 创建 5 个任务，丢入线程池，立即返回 [job_dict, ...]
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from core import codex_retry_service, db

logger = logging.getLogger(__name__)

# 全局线程池，最大并发数（WebUI 每次提交时可按最新 workers 重建）
# 默认 3：批量注册应并行；单次提交 workers=1 不应永久把池锁成串行（见 get_executor）。
_DEFAULT_MAX_WORKERS = 3
_MIN_MAX_WORKERS = 1
_MAX_MAX_WORKERS = 16
_executor: ThreadPoolExecutor | None = None
_executor_workers = _DEFAULT_MAX_WORKERS
_executor_generation = 0
_retired_executors: list[ThreadPoolExecutor] = []
_executor_lock = threading.RLock()

_STOP_EVENTS: dict[int, threading.Event] = {}
_ACTIVE_JOBS: set[int] = set()
_STOP_LOCK = threading.Lock()
_THREAD_CTX = threading.local()


class StopRequested(RuntimeError):
    """用户手动停止注册任务。"""


def _activate_job(job_id: int) -> None:
    _THREAD_CTX.job_id = int(job_id)
    with _STOP_LOCK:
        _STOP_EVENTS.setdefault(int(job_id), threading.Event())
        _ACTIVE_JOBS.add(int(job_id))


def _deactivate_job(job_id: int) -> None:
    with _STOP_LOCK:
        _STOP_EVENTS.pop(int(job_id), None)
        _ACTIVE_JOBS.discard(int(job_id))
    try:
        delattr(_THREAD_CTX, "job_id")
    except Exception:
        pass


def is_job_active(job_id: int | None) -> bool:
    """判断任务是否有真实的活跃线程实例（即还在内存 _ACTIVE_JOBS 集合中）。"""
    if not job_id:
        return False
    with _STOP_LOCK:
        return int(job_id) in _ACTIVE_JOBS


def is_stop_requested(job_id: int | None = None) -> bool:
    if job_id is None:
        job_id = getattr(_THREAD_CTX, "job_id", None)
    if not job_id:
        return False
    with _STOP_LOCK:
        ev = _STOP_EVENTS.get(int(job_id))
        if ev and ev.is_set():
            return True
    job = db.get_job(int(job_id))
    return bool(job and job.get("status") in ("stopping", "stopped", "cancelled"))


def check_stop_requested() -> None:
    job_id = getattr(_THREAD_CTX, "job_id", None)
    if is_stop_requested(job_id):
        raise StopRequested(f"任务 #{job_id} 已被用户手动停止")


def _append_job_log(job_id: int, message: str, tag: str = "manual-stop") -> None:
    try:
        job = db.get_job(job_id)
        log_file = job.get("log_file") if job else None
        if not log_file:
            return
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H:%M:%S")
        with Path(log_file).open("a", encoding="utf-8") as f:
            f.write(f"{ts} [WARNING] [{tag}] {message}\n")
    except Exception:
        pass


def _random_display_name() -> str:
    """生成符合 OpenAI 限制的英文字母显示名。"""
    import random
    import string

    first = random.choice(string.ascii_uppercase) + "".join(
        random.choices(string.ascii_lowercase, k=random.randint(3, 6))
    )
    last = random.choice(string.ascii_uppercase) + "".join(
        random.choices(string.ascii_lowercase, k=random.randint(3, 6))
    )
    return f"{first} {last}"


def _prepare_registration_args(
    assigned_email: str | None = None,
    assigned_source: str | None = None,
) -> tuple[str, str, str]:
    """复用 CLI 的默认规则，为旧 Web 任务入口补齐注册参数。"""
    # 用模块属性读，支持 WebUI 热加载
    from config import register as _r, email as _e
    from core.email_provider import acquire_email
    from core.profile_utils import generate_random_birthday

    assigned_email = str(assigned_email or "").strip()
    assigned_source = str(assigned_source or "").strip().lower()
    email = assigned_email or str(getattr(_r, "REGISTER_EMAIL", "") or "").strip()
    name = str(getattr(_r, "REGISTER_NAME", "") or "").strip()
    # WebUI/配置里有时会把空值存成 "-"，这不是合法 OpenAI 显示名，按空处理并自动生成
    if name in {"-", "—", "无", "空", "none", "None", "null", "NULL"}:
        name = ""

    if not name:
        if _e.USE_EMAIL_SERVICE:
            name = _random_display_name()
        else:
            raise RuntimeError("Web 任务入口无法交互输入名称，请在 config.REGISTER_NAME 配置显示名")

    birthday = generate_random_birthday()

    # 指定邮箱链接注册必须领取任务绑定的邮箱，不能再按 EMAIL_SOURCE 顺序随机取池。
    if assigned_email and assigned_source == "generic_api":
        row = db.claim_generic_api_email(assigned_email)
        if row is None:
            current = db.get_generic_api_email_by_email(assigned_email)
            status = str((current or {}).get("status") or "不存在")
            raise RuntimeError(f"指定邮箱不可用：{assigned_email}（状态：{status}）")
        email = str(row.get("email") or assigned_email).strip()
    # 邮箱领取会把池状态置为 used，因此放在所有其他准备逻辑之后。
    elif not email:
        if _e.USE_EMAIL_SERVICE:
            email = acquire_email()
        else:
            raise RuntimeError("Web 任务入口无法交互输入邮箱，请在 config.REGISTER_EMAIL 配置邮箱")

    return email, name, birthday


def _release_unconsumed_job_email(email: str | None, reason: str) -> None:
    """任务失败兜底：只回收尚未生成账号、仍处于 used 的邮箱领取。"""
    if not email:
        return
    try:
        from core.email_provider import release_email_if_unconsumed

        release_email_if_unconsumed(email, note=f"任务未消耗，已自动回收: {reason[:180]}")
    except Exception:
        logger.exception("[Service] 回收未消耗邮箱失败: %s", email)


def _is_final_session_access_token_timeout(error: object) -> bool:
    """
    识别注册最后一步已经返回 /api/auth/session 200 但没有 accessToken 的失败。
    这种邮箱后续继续注册通常会卡在同一状态，按要求直接停用邮箱池条目。
    """
    text = str(error or "")
    if not text:
        return False
    return (
        "等待 /api/auth/session accessToken 超时" in text
        and "WARNING_BANNER" in text
        and "'_http_status': 200" in text
    )


def _should_disable_failed_registration_email(error: object) -> bool:
    """需要直接停用邮箱的注册失败类型。"""
    text = str(error or "")
    if not text:
        return False
    return (
        _is_final_session_access_token_timeout(text)
        or "邮箱提交后进入登录密码页" in text
        or "auth.openai.com/log-in/password" in text
        or "/log-in/password" in text
        or "user_already_exists" in text
    )


def _disable_job_email(email: str | None, reason: str) -> bool:
    """把本次任务邮箱停用，避免后续再次领取。"""
    if not email:
        return False
    try:
        from core.email_provider import release_email

        source = release_email(email, status="disabled", note=f"自动停用: {reason[:180]}")
        logger.warning("[Service] 已自动停用邮箱: source=%s email=%s reason=%s", source, email, reason[:220])
        return True
    except Exception:
        logger.exception("[Service] 自动停用邮箱失败: %s", email)
        return False


def _normalize_workers(max_workers: int | None) -> int:
    if max_workers is None:
        return _DEFAULT_MAX_WORKERS
    try:
        value = int(max_workers)
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_WORKERS
    return max(_MIN_MAX_WORKERS, min(_MAX_MAX_WORKERS, value))


def resolve_submit_workers(*, count: int = 1, workers: int | None = None) -> int:
    """解析本次提交应用的并发数。

    规则：
    - 显式 workers 优先，夹在 1~16；
    - 批量 count>=2 且请求 workers<=1 时，自动抬到 min(count, 默认3)，避免「开了 5 个任务却串行」；
    - 单任务 count==1 允许 workers=1（不浪费浏览器），但不因此永久锁死后续批量（见 get_executor 的不降级策略）。
    """
    requested = _normalize_workers(workers if workers is not None else _DEFAULT_MAX_WORKERS)
    try:
        n = max(1, int(count or 1))
    except (TypeError, ValueError):
        n = 1
    if n >= 2 and requested <= 1:
        bumped = min(n, _DEFAULT_MAX_WORKERS)
        logger.warning(
            "[Service] 批量 count=%s 但 workers=%s，自动改为 %s 以启用并发",
            n, requested, bumped,
        )
        return bumped
    if n >= 2:
        # 批量时 workers 至少 2，且不超过任务数（多余线程无意义）
        return max(2, min(requested, n, _MAX_MAX_WORKERS))
    return requested


def get_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    """返回注册线程池。

    旧逻辑只在首次创建线程池时使用 max_workers，后续 WebUI 改线程数再提交仍会复用
    上一次的池。这里改成：每次传入的 max_workers 和当前池不一致时，立即创建新池供
    新提交任务使用；旧池不接收新任务，但会继续把已经排队/运行的任务跑完。

    重要：单次「workers=1」的提交**不会把已有更大的池降级**。
    否则 UI 误传 1 或单任务调试后，后续批量会一直串行（日志里 workers=1 的根因）。
    只有明确请求更大并发，或当前池尚不存在时，才按请求值建池；
    若请求值更小且池已存在，保持现有池容量，仅用现有池跑（多出来的线程空闲即可）。
    """
    global _executor, _executor_workers, _executor_generation
    requested_workers = _normalize_workers(max_workers) if max_workers is not None else _executor_workers
    with _executor_lock:
        if _executor is None:
            _executor_workers = requested_workers
            _executor_generation += 1
            _executor = ThreadPoolExecutor(
                max_workers=requested_workers,
                thread_name_prefix=f"reg-worker-{_executor_generation}",
            )
            logger.info("[Service] 创建注册线程池 workers=%s", requested_workers)
            return _executor

        if requested_workers > _executor_workers:
            # 升配：建更大的新池，旧池把在途任务跑完
            old_executor = _executor
            old_executor.shutdown(wait=False, cancel_futures=False)
            _retired_executors.append(old_executor)
            logger.info(
                "[Service] 注册线程池 workers 从 %s 升到 %s；旧池继续处理已排队任务",
                _executor_workers,
                requested_workers,
            )
            _executor_workers = requested_workers
            _executor_generation += 1
            _executor = ThreadPoolExecutor(
                max_workers=requested_workers,
                thread_name_prefix=f"reg-worker-{_executor_generation}",
            )
        elif requested_workers < _executor_workers:
            # 不降级：避免 workers=1 的单次提交把并发锁死
            logger.info(
                "[Service] 本次请求 workers=%s < 当前池 %s，保持不降级（避免并发被锁成串行）",
                requested_workers,
                _executor_workers,
            )
        return _executor


def get_executor_workers() -> int:
    """当前新提交注册任务会使用的线程数。"""
    with _executor_lock:
        return _executor_workers


def shutdown_executor(wait: bool = True) -> None:
    global _executor
    with _executor_lock:
        executors = []
        if _executor is not None:
            executors.append(_executor)
            _executor = None
        executors.extend(_retired_executors)
        _retired_executors.clear()
    for ex in executors:
        ex.shutdown(wait=wait, cancel_futures=False)


# ============================================================
# 单任务执行：日志重定向到任务专属文件
# ============================================================

class _JobLogContext:
    """让本线程的根 logger 多一个 FileHandler，结束后移除。"""

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.handler: logging.FileHandler | None = None

    def __enter__(self):
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self.handler = logging.FileHandler(self.log_path, encoding="utf-8")
        self.handler.setLevel(logging.INFO)
        self.handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        # 仅给本线程过滤 —— 用 thread name 做区分，避免污染其他任务的日志
        thread_name = threading.current_thread().name
        self.handler.addFilter(lambda r: r.threadName == thread_name)
        logging.getLogger().addHandler(self.handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handler is not None:
            self.handler.close()
            logging.getLogger().removeHandler(self.handler)


def _run_one_job(job_id: int, log_file: str) -> None:
    """单任务入口（线程池里跑这个）。"""
    log_logger = logging.getLogger(__name__)
    _activate_job(job_id)

    # 取消检查：用户可能在任务排队期间点了"取消排队"，把 status 改成了 cancelled。
    # 因为 Future 已经 submit 进线程池无法撤回，只能在真正执行前自检一下，跳过 cancelled 的。
    current = db.get_job(job_id)
    if not current:
        log_logger.info(f"[Job {job_id}] 任务记录已删除，跳过执行")
        _deactivate_job(job_id)
        return
    if current.get("status") == "cancelled":
        log_logger.info(f"[Job {job_id}] 已被用户取消，跳过执行")
        _deactivate_job(job_id)
        return

    db.update_job(job_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"))

    email: str | None = None
    try:
        with _JobLogContext(log_file):
            from main import run_registration
            log_logger.info(f"[Job {job_id}] 开始注册任务")
            assigned_email = str(current.get("email") or "").strip()
            assigned_source = str(current.get("email_source") or "").strip()
            email, name, birthday = _prepare_registration_args(
                assigned_email=assigned_email or None,
                assigned_source=assigned_source or None,
            )
            db.update_job(job_id, email=email)
            check_stop_requested()
            result = run_registration(email=email, name=name, birthday=birthday)
            if is_stop_requested(job_id):
                _release_unconsumed_job_email(email, "用户手动停止")
                db.update_job(
                    job_id,
                    status="stopped",
                    error="用户手动停止",
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                log_logger.warning(f"[Job {job_id}] 已按用户请求停止")
                return
            if isinstance(result, dict) and result.get("success"):
                db.update_job(
                    job_id,
                    status="success",
                    email=result.get("email"),
                    account_id=result.get("account_id"),
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                log_logger.info(f"[Job {job_id}] 成功: {result.get('email')}")
            else:
                # 注意：失败也可能伴随 account_id（如 Codex 失败但账号已注册成功）
                err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
                result_email = (result or {}).get("email") if isinstance(result, dict) else None
                db.update_job(
                    job_id,
                    status="failed",
                    email=result_email,
                    account_id=(result or {}).get("account_id") if isinstance(result, dict) else None,
                    error=str(err)[:500],
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                email_to_handle = str(result_email or email or "").strip()
                if _should_disable_failed_registration_email(err):
                    _disable_job_email(email_to_handle, str(err))
                else:
                    _release_unconsumed_job_email(email_to_handle, str(err))
                log_logger.error(f"[Job {job_id}] 失败: {err}")
    except StopRequested as exc:
        _release_unconsumed_job_email(email, str(exc))
        log_logger.warning(f"[Job {job_id}] 已停止: {exc}")
        db.update_job(
            job_id,
            status="stopped",
            error="用户手动停止",
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
    except Exception as exc:
        err_text = f"{type(exc).__name__}: {exc}"
        if _should_disable_failed_registration_email(err_text):
            _disable_job_email(email, err_text)
        else:
            _release_unconsumed_job_email(email, err_text)
        if is_stop_requested(job_id):
            log_logger.warning(f"[Job {job_id}] 停止中捕获异常，按停止处理: {type(exc).__name__}: {exc}")
            db.update_job(
                job_id,
                status="stopped",
                error="用户手动停止",
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            return
        log_logger.exception(f"[Job {job_id}] 异常")
        db.update_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
    finally:
        _deactivate_job(job_id)


def _run_codex_retry_job(job_id: int, log_file: str, email: str, account_id: int) -> None:
    """把 Codex 补跑作为标准任务执行，并复用任务状态、日志和停止入口。"""
    _activate_job(job_id)
    current = db.get_job(job_id)
    if not current or current.get("status") == "cancelled":
        codex_retry_service.release(email)
        _deactivate_job(job_id)
        return

    db.update_job(job_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"))
    try:
        result = codex_retry_service.run_worker(
            email,
            clear_log=False,
            target_log_path=log_file,
        )
        now_iso = datetime.now().isoformat(timespec="seconds")
        if is_stop_requested(job_id) or result.get("status") == "stopped":
            db.update_job(job_id, status="stopped", email=email, account_id=account_id, error=str(result.get("message") or "用户手动停止")[:500], completed_at=now_iso)
        elif result.get("ok"):
            db.update_job(
                job_id,
                status="success",
                email=email,
                account_id=account_id,
                completed_at=now_iso,
            )
        else:
            db.update_job(
                job_id,
                status="failed",
                email=email,
                account_id=account_id,
                error=str(result.get("message") or "Codex 补跑失败")[:500],
                completed_at=now_iso,
            )
    except Exception as exc:
        db.update_job(
            job_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
        codex_retry_service.release(email)
        logger.exception("[Job %s] Codex 补跑异常", job_id)
    finally:
        _deactivate_job(job_id)


# ============================================================
# 公共接口
# ============================================================

def submit_registration(
    count: int = 1,
    email_source: str | None = None,
    workers: int | None = None,
    emails: list[str] | None = None,
) -> list[dict]:
    """
    创建注册任务并提交到线程池。
    emails 非空时，每个任务绑定一个明确邮箱；否则按 count 从配置邮箱源领取。

    Returns:
        N 个新创建的 job dict
    """
    assigned_emails = [str(item or "").strip() for item in (emails or []) if str(item or "").strip()]
    if assigned_emails:
        count = len(assigned_emails)
    if email_source is None:
        from config import email as _email_cfg
        email_source = _email_cfg.EMAIL_SOURCE

    # 创建/切换线程池和提交本批任务必须整体串行化：否则另一请求在本批提交中途
    # 切换 workers 并 shutdown 旧池，会导致后续 submit 报 cannot schedule new futures after shutdown。
    effective_workers = resolve_submit_workers(count=count, workers=workers)
    with _executor_lock:
        executor = get_executor(max_workers=effective_workers)
        pool_workers = get_executor_workers()
        jobs = []
        for index in range(count):
            assigned_email = assigned_emails[index] if assigned_emails else None
            job = db.create_job(
                email_source=email_source,
                email=assigned_email,
            )
            try:
                executor.submit(_run_one_job, job["id"], job["log_file"])
            except Exception as exc:
                db.update_job(
                    int(job["id"]),
                    status="failed",
                    error=f"队列提交失败：{type(exc).__name__}: {exc}"[:500],
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                logger.exception("[Service] 注册任务 #%s 提交线程池失败", job["id"])
            jobs.append(db.get_job(int(job["id"])) or job)
    logger.info(
        f"[Service] 已提交 {count} 个注册任务，源={email_source}，"
        f"请求workers={workers} → 生效={effective_workers}，池大小={pool_workers}"
    )
    return jobs


def _account_for_job(job: dict) -> dict | None:
    account_id = job.get("account_id")
    if account_id is not None:
        try:
            account = db.get_account(int(account_id))
            if account is not None:
                return account
        except (TypeError, ValueError):
            pass
    email = str(job.get("email") or "").strip()
    return db.get_account_by_email(email) if email else None


def get_retry_info(job: dict) -> dict:
    """返回给 API/UI 的重试能力描述，不依赖前端猜测错误阶段。"""
    status = str(job.get("status") or "")
    info = {
        "retryable": False,
        "retry_action": None,
        "retry_label": None,
        "retry_reason": None,
        "display_status": status,
    }
    if status not in ("failed", "stopped", "cancelled"):
        return info

    successful_retry = db.get_successful_retry_for_job(int(job.get("id") or 0))
    if successful_retry is not None:
        info["retry_reason"] = f"后续重试任务 #{successful_retry.get('id')} 已成功"
        info["successful_retry_job_id"] = successful_retry.get("id")
        return info

    account = _account_for_job(job)
    if account and job.get("account_id") is not None and status in ("failed", "stopped"):
        info["display_status"] = "success" if (account.get("codex_status") or "") == "success" else "partial_success"

    if account:
        codex_status = str(account.get("codex_status") or "")
        if codex_status == "deactivated":
            info["retry_reason"] = "账号已废号，不能补跑 Codex"
            return info
        if codex_status == "success":
            info["retry_reason"] = "账号和 Codex 授权均已完成"
            return info
        # 用户没开 Codex 自动授权时，不补跑 Codex（账号已注册成功即可）
        try:
            from config import codex as _codex_cfg
            _codex_auto = bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False))
        except Exception:
            _codex_auto = False
        if not _codex_auto:
            info["retry_reason"] = "Codex 未启用（ENABLE_CODEX_AUTO=False），账号已注册无需补跑"
            return info
        info.update({
            "retryable": True,
            "retry_action": "codex",
            "retry_label": "补跑 Codex",
        })
        return info

    info.update({
        "retryable": True,
        "retry_action": "registration",
        "retry_label": "重试",
    })
    return info


def retry_job(job_id: int, workers: int | None = None) -> dict:
    """智能重试终态任务：未生成账号则重新注册，已有账号则仅补跑 Codex。"""
    source = db.get_job(job_id)
    if source is None:
        return {"ok": False, "error": "任务不存在", "status": 404}

    retry_info = get_retry_info(source)
    if not retry_info["retryable"]:
        reason = retry_info.get("retry_reason") or f"当前状态不支持重试：{source.get('status')}"
        return {"ok": False, "error": reason, "status": 409}

    action = str(retry_info["retry_action"])
    account = _account_for_job(source)
    email = str((account or {}).get("email") or source.get("email") or "").strip()
    account_id = int(account["id"]) if account and account.get("id") is not None else None
    reserved_codex = False
    if action == "codex":
        if not email or account_id is None:
            return {"ok": False, "error": "已注册账号信息不完整，无法补跑 Codex", "status": 409}
        if not codex_retry_service.reserve(email):
            return {"ok": False, "error": "该账号正在补跑 Codex，请稍候", "status": 409}
        reserved_codex = True

    try:
        job, created = db.create_retry_job(
            int(job_id),
            job_type="codex_retry" if action == "codex" else "registration",
            email_source=str(source.get("email_source") or "outlook"),
            email=email if (
                action == "codex"
                or str(source.get("email_source") or "").strip() == "generic_api"
            ) else None,
            account_id=account_id if action == "codex" else None,
        )
    except LookupError as exc:
        if reserved_codex:
            codex_retry_service.release(email)
        return {"ok": False, "error": str(exc), "status": 404}
    except ValueError as exc:
        if reserved_codex:
            codex_retry_service.release(email)
        return {"ok": False, "error": str(exc), "status": 409}

    if not created:
        if reserved_codex:
            codex_retry_service.release(email)
        return {
            "ok": True,
            "created": False,
            "reused": True,
            "message": f"已有重试任务 #{job['id']} 在排队或运行中",
            "source_job_id": int(job_id),
            "retry_action": action,
            "job": job,
        }

    try:
        if action == "codex":
            db.update_account_codex_status(email, "retrying", None)
        with _executor_lock:
            executor = get_executor(max_workers=workers)
            if action == "codex":
                executor.submit(_run_codex_retry_job, job["id"], job["log_file"], email, int(account_id))
            else:
                executor.submit(_run_one_job, job["id"], job["log_file"])
    except Exception as exc:
        if reserved_codex:
            codex_retry_service.release(email)
            db.update_account_codex_status(email, "failed", f"队列提交失败：{type(exc).__name__}: {exc}"[:500])
        db.update_job(
            int(job["id"]),
            status="failed",
            error=f"队列提交失败：{type(exc).__name__}: {exc}"[:500],
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
        logger.exception("[Service] 重试任务 #%s 提交线程池失败", job["id"])
        return {"ok": False, "error": "重试任务创建成功，但提交执行失败", "status": 500, "job": db.get_job(int(job["id"]))}

    return {
        "ok": True,
        "created": True,
        "reused": False,
        "message": f"已创建重试任务 #{job['id']}（{'Codex 补跑' if action == 'codex' else '完整注册'}）",
        "source_job_id": int(job_id),
        "retry_action": action,
        "job": job,
    }


def cancel_pending_jobs() -> int:
    """
    把所有 status=pending 的任务批量改成 cancelled，避免它们被执行。
    已经在 running 的任务不动（线程池中无法中途打断）。
    返回成功取消的数量。

    实际"不执行"的保证在 _run_one_job 开头——它真要跑起来时会先看 status 决定是否跳过。
    """
    jobs = db.list_jobs(limit=1000)
    cancelled = 0
    now_iso = datetime.now().isoformat(timespec="seconds")
    for job in jobs:
        if job.get("status") == "pending":
            db.update_job(
                int(job["id"]),
                status="cancelled",
                completed_at=now_iso,
                error="用户手动取消",
            )
            cancelled += 1
    logger.info(f"[Service] 已取消 {cancelled} 个排队任务")
    return cancelled


def resume_pending_jobs(workers: int | None = None) -> int:
    """进程重启后，把尚未开始的注册任务重新提交到新线程池。"""
    pending = [
        job
        for job in db.list_jobs(limit=1000)
        if job.get("status") == "pending"
        and str(job.get("job_type") or "registration") == "registration"
    ]
    if not pending:
        return 0

    effective_workers = resolve_submit_workers(count=len(pending), workers=workers)
    resumed = 0
    with _executor_lock:
        executor = get_executor(max_workers=effective_workers)
        for job in sorted(pending, key=lambda row: int(row.get("id") or 0)):
            job_id = int(job.get("id") or 0)
            log_file = str(job.get("log_file") or "")
            if job_id <= 0 or not log_file:
                continue
            try:
                executor.submit(_run_one_job, job_id, log_file)
                _append_job_log(
                    job_id,
                    "进程重启后恢复排队任务：已重新提交到注册线程池。",
                    tag="pending-resume",
                )
                resumed += 1
            except Exception:
                logger.exception("[Service] 恢复排队任务 #%s 失败", job_id)
    if resumed:
        logger.warning(
            "[Service] 已恢复 %s 个进程重启前的排队注册任务（workers=%s）",
            resumed,
            effective_workers,
        )
    return resumed


def reap_zombie_jobs() -> int:
    """
    回收“僵尸任务”：磁盘上 status 为 running/stopping，
    但进程内存 _ACTIVE_JOBS 中已无对应线程实例的任务。
    典型来源：WebUI 进程崩溃/重启后内存态丢失，而磁盘记录不会自动改状态，
    导致前台永远显示 running、无法删除/停止。把它们标记为 stopped 以便清理。
    幂等：第二次调用不会重复处理已回收的任务。返回本次回收数量。
    """
    jobs = db.list_jobs(limit=1000)
    now_iso = datetime.now().isoformat(timespec="seconds")
    reaped = 0
    for job in jobs:
        job_id = int(job.get("id") or 0)
        if job_id <= 0:
            continue
        if job.get("status") not in ("running", "stopping"):
            continue
        with _STOP_LOCK:
            active = job_id in _ACTIVE_JOBS
            if not active:
                _STOP_EVENTS.pop(job_id, None)
                _ACTIVE_JOBS.discard(job_id)
        if active:
            continue
        try:
            db.update_job(
                job_id,
                status="stopped",
                completed_at=now_iso,
                error="进程重启/任务实例丢失，僵尸任务已回收",
            )
        except Exception:
            logger.exception("回收僵尸任务 #%s 失败", job_id)
            continue
        _append_job_log(job_id, "进程重启后回收僵尸任务：任务实例已丢失，已标记为已停止。", tag="zombie-reap")
        logger.warning("[Service] 回收僵尸任务 #%s（status=%s）", job_id, job.get("status"))
        reaped += 1
    if reaped:
        logger.info("[Service] 已回收 %s 个僵尸任务", reaped)
    return reaped


def request_stop_job(job_id: int) -> dict:
    """手动停止单个注册任务。pending 直接取消；running 设置停止标记，运行线程会在检查点退出。"""
    job = db.get_job(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在", "status": 404}
    status = job.get("status")
    now_iso = datetime.now().isoformat(timespec="seconds")
    if status == "pending":
        db.update_job(job_id, status="cancelled", completed_at=now_iso, error="用户手动停止/取消排队")
        _append_job_log(job_id, "用户手动停止：任务尚未运行，已取消排队。")
        return {"ok": True, "message": "排队任务已取消", "job_id": job_id, "state": "cancelled"}
    if status in ("success", "failed", "cancelled", "stopped"):
        return {"ok": True, "message": f"任务已结束：{status}", "job_id": job_id, "state": status}
    if status in ("running", "stopping"):
        with _STOP_LOCK:
            active = int(job_id) in _ACTIVE_JOBS
            ev = _STOP_EVENTS.get(int(job_id)) if active else None
            if ev is not None:
                ev.set()
        if not active or ev is None:
            # Web 服务重启、线程异常退出、历史残留 stopping，或之前手动停止时只创建了 stop event
            # 但没有真实线程实例：直接落为 stopped，避免永远卡在“停止中”。
            with _STOP_LOCK:
                _STOP_EVENTS.pop(int(job_id), None)
                _ACTIVE_JOBS.discard(int(job_id))
            db.update_job(
                job_id,
                status="stopped",
                completed_at=now_iso,
                error="用户手动停止（任务实例不存在）",
            )
            _release_unconsumed_job_email(
                str(job.get("email") or "").strip() or None,
                "任务实例不存在，确认未继续执行",
            )
            _append_job_log(job_id, "用户手动停止：未找到运行中的任务实例，已直接标记为已停止。")
            logger.warning("[Service] 用户停止任务 #%s：任务实例不存在，已直接标记 stopped", job_id)
            return {"ok": True, "message": "任务实例不存在，已直接标记为已停止", "job_id": job_id, "state": "stopped"}
        db.update_job(job_id, status="stopping", error="用户手动停止中")
        _append_job_log(job_id, "用户手动停止：已发送停止信号，任务会在当前步骤检查点退出。")
        logger.warning("[Service] 用户请求停止任务 #%s", job_id)
        return {"ok": True, "message": "已发送停止信号", "job_id": job_id, "state": "stopping"}
    return {"ok": False, "error": f"当前状态不支持停止：{status}", "status": 409}


def read_job_log(job_id: int, max_bytes: int = 50_000) -> str:
    """读取任务日志文件最后 max_bytes 字节，给 Web UI 显示。"""
    job = db.get_job(job_id)
    if not job or not job.get("log_file"):
        return ""
    p = Path(job["log_file"])
    if not p.exists():
        return ""
    size = p.stat().st_size
    with p.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    return data.decode("utf-8", errors="replace")
