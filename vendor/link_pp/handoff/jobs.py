from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .engine import CancelledError, HandoffEngine, RunSpec
from .security import sanitize_diagnostic_payload, sanitize_message


_CONFIRM_DIAGNOSTICS = (
    ("second_confirm", "二次 confirm 完整响应（已脱敏）"),
    ("confirm", "confirm 完整响应（已脱敏）"),
)
_PROTOCOL_DIAGNOSTIC_MARKER = "[protocol-diagnostic] "
_FLOW_FAILURE_MARKER = "仍未取得 PayPal 链接："

_UI_INFO_MARKERS = (
    "生成 Checkout ",
    "提取 PayPal 链接 ",
    "未取得 PayPal 链接",
    "approve 返回 blocked",
    "approve 已通过",
    "Checkout 传输错误",
    "重建同代理 HTTP 会话",
    "临时限流",
    "等待 Stripe 最终状态",
    "Stripe 提链引擎",
    "开始 Go Stripe",
    "开始 Python Stripe",
    "Stripe 出口预检",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _failure_reason(error: object) -> str:
    text = str(error or "").strip()
    marker_at = text.rfind(_FLOW_FAILURE_MARKER)
    if marker_at >= 0:
        return text[marker_at + len(_FLOW_FAILURE_MARKER) :].strip()
    return text


def _clone_spec(spec: RunSpec) -> RunSpec:
    return RunSpec(
        access_token=spec.access_token,
        token_profile=spec.token_profile,
        proxy_country=spec.proxy_country,
        checkout_country=spec.checkout_country,
        proxies=spec.proxies,
        checkout_attempts=spec.checkout_attempts,
        provider_attempts=spec.provider_attempts,
        stripe_checkout=spec.stripe_checkout,
        stripe_engine=spec.stripe_engine,
        stripe_promo_strategy=spec.stripe_promo_strategy,
        device_id=spec.device_id,
        proxy_offset=spec.proxy_offset,
    )


class RetrySpec:
    """Private in-memory template used only to rebuild a failed batch item."""

    def __init__(self, spec: RunSpec):
        self._spec: RunSpec | None = _clone_spec(spec)

    def build(self) -> RunSpec:
        if self._spec is None:
            raise RuntimeError("批次重试信息已过期")
        return _clone_spec(self._spec)

    def clear(self) -> None:
        if self._spec is not None:
            self._spec.clear_secrets()
        self._spec = None


@dataclass(frozen=True, slots=True)
class JobEvent:
    seq: int
    event: str
    timestamp: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event": self.event,
            "timestamp": self.timestamp,
            "data": self.data,
        }


