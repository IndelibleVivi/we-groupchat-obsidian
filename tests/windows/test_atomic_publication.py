"""Atomic byte publication behavior for the W0.2B.2 boundary.

The macOS section runs real ``os.replace`` publication; the Windows section
runs real ``MoveFileExW`` publication plus independent SDDL evidence and a
privilege-free directory junction. Pre-replace failure is injected through the
publisher's documented ``replace`` seam, which is why the injected failure test
can prove that previous bytes survive without mocking the privacy checks.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest

from core.platform import (
    PlatformName,
    PrivateStorageError,
    create_atomic_publisher,
    create_private_storage,
    detect_platform,
)
from tests.windows.native_probe import WindowsNativeProbe
from tests.windows.storage_doubles import no_blocking_wait


IS_DARWIN = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
_TEMPORARY_PREFIX = ".wgo-publish-"


class _InjectedReplaceFailure:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, source, target):
        self.calls.append(os.path.abspath(target))
        raise OSError(errno.EIO, "injected publication failure")


@unittest.skipUnless(
    detect_platform() in {PlatformName.MACOS, PlatformName.WINDOWS},
    "W0.2B.2 publication evidence targets macOS and Windows",
)
class AtomicPublicationTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.storage = create_private_storage()
        self.publisher = create_atomic_publisher()

    def _failing_publisher(self):
        replace = _InjectedReplaceFailure()
        if IS_WINDOWS:
            from core.platform.windows_private_storage import (
                WindowsAtomicPublisher,
                WindowsPrivateStorage,
            )

            storage = WindowsPrivateStorage()
            return (
                WindowsAtomicPublisher(storage=storage, replace=replace),
                storage,
                replace,
            )
        from core.platform.macos_private_storage import (
            MacOSAtomicPublisher,
            MacOSPrivateStorage,
        )

        storage = MacOSPrivateStorage()
        return MacOSAtomicPublisher(storage=storage, replace=replace), storage, replace

    @staticmethod
    def _temporary_payloads(directory) -> list[str]:
        return [
            entry
            for entry in os.listdir(directory)
            if entry.startswith(_TEMPORARY_PREFIX)
        ]

    def test_missing_target_is_created_private(self):
        target = self.root / "nested" / "value.bin"

        self.publisher.write_bytes(target, b"payload")

        self.assertEqual(target.read_bytes(), b"payload")
        self.assertTrue(self.storage.verify(target))
        self.assertTrue(self.storage.verify(target.parent))
        self.assertEqual(sorted(os.listdir(target.parent)), ["value.bin"])

    def test_existing_target_is_replaced_as_one_step(self):
        target = self.root / "value.bin"
        self.publisher.write_bytes(target, b"first")
        before = os.lstat(target)

        self.publisher.write_bytes(target, b"second")

        self.assertEqual(target.read_bytes(), b"second")
        self.assertTrue(self.storage.verify(target))
        self.assertEqual(sorted(os.listdir(self.root)), ["value.bin"])
        if IS_DARWIN:
            # A replace publishes a different inode instead of truncating the
            # previous target in place.
            self.assertNotEqual(os.lstat(target).st_ino, before.st_ino)

    def test_injected_pre_replace_failure_preserves_previous_bytes(self):
        target = self.root / "value.bin"
        self.publisher.write_bytes(target, b"first")
        failing, storage, replace = self._failing_publisher()

        with self.assertRaises(OSError):
            failing.write_bytes(target, b"second")

        self.assertEqual(len(replace.calls), 1)
        # Windows publication uses an extended operational path. Compare the
        # actual target identity instead of its display spelling.
        self.assertTrue(os.path.samefile(replace.calls[0], target))
        self.assertEqual(target.read_bytes(), b"first")
        self.assertTrue(storage.verify(target))
        self.assertEqual(sorted(os.listdir(self.root)), ["value.bin"])
        self.assertEqual(self._temporary_payloads(self.root), [])

    def test_failed_first_creation_leaves_no_target_or_temporary_payload(self):
        target = self.root / "value.bin"
        failing, _storage, _replace = self._failing_publisher()

        with self.assertRaises(OSError):
            failing.write_bytes(target, b"payload")

        self.assertFalse(os.path.exists(target))
        self.assertEqual(sorted(os.listdir(self.root)), [])

    def test_reparse_target_is_rejected_without_publishing(self):
        if IS_WINDOWS:
            probe = WindowsNativeProbe()
            outside_file = self.root / "outside.bin"
            outside_file.write_bytes(b"outside")
            link = self.root / "link.bin"
            try:
                os.symlink(str(outside_file), str(link))
            except OSError:
                # An unprivileged runner cannot always create a file symlink;
                # a directory junction needs no privilege and exercises the
                # same reparse-point admission.
                outside_directory = self.root / "outside"
                outside_directory.mkdir()
                link = self.root / "junction"
                link.mkdir()
                probe.create_reparse_point(str(link), str(outside_directory))
                with self.assertRaises(PrivateStorageError) as raised:
                    self.publisher.write_bytes(link / "value.bin", b"payload")
                self.assertEqual(raised.exception.reason, "reparse_point")
                self.assertEqual(sorted(os.listdir(outside_directory)), [])
                return
            with self.assertRaises(PrivateStorageError) as raised:
                self.publisher.write_bytes(link, b"payload")
            self.assertIn(
                raised.exception.reason,
                {"symbolic_link", "reparse_point"},
            )
            self.assertEqual(outside_file.read_bytes(), b"outside")
        else:
            outside_file = self.root / "outside.bin"
            outside_file.write_bytes(b"outside")
            os.chmod(outside_file, 0o644)
            link = self.root / "link.bin"
            os.symlink(outside_file, link)

            with self.assertRaises(PrivateStorageError) as raised:
                self.publisher.write_bytes(link, b"payload")

            self.assertEqual(raised.exception.code, "private_storage_path_rejected")
            self.assertEqual(raised.exception.reason, "symbolic_link")
            self.assertEqual(outside_file.read_bytes(), b"outside")
        self.assertEqual(self._temporary_payloads(self.root), [])

    def test_directory_target_is_rejected(self):
        target = self.root / "directory"
        target.mkdir()

        with self.assertRaises(PrivateStorageError) as raised:
            self.publisher.write_bytes(target, b"payload")

        self.assertEqual(raised.exception.reason, "not_regular")
        self.assertTrue(target.is_dir())

    def test_payload_must_be_bytes_like(self):
        target = self.root / "value.bin"

        with self.assertRaises(TypeError):
            self.publisher.write_bytes(target, "text")
        self.assertFalse(os.path.exists(target))

        self.publisher.write_bytes(target, bytearray(b"abc"))
        self.assertEqual(target.read_bytes(), b"abc")
        self.publisher.write_bytes(target, memoryview(b"def"))
        self.assertEqual(target.read_bytes(), b"def")

    def test_empty_and_large_payloads_round_trip(self):
        target = self.root / "value.bin"

        self.publisher.write_bytes(target, b"")
        self.assertEqual(target.read_bytes(), b"")
        self.assertTrue(self.storage.verify(target))

        payload = (b"0123456789abcdef" * ((1 << 20) // 16 + 1))[:(1 << 20) + 7]
        self.publisher.write_bytes(target, payload)
        self.assertEqual(target.read_bytes(), payload)
        self.assertTrue(self.storage.verify(target))
        self.assertEqual(self._temporary_payloads(self.root), [])

    def test_filesystem_root_is_rejected(self):
        with self.assertRaises(PrivateStorageError) as raised:
            self.publisher.write_bytes(os.path.abspath(os.sep), b"payload")
        self.assertEqual(raised.exception.reason, "filesystem_root")

    @unittest.skipUnless(IS_WINDOWS, "SDDL evidence is Windows-specific")
    def test_published_windows_file_has_a_protected_two_ace_dacl(self):
        target = self.root / "value.bin"

        self.publisher.write_bytes(target, b"payload")

        sddl = WindowsNativeProbe().sddl(target)
        self.assertIn("D:P", sddl)
        self.assertIn("SY", sddl)
        self.assertNotIn("S-1-1-0", sddl)
        self.assertEqual(sddl.count("(A;"), 2)
        self.assertEqual(self._temporary_payloads(self.root), [])


@unittest.skipUnless(IS_DARWIN, "FIFO admission evidence is POSIX-specific")
class AtomicPublicationFifoTests(unittest.TestCase):
    def test_fifo_target_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "fifo"
            os.mkfifo(fifo)

            with no_blocking_wait():
                with self.assertRaises(PrivateStorageError) as raised:
                    create_atomic_publisher().write_bytes(fifo, b"payload")

            self.assertEqual(
                raised.exception.code,
                "private_storage_path_rejected",
            )
            self.assertEqual(raised.exception.reason, "unsupported_type")
            self.assertTrue(fifo.exists())


if __name__ == "__main__":
    unittest.main()
