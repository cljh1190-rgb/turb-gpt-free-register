# -*- coding: utf-8 -*-
"""号池服务测试：可用性判定 / 分配 / 无感切号 / 统计 / 巡检。"""
import base64
import contextlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from core import account_pool, db


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(payload: dict) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    return f"{header}.{body}.sig"


def _expired_token() -> str:
    return _jwt({"exp": int(time.time()) - 3600, "email": "expired@x.com"})


def _valid_token() -> str:
    return _jwt({"exp": int(time.time()) + 3600, "email": "ok@x.com"})


@contextlib.contextmanager
def _isolated_accounts(rows: list[dict]):
    """把假账号写入独立临时 JSON，并 patch db 各文件路径 + 号池配置，隔离真实数据。"""
    with tempfile.TemporaryDirectory(prefix="pool_test_") as td:
        root = Path(td)
        accounts_path = root / "accounts.json"
        accounts_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        with patch.object(db, "_ACCOUNTS_JSON", accounts_path), \
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy_accounts.json"), \
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                patch.object(db, "_VIEWER_HTML", root / "viewer.html"), \
                patch("config.pool.POOL_ENABLED", True), \
                patch("config.pool.POOL_QUOTA_THRESHOLD_PERCENT", 20.0), \
                patch("config.pool.POOL_ALLOW_UNKNOWN_QUOTA", True), \
                patch("config.pool.POOL_PROBE_STALE_SECONDS", 3600), \
                patch("config.pool.POOL_ACQUIRE_STRATEGY", "round_robin"):
            yield db


class AccountUsableTests(unittest.TestCase):
    def test_quota_limit_reached_unusable(self):
        row = {"id": 1, "email": "a@x.com", "access_token": _valid_token(), "quota_limit_reached": True}
        usable, reason = account_pool.account_usable(row)
        self.assertFalse(usable)
        self.assertIn("上限", reason)

    def test_remaining_below_threshold_unusable(self):
        row = {"id": 1, "email": "a@x.com", "access_token": _valid_token(), "primary_remaining_percent": 10}
        usable, reason = account_pool.account_usable(row)
        self.assertFalse(usable)
        self.assertIn("阈值", reason)

    def test_spend_control_reached_unusable(self):
        row = {"id": 1, "email": "a@x.com", "access_token": _valid_token(), "spend_control_reached": True}
        usable, _ = account_pool.account_usable(row)
        self.assertFalse(usable)

    def test_credits_depleted_unusable(self):
        row = {"id": 1, "email": "a@x.com", "access_token": _valid_token(),
               "credits_has_credits": True, "credits_unlimited": False, "credits_balance": 0}
        usable, _ = account_pool.account_usable(row)
        self.assertFalse(usable)

    def test_token_expired_unusable(self):
        row = {"id": 1, "email": "a@x.com", "access_token": _expired_token()}
        usable, reason = account_pool.account_usable(row)
        self.assertFalse(usable)
        self.assertIn("过期", reason)

    def test_http_401_unusable(self):
        row = {"id": 1, "email": "a@x.com", "access_token": _valid_token(), "quota_check_http_status": 401}
        usable, _ = account_pool.account_usable(row)
        self.assertFalse(usable)

    def test_manual_disabled_unusable(self):
        row = {"id": 1, "email": "a@x.com", "access_token": _valid_token(),
               "pool_enabled": False, "pool_disabled_reason": "人工处理中"}
        usable, reason = account_pool.account_usable(row)
        self.assertFalse(usable)
        self.assertIn("人工处理中", reason)

    def test_codex_deactivated_unusable(self):
        row = {"id": 1, "email": "a@x.com", "access_token": _valid_token(), "codex_status": "deactivated"}
        usable, _ = account_pool.account_usable(row)
        self.assertFalse(usable)

    def test_unknown_quota_default_available(self):
        row = {"id": 1, "email": "a@x.com", "access_token": _valid_token()}
        usable, reason = account_pool.account_usable(row)
        self.assertTrue(usable)
        self.assertEqual(reason, "")

    def test_unknown_quota_blocked_when_not_allowed(self):
        row = {"id": 1, "email": "a@x.com", "access_token": _valid_token()}
        with patch("config.pool.POOL_ALLOW_UNKNOWN_QUOTA", False):
            usable, reason = account_pool.account_usable(row)
            self.assertFalse(usable)
            self.assertIn("额度数据", reason)