class Job:
    TERMINAL = {"success", "failed", "cancelled"}

    def __init__(
        self,
        spec: RunSpec,
        *,
        diagnostic_dir: Path | None = None,
        batch_id: str = "",
        batch_index: int = 0,
        label: str = "",
        attempt: int = 1,
        retry_of: str = "",
        on_change: Callable[[], None] | None = None,
    ):
        self.id = uuid.uuid4().hex
        self.batch_id = batch_id
        self.batch_index = batch_index
        self.label = label
        self.attempt = attempt
        self.retry_of = retry_of
        self.created_at = _now_iso()
        self.finished_at = ""
        self.status = "queued"
        self.result: dict[str, Any] | None = None
        self.error = ""
        self._spec: RunSpec | None = spec
        self._config = {
            "country": spec.proxy_country.code,
            "proxy_country": spec.proxy_country.code,
            "billing_country": spec.checkout_country.code,
            "checkout_country": spec.checkout_country.code,
            "checkout_currency": spec.checkout_country.currency,
            "checkout_attempts": spec.checkout_attempts,
            "provider_attempts": spec.provider_attempts,
            "stripe_checkout": spec.stripe_checkout,
            "stripe_engine": spec.stripe_engine,
            "stripe_promo_strategy": spec.stripe_promo_strategy,
            "proxy_count": len(spec.proxies),
        }
        self._cancel = threading.Event()
        self._condition = threading.Condition()
        self._diagnostic_lock = threading.Lock()
        self._diagnostic_dir = diagnostic_dir
        self._diagnostic_file = (
            diagnostic_dir / f"{self.id}.jsonl" if diagnostic_dir is not None else None
        )
        self._events: list[JobEvent] = []
        self._seq = 0
        self._finished_epoch = 0.0
        self._on_change = on_change
        self.add_event("state", {"status": "queued"})

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL

    @property
    def finished_epoch(self) -> float:
        return self._finished_epoch

    def cancel(self) -> bool:
        if self.is_terminal:
            return False
        self._cancel.set()
        self.add_event("state", {"status": "stopping"})
        return True

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def clear_secrets(self) -> None:
        with self._condition:
            if self._spec is not None:
                self._spec.clear_secrets()
                self._spec = None

    def add_event(self, event: str, data: dict[str, Any]) -> JobEvent:
        with self._condition:
            self._seq += 1
            item = JobEvent(self._seq, event, _now_iso(), data)
            self._events.append(item)
            if len(self._events) > 5000:
                del self._events[: len(self._events) - 5000]
            self._condition.notify_all()
        if event in {"state", "result", "done"} and self._on_change is not None:
            self._on_change()
        return item

    def emit_log(self, level: str, stage: str, message: str) -> None:
        token = self._spec.access_token if self._spec is not None else ""
        secrets: tuple[str, ...] = ()
        sanitized = sanitize_message(
            message,
            access_token=token,
            secrets=secrets,
            max_length=None,
        )
        diagnostic = self._parse_confirm_diagnostic(sanitized)
        full_message = sanitized
        if diagnostic is not None:
            _kind, label, payload = diagnostic
            full_message = (
                f"{label}: "
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
        try:
            self._append_full_log(level=level, stage=stage, message=full_message)
        except OSError:
            # Log persistence is observability only and must not affect a job.
            pass
        if diagnostic is not None:
            kind, _label, payload = diagnostic
            try:
                self._append_diagnostic(stage=stage, kind=kind, payload=payload)
            except OSError:
                pass
            return
        if level == "info" and not any(marker in sanitized for marker in _UI_INFO_MARKERS):
            return
        self.add_event(
            "log",
            {
                "level": level,
                "stage": stage,
                "message": sanitized,
            },
        )

    @staticmethod
    def _parse_confirm_diagnostic(
        message: object,
    ) -> tuple[str, str, object] | None:
        text = str(message or "")
        marker_at = text.find(_PROTOCOL_DIAGNOSTIC_MARKER)
        if marker_at >= 0:
            raw_payload = text[marker_at + len(_PROTOCOL_DIAGNOSTIC_MARKER) :]
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                return None
            sanitized_payload = sanitize_diagnostic_payload(payload)
            kind = "protocol"
            if isinstance(sanitized_payload, dict):
                candidate = str(sanitized_payload.get("kind") or "").strip()
                if candidate:
                    kind = candidate[:80]
            return kind, "协议交换完整记录（已脱敏）", sanitized_payload
        for kind, label in _CONFIRM_DIAGNOSTICS:
            marker = f"{label}: "
            marker_at = text.find(marker)
            if marker_at < 0:
                continue
            try:
                payload = json.loads(text[marker_at + len(marker) :])
            except json.JSONDecodeError:
                return None
            return kind, label, sanitize_diagnostic_payload(payload)
        return None

    def _append_record(self, record: dict[str, Any]) -> None:
        if self._diagnostic_file is None:
            return
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._diagnostic_lock:
            self._diagnostic_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                self._diagnostic_file,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as output:
                output.write(line)

    def _append_full_log(self, *, level: str, stage: str, message: str) -> None:
        self._append_record(
            {
                "timestamp": _now_iso(),
                "job_id": self.id,
                "kind": "log",
                "level": level,
                "stage": stage,
                "message": message,
            }
        )

    def _append_diagnostic(self, *, stage: str, kind: str, payload: object) -> None:
        self._append_record({
            "timestamp": _now_iso(),
            "job_id": self.id,
            "stage": stage,
            "kind": kind,
            "response": payload,
        })

    def events_after(self, seq: int) -> list[JobEvent]:
        with self._condition:
            return [item for item in self._events if item.seq > seq]

    def wait_for_events(self, seq: int, timeout: float = 15.0) -> list[JobEvent]:
        with self._condition:
            items = [item for item in self._events if item.seq > seq]
            if items or self.is_terminal:
                return items
            self._condition.wait(timeout)
            return [item for item in self._events if item.seq > seq]

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "id": self.id,
                "batch_id": self.batch_id or None,
                "batch_index": self.batch_index or None,
                "label": self.label or None,
                "attempt": self.attempt,
                "retry_of": self.retry_of or None,
                "status": self.status,
                "created_at": self.created_at,
                "finished_at": self.finished_at or None,
                "config": dict(self._config),
                "result": dict(self.result) if self.result else None,
                "error": self.error or None,
                "failure_reason": _failure_reason(self.error) or None,
                "last_seq": self._seq,
            }

    def compact_snapshot(self) -> dict[str, Any]:
        with self._condition:
            result = self.result or {}
            return {
                "id": self.id,
                "batch_index": self.batch_index or None,
                "label": self.label or None,
                "attempt": self.attempt,
                "status": self.status,
                "result_url": str(
                    result.get("paypal_approve_url")
                    or result.get("provider_redirect_url")
                    or ""
                ),
                "failure_reason": _failure_reason(self.error) or None,
            }

    def run(self, engine: HandoffEngine) -> None:
        spec = self._spec
        if spec is None:
            return
        token = spec.access_token
        try:
            if self.is_cancelled():
                raise CancelledError("任务已停止")
            self.status = "running"
            self.add_event("state", {"status": "running"})
            result = engine.run(
                spec,
                emit=self.emit_log,
                is_cancelled=self.is_cancelled,
            )
            self.result = result.to_dict()
            self.status = "success"
            self.add_event("result", dict(self.result))
        except CancelledError:
            self.status = "cancelled"
            self.error = "任务已停止"
        except Exception as exc:
            self.status = "failed"
            self.error = sanitize_message(
                exc,
                access_token=token,
            )
            self.emit_log("error", "job", self.error)
        finally:
            self.clear_secrets()
            self.finished_at = _now_iso()
            self._finished_epoch = time.time()
            self.add_event("state", {"status": self.status})
            self.add_event("done", {"status": self.status})


