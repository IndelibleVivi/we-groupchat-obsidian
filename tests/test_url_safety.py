import hashlib
import unittest

from core.resource_capture import LINK_ID_DOMAIN, _url_sha256
from core.url_safety import (
    INVALID_URL_VALUE,
    redact_url_for_display,
    redact_urls_in_text,
    redact_urls_with_offsets,
)


class URLSafetyTests(unittest.TestCase):
    def test_offsets_keep_exact_surroundings_and_whole_url_replacements(self):
        raw = "长" * 55000 + " https://example.test/?token=private " + "尾" * 7000
        visible, ranges = redact_urls_with_offsets(raw)
        self.assertEqual(visible, raw.replace("token=private", "token=REDACTED"))
        self.assertEqual(visible, redact_urls_in_text(raw))
        self.assertEqual(len(ranges), 3)
        self.assertEqual(ranges[1][0], "replace")
        for tag, a, b, x, y in ranges:
            if tag == "equal":
                self.assertEqual(raw[a:b], visible[x:y])
            else:
                self.assertEqual(redact_url_for_display(raw[a:b]), visible[x:y])
        self.assertEqual(ranges[0][1:3], (0, ranges[1][1]))
        self.assertEqual(ranges[-1][2], len(raw))
        self.assertEqual(ranges[-1][4], len(visible))

    def test_offset_identity_and_legacy_substitution_are_exact(self):
        from core.url_safety import URL_RE
        for raw in ("", "猫" * 55000, "https://example.test/a?view=1",
                    "start https://user:p@example.test/a?token=s then https://bad／url end"):
            with self.subTest(length=len(raw)):
                visible, ranges = redact_urls_with_offsets(raw)
                self.assertEqual(visible, URL_RE.sub(lambda m: redact_url_for_display(m.group(0)), raw))
                if "http" not in raw and raw:
                    self.assertEqual(ranges, [("equal", 0, len(raw), 0, len(raw))])

    def test_benign_query_and_fragment_remain_exact(self):
        url = "https://example.com/report?view=full&lang=zh#section-2"

        self.assertEqual(redact_url_for_display(url), url)

    def test_aws_and_gcs_signed_credentials_are_redacted(self):
        aws = (
            "https://bucket.example.com/object?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=AKIA-SECRET%2F20260830%2Fregion%2Fs3%2Faws4_request"
            "&X-Amz-Security-Token=sts-secret&X-Amz-Signature=aws-secret"
            "&download=1"
        )
        gcs = (
            "https://storage.example.com/object?X-Goog-Credential=gcs-secret"
            "&X-Goog-Signature=gcs-signature&response-content-type=text%2Fplain"
        )

        aws_display = redact_url_for_display(aws)
        gcs_display = redact_url_for_display(gcs)

        for secret in ("AKIA-SECRET", "sts-secret", "aws-secret"):
            self.assertNotIn(secret, aws_display)
        self.assertIn("X-Amz-Algorithm=AWS4-HMAC-SHA256", aws_display)
        self.assertIn("download=1", aws_display)
        for secret in ("gcs-secret", "gcs-signature"):
            self.assertNotIn(secret, gcs_display)
        self.assertIn("response-content-type=text%2Fplain", gcs_display)

    def test_azure_sas_fields_are_redacted_as_one_credential(self):
        url = (
            "https://account.blob.example.com/container/file?sv=2026-01-01"
            "&st=2026-08-30T00%3A00Z&se=2026-08-31T00%3A00Z&sp=rw"
            "&sr=b&sig=azure-secret&filename=report.pdf"
        )

        display = redact_url_for_display(url)

        for value in (
            "2026-01-01",
            "2026-08-30T00",
            "2026-08-31T00",
            "azure-secret",
            "sp=rw",
            "sr=b",
        ):
            self.assertNotIn(value, display)
        self.assertIn("filename=report.pdf", display)
        self.assertGreaterEqual(display.count("REDACTED"), 6)

    def test_fragment_credentials_and_route_fragments_are_redacted(self):
        oauth = "https://example.com/callback#access_token=fragment-secret&state=visible"
        routed = (
            "https://example.com/app#/callback?authorization=Bearer-secret"
            "&tab=settings"
        )

        oauth_display = redact_url_for_display(oauth)
        routed_display = redact_url_for_display(routed)

        self.assertNotIn("fragment-secret", oauth_display)
        self.assertIn("state=visible", oauth_display)
        self.assertNotIn("Bearer-secret", routed_display)
        self.assertIn("#/callback?", routed_display)
        self.assertIn("tab=settings", routed_display)

    def test_userinfo_and_malformed_urls_fail_closed(self):
        self.assertEqual(
            redact_url_for_display("https://user:password@example.com/path"),
            "https://REDACTED@example.com/path",
        )
        self.assertEqual(
            redact_url_for_display("https://example.com／evil?token=secret"),
            INVALID_URL_VALUE,
        )

    def test_embedded_url_redaction_never_changes_surrounding_text(self):
        text = (
            "open https://example.com/a?token=text-secret&view=1 then continue"
        )

        display = redact_urls_in_text(text)

        self.assertNotIn("text-secret", display)
        self.assertIn("open https://example.com/a?token=REDACTED&view=1 then continue", display)

    def test_exact_url_identity_hash_remains_bound_to_unredacted_value(self):
        exact = "https://example.com/object?token=identity-secret&view=1"
        expected = hashlib.sha256(LINK_ID_DOMAIN + exact.encode("utf-8")).hexdigest()

        self.assertEqual(_url_sha256(exact), expected)
        self.assertNotEqual(
            _url_sha256(exact),
            _url_sha256(redact_url_for_display(exact)),
        )


if __name__ == "__main__":
    unittest.main()
