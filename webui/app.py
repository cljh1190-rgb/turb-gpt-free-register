# -*- coding: utf-8 -*-
"""
Flask 本地控制台。

复用现有后端：
    core.db                     —— 账号 / 邮箱池 / 任务的文件持久化与查询
    core.registration_service   —— 线程池批量注册 + 任务日志
    webui.config_editor         —— 安全读写 config/*.py

所有接口返回 JSON；前端是单文件 templates/index.html（原生 JS + fetch）。
默认绑定 127.0.0.1，仅本地访问。
"""
import logging
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, make_response, redirect, render_template, request

from core import codex_retry_service, db, plan_check_service, quota_check_service, extract_link_service, codex_agent_service, account_pool
from core.account_import_parser import parse_account_import_text
from core.account_archive_parser import AccountArchiveError, normalize_plus_archive
from core.generic_api_mail_client import (
    fetch_latest_otp_from_url,
    mask_code_url,
    parse_generic_api_line,
    parse_otp_viewer_text,
)
from core.mail_archive_viewer import fetch_mail_archive, mask_mail_url, parse_mail_viewer_input
from core.link_otp_login_service import (
    clear_link_login_jobs,
    enqueue_link_login_queries,
    link_login_pause_state,
    list_link_login_jobs,
    pause_link_login_jobs,
    resume_link_login_jobs,
)
from core.plus_check_import_service import import_and_enqueue_plus_accounts
from webui.auth import init_auth, register_auth_routes
from core import registration_service as svc
from core import account_browser_service
from webui import config_editor

logger = logging.getLogger(__name__)


def _account_plan_check_context(account: dict, *, fallback_proxy: str | None = None) -> dict:
    """读取注册时保存的设备画像，供后续套餐查询保持同一环境。"""
    extra: dict = {}
    try:
        raw_extra = account.get("extra_json")
        if isinstance(raw_extra, dict):
            extra = raw_extra
        elif raw_extra:
            parsed = json.loads(str(raw_extra))
            extra = parsed if isinstance(parsed, dict) else {}
    except Exception:
        extra = {}
    profile = extra.get("browser_profile") if isinstance(extra.get("browser_profile"), dict) else None
    timezone_offset_min = "-"
    if profile and profile.get("timezone_offset_minutes") is not None:
        try:
            timezone_offset_min = str(-int(profile.get("timezone_offset_minutes") or 0))
        except (TypeError, ValueError):
            timezone_offset_min = "-"
    return {
        "proxy": account.get("proxy_used") or fallback_proxy,
        "timezone_offset_min": timezone_offset_min,
        "device_id": account.get("device_id") or extra.get("device_id"),
        "browser_profile": profile,
    }

def _pool_source_arg(default: str = "outlook") -> str:
    src = (request.args.get("source") or "").strip()
    if not src and request.method == "POST":
        data = request.get_json(silent=True) or {}
        src = (data.get("source") or data.get("type") or "").strip()
    return src if src in ("all", "outlook", "generic_api", "cloudflare_domain") else default


def _with_pool_source(rows: list[dict], source: str) -> list[dict]:
    out = []
    for r in rows:
        x = dict(r)
        x["source"] = source
        if not x.get("copy_line"):
            x["copy_line"] = x.get("email") or ""
        out.append(x)
    return out




