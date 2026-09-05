import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core import key_extractor
from core import wechat_resign
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

    def test_target_change_after_privilege_check_stops_before_signing(self):
        calls = []

        def runner(args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
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

        self.assertEqual(calls, [["sudo", "-v"]])

    def test_missing_explicit_authorization_performs_no_discovery_or_mutation(self):
        with (
            patch.object(wechat_resign, "resolve_wechat_resign_target") as resolve,
            patch.object(wechat_resign.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(
                wechat_resign.WeChatResignError,
                "wechat_resign_not_authorized",
            ):
                wechat_resign.resign_wechat_bundle(self.app, allow=False)

        resolve.assert_not_called()
        run.assert_not_called()

    def test_termination_timeout_never_calls_codesign(self):
        calls = []

        def runner(args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
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

        self.assertEqual(calls, [["sudo", "-v"]])

    def test_sign_success_without_independent_verification_does_not_reopen(self):
        calls = []

        def runner(args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
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

        self.assertEqual(calls[1][-1], self.bundle.app_path)
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

    def test_verification_rejects_non_adhoc_signature(self):
        results = iter((
            subprocess.CompletedProcess(["codesign", "--verify"], 0, "", ""),
            subprocess.CompletedProcess(
                ["codesign", "--display"],
                0,
                "",
                "CodeDirectory v=20500 size=755 flags=0x10000(runtime) hashes=14+2 location=embedded\n"
                "Signature size=8979\n",
            ),
        ))

        with self.assertRaisesRegex(
            wechat_resign.WeChatResignError,
            "wechat_signature_not_adhoc",
        ):
            wechat_resign.verify_resigned_wechat_bundle(
                self.bundle,
                runner=lambda *_args, **_kwargs: next(results),
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
        with (
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
        verify.assert_called_once_with(self.bundle, runner=runner)
        reopen.assert_called_once_with(self.bundle, previous=self.running)
        self.assertEqual(outcome.running, reopened)
        self.assertEqual(calls[1], [
            "sudo", "-n", "codesign", "--force", "--deep", "--sign", "-",
            self.bundle.app_path,
        ])

    def test_launcher_has_no_name_bound_termination_or_rediscovered_reopen(self):
        launcher = repo_path("launchers", "启动.command").read_text(encoding="utf-8")
        self.assertNotIn("killall WeChat", launcher)
        self.assertNotIn('tell application "WeChat" to quit', launcher)
        self.assertIn("scripts/resign_wechat.py", launcher)
        self.assertNotIn('open "$app_path"', launcher)
        self.assertIn('grep -qx "Signature=adhoc"', launcher)


if __name__ == "__main__":
    unittest.main()
