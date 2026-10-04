import base64
from datetime import datetime, timezone
import json
import unittest

from operations.paragon_login_probe import (
    ENV_NAMES, EXPIRES_AT, PROBE_ID, Totp, eligible, is_page,
    missing_credentials, parse_totp, safe_result,
)


class ParagonLoginProbeTests(unittest.TestCase):
    def test_rfc6238_sha1_vectors(self):
        totp = Totp(b"12345678901234567890", digits=8)
        for timestamp, expected in [(59, "94287082"), (1111111109, "07081804"),
                                    (1111111111, "14050471"), (1234567890, "89005924"),
                                    (2000000000, "69279037"), (20000000000, "65353130")]:
            self.assertEqual(totp.code(timestamp), expected)

    def test_configuration_uri_and_plain_key_agree(self):
        key = base64.b32encode(b"12345678901234567890").decode()
        plain = parse_totp(key.lower())
        uri = parse_totp("otpauth://totp/Paragon?secret=" + key + "&issuer=Paragon")
        self.assertEqual(plain.code(59), "287082")
        self.assertEqual(plain.code(59), uri.code(59))
        self.assertNotIn(key, repr(uri))

    def test_reject_wrong_types_and_duplicate_parameters(self):
        for value in ["not-a-key", "otpauth://hotp/x?secret=JBSWY3DPEHPK3PXP",
                      "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&secret=JBSWY3DPEHPK3PXP"]:
            with self.assertRaises(ValueError):
                parse_totp(value)

    def test_explicit_switch_and_expiry(self):
        now = datetime(2026, 10, 4, 10, tzinfo=timezone.utc)
        self.assertFalse(eligible({}, now))
        self.assertFalse(eligible({"PARAGON_LOGIN_PROBE_ID": "other"}, now))
        self.assertTrue(eligible({"PARAGON_LOGIN_PROBE_ID": PROBE_ID}, now))
        self.assertFalse(eligible({"PARAGON_LOGIN_PROBE_ID": PROBE_ID}, EXPIRES_AT))

    def test_reject_other_origins_and_lookalike_hosts(self):
        self.assertTrue(is_page("https://paragon.online/dashboard", "/dashboard"))
        for url in ["http://paragon.online/dashboard", "https://paragon.online.evil.test/dashboard",
                    "https://evil.test/dashboard", "https://paragon.online/login"]:
            self.assertFalse(is_page(url, "/dashboard"))

    def test_results_cannot_disclose_extra_fields(self):
        result = safe_result("failed", "password", password="SECRET", totp="123456",
                             missing=["PARAGON_PASSWORD", "SECRET"], password_accepted="SECRET")
        encoded = json.dumps(result)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("123456", encoded)
        self.assertEqual(result["missing"], ["PARAGON_PASSWORD"])
        self.assertFalse(result["password_accepted"])
        self.assertEqual(missing_credentials({}), list(ENV_NAMES))


if __name__ == "__main__":
    unittest.main()
