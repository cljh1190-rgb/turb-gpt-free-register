# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。

IP 卡顿策略：
  - 启动前 pick_healthy_proxy 探测出口，卡则换下一个
  - 流程中导航/超时/代理类错误 → 拉黑当前出口，换 IP 重开浏览器重跑
  - 邮箱 OTP 已发出后换 IP 仍用同一邮箱（OTP 与出口无关）
"""
from __future__ import annotations

import logging
import hashlib
import time
from pathlib import Path

from config import cloakbrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data
from core.cloakbrowser_driver import (
    build_cloak_driver,
    capture_cloak_environment,
    prime_cloak_device_id,
    stable_cloak_device_id,
    stable_cloak_fingerprint_seed,
)
from core.email_provider import wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _click_resend_email_otp, _complete_profile_page, _fetch_chatgpt_session, _check_manual_stop,
)

logger = logging.getLogger(__name__)


def _proxy_label(proxy: str | None) -> str:
    try:
        from config.proxy import _mask_proxy
        return _mask_proxy(str(proxy or "")) or "无"
    except Exception:
        return "已配置" if proxy else "无"


def _account_browser_profile_dir(email: str) -> Path:
    """注册和“打开浏览器”共用同一持久目录，以便保留真实网页登录 Cookie。"""
    identity = str(email or "account").strip().lower()
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return Path(__file__).resolve().parent.parent / "accounts" / "manual_browser_sessions" / digest


def _proxy_switch_max() -> int:
    try:
        from config import proxy as proxy_cfg
        return max(1, min(8, int(getattr(proxy_cfg, "PROXY_SWITCH_MAX", 3) or 3)))
    except Exception:
        return 3


def _openai_accepts_proxy(proxy: str, timeout: float = 12.0) -> bool:
    """探测 OpenAI/Cloudflare 是否放行该出口（csrf 200 才算可用）。"""
    try:
        from curl_cffi import requests as creq
        from config.proxy import proxy_curl_options
        r = creq.get(
            "https://chatgpt.com/api/auth/csrf",
            proxies={"http": proxy, "https": proxy},
            timeout=timeout,
            impersonate="chrome146",
            curl_options=proxy_curl_options(proxy),
        )
        return r.status_code == 200
    except Exception:
        return False


def _pick_start_proxy(explicit: str | None, exclude: set[str]) -> str | None:
    """选启动出口。explicit 非空则尊重（仍可因卡顿在上层换）。

    自动模式：优先挑一个 OpenAI/Cloudflare 放行（chatgpt csrf=200）的出口，
    被拦的直接拉黑换下一个，避免浏览器开局就撞 403/chrome-error。
    """
    if explicit is not None and str(explicit).strip() == "":
        return ""  # 显式直连
    if explicit:
        return str(explicit).strip()
    try:
        from config.proxy import pick_healthy_proxy
    except Exception:
        pick_healthy_proxy = None
    for _ in range(8):
        try:
            if pick_healthy_proxy is not None:
                cand = str(pick_healthy_proxy(exclude=exclude, probe=True) or "").strip()
            else:
                from config.proxy import pick_proxy
                cand = str(pick_proxy(exclude=exclude) or "").strip()
        except Exception:
            cand = ""
        if not cand:
            # 池子空了：强制重新提取一批再试
            try:
                from config.proxy import refresh_cliproxy_pool
                # 动态提取按 TTL 刷新；白名单接口失败时保留现有静态 sid 池。
                refresh_cliproxy_pool()
            except Exception:
                pass
            continue
        if cand in exclude:
            continue
        exclude.add(str(cand).strip())
        if _openai_accepts_proxy(cand):
            return cand
        logger.warning("[Cloak注册] 出口被OpenAI拦截(csrf!=200) %s，重新提取换新IP", _proxy_label(cand))
        try:
            from config.proxy import ban_proxy
            ban_proxy(cand, reason="openai_csrf_rejected")
        except Exception:
            pass
        # 关键：强制重新提取，避免反复抽到同一个被拦 IP
        try:
            from config.proxy import refresh_cliproxy_pool
            # 不要每个任务强制请求白名单接口，失败时继续使用已有代理池。
            refresh_cliproxy_pool()
        except Exception:
            pass
    return None


def _ban_and_log(proxy: str | None, reason: str) -> None:
    if not proxy:
        return
    try:
        from config.proxy import ban_proxy
        ban_proxy(proxy, reason=reason)
    except Exception:
        pass


def _should_switch_proxy(exc: BaseException) -> bool:
    try:
        from config.proxy import is_proxy_lag_error
        return bool(is_proxy_lag_error(exc))
    except Exception:
        text = f"{type(exc).__name__} {exc}".lower()
        return any(k in text for k in (
            "timeout", "timed out", "proxy", "curl: (28)", "curl: (7)", "navigation",
            "challenge_stuck", "just a moment", "checking your browser", "challenge-platform",
        ))


def _run_cloak_registration_once(
    email: str,
    name: str,
    birthday: str,
    proxy: str | None,
    otp_code: str | None,
    batch_dir: Path | None,
    *,
    fingerprint_seed: str,
    device_id: str,
    otp_after_ts: float | None = None,
) -> dict:
    """单次出口上的完整注册（不含换 IP 外壳）。"""
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    used_proxy = proxy
    fingerprint = {
        "device_id": device_id,
        "browser_profile": {},
        "summary": "未采集",
    }
    try:
        driver, opened = build_cloak_driver(
            proxy=proxy,
            fingerprint_seed=fingerprint_seed,
            user_data_dir=str(_account_browser_profile_dir(email)),
        )
        prime_cloak_device_id(driver, device_id)
        used_proxy = ((opened.raw or {}).get("proxy") if opened else None) or proxy
        logger.info("[Cloak注册] 开始：%s，profile=%s，proxy=%s", email, opened.profile_id, _proxy_label(used_proxy))

        after_ts = float(otp_after_ts or time.time())
        logger.info("[Cloak注册] 打开登录页：https://chatgpt.com/auth/login")
        try:
            driver.get("https://chatgpt.com/auth/login")
        except Exception as exc:
            # 首页都打不开 = 出口基本废了
            raise RuntimeError(f"proxy_lag: open_login_failed: {type(exc).__name__}: {exc}") from exc
        human_delay("navigate")
        _maybe_accept(driver)
        _check_manual_stop()
        try:
            fingerprint = capture_cloak_environment(
                driver,
                opened,
                fallback_device_id=device_id,
            )
            logger.info("[Cloak注册] 环境画像：%s", fingerprint.get("summary") or "-")
        except Exception as exc:
            logger.warning("[Cloak注册] 首次环境画像采集失败，将在登录后重试：%s: %s", type(exc).__name__, str(exc)[:160])

        next_state = _submit_email_and_wait_next(driver, email, attempts=3)
        _check_manual_stop()

        openai_password = None if next_state == "otp" else _fill_password_page_if_present(driver, email, timeout=25)
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Cloak注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Cloak注册][OTP] 收到验证码：%s", current_otp)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            human_delay("otp_input")
            try:
                _click_continue(driver)
            except Exception as exc:
                logger.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            outcome = _wait_after_email_otp_submit(driver, timeout=10)
            if outcome == "accepted":
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            after_ts = time.time()
            _click_resend_email_otp(driver, timeout=25)
            human_delay("api")
            current_otp = None

        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        logger.info("[Cloak注册] 已拿到 accessToken：%s", email)
        try:
            final_fingerprint = capture_cloak_environment(
                driver,
                opened,
                fallback_device_id=str(fingerprint.get("device_id") or device_id),
            )
            if final_fingerprint.get("browser_profile"):
                fingerprint = final_fingerprint
            logger.info("[Cloak注册] 最终环境画像：%s", fingerprint.get("summary") or "-")
        except Exception as exc:
            logger.warning("[Cloak注册] 最终环境画像采集失败，沿用首次画像：%s: %s", type(exc).__name__, str(exc)[:160])

        totp_secret = None
        if _twofa_cfg.ENABLE_2FA:
            logger.info("[Cloak注册][2FA] ENABLE_2FA=True，准备设置 2FA：%s", email)
            try:
                from core.session import BrowserSession
                from core.account_export import maybe_setup_2fa
                twofa_session = BrowserSession(
                    proxy=(used_proxy or "").strip(),
                    detect_exit_geo=False,
                    device_id=str(fingerprint.get("device_id") or device_id),
                    browser_profile=fingerprint.get("browser_profile") or {},
                )
                totp_secret = maybe_setup_2fa(twofa_session, email, driver=driver)
            except Exception as exc:
                logger.warning("[Cloak注册][2FA] 会话创建失败（不影响账号保存）: %s: %s", type(exc).__name__, str(exc)[:200])
                totp_secret = None

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=True，复用当前 CloakBrowser 窗口执行 Codex 授权")
                _check_manual_stop()
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
            else:
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy") if opened else None) or used_proxy or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "device_id": str(fingerprint.get("device_id") or device_id),
                "browser_profile": fingerprint.get("browser_profile") or {},
                "cloakbrowser": {
                    "profile_id": opened.profile_id,
                    "fingerprint_seed": fingerprint_seed,
                    "fingerprint_summary": fingerprint.get("summary"),
                    "open_result": opened.raw,
                },
                "registration_password": openai_password,
                "codex": codex_result,
            },
        )
        codex_ok = bool(codex_result.get("ok") or codex_result.get("status") in ("skipped", "success"))
        codex_note = None if codex_ok else f"Codex 未完成(账号已注册可补跑): {codex_result.get('message')}"
        if codex_note:
            logger.warning("[Cloak注册] %s", codex_note)
        return {
            "success": True,
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "error": codex_note,
            "warning": codex_note,
            "proxy_used": used_proxy,
            "create_acknowledged": create_acknowledged,
        }
    except Exception as exc:
        # 带上当前出口，供外壳换 IP
        err = RuntimeError(f"{type(exc).__name__}: {str(exc)[:300]}")
        err.__cause__ = exc
        setattr(err, "proxy_used", used_proxy)
        setattr(err, "create_acknowledged", create_acknowledged)
        raise err
    finally:
        if driver and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass


def run_cloak_registration(
    email: str,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
) -> dict:
    """CloakBrowser 自动化注册入口（含 IP 卡顿自动换出口）。"""
    max_switch = _proxy_switch_max()
    # 调用方指定了固定 proxy 时，只跑一次（不擅自换，避免违背显式配置）
    fixed_proxy = proxy is not None and str(proxy).strip() != ""
    exclude: set[str] = set()
    last_error: str | None = None
    otp_after_ts = time.time()
    fingerprint_seed = stable_cloak_fingerprint_seed(email)
    device_id = stable_cloak_device_id(email)

    attempts = 1 if fixed_proxy else max_switch
    for attempt in range(1, attempts + 1):
        if not fixed_proxy:
            try:
                from config.proxy import refresh_cliproxy_pool
                refresh_cliproxy_pool()
            except Exception:
                pass
        chosen = proxy if fixed_proxy else _pick_start_proxy(proxy, exclude)
        if chosen is None:
            try:
                from config.proxy import last_cliproxy_error
                detail = last_cliproxy_error()
            except Exception:
                detail = ""
            suffix = f"：{detail}" if detail else ""
            raise RuntimeError(f"代理池没有健康出口，注册已中止，未使用本机直连{suffix}")
        if chosen:
            exclude.add(str(chosen).strip())
        logger.info(
            "[Cloak注册] 出口尝试 %s/%s proxy=%s%s",
            attempt,
            attempts,
            _proxy_label(chosen) if chosen else ("直连" if chosen == "" else "自动"),
            f" exclude={len(exclude)}" if exclude else "",
        )
        try:
            result = _run_cloak_registration_once(
                email,
                name,
                birthday,
                chosen,
                otp_code,
                batch_dir,
                fingerprint_seed=fingerprint_seed,
                device_id=device_id,
                otp_after_ts=otp_after_ts,
            )
            if attempt > 1:
                result["proxy_switched"] = True
                result["proxy_attempts"] = attempt
            return result
        except Exception as exc:
            used = getattr(exc, "proxy_used", None) or chosen
            create_ack = bool(getattr(exc, "create_acknowledged", False))
            root = exc.__cause__ or exc
            last_error = f"{type(root).__name__}: {str(root)[:300]}"
            switchable = (not fixed_proxy) and (not create_ack) and _should_switch_proxy(root)
            if used:
                exclude.add(str(used).strip())
            if switchable and attempt < attempts:
                _ban_and_log(used, reason=last_error)
                logger.warning(
                    "[Cloak注册] 出口卡顿/不可用，换 IP 重开浏览器（%s/%s）：%s",
                    attempt,
                    attempts,
                    last_error[:180],
                )
                # 邮箱可能已发过 OTP：换 IP 后继续用同一邮箱，放宽 after_ts 窗口
                # 保留原 after_ts，避免旧码干扰；若页面要 resend 由内层处理
                continue
            # 不可换或不该换：按原逻辑释放邮箱
            logger.error("[Cloak注册] 失败：%s", last_error)
            logger.debug("[Cloak注册] 失败详情", exc_info=True)
            try:
                from core.email_provider import release_email
                release_email(
                    email,
                    status="failed" if create_ack else "available",
                    note=f"Cloak注册失败: {last_error[:180]}",
                )
            except Exception:
                pass
            return {
                "success": False,
                "email": email,
                "error": last_error,
                "proxy_used": used,
                "proxy_attempts": attempt,
            }

    return {
        "success": False,
        "email": email,
        "error": last_error or "所有出口均卡顿/失败",
        "proxy_attempts": attempts,
    }
