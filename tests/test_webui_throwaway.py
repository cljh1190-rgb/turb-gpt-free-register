# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class ThrowawayWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": index} for index in range(7)])
    def test_jobs_only_need_count_with_throwaway(self, submit_registration, outlook_pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "throwaway"
        ):
            response = self.client.post("/api/jobs", json={"count": 7, "workers": 3})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=7, workers=3)

    @patch("webui.app.db.domain_email_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.db.count_accounts", return_value=0)
    def test_summary_does_not_treat_throwaway_as_outlook(self, count_accounts, outlook_pool_summary, domain_pool_summary):
        with patch.object(email_config, "EMAIL_SOURCE", "throwaway"):
            response = self.client.get("/api/summary")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["outlook_total"], 0)
        outlook_pool_summary.assert_not_called()

    def test_main_registration_toolbar_hides_worker_input(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertIn('<input id="regWorkersV2" type="hidden" value="3">', html)

    def test_throwaway_is_source_code_default(self):
        self.assertEqual(email_config.EMAIL_SOURCE, "throwaway")
        self.assertTrue(email_config.USE_EMAIL_SERVICE)


if __name__ == "__main__":
    unittest.main()