def _matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _paginate_items(items: list[dict], *, page: int, page_size: int) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(500, int(page_size or 50)))
    total = len(items)
    offset = (page - 1) * page_size
    return {
        "ok": True,
        "items": items[offset:offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "offset": offset,
        "limit": page_size,
    }


def _compact_account_for_list(row: dict) -> dict:
    """账号列表轻量对象：只返回当前表格渲染和按钮判断必需字段。

    原则：
    - 不返回完整 Token / Token 预览 / TOTP Secret / Agent Token。
    - 时间戳、错误原因、提链详情等只在前端确实要展示时返回；空值不返回。
    - 复制/下载敏感内容时再通过 /secret 接口按需读取。
    """
    out = {
        "id": row.get("id"),
        "email": row.get("email"),
        "has_access_token": bool(str(row.get("access_token") or "").strip()),
        "totp_enabled": bool(row.get("totp_secret")),
        "codex_agent_has_token": bool(str(row.get("codex_agent_token") or "").strip()),
    }

    # 这些是列表固定列直接展示字段。
    for key in (
        "user_name", "email_source", "note", "archived", "created_at",
        "plan_type", "current_plan_type", "plus_trial_eligible",
        "plus_trial_coupon_state", "plus_trial_coupon_eligible",
        "plus_trial_coupon_error", "plus_yearly_new_user_eligible",
        "plan_check_status", "codex_status", "codex_agent_status",
    ):
        if key in row:
            out[key] = row.get(key)

    if row.get("plan_check_status") in ("queued", "running") or row.get("plan_check_ok") is False:
        out["plan_check_ok"] = row.get("plan_check_ok")

    # 下面字段仅在有值时返回，避免每行堆满 null/空字符串/内部状态。
    optional_keys = (
        # 套餐展示补充：付费到期/折扣/失败原因。
        "plan_check_error", "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        # 提链成功/失败时才需要。
        "extract_link_status", "extract_link_type", "extract_link_message", "extract_link_error",
        "extract_link_long_url", "extract_link_copy_paste", "extract_link_image_url_png",
        "extract_link_image_url_svg", "extract_link_expires_at",
        # Codex / Agent 状态提示。
        "codex_error", "codex_agent_message", "codex_agent_runtime_id",
        "codex_agent_sub2api_url", "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
    )
    for key in optional_keys:
        value = row.get(key)
        if value is not None and value != "":
            out[key] = value
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
    if any(x in plan for x in ("plus", "pro", "team", "go")):
        expire = row.get("expires_at")
        if expire:
            out["expires_at"] = expire
    return out


def _compact_plus_check_account(row: dict) -> dict:
    """独立 PLUS 查询页使用的非敏感账号状态。"""
    keys = (
        "id", "email", "plus_check_imported_at", "plus_check_import_format",
        "plus_check_synthetic_email", "plan_type", "current_plan_type",
        "has_active_subscription", "plus_trial_eligible", "plan_check_status",
        "plus_trial_coupon_state", "plus_trial_coupon_eligible",
        "plus_trial_coupon_error", "plus_yearly_new_user_eligible",
        "plan_check_ok", "plan_check_error", "account_validity", "plan_check_trigger",
        "plan_check_queued_at", "plan_check_started_at", "plan_checked_at",
        "plan_expires_at", "plan_renews_at", "billing_period", "billing_currency",
        "quota_check_status", "quota_check_ok", "quota_check_error", "quota_checked_at",
        "primary_used_percent", "primary_remaining_percent", "primary_limit_window_seconds",
        "primary_reset_after_seconds", "primary_reset_at_iso", "secondary_used_percent",
        "secondary_remaining_percent", "secondary_reset_at_iso", "quota_allowed",
        "quota_limit_reached", "credits_has_credits", "credits_unlimited", "credits_balance",
    )
    out = {key: row.get(key) for key in keys if row.get(key) is not None and row.get(key) != ""}
    out["id"] = row.get("id")
    out["email"] = row.get("email")
    out["has_access_token"] = bool(str(row.get("access_token") or "").strip())
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").strip().lower()
    status = str(row.get("plan_check_status") or "").lower()
    if status in {"queued", "running"}:
        plus_status = "checking"
    elif status == "failed" or row.get("plan_check_ok") is False:
        plus_status = "failed"
    elif "plus" in plan:
        plus_status = "opened"
    elif any(name in plan for name in ("pro", "team", "go", "enterprise")):
        plus_status = "other_paid"
    elif status == "success" or row.get("plan_check_ok") is True:
        plus_status = "not_opened"
    else:
        plus_status = "unknown"
    out["plus_status"] = plus_status
    return out


def _account_secret_value(row: dict, field: str) -> str:
    field = (field or "").strip()
    if field == "access_token":
        return str(row.get("access_token") or "")
    if field == "copy_line":
        return str(row.get("copy_line") or "")
    if field == "codex_agent_token":
        return str(row.get("codex_agent_token") or "")
    raise ValueError("field 仅支持 access_token/copy_line/codex_agent_token")


def _compact_job_for_list(row: dict) -> dict:
    """注册任务列表轻量对象：只返回表格展示和按钮判断需要的字段。"""
    out = {
        "id": row.get("id"),
        "status": row.get("status"),
    }
    for key in (
        "parent_job_id", "retry_attempt", "email", "started_at", "completed_at",
        "display_status", "retryable", "retry_action", "retry_label",
    ):
        value = row.get(key)
        if value is not None and value != "" and value is not False:
            out[key] = value
    err = str(row.get("error_message") or "").strip()
    if err:
        # 列表只需要摘要；完整错误和堆栈看“任务日志”。
        out["error_message"] = err[:240] + ("…" if len(err) > 240 else "")
    return out


def _job_status_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts["active"] = sum(int(counts.get(s, 0) or 0) for s in ("pending", "running", "stopping"))
    return counts

def create_app(auth_code: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    # 内置 link-pp：与注册机共用 5000 端口和进程，不再依赖独立 Docker 服务。
    try:
        from vendor.link_pp.handoff import create_app as create_linkpp_app
        from werkzeug.middleware.dispatcher import DispatcherMiddleware
        app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {
            "/link-pp": create_linkpp_app({"TESTING": False}).wsgi_app,
        })
        logger.info("[link-pp] 已内置挂载到 /link-pp")
    except Exception:
        logger.exception("[link-pp] 内置挂载失败；提链服务将保留外部 API 兼容")
    _prepared_downloads: dict[str, dict] = {}

    def _put_prepared_download(content: bytes, filename: str, mimetype: str = "application/zip") -> str:
        now = time.time()
        # 顺手清理 10 分钟前的临时下载，避免内存堆积。
        for k, v in list(_prepared_downloads.items()):
            if now - float(v.get("created_at") or 0) > 600:
                _prepared_downloads.pop(k, None)
        download_id = uuid.uuid4().hex
        _prepared_downloads[download_id] = {
            "content": bytes(content),
            "filename": filename,
            "mimetype": mimetype,
            "created_at": now,
        }
        return download_id

    @app.get("/api/downloads/<download_id>")
    def api_prepared_download(download_id: str):
        item = _prepared_downloads.pop(str(download_id or ""), None)
        if not item:
            return jsonify({"ok": False, "error": "下载已过期或不存在，请重新生成"}), 404
        content = item.get("content") or b""
        filename = item.get("filename") or "download.zip"
        mimetype = item.get("mimetype") or "application/octet-stream"
        return Response(
            content,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(content)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    init_auth(app, auth_code=auth_code)
    register_auth_routes(app)

    # 初始化代理入口池；健康探测放到任务开始前，避免 WebUI 启动被网络超时阻塞。
    try:
        from config.proxy import THORDATA_COUNTRY, THORDATA_ENABLED, _mask_proxy, ensure_reg_proxy_pool, pick_proxy
        pool = ensure_reg_proxy_pool()
        sample = pick_proxy()
        logger.info(
            "[proxy] 代理入口池已加载 mode=%s count=%s sample=%s country=%s（任务开始前会执行健康探测）",
            "ThorData" if THORDATA_ENABLED else "static/Cliproxy",
            len(pool), _mask_proxy(sample) or "-", str(THORDATA_COUNTRY or "US"),
        )
    except Exception as exc:
        logger.error("[proxy] 代理初始化失败；注册与查询将中止，不会回退本机 IP：%s", exc)

    recovered_plan_checks = db.recover_interrupted_plan_checks()
    if recovered_plan_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的套餐查询状态", recovered_plan_checks)
    recovered_quota_checks = db.recover_interrupted_quota_checks()
    if recovered_quota_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的额度查询状态", recovered_quota_checks)
    recovered_extract_links = db.recover_interrupted_extract_links()
    if recovered_extract_links:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的提链状态", recovered_extract_links)
    recovered_codex_agents = db.recover_interrupted_codex_agents()
    if recovered_codex_agents:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 Codex Agent Token 状态", recovered_codex_agents)

    # 号池主动巡检线程（POOL_ENABLED 且 POOL_PROBE_INTERVAL_SECONDS>0 时生效）
    try:
        account_pool.start_probe_loop()
    except Exception:
        logger.exception("号池巡检线程启动失败")

    # ----------------------------------------------------------
    # 页面
    # ----------------------------------------------------------
    @app.get("/")
    def index():
        requested_ui = (request.args.get("ui") or "").strip().lower()
        if requested_ui in {"legacy", "modern"}:
            ui_mode = requested_ui
        else:
            ui_mode = (request.cookies.get("ui_mode") or "modern").strip().lower()
            if ui_mode not in {"legacy", "modern"}:
                ui_mode = "modern"

        template_name = "index_legacy.html" if ui_mode == "legacy" else "index.html"
        resp = make_response(render_template(template_name))
        if requested_ui in {"legacy", "modern"}:
            resp.set_cookie("ui_mode", ui_mode, max_age=60 * 60 * 24 * 365, samesite="Lax")
        return resp

    @app.get("/plus-handoff")
    def plus_handoff_page():
        return render_template("plus_handoff.html")

    # ----------------------------------------------------------
    # 统计概览
    # ----------------------------------------------------------
    @app.get("/api/summary")
    def api_summary():
        from config import email as _email_cfg
        from core.email_provider import parse_email_sources
        pool = {"total": 0, "available": 0, "used": 0, "failed": 0}
        for src in parse_email_sources(_email_cfg.EMAIL_SOURCE):
            # 临时邮箱地址按需生成，不属于本地邮箱池。
            if src in ("gptmail", "mailnest", "cloudmail", "cloudflare", "throwaway"):
                continue
            one = (
                db.generic_api_email_pool_summary() if src == "generic_api"
                else db.domain_email_pool_summary() if src == "cloudflare_domain"
                else db.outlook_pool_summary()
            )
            for k in pool:
                pool[k] += int(one.get(k, 0) or 0)
        domain_pool = db.domain_email_pool_summary()
        return jsonify({
            "accounts": db.count_accounts(),
            "outlook_total": pool.get("total", 0),
            "outlook_available": pool.get("available", 0),
            "outlook_used": pool.get("used", 0),
            "outlook_failed": pool.get("failed", 0),
            "domain_total": domain_pool.get("total", 0),
            "domain_available": domain_pool.get("available", 0),
            "domain_used": domain_pool.get("used", 0),
            "domain_failed": domain_pool.get("failed", 0),
        })

    @app.post("/api/otp-viewer/fetch")
    def api_otp_viewer_fetch():
        """Immediately fetch OTPs from standalone email----URL entries."""
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or "").strip()
        if not text and data.get("email") and (data.get("code_url") or data.get("url")):
            text = f"{data.get('email')}----{data.get('code_url') or data.get('url')}"

        parsed = parse_otp_viewer_text(text, max_records=100)
        records = parsed.get("records") or []
        if not records:
            return jsonify({
                "ok": False,
                "error": "未识别到可用内容，格式应为：邮箱----取码链接",
                "errors": parsed.get("errors") or [],
            }), 400

        try:
            timeout = max(3, min(int(data.get("timeout", 20)), 30))
        except (TypeError, ValueError):
            timeout = 20

        results: list[dict | None] = [None] * len(records)
        with ThreadPoolExecutor(max_workers=min(8, len(records)), thread_name_prefix="otp-viewer") as executor:
            futures = {
                executor.submit(
                    fetch_latest_otp_from_url,
                    record.get("email") or "",
                    record.get("code_url") or "",
                    timeout=timeout,
                ): (index, record)
                for index, record in enumerate(records)
            }
            for future in as_completed(futures):
                index, record = futures[future]
                try:
                    item = future.result()
                except Exception as exc:
                    logger.warning(
                        "OTP viewer fetch failed for %s: %s",
                        record.get("email") or "-",
                        type(exc).__name__,
                    )
                    item = {
                        "ok": False,
                        "email": record.get("email") or "",
                        "code": None,
                        "http_status": None,
                        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "url": mask_code_url(record.get("code_url") or ""),
                        "error": f"获取失败：{type(exc).__name__}",
                    }
                item["line_no"] = record.get("line_no")
                results[index] = item

        items = [item for item in results if item is not None]
        success_count = sum(1 for item in items if item.get("ok"))
        return jsonify({
            "ok": True,
            "items": items,
            "count": len(items),
            "success_count": success_count,
            "failed_count": len(items) - success_count,
            "errors": parsed.get("errors") or [],
        })

    @app.post("/api/mail-viewer/fetch")
    def api_mail_viewer_fetch():
        """Read all visible messages from one or more mailbox archive URLs."""
        data = request.get_json(silent=True) or {}
        parsed = parse_mail_viewer_input(data.get("text") or data.get("url") or "", max_urls=20)
        records = parsed.get("records") or []
        if not records:
            errors = parsed.get("errors") or []
            reason = str((errors[0] or {}).get("reason") or "未识别到邮箱链接") if errors else "未识别到邮箱链接"
            return jsonify({"ok": False, "error": reason, "errors": errors}), 400
        try:
            timeout = max(3, min(int(data.get("timeout", 20)), 30))
        except (TypeError, ValueError):
            timeout = 20

        results: list[dict | None] = [None] * len(records)
        with ThreadPoolExecutor(max_workers=min(5, len(records)), thread_name_prefix="mail-viewer") as executor:
            futures = {
                executor.submit(fetch_mail_archive, record.get("url") or "", timeout=timeout): (index, record)
                for index, record in enumerate(records)
            }
            for future in as_completed(futures):
                index, record = futures[future]
                try:
                    item = future.result()
                except Exception as exc:
                    logger.warning("Mail viewer fetch failed: %s", type(exc).__name__)
                    item = {
                        "ok": False,
                        "url": mask_mail_url(record.get("url") or ""),
                        "mailbox": "未知邮箱",
                        "messages": [],
                        "message_count": 0,
                        "plus_count": 0,
                        "has_plus": False,
                        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "error": f"获取失败：{type(exc).__name__}",
                    }
                item["line_no"] = record.get("line_no")
                results[index] = item

        items = [item for item in results if item is not None]
        return jsonify({
            "ok": True,
            "items": items,
            "mailbox_count": len(items),
            "message_count": sum(int(item.get("message_count") or 0) for item in items),
            "plus_count": sum(int(item.get("plus_count") or 0) for item in items),
            "failed_count": sum(1 for item in items if not item.get("ok")),
            "errors": parsed.get("errors") or [],
        })

    # ----------------------------------------------------------
    # 已注册账号
    # ----------------------------------------------------------
    @app.get("/api/accounts")
    def api_accounts():
        limit = request.args.get("limit", default=500, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        q = str(request.args.get("q", default="") or "").strip()
        # 新分页接口：传 page/page_size 或 paged=1 时返回 {items,total,page,page_size,...}
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            result = db.list_accounts_page(limit=page_size, offset=offset, archived=archived, plan_filter=plan_filter, q=q)
            result["items"] = [_compact_account_for_list(r) for r in (result.get("items") or [])]
            result.update({"ok": True, "page": page, "page_size": page_size, "compact": True})
            return jsonify(result)
        return jsonify(db.list_accounts(limit=limit, archived=archived, plan_filter=plan_filter, q=q))

    @app.get("/api/accounts/plan-check-status")
    def api_account_plan_check_status():
        """套餐查询轻量状态，不返回 Token、邮箱密码等敏感字段。"""
        limit = request.args.get("limit", default=5000, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        q = str(request.args.get("q", default="") or "").strip()
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            snapshot = db.list_account_plan_check_statuses(limit=page_size, offset=offset, archived=archived, plan_filter=plan_filter, q=q)
            snapshot.update({"page": page, "page_size": page_size})
        else:
            snapshot = db.list_account_plan_check_statuses(limit=max(1, min(5000, limit)), archived=archived, plan_filter=plan_filter, q=q)
        snapshot["queue"] = plan_check_service.queue_settings()
        return jsonify(snapshot)

    @app.get("/api/plus-check/accounts")
    def api_plus_check_accounts():
        """独立 PLUS 查询页列表；不返回 Token 和邮箱密码。"""
        limit = max(1, min(5000, request.args.get("limit", default=500, type=int) or 500))
        q = str(request.args.get("q") or "").strip()
        rows = db.list_plus_check_accounts(limit=limit, q=q)
        return jsonify({
            "ok": True,
            "items": [_compact_plus_check_account(row) for row in rows],
            "total": len(rows),
            "queue": plan_check_service.queue_settings(),
        })

    @app.post("/api/plus-check/login-by-mail-link")
    def api_plus_check_login_by_mail_link():
        """Queue protocol logins using email archive links as the OTP source."""
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "请粘贴邮箱----邮箱链接，或直接粘贴邮箱链接"}), 400
        proxy = data.get("proxy") if "proxy" in data else None
        result = enqueue_link_login_queries(text, proxy=proxy)
        if not result.get("accepted_count"):
            errors = result.get("errors") or []
            reason = str((errors[0] or {}).get("reason") or "未识别到有效邮箱链接") if errors else "未识别到有效邮箱链接"
            return jsonify({**result, "error": reason}), 400
        return jsonify(result), 202

    @app.get("/api/plus-check/login-by-mail-link/jobs")
    def api_plus_check_login_by_mail_link_jobs():
        jobs = list_link_login_jobs()
        return jsonify({"ok": True, "items": jobs, "total": len(jobs), **link_login_pause_state()})

    @app.post("/api/plus-check/login-by-mail-link/pause")
    def api_plus_check_login_by_mail_link_pause():
        return jsonify(pause_link_login_jobs())

    @app.post("/api/plus-check/login-by-mail-link/resume")
    def api_plus_check_login_by_mail_link_resume():
        return jsonify(resume_link_login_jobs())

    @app.post("/api/plus-check/login-by-mail-link/clear")
    def api_plus_check_login_by_mail_link_clear():
        removed = clear_link_login_jobs(completed_only=True)
        return jsonify({"ok": True, "removed_count": removed})

    @app.post("/api/plus-check/import")
    def api_plus_check_import():
        """自动识别账号格式，导入/更新 Token 后立即排队查询 PLUS。"""
        data = request.get_json(silent=True) or {}
        parsed = parse_account_import_text(data.get("text") or "", max_records=500)
        proxy = data.get("proxy") if "proxy" in data else ""
        timezone_offset_min = str(data.get("timezone_offset_min") or "-")
        result, status = import_and_enqueue_plus_accounts(
            parsed,
            proxy=proxy,
            timezone_offset_min=timezone_offset_min,
        )
        return jsonify(result), status

    @app.post("/api/plus-check/import-archive")
    def api_plus_check_import_archive():
        """安全读取 ZIP 内 Sub2 JSON，优先 JSON 并忽略同名四字段 TXT。"""
        uploads = request.files.getlist("files") or request.files.getlist("file")
        if not uploads:
            return jsonify({"ok": False, "error": "请选择 ZIP 压缩包"}), 400
        if len(uploads) > 20:
            return jsonify({"ok": False, "error": "单次最多上传 20 个 ZIP"}), 400

        chunks: list[str] = []
        archive_summaries: list[dict] = []
        for upload in uploads:
            filename = str(upload.filename or "archive.zip")
            if not filename.lower().endswith(".zip"):
                return jsonify({"ok": False, "error": f"仅支持 ZIP 压缩包：{filename}"}), 400
            raw = upload.stream.read(50_000_001)
            if len(raw) > 50_000_000:
                return jsonify({"ok": False, "error": f"ZIP 文件超过 50MB：{filename}"}), 400
            try:
                normalized = normalize_plus_archive(raw)
            except AccountArchiveError as exc:
                return jsonify({"ok": False, "error": f"{filename}：{exc}"}), 400
            chunks.append(normalized.pop("text"))
            normalized["filename"] = filename
            archive_summaries.append(normalized)

        parsed = parse_account_import_text("\n".join(chunks), max_records=500)
        result, status = import_and_enqueue_plus_accounts(parsed, proxy="", timezone_offset_min="-")
        result["archives"] = archive_summaries
        result["archive_count"] = len(archive_summaries)
        return jsonify(result), status

    @app.post("/api/plus-check/check")
    def api_plus_check_recheck():
        """重新查询独立 PLUS 查询页中的一个或多个账号，不触发自动提链。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400
        proxy = data.get("proxy") if "proxy" in data else ""
        timezone_offset_min = str(data.get("timezone_offset_min") or "-")
        started, busy, failed = [], [], []
        seen_ids: set[int] = set()
        for raw_id in ids:
            try:
                acc_id = int(raw_id)
            except (TypeError, ValueError):
                failed.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc or not acc.get("plus_check_imported_at"):
                failed.append({"id": raw_id, "reason": "PLUS 查询账号不存在"})
                continue
            token = str(acc.get("access_token") or "").strip()
            context = _account_plan_check_context(acc, fallback_proxy="")
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="plus_query_manual",
                proxy=proxy if "proxy" in data else "",
                timezone_offset_min=timezone_offset_min if "timezone_offset_min" in data else context["timezone_offset_min"],
                device_id=context["device_id"],
                browser_profile=context["browser_profile"],
            )
            item = {"id": acc.get("id"), "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append({**item, "reason": queued.get("error") or "查询入队失败"})
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
        }), 202

    @app.post("/api/plus-check/quota")
    def api_plus_check_quota():
        """批量查询 ChatGPT/Codex 使用额度。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号额度"}), 400
        started, busy, failed = [], [], []
        seen_ids: set[int] = set()
        for raw_id in ids:
            try:
                acc_id = int(raw_id)
            except (TypeError, ValueError):
                failed.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc or not acc.get("plus_check_imported_at"):
                failed.append({"id": acc_id, "reason": "PLUS 查询账号不存在"})
                continue
            if acc.get("plan_check_status") != "success" or acc.get("plan_check_ok") is not True:
                failed.append({"id": acc_id, "email": acc.get("email"), "reason": "套餐/Token 尚未验证成功，请先重新查询套餐"})
                continue
            queued = quota_check_service.enqueue_account_quota_check(
                account_id=acc_id,
                email=acc.get("email") or "",
                access_token=acc.get("access_token") or "",
            )
            item = {"id": acc_id, "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append({**item, "reason": queued.get("error") or "额度查询入队失败"})
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "queue": quota_check_service.queue_settings(),
        }), 202

    @app.post("/api/plus-check/clear")
    def api_plus_check_clear():
        """清空全部或指定 PLUS 查询池记录；不删除主账号及 Token。"""
        data = request.get_json(silent=True) or {}
        raw_ids = data.get("account_ids") if "account_ids" in data else data.get("ids")
        account_ids = None
        if raw_ids is not None:
            if not isinstance(raw_ids, list) or not raw_ids:
                return jsonify({"ok": False, "error": "account_ids 必须是非空数组；不传表示清空全部"}), 400
            if len(raw_ids) > 5000:
                return jsonify({"ok": False, "error": "单次最多移除 5000 个账号"}), 400
            account_ids = []
            for raw_id in raw_ids:
                try:
                    account_ids.append(int(raw_id))
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": f"账号 ID 非法: {raw_id}"}), 400
        removed = db.clear_plus_check_accounts(account_ids=account_ids)
        return jsonify({
            "ok": True,
            "removed": removed,
            "removed_count": len(removed),
            "accounts_preserved": True,
        })

    @app.get("/api/plus-check/export")
    def api_plus_check_export():
        """导出已实时确认开通 PLUS 的查询池账号。"""
        export_format = str(request.args.get("format") or "txt").strip().lower()
        if export_format not in {"txt", "json"}:
            return jsonify({"ok": False, "error": "format 仅支持 txt/json"}), 400
        rows = db.list_plus_check_accounts(limit=5000)
        plus_rows = [
            row for row in rows
            if row.get("plan_check_status") == "success"
            and row.get("plan_check_ok") is True
            and "plus" in str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
            and str(row.get("access_token") or "").strip()
        ]
        if not plus_rows:
            return jsonify({"ok": False, "error": "查询池中没有已确认开通 PLUS 的账号"}), 400

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if export_format == "json":
            payload = [
                {
                    "email": row.get("email"),
                    "access_token": row.get("access_token"),
                    "plan_type": row.get("current_plan_type") or row.get("plan_type"),
                    "account_id": row.get("account_id"),
                    "expires_at": row.get("plan_expires_at") or row.get("expires_at"),
                    "checked_at": row.get("plan_checked_at"),
                }
                for row in plus_rows
            ]
            content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            filename = f"PLUS_accounts_{stamp}.json"
            mimetype = "application/json; charset=utf-8"
        else:
            lines = [f"{row.get('email') or ''}----{row.get('access_token') or ''}" for row in plus_rows]
            content = ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")
            filename = f"PLUS_accounts_{stamp}.txt"
            mimetype = "text/plain; charset=utf-8"
        return Response(
            content,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(content)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/plus-check/convert-cpa")
    def api_plus_check_convert_cpa():
        """仅导出完整且经 Codex OAuth client_id 校验的 CPA JSON，并打包 ZIP。"""
        import io
        import zipfile
        from core.cpa_converter import build_cpa_auth_file, safe_cpa_filename

        data = request.get_json(silent=True) or {}
        raw_ids = data.get("account_ids") or data.get("ids")
        selected_ids = None
        if raw_ids is not None:
            if not isinstance(raw_ids, list) or not raw_ids:
                return jsonify({"ok": False, "error": "account_ids 必须是非空数组；不传表示转换全部 PLUS"}), 400
            selected_ids = set()
            for raw_id in raw_ids:
                try:
                    selected_ids.add(int(raw_id))
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": f"账号 ID 非法: {raw_id}"}), 400

        rows = db.list_plus_check_accounts(limit=5000)
        plus_rows = [
            row for row in rows
            if (selected_ids is None or int(row.get("id") or 0) in selected_ids)
            and row.get("plan_check_status") == "success"
            and row.get("plan_check_ok") is True
            and "plus" in str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
        ]
        if not plus_rows:
            return jsonify({"ok": False, "error": "没有可转换的已确认 PLUS 账号"}), 400

        local_credentials: dict[str, dict] = {}
        try:
            for item in db.list_codex_accounts():
                email_key = str(item.get("email") or "").strip().lower()
                filename = str(item.get("filename") or "").strip()
                if not email_key or not filename or email_key in local_credentials:
                    continue
                try:
                    content, _ = db.read_codex_credential(filename)
                    parsed = json.loads(content)
                    if isinstance(parsed, dict) and parsed.get("type") == "codex":
                        local_credentials[email_key] = parsed
                except Exception:
                    continue
        except Exception:
            local_credentials = {}

        converted, errors = [], []
        complete_count = 0
        used_names: set[str] = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for row in plus_rows:
                email = str(row.get("email") or "").strip()
                try:
                    auth_file, meta = build_cpa_auth_file(
                        row,
                        fallback_credential=local_credentials.get(email.lower()),
                    )
                    filename = safe_cpa_filename(email, "plus")
                    if filename in used_names:
                        filename = safe_cpa_filename(f"{email}-{row.get('id')}", "plus")
                    used_names.add(filename)
                    zf.writestr(filename, json.dumps(auth_file, ensure_ascii=False, indent=2) + "\n")
                    meta.update({"id": row.get("id"), "filename": filename})
                    converted.append(meta)
                    complete_count += 1
                except Exception as exc:
                    errors.append({"id": row.get("id"), "email": email, "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "source": "plus-check-to-cpa",
                "count": len(converted),
                "complete_count": complete_count,
                "rejected_count": len(errors),
                "files": converted,
                "errors": errors,
                "note": "只导出同一套完整 Codex OAuth access_token/id_token/refresh_token；ChatGPT 网页 Token、缺字段或混合来源凭证会被拒绝，不再生成不可用文件。",
            }
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not converted:
            detail = "；".join(str(item.get("error") or "") for item in errors[:3])
            return jsonify({
                "ok": False,
                "error": "没有可导出的真实 Codex OAuth 凭证。请导入完整 Sub2/CPA OAuth 数据，或先完成正常 Codex 授权。" + (f" 详情：{detail}" if detail else ""),
                "errors": errors,
            }), 400
        zip_bytes = buf.getvalue()
        filename = f"PLUS_CPA_files_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store, max-age=0",
                "X-CPA-Complete-Count": str(complete_count),
                "X-CPA-Rejected-Count": str(len(errors)),
                "X-Content-Type-Options": "nosniff",
            },
        )


    @app.get("/api/accounts/<int:acc_id>/secret")
    def api_account_secret(acc_id: int):
        """按需读取单账号敏感值，避免账号列表一次性下发完整 Token/整行。"""
        field = str(request.args.get("field") or "").strip()
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            value = _account_secret_value(acc, field)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "id": acc_id, "field": field, "value": value})

    @app.post("/api/accounts/secret-bulk")
    def api_accounts_secret_bulk():
        """按需批量读取账号敏感值。Body {account_ids:[...], field}."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        field = str(data.get("field") or "").strip()
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多读取 5000 个账号"}), 400
        values = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            try:
                value = _account_secret_value(acc, field)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            if value:
                values.append({"id": acc_id, "email": acc.get("email"), "value": value})
            else:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "值为空"})
        return jsonify({"ok": True, "field": field, "values": values, "count": len(values), "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/archive")
    def api_account_archive(acc_id: int):
        """归档/取消归档一个账号。Body {archived: true|false}。"""
        data = request.get_json(silent=True) or {}
        archived = bool(data.get("archived", True))
        updated = db.archive_account(acc_id=acc_id, archived=archived)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "archived": archived})

    @app.post("/api/accounts/<int:acc_id>/open-browser")
    def api_account_open_browser(acc_id: int):
        """Open a visible CloakBrowser using the account's saved registration proxy."""
        account = db.get_account(acc_id)
        if not account:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        data = request.get_json(silent=True) or {}
        result = account_browser_service.open_account_browser(
            account,
            allow_rotated_exit=bool(data.get("allow_rotated_exit", False)),
        )
        status = 202 if result.get("accepted") else 200 if result.get("busy") else 400
        return jsonify({"ok": bool(result.get("accepted") or result.get("busy")), **result}), status

    @app.post("/api/accounts/<int:acc_id>/open-billing")
    def api_account_open_billing(acc_id: int):
        """Open the saved account directly on ChatGPT's official Plus page."""
        if not db.get_account(acc_id):
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        data = request.get_json(silent=True) or {}
        from core.billing_handoff import open_billing_handoff
        result = open_billing_handoff(
            acc_id,
            allow_rotated_exit=bool(data.get("allow_rotated_exit", False)),
        )
        status = 202 if result.get("accepted") else 200 if result.get("busy") else 400
        return jsonify({"ok": bool(result.get("accepted") or result.get("busy")), **result}), status

    @app.post("/api/accounts/<int:acc_id>/billing-short-link")
    def api_account_billing_short_link(acc_id: int):
        if not db.get_account(acc_id):
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        from config import billing_handoff as cfg
        from core.plus_handoff_links import create_short_link
        item = create_short_link(account_id=acc_id, target_url=cfg.BILLING_HANDOFF_URL)
        return jsonify({
            "ok": True,
            "account_id": acc_id,
            "url": request.host_url.rstrip("/") + "/p/" + item["code"],
            "expires_at": item["expires_at"],
        })

    @app.get("/api/accounts/<int:acc_id>/billing-payment-link")
    def api_account_billing_payment_link(acc_id: int):
        if not db.get_account(acc_id):
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        state = account_browser_service.account_browser_status(acc_id)
        return jsonify({
            "ok": True,
            "account_id": acc_id,
            "status": state.get("status"),
            "stage": state.get("checkout_stage"),
            "payment_link": state.get("payment_link") or "",
            "payment_status": state.get("payment_status") or "not_started",
            "detected_plan": state.get("detected_plan") or "",
            "error": state.get("error"),
        })

    @app.get("/p/<string:code>")
    def billing_short_link_redirect(code: str):
        from core.plus_handoff_links import resolve_short_link
        item = resolve_short_link(code)
        if not item:
            return "短链接不存在或已过期", 404
        return redirect(item["target_url"], code=302)

    @app.get("/api/accounts/<int:acc_id>/open-browser")
    def api_account_open_browser_status(acc_id: int):
        if not db.get_account(acc_id):
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, **account_browser_service.account_browser_status(acc_id)})

    @app.post("/api/accounts/<int:acc_id>/close-browser")
    def api_account_close_browser(acc_id: int):
        if not db.get_account(acc_id):
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify(account_browser_service.close_account_browser(acc_id))

    @app.post("/api/accounts/archive-bulk")
    def api_accounts_archive_bulk():
        """批量归档/取消归档账号。Body {account_ids:[...], archived:true|false}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        archived = bool(data.get("archived", True))
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多归档 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.archive_accounts(account_ids=account_ids, archived=archived)
        skipped.extend(db_skipped)
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/delete")
    def api_account_delete(acc_id: int):
        """删除一个已注册账号记录。只删除本地保存的账号/token记录，不改邮箱池状态。"""
        deleted = db.delete_account(acc_id=acc_id)
        if not deleted:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "deleted": True})

    @app.post("/api/accounts/delete-bulk")
    def api_accounts_delete_bulk():
        """批量删除已注册账号记录。Body {account_ids: [...]} 或 {ids: [...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        deleted, db_skipped = db.delete_accounts(account_ids=account_ids)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    @app.post("/api/accounts/<int:acc_id>/note")
    def api_account_note(acc_id: int):
        """更新单个已注册账号备注。Body {note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        note = str(data.get("note") or "")
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400
        updated = db.update_account_note(acc_id=acc_id, note=note)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "note": note})

    @app.post("/api/accounts/note-bulk")
    def api_accounts_note_bulk():
        """批量更新已注册账号备注。Body {account_ids: [...], note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        note = str(data.get("note") or "")
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多备注 5000 个账号"}), 400
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400

        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.update_accounts_note(account_ids=account_ids, note=note)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })


    @app.post("/api/accounts/check-plan")
    @app.post("/api/accounts/check-validity")
    def api_account_check_plan():
        """把单账号套餐查询加入后台队列。Body {account_id|email, proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        email = (data.get("email") or "").strip()
        acc = None
        if acc_id is not None:
            try:
                acc = db.get_account(int(acc_id))
            except Exception:
                acc = None
        if acc is None and email:
            acc = db.get_account_by_email(email)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        account_id = int(acc.get("id"))
        context = _account_plan_check_context(acc, fallback_proxy=None)
        queued = plan_check_service.enqueue_account_plan_check(
            account_id=account_id,
            email=acc.get("email") or "",
            access_token=token,
            trigger="validity" if request.path.endswith("check-validity") else "manual",
            proxy=data.get("proxy") if "proxy" in data else "",
            timezone_offset_min=(
                str(data.get("timezone_offset_min") or "-")
                if "timezone_offset_min" in data
                else context["timezone_offset_min"]
            ),
            device_id=context["device_id"],
            browser_profile=context["browser_profile"],
        )
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **queued}), 202

    @app.post("/api/accounts/check-plan-bulk")
    @app.post("/api/accounts/check-validity-bulk")
    def api_accounts_check_plan_bulk():
        """批量把套餐查询加入统一后台队列。Body {account_ids:[...], proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400
        # 与单账号查询保持一致：未传时使用独立网络策略。
        proxy = data.get("proxy") if "proxy" in data else None
        timezone_offset_min = str(data.get("timezone_offset_min") or "-")

        items = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not (acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            items.append(acc)

        started = []
        busy = []
        failed = []
        for acc in items:
            context = _account_plan_check_context(acc, fallback_proxy=None)
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=acc.get("access_token") or "",
                trigger="validity_bulk" if request.path.endswith("check-validity-bulk") else "manual_bulk",
                proxy=proxy if "proxy" in data else "",
                timezone_offset_min=timezone_offset_min if "timezone_offset_min" in data else context["timezone_offset_min"],
                device_id=context["device_id"],
                browser_profile=context["browser_profile"],
            )
            item = {"id": acc.get("id"), "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.get("/api/extract-link/cdk")
    def api_extract_link_cdk():
        """查询当前配置或传入 CDK 的剩余次数。"""
        if not extract_link_service.extraction_enabled():
            return jsonify({"ok": False, "disabled": True, "error": "提链功能已关闭"}), 410
        code = (request.args.get("code") or "").strip() or None
        try:
            return jsonify({"ok": True, **extract_link_service.query_cdk(cdk=code)})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.get("/api/extract-link/health")
    def api_extract_link_health():
        """探测提链远端（BurstPro GET /api/health），并回传规范化后的 api_base/docs_url。"""
        if not extract_link_service.extraction_enabled():
            return jsonify({"ok": False, "disabled": True, "error": "提链功能已关闭"}), 410
        try:
            return jsonify({"ok": True, **extract_link_service.health_check()})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    def _is_extract_eligible(acc: dict) -> bool:
        plan = str(acc.get("current_plan_type") or acc.get("plan_type") or "").lower()
        return plan == "free" and bool(acc.get("plus_trial_eligible"))

    @app.post("/api/accounts/extract-link")
    def api_account_extract_link():
        """单账号提链。Body {account_id|id, link_type?, cdk?}。"""
        if not extract_link_service.extraction_enabled():
            return jsonify({"ok": False, "disabled": True, "error": "提链功能已关闭"}), 410
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        if not _is_extract_eligible(acc):
            return jsonify({"ok": False, "error": "仅支持 free(可Plus试用) 账号提链；请先查询套餐确认资格"}), 400
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = extract_link_service.enqueue_account_extract(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                link_type=data.get("link_type"),
                cdk=data.get("cdk"),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/extract-link-bulk")
    def api_accounts_extract_link_bulk():
        """批量提链。Body {account_ids:[...], link_type?, cdk?}。"""
        if not extract_link_service.extraction_enabled():
            return jsonify({"ok": False, "disabled": True, "error": "提链功能已关闭"}), 410
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提链 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if not _is_extract_eligible(acc):
                skipped.append({"id": acc_id, "email": email, "reason": "不是 free(可Plus试用)"})
                continue
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = extract_link_service.enqueue_account_extract(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    link_type=data.get("link_type"),
                    cdk=data.get("cdk"),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.post("/api/accounts/codex-agent")
    def api_account_codex_agent():
        """单账号生成 Codex Agent Token。Body {account_id|id, verify_task?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = codex_agent_service.enqueue_account_codex_agent(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                verify_task=bool(data.get("verify_task", True)),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/codex-agent-bulk")
    def api_accounts_codex_agent_bulk():
        """批量生成 Codex Agent Token。Body {account_ids:[...], verify_task?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = codex_agent_service.enqueue_account_codex_agent(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    verify_task=bool(data.get("verify_task", True)),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    def _codex_agent_auth_for_account(acc: dict) -> tuple[str, str]:
        """返回账号已生成的 Codex Agent auth.json 文本与下载文件名。"""
        import json as _json
        from pathlib import Path as _Path

        email = str(acc.get("email") or "").strip()
        safe_email = "".join(ch if ch.isalnum() or ch in ("@", ".", "-", "_") else "_" for ch in (email or f"account-{acc.get('id')}"))
        filename = f"codex-agent-{safe_email}.json"
        token_text = str(acc.get("codex_agent_token") or "").strip()
        if token_text:
            try:
                payload = _json.loads(token_text)
                token_text = _json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            except Exception:
                token_text = token_text + ("\n" if not token_text.endswith("\n") else "")
            return token_text, filename

        auth_path = str(acc.get("codex_agent_auth_path") or "").strip()
        if auth_path:
            p = _Path(auth_path)
            if p.exists() and p.is_file():
                return p.read_text(encoding="utf-8"), p.name or filename

        raise RuntimeError("该账号还没有生成 Codex Agent Token")

    def _join_sub2_url(base: str, path: str) -> str:
        base = str(base or "").strip().rstrip("/")
        path = str(path or "").strip()
        if not base or not path:
            return ""
        parsed = urlparse(path)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return path
        return f"{base}/{path.lstrip('/')}"

    def _sub2_codex_session_import_url() -> str:
        from config import sub2api as sub2api_cfg
        api_base = str(getattr(sub2api_cfg, "SUB2API_API_BASE", "") or "").strip()
        if api_base:
            return _join_sub2_url(api_base, "/api/v1/admin/accounts/import/codex-session")
        # 兼容旧配置：之前 SUB2API_API_URL 是完整上传接口 URL。
        return str(getattr(sub2api_cfg, "SUB2API_API_URL", "") or "").strip()

    def _upload_account_codex_agent_to_sub2(acc: dict) -> dict:
        """把账号已生成的 Codex Agent auth.json 上传到 sub2api。"""
        import json as _json
        from config import sub2api as sub2api_cfg
        from core.codex_agent import upload_sub2api_account

        text, _filename = _codex_agent_auth_for_account(acc)
        try:
            auth_json = _json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"Agent Token JSON 无效: {exc}") from exc

        api_url = _sub2_codex_session_import_url()
        api_token = str(getattr(sub2api_cfg, "SUB2API_API_KEY", "") or getattr(sub2api_cfg, "SUB2API_API_TOKEN", "") or "").strip()
        auth_header = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key") or "x-api-key").strip()
        auth_prefix = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_PREFIX", "") or "").strip()
        payload_mode = "codex_session_import"
        proxy_key = str(getattr(sub2api_cfg, "SUB2API_PROXY_KEY", "") or "").strip() or None
        timeout = float(getattr(sub2api_cfg, "SUB2API_API_TIMEOUT", 20) or 20)

        result = upload_sub2api_account(
            auth_json,
            api_url,
            api_token=api_token,
            auth_header=auth_header,
            auth_prefix=auth_prefix,
            payload_mode=payload_mode,
            proxy_key=proxy_key,
            timeout=timeout,
        )
        try:
            db.update_account_codex_agent(int(acc.get("id")), {
                "ok": True,
                "status": "success",
                "message": "Agent Token 已上传 sub2api",
                "sub2api_url": result.get("url"),
                "sub2api_mode": result.get("payload_mode"),
                "sub2api_total": result.get("total"),
            })
        except Exception:
            logger.exception("更新账号 sub2api 上传状态失败: account_id=%s", acc.get("id"))
        return result

    @app.post("/api/accounts/<int:acc_id>/codex-agent/upload-sub2")
    def api_account_codex_agent_upload_sub2(acc_id: int):
        """单账号把已生成的 Codex Agent Token 上传到 sub2api。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            result = _upload_account_codex_agent_to_sub2(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify({"ok": True, "account_id": acc_id, "email": acc.get("email"), "result": result})

    @app.post("/api/accounts/codex-agent/upload-sub2-bulk")
    def api_accounts_codex_agent_upload_sub2_bulk():
        """批量把已生成的 Codex Agent Token 上传到 sub2api。Body {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        uploaded, failed, skipped = [], [], []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if (acc.get("codex_agent_status") or "") != "success" and not (acc.get("codex_agent_token") or acc.get("codex_agent_auth_path")):
                skipped.append({"id": acc_id, "email": email, "reason": "未生成 Agent Token"})
                continue
            try:
                result = _upload_account_codex_agent_to_sub2(acc)
                uploaded.append({"id": acc_id, "email": email, "url": result.get("url"), "status_code": result.get("status_code")})
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "uploaded": uploaded,
            "uploaded_count": len(uploaded),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.get("/api/accounts/<int:acc_id>/codex-agent/download")
    def api_account_codex_agent_download(acc_id: int):
        """下载单个账号的 Codex Agent auth.json。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            content, filename = _codex_agent_auth_for_account(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 404
        data = content.encode("utf-8")
        return Response(
            data,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/codex-agent/download-bulk")
    def api_accounts_codex_agent_download_bulk():
        """下载选中账号已生成的 Codex Agent Token，打包 ZIP。"""
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        added = []
        errors = []
        used_names = set()
        seen = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw in ids:
                try:
                    acc_id = int(raw)
                except Exception:
                    errors.append({"id": raw, "error": "ID 非法"})
                    continue
                if acc_id in seen:
                    continue
                seen.add(acc_id)
                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                try:
                    content, filename = _codex_agent_auth_for_account(acc)
                    arcname = filename
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, content)
                    added.append({"id": acc_id, "email": acc.get("email"), "filename": arcname})
                except Exception as exc:
                    errors.append({"id": acc_id, "email": acc.get("email"), "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-codex-agent",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有可下载的 Codex Agent Token", "errors": errors}), 404
        now = _dt.now()
        dl_name = f"accounts-codex-agent-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/download-cpa-bulk")
    def api_accounts_download_cpa_bulk():
        """
        从账号列表选中的账号直接到 CPA auth-files 下载 Codex CPA JSON，并打包为 ZIP。
        Body: {"account_ids": [1,2,...]} 或 {"ids": [...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text, list_cpa_codex_auth_files

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        try:
            cpa_files = list_cpa_codex_auth_files()
        except Exception as exc:
            return jsonify({"ok": False, "error": f"读取 CPA auth-files 失败: {type(exc).__name__}: {exc}"}), 502

        def _match_cpa_file(email: str, local_filename: str = "") -> dict | None:
            """在已缓存的 CPA 文件列表中匹配，避免每个账号都重新请求 auth-files。"""
            email_l = str(email or "").strip().lower()
            local_name_l = str(local_filename or "").strip().lower()
            local_stem_l = local_name_l[:-5] if local_name_l.endswith(".json") else local_name_l

            def score(item: dict) -> int:
                name_l = str(item.get("name") or "").lower()
                item_email_l = str(item.get("email") or "").lower()
                s = 0
                if local_name_l and name_l == local_name_l:
                    s = max(s, 100)
                if local_stem_l and name_l.startswith(local_stem_l):
                    s = max(s, 80)
                if email_l and item_email_l == email_l:
                    s = max(s, 70)
                if email_l and email_l in name_l:
                    s = max(s, 60)
                if local_stem_l.endswith("-cpa-callback"):
                    base = local_stem_l[:-len("-cpa-callback")]
                    if base and name_l.startswith(base + "-"):
                        s = max(s, 75)
                return s

            ranked = sorted(((score(item), item) for item in cpa_files), key=lambda x: x[0], reverse=True)
            return ranked[0][1] if ranked and ranked[0][0] > 0 else None

        # 建立 email -> 本地 codex 文件名索引；有本地文件名时传给 CPA 匹配逻辑可提升命中率。
        local_by_email: dict[str, str] = {}
        try:
            for item in db.list_codex_accounts():
                email_key = str(item.get("email") or "").strip().lower()
                fname = str(item.get("filename") or "").strip()
                if email_key and fname and email_key not in local_by_email:
                    local_by_email[email_key] = fname
        except Exception:
            local_by_email = {}

        errors = []
        added = []
        used_names = set()
        seen_ids = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw_id in ids:
                try:
                    acc_id = int(raw_id)
                except (TypeError, ValueError):
                    errors.append({"id": raw_id, "error": "ID 非法"})
                    continue
                if acc_id in seen_ids:
                    continue
                seen_ids.add(acc_id)

                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                email = str(acc.get("email") or "").strip()
                if not email:
                    errors.append({"id": acc_id, "error": "账号缺少 email"})
                    continue

                local_filename = local_by_email.get(email.lower(), "")
                try:
                    meta = _match_cpa_file(email=email, local_filename=local_filename)
                    cpa_name_hint = str((meta or {}).get("name") or "").strip()
                    if not cpa_name_hint:
                        raise RuntimeError(f"[Codex][CPA] 未在 CPA auth-files 中找到匹配的 Codex 凭证: {email}")
                    cpa_text, cpa_name, meta = download_cpa_codex_auth_text(
                        cpa_name=cpa_name_hint,
                    )
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({
                        "id": acc_id,
                        "email": email,
                        "local_filename": local_filename,
                        "cpa_filename": cpa_name,
                        "cpa_meta": meta,
                    })
                    if local_filename:
                        try:
                            db.mark_codex_exported(local_filename)
                        except Exception:
                            pass
                except Exception as exc:
                    errors.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})

            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"accounts-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        if isinstance(data, dict) and data.get("prepare"):
            download_id = _put_prepared_download(zip_bytes, dl_name, "application/zip")
            return jsonify({
                "ok": True,
                "prepared": True,
                "download_id": download_id,
                "download_url": f"/api/downloads/{download_id}",
                "filename": dl_name,
                "added_count": len(added),
                "error_count": len(errors),
            })
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    # ----------------------------------------------------------
    # 邮箱池
    # ----------------------------------------------------------
    @app.get("/api/outlook")
    def api_outlook():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        source = _pool_source_arg()
        q = str(request.args.get("q", default="") or "").strip()
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        fetch_limit = 1_000_000 if (paged or q) else limit
        if source == "all":
            rows = []
            rows += _with_pool_source(db.list_outlook_pool(status=status, limit=fetch_limit), "outlook")
            rows += _with_pool_source(db.list_generic_api_email_pool(status=status, limit=fetch_limit), "generic_api")
            rows += _with_pool_source(db.list_domain_email_pool(status=status, limit=fetch_limit), "cloudflare_domain")
            rows = sorted(rows, key=lambda x: str(x.get("created_at") or x.get("imported_at") or x.get("used_at") or ""), reverse=True)
        elif source == "generic_api":
            rows = _with_pool_source(db.list_generic_api_email_pool(status=status, limit=fetch_limit), "generic_api")
        elif source == "cloudflare_domain":
            rows = _with_pool_source(db.list_domain_email_pool(status=status, limit=fetch_limit), "cloudflare_domain")
        else:
            rows = _with_pool_source(db.list_outlook_pool(status=status, limit=fetch_limit), "outlook")
        if q:
            rows = [r for r in rows if _matches_query(r, q)]
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            return jsonify(_paginate_items(rows, page=page, page_size=page_size))
        return jsonify(rows[:limit])

    @app.post("/api/outlook/import")
    def api_outlook_import():
        """
        粘贴文本导入邮箱素材。
        Outlook：email----password----clientId----refreshToken
        通用 API：email----code_url
        分隔符兼容 ---- 与 ====。
        """
        data = request.get_json(silent=True) or {}
        source = (data.get("source") or data.get("type") or "").strip()
        if source not in ("outlook", "generic_api"):
            return jsonify({"ok": False, "error": "导入时请选择具体类型：Outlook 或 通用 API"}), 400
        text = data.get("text") or ""
        as_registered = bool(data.get("as_registered", False))
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("----") if "----" in line else line.split("====")
            parts = [p.strip() for p in parts]
            if source == "generic_api":
                parsed = parse_generic_api_line(line)
                if not parsed:
                    continue
                records.append({
                    **parsed,
                    "access_token": parsed.get("extra_parts", [""])[0] if parsed.get("extra_parts") else "",
                    "totp_secret": parsed.get("extra_parts", ["", ""])[1] if len(parsed.get("extra_parts", [])) > 1 else "",
                })
                continue
            if len(parts) < 4:
                continue
            records.append({
                "email": parts[0],
                "password": parts[1],
                "client_id": parts[2],
                "refresh_token": parts[3],
                "access_token": parts[4] if len(parts) > 4 else "",
                "totp_secret": parts[5] if len(parts) > 5 else "",
            })
        if not records:
            need = "2 段：邮箱----取码地址" if source == "generic_api" else "4 段：email----password----clientId----refreshToken"
            return jsonify({"ok": False, "error": f"未解析到有效邮箱行（需 {need}，---- 或 ==== 分隔）"}), 400
        if as_registered:
            inserted, skipped = db.import_registered_email_accounts(records, source=source)
        elif source == "generic_api":
            inserted, skipped = db.import_generic_api_emails(records)
        else:
            inserted, skipped = db.import_outlook_accounts(records)
        return jsonify({
            "ok": True,
            "inserted": inserted,
            "skipped": skipped,
            "parsed": len(records),
            "as_registered": as_registered,
        })

    @app.post("/api/outlook/status")
    def api_outlook_status():
        """手动改邮箱状态：body {email, status, note?, source?}。status ∈ available/used/failed/disabled。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        if source == "generic_api":
            db.release_generic_api_email(email, status=status, note=data.get("note"))
        elif source == "cloudflare_domain":
            db.release_domain_email(email, status=status, note=data.get("note"))
        else:
            db.release_outlook(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/outlook/status-bulk")
    def api_outlook_status_bulk():
        """批量修改邮箱状态。Body {items:[{email,source}], status, note?}。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or data.get("emails") or []
        status = (data.get("status") or "").strip()
        note = data.get("note")
        default_source = (data.get("source") or _pool_source_arg()).strip()
        if status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "status 非法"}), 400
        if not isinstance(items, list) or not items:
            return jsonify({"ok": False, "error": "items/emails 必须是非空数组"}), 400
        if len(items) > 5000:
            return jsonify({"ok": False, "error": "单次最多操作 5000 个邮箱"}), 400

        updated = []
        skipped = []
        seen = set()
        for raw_item in items:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or default_source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = default_source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            try:
                if item_source == "generic_api":
                    db.release_generic_api_email(email, status=status, note=note)
                elif item_source == "cloudflare_domain":
                    db.release_domain_email(email, status=status, note=note)
                else:
                    db.release_outlook(email, status=status, note=note)
                updated.append({"email": email, "source": item_source, "status": status})
            except Exception as exc:
                skipped.append({"email": email, "source": item_source, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
        })

    @app.post("/api/outlook/delete")
    def api_outlook_delete():
        """从邮箱池彻底删除一个邮箱：body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        deleted = (
            db.delete_generic_api_email(email)
            if source == "generic_api"
            else db.delete_domain_email(email)
            if source == "cloudflare_domain"
            else db.delete_outlook(email)
        )
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/outlook/delete-bulk")
    def api_outlook_delete_bulk():
        """从邮箱池批量彻底删除邮箱：body {emails: [...]}。"""
        data = request.get_json(silent=True) or {}
        source = _pool_source_arg()
        emails = data.get("items") or data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"ok": False, "error": "emails/items 必须是非空数组"}), 400
        if len(emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个邮箱"}), 400

        deleted: list[str] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        for raw_item in emails:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            deleted_ok = (
                db.delete_generic_api_email(email)
                if item_source == "generic_api"
                else db.delete_domain_email(email)
                if item_source == "cloudflare_domain"
                else db.delete_outlook(email)
            )
            if deleted_ok:
                deleted.append({"email": email, "source": item_source})
            else:
                skipped.append({"email": email, "reason": "邮箱不存在"})

        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    # ----------------------------------------------------------
    # 域名邮箱池（Cloudflare 域名邮箱模式）
    # ----------------------------------------------------------
    @app.get("/api/domain-pool")
    def api_domain_pool():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        return jsonify(db.list_domain_email_pool(status=status, limit=limit))

    @app.post("/api/domain-pool/status")
    def api_domain_pool_status():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        db.release_domain_email(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/domain-pool/delete")
    def api_domain_pool_delete():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        deleted = db.delete_domain_email(email)
        return jsonify({"ok": True, "deleted": deleted})

    # ----------------------------------------------------------
    # Codex 授权账号（CPA 兼容凭证）
    # ----------------------------------------------------------
    @app.get("/api/codex")
    def api_codex_list():
        rows = db.list_codex_accounts()
        q = str(request.args.get("q", default="") or "").strip()
        if q:
            rows = [r for r in rows if _matches_query(r, q)]
        limit = request.args.get("limit", default=500, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = _paginate_items(rows, page=page, page_size=page_size)
            result["accounts"] = result.pop("items")
            result["summary"] = db.codex_accounts_summary()
            return jsonify(result)
        return jsonify({
            "summary": db.codex_accounts_summary(),
            "accounts": rows[:limit],
        })

    @app.get("/api/codex/download/<path:filename>")
    def api_codex_download(filename: str):
        """
        下载一个 CPA 兼容的 codex-*.json 文件，下载即标记为已导出（计数+1）。
        前端通过浏览器原生下载触发（a 标签 / window.location）。
        """
        try:
            content, fname = db.read_codex_credential(filename)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        db.mark_codex_exported(fname)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/codex/download-from-cpa/<path:filename>")
    def api_codex_download_from_cpa(filename: str):
        """按本地 codex 文件/回执匹配 CPA auth-files，并从 CPA 下载实际 Codex JSON。"""
        try:
            content, fname = db.read_codex_credential(filename)
            import json as _json
            try:
                local = _json.loads(content)
            except Exception:
                local = {}
            email = str(local.get("email") or "").strip()
            from core.codex_oauth import download_cpa_codex_auth_text
            cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=fname)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
        db.mark_codex_exported(fname)
        return Response(
            cpa_text,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{cpa_name}"'},
        )

    @app.post("/api/codex/download-bulk-from-cpa")
    def api_codex_download_bulk_from_cpa():
        """
        批量从 CPA 下载选中的 Codex 凭证，打包成 zip；zip 内每个文件都是 CPA 原始 JSON。
        Body: {"filenames": ["codex-xxx-cpa-callback.json", ...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        errors = []
        added = []
        used_names = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fname in filenames:
                if not isinstance(fname, str):
                    errors.append({"filename": str(fname), "error": "非字符串"})
                    continue
                try:
                    content, real_fname = db.read_codex_credential(fname)
                    try:
                        local = _json.loads(content)
                    except Exception:
                        local = {}
                    email = str(local.get("email") or "").strip()
                    cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=real_fname)
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({"local_filename": real_fname, "cpa_filename": cpa_name})
                    db.mark_codex_exported(real_fname)
                except Exception as exc:
                    errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"codex-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        return Response(
            buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/download-bulk")
    def api_codex_download_bulk():
        """
        批量下载选中的 codex 凭证，打包到一个 JSON 文件里。

        Body: {"filenames": ["codex-xxx.json", ...]}
        响应：聚合 JSON（attachment 触发浏览器下载），结构：
            {
              "exported_at": "...",
              "count": N,
              "credentials": [{"filename": "...", "data": {...原始凭证内容...}}, ...],
              "errors": [...]   // 仅当部分失败时出现
            }
        注意：聚合格式**不能直接被 CPA 读**，CPA 是按单文件加载 auths/ 目录的。
              本接口主要用途是备份 / 跨机迁移 / 二次处理。
        每个成功的凭证会自动标记 mark_exported（计数+1）。
        """
        import json as _json
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        bundle = []
        errors = []
        for fname in filenames:
            if not isinstance(fname, str):
                errors.append({"filename": str(fname), "error": "非字符串"})
                continue
            try:
                content, real_fname = db.read_codex_credential(fname)
                parsed = _json.loads(content)
                bundle.append({"filename": real_fname, "data": parsed})
                db.mark_codex_exported(real_fname)
            except Exception as exc:
                errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})

        now = _dt.now()
        result = {
            "exported_at": now.isoformat(timespec="seconds"),
            "count": len(bundle),
            "credentials": bundle,
        }
        if errors:
            result["errors"] = errors

        dl_name = f"codex-bulk-{now.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            _json.dumps(result, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/reset-export")
    def api_codex_reset_export():
        """清掉某个 codex 凭证的导出状态（重新标为未导出）。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            db.reset_codex_exported(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/codex/delete")
    def api_codex_delete():
        """删除一个 codex 凭证文件。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            deleted = db.delete_codex_credential(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not deleted:
            return jsonify({"ok": False, "error": "凭证文件不存在"}), 404
        return jsonify({"ok": True, "deleted": fname})

    @app.post("/api/codex/delete-bulk")
    def api_codex_delete_bulk():
        """批量删除 codex 凭证文件。body {filenames:[...]}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个"}), 400
        deleted = []
        skipped = []
        seen = set()
        for fname in filenames:
            fname = str(fname or "").strip()
            if not fname or fname in seen:
                continue
            seen.add(fname)
            try:
                ok = db.delete_codex_credential(fname)
                if ok:
                    deleted.append(fname)
                else:
                    skipped.append({"filename": fname, "reason": "文件不存在"})
            except Exception as exc:
                skipped.append({"filename": fname, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    def _reserve_codex_retry(email: str) -> bool:
        """进程内防重复占位；成功返回 True。"""
        return codex_retry_service.reserve(email)

    def _release_codex_retry(email: str) -> None:
        codex_retry_service.release(email)

    def _run_codex_retry_worker(email: str, *, batch_label: str | None = None, clear_log: bool = True) -> None:
        """执行一个账号的 Codex 补跑。调用前必须已经 reserve。"""
        codex_retry_service.run_worker(email, batch_label=batch_label, clear_log=clear_log)


    @app.post("/api/codex/stop")
    def api_codex_stop():
        """停止单个 Codex 补跑。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        result = codex_retry_service.request_stop(email)
        status = int(result.pop("status", 200) or 200)
        return jsonify(result), status

    @app.post("/api/codex/stop-bulk")
    def api_codex_stop_bulk():
        """批量停止 Codex 补跑。Body {emails:[...]} 或 {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        ids = data.get("account_ids") or data.get("ids") or []
        targets = []
        if isinstance(emails, list) and emails:
            targets = [str(x or "").strip() for x in emails]
        elif isinstance(ids, list) and ids:
            for raw in ids:
                try:
                    acc = db.get_account(int(raw))
                except Exception:
                    acc = None
                if acc and acc.get("email"):
                    targets.append(str(acc.get("email") or "").strip())
        else:
            return jsonify({"ok": False, "error": "emails 或 account_ids 必须是非空数组"}), 400
        if len(targets) > 500:
            return jsonify({"ok": False, "error": "单次最多停止 500 个"}), 400
        stopped = []
        skipped = []
        seen = set()
        for email in targets:
            key = email.lower()
            if not email or key in seen:
                continue
            seen.add(key)
            acc = db.get_account_by_email(email)
            if acc is None:
                skipped.append({"email": email, "reason": "账号不存在"})
                continue
            if (acc.get("codex_status") or "") != "retrying" and not codex_retry_service.is_retrying(email):
                skipped.append({"email": email, "reason": "未处于补跑中"})
                continue
            r = codex_retry_service.request_stop(email)
            if r.get("ok"):
                stopped.append({"email": email, "injected": r.get("injected"), "running": r.get("running")})
            else:
                skipped.append({"email": email, "reason": r.get("error") or "停止失败"})
        return jsonify({"ok": True, "stopped": stopped, "stopped_count": len(stopped), "skipped": skipped})

    @app.post("/api/codex/reset-retrying")
    def api_codex_reset_retrying():
        """手动重置某账号的 Codex 补跑中状态。Body {email, status?}。"""
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        raw_status = (data.get("status") or "failed").strip().lower()
        if raw_status in ("", "none", "null", "clear"):
            raw_status = "empty"
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        if raw_status not in ("failed", "skipped", "empty"):
            return jsonify({"ok": False, "error": "status 仅支持 failed/skipped/empty"}), 400

        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        new_status = "" if raw_status == "empty" else raw_status
        err = None if raw_status == "empty" else "用户手动重置补跑中状态"
        ok = db.update_account_codex_status(email, new_status, err)
        if not ok:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        _release_codex_retry(email)

        try:
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                ts = _dt.now().strftime("%H:%M:%S")
                shown = new_status or "空"
                f.write(f"{ts} [WARNING] [Codex 补跑] 用户手动重置补跑中状态，当前状态={shown}\n")
        except Exception:
            logger.exception("写入 Codex 补跑重置日志失败")

        return jsonify({"ok": True, "message": "已重置补跑中状态", "status": new_status})

    @app.post("/api/codex/retry")
    def api_codex_retry():
        """手动补跑某账号的 Codex 授权。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        if (acc.get("codex_status") or "") == "deactivated":
            return jsonify({"ok": False, "error": "账号已废号，不能补跑 Codex"}), 409
        if not _reserve_codex_retry(email):
            return jsonify({"ok": False, "error": "该账号正在补跑中，请稍候"}), 409

        db.update_account_codex_status(email, "retrying", None)
        threading.Thread(
            target=_run_codex_retry_worker,
            kwargs={"email": email, "clear_log": True},
            name=f"codex-retry-{email}",
            daemon=True,
        ).start()
        return jsonify({"ok": True, "message": "已在后台开始补跑，~1-2 分钟后刷新查看"})

    @app.post("/api/codex/retry-bulk")
    def api_codex_retry_bulk():
        """批量补跑 Codex。Body {account_ids:[...], workers: 1-16}。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        workers = data.get("workers", 1)
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        try:
            workers = max(1, min(16, int(workers)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是数字"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多选择 500 个账号"}), 400

        selected = []
        skipped = []
        seen_ids = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = (acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            if (acc.get("codex_status") or "") == "deactivated":
                skipped.append({"id": acc_id, "email": email, "reason": "账号已废号"})
                continue
            if not _reserve_codex_retry(email):
                skipped.append({"id": acc_id, "email": email, "reason": "正在补跑中"})
                continue
            selected.append({"id": acc_id, "email": email})

        if not selected:
            return jsonify({"ok": False, "error": "没有可补跑的账号", "skipped": skipped}), 409

        batch_id = _dt.now().strftime("%Y%m%d-%H%M%S")
        for item in selected:
            email = item["email"]
            db.update_account_codex_status(email, "retrying", None)
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"{_dt.now().strftime('%H:%M:%S')} [INFO] [Codex 批量补跑] 已加入批量任务 batch={batch_id} workers={workers}，等待线程执行\n",
                encoding="utf-8",
            )

        def _bulk_runner(items: list[dict], max_workers: int, batch: str):
            logger.info(f"[Codex 批量补跑] 启动 batch={batch} count={len(items)} workers={max_workers}")
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"codex-bulk-{batch}") as ex:
                futures = [ex.submit(_run_codex_retry_worker, it["email"], batch_label=f"{batch} #{idx}/{len(items)}", clear_log=False) for idx, it in enumerate(items, 1)]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception:
                        logger.exception(f"[Codex 批量补跑] 子任务异常 batch={batch}")
            logger.info(f"[Codex 批量补跑] 完成 batch={batch}")

        threading.Thread(
            target=_bulk_runner,
            args=(selected, workers, batch_id),
            name=f"codex-bulk-dispatch-{batch_id}",
            daemon=True,
        ).start()
        return jsonify({
            "ok": True,
            "message": f"已开始批量补跑 {len(selected)} 个账号，并发 {workers}",
            "started": selected,
            "started_count": len(selected),
            "skipped": skipped,
            "batch_id": batch_id,
        })

    @app.get("/api/codex/retry-log")
    def api_codex_retry_log():
        """读取某邮箱最近一次补跑的日志。?email=xxx"""
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = codex_retry_service.log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": False})
        max_bytes = 50_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": codex_retry_service.is_retrying(email),
        })

    # ----------------------------------------------------------
    # 注册任务
    # ----------------------------------------------------------
    @app.get("/api/jobs")
    def api_jobs():
        limit = request.args.get("limit", default=100, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        fetch_limit = 1_000_000 if (paged or page_arg is not None or page_size_arg is not None) else limit
        rows = db.list_jobs(limit=fetch_limit)
        for row in rows:
            row.update(svc.get_retry_info(row))
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = _paginate_items(rows, page=page, page_size=page_size)
            result["items"] = [_compact_job_for_list(r) for r in (result.get("items") or [])]
            result["status_counts"] = _job_status_counts(rows)
            result["compact"] = True
            return jsonify(result)
        return jsonify(rows)

    @app.post("/api/jobs")
    def api_jobs_create():
        """启动批量注册，或使用 mail_link_text 创建指定邮箱链接注册任务。"""
        data = request.get_json(silent=True) or {}

        # workers 控制本次新提交任务使用的线程池；若和上次不同，服务层会为新任务切换到新池。
        try:
            workers = max(1, min(16, int(data.get("workers", 3))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        mail_link_text = str(data.get("mail_link_text") or "").strip()
        if mail_link_text:
            from config import email as _email_cfg
            if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True)):
                return jsonify({
                    "ok": False,
                    "error": "邮箱链接注册依赖自动取码，请先开启自动取邮箱并收取验证码。",
                }), 400

            from core.link_otp_login_service import parse_link_login_input

            parsed = parse_link_login_input(mail_link_text, max_records=200)
            records = [
                {
                    "line_no": item.get("line_no"),
                    "email": item.get("email"),
                    "password": item.get("password") or "",
                    "code_url": item.get("mail_url"),
                }
                for item in (parsed.get("records") or [])
            ]
            if not records:
                return jsonify({
                    "ok": False,
                    "error": "未识别到可用于注册的邮箱查看链接。",
                    "parsed": 0,
                    "errors": parsed.get("errors") or [],
                }), 400

            imported = db.upsert_generic_api_emails_for_registration(records)
            accepted = list(imported.get("accepted") or [])
            errors = list(parsed.get("errors") or []) + list(imported.get("errors") or [])
            if not accepted:
                return jsonify({
                    "ok": False,
                    "error": "识别到的邮箱均已注册、重复或正在执行任务。",
                    "parsed": int(parsed.get("count") or 0),
                    "inserted": int(imported.get("inserted") or 0),
                    "updated": int(imported.get("updated") or 0),
                    "skipped": int(imported.get("skipped") or 0),
                    "errors": errors,
                }), 409

            jobs = svc.submit_registration(
                emails=accepted,
                email_source="generic_api",
                workers=workers,
            )
            return jsonify({
                "ok": True,
                "submitted": len(jobs),
                "parsed": int(parsed.get("count") or 0),
                "inserted": int(imported.get("inserted") or 0),
                "updated": int(imported.get("updated") or 0),
                "skipped": int(imported.get("skipped") or 0),
                "errors": errors,
                "jobs": jobs,
                "warning": "" if not errors else f"有 {len(errors)} 行未提交，请查看提示。",
                "workers": svc.get_executor_workers(),
                "requested_workers": workers,
            })

        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "count 非法"}), 400
        if count < 1 or count > 200:
            return jsonify({"ok": False, "error": "count 需在 1~200 之间"}), 400

        # 提交前先确认池里有足够可用邮箱，给前端一个温和提示（不阻断）
        from config import email as _email_cfg
        from core.email_provider import parse_email_sources
        sources = parse_email_sources(_email_cfg.EMAIL_SOURCE)
        if "gptmail" in sources:
            api_key = str(getattr(_email_cfg, "GPTMAIL_API_KEY", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 gptmail 邮箱来源，请填写 GPTMail API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudflare" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDFLARE_API_BASE", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudflare 邮箱来源，请填写 Cloudflare API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            auth_mode = str(getattr(_email_cfg, "CLOUDFLARE_AUTH_MODE", "none") or "none").strip().lower()
            accounts_path = str(getattr(_email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address") or "").strip().lower()
            api_key = str(getattr(_email_cfg, "CLOUDFLARE_API_KEY", "") or "").strip()
            needs_key = auth_mode in ("x-admin-auth", "bearer", "x-api-key", "query-key") or accounts_path.rstrip("/").endswith("/admin/new_address")
            if needs_key and not api_key:
                return jsonify({
                    "ok": False,
                    "error": "Cloudflare admin/鉴权模式需要填写 Cloudflare API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "mailnest" in sources:
            api_key = str(getattr(_email_cfg, "MAIL_NEST_API_KEY", "") or "").strip()
            project_code = str(getattr(_email_cfg, "MAIL_NEST_PROJECT_CODE", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest API Key（配置 → 邮箱 / OTP）。",
                }), 400
            if not project_code:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest 项目代码（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudmail" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDMAIL_API_BASE", "") or "").strip()
            token = str(getattr(_email_cfg, "CLOUDMAIL_AUTH_TOKEN", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            if not token:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail Token（配置 → 邮箱 / OTP）。",
                }), 400
        if any(source in sources for source in ("gptmail", "mailnest", "cloudmail", "cloudflare", "throwaway")):
            # 临时邮箱在任务开始时动态生成，不需要本地邮箱池容量提示。
            warning = ""
        elif "cloudflare_domain" in sources:
            pool = db.domain_email_pool_summary()
            warning = ""
            if sources == ["cloudflare_domain"] and pool.get("available", 0) < count:
                warning = f"域名邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会自动生成"
        elif sources == ["generic_api"]:
            pool = db.generic_api_email_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"通用 API 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif len(sources) > 1:
            available = 0
            if "outlook" in sources:
                available += db.outlook_pool_summary().get("available", 0)
            if "generic_api" in sources:
                available += db.generic_api_email_pool_summary().get("available", 0)
            warning = ""
            if available < count:
                warning = f"多个邮箱池合计仅 {available} 个可用，少于任务数 {count}，不足的会失败"
        else:
            pool = db.outlook_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"可用邮箱仅 {pool.get('available', 0)} 个，少于任务数 {count}，不足的会失败"
        jobs = svc.submit_registration(count=count, workers=workers)
        return jsonify({
            "ok": True,
            "submitted": len(jobs),
            "jobs": jobs,
            "warning": warning,
            "workers": svc.get_executor_workers(),
            "requested_workers": workers,
        })

    @app.post("/api/jobs/cancel-pending")
    def api_jobs_cancel_pending():
        """取消所有还在排队（status=pending）的任务。已在 running 的不动。"""
        cancelled = svc.cancel_pending_jobs()
        return jsonify({"ok": True, "cancelled": cancelled})

    @app.post("/api/jobs/<int:job_id>/stop")
    def api_job_stop(job_id: int):
        """手动停止单个注册任务。pending 取消；running 发送停止信号。"""
        result = svc.request_stop_job(job_id)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error") or "停止失败"}), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/<int:job_id>/retry")
    def api_job_retry(job_id: int):
        """重试失败/停止/取消任务；服务端自动判断完整注册或 Codex 补跑。"""
        data = request.get_json(silent=True) or {}
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400
        result = svc.retry_job(job_id, workers=workers)
        if not result.get("ok"):
            return jsonify(result), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/retry-bulk")
    def api_jobs_retry_bulk():
        """批量重试任务；不支持项逐条跳过并返回原因。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 500:
            return jsonify({"ok": False, "error": "单次最多重试 500 个任务"}), 400
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        started: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                one_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if one_id in seen:
                continue
            seen.add(one_id)
            result = svc.retry_job(one_id, workers=workers)
            if not result.get("ok"):
                skipped.append({"id": one_id, "reason": result.get("error") or "不能重试"})
            elif result.get("reused"):
                reused.append(result)
            else:
                started.append(result)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "reused": reused,
            "reused_count": len(reused),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "workers": workers,
        })

    @app.post("/api/jobs/<int:job_id>/delete")
    def api_job_delete(job_id: int):
        """删除一个任务记录。真正运行中的任务不允许删除；僵尸任务（无活跃实例）可直接删除。"""
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        status = job.get("status")
        if status in ("running", "stopping") and svc.is_job_active(job_id):
            return jsonify({"ok": False, "error": "运行中的任务不能删除，请等待完成后再删"}), 409
        # 僵尸任务（磁盘 running/stopping 但 _ACTIVE_JOBS 中无真实实例）允许删除：
        # 直接物理删除记录与日志，而不是先改写 stopped，避免多余一步写入，
        # 记录删除后前台列表立即消失。
        deleted = db.delete_job(job_id, delete_log=True, allow_running=status in ("running", "stopping"))
        if not deleted:
            return jsonify({"ok": False, "error": "任务不存在或已开始运行"}), 409
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/jobs/delete-bulk")
    def api_jobs_delete_bulk():
        """批量删除任务记录。running 任务跳过，其它任务删除记录和日志。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个任务"}), 400

        deleted: list[int] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                job_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if job_id in seen:
                continue
            seen.add(job_id)

            job = db.get_job(job_id)
            if not job:
                skipped.append({"id": job_id, "reason": "任务不存在"})
                continue
            status = job.get("status")
            if status in ("running", "stopping") and svc.is_job_active(job_id):
                skipped.append({"id": job_id, "reason": "运行中，不能删除"})
                continue
            # 僵尸任务（running/stopping 但无活跃实例）允许删除：直接物理删除记录与日志。
            allow_running = status in ("running", "stopping")
            if db.delete_job(job_id, delete_log=True, allow_running=allow_running):
                deleted.append(job_id)
            else:
                skipped.append({"id": job_id, "reason": "任务不存在或已开始运行"})

        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    @app.get("/api/jobs/<int:job_id>/log")
    def api_job_log(job_id: int):
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        return jsonify({
            "ok": True,
            "job": job,
            "log": svc.read_job_log(job_id),
        })

    # ----------------------------------------------------------
    # RoxyBrowser 辅助接口
    # ----------------------------------------------------------
    @app.get("/api/roxy/workspaces")
    def api_roxy_workspaces():
        try:
            from core.roxybrowser_client import RoxyBrowserClient
            result = RoxyBrowserClient().list_workspaces()
            return jsonify(result)
        except Exception as exc:
            logger.exception("获取 Roxy 团队/工作区失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    # ----------------------------------------------------------
    # 配置读写
    # ----------------------------------------------------------
    @app.get("/api/config")
    def api_config_get():
        return jsonify(config_editor.get_config())

    @app.post("/api/cloudmail/gen-token")
    def api_cloudmail_gen_token():
        """手动生成 CloudMail Authorization Token，并把本次填写的 CloudMail 配置一并写入 .env。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import gen_token
            from config.env_loader import write_env_values

            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            path = (data.get("path") or "/api/public/genToken").strip() or "/api/public/genToken"
            token = gen_token(
                email=admin_email,
                password=password,
                path=path,
                base_url=api_base,
            )
            updates = {"CLOUDMAIL_AUTH_TOKEN": token}
            # 生成 Token 时用户通常尚未点“保存配置”；这里同步保存本次填写的字段，
            # 避免 loadConfig() 后 API 地址/账号/密码被旧 .env 值覆盖。
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if path:
                updates["CLOUDMAIL_TOKEN_PATH"] = path
            written = write_env_values(updates)
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail Token 写入后热加载失败")
            return jsonify({
                "ok": True,
                "token": token,
                "written": written,
                "message": "CloudMail Token 已生成，且当前 CloudMail 配置已保存",
            })
        except Exception as exc:
            logger.exception("生成 CloudMail Token 失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/cloudmail/domains")
    def api_cloudmail_domains():
        """从 CloudMail 平台获取域名列表，并可写入 .env 作为本地缓存。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import fetch_domains
            from config.env_loader import write_env_values

            updates = {}
            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            token = (data.get("token") or "").strip()
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if token:
                updates["CLOUDMAIL_AUTH_TOKEN"] = token
            if updates:
                write_env_values(updates)
                import config as _config_pkg
                _config_pkg.reload_all()

            domains = fetch_domains(force=True)
            written = write_env_values({"CLOUDMAIL_DOMAINS": "\n".join(domains)})
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail 域名写入后热加载失败")
            return jsonify({
                "ok": True,
                "domains": domains,
                "count": len(domains),
                "written": written,
                "message": f"已获取 {len(domains)} 个 CloudMail 可用域名并保存",
            })
        except Exception as exc:
            logger.exception("获取 CloudMail 域名失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/config")
    def api_config_set():
        data = request.get_json(silent=True) or {}
        updates = data.get("updates") if isinstance(data.get("updates"), dict) else data
        if not isinstance(updates, dict) or not updates:
            return jsonify({"ok": False, "error": "无更新内容"}), 400
        try:
            result = config_editor.update_config(updates)
        except Exception as exc:
            logger.exception("配置写入失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

        # 写盘成功后立即热加载所有 config 子模块，让运行时代码看到新值。
        reload_ok = True
        reload_err = ""
        try:
            import config as _config_pkg
            _config_pkg.reload_all()
        except Exception as exc:
            reload_ok = False
            reload_err = f"{type(exc).__name__}: {exc}"
            logger.exception("配置热加载失败")

        return jsonify({
            "ok": True,
            "updated": result["updated"],
            "ignored": result["ignored"],
            "reloaded": reload_ok,
            "note": (
                "✅ 已保存并热加载，新值立即生效"
                if reload_ok
                else f"⚠️ 已写入文件但热加载失败（{reload_err}），需重启 Web 服务才能生效"
            ),
        })

    # ----------------------------------------------------------
    # 号池
    # ----------------------------------------------------------
    @app.get("/api/pool/summary")
    def api_pool_summary():
        """号池统计。"""
        try:
            return jsonify(account_pool.pool_summary())
        except Exception as exc:
            logger.exception("号池统计失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    @app.get("/api/pool/accounts")
    def api_pool_accounts():
        """号池账号列表（带可用性判定与原因，可筛选）。"""
        limit = request.args.get("limit", default=500, type=int)
        q = str(request.args.get("q", default="") or "").strip()
        status = str(request.args.get("status", default="") or "").strip()
        try:
            return jsonify(account_pool.list_pool_accounts(limit=limit, q=q, status=status))
        except Exception as exc:
            logger.exception("号池账号列表失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/pool/acquire")
    def api_pool_acquire():
        """分配一个可用账号。Body 可选 {prefer_email, tags}。返回 access_token 明文。"""
        data = request.get_json(silent=True) or {}
        prefer_email = str(data.get("prefer_email") or "").strip()
        tags = data.get("tags")
        if tags is not None and not isinstance(tags, list):
            return jsonify({"ok": False, "error": "tags 必须是字符串数组"}), 400
        try:
            result = account_pool.acquire(prefer_email=prefer_email or None, tags=tags)
        except Exception as exc:
            logger.exception("号池分配失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        status = 404 if not result.get("ok") else 200
        return jsonify(result), status

    @app.post("/api/pool/switch")
    def api_pool_switch():
        """无感切号：标记当前账号额度耗尽/受限并分配下一个可用账号。
        Body 可选 {current_email, next_prefer_email, reason}。"""
        data = request.get_json(silent=True) or {}
        current_email = str(data.get("current_email") or "").strip()
        next_prefer_email = str(data.get("next_prefer_email") or "").strip()
        reason = str(data.get("reason") or "").strip()
        try:
            result = account_pool.switch(
                current_email=current_email or None,
                next_prefer_email=next_prefer_email or None,
                reason=reason or None,
            )
        except Exception as exc:
            logger.exception("号池切号失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        status = 404 if not result.get("ok") else 200
        return jsonify(result), status

    @app.post("/api/pool/accounts/<int:acc_id>/enable")
    def api_pool_account_enable(acc_id: int):
        """手动启用池内账号。"""
        try:
            return jsonify(account_pool.set_account_pool_state(acc_id, enabled=True))
        except Exception as exc:
            logger.exception("号池启用账号失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/pool/accounts/<int:acc_id>/disable")
    def api_pool_account_disable(acc_id: int):
        """手动禁用（踢出）池内账号。Body 可选 {reason}。"""
        data = request.get_json(silent=True) or {}
        reason = str(data.get("reason") or "").strip()
        try:
            return jsonify(account_pool.set_account_pool_state(acc_id, enabled=False, reason=reason or None))
        except Exception as exc:
            logger.exception("号池禁用账号失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/pool/probe")
    def api_pool_probe():
        """手动触发一轮号池巡检（对过期未查额度的账号入队额度检查）。"""
        try:
            return jsonify(account_pool.enqueue_pool_probe())
        except Exception as exc:
            logger.exception("号池巡检失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    return app
