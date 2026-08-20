# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import patch

from core import extract_link_service as service
from core import plan_check_service


class ExtractLinkWorkbenchTests(unittest.TestCase):
    def setUp(self):
        with service._WORKBENCH_PROXY_CURSOR_LOCK:
            service._WORKBENCH_PROXY_CURSORS.update({"checkout": 0, "update": 0})

    def test_proxy_pool_rotates_per_task(self):
        pool = ["http://first.example:3010", "http://second.example:3010"]
        self.assertEqual(service._next_workbench_proxy("checkout", pool), pool[0])
        self.assertEqual(service._next_workbench_proxy("checkout", pool), pool[1])
        self.assertEqual(service._next_workbench_proxy("checkout", pool), pool[0])

    def test_plan_eligibility_auto_queues_workbench_without_cdk(self):
        with patch.dict(os.environ, {"ENABLE_EXTRACT_AUTO": "True"}), \
             patch.object(service, "extraction_enabled", return_value=True), \
             patch.object(service, "_api_base", return_value="https://www.1k50.xyz/extract"), \
             patch.object(service, "_provider", return_value="workbench"), \
             patch.object(service, "_cdk") as cdk, \
             patch.object(
                 service,
                 "enqueue_account_extract",
                 return_value={"accepted": True, "busy": False},
             ) as enqueue:
            plan_check_service._maybe_auto_extract_after_plan(
                account_id=7,
                email="eligible@example.com",
                access_token="opaque-token",
                plan_result={
                    "current_plan_type": "free",
                    "plus_trial_eligible": True,
                },
                trigger="registration_auto",
            )

        cdk.assert_not_called()
        enqueue.assert_called_once_with(
            account_id=7,
            email="eligible@example.com",
            access_token="opaque-token",
            trigger="plan_auto:registration_auto",
            link_type=None,
        )

    def test_normalizes_external_workbench_proxy_format(self):
        self.assertEqual(
            service._normalize_workbench_proxy(
                "us.example:3010:user-name:pass-word"
            ),
            "http://user-name:pass-word@us.example:3010",
        )
        self.assertEqual(
            service._normalize_workbench_proxy("socks5h://user:pass@host:1080"),
            "socks5h://user:pass@host:1080",
        )

    def test_creates_workbench_task_with_both_proxy_pools(self):
        values = {
            "EXTRACT_LINK_CHECKOUT_PROXY_POOL": ["checkout.example:3010:cu:cp"],
            "EXTRACT_LINK_UPDATE_PROXY_POOL": ["update.example:3010:uu:up"],
            "EXTRACT_LINK_WORKBENCH_APPLY_UPDATE": "True",
            "EXTRACT_LINK_WORKBENCH_OAICS_ONLY": "False",
            "EXTRACT_LINK_WORKBENCH_COUNTRY": "GB",
            "EXTRACT_LINK_WORKBENCH_PAYMENT_METHOD": "paypal",
            "EXTRACT_LINK_WORKBENCH_WINDOW_ID": "test-window",
        }

        def setting(name, default=None):
            return values.get(name, default)

        with patch.object(service, "_runtime_setting", side_effect=setting), \
             patch.object(service, "_api_base", return_value="https://www.1k50.xyz/extract"), \
             patch.object(service, "_is_workbench", return_value=True), \
             patch.object(service, "_http_json", return_value=(202, {"task_id": "wb-1"})) as http:
            result = service._create_extract_job(
                token="opaque-token",
                link_type="upi",
                cdk="",
                email="user@example.com",
            )

        self.assertEqual(result["provider"], "workbench")
        self.assertEqual(result["task_id"], "wb-1")
        payload = http.call_args.kwargs["payload"]
        self.assertEqual(
            http.call_args.kwargs["headers"],
            {"X-Workbench-Visitor": "test-window"},
        )
        self.assertEqual(payload["checkout_proxy"], "http://cu:cp@checkout.example:3010")
        self.assertEqual(payload["update_proxy"], "http://uu:up@update.example:3010")
        self.assertEqual(payload["checkout_proxy_pool"], ["http://cu:cp@checkout.example:3010"])
        self.assertEqual(payload["update_proxy_pool"], ["http://uu:up@update.example:3010"])
        self.assertTrue(payload["apply_checkout_update"])
        self.assertEqual(payload["country"], "GB")
        self.assertEqual(payload["payment_method"], "paypal")

    def test_polls_workbench_until_result_link(self):
        responses = iter([
            (200, {"task": {"status": "running", "stage": "checkout", "progress": 45}}),
            (200, {"task": {"status": "completed", "result_url": "https://pay.example/link"}}),
        ])
        with patch.object(service, "_api_base", return_value="https://www.1k50.xyz/extract"), \
             patch.object(service, "_http_json", side_effect=lambda *args, **kwargs: next(responses)) as http, \
             patch.object(service.time, "sleep"), \
             patch.object(service.db, "update_account_extract") as update:
            result = service._poll_workbench_task(
                task_id="wb-1",
                visitor_id="test-window",
                account_id=9,
                link_type="upi",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["url"], "https://pay.example/link")
        self.assertEqual(update.call_count, 2)
        for call in http.call_args_list:
            self.assertEqual(
                call.kwargs["headers"],
                {"X-Workbench-Visitor": "test-window"},
            )

    def test_run_extract_rotates_after_unusual_activity(self):
        jobs = [
            {"provider": "workbench", "job_id": "wb-1", "visitor_id": "window-1"},
            {"provider": "workbench", "job_id": "wb-2", "visitor_id": "window-2"},
        ]
        final = {"ok": True, "status": "success", "job_id": "wb-2"}
        with patch.object(service, "extraction_enabled", return_value=True), \
             patch.object(service.db, "mark_account_extract_running", return_value=True), \
             patch.object(service.db, "update_account_extract"), \
             patch.object(service, "_workbench_proxy_pool", return_value=["p1", "p2"]), \
             patch.object(service, "_create_extract_job", side_effect=jobs) as create, \
             patch.object(
                 service,
                 "_poll_workbench_task",
                 side_effect=[RuntimeError("checkout create failed: unusual activity"), final],
             ) as poll:
            result = service._run_extract(
                account_id=7,
                email="eligible@example.com",
                access_token="opaque-token",
                link_type="upi",
                cdk="",
                trigger="plan_auto:registration_auto",
                release_slot=False,
            )

        self.assertEqual(result, final)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(poll.call_count, 2)


if __name__ == "__main__":
    unittest.main()