class Batch:
    TERMINAL = {"success", "failed", "cancelled", "partial"}

    def __init__(self, name: str, concurrency: int):
        self.id = uuid.uuid4().hex
        self.name = str(name or "").strip()[:80] or f"批次 {self.id[:8]}"
        self.concurrency = concurrency
        self.created_at = _now_iso()
        self.cancel_requested = False
        self._templates: dict[int, RetrySpec] = {}
        self._labels: dict[int, str] = {}
        self._attempts: dict[int, list[str]] = {}
        self._revision = 1
        self._revision_lock = threading.Lock()

    @property
    def revision(self) -> int:
        with self._revision_lock:
            return self._revision

    def touch(self) -> None:
        with self._revision_lock:
            self._revision += 1

    def add_item(self, index: int, label: str, spec: RunSpec, job_id: str) -> None:
        self._templates[index] = RetrySpec(spec)
        self._labels[index] = label
        self._attempts[index] = [job_id]

    def add_attempt(self, index: int, job_id: str) -> None:
        self._attempts[index].append(job_id)
        self.cancel_requested = False

    def latest_job_id(self, index: int) -> str:
        attempts = self._attempts.get(index) or []
        return attempts[-1] if attempts else ""

    def all_job_ids(self) -> tuple[str, ...]:
        return tuple(
            job_id
            for attempts in self._attempts.values()
            for job_id in attempts
        )

    def build_spec(self, index: int) -> RunSpec:
        template = self._templates.get(index)
        if template is None:
            raise RuntimeError("批次项目不存在或已过期")
        return template.build()

    def clear_secrets(self) -> None:
        for template in self._templates.values():
            template.clear()
        self._templates.clear()

    def _latest_jobs(self, jobs: dict[str, Job]) -> list[Job]:
        latest: list[Job] = []
        for index in sorted(self._attempts):
            job = jobs.get(self.latest_job_id(index))
            if job is not None:
                latest.append(job)
        return latest

    def finished_epoch(self, jobs: dict[str, Job]) -> float:
        latest = self._latest_jobs(jobs)
        if not latest or any(not job.is_terminal for job in latest):
            return 0.0
        return max((job.finished_epoch for job in latest), default=0.0)

    def snapshot(
        self,
        jobs: dict[str, Job],
        *,
        include_jobs: bool,
        compact_jobs: bool = False,
        after_revision: int | None = None,
    ) -> dict[str, Any]:
        revision = self.revision
        if after_revision is not None and after_revision == revision:
            return {"id": self.id, "revision": revision, "unchanged": True}
        latest = self._latest_jobs(jobs)
        counts = {
            "queued": 0,
            "running": 0,
            "success": 0,
            "failed": 0,
            "cancelled": 0,
        }
        for job in latest:
            counts[job.status] = counts.get(job.status, 0) + 1
        active = counts["queued"] + counts["running"]
        if active:
            status = "stopping" if self.cancel_requested else (
                "running" if counts["running"] else "queued"
            )
        elif latest and counts["success"] == len(latest):
            status = "success"
        elif latest and counts["failed"] == len(latest):
            status = "failed"
        elif latest and counts["cancelled"] == len(latest):
            status = "cancelled"
        else:
            status = "partial"
        finished_at = max(
            (job.finished_at for job in latest if job.finished_at),
            default="",
        )
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "status": status,
            "created_at": self.created_at,
            "finished_at": finished_at or None,
            "total": len(self._attempts),
            "concurrency": self.concurrency,
            "counts": counts,
            "active_count": active,
            "retryable_count": counts["failed"] + counts["cancelled"],
            "revision": revision,
        }
        if include_jobs:
            items = []
            for job in latest:
                item = job.compact_snapshot() if compact_jobs else job.snapshot()
                item["attempt_count"] = len(self._attempts.get(job.batch_index) or ())
                items.append(item)
            payload["jobs"] = items
        return payload


