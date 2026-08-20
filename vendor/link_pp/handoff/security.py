from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
_PROXY_CREDENTIAL_RE = re.compile(r"(?i)(\b(?:socks5h?|https?)://)[^\s/@]+(?::[^\s/@]*)?@")
_BA_RE = re.compile(r"(?i)(ba_token=)(BA-[A-Z0-9-]+)")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_SENSITIVE_DIAGNOSTIC_FIELDS = (
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "credential",
    "email",
    "name",
    "password",
    "phone",
    "secret",
    "tax_id",
    "token",
)
_REDACT_DIAGNOSTIC_CONTENT_FIELDS = {
    "address",
    "billing_address",
    "billing_details",
    "metadata",
    "shipping",
}


def _is_sensitive_diagnostic_field(field_name: str) -> bool:
    normalized = str(field_name or "").lower()
    return any(marker in normalized for marker in _SENSITIVE_DIAGNOSTIC_FIELDS)


def _sanitize_diagnostic_string(value: str) -> str:
    raw = str(value)
    if raw.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(raw)
            query = urlencode(
                [
                    (
                        key,
                        "[REDACTED]"
                        if _is_sensitive_diagnostic_field(key)
                        else _EMAIL_RE.sub("[EMAIL]", item),
                    )
                    for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                ]
            )
            raw = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))
        except ValueError:
            pass
    safe = sanitize_message(raw, max_length=None)
    return _EMAIL_RE.sub("[EMAIL]", safe)


@dataclass(frozen=True, slots=True)
class TokenProfile:
    email: str
    name: str
    account_id: str


def normalize_access_token(raw: str) -> str:
    token = str(raw or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or len(token.split(".")) != 3:
        raise ValueError("AT 格式无效")
    return token


def token_profile(access_token: str) -> TokenProfile:
    token = normalize_access_token(access_token)
    payload_part = token.split(".", 2)[1]
    padded = payload_part + "=" * (-len(payload_part) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("AT payload 无法解析") from exc
    if not isinstance(payload, dict):
        raise ValueError("AT payload 无效")

    profile = payload.get("https://api.openai.com/profile") or {}
    auth = payload.get("https://api.openai.com/auth") or {}
    email = str(profile.get("email") or payload.get("email") or "").strip()
    name = str(profile.get("name") or payload.get("name") or "").strip()
    if not email or "@" not in email:
        raise ValueError("AT 中没有可用邮箱")
    if not name:
        name = email.split("@", 1)[0][:64]
    account_id = str(auth.get("chatgpt_account_id") or "").strip()
    return TokenProfile(email=email, name=name[:128], account_id=account_id)


def mask_identifier(value: str, *, left: int = 10, right: int = 5) -> str:
    text = str(value or "")
    if len(text) <= left + right + 3:
        return text
    return f"{text[:left]}...{text[-right:]}"


def sanitize_message(
    message: object,
    *,
    access_token: str = "",
    secrets: tuple[str, ...] = (),
    max_length: int | None = 500,
) -> str:
    text = str(message or "").replace("\r", " ").replace("\n", " ")
    if access_token:
        text = text.replace(access_token, "[AT]")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[SECRET]")
    text = _BEARER_RE.sub("Bearer [AT]", text)
    text = _JWT_RE.sub("[AT]", text)
    text = _PROXY_CREDENTIAL_RE.sub(r"\1***@", text)
    text = _BA_RE.sub(r"\1BA-***", text)
    return text if max_length is None else text[:max_length]


def sanitize_diagnostic_payload(
    payload: object,
    *,
    field_name: str = "",
    redact_values: bool = False,
) -> object:
    """Preserve response structure while removing credentials and personal data."""
    normalized_field = field_name.lower()
    if normalized_field in _REDACT_DIAGNOSTIC_CONTENT_FIELDS:
        redact_values = True
    if redact_values:
        if isinstance(payload, dict):
            return {
                str(key): sanitize_diagnostic_payload(value, redact_values=True)
                for key, value in payload.items()
            }
        if isinstance(payload, (list, tuple)):
            return [sanitize_diagnostic_payload(item, redact_values=True) for item in payload]
        return "[REDACTED]"
    if _is_sensitive_diagnostic_field(normalized_field):
        return "[REDACTED]"
    if isinstance(payload, dict):
        return {
            str(key): sanitize_diagnostic_payload(value, field_name=str(key))
            for key, value in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [sanitize_diagnostic_payload(item, field_name=field_name) for item in payload]
    if isinstance(payload, str):
        return _sanitize_diagnostic_string(payload)
    return payload