class AcquireTests(unittest.TestCase):
    def test_acquire_prefers_available_account(self):
        rows = [
            {"id": 1, "email": "bad@x.com", "access_token": _valid_token(), "quota_limit_reached": True},
            {"id": 2, "email": "good@x.com", "access_token": _valid_token()},
        ]
        with _isolated_accounts(rows):
            result = account_pool.acquire()
            self.assertTrue(result["ok"])
            self.assertEqual(result["email"], "good@x.com")
            self.assertTrue(result["access_token"])
            self.assertEqual(result["account_id"], 2)

    def test_acquire_no_available_returns_error(self):
        rows = [
            {"id": 1, "email": "a@x.com", "access_token": _valid_token(), "quota_limit_reached": True},
        ]
        with _isolated_accounts(rows):
            result = account_pool.acquire()
            self.assertFalse(result["ok"])
            self.assertIn("暂无可用账号", result["error"])

    def test_acquire_prefer_email(self):
        rows = [
            {"id": 1, "email": "one@x.com", "access_token": _valid_token()},
            {"id": 2, "email": "two@x.com", "access_token": _valid_token()},
        ]
        with _isolated_accounts(rows):
            result = account_pool.acquire(prefer_email="two@x.com")
            self.assertTrue(result["ok"])
            self.assertEqual(result["email"], "two@x.com")

    def test_acquire_prefer_email_unavailable_falls_back(self):
        rows = [
            {"id": 1, "email": "one@x.com", "access_token": _valid_token(), "quota_limit_reached": True},
            {"id": 2, "email": "two@x.com", "access_token": _valid_token()},
        ]
        with _isolated_accounts(rows):
            result = account_pool.acquire(prefer_email="one@x.com")
            self.assertTrue(result["ok"])
            self.assertEqual(result["email"], "two@x.com")

    def test_acquire_round_robin_no_consecutive(self):
        rows = [
            {"id": 1, "email": "one@x.com", "access_token": _valid_token()},
            {"id": 2, "email": "two@x.com", "access_token": _valid_token()},
        ]
        with _isolated_accounts(rows):
            first = account_pool.acquire()
            second = account_pool.acquire()
            self.assertNotEqual(first["email"], second["email"])

    def test_acquire_tags_filter(self):
        rows = [
            {"id": 1, "email": "plus@x.com", "access_token": _valid_token(), "plan_type": "chatgpt_plus"},
            {"id": 2, "email": "free@x.com", "access_token": _valid_token(), "plan_type": "free"},
        ]
        with _isolated_accounts(rows):
            result = account_pool.acquire(tags=["plus"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["email"], "plus@x.com")
            self.assertIn("plus", str(result["plan_type"]).lower())


class SwitchTests(unittest.TestCase):
    def test_switch_marks_old_and_returns_new(self):
        rows = [
            {"id": 1, "email": "old@x.com", "access_token": _valid_token()},
            {"id": 2, "email": "new@x.com", "access_token": _valid_token()},
        ]
        with _isolated_accounts(rows):
            result = account_pool.switch(current_email="old@x.com", reason="调用方限流")
            self.assertTrue(result["ok"])
            self.assertEqual(result["email"], "new@x.com")
            self.assertEqual(result["switch_reason"], "调用方限流")
            self.assertEqual(result["switched_from"]["email"], "old@x.com")
            old = db.get_account_by_email("old@x.com")
            self.assertEqual(old["pool_status"], "exhausted")
            self.assertIn("限流", old["pool_exhausted_reason"] or "")
            # 旧账号已耗尽，重新分配不会再拿到它
            again = account_pool.acquire()
            self.assertEqual(again["email"], "new@x.com")

    def test_switch_no_current_uses_last_acquired(self):
        rows = [
            {"id": 1, "email": "one@x.com", "access_token": _valid_token()},
            {"id": 2, "email": "two@x.com", "access_token": _valid_token()},
        ]
        with _isolated_accounts(rows):
            account_pool.acquire(prefer_email="one@x.com")
            result = account_pool.switch(reason="auto")
            self.assertTrue(result["ok"])
            self.assertEqual(result["switched_from"]["email"], "one@x.com")
            self.assertEqual(result["email"], "two@x.com")


class PoolSummaryTests(unittest.TestCase):
    def test_pool_summary_counts(self):
        rows = [
            {"id": 1, "email": "ok@x.com", "access_token": _valid_token()},
            {"id": 2, "email": "ex@x.com", "access_token": _valid_token(), "quota_check_status": "success", "quota_limit_reached": True},
            {"id": 3, "email": "off@x.com", "access_token": _valid_token(), "pool_enabled": False, "pool_disabled_reason": "人工"},
            {"id": 4, "email": "arch@x.com", "access_token": _valid_token(), "archived": True},
        ]
        with _isolated_accounts(rows):
            summary = account_pool.pool_summary()
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["total"], 4)
            self.assertEqual(summary["in_pool"], 2)
            self.assertEqual(summary["available"], 1)
            self.assertEqual(summary["unavailable"], 1)
            self.assertEqual(summary["disabled"], 1)
            self.assertEqual(summary["unknown"], 1)
            self.assertIn("额度已达上限", summary["reasons"])


