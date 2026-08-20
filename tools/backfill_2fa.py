# -*- coding: utf-8 -*-
"""为注册成功但没有 2FA 的账号批量补跑 2FA（TOTP）。

用法：
    python tools/backfill_2fa.py            # 后台/前台均可，日志写 注册日志/backfill_2fa.log

逻辑：
    1. 扫描账号库中 totp_secret 为空的账号
    2. 每个账号：换新 cliproxy 出口 -> 复用账号设备画像 -> 走 setup_2fa（邮件重认证 + enroll + activate）
    3. 成功后用 insert_account 写回 totp_secret
"""
from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

LOG_DIR = os.path.join(ROOT, "注册日志")
os.makedirs(LOG_DIR, exist_ok=True)
_LOG = os.path.join(LOG_DIR, "backfill_2fa.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(_LOG, encoding="utf-8")],
)
log = logging.getLogger("backfill2fa")

from core import db  # noqa: E402
from core.account_export import maybe_setup_2fa  # noqa: E402
from core.session import BrowserSession  # noqa: E402
from config import proxy as pc  # noqa: E402
from config import twofa as _twofa_cfg  # noqa: E402


def _account_extra(acc: dict) -> dict:
    raw = acc.get("extra_json")
    try:
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {}


def _proxy_reachable(proxy: str) -> bool:
    """通过 chatgpt csrf 探测该出口是否被 OpenAI 放行。"""
    try:
        from curl_cffi import requests as creq
        r = creq.get(
            "https://chatgpt.com/api/auth/csrf",
            proxies={"http": proxy, "https": proxy},
            timeout=20,
            impersonate="chrome",
        )
        return r.status_code == 200
    except Exception:
        return False


def _workers() -> int:
    try:
        return max(1, min(8, int(os.getenv("BACKFILL_WORKERS", "3") or 3)))
    except (TypeError, ValueError):
        return 3


def _process_one(acc: dict) -> tuple[str, str, str | None]:
    email = str(acc.get("email") or "").strip()
    token = str(acc.get("access_token") or "").strip()
    if not email or not token:
        log.warning("跳过（缺邮箱/token）: %s", email)
        return email, token, None
    secret = None
    for attempt in range(1, 4):
        try:
            # 错开各 worker 的提取节奏，避免并发把提取 API 打限流
            time.sleep(random.uniform(0.4, 1.8))
            pc.refresh_cliproxy_pool(force=True)
            proxy = pc.pick_proxy()
            if not _proxy_reachable(proxy):
                log.warning("[%s] 出口被OpenAI拦截(403) %s，换批重试 %s/3", email, proxy, attempt)
                time.sleep(1)
                continue
            extra = _account_extra(acc)
            bp = (extra or {}).get("browser_profile") or {}
            session = BrowserSession(
                proxy=proxy,
                detect_exit_geo=False,
                device_id=acc.get("device_id"),
                browser_profile=bp,
            )
            log.info("[%s] 出口可用 %s，开始 2FA（尝试 %s/3）", email, proxy, attempt)
            secret = maybe_setup_2fa(session, email)
            if secret:
                break
            log.warning("[%s] 2FA 未成功，换批重试 %s/3", email, attempt)
            time.sleep(1)
        except Exception as exc:
            log.exception("[%s] 补跑异常（尝试 %s/3）: %s", email, attempt, exc)
            time.sleep(1)
    return email, token, secret


def main() -> None:
    if not bool(getattr(_twofa_cfg, "ENABLE_2FA", False)):
        log.warning("ENABLE_2FA=False，跳过补跑")
        return
    accounts = db.list_accounts(limit=500)
    targets = [a for a in accounts if not str(a.get("totp_secret") or "").strip()]
    workers = _workers()
    log.info("账号总数=%s，无2FA=%s，并发=%s", len(accounts), len(targets), workers)

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="b2fa") as ex:
        futures = {ex.submit(_process_one, acc): acc for acc in targets}
        for fut in as_completed(futures):
            acc = futures[fut]
            email = str(acc.get("email") or "").strip()
            try:
                _, token, secret = fut.result()
            except Exception as exc:
                log.exception("[%s] worker 异常: %s", email, exc)
                secret = None
            if secret:
                try:
                    db.insert_account(email=email, access_token=token, totp_secret=secret)
                    log.info("[%s] 2FA 设置成功 secret=%s...%s", email, secret[:4], secret[-4:])
                    ok += 1
                except Exception as exc:
                    log.exception("[%s] 写回账号库失败: %s", email, exc)
                    fail += 1
            else:
                log.warning("[%s] 3 次尝试均未成功，跳过", email)
                fail += 1
    log.info("完成: 成功=%s 失败=%s", ok, fail)


if __name__ == "__main__":
    main()
