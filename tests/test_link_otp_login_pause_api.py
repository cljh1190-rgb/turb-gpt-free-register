# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class LinkOtpLoginPauseApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.link_login_pause_state", return_value={"paused": True, "counts": {"paused": 1}})
    @patch("webui.app.list_link_login_jobs", return_value=[{"id": "job-1", "status": "paused"}])
    def test_jobs_response_contains_pause_state(self, _list_jobs, _pause_state):
        response = self.client.get("/api/plus-check/login-by-mail-link/jobs")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["paused"])

    @patch("webui.app.pause_link_login_jobs", return_value={"ok": True, "paused": True, "affected_running": 2, "queued": 3})
    def test_pause_endpoint(self, pause_jobs):
        response = self.client.post("/api/plus-check/login-by-mail-link/pause")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["paused"])
        pause_jobs.assert_called_once_with()

    @patch("webui.app.resume_link_login_jobs", return_value={"ok": True, "paused": False, "resumed": 2})
    def test_resume_endpoint(self, resume_jobs):
        response = self.client.post("/api/plus-check/login-by-mail-link/resume")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["paused"])
        resume_jobs.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