class PoolStateTests(unittest.TestCase):
    def test_set_state_enable_disable(self):
        rows = [{"id": 1, "email": "a@x.com", "access_token": _valid_token()}]
        with _isolated_accounts(rows):
            res = account_pool.set_account_pool_state(1, enabled=False, reason="临时停用")
            self.assertTrue(res["ok"])
            acc = db.get_account(1)
            self.assertFalse(acc["pool_enabled"])
            self.assertEqual(acc["pool_disabled_reason"], "临时停用")
            res = account_pool.set_account_pool_state(1, enabled=True)
            self.assertTrue(res["ok"])
            acc = db.get_account(1)
            self.assertTrue(acc["pool_enabled"])
            self.assertIsNone(acc.get("pool_disabled_reason"))
            self.assertEqual(acc.get("pool_status"), "available")

    def test_set_state_missing_account(self):
        with _isolated_accounts([]):
            res = account_pool.set_account_pool_state(99, enabled=False, reason="x")
            self.assertFalse(res["ok"])
            self.assertIn("不存在", res["error"])


class ProbeTests(unittest.TestCase):
    def test_probe_enqueues_stale_quota_checks(self):
        recent = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
        rows = [
            {"id": 1, "email": "a@x.com", "access_token": _valid_token()},
            {"id": 2, "email": "b@x.com", "access_token": _valid_token(), "quota_last_success_at": recent},
            {"id": 3, "email": "c@x.com"},
        ]
        with _isolated_accounts(rows), \
                patch("core.account_pool.enqueue_account_quota_check", return_value={"accepted": True}) as m:
            result = account_pool.enqueue_pool_probe()
            self.assertTrue(result["ok"])
            self.assertEqual(result["queued"], 1)
            self.assertEqual(result["skipped_fresh"], 1)
            self.assertEqual(result["skipped_invalid"], 1)
            m.assert_called_once()
            self.assertEqual(m.call_args.kwargs["account_id"], 1)
            self.assertEqual(m.call_args.kwargs["email"], "a@x.com")


if __name__ == "__main__":
    unittest.main()
