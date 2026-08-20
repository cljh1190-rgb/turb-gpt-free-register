# -*- coding: utf-8 -*-
"""
通用 API 取码邮箱客户端。

邮箱池导入格式：
    email----code_url

注册时领取 email；取码时直接 GET code_url，并从响应中提取 6 位验证码。
响应可以是纯文本、HTML 或 JSON，只要其中包含 6 位验证码即可。
"""
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp

logger = logging.getLogger(__name__)

_CODE_REGEX = re.compile(r"\b(\d{6})\b")
_CONTEXT_WORDS = ("code", "verify", "verification", "验证码", "代码", "确认码", "認証", "コード")
_CONTEXT_CACHE: dict[str, "GenericApiEmailAccount"] = {}
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_FILE = _PROJECT_ROOT / "用于注册的API邮箱.txt"

# email2.ymb1668.com/client/mailbox is a Vite SPA shell.  Its HTML contains a
# fixed demo number, so it must never be treated as an OTP source.  The public
# JSON endpoint is read-only and returns the current mailbox message.
_EMAIL2_VIEWER_HOST = "email2.ymb1668.com"
_EMAIL2_API_HOST = "email2.api.ymb1668.com"
_EMAIL2_API_PATH = "/api/v2/public/mailbox/latest"


class GenericApiMailError(RuntimeError):
    """通用 API 取码邮箱错误。"""


@dataclass
class GenericApiEmailAccount:
    email: str
    code_url: str
    password: str = ""


def parse_generic_api_line(line: str) -> dict | None:
    """Parse generic API mailbox lines, including password-bearing records."""
    raw = str(line or "").strip()
    if not raw or raw.startswith("#"):
        return None
    delimiter = "----" if "----" in raw else ("====" if "====" in raw else None)
    if not delimiter:
        return None
    parts = [part.strip() for part in raw.split(delimiter)]
    if len(parts) < 2 or not re.fullmatch(r"[^\s@]+@[^\s@]+", parts[0]):
        return None

    def is_url(value: str) -> bool:
        parsed = urlsplit(value)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)

    if len(parts) >= 3 and is_url(parts[2]):
        password, code_url, extra_parts = parts[1], parts[2], parts[3:]
    elif is_url(parts[1]):
        password, code_url, extra_parts = "", parts[1], parts[2:]
    else:
        # Some mailbox vendors use four dashes after the email but only
        # three between the password and URL: email----password---https://...
        mixed = re.fullmatch(r"(.+?)(?:---|===)(https?://\S+)", parts[1], re.IGNORECASE)
        if not mixed or not is_url(mixed.group(2)):
            return None
        password, code_url, extra_parts = mixed.group(1).strip(), mixed.group(2).strip(), []
    return {"email": parts[0], "password": password, "code_url": code_url, "extra_parts": extra_parts}


def parse_otp_viewer_text(text: str, *, max_records: int = 100) -> dict:
    """Parse standalone OTP viewer input without importing it into the email pool."""
    records: list[dict] = []
    errors: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for line_no, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = parse_generic_api_line(line)
        if not parsed:
            errors.append({"line_no": line_no, "reason": "格式应为：邮箱----取码链接"})
            continue
        email = parsed["email"]
        code_url = parsed["code_url"]
        parsed_url = urlsplit(code_url)
        if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
            errors.append({"line_no": line_no, "email": email, "reason": "取码链接必须是有效的 http/https 地址"})
            continue

        key = (email.lower(), code_url)
        if key in seen:
            continue
        if len(records) >= max_records:
            errors.append({"line_no": line_no, "reason": f"单次最多处理 {max_records} 个邮箱，后续内容已忽略"})
            break
        seen.add(key)
        records.append({"line_no": line_no, **parsed})

    return {"records": records, "errors": errors, "count": len(records)}


def mask_code_url(code_url: str) -> str:
    """Return a display-safe URL that hides secret path/query components."""
    try:
        parsed = urlsplit(str(code_url or "").strip())
        tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        suffix = f"/{tail}" if tail else ""
        hostname = parsed.hostname or "已隐藏"
        return f"{parsed.scheme}://{hostname}/…{suffix}"
    except Exception:
        return "已隐藏"


def _resolve_mailbox_url(email: str, code_url: str) -> str:
    """Resolve known mailbox viewer pages to their JSON API endpoint.

    Existing provider URLs are returned byte-for-byte.  Only the known
    email2 viewer host is rewritten, and the address is taken from its query
    (falling back to the supplied account email when the query is absent).
    """
    raw = str(code_url or "").strip()
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/").lower()
        if host != _EMAIL2_VIEWER_HOST or path != "/client/mailbox":
            return raw
        address = (parse_qs(parsed.query).get("address") or [email])[0].strip()
        if not address:
            return raw
        return urlunsplit((
            "https",
            _EMAIL2_API_HOST,
            _EMAIL2_API_PATH,
            urlencode({"address": address}),
            "",
        ))
    except Exception:
        return raw


