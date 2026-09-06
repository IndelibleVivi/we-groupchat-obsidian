import base64
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from core import wechat_signing_identity as identity


FIXTURE_PEM = """-----BEGIN CERTIFICATE-----
MIIC6jCCAdKgAwIBAgIJAPRTSa4spWOwMA0GCSqGSIb3DQEBCwUAMBkxFzAVBgNV
BAMMDldHTyBTbW9rZSBUZXN0MB4XDTI2MDkwNjAxNTQ1M1oXDTM2MDkwMzAxNTQ1
M1owGTEXMBUGA1UEAwwOV0dPIFNtb2tlIFRlc3QwggEiMA0GCSqGSIb3DQEBAQUA
A4IBDwAwggEKAoIBAQDVnEOxmm7i9Yq2c3aFtIzkWWTmkS2YsVZigZwtU+NeeiJH
hfJkxvCu+kcHVnApI7dPYMHY38vJcANEkjycKXcMzxyAIQvVlC2QUhLnBR3KYEq5
Rs8kc+zEAazjPRibhkkgCa77pgU3TOt5PgkGcIHS0QLSfTFWorta0XdoJFXZeKV8
YPo2xJLaNXS1LEAc9GL5NEHgT3+61lEAVBhwhAsdoCge9lh+rNFfLVAiVXIH3fOm
dcCjxShnnL5RpdFRLpGkM2vL2n6TmPtAHd+Pld7Y0G+fJ44uzRppVf6ExWFzA9II
kQTpYabsq044n7m//8Vt6Pycm+YwEY+57hFoOpQJAgMBAAGjNTAzMAwGA1UdEwEB
/wQCMAAwDgYDVR0PAQH/BAQDAgeAMBMGA1UdJQQMMAoGCCsGAQUFBwMDMA0GCSqG
SIb3DQEBCwUAA4IBAQBXqdq9CByBCDJzmAc+eMGi+vbIFuFvs1pbWReMmHf/SRSi
tRY/WykE4JPe8HqmyTtHfmfgo3/ndwwYt13yX6/cFV5bWM6ch7hxPd0WtVOc+m/f
WQf0PlLaK2WALpnekeLOtpUvquBcuXvCjupH8ldgDV8pa0tcj2iIXOfkcQKOSc4X
Efc4Yeq1HmaE8MJLHcCNertzfssQutDy1I2BU9VZ+hfhReP1B6u3T6n9mcUuJ31J
HPGYDIX6ADzCxkVOiT9m0XnyvhQc373zT1DF2s+hWcwS8AY4gN3LQiTsOaqYPtkT
NoDNNiYfsDDo8ehkSF6yWr/WrnpetzkI9A00mYHS
-----END CERTIFICATE-----"""


def fixture_root() -> str:
    der = base64.b64decode("".join(FIXTURE_PEM.splitlines()[1:-1]))
    return hashlib.sha1(der).hexdigest()


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class ManagedCertificateRootTests(unittest.TestCase):
    def test_pem_is_hashed_as_sha1_of_der(self):
        def runner(args, **_kwargs):
            return completed(args, stdout=FIXTURE_PEM)

        root = identity.managed_certificate_root_hash(
            runner=runner, keychain_path="/fixture/wgo.keychain-db"
        )
        self.assertEqual(root, fixture_root())

    def test_missing_certificate_returns_none(self):
        def runner(args, **_kwargs):
            return completed(args, returncode=44)

        self.assertIsNone(
            identity.managed_certificate_root_hash(
                runner=runner, keychain_path="/fixture/wgo.keychain-db"
            )
        )

    def test_runner_failure_returns_none_without_exception_text(self):
        def runner(args, **_kwargs):
            raise FileNotFoundError("PRIVATE_SENTINEL")

        self.assertIsNone(
            identity.managed_certificate_root_hash(
                runner=runner, keychain_path="/fixture/wgo.keychain-db"
            )
        )


class EnsureStableIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.keychain = identity.identity_keychain_path(self.home)
        self.password_path = identity.identity_password_path(self.home)

    def make_runner(self, *, openssl_version="LibreSSL 3.3.6", record=None):
        calls = []
        state = {"find_count": 0}

        def runner(args, **_kwargs):
            calls.append(list(args))
            if record is not None:
                record(args)
            if args[:2] == ["security", "find-certificate"]:
                state["find_count"] += 1
                if state["find_count"] == 1:
                    return completed(args, returncode=44)
                return completed(args, stdout=FIXTURE_PEM)
            if args[0].endswith("openssl") and args[1] == "version":
                return completed(args, stdout=f"{openssl_version} smoke\n")
            if args[:2] == ["security", "create-keychain"]:
                self.keychain.parent.mkdir(parents=True, exist_ok=True)
                self.keychain.touch()
                return completed(args)
            if args[:2] == ["security", "list-keychains"] and "-s" not in args:
                return completed(args, stdout='    "/fixture/login.keychain-db"\n')
            return completed(args)

        return runner, calls

    def test_existing_identity_is_reused_without_mutation(self):
        def runner(args, **_kwargs):
            return completed(args, stdout=FIXTURE_PEM)

        result = identity.ensure_stable_signing_identity(
            runner=runner, home=self.home
        )
        self.assertEqual(result.common_name, identity.IDENTITY_COMMON_NAME)
        self.assertEqual(result.keychain_path, str(self.keychain))
        self.assertEqual(result.certificate_root, fixture_root())

    def test_missing_identity_creates_keychain_certificate_and_partition_list(self):
        captured = {}

        def record(args):
            if "-config" in args:
                captured["config"] = Path(
                    args[args.index("-config") + 1]
                ).read_text(encoding="ascii")
            if args[0].endswith("openssl") and args[1] == "pkcs12":
                captured["workspace"] = Path(args[args.index("-out") + 1]).parent

        runner, calls = self.make_runner(record=record)
        result = identity.ensure_stable_signing_identity(
            runner=runner, home=self.home
        )

        verbs = [(call[0], call[1] if len(call) > 1 else "") for call in calls]
        self.assertEqual(verbs[0], ("security", "find-certificate"))
        self.assertIn("req", calls[1])
        self.assertIn("pkcs12", calls[3])
        self.assertNotIn("-legacy", calls[3])
        self.assertEqual(calls[4][:2], ["security", "create-keychain"])
        self.assertEqual(calls[5][:2], ["security", "import"])
        self.assertIn("-T", calls[5])
        self.assertEqual(
            calls[5][calls[5].index("-T") + 1], "/usr/bin/codesign"
        )
        self.assertEqual(calls[6][:2], ["security", "unlock-keychain"])
        self.assertEqual(calls[7][:2], ["security", "set-key-partition-list"])
        self.assertIn("apple-tool:,apple:,codesign:", calls[7])
        self.assertEqual(calls[-1][:2], ["security", "find-certificate"])
        self.assertEqual(result.certificate_root, fixture_root())

        self.assertIn("extendedKeyUsage = codeSigning", captured["config"])
        self.assertFalse(captured["workspace"].exists())

        mode = self.password_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        password = self.password_path.read_text(encoding="ascii")
        self.assertEqual(len(password), 48)
        self.assertIn(password, calls[4])

    def test_openssl3_pkcs12_export_uses_legacy_flag(self):
        runner, calls = self.make_runner(openssl_version="OpenSSL 3.6.3")
        identity.ensure_stable_signing_identity(runner=runner, home=self.home)
        export = next(call for call in calls if "pkcs12" in call)
        self.assertEqual(export[-1], "-legacy")

    def test_existing_keychain_without_password_fails_closed(self):
        self.keychain.parent.mkdir(parents=True, exist_ok=True)
        self.keychain.touch()

        def runner(args, **_kwargs):
            return completed(args, returncode=44)

        with self.assertRaisesRegex(
            identity.WeChatSigningIdentityError,
            "wechat_signing_identity_password_missing",
        ):
            identity.ensure_stable_signing_identity(
                runner=runner, home=self.home
            )

    def test_symlink_password_file_is_rejected(self):
        self.password_path.parent.mkdir(parents=True, exist_ok=True)
        target = self.home / "elsewhere"
        target.write_text("x" * 48, encoding="ascii")
        self.password_path.symlink_to(target)

        def runner(args, **_kwargs):
            return completed(args, returncode=44)

        with self.assertRaisesRegex(
            identity.WeChatSigningIdentityError,
            "wechat_signing_identity_password_invalid",
        ):
            identity.ensure_stable_signing_identity(
                runner=runner, home=self.home
            )

    def test_failed_keychain_creation_is_bounded(self):
        def runner(args, **_kwargs):
            if args[:2] == ["security", "find-certificate"]:
                return completed(args, returncode=44)
            if args[:2] == ["security", "create-keychain"]:
                return completed(args, returncode=1)
            return completed(args, stdout="LibreSSL 3.3.6\n")

        with self.assertRaisesRegex(
            identity.WeChatSigningIdentityError,
            "wechat_signing_identity_unavailable",
        ):
            identity.ensure_stable_signing_identity(
                runner=runner, home=self.home
            )


class UnlockKeychainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.keychain = identity.identity_keychain_path(self.home)
        self.password_path = identity.identity_password_path(self.home)
        self.managed = identity.WeChatSigningIdentity(
            identity.IDENTITY_COMMON_NAME, str(self.keychain), fixture_root()
        )

    def test_unlock_uses_stored_password(self):
        self.password_path.parent.mkdir(parents=True, exist_ok=True)
        self.password_path.write_text("p" * 48, encoding="ascii")
        calls = []

        def runner(args, **_kwargs):
            calls.append(list(args))
            return completed(args)

        identity.unlock_signing_keychain(self.managed, runner=runner, home=self.home)
        self.assertEqual(
            calls,
            [["security", "unlock-keychain", "-p", "p" * 48, str(self.keychain)]],
        )

    def test_missing_password_never_creates_a_new_one(self):
        def runner(args, **_kwargs):
            return completed(args)

        with self.assertRaisesRegex(
            identity.WeChatSigningIdentityError,
            "wechat_signing_identity_password_missing",
        ):
            identity.unlock_signing_keychain(
                self.managed, runner=runner, home=self.home
            )
        self.assertFalse(self.password_path.exists())


if __name__ == "__main__":
    unittest.main()
