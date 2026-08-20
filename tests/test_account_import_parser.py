# -*- coding: utf-8 -*-
import base64
import json
import unittest

from core.account_import_parser import parse_account_import_text


def _b64(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode("utf-8")).decode("ascii").rstrip("=")


def _token(email: str = "user@example.com", plan: str | None = "free") -> str:
    auth = {
        "chatgpt_user_id": "user-1",
        "chatgpt_account_id": "account-1",
    }
    if plan is not None:
        auth["chatgpt_plan_type"] = plan
    payload = {
        "https://api.openai.com/profile": {"email": email, "name": "Test User"},
        "https://api.openai.com/auth": auth,
    }
    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(payload)}.signature123"


class AccountImportParserTests(unittest.TestCase):
    def test_pure_jwt_derives_email_from_claims(self):
        result = parse_account_import_text(_token())
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["email"], "user@example.com")
        self.assertFalse(result["records"][0]["synthetic_email"])

    def test_full_export_line_finds_chatgpt_token(self):
        token = _token("line@example.com")
        text = f"line@example.com----password----client-id----microsoft-refresh-token----{token}----TOTPSECRET"
        result = parse_account_import_text(text)
        self.assertEqual([row["access_token"] for row in result["records"]], [token])
        self.assertEqual(result["records"][0]["email"], "line@example.com")

    def test_multiline_json_and_nested_access_token(self):
        token1 = _token("one@example.com")
        token2 = _token("two@example.com")
        text = json.dumps({"accounts": [
            {"email": "one@example.com", "access_token": token1},
            {"profile": {"email": "two@example.com"}, "tokens": {"accessToken": token2}},
        ]})
        result = parse_account_import_text(text)
        self.assertEqual({row["email"] for row in result["records"]}, {"one@example.com", "two@example.com"})

    def test_opaque_token_with_common_delimiter(self):
        opaque = "x" * 100
        result = parse_account_import_text(f"opaque@example.com|{opaque}")
        self.assertEqual(result["records"][0]["email"], "opaque@example.com")
        self.assertEqual(result["records"][0]["access_token"], opaque)

    def test_json_array_of_tokens(self):
        token1 = _token("array-one@example.com")
        token2 = _token("array-two@example.com")
        result = parse_account_import_text(json.dumps([token1, token2]))
        self.assertEqual(len(result["records"]), 2)

    def test_sub2api_export_uses_access_token_only(self):
        access_token = _token("sub2@example.com", plan=None)
        id_token = _token("wrong-id-token@example.com")
        text = json.dumps({
            "type": "sub2api-data",
            "accounts": [{
                "name": "sub2@example.com----mail-password",
                "credentials": {
                    "access_token": access_token,
                    "id_token": id_token,
                    "refresh_token": "rt." + "x" * 120,
                    "email": "sub2@example.com",
                    "plan_type": "plus",
                },
            }],
        })
        result = parse_account_import_text(text)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["access_token"], access_token)
        self.assertEqual(result["records"][0]["plan_type"], "plus")
        self.assertEqual(result["records"][0]["oauth_id_token"], id_token)
        self.assertTrue(result["records"][0]["oauth_refresh_token"].startswith("rt."))

    def test_mixed_input_reports_bad_lines_and_deduplicates(self):
        token = _token("mix@example.com")
        result = parse_account_import_text(f"{token}\nnot-an-account\nBearer {token}")
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["errors"][0]["line_no"], 2)

    def test_rejects_malformed_multi_segment_jwt(self):
        malformed = "eyJheader..part." + "x" * 120 + ".tail"
        result = parse_account_import_text(malformed)
        self.assertEqual(result["records"], [])
        self.assertEqual(result["errors"][0]["line_no"], 1)

    def test_rejects_microsoft_msa_refresh_token_with_precise_reason(self):
        msa_token = "M.C541_BAY.0.U.MsaArtifacts.-Cg!" + "x" * 120
        text = f"mail@example.com----password----client-id----{msa_token}"
        result = parse_account_import_text(text)
        self.assertEqual(result["records"], [])
        self.assertIn("Microsoft MSA Refresh Token", result["errors"][0]["reason"])
        self.assertIn("credentials.access_token", result["errors"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
