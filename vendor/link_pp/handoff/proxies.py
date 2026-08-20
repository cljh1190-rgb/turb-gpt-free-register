from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit, urlunsplit


SUPPORTED_PROXY_SCHEMES = ("socks5", "socks5h", "http", "https")
_SCHEME_ALIASES = {
    # curl distinguishes local-DNS socks5 from proxy-DNS socks5h. The proxy
    # pools used here reach ChatGPT reliably only when the proxy resolves it.
    "socket5": "socks5h",
    "socks": "socks5h",
    "socks5": "socks5h",
}


class ProxyInputError(ValueError):
    def __init__(self, line_number: int, reason: str):
        self.line_number = line_number
        self.reason = reason
        super().__init__(f"第 {line_number} 行代理格式错误：{reason}")


@dataclass(frozen=True, slots=True)
class ProxyEndpoint:
    url: str
    scheme: str
    host: str
    port: int
    has_credentials: bool

    @property
    def safe_label(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


class ProxyPool:
    def __init__(self, endpoints: list[ProxyEndpoint]):
        if not endpoints:
            raise ValueError("代理池不能为空")
        self._endpoints = tuple(endpoints)

    def pick(self, sequence: int) -> ProxyEndpoint:
        if sequence < 1:
            raise ValueError("代理序号必须从 1 开始")
        return self._endpoints[(sequence - 1) % len(self._endpoints)]

    def __len__(self) -> int:
        return len(self._endpoints)


def _normalize_scheme(raw: str) -> str:
    scheme = _SCHEME_ALIASES.get(raw.lower(), raw.lower())
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError("仅支持 SOCKS5、HTTP 或 HTTPS")
    return scheme


def _normalize_url(raw: str, default_scheme: str) -> ProxyEndpoint:
    value = raw.strip()
    if not value:
        raise ValueError("内容为空")

    if "://" not in value:
        if "@" in value:
            value = f"{default_scheme}://{value}"
        else:
            parts = value.split(":")
            if len(parts) == 2:
                host, port = parts
                value = f"{default_scheme}://{host}:{port}"
            elif len(parts) >= 4:
                host, port, username = parts[:3]
                password = ":".join(parts[3:])
                if not username or not password:
                    raise ValueError("用户名或密码为空")
                value = (
                    f"{default_scheme}://{quote(username, safe='')}:{quote(password, safe='')}"
                    f"@{host}:{port}"
                )
            else:
                raise ValueError("应为 URL、host:port 或 host:port:user:password")

    parsed = urlsplit(value)
    scheme = _normalize_scheme(parsed.scheme)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("端口无效") from exc
    if not parsed.hostname:
        raise ValueError("缺少主机")
    if port is None or not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或片段")
    if (parsed.username is None) != (parsed.password is None):
        raise ValueError("用户名和密码必须同时填写")

    host = parsed.hostname
    host_for_url = f"[{host}]" if ":" in host else host
    has_credentials = parsed.username is not None
    if has_credentials:
        username = quote(unquote(parsed.username or ""), safe="")
        password = quote(unquote(parsed.password or ""), safe="")
        if not username or not password:
            raise ValueError("用户名或密码为空")
        netloc = f"{username}:{password}@{host_for_url}:{port}"
    else:
        netloc = f"{host_for_url}:{port}"
    normalized = urlunsplit((scheme, netloc, "", "", ""))
    return ProxyEndpoint(normalized, scheme, host, port, has_credentials)


def parse_proxy_lines(raw: str, *, default_scheme: str = "socks5") -> list[ProxyEndpoint]:
    scheme = _normalize_scheme(default_scheme)
    endpoints: list[ProxyEndpoint] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(str(raw or "").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            endpoint = _normalize_url(line, scheme)
        except ValueError as exc:
            raise ProxyInputError(line_number, str(exc)) from exc
        if endpoint.url in seen:
            continue
        seen.add(endpoint.url)
        endpoints.append(endpoint)
    if not endpoints:
        raise ProxyInputError(1, "至少填写一个代理")
    return endpoints
