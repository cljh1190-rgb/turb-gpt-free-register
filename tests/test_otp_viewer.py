from unittest import TestCase
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from core.generic_api_mail_client import (
    _extract_code,
    _message_received_after,
    fetch_latest_otp_from_url,
    mask_code_url,
    parse_generic_api_line,
    parse_otp_viewer_text,
)


class OtpViewerTests(TestCase):
    def test_parse_supported_separators(self):
        parsed = parse_otp_viewer_text(
            "a@example.com----https://mail.example/messages/secret/a@example.com\n"
            "b@example.com====http://mail.example/code/b@example.com"
        )
        self.assertEqual(parsed["count"], 2)
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["records"][1]["email"], "b@example.com")

    def test_parse_rejects_invalid_lines(self):
        parsed = parse_otp_viewer_text("bad line\na@example.com----ftp://example.com/code")
        self.assertEqual(parsed["count"], 0)
        self.assertEqual(len(parsed["errors"]), 2)

    def test_parse_password_bearing_generic_api_line(self):
        line = (
            "sample@example.test----fixture-password----"
            "http://mail.example.test/api/getcode?email=sample@example.test"
        )
        parsed = parse_generic_api_line(line)
        self.assertEqual(parsed["email"], "sample@example.test")
        self.assertEqual(parsed["password"], "fixture-password")
        self.assertEqual(
            parsed["code_url"],
            "http://mail.example.test/api/getcode?email=sample@example.test",
        )
        self.assertEqual(parse_otp_viewer_text(line)["count"], 1)

    def test_parse_012e_token_url_without_changing_query(self):
        code_url = (
            "http://mail.012e.com/api/getcode.php?"
            "token=dGVzdEAwMTJlLmNvbS0tLS1wYXNz%3D%3D"
        )
        line = f"test@012e.com----pass---{code_url}"

        parsed = parse_generic_api_line(line)

        self.assertEqual(parsed["email"], "test@012e.com")
        self.assertEqual(parsed["password"], "pass")
        self.assertEqual(parsed["code_url"], code_url)
        self.assertEqual(parsed["extra_parts"], [])

    def test_parse_legacy_generic_api_extras(self):
        parsed = parse_generic_api_line(
            "a@example.com====https://mail.example/code====token====totp"
        )
        self.assertEqual(parsed["password"], "")
        self.assertEqual(parsed["code_url"], "https://mail.example/code")
        self.assertEqual(parsed["extra_parts"], ["token", "totp"])

    def test_extracts_otp_from_json_html_and_plain_text(self):
        self.assertEqual(_extract_code('{"message":"Your verification code is 123456"}'), "123456")
        self.assertEqual(_extract_code("<strong>Verification code: 234567</strong>"), "234567")
        self.assertEqual(_extract_code("Use 345678 to continue"), "345678")
        self.assertIsNone(_extract_code("<style>:root{--splash:#111827}</style><div id='root'></div>"))
        self.assertIsNone(_extract_code("暂无新邮件"))

    @patch("core.generic_api_mail_client.requests.get")
    def test_fetch_extracts_012e_plain_text_code(self, get):
        get.return_value = Mock(status_code=200, text="验证码：654321")
        code_url = "http://mail.012e.com/api/getcode.php?token=opaque%3D%3D"

        result = fetch_latest_otp_from_url("test@012e.com", code_url)

        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "654321")
        self.assertEqual(get.call_args.args[0], code_url)

    @patch("core.generic_api_mail_client.requests.get")
    def test_fetch_returns_code_without_full_secret_url(self, get):
        response = Mock(status_code=200, text='{"code":"456789"}')
        get.return_value = response
        secret_url = "https://mail.example/messages/top-secret-token/a@example.com?key=hidden"

        result = fetch_latest_otp_from_url("a@example.com", secret_url)

        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "456789")
        self.assertNotIn("top-secret-token", result["url"])
        self.assertNotIn("key=hidden", result["url"])
        get.assert_called_once()

    @patch("core.generic_api_mail_client.requests.get")
    def test_fetch_reports_http_and_missing_code(self, get):
        get.return_value = Mock(status_code=401, text="unauthorized")
        denied = fetch_latest_otp_from_url("a@example.com", "https://mail.example/code/a@example.com")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"], "HTTP 401")

        get.return_value = Mock(status_code=200, text="no message yet")
        missing = fetch_latest_otp_from_url("a@example.com", "https://mail.example/code/a@example.com")
        self.assertFalse(missing["ok"])
        self.assertIn("未识别", missing["error"])

    @patch("core.generic_api_mail_client.requests.get")
    def test_email2_viewer_is_resolved_to_json_api(self, get):
        response = Mock(
            status_code=200,
            text=(
                '{"success":true,"email":{"id":"6635965",'
                '"subject":"Your temporary ChatGPT verification code",'
                '"text_body":"Your verification code is 798057",'
                '"received_at":"2026-08-18 01:30:00"}}'
            ),
        )
        get.return_value = response
        viewer_url = "http://email2.ymb1668.com/client/mailbox?address=a%40example.com"

        result = fetch_latest_otp_from_url("a@example.com", viewer_url)

        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "798057")
        request_url = get.call_args.args[0]
        self.assertEqual(
            request_url,
            "https://email2.api.ymb1668.com/api/v2/public/mailbox/latest?address=a%40example.com",
        )
        self.assertEqual(get.call_args.kwargs["headers"]["Cache-Control"], "no-cache")

    @patch("core.generic_api_mail_client.requests.get")
    def test_email2_spa_demo_number_is_not_used(self, get):
        get.return_value = Mock(
            status_code=200,
            text='<!doctype html><html><body><div id="root">111827</div></body></html>',
        )

        result = fetch_latest_otp_from_url(
            "a@example.com",
            "https://email2.ymb1668.com/client/mailbox?address=a%40example.com",
        )

        self.assertFalse(result["ok"])
        self.assertIsNone(result["code"])
        self.assertIn("未识别", result["error"])

    def test_old_mail_is_rejected_when_polling_after_timestamp(self):
        after_ts = datetime.now().timestamp()
        old = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertFalse(
            _message_received_after(
                '{"email":{"received_at":"%s","text_body":"code 111111"}}' % old,
                after_ts,
            )
        )
        self.assertTrue(
            _message_received_after(
                '{"email":{"received_at":"%s","text_body":"code 222222"}}' % fresh,
                after_ts,
            )
        )

    def test_mask_url_hides_middle_path_and_query(self):
        masked = mask_code_url("https://mail.example/messages/secret-token/a@example.com?key=hidden")
        self.assertEqual(masked, "https://mail.example/…/a@example.com")
