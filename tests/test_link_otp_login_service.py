import unittest
import threading
import time
from unittest.mock import patch

from core import link_otp_login_service as service


class FakeSession:
    def __init__(self, proxy):
        self.proxy = proxy
        self.closed = False

    def close(self):
        self.closed = True


class LinkOtpLoginServiceTests(unittest.TestCase):
    def tearDown(self):
        service.resume_link_login_jobs()
        with service._LOCK:
            service._JOBS.clear()

    def test_parses_prefixed_and_raw_mail_links(self):
        parsed = service.parse_link_login_input(
            "first@icloud.com----https://mail.example/messages/secret/first@icloud.com\n"
            "https://icloud-api.top/show/another-secret/second@icloud.com"
        )

        self.assertEqual(parsed["count"], 2)
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["records"][0]["email"], "first@icloud.com")
        self.assertEqual(parsed["records"][1]["email"], "second@icloud.com")

    def test_parses_password_bearing_mail_link(self):
        parsed = service.parse_link_login_input(
            "sample@example.test----fixture-password----"
            "http://mail.example.test/api/getcode?email=sample@example.test"
        )

        self.assertEqual(parsed["count"], 1)
        self.assertEqual(parsed["records"][0]["password"], "fixture-password")
        self.assertEqual(
            parsed["records"][0]["mail_url"],
            "http://mail.example.test/api/getcode?email=sample@example.test",
        )

    def test_public_job_hides_private_values_in_fields_and_errors(self):
        mail_url = "https://mail.example/messages/private-token/user@icloud.com"
        proxy = "http://proxy-user:proxy-pass@127.0.0.1:8080"
        access_token = "secret-access-token"
        public = service._public_job({
            "id": "job-1",
            "email": "user@icloud.com",
            "mail_url": mail_url,
            "proxy": proxy,
            "access_token": access_token,
            "message": f"读取 {mail_url}",
            "error": f"proxy={proxy} token={access_token}",
        })

        serialized = repr(public)
        self.assertNotIn(mail_url, serialized)
        self.assertNotIn("proxy-pass", serialized)
        self.assertNotIn(access_token, serialized)
        self.assertNotIn("mail_url", public)
        self.assertNotIn("proxy", public)
        self.assertNotIn("access_token", public)

    def test_proxy_preflight_failure_does_not_fall_back_to_local_ip(self):
        sessions = []
        progress_events = []

        def make_session(proxy=None, **_kwargs):
            fake = FakeSession(proxy)
            sessions.append(fake)
            return fake

        with patch.object(service, "BrowserSession", side_effect=make_session), patch.object(
            service, "network_preflight", side_effect=[RuntimeError("proxy unavailable"), None]
        ), patch("config.proxy.proxy_required", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "禁止切换本机直连"):
                service._create_preflight_session(
                    proxy="https://43.135.181.9:18155",
                    progress=lambda **event: progress_events.append(event),
                )

        self.assertEqual([item.proxy for item in sessions], ["https://43.135.181.9:18155"])
        self.assertTrue(sessions[0].closed)
        self.assertFalse(any("已切换本机直连" in event.get("message", "") for event in progress_events))

    def test_complete_existing_account_login_and_plus_query(self):
        fake_session = FakeSession("")
        plan = {
            "ok": True,
            "current_plan_type": "plus",
            "has_active_subscription": True,
            "plus_trial_eligible": False,
            "checked_at": "2026-08-03T10:00:00",
        }
        with patch.object(service, "_latest_openai_otp", return_value=("111111", {"ok": True})), \
             patch.object(service, "_create_preflight_session", return_value=(fake_session, "direct", True)), \
             patch.object(service, "get_providers"), \
             patch.object(service, "get_csrf_token", return_value="csrf"), \
             patch.object(service, "signin_openai", return_value="https://auth.openai.com/authorize"), \
             patch.object(service, "follow_authorize"), \
             patch.object(service, "_wait_for_new_otp", return_value="222222"), \
             patch.object(service, "validate_email_otp", return_value={"continue_url": "https://chatgpt.com/api/auth/callback"}), \
             patch.object(service, "_finish_oauth_session", return_value=({}, "access-token")), \
             patch.object(service, "check_account_plan", return_value=plan), \
             patch.object(service, "token_claims", return_value={"user_id": "user-1", "claim_plan_type": "plus"}), \
             patch.object(service.db, "upsert_plus_check_accounts", return_value=[{"id": 7}]), \
             patch.object(service.db, "update_account_plan_check") as update_plan:
            result = service.login_and_query_by_mail_link(
                email="user@icloud.com",
                mail_url="https://mail.example/messages/secret/user@icloud.com",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["plus_status"], "opened")
        self.assertEqual(result["network_mode"], "direct")
        self.assertTrue(result["fallback_used"])
        self.assertTrue(fake_session.closed)
        update_plan.assert_called_once_with(acc_id=7, result=plan)

    def test_about_you_flow_is_rejected_without_registration(self):
        fake_session = FakeSession("")
        with patch.object(service, "_latest_openai_otp", return_value=(None, {"ok": True})), \
             patch.object(service, "_create_preflight_session", return_value=(fake_session, "direct", False)), \
             patch.object(service, "get_providers"), \
             patch.object(service, "get_csrf_token", return_value="csrf"), \
             patch.object(service, "signin_openai", return_value="https://auth.openai.com/authorize"), \
             patch.object(service, "follow_authorize"), \
             patch.object(service, "_wait_for_new_otp", return_value="222222"), \
             patch.object(service, "validate_email_otp", return_value={"page": {"type": "about_you"}}), \
             patch.object(service, "_finish_oauth_session") as finish_session:
            with self.assertRaisesRegex(RuntimeError, "不会自动注册"):
                service.login_and_query_by_mail_link(
                    email="new@icloud.com",
                    mail_url="https://mail.example/messages/secret/new@icloud.com",
                )

        finish_session.assert_not_called()
        self.assertTrue(fake_session.closed)

    def test_pause_checkpoint_blocks_and_resume_continues(self):
        job_id = "pause-job"
        with service._LOCK:
            service._JOBS[job_id] = {
                "id": job_id,
                "email": "user@icloud.com",
                "status": "running",
                "stage": "waiting_otp",
                "message": "等待验证码",
                "started_at": service._now(),
                "created_at": service._now(),
                "updated_at": service._now(),
            }

        pause_result = service.pause_link_login_jobs()
        worker = threading.Thread(target=service._pause_checkpoint, args=(job_id,))
        worker.start()
        deadline = time.time() + 2
        while time.time() < deadline:
            if service._JOBS[job_id]["status"] == "paused":
                break
            time.sleep(0.02)

        self.assertTrue(pause_result["paused"])
        self.assertEqual(service._JOBS[job_id]["status"], "paused")
        self.assertTrue(worker.is_alive())

        resume_result = service.resume_link_login_jobs()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertFalse(resume_result["paused"])
        self.assertEqual(service._JOBS[job_id]["status"], "running")
        self.assertEqual(service._JOBS[job_id]["message"], "等待验证码")

    def test_paused_time_does_not_consume_otp_timeout(self):
        control_calls = 0

        def control():
            nonlocal control_calls
            control_calls += 1
            if control_calls == 1:
                time.sleep(0.08)

        with patch.object(
            service,
            "_latest_openai_otp",
            side_effect=[(None, {"ok": True}), ("222222", {"ok": True})],
        ):
            code = service._wait_for_new_otp(
                "https://mail.example.test/user@icloud.com",
                baseline="111111",
                max_wait=0.05,
                poll_interval=0.001,
                control=control,
            )

        self.assertEqual(code, "222222")


if __name__ == "__main__":
    unittest.main()
