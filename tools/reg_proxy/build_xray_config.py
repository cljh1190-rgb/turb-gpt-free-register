# -*- coding: utf-8 -*-
"""Build dedicated xray config: one local SOCKS port per node. Does NOT use 10808."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NODES = json.loads((ROOT / "nodes.json").read_text(encoding="utf-8"))
# Prefer EU first, then US/JP/SG — cap to keep config light
PRIORITY = ["德国", "英国", "美国", "日本", "新加坡", "法国", "荷兰", "韩国", "加拿大"]
BASE_PORT = 17891  # dedicated range; never 10808 / 7897


def sort_key(n: dict) -> tuple:
    name = n.get("name") or ""
    for i, k in enumerate(PRIORITY):
        if k in name:
            return (i, name)
    return (len(PRIORITY), name)


def main() -> None:
    nodes = sorted(NODES, key=sort_key)
    # keep up to 20
    nodes = nodes[:20]
    inbounds = []
    outbounds = [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block", "protocol": "blackhole"},
    ]
    rules = []
    pool = []
    for i, n in enumerate(nodes):
        tag = f"ss-{i}"
        port = BASE_PORT + i
        inbounds.append({
            "tag": f"in-{i}",
            "listen": "127.0.0.1",
            "port": port,
            "protocol": "socks",
            "settings": {"udp": True, "auth": "noauth"},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        })
        outbounds.append({
            "tag": tag,
            "protocol": "shadowsocks",
            "settings": {
                "servers": [{
                    "address": n["server"],
                    "port": int(n["port"]),
                    "method": n["method"],
                    "password": n["password"],
                }]
            },
        })
        rules.append({
            "type": "field",
            "inboundTag": [f"in-{i}"],
            "outboundTag": tag,
        })
        pool.append({
            "proxy": f"socks5h://127.0.0.1:{port}",
            "name": n["name"],
            "server": n["server"],
            "port": n["port"],
        })

    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {
            "domainStrategy": "AsIs",
            "rules": rules,
        },
    }
    (ROOT / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "pool.json").write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
    # plain list for PROXY_POOL
    lines = [p["proxy"] for p in pool]
    (ROOT / "pool.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"nodes={len(nodes)} ports={BASE_PORT}-{BASE_PORT+len(nodes)-1}")
    for p in pool:
        print(f"  {p['proxy']}  <=  {p['name']}")


if __name__ == "__main__":
    main()
