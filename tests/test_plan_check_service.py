# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import plan_check_service


class PlanCheckServiceTests(unittest.TestCase):
    def test_auto_delete_only_for_explicit_invalid_account(self):
        with patch.object(plan_check_service.db, "delete_account", return_value=True) as delete:
            self.assertTrue(plan_check_service._auto_delete_invalid_account(
                account_id=7,
                email="dead@example.com",
                result={"account_validity": "invalid", "http_status": 401},
            ))
            delete.assert_called_once_with(acc_id=7)

    def test_proxy_policy_failure_is_not_deleted(self):
        with patch.object(plan_check_service.db, "delete_account", return_value=True) as delete:
            self.assertFalse(plan_check_service._auto_delete_invalid_account(
                account_id=7,
                email="blocked@example.com",
                result={"account_validity": "unknown_proxy_or_policy", "http_status": 403},
            ))
            delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
