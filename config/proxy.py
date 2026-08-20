# -*- coding: utf-8 -*-
"""
代理池配置

注册机使用「专用出口池」自由轮换 IP，与本机 v2ray（10808）隔离，不占用/不跟随系统代理。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐）
"""
from __future__ import annotations

import json
import logging
import random
import ipaddress
import re
import secrets
import string
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from config.env_loader import apply_env_overrides

# 默认代理池由 ThorData API 动态填充；PROXY_POOL 仅保留手动/旧配置兼容。
PROXY_POOL: list[str] = [
    # THORDATA_ENABLED=False 时可手动填写其他代理。
]

# ---- ThorData 动态 HTTPS 代理 ----
THORDATA_ENABLED: bool = True
THORDATA_CUSTOMER: str = ""
THORDATA_SESSION_TYPE: int = 2
THORDATA_NUMBER: int = 16
THORDATA_COUNTRY: str = "US"
THORDATA_API_BASE: str = "https://get-ip.thordata.net/api"
THORDATA_IPINFO_URL: str = "https://ipinfo.thordata.com"
THORDATA_POOL_TTL: float = 300.0
THORDATA_PROXY_INSECURE: bool = True
THORDATA_PURITY_CHECK: bool = False
THORDATA_PROXYCHECK_URL: str = "https://proxycheck.io/v2/{ip}?vpn=1&asn=1&risk=1"
THORDATA_BLACKBOX_URL: str = "https://blackbox.ipinfo.app/lookup/{ip}"

_THORDATA_LOCK = threading.RLock()
_THORDATA_REFRESHED_AT = 0.0
# 代理入口与真实出口的最后一次探测结果。保留 _THORDATA_META 别名兼容旧调用方。
_PROXY_META: dict[str, dict] = {}
_THORDATA_META = _PROXY_META
_THORDATA_COUNTRY_POOLS: dict[tuple[str, int, int], list[str]] = {}
_THORDATA_COUNTRY_REFRESHED_AT: dict[tuple[str, int, int], float] = {}
# ---- Cliproxy 白名单提取池（THORDATA 关闭时的自动出口源）----
CLIPROXY_POOL_ENABLED: bool = False
CLIPROXY_EXTRACT_URL: str = ""
CLIPROXY_EXTRACT_URLS: list[str] = []
CLIPROXY_POOL_TTL: float = 300.0
# Cliproxy white/api 的 ``type=txt`` 是响应格式，不是代理协议。
CLIPROXY_PROXY_SCHEME: str = "socks5h"
# 新提取的 session 可能需要几秒才能固定出口；注册前先等待，避免把短暂的 403 当作坏 IP。
CLIPROXY_PROXY_WARMUP_SECONDS: float = 3.0
CLIPROXY_SESSION_TTL_MINUTES: int = 5

_CLIP_LOCK = threading.RLock()
_CLIP_REFRESHED_AT = 0.0
_CLIP_REGION_IDX = -1
_CLIP_LAST_ERROR = ""
_CLIP_SEEN_FILE = Path(__file__).resolve().parent.parent / "注册日志" / "_clip_seen.txt"
_CLIP_SEEN: set[str] = set()


def _clip_load_seen() -> None:
    global _CLIP_SEEN
    try:
        if _CLIP_SEEN_FILE.exists():
            _CLIP_SEEN = {
                line.strip()
                for line in _CLIP_SEEN_FILE.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
    except Exception:
        pass


def _clip_save_seen() -> None:
    try:
        _CLIP_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CLIP_SEEN_FILE.write_text("\n".join(sorted(_CLIP_SEEN)), encoding="utf-8")
    except Exception:
        pass


PLAN_CHECK_PROXY_MODE = "proxy"
PLAN_CHECK_PROXY = ""
PLAN_CHECK_THORDATA_COUNTRY = "JP"
PLAN_CHECK_THORDATA_NUMBER = 3
PLAN_CHECK_CLIPROXY_COUNTRY = "JP"
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 2
PLAN_CHECK_RETRY_DELAY = 1.5
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 0.4
PLAN_CHECK_JITTER = 0.3

# 是否自动拉起注册专用 xray（独立进程，不碰 10808）
REG_PROXY_AUTO_START = False

# ---- 出口健康 / 卡顿换 IP ----
# 探测超时（秒）：超过视为卡顿，换下一个出口
PROXY_PROBE_TIMEOUT: float = 5.0
# 探测 URL（需能快速返回；默认用 ipify）
PROXY_PROBE_URL: str = "https://ipinfo.thordata.com"
# 是否强制校验出口国家与期望国家一致。CLIPROXY 会话被兜底路由到
# 非预期国家（如 sid=region-GB 实际出口 JP）时，可设 False 跳过。
PROXY_ENFORCE_COUNTRY: bool = True
# 选出口时最多试几个候选（含探测）
PROXY_PICK_PROBE_CANDIDATES: int = 4
# 卡顿/失败后临时拉黑秒数，避免马上又抽到同一死节点
PROXY_TEMP_BAN_SECONDS: float = 180.0
# 注册流程因 IP 卡顿自动换出口重开浏览器的最大次数
PROXY_SWITCH_MAX: int = 3
# 同一代理收到 OpenAI 403 时至少确认几次，避免边缘节点短暂抖动造成误判。
PROXY_403_CONFIRM_ATTEMPTS: int = 3


def proxy_required() -> bool:
    """配置了专用代理时，业务流量禁止静默回退到本机直连。"""
    return bool(THORDATA_ENABLED or CLIPROXY_POOL_ENABLED or PROXY_POOL)


def _is_https_proxy(proxy: str | None) -> bool:
    return str(proxy or "").strip().lower().startswith("https://")


def proxy_allowed(proxy: str | None) -> bool:
    """判断代理是否符合当前模式；ThorData 只接受其 HTTPS 入口。"""
    value = str(proxy or "").strip()
    if not value:
        return False
    if THORDATA_ENABLED:
        return _is_https_proxy(value)
    normalized = _normalize_proxy_url(value)
    if normalized is None or _is_forbidden_local_proxy(normalized):
        return False
    if CLIPROXY_POOL_ENABLED:
        expected_scheme = str(CLIPROXY_PROXY_SCHEME or "socks5h").strip().lower()
        return normalized.lower().startswith(f"{expected_scheme}://")
    return True


def _normalize_proxy_url(value: str | None, *, default_scheme: str = "http") -> str | None:
    """规范化代理入口并保留认证信息。

    同时兼容 Cliproxy 常见的 ``host:port:user:password`` 纯协议格式。
    """
    text = str(value or "").strip()
    if not text:
        return None


    if "://" not in text and "@" not in text:
        # Cliproxy 也会返回 host:port:user:password；密码中的冒号保留。
        parts = text.split(":", 3)
        if len(parts) == 4 and parts[1].isdigit() and parts[0] and parts[2]:
            host, port, username, password = parts
            text = (
                f"{default_scheme}://{quote(username, safe='')}:{quote(password, safe='')}"
                f"@{host}:{port}"
            )
    candidate = text if "://" in text else f"{default_scheme}://{text}"
    try:
        parsed = urlparse(candidate)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "socks5", "socks5h"}:
            return None
        if not parsed.hostname or not parsed.port:
            return None
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        credentials = ""
        if parsed.username is not None:
            credentials = quote(unquote(parsed.username), safe="")
            if parsed.password is not None:
                credentials += f":{quote(unquote(parsed.password), safe='')}"
            credentials += "@"
        return f"{scheme}://{credentials}{host}:{parsed.port}"
    except (TypeError, ValueError):
        return None


