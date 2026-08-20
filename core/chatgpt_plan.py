# -*- coding: utf-8 -*-
"""ChatGPT 账号套餐/试用资格查询。"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlparse

from core.session import BrowserSession

logger = logging.getLogger(__name__)

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
PLUS_TRIAL_COUPON_PATH = "/backend-api/promo_campaign/check_coupon"
PLUS_TRIAL_COUPON_ID = "plus-1-month-free"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_token(token: str) -> str:
    token = (token or "").strip().strip('"').strip("'")
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _mask_proxy(proxy: str) -> str:
    """返回可用于日志/API 结果的代理摘要，不泄露用户名和密码。"""
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        auth = "***:***@" if parsed.username or parsed.password else ""
        return f"{scheme}{auth}{host}{port}" or "***"
    except Exception:
        return "***"


def _local_proxy_status(proxy: str) -> tuple[bool, bool, str | None]:
    """检查回环代理端口；非本地代理不做预探测，避免额外网络请求。"""
    value = str(proxy or "").strip()
    if not value:
        return False, False, None
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        is_loopback = host.lower() == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            return False, True, None
        if not parsed.port:
            return True, False, "本地代理未配置端口"
        try:
            with socket.create_connection((host, parsed.port), timeout=0.5):
                return True, True, None
        except OSError as exc:
            return True, False, f"本地代理 {host}:{parsed.port} 未监听（{type(exc).__name__}）"
    except Exception as exc:
        return False, False, f"代理地址解析失败（{type(exc).__name__}）"


def _plan_country_route(proxy_cfg: Any, explicit_proxy: Optional[str]) -> dict | None:
    """ThorData 开启时，为套餐/试用查询强制选择隔离的国家池。"""
    if not bool(getattr(proxy_cfg, "THORDATA_ENABLED", False)):
        return None
    country = str(getattr(proxy_cfg, "PLAN_CHECK_THORDATA_COUNTRY", "") or "").strip().upper()
    if not country:
        return None
    number = max(1, min(100, int(getattr(proxy_cfg, "PLAN_CHECK_THORDATA_NUMBER", 3) or 3)))
    selected = str(
        proxy_cfg.pick_healthy_country_proxy(country, number=number, probe=True) or ""
    ).strip()
    if not selected:
        raise ValueError(f"套餐/试用查询未找到可用的 ThorData {country} 出口")
    exit_meta = proxy_cfg.get_proxy_metadata(selected)
    return {
        "proxy": selected,
        "proxy_mode": "plan_country_enforced",
        "network_route": "proxy",
        "proxy_used": _mask_proxy(selected),
        "proxy_fallback_reason": "plan_check_country_enforced" if explicit_proxy is not None else None,
        "plan_check_proxy_country": country,
        "plan_check_proxy_number": number,
        "proxy_gateway_ip": exit_meta.get("gateway_ip"),
        "proxy_entry_port": exit_meta.get("entry_port"),
        "proxy_exit_ip": exit_meta.get("exit_ip") or exit_meta.get("ip"),
        "proxy_exit_country": exit_meta.get("country"),
        "proxy_exit_verified": bool(exit_meta.get("verified_exit")),
    }


def _cliproxy_route(proxy_cfg: Any, explicit_proxy: Optional[str]) -> dict | None:
    """动态 Cliproxy 模式为套餐查询创建全新 SOCKS5 会话。"""
    if not bool(getattr(proxy_cfg, "cliproxy_pool_enabled", lambda: False)()):
        return None
    country = str(getattr(proxy_cfg, "PLAN_CHECK_CLIPROXY_COUNTRY", "JP") or "JP").strip().upper()
    # 不预探测出口：探测最多会先阻塞三轮，而且探测成功也不能代表
    # ChatGPT 不会返回 403。直接发业务请求，失败时再换新 sid。
    selected = str(proxy_cfg.new_cliproxy_country_session(country) or "").strip()
    if not selected:
        detail = str(getattr(proxy_cfg, "last_cliproxy_error", lambda: "")() or "").strip()
        suffix = f"：{detail}" if detail else ""
        raise ValueError(f"套餐查询未找到可用的 Cliproxy SOCKS5 出口{suffix}")
    exit_meta = proxy_cfg.get_proxy_metadata(selected)
    return {
        "proxy": selected,
        "proxy_mode": "cliproxy_dynamic_enforced",
        "network_route": "proxy",
        "proxy_used": _mask_proxy(selected),
        "proxy_fallback_reason": "cliproxy_dynamic_required" if explicit_proxy is not None else None,
        "plan_check_cliproxy_country": country,
        "proxy_gateway_ip": exit_meta.get("gateway_ip"),
        "proxy_entry_port": exit_meta.get("entry_port"),
        "proxy_exit_ip": exit_meta.get("exit_ip") or exit_meta.get("ip"),
        "proxy_exit_country": exit_meta.get("country"),
        "proxy_exit_verified": bool(exit_meta.get("verified_exit")),
    }


def resolve_plan_check_route(
    explicit_proxy: Optional[str] = None,
    *,
    use_plan_country: bool = True,
) -> dict:
    """解析套餐查询的实际网络路径。

    explicit_proxy 不是 None 时表示 API 调用方明确覆盖配置；空字符串代表直连。
    """
    from config import proxy as proxy_cfg

    if use_plan_country:
        country_route = _plan_country_route(proxy_cfg, explicit_proxy)
        if country_route is not None:
            return country_route

    cliproxy_route = _cliproxy_route(proxy_cfg, explicit_proxy)
    if cliproxy_route is not None:
        return cliproxy_route

    if explicit_proxy is not None:
        selected = str(explicit_proxy or "").strip()
        enforced = False
        if (
            bool(getattr(proxy_cfg, "proxy_required", lambda: False)())
            and not bool(getattr(proxy_cfg, "proxy_allowed", lambda value: bool(value))(selected))
        ):
            selected = str(proxy_cfg.pick_proxy() or "").strip()
            enforced = True
        return {
            "proxy": selected,
            "proxy_mode": "proxy_enforced" if enforced else "request",
            "network_route": "proxy" if selected else "direct",
            "proxy_used": _mask_proxy(selected) or None,
            "proxy_fallback_reason": "thordata_required" if enforced else None,
        }

    mode = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto") or "auto").strip().lower()
    if mode not in {"auto", "proxy", "direct"}:
        raise ValueError(f"PLAN_CHECK_PROXY_MODE={mode!r} 无效，可选 auto / proxy / direct")
    if mode == "direct":
        if bool(getattr(proxy_cfg, "proxy_required", lambda: False)()):
            selected = str(proxy_cfg.pick_proxy() or "").strip()
            return {
                "proxy": selected,
                "proxy_mode": "proxy_enforced",
                "network_route": "proxy",
                "proxy_used": _mask_proxy(selected),
                "proxy_fallback_reason": "direct_disabled_by_thordata",
            }
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct",
            "proxy_used": None,
            "proxy_fallback_reason": None,
        }

    selected = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY", "") or "").strip()
    # 禁止本机系统代理（10808/7897）：直连 chatgpt 会超时，且与注册专用池隔离策略冲突。
    if selected and getattr(proxy_cfg, "_is_forbidden_local_proxy", None) and proxy_cfg._is_forbidden_local_proxy(selected):
        logger.warning("[Plan] PLAN_CHECK_PROXY 指向禁止端口（%s），改抽注册专用池", _mask_proxy(selected))
        selected = ""
    if not selected:
        selected = str(proxy_cfg.pick_proxy() or "").strip()
    if selected and getattr(proxy_cfg, "_is_forbidden_local_proxy", None) and proxy_cfg._is_forbidden_local_proxy(selected):
        selected = ""
    if not selected:
        if mode == "proxy" or bool(getattr(proxy_cfg, "proxy_required", lambda: False)()):
            raise ValueError("套餐查询网络模式为 proxy，但未配置 PLAN_CHECK_PROXY 或 PROXY_POOL")
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct",
            "proxy_used": None,
            "proxy_fallback_reason": "未配置套餐查询代理或代理池",
        }

    is_local, available, reason = _local_proxy_status(selected)
    if mode == "auto" and is_local and not available:
        # 本地端口未监听：再试一次抽别的池端口，仍不行才 direct（国内 direct 通常也超时）
        alt = str(proxy_cfg.pick_proxy(exclude=[selected]) or "").strip()
        if alt and alt != selected:
            selected = alt
            is_local, available, reason = _local_proxy_status(selected)
        if is_local and not available:
            return {
                "proxy": "",
                "proxy_mode": mode,
                "network_route": "direct_fallback",
                "proxy_used": _mask_proxy(selected),
                "proxy_fallback_reason": reason,
            }
    return {
        "proxy": selected,
        "proxy_mode": mode,
        "network_route": "proxy",
        "proxy_used": _mask_proxy(selected),
        "proxy_fallback_reason": None,
    }


def decode_jwt_payload_unverified(token: str) -> dict:
    """仅本地解析 JWT payload，不校验签名。"""
    token = normalize_token(token)
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def token_claims(token: str) -> dict:
    payload = decode_jwt_payload_unverified(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    profile = payload.get("https://api.openai.com/profile") or {}
    exp = payload.get("exp")
    exp_iso = None
    expired = None
    if isinstance(exp, (int, float)):
        exp_iso = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        expired = datetime.now(tz=timezone.utc).timestamp() >= float(exp)
    return {
        "payload": payload,
        "email": profile.get("email"),
        "user_name": profile.get("name"),
        "user_id": auth.get("chatgpt_user_id") or auth.get("user_id"),
        "account_id": auth.get("chatgpt_account_id"),
        "claim_plan_type": auth.get("chatgpt_plan_type"),
        "exp": exp,
        "token_expires_at": exp_iso,
        "token_expired": expired,
    }


def _common_headers(env: BrowserSession, token: str) -> dict[str, str]:
    headers = env._get_common_headers()
    headers.update({
        "accept": "*/*",
        "authorization": f"Bearer {normalize_token(token)}",
        "oai-device-id": env.device_id,
        "oai-language": env.navigator_language(),
        "referer": "https://chatgpt.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-openai-target-path": ACCOUNTS_CHECK_PATH,
        "x-openai-target-route": ACCOUNTS_CHECK_PATH,
    })
    return headers


def parse_plus_trial_coupon(data: dict) -> dict:
    """Parse the dedicated Plus trial coupon eligibility response."""
    if not isinstance(data, dict):
        raise ValueError("试用资格响应不是 JSON 对象")
    state = str(data.get("state") or "").strip().lower()
    redemption = data.get("redemption") if isinstance(data.get("redemption"), dict) else {}
    redeemed = bool(
        redemption.get("redeemed")
        or redemption.get("redeemed_by_user")
        or redemption.get("redeemed_by_workspace")
    )
    eligible = bool(
        not redeemed
        and state in {"eligible", "available", "active", "valid"}
    )
    return {
        "plus_trial_coupon_checked": True,
        "plus_trial_coupon_state": state or None,
        "plus_trial_coupon_redeemed": redeemed,
        "plus_trial_coupon_expires_at": redemption.get("expires_at"),
        "plus_trial_coupon_promotion_length_days": redemption.get("promotion_length_days"),
        "plus_trial_coupon_eligible": eligible,
    }


def _check_plus_trial_coupon(env: BrowserSession, token: str, timeout: float) -> dict:
    headers = _common_headers(env, token)
    headers.update({
        "accept": "application/json",
        "x-openai-target-path": PLUS_TRIAL_COUPON_PATH,
        "x-openai-target-route": PLUS_TRIAL_COUPON_PATH,
    })
    response = env.session.get(
        f"https://chatgpt.com{PLUS_TRIAL_COUPON_PATH}",
        params={
            "coupon": PLUS_TRIAL_COUPON_ID,
            "is_coupon_from_query_param": "true",
        },
        headers=headers,
        allow_redirects=False,
        timeout=timeout,
    )
    status = int(response.status_code)
    if not (200 <= status < 300):
        return {
            "plus_trial_coupon_checked": False,
            "plus_trial_coupon_http_status": status,
            "plus_trial_coupon_error": f"HTTP {status}",
        }
    try:
        data = response.json()
    except Exception:
        text = response.text or ""
        data = json.loads(text) if text.strip().startswith("{") else None
    parsed = parse_plus_trial_coupon(data)
    parsed["plus_trial_coupon_http_status"] = status
    return parsed


def _merge_plus_trial_coupon(plan: dict, coupon: dict) -> dict:
    plan.update(coupon)
    if not coupon.get("plus_trial_coupon_checked"):
        return plan
    coupon_eligible = bool(coupon.get("plus_trial_coupon_eligible"))
    plan["plus_trial_eligible"] = bool(plan.get("plus_trial_eligible") or coupon_eligible)
    if coupon_eligible:
        plan["plus_trial_campaign_id"] = plan.get("plus_trial_campaign_id") or PLUS_TRIAL_COUPON_ID
        plan["plus_trial_title"] = plan.get("plus_trial_title") or "ChatGPT Plus 一月免费试用"
        promotion_days = coupon.get("plus_trial_coupon_promotion_length_days")
        if promotion_days and not plan.get("plus_trial_duration_num_periods"):
            plan["plus_trial_duration_num_periods"] = promotion_days
            plan["plus_trial_duration_period"] = "day"
    return plan


def parse_accounts_check(data: dict, *, token: str = "") -> dict:
    """从 accounts/check 响应提取套餐和 Plus 试用资格。"""
    claims = token_claims(token) if token else {}
    claim_account_id = claims.get("account_id")
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        raise ValueError("响应缺少 accounts 对象")

    item = None
    account_key = None
    if claim_account_id and isinstance(accounts.get(claim_account_id), dict):
        item = accounts.get(claim_account_id)
        account_key = claim_account_id
    elif isinstance(accounts.get("default"), dict):
        item = accounts.get("default")
        account = item.get("account") or {}
        account_key = account.get("account_id") or "default"
    else:
        for k, v in accounts.items():
            if k != "default" and isinstance(v, dict):
                item = v
                account_key = k
                break
    if not isinstance(item, dict):
        raise ValueError("未找到可解析的账号条目")

    account = item.get("account") or {}
    entitlement = item.get("entitlement") or {}
    last_sub = item.get("last_active_subscription") or {}
    eligible_promo_campaigns = item.get("eligible_promo_campaigns") or {}
    plus_campaign = eligible_promo_campaigns.get("plus") if isinstance(eligible_promo_campaigns, dict) else None
    plus_meta = (plus_campaign or {}).get("metadata") or {}
    discount = plus_meta.get("discount") or {}
    duration = plus_meta.get("duration") or {}

    plan_type = account.get("plan_type") or claims.get("claim_plan_type") or ""
    subscription_plan = entitlement.get("subscription_plan") or ""
    has_active_subscription = bool(entitlement.get("has_active_subscription"))
    is_free = str(plan_type).lower() == "free" or str(subscription_plan).lower() == "chatgptfreeplan"
    plus_trial_eligible = bool(is_free and plus_campaign)

    offers = ((item.get("eligible_offers") or {}).get("offers") or [])
    eligible_offer_ids = [o.get("id") for o in offers if isinstance(o, dict) and o.get("id")]

    result = {
        "ok": True,
        "checked_at": now_iso(),
        "account_id": account.get("account_id") or account_key or claim_account_id,
        "account_user_role": account.get("account_user_role"),
        "current_plan_type": plan_type,
        "subscription_plan": subscription_plan,
        "has_active_subscription": has_active_subscription,
        "is_active_subscription_gratis": bool(entitlement.get("is_active_subscription_gratis")),
        "expires_at": entitlement.get("expires_at"),
        "renews_at": entitlement.get("renews_at"),
        "cancels_at": entitlement.get("cancels_at"),
        "billing_period": entitlement.get("billing_period"),
        "billing_currency": entitlement.get("billing_currency"),
        "is_delinquent": bool(entitlement.get("is_delinquent")),
        "discount_type": (entitlement.get("discount") or {}).get("discount_type"),
        "discount_amount": (entitlement.get("discount") or {}).get("amount"),
        "discount_duration_num_periods": (entitlement.get("discount") or {}).get("duration_num_periods"),
        "discount_expires_at": (entitlement.get("discount") or {}).get("discount_expires_at"),
        "discount_cancellation_policy": (entitlement.get("discount") or {}).get("cancellation_policy"),
        "discount_promo_campaign_id": (entitlement.get("discount") or {}).get("promo_campaign_id"),
        "last_purchase_origin_platform": last_sub.get("purchase_origin_platform"),
        "last_will_renew": bool(last_sub.get("will_renew")),
        "plus_trial_eligible": plus_trial_eligible,
        "plus_trial_campaign_id": (plus_campaign or {}).get("id"),
        "plus_trial_title": plus_meta.get("title"),
        "plus_trial_summary": plus_meta.get("summary"),
        "plus_trial_discount_percentage": discount.get("percentage"),
        "plus_trial_duration_num_periods": duration.get("num_periods"),
        "plus_trial_duration_period": duration.get("period"),
        "plus_trial_promotion_type_label": plus_meta.get("promotion_type_label"),
        "plus_yearly_eligible": bool(item.get("is_eligible_for_yearly_plus_subscription")),
        "plus_yearly_new_user_eligible": bool(item.get("is_eligible_for_yearly_plus_new_user_subscription")),
        "plus_yearly_existing_user_eligible": bool(item.get("is_eligible_for_yearly_plus_existing_user_subscription")),
        "eligible_offer_ids": eligible_offer_ids,
        "features_count": len(item.get("features") or []),
        "can_access_with_session": bool(item.get("can_access_with_session")),
        "raw_account_plan_type": account.get("plan_type"),
    }
    result.update({k: v for k, v in claims.items() if k != "payload" and v is not None})
    return result


def _plan_check_settings(
    timeout: float | None,
    max_attempts: int | None,
    retry_delay: float | None,
) -> tuple[float, int, float]:
    from config import proxy as proxy_cfg

    timeout_value = timeout if timeout is not None else getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 15.0)
    attempts_value = max_attempts if max_attempts is not None else getattr(proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 2)
    delay_value = retry_delay if retry_delay is not None else getattr(proxy_cfg, "PLAN_CHECK_RETRY_DELAY", 1.5)
    return (
        max(1.0, min(60.0, float(timeout_value or 15.0))),
        max(1, min(8, int(attempts_value or 1))),
        max(0.0, min(30.0, float(delay_value or 0.0))),
    )


def _retryable_plan_error(http_status: int | None, response_text: str = "") -> bool:
    if http_status is None:
        return True
    body = str(response_text or "").lower()
    if http_status == 401 and any(
        marker in body
        for marker in (
            "token_revoked",
            "token_invalidated",
            "token has been invalidated",
            "invalidated oauth token",
            "oauth token for user",
        )
    ):
        return False
    # ChatGPT's account-check edge can return 401/403 for a rejected proxy
    # exit, not only for an invalid token. Token expiry is handled before the
    # request by token_claims(); retrying here lets dynamic proxy routes switch
    # to a fresh exit before giving up.
    return http_status in {401, 403, 408, 409, 425, 429} or http_status >= 500


def _account_validity(http_status: int | None, response_text: str = "", *, token_expired: bool = False) -> str:
    """Classify token validity without mistaking a proxy-blocked 403 for an invalid account."""
    if token_expired:
        return "invalid"
    body = str(response_text or "").lower()
    if http_status == 401:
        return "invalid"
    if any(marker in body for marker in ("token_revoked", "token has been invalidated", "invalidated oauth token")):
        return "invalid"
    if http_status == 403:
        return "unknown_proxy_or_policy"
    if http_status is not None and 200 <= http_status < 300:
        return "valid"
    return "unknown"


def _retry_wait_seconds(resp: Any, base_delay: float, attempt: int) -> float:
    try:
        retry_after = (getattr(resp, "headers", {}) or {}).get("retry-after")
        if retry_after is not None:
            return max(0.0, min(30.0, float(retry_after)))
    except (TypeError, ValueError):
        pass
    return max(0.0, min(30.0, base_delay * attempt))


def check_account_plan(
    token: str,
    *,
    proxy: Optional[str] = None,
    timezone_offset_min: str = "-",
    device_id: str | None = None,
    browser_profile: dict | None = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
    include_plus_trial: bool = True,
) -> dict:
    token = normalize_token(token)
    if not token:
        return {"ok": False, "checked_at": now_iso(), "error": "token 为空"}
    if str(timezone_offset_min or "").strip() in {"", "-"} and isinstance(browser_profile, dict):
        profile_offset = browser_profile.get("timezone_offset_minutes")
        if profile_offset is not None:
            try:
                timezone_offset_min = str(-int(profile_offset or 0))
            except (TypeError, ValueError):
                timezone_offset_min = "-"
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": "token 已过期",
            "account_validity": "invalid",
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    try:
        route = resolve_plan_check_route(proxy)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"套餐查询网络配置错误: {exc}",
            **{k: v for k, v in claims.items() if k != "payload"},
        }
    route_meta = {k: v for k, v in route.items() if k != "proxy"}
    url = f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}?timezone_offset_min={quote(str(timezone_offset_min))}"
    from config import proxy as runtime_proxy_cfg
    allow_proxy_switch = route.get("proxy_mode") == "cliproxy_dynamic_enforced" or bool(route.get("plan_check_proxy_country")) or proxy is None or (
        not str(proxy or "").strip()
        and bool(getattr(runtime_proxy_cfg, "proxy_required", lambda: False)())
    )
    try:
        timeout_seconds, attempts, base_delay = _plan_check_settings(timeout, max_attempts, retry_delay)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"套餐查询重试配置错误: {exc}",
            "retryable": False,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    last_result: dict | None = None
    failed_proxies: list[str] = []
    for attempt in range(1, attempts + 1):
        env = None
        resp = None
        # 自动模式下每次重试换出口，避免同一死节点连撞 15s 超时。
        if attempt > 1 and allow_proxy_switch:
            try:
                from config import proxy as proxy_cfg
                alt = ""
                plan_country = str(route.get("plan_check_proxy_country") or "").strip().upper()
                if plan_country:
                    alt = str(proxy_cfg.pick_healthy_country_proxy(
                        plan_country,
                        number=int(route.get("plan_check_proxy_number") or 3),
                        exclude=failed_proxies,
                        probe=True,
                    ) or "").strip()
                else:
                    if bool(getattr(proxy_cfg, "cliproxy_pool_enabled", lambda: False)()):
                        # 403/临时网络错误后直接创建新 sid，并用新出口发送下一次业务请求。
                        clip_country = str(route.get("plan_check_cliproxy_country") or "JP").strip().upper()
                        alt = str(proxy_cfg.new_cliproxy_country_session(clip_country) or "").strip()
                    else:
                        alt = str(proxy_cfg.pick_proxy(exclude=failed_proxies) or "").strip()
                if alt:
                    exit_meta = proxy_cfg.get_proxy_metadata(alt)
                    route = {
                        "proxy": alt,
                        "proxy_mode": route.get("proxy_mode") or "auto",
                        "network_route": "proxy",
                        "proxy_used": _mask_proxy(alt),
                        "proxy_fallback_reason": f"retry_switch_from={_mask_proxy(failed_proxies[-1]) if failed_proxies else '-'}",
                        "plan_check_proxy_country": plan_country or None,
                        "plan_check_proxy_number": route.get("plan_check_proxy_number") if plan_country else None,
                        "plan_check_cliproxy_country": route.get("plan_check_cliproxy_country"),
                        "proxy_gateway_ip": exit_meta.get("gateway_ip"),
                        "proxy_entry_port": exit_meta.get("entry_port"),
                        "proxy_exit_ip": exit_meta.get("exit_ip") or exit_meta.get("ip"),
                        "proxy_exit_country": exit_meta.get("country"),
                        "proxy_exit_verified": bool(exit_meta.get("verified_exit")),
                    }
                    route_meta = {k: v for k, v in route.items() if k != "proxy"}
            except Exception:
                pass
        try:
            # 套餐查询只需要稳定的请求头，不需要额外访问 IP 地理信息接口。
            env = BrowserSession(
                proxy=route["proxy"],
                detect_exit_geo=False,
                device_id=device_id,
                browser_profile=browser_profile,
            )
            resp = env.session.get(
                url,
                headers=_common_headers(env, token),
                allow_redirects=False,
                timeout=timeout_seconds,
            )
            response_text = resp.text or ""
            http_status = int(resp.status_code)
            if not (200 <= http_status < 300):
                last_result = {
                    "ok": False,
                    "checked_at": now_iso(),
                    "http_status": http_status,
                    "error": (
                        "OAuth Token 已被撤销（token_revoked），请重新注册/重新授权"
                        if not _retryable_plan_error(http_status, response_text)
                        else f"HTTP {http_status}"
                    ),
                    "response_preview": response_text[:500],
                    "retryable": _retryable_plan_error(http_status, response_text),
                    "account_validity": _account_validity(http_status, response_text),
                }
            else:
                try:
                    data: Any = resp.json()
                except Exception:
                    data = json.loads(response_text) if response_text.strip().startswith(("{", "[")) else None
                if not isinstance(data, dict):
                    last_result = {
                        "ok": False,
                        "checked_at": now_iso(),
                        "http_status": http_status,
                        "error": "响应不是 JSON 对象",
                        "response_preview": response_text[:500],
                        "retryable": True,
                        "account_validity": "unknown",
                    }
                else:
                    parsed = parse_accounts_check(data, token=token)
                    if include_plus_trial and str(parsed.get("current_plan_type") or "").lower() == "free":
                        try:
                            coupon_result = _check_plus_trial_coupon(env, token, timeout_seconds)
                        except Exception as exc:
                            coupon_result = {
                                "plus_trial_coupon_checked": False,
                                "plus_trial_coupon_error": f"{type(exc).__name__}: {str(exc)[:180]}",
                            }
                        _merge_plus_trial_coupon(parsed, coupon_result)
                    parsed["http_status"] = http_status
                    parsed["attempt_count"] = attempt
                    parsed["max_attempts"] = attempts
                    parsed["request_timeout"] = timeout_seconds
                    parsed["retryable"] = False
                    parsed["account_validity"] = "valid"
                    parsed.update(route_meta)
                    return parsed
        except Exception as exc:
            logger.debug("套餐查询失败: %s: %s", type(exc).__name__, exc, exc_info=True)
            last_result = {
                "ok": False,
                "checked_at": now_iso(),
                "http_status": int(resp.status_code) if resp is not None and getattr(resp, "status_code", None) else None,
                "error": f"{type(exc).__name__}: {exc}",
                "retryable": True,
                "account_validity": "unknown",
            }
        finally:
            if env is not None:
                try:
                    env.session.close()
                except Exception:
                    pass

        last_result = last_result or {"ok": False, "checked_at": now_iso(), "error": "未知错误", "retryable": True}
        last_result.update({
            "attempt_count": attempt,
            "max_attempts": attempts,
            "request_timeout": timeout_seconds,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        })
        used_proxy = str(route.get("proxy") or "").strip()
        if used_proxy and used_proxy not in failed_proxies:
            failed_proxies.append(used_proxy)
        if not last_result.get("retryable") or attempt >= attempts:
            return last_result

        wait_seconds = _retry_wait_seconds(resp, base_delay, attempt)
        logger.warning(
            "套餐查询临时失败，第 %s/%s 次，%.1fs 后重试（将换出口）: %s proxy=%s",
            attempt,
            attempts,
            wait_seconds,
            last_result.get("error"),
            route_meta.get("proxy_used") or "-",
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    return last_result or {
        "ok": False,
        "checked_at": now_iso(),
        "http_status": None,
        "error": "套餐查询未执行",
        "retryable": False,
        **route_meta,
        **{k: v for k, v in claims.items() if k != "payload"},
    }
