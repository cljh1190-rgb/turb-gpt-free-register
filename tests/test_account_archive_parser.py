import io
import json
import unittest
import zipfile

from core.account_archive_parser import AccountArchiveError, normalize_plus_archive
from core.account_import_parser import parse_account_import_text


def _zip_bytes(entries: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


class AccountArchiveParserTests(unittest.TestCase):
    def test_prefers_sub2_json_and_ignores_paired_txt(self):
        token = "eyJheader.eyJpayload." + "x" * 32
        sub2 = {
            "exported_at": "2026-08-02T00:00:00Z",
            "accounts": [{
                "name": "user@example.com",
                "credentials": {"email": "user@example.com", "access_token": token},
            }],
        }
        raw = _zip_bytes({
            "user@example.com.json": json.dumps(sub2),
            "user@example.com.txt": "user@example.com----password----client-id----M.C541.MsaArtifacts.token",
        })

        normalized = normalize_plus_archive(raw)
        parsed = parse_account_import_text(normalized["text"])

        self.assertEqual(normalized["processed_files"], 1)
        self.assertEqual(normalized["ignored_text_files"], 1)
        self.assertEqual(len(parsed["records"]), 1)
        self.assertEqual(parsed["records"][0]["email"], "user@example.com")

    def test_reads_text_when_archive_has_no_json(self):
        opaque = "x" * 100
        raw = _zip_bytes({"accounts.txt": f"user@example.com----{opaque}"})
        normalized = normalize_plus_archive(raw)
        parsed = parse_account_import_text(normalized["text"])
        self.assertEqual(normalized["selected_type"], "text")
        self.assertEqual(len(parsed["records"]), 1)

    def test_rejects_invalid_zip(self):
        with self.assertRaises(AccountArchiveError):
            normalize_plus_archive(b"not-a-zip")


if __name__ == "__main__":
    unittest.main()
