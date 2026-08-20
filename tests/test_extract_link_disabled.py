# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import extract_link_service
from webui.app import create_app


class ExtractLinkDisabledTests(unittest.TestCase):
    def test_disabled_service_does_not_enqueue_task(self):
        with patch.object(extract_link_service, "extraction_enabled", return_value=False):
            result = extract_link_service.enqueue_account_extract(
                account_id=1,
                email="user@example.com",
                access_token="token",
            )
        self.assertFalse(result["accepted"])
        self.assertTrue(result["disabled"])

    def test_disabled_api_rejects_manual_extract_before_account_lookup(self):
        app = create_app(auth_code="test-auth")
        client = app.test_client()
        with patch.object(extract_link_service, "extraction_enabled", return_value=False):
            response = client.post(
                "/api/accounts/extract-link",
                headers={"X-Auth-Code": "test-auth"},
                json={"account_id": 1},
            )
        self.assertEqual(response.status_code, 410)
        self.assertTrue(response.get_json()["disabled"])


if __name__ == "__main__":
    unittest.main()
