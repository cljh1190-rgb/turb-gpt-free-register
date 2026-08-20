# -*- coding: utf-8 -*-
"""自动识别用于套餐查询的账号/Token 导入文本。"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any


_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_JWT_RE = re.compile(r"(eyJ[A-Za-z0-9_\-]{5,}\.eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{8,})")
_TOKEN_KEYS = {
    "access_token", "accesstoken", "access-token", "token", "bearer",
    "chatgpt_token", "session_token",
}
_EMAIL_KEYS = {"email", "mail", "username", "user_email", "useremail"}


def normalize_access_token(value: Any) -> str:
    token = str(value or "").strip().strip('"').strip("'")
    lower = token.lower()
    if lower.startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token.strip().strip('"').strip("'")


def _known_non_chatgpt_token_reason(value: Any) -> str:
    token = normalize_access_token(value)
    if token.startswith("M.") and "MsaArtifacts" in token:
        return (
            "检测到 Microsoft MSA Refresh Token（M...MsaArtifacts），它不是 ChatGPT access_token，"
            "不能直接查询 PLUS。若内容来自 Sub2，请导入该账号 credentials.access_token（通常为 eyJ... 三段 JWT）"
        )
    return ""


def _jwt_payload(token: str) -> dict:
    try:
        parts = normalize_access_token(token).split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _token_identity(token: str) -> dict:
    payload = _jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    profile = payload.get("https://api.openai.com/profile") or {}
    return {
        "email": str(profile.get("email") or payload.get("email") or "").strip(),
        "user_name": str(profile.get("name") or payload.get("name") or "").strip(),
        "user_id": auth.get("chatgpt_user_id") or auth.get("user_id") or payload.get("sub"),
        "account_id": auth.get("chatgpt_account_id"),
        "plan_type": auth.get("chatgpt_plan_type"),
    }


def _looks_like_token(value: Any, *, explicit_key: bool = False) -> bool:
    token = normalize_access_token(value)
    if not token or any(ch.isspace() for ch in token):
        return False
    if _known_non_chatgpt_token_reason(token):
        return False
    if _JWT_RE.fullmatch(token):
        return True
    # 以 JWT 头开头却不是标准三段 JWT，通常是多 Token 拼接、截断或错误字段，
    # 不能再因为长度足够而当成 opaque token 接受。
    if token.startswith("eyJ"):
        return False
    # JSON 中明确叫 access_token/token 的值，以及纯行中的长凭证，也允许导入；
    # 最终有效性由套餐查询接口判断。
    return len(token) >= (24 if explicit_key else 80)


def _fallback_email(token: str) -> str:
    return f"token-{hashlib.sha256(token.encode('utf-8')).hexdigest()[:12]}@plus-check.local"


def _extract_emails(text: str) -> list[str]:
    """先按账号常见分隔符切段，避免 ---- 被邮箱域名正则吞进去。"""
    found: list[str] = []
    for part in re.split(r"(?:----|====|\t|\||,|;|\s+)", str(text or "")):
        match = _EMAIL_RE.search(part)
        if match and match.group(0).lower() not in {x.lower() for x in found}:
            found.append(match.group(0))
    return found


def _make_record(token: str, email: str = "", *, detected_format: str, line_no: int | None = None) -> dict:
    token = normalize_access_token(token)
    identity = _token_identity(token)
    resolved_email = str(email or identity.get("email") or "").strip()
    if not _EMAIL_RE.fullmatch(resolved_email):
        resolved_email = identity.get("email") if _EMAIL_RE.fullmatch(str(identity.get("email") or "")) else ""
    synthetic_email = not bool(resolved_email)
    if synthetic_email:
        resolved_email = _fallback_email(token)
    return {
        "email": resolved_email,
        "access_token": token,
        "user_name": identity.get("user_name") or ("Imported Plus Check" if synthetic_email else ""),
        "user_id": identity.get("user_id"),
        "account_id": identity.get("account_id"),
        "plan_type": identity.get("plan_type"),
        "synthetic_email": synthetic_email,
        "detected_format": detected_format,
        "line_no": line_no,
    }


def _find_email(value: Any) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower().replace("-", "_") in _EMAIL_KEYS:
                match = _EMAIL_RE.search(str(item or ""))
                if match:
                    return match.group(0)
        for item in value.values():
            found = _find_email(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_email(item)
            if found:
                return found
    return ""


def _records_from_json(value: Any, *, inherited_email: str = "", line_no: int | None = None) -> list[dict]:
    records: list[dict] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, (dict, list)) and _looks_like_token(item, explicit_key=True):
                records.append(_make_record(item, inherited_email, detected_format="json", line_no=line_no))
            else:
                records.extend(_records_from_json(item, inherited_email=inherited_email, line_no=line_no))
        return records
    if not isinstance(value, dict):
        return records

    local_email = _find_email(value) or inherited_email
    raw_plan_type = value.get("plan_type") or value.get("chatgpt_plan_type")
    for key, item in value.items():
        normalized_key = str(key).lower().replace("-", "_")
        if normalized_key in _TOKEN_KEYS and not isinstance(item, (dict, list)) and _looks_like_token(item, explicit_key=True):
            record = _make_record(item, local_email, detected_format="json", line_no=line_no)
            if not record.get("plan_type") and raw_plan_type:
                record["plan_type"] = str(raw_plan_type)
            # sub2api/CPA OAuth 导出中的同级字段，保留供后续转换 CPA auth-file。
            record["oauth_id_token"] = str(value.get("id_token") or "").strip()
            record["oauth_refresh_token"] = str(value.get("refresh_token") or "").strip()
            record["oauth_client_id"] = str(value.get("client_id") or "").strip()
            record["oauth_organization_id"] = str(value.get("organization_id") or "").strip()
            record["oauth_expires_at"] = value.get("expires_at")
            if not record.get("account_id") and value.get("chatgpt_account_id"):
                record["account_id"] = value.get("chatgpt_account_id")
            if not record.get("user_id") and value.get("chatgpt_user_id"):
                record["user_id"] = value.get("chatgpt_user_id")
            records.append(record)

    # 即使当前对象已经带 token，仍递归处理 accounts/tokens 等容器中的其他账号。
    for item in value.values():
        if isinstance(item, (dict, list)):
            records.extend(_records_from_json(item, inherited_email=local_email, line_no=line_no))
    return records


def _records_from_line(line: str, line_no: int) -> tuple[list[dict], str | None]:
    stripped = line.strip().lstrip("\ufeff")
    if not stripped or stripped.startswith("#") or stripped.startswith("//"):
        return [], None

    if stripped.startswith(("{", "[")):
        try:
            records = _records_from_json(json.loads(stripped), line_no=line_no)
            if records:
                return records, None
        except Exception:
            pass

    emails = _extract_emails(stripped)
    jwt_tokens = []
    for match in _JWT_RE.findall(stripped):
        token = match
        for separator in ("----", "====", "\t", "|", ",", ";"):
            token = token.split(separator, 1)[0]
        jwt_tokens.append(normalize_access_token(token))
    if jwt_tokens:
        return [
            _make_record(token, emails[index] if index < len(emails) else (emails[0] if len(jwt_tokens) == 1 and emails else ""),
                         detected_format="jwt" if stripped == token else "delimited", line_no=line_no)
            for index, token in enumerate(jwt_tokens)
        ], None

    # 非 JWT 的长 Token：兼容 email----token、email|token、CSV、TAB、email:token
    parts: list[str] = []
    for separator in ("----", "====", "\t", "|", ",", ";"):
        if separator in stripped:
            parts = [part.strip() for part in stripped.split(separator)]
            break
    if not parts and emails and ":" in stripped:
        parts = [part.strip() for part in stripped.split(":")]
    candidates = [part for part in parts if _looks_like_token(part)]
    if candidates:
        return [_make_record(token, emails[0] if emails else "", detected_format="delimited", line_no=line_no) for token in candidates], None

    for part in parts:
        reason = _known_non_chatgpt_token_reason(part)
        if reason:
            return [], reason

    normalized = normalize_access_token(stripped)
    reason = _known_non_chatgpt_token_reason(normalized)
    if reason:
        return [], reason
    if _looks_like_token(normalized):
        return [_make_record(normalized, detected_format="token", line_no=line_no)], None

    return [], "未找到可识别的 ChatGPT access token"


def parse_account_import_text(text: str, *, max_records: int = 500) -> dict:
    """解析混合格式文本，返回 records/errors；相同 Token 自动去重。"""
    raw = str(text or "").strip()
    if not raw:
        return {"records": [], "errors": [{"line_no": None, "reason": "导入内容为空"}], "duplicates": 0}

    records: list[dict] = []
    errors: list[dict] = []
    try:
        whole_json = json.loads(raw)
    except Exception:
        whole_json = None
    if isinstance(whole_json, (dict, list)):
        records = _records_from_json(whole_json)
        if not records:
            errors.append({"line_no": None, "reason": "JSON 中未找到 access_token/token 字段"})
    else:
        for line_no, line in enumerate(raw.splitlines(), 1):
            line_records, reason = _records_from_line(line, line_no)
            records.extend(line_records)
            if reason:
                errors.append({"line_no": line_no, "reason": reason, "preview": line.strip()[:120]})

    unique: list[dict] = []
    seen: set[str] = set()
    duplicates = 0
    for record in records:
        token = record.get("access_token") or ""
        if token in seen:
            duplicates += 1
            continue
        seen.add(token)
        if len(unique) >= max_records:
            errors.append({"line_no": record.get("line_no"), "reason": f"单次最多导入 {max_records} 个账号"})
            continue
        unique.append(record)
    return {"records": unique, "errors": errors, "duplicates": duplicates}
