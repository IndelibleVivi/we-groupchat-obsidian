"""Native Credential Manager tests write only unique synthetic credentials."""
import ctypes
import subprocess
import sys
import unittest
import uuid
from unittest.mock import Mock, patch

from core.platform import PlatformName, SecretStoreError, create_secret_store
from core.platform.windows_secrets import WindowsSecretStore


class WindowsSecretContractTests(unittest.TestCase):
    def test_construction_and_selection_do_not_access_credentials(self):
        with patch("core.platform.windows_secrets._native_api") as native:
            store = (create_secret_store(PlatformName.WINDOWS) if sys.platform == "win32"
                     else WindowsSecretStore())
            self.assertIsInstance(store, WindowsSecretStore)
            native.assert_not_called()

    def test_write_failure_has_no_plaintext_fallback(self):
        api = Mock()
        api.CredWriteW.return_value = False
        with patch("core.platform.windows_secrets._native_api", return_value=api), \
             patch("builtins.open", side_effect=AssertionError("no sidecar")):
            with self.assertRaisesRegex(SecretStoreError, "^secret_write_failed$"):
                WindowsSecretStore().save("fixture", "private-secret")
        self.assertEqual(api.CredWriteW.call_count, 1)

    def test_invalid_blob_rejected_before_native_write(self):
        with patch("core.platform.windows_secrets._native_api") as native:
            for value, code in (("", "secret_empty"), ("x" * 2561, "secret_too_large")):
                with self.assertRaisesRegex(SecretStoreError, f"^{code}$"):
                    WindowsSecretStore().save("fixture", value)
            native.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "native Windows Credential Manager")
class WindowsNativeSecretTests(unittest.TestCase):
    def setUp(self):
        self.store = WindowsSecretStore(service="wgo-test-" + uuid.uuid4().hex)
        self.addCleanup(self.store.delete, "second")
        self.addCleanup(self.store.delete, "first")

    def test_native_roundtrip_update_process_reload_and_delete(self):
        self.assertIsNone(self.store.load("first"))
        self.store.save("first", "fixture-密钥-🔑")
        self.store.save("second", "retained")
        self.assertEqual(self.store.load("first"), "fixture-密钥-🔑")
        self.store.save("first", "replacement")
        # New process uses the same user store, with no value in argv/output.
        code = ("from core.platform.windows_secrets import WindowsSecretStore; import sys; "
                "assert WindowsSecretStore(service=sys.argv[1]).load('first') == 'replacement'")
        result = subprocess.run([sys.executable, "-c", code, self.store.service],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.store.delete("first")
        self.store.delete("first")
        self.assertIsNone(self.store.load("first"))
        self.assertEqual(self.store.load("second"), "retained")

    def test_oversized_update_preserves_previous_protected_value(self):
        self.store.save("first", "previous")
        with self.assertRaisesRegex(SecretStoreError, "secret_too_large"):
            self.store.save("first", "x" * 2561)
        self.assertEqual(self.store.load("first"), "previous")

    def test_native_missing_differs_from_unavailable(self):
        api = Mock()
        api.CredReadW.return_value = False
        with patch("core.platform.windows_secrets._native_api", return_value=api):
            ctypes.set_last_error(5)
            with self.assertRaisesRegex(SecretStoreError, "secret_store_unavailable"):
                self.store.load("first")
            ctypes.set_last_error(1168)
            self.assertIsNone(self.store.load("first"))
