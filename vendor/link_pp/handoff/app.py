from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from .countries import checkout_country_for_proxy, get_country, list_countries
from .engine import (
    DEFAULT_CHECKOUT_ATTEMPTS,
    DEFAULT_PROVIDER_ATTEMPTS,
    HandoffEngine,
    RunSpec,
    positive_attempts,
)
from .gateway import LiveProtocolGateway
from .jobs import JobManager
from .proxies import ProxyInputError, ProxyPool, SUPPORTED_PROXY_SCHEMES, parse_proxy_lines
from .security import normalize_access_token, token_profile


DEFAULT_MAX_BATCH_ITEMS = 200
DEFAULT_BATCH_CONCURRENCY = 8


def _proxy_text(value: object) -> str:
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value or "")


def _parse_shared_run_config(payload: dict) -> dict:
    proxy_country = get_country(
        payload.get("country") or payload.get("proxy_country", "")
    )
    requested_billing_country = payload.get("billing_country") or payload.get(
        "checkout_country"
    )
    checkout_country = (
        get_country(requested_billing_country)
        if requested_billing_country
        else checkout_country_for_proxy(proxy_country)
    )
    proxy_text = payload.get("proxies")
    if proxy_text is None:
        proxy_text = payload.get("checkout_proxies")
    proxy_scheme = payload.get("proxy_scheme") or payload.get("checkout_proxy_scheme")
    stripe_checkout = _boolean_option(
        payload.get("stripe_checkout"),
        name="stripe_checkout",
    )
    stripe_engine = str(payload.get("stripe_engine") or "python").strip().lower()
    if stripe_engine not in {"python", "go"}:
        raise ValueError("stripe_engine 只支持 python 或 go")
    if stripe_engine == "go" and not stripe_checkout:
        raise ValueError("Go 引擎当前只支持 Stripe 链提炼")
    stripe_promo_strategy = str(
        payload.get("stripe_promo_strategy") or "post_update"
    ).strip().lower()
    if stripe_promo_strategy not in {"upfront", "post_update", "mixed"}:
        raise ValueError("stripe_promo_strategy 只支持 upfront、post_update 或 mixed")
    return {
        "proxy_country": proxy_country,
        "checkout_country": checkout_country,
        "proxies": ProxyPool(
            parse_proxy_lines(
                _proxy_text(proxy_text),
                default_scheme=str(proxy_scheme or "socks5"),
            )
        ),
        "checkout_attempts": positive_attempts(
            payload.get("checkout_attempts"),
            default=DEFAULT_CHECKOUT_ATTEMPTS,
        ),
        "provider_attempts": positive_attempts(
            payload.get("provider_attempts"),
            default=DEFAULT_PROVIDER_ATTEMPTS,
        ),
        "stripe_checkout": stripe_checkout,
        "stripe_engine": stripe_engine,
        "stripe_promo_strategy": stripe_promo_strategy,
    }


def _boolean_option(value: object, *, name: str) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{name} 必须是布尔值")


def _build_run_spec(access_token: object, shared: dict) -> RunSpec:
    normalized_token = normalize_access_token(access_token)
    return RunSpec(
        access_token=normalized_token,
        token_profile=token_profile(normalized_token),
        **shared,
    )


def _batch_token_lines(value: object) -> list[tuple[int, str]]:
    raw_lines = value if isinstance(value, list) else str(value or "").splitlines()
    items: list[tuple[int, str]] = []
    for line_number, raw in enumerate(raw_lines, 1):
        text = str(raw or "").strip()
        if not text or text.startswith("#"):
            continue
        items.append((line_number, text))
    return items


