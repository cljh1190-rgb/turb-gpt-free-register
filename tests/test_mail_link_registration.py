# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from core import registration_service as svc
from core import generic_api_mail_client
from webui.app import create_app


class MailLinkRegistrationApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.get_executor_workers", return_value=2)
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}, {"id": 2}])
    @patch("webui.app.db.upsert_generic_api_emails_for_registration")
    def test_mail_link_text_binds_each_parsed_email(self, upsert, submit, _workers):
        upsert.return_value = {
            "accepted": ["a@icloud.com", "b@icloud.com"],
            "inserted": 2,
            "updated": 0,
            "skipped": 0,
            "errors": [],
        }
        text = "\n".join([
            "a@icloud.com----https://mail.example.test/a@icloud.com",
            "https://mail.example.test/b@icloud.com",
        ])
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            response = self.client.post("/api/jobs", json={"mail_link_text": text, "workers": 2})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["parsed"], 2)
        self.assertEqual(payload["submitted"], 2)
        records = upsert.call_args.args[0]
        self.assertEqual([item["email"] for item in records], ["a@icloud.com", "b@icloud.com"])
        submit.assert_called_once_with(
            emails=["a@icloud.com", "b@icloud.com"],
            email_source="generic_api",
            workers=2,
        )

    @patch("webui.app.svc.get_executor_workers", return_value=2)
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    @patch("webui.app.db.upsert_generic_api_emails_for_registration")
    def test_012e_password_and_token_url_reach_email_pool(self, upsert, _submit, _workers):
        upsert.return_value = {
            "accepted": ["test@012e.com"],
            "inserted": 1,
            "updated": 0,
            "skipped": 0,
            "errors": [],
        }
        code_url = (
            "http://mail.012e.com/api/getcode.php?"
            "token=dGVzdEAwMTJlLmNvbS0tLS1wYXNz%3D%3D"
        )

        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            response = self.client.post(
                "/api/jobs",
                json={"mail_link_text": f"test@012e.com----pass---{code_url}"},
            )

        self.assertEqual(response.status_code, 200)
        record = upsert.call_args.args[0][0]
        self.assertEqual(record["email"], "test@012e.com")
        self.assertEqual(record["password"], "pass")
        self.assertEqual(record["code_url"], code_url)

    @patch("webui.app.svc.submit_registration")
    def test_mail_link_registration_requires_auto_otp(self, submit):
        with patch.object(email_config, "USE_EMAIL_SERVICE", False):
            response = self.client.post(
                "/api/jobs",
                json={"mail_link_text": "a@icloud.com----https://mail.example.test/a@icloud.com"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("自动取码", response.get_json()["error"])
        submit.assert_not_called()


class AssignedGenericApiRegistrationTests(unittest.TestCase):
    @patch("core.email_provider.acquire_email")
    @patch("core.registration_service.db.claim_generic_api_email")
    def test_prepare_claims_assigned_generic_email_only(self, claim, acquire):
        claim.return_value = {"email": "a@icloud.com", "status": "used"}

        email, name, birthday = svc._prepare_registration_args(
            assigned_email="a@icloud.com",
            assigned_source="generic_api",
        )

        self.assertEqual(email, "a@icloud.com")
        self.assertTrue(name)
        self.assertTrue(birthday)
        claim.assert_called_once_with("a@icloud.com")
        acquire.assert_not_called()

    @patch("core.registration_service.db.get_generic_api_email_by_email", return_value={"status": "used"})
    @patch("core.registration_service.db.claim_generic_api_email", return_value=None)
    def test_prepare_rejects_unavailable_assigned_email(self, _claim, _get):
        with self.assertRaisesRegex(RuntimeError, "指定邮箱不可用"):
            svc._prepare_registration_args(
                assigned_email="busy@icloud.com",
                assigned_source="generic_api",
            )

    @patch("core.db.get_generic_api_email_by_email")
    def test_updated_mail_link_replaces_cached_context(self, get_row):
        generic_api_mail_client._CONTEXT_CACHE["a@icloud.com"] = generic_api_mail_client.GenericApiEmailAccount(
            email="a@icloud.com",
            code_url="https://old.example.test/a@icloud.com",
        )
        get_row.return_value = {
            "email": "a@icloud.com",
            "code_url": "https://new.example.test/a@icloud.com",
        }

        account = generic_api_mail_client.get_account_context("a@icloud.com")

        self.assertEqual(account.code_url, "https://new.example.test/a@icloud.com")
        generic_api_mail_client._CONTEXT_CACHE.pop("a@icloud.com", None)


if __name__ == "__main__":
    unittest.main()
