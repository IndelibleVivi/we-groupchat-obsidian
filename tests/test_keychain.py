import subprocess
import unittest
from unittest.mock import Mock, patch

from core import keychain
from core.platform import SecretStoreError
from core.platform.macos_secrets import MacOSSecretStore


class KeychainTests(unittest.TestCase):
    def test_new_service_name_is_primary_identity(self):
        self.assertEqual(keychain.SERVICE_NAME, "we-groupchat-obsidian")
        self.assertEqual(keychain.LEGACY_SERVICE_NAMES, ("wechat-summary",))

    def test_load_key_falls_back_only_when_missing(self):
        runner = Mock(side_effect=[
            subprocess.CompletedProcess([], 44, stdout=""),
            subprocess.CompletedProcess([], 0, stdout="legacy-key\n"),
        ])
        store = MacOSSecretStore(runner=runner)
        with patch("core.keychain.create_secret_store", return_value=store):
            self.assertEqual(keychain.load_key("ai-api-key"), "legacy-key")
        services = [c.args[0][c.args[0].index("-s") + 1] for c in runner.call_args_list]
        self.assertEqual(services, ["we-groupchat-obsidian", "wechat-summary"])

    def test_locked_keychain_does_not_fall_back(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 36, stdout="", stderr="private"))
        with self.assertRaisesRegex(SecretStoreError, "^secret_store_unavailable$"):
            MacOSSecretStore(runner=runner).load("private-account")
        self.assertEqual(runner.call_count, 1)

    def test_timeout_and_os_error_are_content_free(self):
        for error in (subprocess.TimeoutExpired(["private-secret"], 5), OSError("private-path")):
            with self.subTest(kind=type(error).__name__):
                store = MacOSSecretStore(runner=Mock(side_effect=error))
                with self.assertRaisesRegex(SecretStoreError, "^secret_store_unavailable$"):
                    store.save("private-account", "private-secret")

    def test_save_uses_current_service_and_bounded_native_call(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=""))
        MacOSSecretStore(runner=runner).save("ai-api-key", "fixture-secret")
        runner.assert_called_once_with([
            "security", "add-generic-password", "-a", "ai-api-key",
            "-s", "we-groupchat-obsidian", "-w", "fixture-secret", "-U",
        ], capture_output=True, text=True, check=False, timeout=5)

    def test_delete_removes_legacy_fallback_and_is_idempotent(self):
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, stdout=""),
                                   subprocess.CompletedProcess([], 44, stdout="")])
        MacOSSecretStore(runner=runner).delete("ai-api-key")
        services = [c.args[0][c.args[0].index("-s") + 1] for c in runner.call_args_list]
        self.assertEqual(services, ["we-groupchat-obsidian", "wechat-summary"])

    def test_facade_failure_never_reports_success(self):
        store = Mock()
        store.load.side_effect = SecretStoreError("secret_store_unavailable")
        store.save.side_effect = SecretStoreError("secret_write_failed")
        store.delete.side_effect = SecretStoreError("secret_delete_failed")
        with patch("core.keychain.create_secret_store", return_value=store):
            self.assertIsNone(keychain.load_key("fixture"))
            self.assertFalse(keychain.save_key("fixture", "value"))
            self.assertFalse(keychain.delete_key("fixture"))
            self.assertEqual(keychain.secret_status("fixture"), "unavailable")

    def test_status_distinguishes_missing_and_present_without_content(self):
        store = Mock()
        with patch("core.keychain.create_secret_store", return_value=store):
            store.load.return_value = None
            self.assertEqual(keychain.secret_status("fixture"), "missing")
            store.load.return_value = "private-secret"
            self.assertEqual(keychain.secret_status("fixture"), "present")
