# -*- coding: utf-8 -*-
"""Safely read PLUS-query account files from ZIP archives without extracting to disk."""
from __future__ import annotations

import io
import json
import zipfile


class AccountArchiveError(ValueError):
    pass


def normalize_plus_archive(data: bytes, *, max_entries: int = 1000, max_uncompressed: int = 100_000_000) -> dict:
    if not data:
        raise AccountArchiveError("ZIP 文件为空")
    if len(data) > 50_000_000:
        raise AccountArchiveError("ZIP 文件超过 50MB 限制")

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise AccountArchiveError("文件不是有效 ZIP 压缩包") from exc

    with archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        if len(entries) > max_entries:
            raise AccountArchiveError(f"ZIP 内文件超过 {max_entries} 个限制")
        total_size = sum(max(0, int(item.file_size)) for item in entries)
        if total_size > max_uncompressed:
            raise AccountArchiveError("ZIP 解压后内容超过 100MB 限制")

        json_entries = [item for item in entries if item.filename.lower().endswith((".json", ".jsonl"))]
        text_entries = [item for item in entries if item.filename.lower().endswith((".txt", ".csv", ".log"))]
        selected = json_entries or text_entries
        if not selected:
            raise AccountArchiveError("ZIP 中未找到 JSON、JSONL、TXT、CSV 或 LOG 账号文件")

        chunks: list[str] = []
        errors: list[dict] = []
        processed = 0
        for item in selected:
            if item.file_size > 10_000_000:
                errors.append({"file": item.filename, "reason": "单个文件超过 10MB，已跳过"})
                continue
            try:
                raw = archive.read(item)
                text = raw.decode("utf-8-sig").strip()
            except Exception:
                errors.append({"file": item.filename, "reason": "无法按 UTF-8 读取，已跳过"})
                continue
            if not text:
                continue
            if item.filename.lower().endswith(".json"):
                try:
                    text = json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
                except Exception:
                    errors.append({"file": item.filename, "reason": "JSON 格式无效，已跳过"})
                    continue
            chunks.append(text)
            processed += 1

        if not chunks:
            raise AccountArchiveError("ZIP 中没有可读取的账号内容")
        return {
            "text": "\n".join(chunks),
            "processed_files": processed,
            "selected_type": "json" if json_entries else "text",
            "ignored_text_files": len(text_entries) if json_entries else 0,
            "archive_errors": errors,
            "entry_count": len(entries),
        }
