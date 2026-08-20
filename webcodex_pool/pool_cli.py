#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_cli.py — 号池命令行客户端

通过 WebUI 的 HTTP API（/api/pool/*）操作 ChatGPT 账号池：
  acquire / switch / summary / list / probe / disable / enable

鉴权：读取项目根目录 .env 中的 WEBUI_AUTH_CODE，作为 X-Auth-Code 头发送。

用法示例：
  python pool_cli.py summary
  python pool_cli.py list --status available
  python pool_cli.py acquire --raw
  python pool_cli.py acquire --email xxx@yyy.com
  python pool_cli.py switch --email old@yyy.com --reason quota_exhausted
  python pool_cli.py probe
  python pool_cli.py disable 12 --reason abuse
  python pool_cli.py enable 12
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# Windows 控制台默认 GBK，强制 UTF-8 输出避免中文乱码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# WebUI 默认地址；可通过环境变量 POOL_API_BASE 覆盖（便于远程接入）。
DEFAULT_API_BASE = "http://127.0.0.1:5000"

# 相对脚本所在目录定位项目根 .env（绝对路径，最稳）。
# 可用环境变量 POOL_ENV_PATH 显式指定 .env 路径（脚本被复制到其它位置时使用）。
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
_ENV_PATH = os.environ.get("POOL_ENV_PATH", "").strip() or os.path.join(_PROJECT_ROOT, ".env")
# webcodex 会把项目 checkout 到隔离 worktree 中执行脚本，相对路径会失效，
# 这里保留一个硬编码绝对路径兜底（WebUI 项目根 .env）。
_FALLBACK_ENV_PATHS = [
    r"E:\GPT注册\turb-gpt-free-register\.env",
]


def _read_env_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw = line.partition("=")
                if key.strip().upper() == "WEBUI_AUTH_CODE":
                    value = raw.strip().strip('"').strip("'")
                    if value:
                        return value
    except OSError:
        pass
    return ""


def load_auth_code() -> str:
    """从 .env 读取 WEBUI_AUTH_CODE；缺失时回退到环境变量与绝对路径兜底。"""
    value = os.environ.get("WEBUI_AUTH_CODE", "").strip()
    if not value:
        value = _read_env_file(_ENV_PATH)
    if not value:
        for p in _FALLBACK_ENV_PATHS:
            if os.path.abspath(p) != os.path.abspath(_ENV_PATH):
                value = _read_env_file(p)
                if value:
                    break
    if not value:
        print(
            "[错误] 未找到 WEBUI_AUTH_CODE。请检查项目根目录 .env 是否配置，"
            "或设置环境变量 WEBUI_AUTH_CODE。",
            file=sys.stderr,
        )
        sys.exit(2)
    return value


def _api_base() -> str:
    return os.environ.get("POOL_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _request(method: str, path: str, body: dict | None = None) -> dict:
    """发起请求并统一处理 HTTP/网络错误。返回解析后的 JSON（dict）。"""
    url = _api_base() + path
    data = None
    headers = {
        "Accept": "application/json",
        "User-Agent": "pool-cli/1.0",
        "X-Auth-Code": load_auth_code(),
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(payload)
            if isinstance(parsed, dict) and parsed.get("error"):
                detail = str(parsed["error"])
            else:
                detail = payload[:500]
        except Exception:
            detail = f"HTTP {exc.code}"
        if exc.code == 401:
            print(
                f"[错误] 401 未授权：WEBUI_AUTH_CODE 不匹配或已变更，"
                f"请检查项目根目录 .env。({detail})",
                file=sys.stderr,
            )
        else:
            print(f"[错误] HTTP {exc.code}：{detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        print(
            f"[错误] 无法连接 WebUI（{_api_base()}）：{reason}\n"
            "  请确认注册机 WebUI 已启动（python main.py / _webui_keeper.py），"
            "且监听该地址。",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"[错误] 请求失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        print(f"[错误] 响应不是合法 JSON：{raw[:500]}", file=sys.stderr)
        sys.exit(1)


def _mask_token(token: str) -> str:
    """脱敏显示 access_token：前 12 位 + … + 后 8 位。"""
    if not token:
        return "(无)"
    if len(token) <= 24:
        return token[:6] + "…" + token[-6:]
    return token[:12] + "…" + token[-8:]


def _dump_account(acc: dict) -> None:
    """打印 acquire/switch 返回的账号（脱敏，不打印完整 token）。"""
    print(f"  account_id : {acc.get('account_id')}")
    print(f"  email      : {acc.get('email')}")
    print(f"  plan_type  : {acc.get('plan_type')}")
    print(f"  user_id    : {acc.get('user_id')}")
    token = acc.get("access_token") or ""
    if token:
        print(f"  access_token: {_mask_token(token)}  (完整值用 --raw 获取，请勿在对话中明文打印)")
    else:
        print("  access_token: (空)")
    quota = acc.get("quota")
    if isinstance(quota, dict) and quota:
        print(f"  quota      : {json.dumps(quota, ensure_ascii=False)}")


def cmd_summary(_: argparse.Namespace) -> None:
    data = _request("GET", "/api/pool/summary")
    if not data.get("ok"):
        print(f"[错误] {data.get('error') or '未知错误'}", file=sys.stderr)
        sys.exit(1)
    print("== 号池统计 ==")
    print(f"  功能启用   : {'是' if data.get('enabled') else '否'}")
    print(f"  账号总数   : {data.get('total', 0)}")
    print(f"  入池数     : {data.get('in_pool', 0)}")
    print(f"  可用       : {data.get('available', 0)}")
    print(f"  不可用     : {data.get('unavailable', 0)}")
    print(f"  额度未知   : {data.get('unknown', 0)}")
    print(f"  手动禁用   : {data.get('disabled', 0)}")
    print(f"  已耗尽     : {data.get('exhausted', 0)}")
    reasons = data.get("reasons") or {}
    if reasons:
        print("  不可用原因 :")
        for reason, cnt in list(reasons.items())[:10]:
            print(f"      {reason}: {cnt}")
    last = data.get("last_acquired_at")
    if last:
        print(f"  最近分配   : {last}")


def cmd_list(args: argparse.Namespace) -> None:
    qs = []
    if args.status:
        qs.append(f"status={urllib.parse.quote(args.status)}")
    if args.limit:
        qs.append(f"limit={int(args.limit)}")
    suffix = ("?" + "&".join(qs)) if qs else ""
    data = _request("GET", f"/api/pool/accounts{suffix}")
    items = data.get("accounts") if isinstance(data, dict) else None
    if items is None and isinstance(data, dict):
        # 兼容直接返回数组或 {ok, items}
        items = data.get("items") or data.get("accounts")
    if not isinstance(items, list):
        print(f"[错误] 无法解析账号列表：{json.dumps(data, ensure_ascii=False)[:300]}", file=sys.stderr)
        sys.exit(1)
    if not items:
        print("（池内无账号）")
        return
    print(f"== 账号列表（{len(items)} 个）==")
    for it in items:
        usable = it.get("usable")
        status = "可用" if usable else "不可用"
        flags = []
        if not it.get("pool_enabled", True):
            flags.append("禁用")
        if it.get("pool_status") == "exhausted":
            flags.append("耗尽")
        if flags:
            status += "[" + ",".join(flags) + "]"
        reason = it.get("unusable_reason") or it.get("pool_disabled_reason") or it.get("pool_exhausted_reason") or ""
        quota = it.get("quota")
        quota_txt = ""
        if isinstance(quota, dict):
            quota_txt = json.dumps(quota, ensure_ascii=False)
        print(
            f"  #{it.get('id')} {it.get('email')} | {it.get('plan_type')} | {status}"
            f"{(' | ' + reason) if reason else ''}"
            f"{(' | quota=' + quota_txt) if quota_txt else ''}"
        )


def cmd_acquire(args: argparse.Namespace) -> None:
    body: dict = {}
    if args.email:
        body["prefer_email"] = args.email
    if args.tags:
        body["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    data = _request("POST", "/api/pool/acquire", body)
    if not data.get("ok"):
        print(f"[错误] acquire 失败：{data.get('error') or '号池暂无可用账号'}", file=sys.stderr)
        sys.exit(1)
    if args.raw:
        print(json.dumps(data, ensure_ascii=False))
        return
    print("== 分配账号 ==")
    _dump_account(data)


def cmd_switch(args: argparse.Namespace) -> None:
    body: dict = {}
    if args.email:
        body["current_email"] = args.email
    if args.reason:
        body["reason"] = args.reason
    data = _request("POST", "/api/pool/switch", body)
    if not data.get("ok"):
        print(f"[错误] switch 失败：{data.get('error')}", file=sys.stderr)
        sys.exit(1)
    if args.raw:
        print(json.dumps(data, ensure_ascii=False))
        return
    switched_from = data.get("switched_from")
    if switched_from:
        print(
            f"== 已切出旧账号（{switched_from.get('email')}，id={switched_from.get('account_id')}，"
            f"原因：{data.get('switch_reason') or '调用方触发'}）=="
        )
    else:
        print("== 切号（未指定旧账号，自动选择最近分配账号）==")
    print("== 新账号 ==")
    _dump_account(data)


def cmd_probe(_: argparse.Namespace) -> None:
    data = _request("POST", "/api/pool/probe", {})
    if not data.get("ok"):
        print(f"[错误] probe 失败：{data.get('error')}", file=sys.stderr)
        sys.exit(1)
    print("== 巡检结果 ==")
    for k in ("scanned", "queued", "skipped_busy", "skipped_fresh", "skipped_invalid"):
        if k in data:
            print(f"  {k}: {data[k]}")
    results = data.get("results")
    if isinstance(results, list) and results:
        print(f"  入队明细（{len(results)} 条）：")
        for r in results[:10]:
            if isinstance(r, dict):
                print(
                    f"    {r.get('email')}: "
                    f"{'已入队' if r.get('ok') else r.get('error') or r.get('message')}"
                )


def cmd_disable(args: argparse.Namespace) -> None:
    body = {"reason": args.reason} if args.reason else {}
    data = _request("POST", f"/api/pool/accounts/{int(args.id)}/disable", body)
    if not data.get("ok"):
        print(f"[错误] disable 失败：{data.get('error')}", file=sys.stderr)
        sys.exit(1)
    print(
        f"已禁用账号 #{data.get('account_id')}"
        f"{('，原因：' + data.get('reason')) if data.get('reason') else ''}"
    )


def cmd_enable(args: argparse.Namespace) -> None:
    data = _request("POST", f"/api/pool/accounts/{int(args.id)}/enable", {})
    if not data.get("ok"):
        print(f"[错误] enable 失败：{data.get('error')}", file=sys.stderr)
        sys.exit(1)
    print(f"已启用账号 #{data.get('account_id')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pool_cli.py",
        description="号池命令行客户端（操作 ChatGPT 账号池，走 WebUI HTTP API）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_summary = sub.add_parser("summary", help="查看号池统计")
    p_summary.set_defaults(func=cmd_summary)

    p_list = sub.add_parser("list", help="列出池内账号")
    p_list.add_argument("--status", default="", help="过滤：available / unavailable / disabled / unknown")
    p_list.add_argument("--limit", type=int, default=500)
    p_list.set_defaults(func=cmd_list)

    p_acquire = sub.add_parser("acquire", help="分配一个可用账号")
    p_acquire.add_argument("--email", default="", help="优先分配指定邮箱（不可用时自动回退）")
    p_acquire.add_argument("--tags", default="", help="逗号分隔标签过滤")
    p_acquire.add_argument("--raw", action="store_true", help="输出完整 JSON（含完整 access_token）")
    p_acquire.set_defaults(func=cmd_acquire)

    p_switch = sub.add_parser("switch", help="无感切号：标记旧账号耗尽并分配新账号")
    p_switch.add_argument("--email", default="", help="旧账号邮箱（缺省自动选最近分配账号）")
    p_switch.add_argument("--reason", default="", help="切号原因，如 quota_exhausted / auth_failed")
    p_switch.add_argument("--raw", action="store_true", help="输出完整 JSON（含完整 access_token）")
    p_switch.set_defaults(func=cmd_switch)

    p_probe = sub.add_parser("probe", help="触发一轮额度巡检")
    p_probe.set_defaults(func=cmd_probe)

    p_disable = sub.add_parser("disable", help="禁用（踢出）池内账号")
    p_disable.add_argument("id", type=int)
    p_disable.add_argument("--reason", default="", help="禁用原因")
    p_disable.set_defaults(func=cmd_disable)

    p_enable = sub.add_parser("enable", help="重新启用池内账号")
    p_enable.add_argument("id", type=int)
    p_enable.set_defaults(func=cmd_enable)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