def _message_received_after(text: str, after_ts: float | None) -> bool:
    """Return whether a JSON mailbox message is newer than ``after_ts``.

    The provider currently emits ``YYYY-MM-DD HH:MM:SS`` without a timezone;
    interpret that form in the local timezone used by the registration worker.
    If the response has no parseable timestamp, keep the previous permissive
    behavior and let the OTP content decide.
    """
    if not after_ts:
        return True
    try:
        payload = json.loads(text or "")
        received = payload.get("email", {}).get("received_at") if isinstance(payload, dict) else None
        if not received:
            return True
        value = str(received).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        # Allow a small clock skew between the registration worker and the
        # mailbox service while still rejecting clearly older messages.
        return parsed.timestamp() >= float(after_ts) - 2.0
    except Exception:
        return True


def fetch_latest_otp_from_url(email: str, code_url: str, *, timeout: int = 20) -> dict:
    """Fetch one OTP URL immediately; this is read-only and never touches the email pool."""
    email = str(email or "").strip()
    code_url = str(code_url or "").strip()
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    masked_url = mask_code_url(code_url)
    parsed_url = urlsplit(code_url)
    if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
        return {
            "ok": False, "email": email, "code": None, "http_status": None,
            "fetched_at": fetched_at, "url": masked_url,
            "error": "取码链接必须是有效的 http/https 地址",
        }

    headers = {
        "Accept": "application/json,text/html,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (compatible; gpt-register-otp-viewer/1.0)",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "If-Modified-Since": "0",
    }
    request_url = _resolve_mailbox_url(email, code_url)
    try:
        response = requests.get(request_url, headers=headers, timeout=max(3, min(int(timeout), 30)), verify=False)
    except requests.Timeout:
        return {
            "ok": False, "email": email, "code": None, "http_status": None,
            "fetched_at": fetched_at, "url": masked_url, "error": "请求超时",
        }
    except requests.RequestException as exc:
        return {
            "ok": False, "email": email, "code": None, "http_status": None,
            "fetched_at": fetched_at, "url": masked_url,
            "error": f"请求失败：{type(exc).__name__}",
        }

    status = int(response.status_code)
    if status != 200:
        return {
            "ok": False, "email": email, "code": None, "http_status": status,
            "fetched_at": fetched_at, "url": masked_url, "error": f"HTTP {status}",
        }

    response_text = (response.text or "")[:2_000_000]
    code = _extract_code(response_text)
    # A known viewer page should never be parsed as a generic HTML source.
    # This guard also protects callers that bypass _resolve_mailbox_url in a
    # custom transport or receive a cached SPA response from an upstream.
    if (
        (parsed_url.hostname or "").lower() == _EMAIL2_VIEWER_HOST
        and not response_text.lstrip().startswith("{")
    ):
        code = None
    if not code:
        return {
            "ok": False, "email": email, "code": None, "http_status": status,
            "fetched_at": fetched_at, "url": masked_url,
            "error": "HTTP 200，但响应中未识别到 6 位验证码",
        }
    return {
        "ok": True, "email": email, "code": code, "http_status": status,
        "fetched_at": fetched_at, "url": masked_url, "error": "",
    }


def _flatten_json(obj) -> str:
    parts: list[str] = []
    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x is not None:
            parts.append(str(x))
    walk(obj)
    return "\n".join(parts)


def _extract_code(text: str) -> str | None:
    """从纯文本/HTML/JSON 文本中提取 6 位 OTP。"""
    if not text:
        return None

    # 兼容 JSON：优先把所有 value 拉平再抽取。
    candidates_text = [text]
    try:
        parsed = json.loads(text)
        candidates_text.insert(0, _flatten_json(parsed))
    except Exception:
        pass

    for body in candidates_text:
        # Do not interpret CSS colors (for example the SPA splash color
        # ``#111827``) as a six-digit verification code when the response is
        # HTML without an actual mailbox message.
        body = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"(?<![0-9A-Fa-f])#[0-9A-Fa-f]{6}(?![0-9A-Fa-f])", " ", body)
        # 复用邮件 OTP 抽取逻辑。
        code = extract_otp({"text": body, "content": body, "subject": body[:200]})
        if code:
            return code

        codes = _CODE_REGEX.findall(body)
        if not codes:
            continue
        lower = body.lower()
        for code in codes:
            idx = lower.find(code)
            window = lower[max(0, idx - 80): idx + 86]
            if any(w.lower() in window for w in _CONTEXT_WORDS):
                return code
        return codes[-1]
    return None


def pick_account() -> GenericApiEmailAccount:
    """领取一个可用通用 API 邮箱。"""
    from core.db import claim_next_generic_api_email, generic_api_email_pool_summary

    inserted, skipped = import_from_file()
    if inserted:
        logger.info(f"[GenericAPI] 已自动从 {_ACCOUNTS_FILE.name} 导入 {inserted} 个邮箱（跳过 {skipped} 个）")

    row = claim_next_generic_api_email()
    if row is None:
        summary = generic_api_email_pool_summary()
        raise GenericApiMailError(
            f"通用 API 邮箱池没有可用账号: {summary}. 请在 WebUI 邮箱池导入：邮箱----取码地址"
        )
    account = GenericApiEmailAccount(email=row["email"], code_url=row["code_url"], password=row.get("password") or "")
    _CONTEXT_CACHE[account.email] = account
    logger.info(f"[GenericAPI] 选中邮箱: {account.email}（DB id={row.get('id')}）")
    return account


