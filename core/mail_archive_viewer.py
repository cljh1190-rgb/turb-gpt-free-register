# -*- coding: utf-8 -*-
"""Read-only viewer for mailbox archive/show URLs."""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import unquote, urlsplit

import requests
from bs4 import BeautifulSoup


_PLUS_RE = re.compile(r"\bplus\b", re.IGNORECASE)


def parse_mail_viewer_input(text: str, *, max_urls: int = 20) -> dict:
    records: list[dict] = []
    errors: list[dict] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.search(r"https?://\S+", line, re.IGNORECASE)
        if not match:
            errors.append({"line_no": line_no, "reason": "未找到 http/https 邮箱链接"})
            continue
        url = match.group(0).strip().rstrip(",;")
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            errors.append({"line_no": line_no, "reason": "邮箱链接格式无效"})
            continue
        if url in seen:
            continue
        if len(records) >= max_urls:
            errors.append({"line_no": line_no, "reason": f"单次最多查看 {max_urls} 个邮箱链接"})
            break
        seen.add(url)
        records.append({"line_no": line_no, "url": url})
    return {"records": records, "errors": errors, "count": len(records)}


def mask_mail_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
        tail = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
        return f"{parsed.scheme}://{parsed.hostname or '已隐藏'}/…/{tail}" if tail else f"{parsed.scheme}://{parsed.hostname or '已隐藏'}/…"
    except Exception:
        return "已隐藏"


def _mailbox_from_url(url: str) -> str:
    try:
        tail = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
        return tail if "@" in tail else ""
    except Exception:
        return ""


def _clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    soup = BeautifulSoup(str(value), "html.parser")
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def _normalize_message(item: dict, index: int) -> dict:
    subject = _clean_text(item.get("subject") or item.get("Subject") or item.get("title"))
    sender = _clean_text(item.get("from") or item.get("From") or item.get("sender") or item.get("fromEmail"))
    received_at = _clean_text(
        item.get("date") or item.get("receivedDateTime") or item.get("created_at") or item.get("time")
    )
    body = _clean_text(
        item.get("body") or item.get("content") or item.get("html") or item.get("text") or item.get("bodyPreview")
    )[:30_000]
    has_plus = bool(_PLUS_RE.search("\n".join((subject, sender, body))))
    return {
        "index": index,
        "subject": subject or "（无主题）",
        "from": sender or "-",
        "received_at": received_at or "-",
        "body": body,
        "has_plus": has_plus,
    }


def _messages_from_json(payload) -> list[dict]:
    rows = payload if isinstance(payload, list) else None
    if isinstance(payload, dict):
        for key in ("emails", "messages", "mails", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
            if isinstance(value, dict):
                nested = _messages_from_json(value)
                if nested:
                    return nested
    if not isinstance(rows, list):
        return []
    return [_normalize_message(item, index + 1) for index, item in enumerate(rows) if isinstance(item, dict)]


def _messages_from_html(text: str) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(text, "html.parser")
    heading = soup.find("h1")
    mailbox = _clean_text(heading.get_text(" ", strip=True)) if heading else ""
    messages: list[dict] = []
    for index, card in enumerate(soup.select(".card"), 1):
        subject_node = card.select_one(".su")
        sender_node = card.select_one(".fr")
        date_node = card.select_one(".dt")
        body_node = card.select_one(".bd")
        item = {
            "subject": subject_node.get_text("\n", strip=True) if subject_node else "",
            "from": sender_node.get_text("\n", strip=True) if sender_node else "",
            "date": date_node.get_text("\n", strip=True) if date_node else "",
            "body": body_node.get_text("\n", strip=True) if body_node else "",
        }
        messages.append(_normalize_message(item, index))
    return mailbox, messages


def fetch_mail_archive(url: str, *, timeout: int = 20) -> dict:
    url = str(url or "").strip()
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    masked_url = mask_mail_url(url)
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return {"ok": False, "url": masked_url, "error": "邮箱链接格式无效", "fetched_at": fetched_at, "messages": []}
    try:
        response = requests.get(
            url,
            headers={"Accept": "application/json,text/html,text/plain,*/*", "User-Agent": "Mozilla/5.0 (compatible; gpt-register-mail-viewer/1.0)"},
            timeout=max(3, min(int(timeout), 30)),
            verify=False,
        )
    except requests.Timeout:
        return {"ok": False, "url": masked_url, "error": "请求超时", "fetched_at": fetched_at, "messages": []}
    except requests.RequestException as exc:
        return {"ok": False, "url": masked_url, "error": f"请求失败：{type(exc).__name__}", "fetched_at": fetched_at, "messages": []}
    if response.status_code != 200:
        return {"ok": False, "url": masked_url, "error": f"HTTP {response.status_code}", "http_status": response.status_code, "fetched_at": fetched_at, "messages": []}

    text = (response.text or "")[:5_000_000]
    messages: list[dict] = []
    mailbox = _mailbox_from_url(url)
    try:
        payload = response.json()
        messages = _messages_from_json(payload)
        if isinstance(payload, dict):
            mailbox = str(payload.get("email") or payload.get("mailbox") or mailbox)
    except Exception:
        html_mailbox, messages = _messages_from_html(text)
        if html_mailbox and "@" in html_mailbox:
            mailbox = html_mailbox

    plus_count = sum(1 for item in messages if item.get("has_plus"))
    return {
        "ok": True,
        "mailbox": mailbox or "未知邮箱",
        "url": masked_url,
        "http_status": response.status_code,
        "fetched_at": fetched_at,
        "messages": messages[:200],
        "message_count": min(len(messages), 200),
        "plus_count": plus_count,
        "has_plus": plus_count > 0,
        "truncated": len(messages) > 200,
        "error": "" if messages else "页面中未识别到邮件",
    }
