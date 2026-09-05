import json
import hashlib
import hmac
import os
import plistlib
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.key_extractor import (
    PROTECTED_KEY_MEMORY_MASKS,
    _parse_raw_keys_from_log,
    _parse_raw_keys_from_text,
    _protected_key_memory_mask,
    _rematch_keys_from_output,
    extract_keys,
    get_wechat_build_identity,
    process_lookup_available,
)


class KeyExtractorTests(unittest.TestCase):
    @staticmethod
    def _scanner_result_writer(payload):
        def run(_args, **kwargs):
            output_path = os.path.join(kwargs["cwd"], "all_keys.json")
            with open(output_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="",
            )

        return run

    def test_process_lookup_availability_uses_current_process_as_visible_sentinel(self):
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"{os.getpid()}\n",
            stderr="",
        )
        with patch("core.key_extractor.subprocess.run", return_value=result) as run:
            self.assertTrue(process_lookup_available())

        run.assert_called_once_with(
            ["ps", "-p", str(os.getpid()), "-o", "pid="],
            capture_output=True,
            text=True,
        )

    def test_build_identity_reads_exact_wechat_version_build_and_architecture(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = os.path.join(tmp, "WeChat.app")
            contents = os.path.join(app_path, "Contents")
            os.makedirs(contents)
            with open(os.path.join(contents, "Info.plist"), "wb") as handle:
                plistlib.dump({
                    "CFBundleShortVersionString": "4.1.11",
                    "CFBundleVersion": "269136",
                }, handle)

            with patch("core.key_extractor.platform.machine", return_value="arm64"):
                self.assertEqual(
                    get_wechat_build_identity(app_path),
                    ("4.1.11", "269136", "arm64"),
                )

    def test_protected_key_mask_is_enabled_only_for_an_exact_supported_build(self):
        identity = ("4.1.11", "269136", "arm64")
        self.assertEqual(_protected_key_memory_mask(identity), PROTECTED_KEY_MEMORY_MASKS[identity])
        self.assertIsNone(_protected_key_memory_mask(("4.1.12", "future", "arm64")))
        self.assertIsNone(_protected_key_memory_mask())

    def test_missing_extract_log_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.log")

            self.assertEqual(_parse_raw_keys_from_log(missing), [])

    def test_parse_raw_keys_from_scanner_text(self):
        key_hex = "a" * 64
        salt_hex = "12" * 16
        text = f"message/message_0.db   {key_hex}   {salt_hex}\n"

        self.assertEqual(_parse_raw_keys_from_text(text), [(key_hex, salt_hex)])

    def test_parse_key_only_candidate_from_current_scanner(self):
        key_hex = "c" * 64

        self.assertEqual(
            _parse_raw_keys_from_text(f"WGO_KEY {key_hex} -\n"),
            [(key_hex, None)],
        )

    def test_rematch_keys_from_in_memory_output_without_extract_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, "db_storage")
            os.makedirs(os.path.join(db_dir, "message"))
            keys_file = os.path.join(tmp, "all_keys.json")
            extract_log = os.path.join(tmp, "extract_keys.log")
            key_hex = "b" * 64
            salt_hex = "34" * 16
            db_path = os.path.join(db_dir, "message", "message_0.db")
            key = bytes.fromhex(key_hex)
            page = bytearray(4096)
            page[:16] = bytes.fromhex(salt_hex)
            mac_salt = bytes(value ^ 0x3A for value in page[:16])
            mac_key = hashlib.pbkdf2_hmac(
                "sha512", key, mac_salt, 2, dklen=32
            )
            digest = hmac.new(mac_key, page[16:4032], hashlib.sha512)
            digest.update(struct.pack("<I", 1))
            page[-64:] = digest.digest()
            with open(db_path, "wb") as handle:
                handle.write(page)

            with patch("core.key_extractor.KEYS_FILE", keys_file), \
                 patch("core.key_extractor.EXTRACT_LOG", extract_log):
                matched = _rematch_keys_from_output(
                    db_dir,
                    f"WGO_KEY {key_hex} -\n",
                )

            self.assertEqual(matched, {"message/message_0.db": {"enc_key": key_hex}})
            self.assertFalse(os.path.exists(extract_log))
            with open(keys_file, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), matched)

    def test_empty_scan_preserves_existing_key_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys_file = os.path.join(tmp, "all_keys.json")
            existing = {
                "message/message_0.db": {"enc_key": "a" * 64},
            }
            with open(keys_file, "w", encoding="utf-8") as handle:
                json.dump(existing, handle)

            with patch("core.key_extractor.DATA_DIR", tmp), \
                 patch("core.key_extractor.KEYS_FILE", keys_file), \
                 patch("core.key_extractor.compile_scanner", return_value=True), \
                 patch("core.key_extractor.get_wechat_pid", return_value=42), \
                 patch("core.key_extractor.load_config", return_value={"db_dir": ""}), \
                 patch(
                     "core.key_extractor.subprocess.run",
                     side_effect=self._scanner_result_writer({}),
                 ):
                result = extract_keys()

            self.assertIsNone(result)
            with open(keys_file, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), existing)

    def test_unverified_partial_stage_preserves_existing_key_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys_file = os.path.join(tmp, "all_keys.json")
            existing = {
                "message/message_0.db": {"enc_key": "a" * 64},
            }
            discovered = {
                "message/message_3.db": {"enc_key": "b" * 64},
            }
            with open(keys_file, "w", encoding="utf-8") as handle:
                json.dump(existing, handle)

            with patch("core.key_extractor.DATA_DIR", tmp), \
                 patch("core.key_extractor.KEYS_FILE", keys_file), \
                 patch("core.key_extractor.compile_scanner", return_value=True), \
                 patch("core.key_extractor.get_wechat_pid", return_value=42), \
                 patch("core.key_extractor.load_config", return_value={"db_dir": ""}), \
                 patch(
                     "core.key_extractor.subprocess.run",
                     side_effect=self._scanner_result_writer(discovered),
                 ):
                result = extract_keys()

            # Staged JSON has no HMAC authority, even when nonempty.
            expected = existing
            self.assertIsNone(result)
            with open(keys_file, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), expected)


if __name__ == "__main__":
    unittest.main()
