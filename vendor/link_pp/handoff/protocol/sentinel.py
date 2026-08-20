from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


_BRIDGE_VERSION = "20260219f9f6"
_BRIDGE_JS = Path(__file__).with_name("sentinel_bridge.js")


def mint_sentinel_sync(
    *,
    flow: str,
    device_id: str,
    user_agent: str,
    proxy: str = "",
    cores: int = 16,
    page_url: str = "https://chatgpt.com/",
    language: str = "en-US",
    timezone: str = "America/Chicago",
    cookie_header: str = "",
    timeout_s: float = 120.0,
) -> tuple[str, str]:
    """Generate OpenAI Sentinel headers with the local Node/V8 SDK bridge."""
    payload = json.dumps(
        {
            "ua": user_agent,
            "cores": cores,
            "deviceId": device_id,
            "flow": flow,
            "proxy": proxy,
            "version": _BRIDGE_VERSION,
            "pageUrl": page_url,
            "language": language,
            "timezone": timezone,
            "cookieHeader": cookie_header,
            "sentinelOrigin": "https://chatgpt.com",
        },
        separators=(",", ":"),
    ).encode()
    try:
        process = subprocess.run(
            [os.environ.get("SENTINEL_NODE", "node"), str(_BRIDGE_JS)],
            input=payload,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Sentinel 需要 Node.js") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Sentinel 生成超时（>{timeout_s:.0f}s）") from exc

    output = (process.stdout or b"").decode("utf-8", "replace").strip()
    if not output:
        error = (process.stderr or b"").decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Sentinel bridge 无输出: {error}")
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Sentinel bridge 输出无效: {output[:200]}") from exc
    if result.get("error"):
        raise RuntimeError(f"Sentinel bridge 失败: {str(result['error'])[:300]}")
    main = str(result.get("main") or "")
    if not main:
        raise RuntimeError("Sentinel bridge 未返回 main token")
    try:
        token_payload = json.loads(main)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Sentinel bridge main token 不是有效 JSON") from exc
    if (
        token_payload.get("id") != device_id
        or token_payload.get("flow") != flow
        or not token_payload.get("c")
    ):
        raise RuntimeError("Sentinel bridge token 与当前 device/flow 不匹配")
    return main, str(result.get("so") or "")
