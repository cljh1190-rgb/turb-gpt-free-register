import unittest
from unittest.mock import Mock, patch

from core.mail_archive_viewer import fetch_mail_archive, mask_mail_url, parse_mail_viewer_input


class MailArchiveViewerTests(unittest.TestCase):
    def test_parses_raw_and_prefixed_urls(self):
        parsed = parse_mail_viewer_input(
            "https://mail.example/show/token/a@icloud.com\n"
            "b@icloud.com----https://mail.example/show/token/b@icloud.com"
        )
        self.assertEqual(parsed["count"], 2)
        self.assertEqual(parsed["errors"], [])

    def test_masks_secret_path(self):
        masked = mask_mail_url("https://mail.example/show/secret-token/a@icloud.com?key=hidden")
        self.assertEqual(masked, "https://mail.example/…/a@icloud.com")

    @patch("core.mail_archive_viewer.requests.get")
    def test_reads_html_cards_and_marks_plus(self, get):
        html = """
        <html><body><h1>a@icloud.com</h1>
          <div class="card"><div class="fr">OpenAI</div><div class="su">Welcome</div><div class="dt">2026-08-02</div><div class="bd">Your ChatGPT Plus is active.</div></div>
          <div class="card"><div class="fr">OpenAI</div><div class="su">Security</div><div class="dt">2026-08-01</div><div class="bd">A login was detected.</div></div>
        </body></html>
        """
        response = Mock(status_code=200, text=html)
        response.json.side_effect = ValueError("not json")
        get.return_value = response

        result = fetch_mail_archive("https://mail.example/show/secret/a@icloud.com")

        self.assertTrue(result["ok"])
        self.assertEqual(result["message_count"], 2)
        self.assertEqual(result["plus_count"], 1)
        self.assertTrue(result["messages"][0]["has_plus"])
        self.assertNotIn("secret", result["url"])

    @patch("core.mail_archive_viewer.requests.get")
    def test_reports_empty_mailbox(self, get):
        response = Mock(status_code=200, text="<html><body><h1>empty@icloud.com</h1></body></html>")
        response.json.side_effect = ValueError("not json")
        get.return_value = response
        result = fetch_mail_archive("https://mail.example/show/token/empty@icloud.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["message_count"], 0)
        self.assertIn("未识别", result["error"])


if __name__ == "__main__":
    unittest.main()