def normalize_proxy_url(value: str | None, *, default_scheme: str | None = None) -> str | None:
    """公开的代理规范化入口，供显式手动代理和会话层复用。"""
    scheme = str(default_scheme or CLIPROXY_PROXY_SCHEME or "http").strip().lower()
    return _normalize_proxy_url(value, default_scheme=scheme)


def ensure_cliproxy_session(proxy: str | None) -> str | None:
    """为无 SID 的 Cliproxy 认证入口补一个会话级固定出口。"""
    normalized = normalize_proxy_url(proxy)
    if not normalized:
        return normalized
    try:
        parsed = urlparse(normalized)
        hostname = str(parsed.hostname or "").lower()
        username = unquote(parsed.username or "")
        if not hostname.endswith(".cliproxy.io") or not username or "-sid-" in username.lower():
            return normalized
        sid = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
        ttl = max(1, min(60, int(CLIPROXY_SESSION_TTL_MINUTES or 5)))
        password = unquote(parsed.password or "")
        host = f"[{parsed.hostname}]" if ":" in str(parsed.hostname or "") else parsed.hostname
        credentials = quote(f"{username}-sid-{sid}-t-{ttl}", safe="")
        if parsed.password is not None:
            credentials += f":{quote(password, safe='')}"
        return f"{parsed.scheme}://{credentials}@{host}:{parsed.port}"
    except (TypeError, ValueError):
        return normalized


def rotate_cliproxy_sessions(*, exclude: list[str] | tuple[str, ...] | set[str] | None = None) -> list[str]:
    """为静态 Cliproxy 账号重新生成 session，避免 403 后反复使用同一批出口。

    white/api 被白名单拦截时无法提取新列表，但带 region 的基础账号仍可通过新的
    ``sid`` 获取新会话。只替换池中已有的 Cliproxy 条目，不影响其他代理。
    """
    global PROXY_POOL
    excluded = {str(x or "").strip() for x in (exclude or []) if str(x or "").strip()}
    rotated: list[str] = []
    seen: set[str] = set()
    sid_pattern = re.compile(r"-sid-[^-@]+-t-\d+$", re.IGNORECASE)
    for raw in list(PROXY_POOL or []):
        value = normalize_proxy_url(raw)
        if not value:
            continue
        try:
            parsed = urlparse(value)
            host = str(parsed.hostname or "").lower()
            username = unquote(parsed.username or "")
            if not host.endswith(".cliproxy.io") or not username:
                continue
            base_username = sid_pattern.sub("", username)
            if base_username == username:
                continue
            password = unquote(parsed.password or "")
            base = f"{parsed.scheme}://{quote(base_username, safe='')}:{quote(password, safe='')}@{host}:{parsed.port}"
            fresh = ensure_cliproxy_session(base)
            if fresh and fresh not in seen and fresh not in excluded:
                seen.add(fresh)
                rotated.append(fresh)
        except (TypeError, ValueError):
            continue
    if rotated:
        PROXY_POOL = rotated
        logging.getLogger(__name__).info("[Cliproxy] 已轮换静态 session，新池数量=%s", len(rotated))
    return rotated


def new_cliproxy_country_session(country: str) -> str:
    """Create a fresh Cliproxy session for an isolated plan-check country."""
    country_code = _normalize_country(country, default="JP")
    sid_pattern = re.compile(r"-sid-[^-@]+-t-\d+$", re.IGNORECASE)
    region_pattern = re.compile(r"(?:^|-)region-[a-z]{2}(?=-|$)", re.IGNORECASE)
    for raw in list(PROXY_POOL or []):
        value = normalize_proxy_url(raw)
        if not value:
            continue
        try:
            parsed = urlparse(value)
            host = str(parsed.hostname or "").lower()
            username = unquote(parsed.username or "")
            if not host.endswith(".cliproxy.io") or not username:
                continue
            base_username = sid_pattern.sub("", username)
            if region_pattern.search(base_username):
                base_username = region_pattern.sub(
                    lambda match: match.group(0).replace(match.group(0)[-2:], country_code),
                    base_username,
                )
            else:
                base_username = f"{base_username}-region-{country_code}"
            password = unquote(parsed.password or "")
            base = f"{parsed.scheme}://{quote(base_username, safe='')}:{quote(password, safe='')}@{host}:{parsed.port}"
            return ensure_cliproxy_session(base) or ""
        except (TypeError, ValueError):
            continue
    return ""


