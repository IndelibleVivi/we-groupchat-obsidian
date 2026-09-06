import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core import key_extractor
from core import wechat_resign
from core.wechat_signing_identity import WeChatSigningIdentity
from tests.paths import repo_path


class WeChatResignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name) / "Selected WeChat.app"
        contents = self.app / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        self.executable = contents / "MacOS" / "WeChat"
        self.executable.write_bytes(b"synthetic executable")
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.tencent.xinWeChat",
            "CFBundleExecutable": "WeChat",
            "CFBundleShortVersionString": "4.1.11",
            "CFBundleVersion": "269136",
        }))
        self.bundle = wechat_resign.inspect_wechat_bundle(self.app)
        self.running = key_extractor.ScanTarget(
            42,
            self.bundle.app_path,
            self.bundle.executable_path,
            100.0,
            (self.bundle.version, self.bundle.build, "arm64"),
            self.bundle.executable_identity,
        )
        self.target = wechat_resign.WeChatResignTarget(self.bundle, self.running)
        self.identity = WeChatSigningIdentity(
            "WGO WeChat Stable Identity",
            "/fixture/wgo-wechat-identity.keychain-db",
            "0123abcdef",
        )

    def _identity_patches(self):
        return (
            patch.object(
                wechat_resign,
                "ensure_stable_signing_identity",
                return_value=self.identity,
            ),
            patch.object(wechat_resign, "unlock_signing_keychain"),
        )

    @staticmethod
    def _sign_call(calls):
        return next(call for call in calls if "--sign" in call)

    def test_ambiguous_running_copies_are_rejected_before_mutation(self):
        other = key_extractor.ScanTarget(
            43,
            "/fixture/Other WeChat.app",
            "/fixture/Other WeChat.app/Contents/MacOS/WeChat",
            101.0,
            ("4.1.11", "269136", "arm64"),
            (2, 3, 4, 5),
        )
        with patch.object(
            key_extractor,
            "get_wechat_scan_targets",
            return_value=(self.running, other),
        ):
            with self.assertRaisesRegex(
                wechat_resign.WeChatResignError,
                "wechat_target_ambiguous",
            ):
                wechat_resign.resolve_wechat_resign_target(
                    self.app,
                    explicit_target=False,
                )

    def test_explicit_path_selects_only_that_running_copy(self):
        other = key_extractor.ScanTarget(
            43,
            "/fixture/Other WeChat.app",
            "/fixture/Other WeChat.app/Contents/MacOS/WeChat",
            101.0,
            ("4.1.11", "269136", "arm64"),
            (2, 3, 4, 5),
        )
        with patch.object(
            key_extractor,
            "get_wechat_scan_targets",
            return_value=(self.running, other),
        ):
            target = wechat_resign.resolve_wechat_resign_target(
                self.app,
                explicit_target=True,
            )
        self.assertEqual(target.running, self.running)

    def test_target_change_after_identity_check_stops_before_signing(self):
        calls = []

        def runner(args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        ensure, unlock = self._identity_patches()
        with (
            ensure,
            unlock,
            patch.object(wechat_resign, "resolve_wechat_resign_target", return_value=self.target),
            patch.object(
                wechat_resign,
                "revalidate_wechat_resign_target",
                side_effect=wechat_resign.WeChatResignError("wechat_target_changed"),
            ),
        ):
            with self.assertRaisesRegex(
                wechat_resign.WeChatResignError,
                "wechat_target_changed",
            ):
                wechat_resign.resign_wechat_bundle(
                    self.app,
                    allow=True,
                    runner=runner,
                )

        self.assertEqual(calls, [])

    def test_missing_explicit_authorization_performs_no_discovery_or_mutation(self):
        with (
            patch.object(wechat_resign, "resolve_wechat_resign_target") as resolve,
            patch.object(wechat_resign, "ensure_stable_signing_identity") as ensure,
            patch.object(wechat_resign.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(
                wechat_resign.WeChatResignError,
                "wechat_resign_not_authorized",
            ):
                wechat_resign.resign_wechat_bundle(self.app, allow=False)

        resolve.assert_not_called()
        ensure.assert_not_called()
        run.assert_not_called()

    def test_termination_timeout_never_calls_codesign(self):
        calls = []

        def runner(args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        ensure, unlock = self._identity_patches()
        with (
            ensure,
            unlock,
            patch.object(wechat_resign, "resolve_wechat_resign_target", return_value=self.target),
            patch.object(wechat_resign, "revalidate_wechat_resign_target", return_value=self.target),
            patch.object(
                wechat_resign,
                "terminate_wechat_target",
                side_effect=wechat_resign.WeChatResignError("wechat_target_quit_timeout"),
            ),
        ):
            with self.assertRaisesRegex(
                wechat_resign.WeChatResignError,
                "wechat_target_quit_timeout",
            ):
                wechat_resign.resign_wechat_bundle(
                    self.app,
                    allow=True,
                    runner=runner,
                )

        self.assertFalse(any("codesign" in call for call in calls))

    def test_sign_success_without_independent_verification_does_not_reopen(self):
        calls = []

        def runner(args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        ensure, unlock = self._identity_patches()
        with (
            ensure,
            unlock,
            patch.object(wechat_resign, "resolve_wechat_resign_target", return_value=self.target),
            patch.object(wechat_resign, "revalidate_wechat_resign_target", return_value=self.target),
            patch.object(wechat_resign, "terminate_wechat_target"),
            patch.object(
                wechat_resign,
                "verify_resigned_wechat_bundle",
                side_effect=wechat_resign.WeChatResignError("wechat_signature_invalid"),
            ),
            patch.object(wechat_resign, "reopen_wechat_target") as reopen,
        ):
            with self.assertRaisesRegex(
                wechat_resign.WeChatResignError,
                "wechat_signature_invalid",
            ):
                wechat_resign.resign_wechat_bundle(
                    self.app,
                    allow=True,
                    runner=runner,
                )

        self.assertEqual(self._sign_call(calls)[-1], self.bundle.app_path)
        reopen.assert_not_called()

    def test_verification_accepts_native_codedirectory_adhoc_output(self):
        results = iter((
            subprocess.CompletedProcess(["codesign", "--verify"], 0, "", ""),
            subprocess.CompletedProcess(
                ["codesign", "--display"],
                0,
                "",
                "Executable=/fixture/WeChat\n"
                "CodeDirectory v=20400 size=755 flags=0x2(adhoc) hashes=14+2 location=embedded\n"
                "Signature=adhoc\n",
            ),
        ))

        with patch.object(
            wechat_resign,
            "inspect_wechat_bundle",
            return_value=self.bundle,
        ):
            observed = wechat_resign.verify_resigned_wechat_bundle(
                self.bundle,
                runner=lambda *_args, **_kwargs: next(results),
            )

        self.assertEqual(observed, self.bundle)

    def test_verification_rejects_certificate_foreign_to_expected_root(self):
        results = iter((
            subprocess.CompletedProcess(["codesign", "--verify"], 0, "", ""),
            subprocess.CompletedProcess(
                ["codesign", "--display"],
                0,
                "",
                "CodeDirectory v=20500 size=755 flags=0x0(none) hashes=14+2 location=embedded\n"
                "Signature size=8979\n",
            ),
            subprocess.CompletedProcess(
                ["codesign", "--display", "-r-"],
                0,
                "",
                'designated => identifier "com.tencent.xinWeChat" and anchor apple generic\n',
            ),
        ))

        with self.assertRaisesRegex(
            wechat_resign.WeChatResignError,
            "wechat_signature_not_stable_identity",
        ):
            wechat_resign.verify_resigned_wechat_bundle(
                self.bundle,
                runner=lambda *_args, **_kwargs: next(results),
                expected_certificate_root=self.identity.certificate_root,
            )

    def test_verification_rejects_adhoc_when_stable_identity_is_required(self):
        results = iter((
            subprocess.CompletedProcess(["codesign", "--verify"], 0, "", ""),
            subprocess.CompletedProcess(
                ["codesign", "--display"],
                0,
                "",
                "CodeDirectory v=20400 size=755 flags=0x2(adhoc) hashes=14+2 location=embedded\n"
                "Signature=adhoc\n",
            ),
        ))

        with self.assertRaisesRegex(
            wechat_resign.WeChatResignError,
            "wechat_signature_not_stable_identity",
        ):
            wechat_resign.verify_resigned_wechat_bundle(
                self.bundle,
                runner=lambda *_args, **_kwargs: next(results),
                expected_certificate_root=self.identity.certificate_root,
            )

    def test_verification_rejects_runtime_flag_even_when_adhoc(self):
        results = iter((
            subprocess.CompletedProcess(["codesign", "--verify"], 0, "", ""),
            subprocess.CompletedProcess(
                ["codesign", "--display"],
                0,
                "",
                "CodeDirectory v=20500 size=755 flags=0x10002(adhoc,runtime) hashes=14+2 location=embedded\n"
                "Signature=adhoc\n",
            ),
        ))

        with self.assertRaisesRegex(
            wechat_resign.WeChatResignError,
            "wechat_hardened_runtime_still_enabled",
        ):
            wechat_resign.verify_resigned_wechat_bundle(
                self.bundle,
                runner=lambda *_args, **_kwargs: next(results),
            )

    def test_exact_target_is_passed_through_sign_verify_and_reopen(self):
        calls = []

        def runner(args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        reopened = key_extractor.ScanTarget(
            44,
            self.bundle.app_path,
            self.bundle.executable_path,
            200.0,
            (self.bundle.version, self.bundle.build, "arm64"),
            self.bundle.executable_identity,
        )
        ensure, unlock = self._identity_patches()
        with (
            ensure,
            unlock,
            patch.object(wechat_resign, "resolve_wechat_resign_target", return_value=self.target),
            patch.object(wechat_resign, "revalidate_wechat_resign_target", return_value=self.target),
            patch.object(wechat_resign, "terminate_wechat_target") as terminate,
            patch.object(
                wechat_resign,
                "verify_resigned_wechat_bundle",
                return_value=self.bundle,
            ) as verify,
            patch.object(
                wechat_resign,
                "reopen_wechat_target",
                return_value=reopened,
            ) as reopen,
        ):
            outcome = wechat_resign.resign_wechat_bundle(
                self.app,
                allow=True,
                explicit_target=True,
                runner=runner,
            )

        terminate.assert_called_once_with(self.running)
        verify.assert_called_once_with(
            self.bundle,
            runner=runner,
            expected_certificate_root=self.identity.certificate_root,
        )
        reopen.assert_called_once_with(self.bundle, previous=self.running)
        self.assertEqual(outcome.running, reopened)
        signed = self._sign_call(calls)
        self.assertNotIn("sudo", signed)
        self.assertEqual(signed[0], "/usr/bin/codesign")
        self.assertEqual(
            signed[signed.index("--sign") + 1], self.identity.common_name
        )
        self.assertEqual(
            signed[signed.index("--keychain") + 1], self.identity.keychain_path
        )
        self.assertIn("--entitlements", signed)
        self.assertEqual(signed[-1], self.bundle.app_path)

    def test_unwritable_bundle_repairs_ownership_before_signing(self):
        calls = []

        def runner(args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        reopened = key_extractor.ScanTarget(
            44,
            self.bundle.app_path,
            self.bundle.executable_path,
            200.0,
            (self.bundle.version, self.bundle.build, "arm64"),
            self.bundle.executable_identity,
        )
        ensure, unlock = self._identity_patches()
        with (
            ensure,
            unlock,
            patch.object(wechat_resign, "resolve_wechat_resign_target", return_value=self.target),
            patch.object(wechat_resign, "revalidate_wechat_resign_target", return_value=self.target),
            patch.object(wechat_resign, "terminate_wechat_target"),
            patch.object(wechat_resign, "_wechat_bundle_writable", return_value=False),
            patch.object(
                wechat_resign, "verify_resigned_wechat_bundle", return_value=self.bundle
            ),
            patch.object(wechat_resign, "reopen_wechat_target", return_value=reopened),
        ):
            wechat_resign.resign_wechat_bundle(
                self.app,
                allow=True,
                runner=runner,
            )

        self.assertEqual(calls[0], ["sudo", "-v"])
        chown = next(call for call in calls if "chown" in call)
        self.assertEqual(
            chown,
            [
                "sudo", "-n", "chown", "-R",
                f"{os.getuid()}:{os.getgid()}",
                self.bundle.app_path,
            ],
        )
        self.assertLess(
            calls.index(chown), calls.index(self._sign_call(calls))
        )

    def test_failed_privilege_check_stops_before_termination(self):
        def runner(args, **_kwargs):
            return subprocess.CompletedProcess(args, 1, "", "")

        ensure, unlock = self._identity_patches()
        with (
            ensure,
            unlock,
            patch.object(wechat_resign, "resolve_wechat_resign_target", return_value=self.target),
            patch.object(wechat_resign, "_wechat_bundle_writable", return_value=False),
            patch.object(wechat_resign, "terminate_wechat_target") as terminate,
        ):
            with self.assertRaisesRegex(
                wechat_resign.WeChatResignError,
                "wechat_privilege_unavailable",
            ):
                wechat_resign.resign_wechat_bundle(
                    self.app,
                    allow=True,
                    runner=runner,
                )

        terminate.assert_not_called()

    def test_launcher_has_no_name_bound_termination_or_rediscovered_reopen(self):
        launcher = repo_path("launchers", "启动.command").read_text(encoding="utf-8")
        self.assertNotIn("killall WeChat", launcher)
        self.assertNotIn('tell application "WeChat" to quit', launcher)
        self.assertIn("scripts/resign_wechat.py", launcher)
        self.assertNotIn('open "$app_path"', launcher)
        self.assertIn("from core.wechat_signature import inspect_wechat_signature", launcher)
        self.assertNotIn('grep -qx "Signature=adhoc"', launcher)


if __name__ == "__main__":
    unittest.main()