class JobManager:
    def __init__(
        self,
        engine: HandoffEngine,
        *,
        max_workers: int = 3,
        retention_seconds: int = 3600,
        diagnostic_dir: str | Path | None = None,
    ):
        self.engine = engine
        self.max_workers = max_workers
        self.retention_seconds = retention_seconds
        self.diagnostic_dir = Path(diagnostic_dir) if diagnostic_dir else None
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="handoff")
        self._jobs: dict[str, Job] = {}
        self._batches: dict[str, Batch] = {}
        self._submitted_batch_jobs: set[str] = set()
        self._shutting_down = False
        self._lock = threading.Lock()
        self._next_cleanup_epoch = 0.0

    def _cleanup(self) -> None:
        monotonic_now = time.monotonic()
        if monotonic_now < self._next_cleanup_epoch:
            return
        with self._lock:
            if monotonic_now < self._next_cleanup_epoch:
                return
            self._next_cleanup_epoch = monotonic_now + min(
                60.0,
                max(1.0, self.retention_seconds / 4),
            )
            cutoff = time.time() - self.retention_seconds
            expired_batches = [
                batch_id
                for batch_id, batch in self._batches.items()
                if (finished_epoch := batch.finished_epoch(self._jobs))
                and finished_epoch < cutoff
            ]
            for batch_id in expired_batches:
                batch = self._batches.pop(batch_id)
                batch.clear_secrets()
                for job_id in batch.all_job_ids():
                    self._jobs.pop(job_id, None)
            expired_jobs = [
                job_id
                for job_id, job in self._jobs.items()
                if not job.batch_id
                if job.finished_epoch and job.finished_epoch < cutoff
            ]
            for job_id in expired_jobs:
                self._jobs.pop(job_id, None)

    def create(self, spec: RunSpec) -> Job:
        self._cleanup()
        job = Job(spec, diagnostic_dir=self.diagnostic_dir)
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(job.run, self.engine)
        return job

    def create_batch(
        self,
        items: list[tuple[str, RunSpec]],
        *,
        name: str = "",
        concurrency: int = 3,
    ) -> Batch:
        if not items:
            raise ValueError("批次至少需要一个任务")
        if concurrency < 1:
            raise ValueError("批次并发至少为 1")
        self._cleanup()
        batch = Batch(name, concurrency)
        with self._lock:
            self._batches[batch.id] = batch
            for index, (label, spec) in enumerate(items, 1):
                spec.proxy_offset = (index - 1) % len(spec.proxies)
                job = Job(
                    spec,
                    diagnostic_dir=self.diagnostic_dir,
                    batch_id=batch.id,
                    batch_index=index,
                    label=label,
                    on_change=batch.touch,
                )
                batch.add_item(index, label, spec, job.id)
                self._jobs[job.id] = job
        self._submit_batch_jobs(batch.id)
        return batch

    def _submit_batch_jobs(self, batch_id: str) -> None:
        jobs_to_submit: list[Job] = []
        with self._lock:
            if self._shutting_down:
                return
            batch = self._batches.get(batch_id)
            if batch is None:
                return
            latest = batch._latest_jobs(self._jobs)
            in_flight = sum(
                1 for job in latest if job.id in self._submitted_batch_jobs
            )
            available = max(0, batch.concurrency - in_flight)
            for job in latest:
                if available == 0:
                    break
                if job.status != "queued" or job.id in self._submitted_batch_jobs:
                    continue
                self._submitted_batch_jobs.add(job.id)
                jobs_to_submit.append(job)
                available -= 1

        for job in jobs_to_submit:
            try:
                future = self._executor.submit(job.run, self.engine)
            except RuntimeError:
                with self._lock:
                    self._submitted_batch_jobs.discard(job.id)
                continue
            future.add_done_callback(
                lambda _future, current_batch_id=batch_id, job_id=job.id:
                    self._batch_job_finished(current_batch_id, job_id)
            )

    def _batch_job_finished(self, batch_id: str, job_id: str) -> None:
        with self._lock:
            self._submitted_batch_jobs.discard(job_id)
            shutting_down = self._shutting_down
        if not shutting_down:
            self._submit_batch_jobs(batch_id)

    def get(self, job_id: str) -> Job | None:
        self._cleanup()
        with self._lock:
            return self._jobs.get(job_id)

    def list_batches(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self._cleanup()
        bounded_limit = max(1, min(int(limit), 100))
        with self._lock:
            batches = sorted(
                self._batches.values(),
                key=lambda item: item.created_at,
                reverse=True,
            )[:bounded_limit]
            return [batch.snapshot(self._jobs, include_jobs=False) for batch in batches]

    def batch_snapshot(
        self,
        batch_id: str,
        *,
        compact_jobs: bool = False,
        after_revision: int | None = None,
    ) -> dict[str, Any] | None:
        self._cleanup()
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            return batch.snapshot(
                self._jobs,
                include_jobs=True,
                compact_jobs=compact_jobs,
                after_revision=after_revision,
            )

    def cancel_batch(self, batch_id: str) -> tuple[int, dict[str, Any]] | None:
        self._cleanup()
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            batch.cancel_requested = True
            latest = batch._latest_jobs(self._jobs)
            accepted = sum(1 for job in latest if job.cancel())
            snapshot = batch.snapshot(self._jobs, include_jobs=True)
        self._submit_batch_jobs(batch_id)
        return accepted, snapshot

    def _retry_item_locked(self, batch: Batch, index: int) -> Job:
        previous_id = batch.latest_job_id(index)
        previous = self._jobs.get(previous_id)
        if previous is None or not previous.is_terminal:
            raise ValueError("任务尚未结束，不能重试")
        if previous.status == "success":
            raise ValueError("成功任务无需重试")
        attempts = batch._attempts.get(index) or []
        job = Job(
            batch.build_spec(index),
            diagnostic_dir=self.diagnostic_dir,
            batch_id=batch.id,
            batch_index=index,
            label=batch._labels.get(index, ""),
            attempt=len(attempts) + 1,
            retry_of=previous.id,
            on_change=batch.touch,
        )
        batch.add_attempt(index, job.id)
        self._jobs[job.id] = job
        return job

    def retry_batch(self, batch_id: str) -> tuple[list[Job], dict[str, Any]] | None:
        self._cleanup()
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            retry_jobs = []
            for index in sorted(batch._attempts):
                latest = self._jobs.get(batch.latest_job_id(index))
                if latest is not None and latest.status in {"failed", "cancelled"}:
                    retry_jobs.append(self._retry_item_locked(batch, index))
            snapshot = batch.snapshot(self._jobs, include_jobs=True)
        self._submit_batch_jobs(batch_id)
        return retry_jobs, snapshot

    def retry_job(self, job_id: str) -> Job | None:
        self._cleanup()
        with self._lock:
            previous = self._jobs.get(job_id)
            if previous is None:
                return None
            if not previous.batch_id:
                raise ValueError("单任务不支持无 AT 重试")
            batch = self._batches.get(previous.batch_id)
            if batch is None:
                return None
            if batch.latest_job_id(previous.batch_index) != previous.id:
                raise ValueError("该任务已有更新的重试记录")
            job = self._retry_item_locked(batch, previous.batch_index)
        self._submit_batch_jobs(batch.id)
        return job

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._shutting_down = True
        self._executor.shutdown(wait=wait, cancel_futures=True)
        with self._lock:
            self._submitted_batch_jobs.clear()
            for batch in self._batches.values():
                batch.clear_secrets()
            if wait:
                for job in self._jobs.values():
                    job.clear_secrets()
