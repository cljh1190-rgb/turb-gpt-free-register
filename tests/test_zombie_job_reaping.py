# -*- coding: utf-8 -*-
"""僵尸任务回收功能测试：reap_zombie_jobs / is_job_active。"""
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, registration_service as svc


def _write_fake_jobs(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


@contextlib.contextmanager
def _isolated_jobs(rows: list[dict]):
    """把假任务写入独立临时文件，并 patch db._JOBS_JSON 指向它，
    隔离真实注册任务.json；退出时自动恢复，不影响其它测试。"""
    path = Path(tempfile.mkdtemp(prefix="zombie_test_")) / "jobs.json"
    _write_fake_jobs(path, rows)
    with patch.object(db, "_JOBS_JSON", path), \
            patch.object(svc, "_ACTIVE_JOBS", set()), \
            patch.object(svc, "_STOP_EVENTS", {}):
        yield db


class ZombieJobReapTests(unittest.TestCase):
    """注意：_ACTIVE_JOBS / _STOP_EVENTS 是模块级状态，测试一律用 patch 换新集合，
    避免污染其它测试和真实 WebUI 运行态。"""

    def test_reaps_running_job_without_active_instance(self):
        jobs = [{
            "id": 9001,
            "status": "running",
            "email": "zombie@example.com",
            "error_message": None,
            "started_at": "2026-08-12T10:00:00",
            "completed_at": None,
        }]
        with _isolated_jobs(jobs) as db_mod:
            reaped = svc.reap_zombie_jobs()
            self.assertEqual(reaped, 1)
            job = db_mod.get_job(9001)
            self.assertEqual(job["status"], "stopped")
            self.assertIsNotNone(job["completed_at"])
            self.assertIn("僵尸任务已回收", job["error_message"] or "")

    def test_reaps_stopping_job_without_active_instance(self):
        jobs = [{
            "id": 9003,
            "status": "stopping",
            "email": "stopping-zombie@example.com",
            "error_message": None,
            "completed_at": None,
        }]
        with _isolated_jobs(jobs) as db_mod:
            # _STOP_EVENTS 里残留停止标记也一并清理
            with patch.object(svc, "_STOP_EVENTS", {9003: object()}):
                reaped = svc.reap_zombie_jobs()
            self.assertEqual(reaped, 1)
            job = db_mod.get_job(9003)
            self.assertEqual(job["status"], "stopped")
            self.assertIsNotNone(job["completed_at"])

    def test_keeps_running_job_with_active_instance(self):
        jobs = [{
            "id": 9002,
            "status": "running",
            "email": "active@example.com",
            "error_message": None,
            "completed_at": None,
        }]
        with _isolated_jobs(jobs) as db_mod, \
                patch.object(svc, "_ACTIVE_JOBS", {9002}):
            reaped = svc.reap_zombie_jobs()
            self.assertEqual(reaped, 0)
            job = db_mod.get_job(9002)
            self.assertEqual(job["status"], "running")
            self.assertIsNone(job["completed_at"])

    def test_reap_is_idempotent(self):
        jobs = [{
            "id": 9004,
            "status": "running",
            "email": "idempotent@example.com",
            "error_message": None,
            "completed_at": None,
        }]
        with _isolated_jobs(jobs) as db_mod:
            self.assertEqual(svc.reap_zombie_jobs(), 1)
            # 已回收后再跑一遍不再重复处理
            self.assertEqual(svc.reap_zombie_jobs(), 0)
            self.assertEqual(db_mod.get_job(9004)["status"], "stopped")

    def test_does_not_touch_terminal_or_pending_jobs(self):
        jobs = [
            {"id": 9010, "status": "success", "email": "a@example.com"},
            {"id": 9011, "status": "failed", "email": "b@example.com"},
            {"id": 9012, "status": "cancelled", "email": "c@example.com"},
            {"id": 9013, "status": "pending", "email": "d@example.com"},
        ]
        with _isolated_jobs(jobs) as db_mod:
            reaped = svc.reap_zombie_jobs()
            self.assertEqual(reaped, 0)
            for job_id, expected in ((9010, "success"), (9011, "failed"), (9012, "cancelled"), (9013, "pending")):
                self.assertEqual(db_mod.get_job(job_id)["status"], expected)

    def test_is_job_active_reflects_active_set(self):
        with patch.object(svc, "_ACTIVE_JOBS", {42}):
            self.assertTrue(svc.is_job_active(42))
            self.assertFalse(svc.is_job_active(43))
        self.assertFalse(svc.is_job_active(None))
        self.assertFalse(svc.is_job_active(0))


if __name__ == "__main__":
    unittest.main()
