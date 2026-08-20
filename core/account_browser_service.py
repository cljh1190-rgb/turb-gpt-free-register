# -*- coding: utf-8 -*-
"""Open a saved account in a visible CloakBrowser with its registration proxy."""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.cloakbrowser_driver import (
    build_cloak_driver,
    prime_cloak_device_id,
    stable_cloak_device_id,
    stable_cloak_fingerprint_seed,
)

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_PROFILE_ROOT = _ROOT / "accounts" / "manual_browser_sessions"
_LOCK = threading.RLock()
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="account-browser")
_SESSIONS: dict[int, dict[str, Any]] = {}
_ACTIVE = {"queued", "probing_proxy", "launching", "ready", "closing"}


def _validated_initial_url(value: str | None) -> str:
    if not value:
        return ""
    url = str(value).strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "chatgpt.com":
        raise ValueError("initial_url 必须是 https://chatgpt.com 官方地址")
    return url


def _official_checkout_url(value: str | None) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return ""
    if host == "checkout.stripe.com" or host == "pay.openai.com":
        return url
    if host == "chatgpt.com":
        marker = f"{parsed.path}?{parsed.query}".lower()
        if "checkout" in marker or "payment" in marker:
            return url
    return ""


def _try_enter_plus_checkout(driver) -> dict:
    """只点击进入 Plus Checkout 的入口，不接触支付表单和确认按钮。"""
    try:
        result = driver.execute_script("""
        const visible = (el) => {
          const r = el.getBoundingClientRect();
          const s = getComputedStyle(el);
          return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none';
        };
        const positive = [
          'get plus', 'upgrade to plus', 'upgrade plan',
          '获取 plus', '升级到 plus', '升级 plus', '开通 plus'
        ];
        const negative = [
          'business', 'team', 'enterprise', 'pro',
          'confirm', 'subscribe', 'pay now', 'purchase',
          '确认支付', '立即支付', '订阅确认'
        ];
        const nodes = [...document.querySelectorAll('a,button,[role="button"]')];
        for (const el of nodes) {
          if (!visible(el) || el.disabled) continue;
          const text = String(el.innerText || el.textContent || el.getAttribute('aria-label') || '')
            .replace(/\s+/g, ' ').trim().toLowerCase();
          if (!text || negative.some(x => text.includes(x))) continue;
          if (!positive.some(x => text.includes(x))) continue;
          el.click();
          return {clicked: true, text: text.slice(0, 120)};
        }
        return {clicked: false};
        """)
        return result if isinstance(result, dict) else {"clicked": False}
    except Exception as exc:
        return {"clicked": False, "error": f"{type(exc).__name__}: {exc}"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _extra(account: dict) -> dict:
    value = account.get("extra_json")
    if isinstance(value, dict):
        return value
    if value:
        try:
            parsed = json.loads(str(value))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
    return {}


def _browser_profile(account: dict, extra: dict) -> dict:
    profile = account.get("browser_profile")
    if isinstance(profile, dict):
        return dict(profile)
    profile = extra.get("browser_profile")
    return dict(profile) if isinstance(profile, dict) else {}


def _saved_exit_ip(account: dict, extra: dict, profile: dict) -> str:
    candidates = [
        account.get("proxy_exit_ip"),
        (profile.get("geo") or {}).get("ip") if isinstance(profile.get("geo"), dict) else None,
    ]
    cloak = extra.get("cloakbrowser") if isinstance(extra.get("cloakbrowser"), dict) else {}
    opened = cloak.get("open_result") if isinstance(cloak.get("open_result"), dict) else {}
    locale = opened.get("locale") if isinstance(opened.get("locale"), dict) else {}
    geo = locale.get("geo") if isinstance(locale.get("geo"), dict) else {}
    candidates.append(geo.get("ip"))
    return next((str(value).strip() for value in candidates if str(value or "").strip()), "")


def _fingerprint_seed(account: dict, extra: dict) -> str:
    cloak = extra.get("cloakbrowser") if isinstance(extra.get("cloakbrowser"), dict) else {}
    return str(cloak.get("fingerprint_seed") or stable_cloak_fingerprint_seed(account.get("email") or "")).strip()


def _device_id(account: dict, extra: dict) -> str:
    return str(
        account.get("device_id")
        or extra.get("device_id")
        or stable_cloak_device_id(account.get("email") or "")
    ).strip()


def _profile_dir(account: dict) -> Path:
    identity = str(account.get("email") or account.get("id") or "account").strip().lower()
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return _PROFILE_ROOT / digest


def _public(entry: dict | None) -> dict:
    if not entry:
        return {"status": "closed", "active": False}
    return {
        key: value
        for key, value in entry.items()
        if key not in {"driver", "close_event", "future", "account"}
    }


def _update(account_id: int, **values: Any) -> None:
    with _LOCK:
        entry = _SESSIONS.get(account_id)
        if not entry:
            return
        entry.update(values)
        entry["updated_at"] = _now()
        entry["active"] = entry.get("status") in _ACTIVE


def _has_chatgpt_session(driver) -> bool:
    try:
        return bool(driver.execute_async_script("""
        const done = arguments[arguments.length - 1];
        fetch('/api/auth/session', {credentials: 'include'})
          .then(r => r.json()).then(data => done(Boolean(data && data.accessToken)))
          .catch(() => done(false));
        """))
    except Exception:
        return False


def _browser_plan_snapshot(driver) -> dict:
    try:
        result = driver.execute_async_script("""
        const done = arguments[arguments.length - 1];
        const timer = setTimeout(() => done({ok:false, error:'session timeout'}), 8000);
        fetch('/api/auth/session', {credentials: 'include', cache: 'no-store'})
          .then(r => r.json())
          .then(data => {
            clearTimeout(timer);
            done({
              ok: Boolean(data && data.accessToken),
              plan: String(data?.account?.planType || ''),
              account_id: String(data?.account?.id || '')
            });
          })
          .catch(error => {
            clearTimeout(timer);
            done({ok:false, error:String(error)});
          });
        """)
        return result if isinstance(result, dict) else {"ok": False}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _run(account_id: int, allow_rotated_exit: bool) -> None:
    driver = None
    with _LOCK:
        entry = _SESSIONS.get(account_id) or {}
        account = dict(entry.get("account") or {})
        close_event = entry.get("close_event")
        initial_url = str(entry.get("initial_url") or "")
        allow_proxy_fallback = bool(entry.get("allow_proxy_fallback"))
        allow_direct_fallback = bool(entry.get("allow_direct_fallback"))
        capture_checkout = bool(entry.get("capture_checkout"))
    try:
        email = str(account.get("email") or "").strip()
        proxy = str(account.get("proxy_used") or "").strip()
        if not proxy and not allow_proxy_fallback:
            raise RuntimeError("该账号没有保存注册代理，无法按注册 IP 打开")

        extra = _extra(account)
        profile = _browser_profile(account, extra)
        registered_exit_ip = _saved_exit_ip(account, extra, profile)
        _update(account_id, status="probing_proxy", message="正在验证注册代理出口")

        from config import proxy as proxy_cfg

        probe_timeout = 5 if allow_proxy_fallback else 15
        probe = proxy_cfg.probe_proxy(proxy, timeout=probe_timeout) if proxy else {"ok": False, "error": "未保存注册代理"}
        fallback_used = False
        direct_fallback_used = False
        original_proxy_error = str(probe.get("error") or "探测失败")
        if not probe.get("ok") and allow_proxy_fallback:
            _update(account_id, status="probing_proxy", message="原注册代理不可用，正在切换当前健康代理")
            fallback_proxy = ""
            fallback_probe = None
            tried = {proxy} if proxy else set()
            # 结账助手需要快速打开；这里只快速试一个当前池节点，避免健康选择器
            # 在整池故障时刷新并逐个探测十几个入口，导致页面长时间卡住。
            for attempt in range(1):
                try:
                    candidate = proxy_cfg.pick_proxy(exclude=tried)
                except RuntimeError:
                    candidate = ""
                if not candidate:
                    break
                tried.add(candidate)
                _update(
                    account_id,
                    status="probing_proxy",
                    message="原注册代理不可用，快速验证备用代理 1/1",
                )
                candidate_probe = proxy_cfg.probe_proxy(candidate, timeout=4)
                if candidate_probe.get("ok"):
                    fallback_proxy = candidate
                    fallback_probe = candidate_probe
                    break
                try:
                    proxy_cfg.ban_proxy(candidate, reason="billing_handoff_probe_fail")
                except Exception:
                    pass
            if not fallback_proxy:
                if not allow_direct_fallback:
                    raise RuntimeError(f"原注册代理不可用，当前代理池也没有健康入口：{original_proxy_error}")
                proxy = ""
                probe = {"ok": True, "ip": ""}
                direct_fallback_used = True
            else:
                proxy = fallback_proxy
                probe = fallback_probe or {"ok": True, "ip": ""}
                fallback_used = True
        elif not probe.get("ok"):
            raise RuntimeError(f"注册代理已不可用：{original_proxy_error}")
        current_exit_ip = str(probe.get("ip") or "").strip()
        same_exit_ip = bool(registered_exit_ip and current_exit_ip and registered_exit_ip == current_exit_ip)
        if registered_exit_ip and current_exit_ip and not same_exit_ip and not allow_rotated_exit and not fallback_used:
            _update(
                account_id,
                status="failed",
                error_code="exit_ip_changed",
                error="原代理入口的实际出口 IP 已轮换",
                message="原出口已变化；确认后可继续使用原注册代理入口打开",
                registered_exit_ip=registered_exit_ip,
                current_exit_ip=current_exit_ip,
                same_exit_ip=False,
            )
            return

        session_dir = _profile_dir(account)
        session_dir.mkdir(parents=True, exist_ok=True)
        _update(
            account_id,
            status="launching",
            message="正在启动可见浏览器",
            registered_exit_ip=registered_exit_ip or None,
            current_exit_ip=current_exit_ip or None,
            same_exit_ip=same_exit_ip if registered_exit_ip and current_exit_ip else None,
            proxy_used=proxy_cfg._mask_proxy(proxy) if proxy else "本机网络",
            proxy_fallback_used=fallback_used,
            direct_fallback_used=direct_fallback_used,
        )
        driver, opened = build_cloak_driver(
            proxy=proxy or None,
            fingerprint_seed=_fingerprint_seed(account, extra),
            headless=False,
            user_data_dir=str(session_dir),
            browser_profile=profile,
            force_direct=direct_fallback_used,
        )
        prime_cloak_device_id(driver, _device_id(account, extra))
        with _LOCK:
            if account_id in _SESSIONS:
                _SESSIONS[account_id]["driver"] = driver

        driver.get("https://chatgpt.com/")
        time.sleep(2.0)
        login_restored = _has_chatgpt_session(driver)
        email_prefilled = False
        handoff_pending = bool(initial_url and not login_restored)
        if login_restored and initial_url:
            driver.get(initial_url)
        elif not login_restored:
            driver.get("https://chatgpt.com/auth/login")
            try:
                from core.roxy_registration import _type_email_address

                _type_email_address(driver, email, timeout=45)
                email_prefilled = True
            except Exception as exc:
                logger.info("[账号浏览器] 未自动预填邮箱，保留窗口供手动操作: id=%s %s", account_id, str(exc)[:160])

        warning = "代理入口均不可用，已使用本机网络打开" if direct_fallback_used else (
            "原注册代理不可用，已切换当前健康代理打开" if fallback_used else None
        )
        if not fallback_used and registered_exit_ip and current_exit_ip and registered_exit_ip != current_exit_ip:
            warning = "ThorData 原入口仍在使用，但实际出口 IP 已轮换"
        _update(
            account_id,
            status="ready",
            message=(
                "官方 Plus 页面已打开，请在页面内确认卡资料与订阅"
                if login_restored and initial_url
                else "浏览器已打开" + ("，真实网页登录状态已恢复" if login_restored else "，请在窗口中完成邮箱登录")
            ),
            warning=warning,
            login_restored=login_restored,
            login_mode="persisted_session" if login_restored else "manual",
            email_prefilled=email_prefilled,
            handoff_pending=handoff_pending,
            checkout_stage="等待进入官方 Checkout" if capture_checkout else None,
            profile_id=getattr(opened, "profile_id", None),
        )

        last_checkout_attempt = 0.0
        last_plan_check = 0.0
        monitor_payment_result = False
        while close_event is not None and not close_event.wait(1.0):
            try:
                if driver.page.is_closed():
                    break
                if handoff_pending and _has_chatgpt_session(driver):
                    driver.get(initial_url)
                    handoff_pending = False
                    _update(
                        account_id,
                        message="登录成功，已进入官方 Plus 页面，请确认卡资料与订阅",
                        login_restored=True,
                        login_mode="manual_then_handoff",
                        handoff_pending=False,
                    )
                if capture_checkout:
                    payment_link = _official_checkout_url(getattr(driver, "current_url", ""))
                    if payment_link:
                        _update(
                            account_id,
                            payment_link=payment_link,
                            checkout_stage="官方支付链接已提取",
                            message="官方 Checkout 已打开，支付链接已提取",
                            payment_status="waiting_user_confirmation",
                        )
                        capture_checkout = False
                        monitor_payment_result = True
                    elif not handoff_pending and time.monotonic() - last_checkout_attempt >= 3.0:
                        last_checkout_attempt = time.monotonic()
                        clicked = _try_enter_plus_checkout(driver)
                        if clicked.get("clicked"):
                            _update(
                                account_id,
                                checkout_stage="已点击 Plus 入口，等待 Checkout 跳转",
                                checkout_entry_text=clicked.get("text"),
                            )
                if monitor_payment_result and time.monotonic() - last_plan_check >= 10.0:
                    last_plan_check = time.monotonic()
                    snapshot = _browser_plan_snapshot(driver)
                    plan = str(snapshot.get("plan") or "").strip()
                    if snapshot.get("ok") and plan:
                        normalized = plan.lower()
                        if normalized not in {"free", "chatgptfreeplan"}:
                            _update(
                                account_id,
                                detected_plan=plan,
                                payment_status="subscription_active",
                                checkout_stage="已检测到订阅生效",
                                message=f"订阅状态已更新：{plan}",
                            )
                            monitor_payment_result = False
                        else:
                            _update(
                                account_id,
                                detected_plan=plan,
                                payment_status="waiting_user_confirmation",
                            )
            except Exception:
                break
    except Exception as exc:
        logger.error("[账号浏览器] 打开失败: id=%s %s: %s", account_id, type(exc).__name__, str(exc)[:240])
        _update(
            account_id,
            status="failed",
            error=f"{type(exc).__name__}: {str(exc)[:240]}",
            message="浏览器打开失败",
        )
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        with _LOCK:
            entry = _SESSIONS.get(account_id)
            if entry and entry.get("status") not in {"failed"}:
                entry.update(status="closed", active=False, message="浏览器已关闭", updated_at=_now())
            if entry:
                entry.pop("driver", None)


def open_account_browser(
    account: dict,
    *,
    allow_rotated_exit: bool = False,
    initial_url: str | None = None,
    allow_proxy_fallback: bool = False,
    allow_direct_fallback: bool = False,
    capture_checkout: bool = False,
) -> dict:
    account_id = int(account.get("id") or 0)
    if account_id <= 0:
        return {"accepted": False, "error": "账号 ID 无效"}
    try:
        initial_url = _validated_initial_url(initial_url)
    except ValueError as exc:
        return {"accepted": False, "error": str(exc)}
    with _LOCK:
        current = _SESSIONS.get(account_id)
        if current and current.get("status") in _ACTIVE:
            return {"accepted": False, "busy": True, **_public(current)}
        close_event = threading.Event()
        entry = {
            "account_id": account_id,
            "email": account.get("email"),
            "status": "queued",
            "active": True,
            "message": "等待启动浏览器",
            "started_at": _now(),
            "updated_at": _now(),
            "allow_rotated_exit": bool(allow_rotated_exit),
            "allow_proxy_fallback": bool(allow_proxy_fallback),
            "allow_direct_fallback": bool(allow_direct_fallback),
            "capture_checkout": bool(capture_checkout),
            "payment_link": "",
            "payment_status": "not_started",
            "detected_plan": "",
            "checkout_stage": "等待浏览器启动" if capture_checkout else None,
            "initial_url": initial_url,
            "close_event": close_event,
            "account": dict(account),
        }
        _SESSIONS[account_id] = entry
        entry["future"] = _EXECUTOR.submit(_run, account_id, bool(allow_rotated_exit))
        return {"accepted": True, **_public(entry)}


def account_browser_status(account_id: int) -> dict:
    with _LOCK:
        return _public(_SESSIONS.get(int(account_id)))


def close_account_browser(account_id: int) -> dict:
    with _LOCK:
        entry = _SESSIONS.get(int(account_id))
        if not entry:
            return {"ok": True, "status": "closed", "active": False}
        event = entry.get("close_event")
        if event is not None:
            event.set()
        entry.update(status="closing", active=True, message="正在关闭浏览器", updated_at=_now())
        return {"ok": True, **_public(entry)}


def _shutdown() -> None:
    with _LOCK:
        events = [entry.get("close_event") for entry in _SESSIONS.values()]
    for event in events:
        if event is not None:
            event.set()


atexit.register(_shutdown)