def import_from_file(path: str | Path | None = None) -> tuple[int, int]:
    """从文本文件导入通用 API 邮箱，每行：email----code_url 或 email====code_url。"""
    from core.db import import_generic_api_emails
    p = Path(path) if path else _ACCOUNTS_FILE
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    if not p.exists():
        return 0, 0
    records = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        parsed = parse_generic_api_line(raw)
        if parsed:
            records.append(parsed)
            continue
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----") if "----" in line else line.split("====")
        parts = [x.strip() for x in parts]
        if len(parts) < 2:
            continue
        records.append({"email": parts[0], "code_url": parts[1]})
    return import_generic_api_emails(records)


def get_account_context(email: str) -> GenericApiEmailAccount | None:
    # DB 中的查看链接可能被“邮箱链接注册”入口更新；始终以最新池记录为准，
    # 避免失败重试仍使用进程缓存里的旧链接。
    from core.db import get_generic_api_email_by_email
    row = get_generic_api_email_by_email(email)
    if row is None:
        _CONTEXT_CACHE.pop(email, None)
        return None
    account = GenericApiEmailAccount(email=row["email"], code_url=row["code_url"], password=row.get("password") or "")
    _CONTEXT_CACHE[email] = account
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    from core.db import release_generic_api_email
    release_generic_api_email(email, status=status, note=note)
    _CONTEXT_CACHE.pop(email, None)


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """
    轮询该邮箱配置的 code_url，直到提取到 6 位验证码或超时。

    settle 机制：首次拿到验证码后不立刻返回，而是继续等 OTP_SETTLE_SECONDS 秒。
    如果期间取码地址返回了不同验证码，则替换候选并重置 settle 倒计时；
    连续 settle 秒没有变化后才返回，避免取到接口缓存中的旧码。
    """
    account = get_account_context(email)
    if account is None:
        raise GenericApiMailError(f"通用 API 邮箱不存在或未导入: {email}")

    deadline = time.time() + (max_wait or _email_cfg.OTP_MAX_WAIT)
    interval = poll_interval or _email_cfg.OTP_POLL_INTERVAL
    settle = settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (compatible; gpt-register/1.0)",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "If-Modified-Since": "0",
    }
    last_error = ""
    best_otp: str | None = None
    best_seen_at: float = 0.0
    settle_until: float | None = None
    logger.info(
        f"[GenericAPI] 开始轮询取码地址: {email}，"
        f"最长 {max_wait or _email_cfg.OTP_MAX_WAIT}s, settle={settle}s"
    )

    while time.time() < deadline:
        try:
            request_url = _resolve_mailbox_url(email, account.code_url)
            resp = requests.get(request_url, headers=headers, timeout=20, verify=False)
            text = resp.text or ""
            if resp.status_code == 200:
                code = _extract_code(text) if _message_received_after(text, after_ts) else None
                if code:
                    now_seen = time.time()
                    if not best_otp:
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                        logger.info(
                            f"[GenericAPI] 首次锁定 OTP={code}, "
                            f"等 {settle}s 看取码接口是否出现更新验证码..."
                        )
                    elif code != best_otp:
                        logger.info(
                            f"[GenericAPI] 发现更新 OTP={code}，"
                            f"替换之前的 {best_otp}, 重置 settle 计时"
                        )
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                    else:
                        logger.debug(f"[GenericAPI] 取码接口仍返回候选 OTP={best_otp}")
                else:
                    last_error = f"HTTP 200 但未提取到 6 位验证码，响应预览: {text[:160]}"
            else:
                last_error = f"HTTP {resp.status_code}: {text[:160]}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        now = time.time()
        if best_otp and settle_until is not None and now >= settle_until:
            logger.info(
                f"[GenericAPI] settle 完成，返回 OTP={best_otp}, "
                f"候选锁定时间={time.strftime('%H:%M:%S', time.localtime(best_seen_at))}"
            )
            return best_otp

        remaining = int(deadline - now)
        if best_otp and settle_until is not None:
            logger.info(
                f"[GenericAPI] 已锁定候选 OTP={best_otp}，等 settle 中"
                f"（剩余 settle ~{max(0, int(settle_until - now))}s, 总剩余 {remaining}s）..."
            )
        else:
            logger.info(
                f"[GenericAPI] 暂未从取码接口拿到验证码，"
                f"{interval}s 后重试（剩余 {remaining}s）..."
            )
        time.sleep(interval)

    if best_otp:
        logger.warning(f"[GenericAPI] 总超时但已有候选，返回 OTP={best_otp}")
        return best_otp

    raise GenericApiMailError(f"等待通用 API 验证码超时: {email}; {last_error}")