def proxy_curl_options(proxy: str | None) -> dict:
    """仅关闭 HTTPS 代理入口证书校验，不关闭目标站 TLS 校验。"""
    if not (_is_https_proxy(proxy) and bool(THORDATA_PROXY_INSECURE)):
        return {}
    try:
        from curl_cffi.const import CurlOpt
        return {
            CurlOpt.PROXY_SSL_VERIFYPEER: 0,
            CurlOpt.PROXY_SSL_VERIFYHOST: 0,
        }
    except Exception:
        return {}


def _normalize_thordata_entries(text: str) -> list[str]:
    """把 ThorData 的 IP:port 响应规范化为 HTTPS 代理 URL。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        entry = raw.strip()
        if not entry:
            continue
        candidate = entry if "://" in entry else f"https://{entry}"
        try:
            parsed = urlparse(candidate)
            if parsed.scheme.lower() != "https" or not parsed.hostname or not parsed.port:
                continue
        except Exception:
            continue
        normalized = f"https://{parsed.hostname}:{parsed.port}"
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _normalize_country(country: str | None, *, default: str = "US") -> str:
    value = str(country or default).strip().upper()
    return value or default


def _thordata_pool_key(country: str, number: int | None = None) -> tuple[str, int, int]:
    return (
        _normalize_country(country),
        max(1, int(THORDATA_SESSION_TYPE or 2)),
        max(1, min(100, int(number if number is not None else THORDATA_NUMBER or 16))),
    )


def _request_thordata_entries(*, country: str, number: int) -> list[str]:
    customer = str(THORDATA_CUSTOMER or "").strip()
    if not customer:
        raise RuntimeError("ThorData 已启用，但 THORDATA_CUSTOMER 未配置")
    from curl_cffi import requests as creq

    query = urlencode({
        "td-customer": customer,
        "sesstype": max(1, int(THORDATA_SESSION_TYPE or 2)),
        "number": max(1, min(100, int(number or 1))),
        "country": _normalize_country(country),
    })
    url = f"{str(THORDATA_API_BASE or '').rstrip('/')}?{query}"
    resp = creq.get(url, timeout=max(10.0, float(PROXY_PROBE_TIMEOUT or 5.0) * 3))
    if int(resp.status_code or 0) != 200:
        raise RuntimeError(f"ThorData 入口 API HTTP {resp.status_code}")
    pool = _normalize_thordata_entries(resp.text or "")
    if not pool:
        raise RuntimeError("ThorData 入口 API 未返回有效 IP:port")
    return pool


def fetch_thordata_country_pool(
    country: str,
    number: int | None = None,
    *,
    force: bool = False,
) -> list[str]:
    """获取一个不会覆盖注册 ``PROXY_POOL`` 的国家专用 ThorData 入口池。"""
    key = _thordata_pool_key(country, number)
    ttl = max(10.0, float(THORDATA_POOL_TTL or 300.0))
    now = time.monotonic()
    with _THORDATA_LOCK:
        cached = _THORDATA_COUNTRY_POOLS.get(key) or []
        refreshed_at = float(_THORDATA_COUNTRY_REFRESHED_AT.get(key) or 0.0)
        if not force and cached and refreshed_at and now - refreshed_at < ttl:
            return list(cached)
        try:
            pool = _request_thordata_entries(country=key[0], number=key[2])
        except Exception:
            if cached:
                return list(cached)
            raise
        _THORDATA_COUNTRY_POOLS[key] = list(pool)
        _THORDATA_COUNTRY_REFRESHED_AT[key] = now
        logging.getLogger(__name__).info(
            "[ThorData] 已刷新国家专用 HTTPS 入口 count=%s country=%s ttl=%ss",
            len(pool), key[0], int(ttl),
        )
        return list(pool)


def _fetch_thordata_pool(*, force: bool = False) -> list[str]:
    """获取动态入口；API 返回的是入口节点，不把它当作真实出口。"""
    global _THORDATA_REFRESHED_AT
    customer = str(THORDATA_CUSTOMER or "").strip()
    if not customer:
        raise RuntimeError("ThorData 已启用，但 THORDATA_CUSTOMER 未配置")
    ttl = max(10.0, float(THORDATA_POOL_TTL or 300.0))
    now = time.monotonic()
    with _THORDATA_LOCK:
        if not force and PROXY_POOL and _THORDATA_REFRESHED_AT and now - _THORDATA_REFRESHED_AT < ttl:
            return list(PROXY_POOL)
        pool = _request_thordata_entries(
            country=_normalize_country(THORDATA_COUNTRY),
            number=max(1, min(100, int(THORDATA_NUMBER or 16))),
        )
        PROXY_POOL[:] = pool
        _THORDATA_REFRESHED_AT = now
        logging.getLogger(__name__).info(
            "[ThorData] 已刷新 HTTPS 入口 count=%s country=%s ttl=%ss",
            len(pool), str(THORDATA_COUNTRY or "US").upper(), int(ttl),
        )
        return list(pool)


def _load_reg_proxy_pool() -> list[str]:
    root = Path(__file__).resolve().parent.parent / "tools" / "reg_proxy"
    for name in ("pool_ok.json", "pool.json"):
        path = root / name
        if not path.exists():
            continue
        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            out = []
            for row in data:
                p = str((row or {}).get("proxy") or "").strip()
                # 明确拒绝本机用户代理端口，防止误配回去
                if not p or ":10808" in p or ":7897" in p:
                    continue
                out.append(p)
            if out:
                return out
        except Exception:
            continue
    return []


def ensure_reg_proxy_pool(*, force_refresh: bool = False) -> list[str]:
    """确保默认代理池可用；ThorData 开启时不再启动本机 xray。"""
    global PROXY_POOL
    if THORDATA_ENABLED:
        try:
            return _fetch_thordata_pool(force=force_refresh)
        except Exception as exc:
            logging.getLogger(__name__).error("[ThorData] 入口刷新失败：%s", exc)
            # 已缓存的 ThorData HTTPS 入口可继续使用；绝不回退本机代理或直连。
            cached = [p for p in PROXY_POOL if _is_https_proxy(p)]
            PROXY_POOL = cached
            return list(cached)

    if cliproxy_pool_enabled():
        if force_refresh or not PROXY_POOL:
            refresh_cliproxy_pool(force=force_refresh)
        return list(PROXY_POOL)

    # 用户已在 .env/config 显式配置 PROXY_POOL 时，不再用本机 xray 池覆盖。
    # 判定"显式配置"：池非空且不含本机回环代理（xray 池都是 127.0.0.1 端口）。
    if PROXY_POOL and any(p and not _is_forbidden_local_proxy(p) for p in PROXY_POOL):
        return list(PROXY_POOL)

    pool = _load_reg_proxy_pool()
    if REG_PROXY_AUTO_START:
        try:
            import importlib.util
            manage = Path(__file__).resolve().parent.parent / "tools" / "reg_proxy" / "manage.py"
            if manage.exists():
                spec = importlib.util.spec_from_file_location("reg_proxy_manage", manage)
                mod = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(mod)
                pool = mod.ensure_running() or pool
                # filter 10808 again
                pool = [p for p in pool if p and ":10808" not in p and ":7897" not in p]
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("[proxy] 注册专用代理启动失败：%s", exc)
    if pool:
        PROXY_POOL = pool
    return list(PROXY_POOL)


def _is_forbidden_local_proxy(proxy: str) -> bool:
    """本机 v2ray/系统代理端口，注册链路禁止使用。"""
    value = str(proxy or "").strip().lower()
    if not value:
        return False
    return (
        ":10808" in value
        or ":7897" in value
        or value.endswith("10808")
        or value.endswith("7897")
    )


# 临时拉黑：proxy -> 解禁时间戳（monotonic）
_TEMP_BANNED: dict[str, float] = {}


def _mask_proxy(proxy: str) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        if "@" in value:
            scheme, rest = value.split("://", 1) if "://" in value else ("", value)
            cred, host = rest.rsplit("@", 1)
            return f"{scheme}://***@{host}" if scheme else f"***@{host}"
        return value
    except Exception:
        return value[:32]


def _proxy_entry_meta(proxy: str) -> dict:
    """解析代理入口；入口地址永远不作为真实出口地址返回。"""
    try:
        parsed = urlparse(str(proxy or "").strip())
        host = str(parsed.hostname or "").strip()
        port = int(parsed.port) if parsed.port else None
    except (TypeError, ValueError):
        host, port = "", None
    gateway_ip = host
    try:
        ipaddress.ip_address(host)
    except ValueError:
        gateway_ip = None
    return {
        "entry_host": host or None,
        "entry_port": port,
        "gateway_ip": gateway_ip,
    }


def _parse_probe_body(body: str) -> dict:
    """从 GeoIP JSON 或纯文本响应中提取可验证的出口信息。"""
    text = str(body or "").strip()
    parsed: dict = {}
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            parsed = value
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = {}
    if parsed:
        raw_ip = parsed.get("ip") or parsed.get("query") or parsed.get("address")
        return {
            "exit_ip": str(raw_ip or "").strip() or None,
            "country": str(parsed.get("country") or parsed.get("country_code") or "").strip().upper() or None,
            "region": parsed.get("region") or parsed.get("regionName"),
            "city": parsed.get("city"),
            "org": parsed.get("org") or parsed.get("isp"),
            "asn": parsed.get("asn"),
            "timezone": parsed.get("timezone"),
        }
    # ipify 等纯文本接口只返回 IP；不要把错误页面或网关描述当成 IP。
    match = re.search(r"(?<![\w:])(?:\d{1,3}\.){3}\d{1,3}(?![\w:])|(?<![\w:])[0-9a-fA-F:]{2,}(?<![\w:])", text)
    return {"exit_ip": match.group(0) if match else None}


def _record_proxy_meta(proxy: str, result: dict) -> None:
    p = str(proxy or "").strip()
    if p:
        _PROXY_META[p] = dict(result)


def ban_proxy(proxy: str, *, seconds: float | None = None, reason: str = "") -> None:
    """临时拉黑卡顿/失败出口。"""
    import time
    import logging
    p = str(proxy or "").strip()
    if not p:
        return
    sec = float(seconds if seconds is not None else PROXY_TEMP_BAN_SECONDS or 180)
    _TEMP_BANNED[p] = time.monotonic() + max(30.0, sec)
    logging.getLogger(__name__).warning(
        "[proxy] 临时拉黑 %s（%.0fs）%s",
        _mask_proxy(p),
        sec,
        f" reason={reason[:120]}" if reason else "",
    )


def _active_temp_bans() -> set[str]:
    import time
    now = time.monotonic()
    dead = [k for k, until in _TEMP_BANNED.items() if until <= now]
    for k in dead:
        _TEMP_BANNED.pop(k, None)
    return set(_TEMP_BANNED.keys())


def _clip_extract_urls() -> list[str]:
    urls = [str(u or "").strip() for u in (CLIPROXY_EXTRACT_URLS or []) if str(u or "").strip()]
    if not urls:
        single = str(CLIPROXY_EXTRACT_URL or "").strip()
        if single:
            urls = [single]
    return urls


def _cliproxy_fetch_pool_url(url: str) -> list[str]:
    """调用 Cliproxy 提取接口，返回保留账号密码的规范代理列表。"""
    import urllib.request
    if not str(url or "").strip():
        return []
    req = urllib.request.Request(str(url), headers={"User-Agent": "turb-gpt-free-register/cliproxy-pool"})
    # 提取 API 必须直连，不能继承 Windows/进程的系统代理设置。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as resp:
        text = resp.read().decode("utf-8", "replace")
    response = text.strip()
    lower_response = response.lower()
    if "not added to whitelist" in lower_response or "not in whitelist" in lower_response:
        ip = response.split(" ", 1)[0].strip()
        raise RuntimeError(f"Cliproxy 拒绝提取：当前公网 IP {ip or '?'} 未加入白名单")

    default_scheme = str(CLIPROXY_PROXY_SCHEME or "socks5h").strip().lower()
    if default_scheme not in {"http", "https", "socks5", "socks5h"}:
        raise RuntimeError(f"CLIPROXY_PROXY_SCHEME 不支持：{default_scheme}")
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        entry = raw.strip()
        if not entry:
            continue
        normalized = _normalize_proxy_url(entry, default_scheme=default_scheme)
        if not normalized:
            continue
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    if response and not out:
        preview = response.replace("\r", " ").replace("\n", " ")[:160]
        raise RuntimeError(f"Cliproxy 提取接口未返回有效代理：{preview}")
    return out


def cliproxy_pool_enabled() -> bool:
    return bool(CLIPROXY_POOL_ENABLED) and not THORDATA_ENABLED and bool(_clip_extract_urls())


def cliproxy_expected_country() -> str:
    """Return the country requested by the active Cliproxy extraction URL."""
    for url in _clip_extract_urls():
        try:
            values = parse_qs(urlparse(url).query).get("region") or []
            country = str(values[0] if values else "").strip().upper()
            if country:
                return country
        except Exception:
            continue
    return ""


def _cliproxy_proxy_country(proxy: str | None) -> str:
    """从静态 Cliproxy 认证用户名的 ``region-XX`` 段提取目标国家。"""
    try:
        parsed = urlparse(normalize_proxy_url(proxy) or "")
        if not str(parsed.hostname or "").lower().endswith("cliproxy.io"):
            return ""
        username = unquote(parsed.username or "")
        match = re.search(r"(?:^|-)region-([a-z]{2})(?:-|$)", username, flags=re.IGNORECASE)
        return _normalize_country(match.group(1), default="") if match else ""
    except Exception:
        return ""


def refresh_cliproxy_pool(*, force: bool = False) -> bool:
    """按 TTL 或强制刷新 cliproxy 提取池；多地区 URL 轮转（每号换一个地区）。"""
    global PROXY_POOL, _CLIP_REFRESHED_AT, _CLIP_REGION_IDX, _CLIP_LAST_ERROR
    urls = _clip_extract_urls()
    if not urls or not cliproxy_pool_enabled():
        return False
    with _CLIP_LOCK:
        now = time.time()
        if not force and _CLIP_REFRESHED_AT > 0 and (now - _CLIP_REFRESHED_AT) < CLIPROXY_POOL_TTL:
            return True
        # 过滤掉已用过/临时拉黑的 IP，只保留新 IP
        banned_now = _active_temp_bans()
        fetched_any = False
        for _ in range(len(urls)):
            _CLIP_REGION_IDX = (_CLIP_REGION_IDX + 1) % len(urls)
            url = urls[_CLIP_REGION_IDX]
            try:
                fresh = _cliproxy_fetch_pool_url(url)
            except Exception as exc:
                _CLIP_LAST_ERROR = str(exc)[:240]
                logging.getLogger(__name__).warning(
                    "[Cliproxy] 提取失败: %s", _CLIP_LAST_ERROR,
                )
                continue
            fetched_any = True
            _CLIP_LAST_ERROR = ""
            fresh = [p for p in fresh if p not in _CLIP_SEEN and p not in banned_now]
            if fresh:
                PROXY_POOL = fresh
                _CLIP_SEEN.update(fresh)
                _clip_save_seen()
                _CLIP_REFRESHED_AT = time.time()
                _CLIP_LAST_ERROR = ""
                logging.getLogger(__name__).info(
                    "[Cliproxy] 已刷新提取池（地区轮转 idx=%s, 过滤已用后 %s 个）",
                    _CLIP_REGION_IDX, len(fresh),
                )
                return True
        if not fetched_any:
            return False
        logging.getLogger(__name__).warning("[Cliproxy] 提取到的都是已用/拉黑 IP，重置 seen 记录重试")
        _CLIP_SEEN.clear()
        _clip_save_seen()
        return False


def last_cliproxy_error() -> str:
    return str(_CLIP_LAST_ERROR or "").strip()


def list_proxy_pool(*, exclude: list[str] | tuple[str, ...] | set[str] | None = None) -> list[str]:
    """当前可用出口列表（已滤禁端口 + 临时拉黑 + exclude）。"""
    if THORDATA_ENABLED:
        pool = ensure_reg_proxy_pool()
        pool = [p for p in pool if _is_https_proxy(p)]
    elif cliproxy_pool_enabled():
        # Cliproxy 是显式代理来源；提取失败时不能静默混入旧的本机 xray 池。
        refresh_cliproxy_pool()
        pool = [
            p for p in PROXY_POOL
            if p and proxy_allowed(p) and not _is_forbidden_local_proxy(p)
        ]
    else:
        pool = PROXY_POOL or _load_reg_proxy_pool()
        pool = [p for p in pool if p and not _is_forbidden_local_proxy(p)]
        if not pool:
            pool = [p for p in ensure_reg_proxy_pool() if p and not _is_forbidden_local_proxy(p)]
    explicit_banned = {str(x or "").strip() for x in (exclude or []) if str(x or "").strip()}
    temp_banned = _active_temp_bans()
    available = [p for p in pool if p not in explicit_banned and p not in temp_banned]
    if available or not (cliproxy_pool_enabled() and pool):
        return available
    # white/api 白名单失败时不会产生新池；不要因为上一轮临时拉黑就让后续任务拿不到任何入口。
    # 当前任务仍通过 exclude 避免重复选择，下一次探测可重新确认入口是否恢复。
    if _CLIP_LAST_ERROR and ("白名单" in _CLIP_LAST_ERROR or "whitelist" in _CLIP_LAST_ERROR.lower()):
        logging.getLogger(__name__).warning(
            "[Cliproxy] 动态提取不可用，静态池入口已耗尽临时黑名单，恢复静态池供任务重试"
        )
        return [p for p in pool if p not in explicit_banned]
    return available


def pick_proxy(*, exclude: list[str] | tuple[str, ...] | set[str] | None = None) -> str:
    """随机抽一个注册专用出口；池空返回空串。

    exclude: 本轮已失败的代理，重试时尽量换出口，避免同一死节点连撞。
    """
    pool = list_proxy_pool(exclude=exclude)
    if pool:
        return random.choice(pool)
    if proxy_required():
        detail = last_cliproxy_error()
        suffix = f"：{detail}" if detail else ""
        raise RuntimeError(f"代理池没有可用入口，已禁止回退本机直连{suffix}")
    return ""


def list_country_proxy_pool(
    country: str,
    *,
    number: int | None = None,
    exclude: list[str] | tuple[str, ...] | set[str] | None = None,
    force_refresh: bool = False,
) -> list[str]:
    """读取国家专用池；该操作不修改注册池和注册国家配置。"""
    if not THORDATA_ENABLED:
        return list_proxy_pool(exclude=exclude)
    pool = fetch_thordata_country_pool(country, number, force=force_refresh)
    banned = {str(x or "").strip() for x in (exclude or []) if str(x or "").strip()}
    banned |= _active_temp_bans()
    return [p for p in pool if _is_https_proxy(p) and p not in banned]


def pick_country_proxy(
    country: str,
    *,
    number: int | None = None,
    exclude: list[str] | tuple[str, ...] | set[str] | None = None,
) -> str:
    pool = list_country_proxy_pool(country, number=number, exclude=exclude)
    if pool:
        return random.choice(pool)
    if proxy_required():
        raise RuntimeError(f"ThorData {_normalize_country(country)} 没有可用 HTTPS 入口，已禁止回退本机直连")
    return ""


def _query_exit_purity(exit_ip: str, proxy: str, timeout: float) -> dict:
    """可选纯净度检查；请求本身仍走当前 ThorData 代理。"""
    if not THORDATA_PURITY_CHECK or not exit_ip:
        return {}
    from curl_cffi import requests as creq
    kwargs = {
        "proxies": {"http": proxy, "https": proxy},
        "timeout": timeout,
        "impersonate": "chrome146",
        "curl_options": proxy_curl_options(proxy),
    }
    result: dict = {}
    try:
        url = str(THORDATA_PROXYCHECK_URL or "").format(ip=quote(exit_ip, safe=""))
        resp = creq.get(url, **kwargs)
        data = resp.json() if int(resp.status_code or 0) == 200 else {}
        row = data.get(exit_ip) if isinstance(data, dict) else None
        if isinstance(row, dict):
            result.update({
                "proxy_detected": row.get("proxy"),
                "proxy_type": row.get("type"),
                "risk": row.get("risk"),
            })
    except Exception:
        pass
    try:
        url = str(THORDATA_BLACKBOX_URL or "").format(ip=quote(exit_ip, safe=""))
        resp = creq.get(url, **kwargs)
        if int(resp.status_code or 0) == 200:
            result["blackbox"] = str(resp.text or "").strip()[:16]
    except Exception:
        pass
    return result


def probe_proxy(
    proxy: str,
    *,
    timeout: float | None = None,
    url: str | None = None,
    expected_country: str | None = None,
) -> dict:
    """探测出口是否卡顿。

    返回: {ok, latency_ms, error, ip?}
    """
    import time
    import logging
    log = logging.getLogger(__name__)
    p = str(proxy or "").strip()
    if not p:
        return {"ok": False, "latency_ms": None, "error": "empty_proxy"}
    to = float(timeout if timeout is not None else PROXY_PROBE_TIMEOUT or 5)
    target = str(
        url
        or (THORDATA_IPINFO_URL if THORDATA_ENABLED else PROXY_PROBE_URL)
        or "https://api.ipify.org"
    ).strip()
    t0 = time.monotonic()
    try:
        try:
            from curl_cffi import requests as creq
            resp = creq.get(
                target,
                proxies={"http": p, "https": p},
                timeout=to,
                impersonate="chrome146",
                curl_options=proxy_curl_options(p),
            )
            body = (resp.text or "").strip()
            status = int(resp.status_code or 0)
        except Exception:
            import urllib.request
            # socks5h 需要 PySocks；没有则退回 http 探测失败
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": p, "https": p})
            )
            with opener.open(target, timeout=to) as resp:
                body = resp.read().decode("utf-8", "replace").strip()
                status = int(getattr(resp, "status", 200) or 200)
        ms = int((time.monotonic() - t0) * 1000)
        entry_meta = _proxy_entry_meta(p)
        if status < 200 or status >= 300:
            result = {
                "ok": False,
                "latency_ms": ms,
                "error": f"HTTP {status}",
                **entry_meta,
                "exit_ip": None,
                "ip": None,
                "verified_exit": False,
            }
            _record_proxy_meta(p, result)
            return result
        parsed_meta = _parse_probe_body(body)
        exit_ip = str(parsed_meta.get("exit_ip") or "").strip()
        try:
            if exit_ip:
                ipaddress.ip_address(exit_ip)
        except ValueError:
            exit_ip = ""
        meta = {k: v for k, v in parsed_meta.items() if k != "exit_ip"}
        # 保留 ip 兼容字段，但其含义现在明确为真实出口 IP。
        meta["ip"] = exit_ip or None
        gateway_ip = str(entry_meta.get("gateway_ip") or "").strip()
        dynamic_pool = bool(THORDATA_ENABLED or cliproxy_pool_enabled())
        verified_exit = bool(exit_ip and (not dynamic_pool or exit_ip != gateway_ip))
        expected_country_code = _normalize_country(expected_country, default="") if expected_country is not None else ""
        base = {
            **entry_meta,
            **meta,
            # exit_ip 只暴露已验证的真实出口；探测到网关时保留 observed_ip 便于诊断。
            "exit_ip": exit_ip if verified_exit else None,
            "ip": exit_ip if verified_exit else None,
            "observed_ip": exit_ip or None,
            "observed_country": meta.get("country"),
            "country": meta.get("country") if verified_exit else None,
            "verified_exit": verified_exit,
        }
        if not verified_exit:
            result = {
                "ok": False,
                "latency_ms": ms,
                "error": "未识别到真实出口 IP（探测结果仍是代理入口或无效响应）",
                **base,
            }
            _record_proxy_meta(p, result)
            return result
        if PROXY_ENFORCE_COUNTRY and expected_country_code and meta.get("country") != expected_country_code:
            result = {
                "ok": False,
                "latency_ms": ms,
                "error": f"出口国家不匹配: {meta.get('country') or '?'} != {expected_country_code}",
                **base,
            }
            _record_proxy_meta(p, result)
            return result
        purity = _query_exit_purity(exit_ip, p, to)
        result = {"ok": True, "latency_ms": ms, "error": None, **base, **purity}
        _record_proxy_meta(p, result)
        return result
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        err = f"{type(exc).__name__}: {exc}"
        log.info("[proxy] 探测失败 %s %sms %s", _mask_proxy(p), ms, err[:120])
        result = {
            "ok": False,
            "latency_ms": ms,
            "error": err[:200],
            **_proxy_entry_meta(p),
            "exit_ip": None,
            "ip": None,
            "verified_exit": False,
        }
        _record_proxy_meta(p, result)
        return result


def pick_healthy_proxy(
    *,
    exclude: list[str] | tuple[str, ...] | set[str] | None = None,
    probe: bool = True,
    max_candidates: int | None = None,
) -> str:
    """抽一个健康出口：随机候选 → 快速探测 → 卡顿则拉黑再换。

    probe=False 时退化为 pick_proxy（不发探测请求）。
    """
    import logging
    log = logging.getLogger(__name__)
    base_exclude = {str(x or "").strip() for x in (exclude or []) if str(x or "").strip()}
    tried: list[str] = []
    n = int(max_candidates if max_candidates is not None else PROXY_PICK_PROBE_CANDIDATES or 4)
    n = max(1, min(12, n))
    for _ in range(n):
        try:
            cand = pick_proxy(exclude=base_exclude | set(tried))
        except RuntimeError:
            break
        if not cand:
            break
        if cand in tried:
            break
        tried.append(cand)
        if not probe:
            return cand
        expected_country = (
            cliproxy_expected_country()
            if cliproxy_pool_enabled()
            else _cliproxy_proxy_country(cand)
        )
        result = probe_proxy(cand, expected_country=expected_country or None)
        if result.get("ok"):
            log.info(
                "[proxy] 选用代理入口 gateway=%s:%s latency=%sms exit=%s country=%s",
                result.get("entry_host") or "?",
                result.get("entry_port") or "?",
                result.get("latency_ms"),
                result.get("ip") or "?",
                result.get("country") or "?",
            )
            return cand
        ban_proxy(
            cand,
            reason=f"probe_fail: {result.get('error') or 'lag'}",
        )
        base_exclude.add(cand)
    if THORDATA_ENABLED:
        # 当前批次全挂时刷新入口再试一次；仍失败则显式返回空，让上层中止而非直连。
        refreshed = ensure_reg_proxy_pool(force_refresh=True)
        for cand in refreshed:
            if cand in tried or cand in base_exclude:
                continue
            expected_country = (
                cliproxy_expected_country()
                if cliproxy_pool_enabled()
                else _cliproxy_proxy_country(cand)
            )
            result = probe_proxy(cand, expected_country=expected_country or None)
            if result.get("ok"):
                log.info(
                    "[proxy] 刷新后选用代理入口 gateway=%s:%s latency=%sms exit=%s country=%s org=%s",
                    result.get("entry_host") or "?", result.get("entry_port") or "?",
                    result.get("latency_ms"), result.get("ip") or "?",
                    result.get("country") or "?", result.get("org") or "?",
                )
                return cand
            ban_proxy(cand, reason=f"probe_fail: {result.get('error') or 'lag'}")
        log.error("[ThorData] 所有 HTTPS 入口均不可用，禁止回退本机直连")
        return ""

    # 已探测失败的入口不能再作为“随机兜底”，否则浏览器会继续撞同一坏节点。
    if cliproxy_pool_enabled():
        # 白名单提取不可用时，仍可从现有静态 sid 池继续尝试剩余入口。
        refresh_cliproxy_pool()
        for cand in list_proxy_pool(exclude=base_exclude | set(tried))[:n]:
            result = probe_proxy(cand, expected_country=cliproxy_expected_country() or None)
            if result.get("ok"):
                return cand
            ban_proxy(cand, reason=f"probe_fail: {result.get('error') or 'lag'}")
    detail = last_cliproxy_error()
    log.error("[proxy] 没有健康代理入口%s", f"：{detail}" if detail else "")
    return ""


def pick_healthy_country_proxy(
    country: str,
    *,
    number: int | None = None,
    exclude: list[str] | tuple[str, ...] | set[str] | None = None,
    probe: bool = True,
    max_candidates: int | None = None,
) -> str:
    """从隔离的国家池选择并校验真实出口国家，不污染注册池。"""
    country_code = _normalize_country(country)
    base_exclude = {str(x or "").strip() for x in (exclude or []) if str(x or "").strip()}
    tried: list[str] = []
    n = int(max_candidates if max_candidates is not None else PROXY_PICK_PROBE_CANDIDATES or 4)
    n = max(1, min(12, n))
    for _ in range(n):
        try:
            cand = pick_country_proxy(
                country_code,
                number=number,
                exclude=base_exclude | set(tried),
            )
        except RuntimeError:
            break
        if not cand or cand in tried:
            break
        tried.append(cand)
        if not probe:
            return cand
        result = probe_proxy(cand, expected_country=country_code)
        if result.get("ok"):
            logging.getLogger(__name__).info(
                "[ThorData] 国家专用出口已就绪 country=%s proxy=%s latency=%sms exit=%s",
                country_code, _mask_proxy(cand), result.get("latency_ms"), result.get("ip") or "?",
            )
            return cand
        ban_proxy(cand, reason=f"{country_code}_probe_fail: {result.get('error') or 'lag'}")
        base_exclude.add(cand)

    try:
        refreshed = list_country_proxy_pool(
            country_code,
            number=number,
            exclude=base_exclude | set(tried),
            force_refresh=True,
        )
    except Exception:
        refreshed = []
    for cand in refreshed:
        result = probe_proxy(cand, expected_country=country_code)
        if result.get("ok"):
            return cand
        ban_proxy(cand, reason=f"{country_code}_probe_fail: {result.get('error') or 'lag'}")
    logging.getLogger(__name__).error(
        "[ThorData] %s 国家专用 HTTPS 入口均不可用，禁止回退本机直连",
        country_code,
    )
    return ""


def get_proxy_metadata(proxy: str) -> dict:
    return dict(_PROXY_META.get(str(proxy or "").strip()) or {})


def is_proxy_lag_error(exc: BaseException | str | None) -> bool:
    """判断异常是否像出口卡顿/代理不可达（应换 IP）。"""
    text = str(exc or "")
    name = type(exc).__name__ if isinstance(exc, BaseException) else ""
    blob = f"{name} {text}".lower()
    keys = (
        "timeout", "timed out", "curl: (28)", "curl: (7)", "curl: (35)",
        "connection refused", "connection reset", "proxy", "tunnel",
        "network", "err_proxy", "err_connection", "err_timed_out",
        "net::", "navigation", "page.goto", "ns_error_proxy",
        "socks", "could not connect", "failed to connect",
        "ssl", "tls", "empty response", "target closed",
        "challenge_stuck", "just a moment", "checking your browser",
        "challenge-platform", "/cdn-cgi/challenge",
    )
    return any(k in blob for k in keys)


# 先读本地 pool 文件（不强制启动，启动放 ensure / WebUI create_app）
_clip_load_seen()
_file_pool = _load_reg_proxy_pool()
if _file_pool:
    PROXY_POOL = _file_pool

PROXY = ""

apply_env_overrides(globals(), {
    'THORDATA_ENABLED': 'bool',
    'THORDATA_CUSTOMER': 'str',
    'THORDATA_SESSION_TYPE': 'int',
    'THORDATA_NUMBER': 'int',
    'THORDATA_COUNTRY': 'str',
    'THORDATA_API_BASE': 'str',
    'THORDATA_IPINFO_URL': 'str',
    'THORDATA_POOL_TTL': 'float',
    'THORDATA_PROXY_INSECURE': 'bool',
    'THORDATA_PURITY_CHECK': 'bool',
    'THORDATA_PROXYCHECK_URL': 'str',
    'THORDATA_BLACKBOX_URL': 'str',
    'CLIPROXY_POOL_ENABLED': 'bool',
    'CLIPROXY_EXTRACT_URL': 'str',
    'CLIPROXY_EXTRACT_URLS': 'list_str_multiline',
    'CLIPROXY_POOL_TTL': 'float',
    'CLIPROXY_PROXY_SCHEME': 'str',
    'CLIPROXY_PROXY_WARMUP_SECONDS': 'float',
    'CLIPROXY_SESSION_TTL_MINUTES': 'int',
    'PROXY_POOL': 'list_str_multiline',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_THORDATA_COUNTRY': 'str',
    'PLAN_CHECK_THORDATA_NUMBER': 'int',
    'PLAN_CHECK_CLIPROXY_COUNTRY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
    'REG_PROXY_AUTO_START': 'bool',
    'PROXY_PROBE_TIMEOUT': 'float',
    'PROXY_PROBE_URL': 'str',
    'PROXY_ENFORCE_COUNTRY': 'bool',
    'PROXY_PICK_PROBE_CANDIDATES': 'int',
    'PROXY_TEMP_BAN_SECONDS': 'float',
    'PROXY_SWITCH_MAX': 'int',
    'PROXY_403_CONFIRM_ATTEMPTS': 'int',
})

# .env 若误写成 10808，强制改回专用池
def _sanitize_pool() -> None:
    global PROXY_POOL, PROXY, PLAN_CHECK_PROXY, _CLIP_REFRESHED_AT
    if THORDATA_ENABLED:
        # 动态池尚未刷新前保持为空，避免误用旧的本机 xray/socks 池。
        PROXY_POOL = []
        if PLAN_CHECK_PROXY and not _is_https_proxy(PLAN_CHECK_PROXY):
            PLAN_CHECK_PROXY = ""
        PROXY = ""
        return
    cleaned = []
    for value in PROXY_POOL or []:
        normalized = _normalize_proxy_url(value)
        if normalized and not _is_forbidden_local_proxy(normalized) and normalized not in cleaned:
            cleaned.append(normalized)
    if CLIPROXY_POOL_ENABLED:
        cleaned = [value for value in cleaned if proxy_allowed(value)]
    if not cleaned and not CLIPROXY_POOL_ENABLED:
        cleaned = [
            normalized
            for value in _load_reg_proxy_pool()
            if (normalized := _normalize_proxy_url(value)) and not _is_forbidden_local_proxy(normalized)
        ]
    PROXY_POOL = cleaned
    # 显式静态池可作为进程启动缓存；注册开始时仍会 force 刷新动态提取接口。
    if CLIPROXY_POOL_ENABLED and cleaned and not _CLIP_REFRESHED_AT:
        _CLIP_REFRESHED_AT = time.time()
    if PLAN_CHECK_PROXY and not proxy_allowed(PLAN_CHECK_PROXY):
        PLAN_CHECK_PROXY = ""
    PROXY = pick_proxy() if PROXY_POOL else ""

_sanitize_pool()
