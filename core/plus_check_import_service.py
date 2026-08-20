# -*- coding: utf-8 -*-
"""Shared PLUS-query import pipeline for text and archive uploads."""
from __future__ import annotations

from core import db, plan_check_service


def import_and_enqueue_plus_accounts(
    parsed: dict,
    *,
    proxy: str = "",
    timezone_offset_min: str = "-",
) -> tuple[dict, int]:
    records = parsed.get("records") or []
    if not records:
        parse_errors = parsed.get("errors") or []
        first_reason = str((parse_errors[0] or {}).get("reason") or "").strip() if parse_errors else ""
        return {"ok": False, "error": first_reason or "未识别到可用账号或 access_token", **parsed}, 400

    imported: list[dict] = []
    updated: list[dict] = []
    started: list[dict] = []
    busy: list[dict] = []
    failed: list[dict] = []
    seen_emails: set[str] = set()
    unique_records: list[dict] = []
    for record in records:
        email = str(record.get("email") or "").strip()
        email_key = email.lower()
        if email_key in seen_emails:
            failed.append({
                "email": email,
                "line_no": record.get("line_no"),
                "reason": "同一次导入中邮箱重复，已保留第一条",
            })
            continue
        seen_emails.add(email_key)
        unique_records.append(record)

    try:
        saved_records = db.upsert_plus_check_accounts(unique_records)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"批量保存账号失败: {type(exc).__name__}: {str(exc)[:180]}",
            "parse_errors": parsed.get("errors") or [],
        }, 500

    for record in saved_records:
        email = str(record.get("email") or "").strip()
        token = str(record.get("access_token") or "").strip()
        try:
            acc_id = int(record.get("id"))
            item = {
                "id": acc_id,
                "email": email,
                "line_no": record.get("line_no"),
                "detected_format": record.get("detected_format"),
                "synthetic_email": bool(record.get("synthetic_email")),
            }
            (updated if record.get("existing") else imported).append(item)
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=acc_id,
                email=email,
                access_token=token,
                trigger="plus_query_import",
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
            )
            queue_item = {**item, **queued}
            if queued.get("accepted"):
                started.append(queue_item)
            elif queued.get("busy"):
                busy.append(queue_item)
            else:
                failed.append({**queue_item, "reason": queued.get("error") or "查询入队失败"})
        except Exception as exc:
            failed.append({
                "email": email,
                "line_no": record.get("line_no"),
                "reason": f"{type(exc).__name__}: {str(exc)[:180]}",
            })

    return {
        "ok": True,
        "parsed_count": len(records),
        "duplicates": parsed.get("duplicates", 0),
        "parse_errors": parsed.get("errors") or [],
        "imported": imported,
        "imported_count": len(imported),
        "updated": updated,
        "updated_count": len(updated),
        "started": started,
        "started_count": len(started),
        "busy": busy,
        "busy_count": len(busy),
        "failed": failed,
        "failed_count": len(failed),
    }, 202