def _batch_concurrency(value: object, *, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        concurrency = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("批次并发必须是整数") from exc
    if concurrency < 1:
        raise ValueError("批次并发必须至少为 1")
    return concurrency


def _batch_result_url(job: dict) -> str:
    result = job.get("result")
    if not isinstance(result, dict):
        return ""
    return str(
        result.get("paypal_approve_url")
        or result.get("provider_redirect_url")
        or result.get("checkout_url")
        or ""
    )


def create_app(config: dict | None = None, *, gateway=None) -> Flask:
    static_dir = Path(__file__).resolve().parent / "static"
    app = Flask(__name__, static_folder=str(static_dir), static_url_path="/static")
    app.config.update(
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    if config:
        app.config.update(config)

    max_batch_items = max(
        1,
        int(app.config.get("MAX_BATCH_ITEMS", DEFAULT_MAX_BATCH_ITEMS)),
    )
    default_batch_concurrency = max(
        1,
        int(app.config.get("DEFAULT_BATCH_CONCURRENCY", DEFAULT_BATCH_CONCURRENCY)),
    )
    engine = HandoffEngine(gateway or LiveProtocolGateway())
    manager = JobManager(
        engine,
        max_workers=max(1, int(app.config.get("JOB_WORKERS", max_batch_items))),
        retention_seconds=int(app.config.get("JOB_RETENTION_SECONDS", 3600)),
        diagnostic_dir=app.config.get("DIAGNOSTIC_DIR")
        or os.environ.get("HANDOFF_DIAGNOSTIC_DIR")
        or Path("/tmp/handoff-diagnostics"),
    )
    app.extensions["job_manager"] = manager

    @app.after_request
    def no_store_sensitive_responses(response: Response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.get("/api/meta")
    def meta():
        return jsonify(
            {
                "countries": list_countries(),
                "proxy_schemes": list(SUPPORTED_PROXY_SCHEMES),
                "stripe_engines": ["python", "go"],
                "stripe_promo_strategies": ["upfront", "post_update", "mixed"],
                "defaults": {
                    "country": "BR",
                    "billing_country": "DE",
                    "checkout_country": "DE",
                    "checkout_attempts": DEFAULT_CHECKOUT_ATTEMPTS,
                    "provider_attempts": DEFAULT_PROVIDER_ATTEMPTS,
                    "batch_concurrency": default_batch_concurrency,
                    "proxy_scheme": "socks5",
                    "stripe_checkout": False,
                    "stripe_engine": "python",
                    "stripe_promo_strategy": "post_update",
                },
                "retention_seconds": manager.retention_seconds,
                "job_workers": manager.max_workers,
                "max_batch_items": max_batch_items,
            }
        )

    @app.post("/api/jobs")
    def create_job():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "请求必须是 JSON 对象"}), 400
        try:
            shared = _parse_shared_run_config(payload)
            spec = _build_run_spec(payload.get("access_token", ""), shared)
        except (ValueError, ProxyInputError) as exc:
            return jsonify({"error": str(exc)}), 400
        job = manager.create(spec)
        return jsonify({"job_id": job.id, "status": job.status}), 202

    @app.post("/api/batches")
    def create_batch():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "请求必须是 JSON 对象"}), 400
        try:
            token_lines = _batch_token_lines(payload.get("access_tokens"))
            if not token_lines:
                raise ValueError("批次至少需要一个 AT")
            concurrency = _batch_concurrency(
                payload.get("concurrency"),
                default=default_batch_concurrency,
            )
            shared = _parse_shared_run_config(payload)
            items: list[tuple[str, RunSpec]] = []
            seen_tokens: set[str] = set()
            duplicate_count = 0
            try:
                for line_number, raw_token in token_lines:
                    try:
                        spec = _build_run_spec(raw_token, shared)
                    except ValueError as exc:
                        raise ValueError(f"第 {line_number} 行 AT 无效：{exc}") from exc
                    if spec.access_token in seen_tokens:
                        spec.clear_secrets()
                        duplicate_count += 1
                        continue
                    if len(items) >= max_batch_items:
                        spec.clear_secrets()
                        raise ValueError(f"单个批次最多 {max_batch_items} 个唯一 AT")
                    seen_tokens.add(spec.access_token)
                    label = f"#{len(items) + 1:03d} · {spec.token_profile.email}"
                    items.append((label, spec))
            except Exception:
                for _label, item_spec in items:
                    item_spec.clear_secrets()
                raise
            if not items:
                raise ValueError("批次没有可提交的唯一 AT")
            batch = manager.create_batch(
                items,
                name=str(payload.get("batch_name") or ""),
                concurrency=concurrency,
            )
        except (ValueError, ProxyInputError) as exc:
            return jsonify({"error": str(exc)}), 400
        snapshot = manager.batch_snapshot(batch.id) or {}
        snapshot["duplicate_count"] = duplicate_count
        return jsonify(snapshot), 202

    @app.get("/api/batches")
    def list_batches():
        try:
            limit = int(request.args.get("limit") or 20)
        except ValueError:
            return jsonify({"error": "limit 必须是整数"}), 400
        return jsonify({"batches": manager.list_batches(limit=limit)})

    @app.get("/api/batches/<batch_id>")
    def batch_status(batch_id: str):
        compact = request.args.get("compact") in {"1", "true", "yes"}
        raw_revision = request.args.get("after_revision")
        try:
            after_revision = (
                max(0, int(raw_revision)) if raw_revision not in {None, ""} else None
            )
        except ValueError:
            return jsonify({"error": "after_revision 必须是整数"}), 400
        snapshot = manager.batch_snapshot(
            batch_id,
            compact_jobs=compact,
            after_revision=after_revision,
        )
        if snapshot is None:
            return jsonify({"error": "批次不存在或结果已过期"}), 404
        return jsonify(snapshot)

    @app.get("/api/batches/<batch_id>/results.csv")
    def batch_results_csv(batch_id: str):
        snapshot = manager.batch_snapshot(batch_id)
        if snapshot is None:
            return jsonify({"error": "批次不存在或结果已过期"}), 404

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            [
                "序号",
                "账号",
                "状态",
                "国家",
                "尝试次数",
                "结果链接",
                "Checkout Session",
                "失败原因",
            ]
        )
        for job in snapshot.get("jobs") or []:
            config = job.get("config") or {}
            result = job.get("result") or {}
            writer.writerow(
                [
                    job.get("batch_index") or "",
                    job.get("label") or "",
                    job.get("status") or "",
                    config.get("country") or "",
                    job.get("attempt") or 1,
                    _batch_result_url(job),
                    result.get("session_id") or "",
                    job.get("error") or "",
                ]
            )
        filename = f"batch-{batch_id[:8]}-results.csv"
        return Response(
            output.getvalue(),
            content_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/batches/<batch_id>/cancel")
    def cancel_batch(batch_id: str):
        result = manager.cancel_batch(batch_id)
        if result is None:
            return jsonify({"error": "批次不存在或结果已过期"}), 404
        accepted, snapshot = result
        return jsonify({"accepted": accepted, "batch": snapshot})

    @app.post("/api/batches/<batch_id>/retry")
    def retry_batch(batch_id: str):
        try:
            result = manager.retry_batch(batch_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        if result is None:
            return jsonify({"error": "批次不存在或结果已过期"}), 404
        jobs, snapshot = result
        return jsonify(
            {
                "created": len(jobs),
                "job_ids": [job.id for job in jobs],
                "batch": snapshot,
            }
        ), (202 if jobs else 200)

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id: str):
        job = manager.get(job_id)
        if job is None:
            return jsonify({"error": "任务不存在或结果已过期"}), 404
        return jsonify(job.snapshot())

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        job = manager.get(job_id)
        if job is None:
            return jsonify({"error": "任务不存在或结果已过期"}), 404
        accepted = job.cancel()
        return jsonify({"accepted": accepted, "status": job.status})

    @app.post("/api/jobs/<job_id>/retry")
    def retry_job(job_id: str):
        try:
            job = manager.retry_job(job_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        if job is None:
            return jsonify({"error": "任务或批次不存在或已过期"}), 404
        return jsonify({"job_id": job.id, "status": job.status}), 202

    @app.get("/api/jobs/<job_id>/events.json")
    def job_event_snapshot(job_id: str):
        job = manager.get(job_id)
        if job is None:
            return jsonify({"error": "任务不存在或结果已过期"}), 404
        try:
            after = max(0, int(request.args.get("after") or "0"))
        except ValueError:
            return jsonify({"error": "after 必须是整数"}), 400
        return jsonify(
            {
                "events": [item.to_dict() for item in job.events_after(after)],
                "status": job.status,
                "last_seq": job.snapshot()["last_seq"],
            }
        )

    @app.get("/api/jobs/<job_id>/events")
    def job_events(job_id: str):
        job = manager.get(job_id)
        if job is None:
            return jsonify({"error": "任务不存在或结果已过期"}), 404
        raw_after = request.args.get("after") or request.headers.get("Last-Event-ID") or "0"
        try:
            after = max(0, int(raw_after))
        except ValueError:
            return jsonify({"error": "after 必须是整数"}), 400

        @stream_with_context
        def stream():
            seq = after
            while True:
                events = job.wait_for_events(seq, timeout=15.0)
                if not events:
                    yield ": heartbeat\n\n"
                    if job.is_terminal:
                        return
                    continue
                for item in events:
                    seq = item.seq
                    data = json.dumps(item.to_dict(), ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {item.seq}\nevent: {item.event}\ndata: {data}\n\n"
                    if item.event == "done":
                        return

        return Response(
            stream(),
            mimetype="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    return app
