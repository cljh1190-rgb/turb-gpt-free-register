# -*- coding: utf-8 -*-
"""Parse subscription and dump candidate SS nodes as JSON for xray rotator."""
import base64
import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import unquote

FEED = "https://tk-03-cn-hk-cdn.dy-dlam-2.xyz/getOrderPrice/fa4bba632c40302e3fd7d41870280ab3"
OUT = Path("tools/reg_proxy/nodes.json")

DROP = re.compile(
    r"香港|hong\s*kong|\bhk\b|中国|大陆|\bcn\b|澳门|台湾|taiwan|\btw\b|"
    r"广告|官网|流量|丁阅|订阅|返利|计费|IPV6|南极|内网|192\.168|不显示|TG|频道|讨论",
    re.I,
)
# Prefer EU + US/JP/SG for registration diversity
KEEP = re.compile(
    r"德国|英国|美国|日本|新加坡|法国|荷兰|加拿大|韩国|巴西|土耳其|印度|迪拜|"
    r"germany|uk|usa|japan|singapore|france|netherlands|korea|brazil|turkey|india|dubai",
    re.I,
)


def b64decode(data: str) -> bytes:
    data = data.strip().replace("-", "+").replace("_", "/")
    pad = "=" * ((4 - len(data) % 4) % 4)
    return base64.b64decode(data + pad)


def parse_ss(line: str):
    """Return dict or None. Supports ss://base64 or ss://base64_userinfo@host:port."""
    rem = ""
    if "#" in line:
        line, rem = line.split("#", 1)
        rem = unquote(rem)
    if not line.startswith("ss://"):
        return None
    body = line[5:]
    method = password = host = None
    port = None
    try:
        if "@" in body:
            userinfo, hostport = body.split("@", 1)
            try:
                ui = b64decode(userinfo).decode("utf-8", "replace")
            except Exception:
                ui = unquote(userinfo)
            if ":" not in ui:
                return None
            method, password = ui.split(":", 1)
            # hostport may include path/query
            hostport = hostport.split("?")[0].split("/")[0]
            if ":" not in hostport:
                return None
            host, port_s = hostport.rsplit(":", 1)
            port = int(port_s)
        else:
            # whole thing base64: method:pass@host:port
            decoded = b64decode(body).decode("utf-8", "replace")
            if "@" not in decoded or ":" not in decoded:
                return None
            ui, hostport = decoded.split("@", 1)
            method, password = ui.split(":", 1)
            host, port_s = hostport.rsplit(":", 1)
            port = int(port_s)
    except Exception:
        return None
    if not all([method, password, host, port]):
        return None
    return {
        "name": rem or f"{host}:{port}",
        "server": host,
        "port": port,
        "method": method,
        "password": password,
        "raw_remark": rem,
    }


def main():
    raw = urllib.request.urlopen(
        urllib.request.Request(FEED, headers={"User-Agent": "Mozilla/5.0"}), timeout=60
    ).read()
    text = raw.decode("utf-8", "replace").strip()
    dec = b64decode(text).decode("utf-8", "replace")
    nodes = []
    seen = set()
    for ln in dec.replace("\r", "").split("\n"):
        ln = ln.strip()
        if not ln.startswith("ss://"):
            continue
        node = parse_ss(ln)
        if not node:
            continue
        rem = node["raw_remark"]
        if DROP.search(rem):
            continue
        if not KEEP.search(rem):
            continue
        key = (node["server"], node["port"], node["password"])
        if key in seen:
            continue
        seen.add(key)
        nodes.append(node)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(nodes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {len(nodes)} nodes -> {OUT}")
    for n in nodes:
        print(f"  - {n['name']} @ {n['server']}:{n['port']} ({n['method']})")


if __name__ == "__main__":
    main()
