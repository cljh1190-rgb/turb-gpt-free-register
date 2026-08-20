# -*- coding: utf-8 -*-
import base64
import json
import unittest

from core.cpa_converter import build_cpa_auth_file, safe_cpa_filename
from config.codex import CODEX_CLIENT_ID


def _jwt(payload: dict) -> str:
    def part(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{part({'alg': 'none'})}.{part(payload)}.signature"


class CpaConverterTests(unittest.TestCase):
    def test_builds_refreshable_cpa_file(self):
        access = _jwt({"client_id": CODEX_CLIENT_ID, "exp": 1786470951})
        identity = _jwt({"aud": [CODEX_CLIENT_ID], "email": "plus@example.com"})
        auth, meta = build_cpa_auth_file({
            "email": "plus@example.com",
            "access_token": access,
            "oauth_id_token": identity,
            "oauth_refresh_token": "refresh",
            "oauth_client_id": CODEX_CLIENT_ID,
            "account_id": "account-1",
            "oauth_expires_at": 1786470951,
            "current_plan_type": "plus",
        })
        self.assertEqual(auth["type"], "codex")
        self.assertEqual(auth["refresh_token"], "refresh")
        self.assertTrue(meta["complete"])
        self.assertTrue(auth["expired"].endswith("Z"))

    def test_access_only_chatgpt_token_is_rejected(self):
        chatgpt_access = _jwt({"client_id": "app_chatgpt_web", "exp": 1786470951})
        with self.assertRaisesRegex(ValueError, "已拒绝生成不可用"):
            build_cpa_auth_file({"email": "x@example.com", "access_token": chatgpt_access})
        self.assertEqual(safe_cpa_filename("x@example.com", "plus"), "codex-x@example.com-plus.json")

    def test_normal_codex_file_is_used_as_one_complete_set(self):
        web_access = _jwt({"client_id": "app_chatgpt_web", "exp": 1786470951})
        codex_access = _jwt({"client_id": CODEX_CLIENT_ID, "exp": 1786470951})
        codex_id = _jwt({"aud": CODEX_CLIENT_ID, "email": "x@example.com"})
        auth, meta = build_cpa_auth_file(
            {"email": "x@example.com", "access_token": web_access, "current_plan_type": "plus"},
            fallback_credential={
                "type": "codex",
                "email": "x@example.com",
                "access_token": codex_access,
                "id_token": codex_id,
                "refresh_token": "rt.valid",
                "account_id": "account-1",
                "expired": "2026-08-10T00:00:00Z",
            },
        )
        self.assertEqual(auth["access_token"], codex_access)
        self.assertNotEqual(auth["access_token"], web_access)
        self.assertEqual(auth["refresh_token"], "rt.valid")
        self.assertEqual(meta["credential_source"], "正常 Codex 授权文件")

    def test_wrong_client_id_is_rejected_even_with_three_tokens(self):
        access = _jwt({"client_id": "wrong-client", "exp": 1786470951})
        identity = _jwt({"aud": ["wrong-client"], "email": "x@example.com"})
        with self.assertRaisesRegex(ValueError, "不是 Codex OAuth Token"):
            build_cpa_auth_file({
                "email": "x@example.com",
                "access_token": access,
                "oauth_id_token": identity,
                "oauth_refresh_token": "refresh",
                "oauth_client_id": "wrong-client",
            })


if __name__ == "__main__":
    unittest.main()
