"""Real private-storage behavior tests for the W0.2B.2 boundary.

macOS runs the concrete posix-mode backend against real temporary fixtures.
The Windows section runs native DACL evidence on a Windows runner and is never
skipped there; there is no mock-only ACL substitute on either platform.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

import core.platform.windows_private_storage as windows_storage
from core.platform import (
    PlatformCapabilityUnavailable,
    PlatformName,
    PlatformServices,
    PrivateStorageError,
    create_private_storage,
)
from core.platform.factory import (
    PlatformServicesUnavailable,
    _PLATFORM_FACTORIES,
)
from core.platform.windows_private_storage import (
    WindowsAtomicPublisher,
    WindowsPrivateStorage,
)
from tests.windows.native_probe import WindowsNativeProbe
from tests.windows.storage_doubles import (
    StubNative,
    StubPathService,
    no_blocking_wait,
)


IS_DARWIN = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
DESKTOP_PLATFORMS = {PlatformName.MACOS, PlatformName.WINDOWS}


class PrivateStorageErrorTests(unittest.TestCase):
    def test_error_is_content_free_oserror_with_stable_fields(self):
        error = PrivateStorageError(
            "private_storage_path_rejected",
            reason="symbolic_link",
            native_error=40,
        )
        self.assertIsInstance(error, OSError)
        self.assertEqual(error.code, "private_storage_path_rejected")
        self.assertEqual(error.reason, "symbolic_link")
        self.assertEqual(error.native_error, 40)
        self.assertEqual(error.errno, 40)
        self.assertNotIn(os.sep, str(error))

    def test_error_defaults_stay_bounded(self):
        error = PrivateStorageError("private_storage_missing")
        self.assertEqual(error.reason, "")
        self.assertIsNone(error.errno)
        self.assertEqual(str(error), "private_storage_missing")

    def test_unavailable_capability_fails_closed(self):
        services = PlatformServices(platform=PlatformName.WINDOWS, locks=object())
        with self.assertRaises(PlatformCapabilityUnavailable) as raised:
            services.require("private_storage")
        self.assertEqual(raised.exception.code, "platform_capability_unavailable")
        with self.assertRaises(PlatformCapabilityUnavailable):
            services.require("atomic_publisher")

    def test_unregistered_platform_fails_closed(self):
        with patch.dict(_PLATFORM_FACTORIES, {}, clear=True):
            with self.assertRaises(PlatformServicesUnavailable):
                create_private_storage(PlatformName.WINDOWS)


@unittest.skipUnless(IS_DARWIN, "posix private-storage evidence runs on macOS")
class MacOSPrivateStorageTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.storage = create_private_storage()

    @staticmethod
    def _mode(path) -> int:
        return stat.S_IMODE(os.lstat(path).st_mode)

    def test_nested_directories_are_created_private(self):
        target = self.root / "a" / "b" / "c"
        self.storage.ensure_directory(target)
        for value in (self.root / "a", self.root / "a" / "b", target):
            self.assertEqual(self._mode(value), 0o700)
            self.assertEqual(os.lstat(value).st_uid, os.getuid())
            self.assertTrue(self.storage.verify(value))

    def test_exact_directory_is_tightened_without_recursive_chmod(self):
        parent = self.root / "parent"
        parent.mkdir()
        os.chmod(parent, 0o755)
        child = parent / "child"
        child.mkdir()
        os.chmod(child, 0o755)
        sibling = parent / "sibling.bin"
        sibling.write_bytes(b"x")
        os.chmod(sibling, 0o644)

        self.storage.ensure_directory(child)

        self.assertEqual(self._mode(child), 0o700)
        self.assertTrue(self.storage.verify(child))
        self.assertEqual(self._mode(parent), 0o755)
        self.assertEqual(self._mode(sibling), 0o644)

    def test_symlinked_endpoint_is_rejected_without_following_it(self):
        outside = self.root / "outside"
        outside.mkdir()
        os.chmod(outside, 0o755)
        link = self.root / "link"
        os.symlink(outside, link)

        with self.assertRaises(PrivateStorageError) as raised:
            self.storage.ensure_directory(link)

        self.assertEqual(raised.exception.code, "private_storage_path_rejected")
        self.assertEqual(raised.exception.reason, "symbolic_link")
        self.assertTrue(link.is_symlink())
        self.assertEqual(self._mode(outside), 0o755)

    def test_symlinked_ancestor_resolves_while_the_link_is_preserved(self):
        real = self.root / "real"
        real.mkdir()
        anchor = self.root / "anchor"
        os.symlink(real, anchor)

        self.storage.ensure_directory(anchor / "leaf")

        self.assertTrue(anchor.is_symlink())
        self.assertEqual(self._mode(real / "leaf"), 0o700)
        self.assertTrue(self.storage.verify(anchor / "leaf"))

    def test_ensure_file_secures_an_existing_regular_file_only(self):
        value = self.root / "value.bin"
        value.write_bytes(b"payload")
        os.chmod(value, 0o644)

        self.storage.ensure_file(value)

        self.assertEqual(self._mode(value), 0o600)
        self.assertTrue(self.storage.verify(value))
        self.assertEqual(value.read_bytes(), b"payload")
        with self.assertRaises(PrivateStorageError) as missing:
            self.storage.ensure_file(self.root / "missing.bin")
        self.assertEqual(missing.exception.code, "private_storage_missing")
        with self.assertRaises(PrivateStorageError) as directory:
            self.storage.ensure_file(self.root)
        self.assertEqual(directory.exception.reason, "not_regular")

    def test_fifo_endpoints_are_rejected_without_blocking(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)

        with no_blocking_wait():
            self.assertFalse(self.storage.verify(fifo))
            with self.assertRaises(PrivateStorageError) as raised:
                self.storage.ensure_file(fifo)

        self.assertEqual(raised.exception.code, "private_storage_path_rejected")
        self.assertEqual(raised.exception.reason, "not_regular")

    def test_ensure_file_rejects_symlink_without_touching_the_target(self):
        outside = self.root / "outside.bin"
        outside.write_bytes(b"outside")
        os.chmod(outside, 0o644)
        link = self.root / "link.bin"
        os.symlink(outside, link)

        with self.assertRaises(PrivateStorageError) as raised:
            self.storage.ensure_file(link)

        self.assertEqual(raised.exception.reason, "symbolic_link")
        self.assertEqual(self._mode(outside), 0o644)
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_verify_is_read_only_and_fails_closed(self):
        insecure_directory = self.root / "insecure"
        insecure_directory.mkdir()
        os.chmod(insecure_directory, 0o755)
        insecure_file = self.root / "insecure.bin"
        insecure_file.write_bytes(b"x")
        os.chmod(insecure_file, 0o644)
        link = self.root / "link"
        os.symlink(insecure_file, link)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        secure_directory = self.root / "secure"
        secure_directory.mkdir()
        os.chmod(secure_directory, 0o700)
        secure_file = self.root / "secure.bin"
        secure_file.write_bytes(b"x")
        os.chmod(secure_file, 0o600)

        for value in (
            insecure_directory,
            insecure_file,
            link,
            fifo,
            self.root / "absent",
        ):
            self.assertFalse(self.storage.verify(value))
        self.assertEqual(self._mode(insecure_directory), 0o755)
        self.assertEqual(self._mode(insecure_file), 0o644)
        self.assertTrue(self.storage.verify(secure_directory))
        self.assertTrue(self.storage.verify(secure_file))
        self.assertFalse(self.storage.verify(""))
        self.assertFalse(self.storage.verify(object()))

    def test_created_directories_ignore_a_hostile_umask(self):
        previous = os.umask(0o777)
        try:
            target = self.root / "umask" / "leaf"
            self.storage.ensure_directory(target)
        finally:
            os.umask(previous)
        self.assertEqual(self._mode(target), 0o700)
        self.assertTrue(self.storage.verify(target))

    def test_filesystem_root_is_rejected(self):
        with self.assertRaises(PrivateStorageError) as raised:
            self.storage.ensure_directory(os.path.abspath(os.sep))
        self.assertEqual(raised.exception.reason, "filesystem_root")


@unittest.skipUnless(IS_WINDOWS, "native DACL evidence runs on Windows")
class WindowsPrivateStorageTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.storage = create_private_storage()
        self.probe = WindowsNativeProbe()

    def _make_reparse_point(self, link_path, target) -> None:
        try:
            os.symlink(str(target), str(link_path), target_is_directory=True)
        except OSError:
            Path(link_path).mkdir()
            self.probe.create_reparse_point(str(link_path), str(target))

    def test_broad_inherited_grant_is_rejected_then_removed(self):
        value = self.root / "value.bin"
        value.write_bytes(b"payload")
        self.probe.grant_everyone(value, directory=False)

        self.assertIn("S-1-1-0", self.probe.sddl(value))
        self.assertFalse(self.storage.verify(value))

        self.storage.ensure_file(value)

        self.assertTrue(self.storage.verify(value))
        sddl = self.probe.sddl(value)
        self.assertNotIn("S-1-1-0", sddl)
        self.assertIn("SY", sddl)
        self.assertIn("D:P", sddl)
        self.assertEqual(sddl.count("(A;"), 2)
        self.assertEqual(sddl.count("(A;;FA;;;"), 2)
        self.assertEqual(value.read_bytes(), b"payload")

    def test_default_inherited_file_is_not_private_until_ensured(self):
        value = self.root / "plain.bin"
        value.write_bytes(b"plain")

        before = self.probe.sddl(value)
        self.assertEqual(self.probe.sddl(value), before)
        self.assertFalse(self.storage.verify(value))
        self.assertEqual(self.probe.sddl(value), before)

        self.storage.ensure_file(value)

        self.assertTrue(self.storage.verify(value))
        self.assertIn("D:P", self.probe.sddl(value))

    def test_exact_directory_is_secured_and_ancestors_are_untouched(self):
        parent = self.root / "parent"
        parent.mkdir()
        ancestor_sddl = self.probe.sddl(parent)
        child = parent / "child"

        self.storage.ensure_directory(child)

        self.assertTrue(self.storage.verify(child))
        sddl = self.probe.sddl(child)
        self.assertIn("D:P", sddl)
        self.assertIn("SY", sddl)
        self.assertNotIn("S-1-1-0", sddl)
        self.assertEqual(sddl.count("(A;"), 2)
        # No ACE carries an inheritance flag: securing an exact object must
        # never propagate a DACL rewrite to children.
        self.assertNotIn("OICI", sddl)
        self.assertEqual(sddl.count("(A;;FA;;;"), 2)
        self.assertEqual(self.probe.sddl(parent), ancestor_sddl)

    def test_securing_an_existing_directory_leaves_children_untouched(self):
        parent = self.root / "existing"
        parent.mkdir()
        child = parent / "child"
        child.mkdir()
        nested = child / "nested.bin"
        nested.write_bytes(b"nested")
        sibling = parent / "sibling.bin"
        sibling.write_bytes(b"sibling")
        parent_before = self.probe.sddl(parent)
        child_before = self.probe.sddl(child)
        nested_before = self.probe.sddl(nested)
        sibling_before = self.probe.sddl(sibling)

        self.storage.ensure_directory(parent)

        self.assertTrue(self.storage.verify(parent))
        parent_sddl = self.probe.sddl(parent)
        self.assertNotEqual(parent_sddl, parent_before)
        self.assertIn("D:P", parent_sddl)
        self.assertNotIn("OICI", parent_sddl)
        self.assertEqual(self.probe.sddl(child), child_before)
        self.assertEqual(self.probe.sddl(nested), nested_before)
        self.assertEqual(self.probe.sddl(sibling), sibling_before)
        self.assertEqual(nested.read_bytes(), b"nested")

    def test_nested_directories_are_created_private(self):
        target = self.root / "a" / "b" / "c"
        self.storage.ensure_directory(target)
        for value in (self.root / "a", self.root / "a" / "b", target):
            with self.subTest(path=str(value)):
                self.assertTrue(self.storage.verify(value))
                sddl = self.probe.sddl(value)
                self.assertIn("D:P", sddl)
                self.assertNotIn("OICI", sddl)
                self.assertEqual(sddl.count("(A;;FA;;;"), 2)

    def test_temp_creation_failure_removes_its_own_file(self):
        directory = self.root / "state"
        directory.mkdir()
        target = directory / ".wgo-publish-probe"
        native = self.storage._native

        with patch.object(
            native,
            "apply_private_dacl",
            side_effect=OSError(5, "acl failure"),
        ):
            with self.assertRaises(OSError):
                native.create_private_file(str(target))

        self.assertFalse(target.exists())
        self.assertEqual(sorted(directory.iterdir()), [])

    def test_candidate_name_collision_preserves_the_older_file(self):
        directory = self.root / "state"
        directory.mkdir()
        older = directory / ".wgo-publish-0000000000000000"
        older.write_bytes(b"older temporary")
        older_sddl = self.probe.sddl(older)
        target = directory / "value.bin"
        real_urandom = os.urandom
        pending = [bytes(8)]

        def fake_urandom(count):
            if pending:
                return pending.pop(0)
            return real_urandom(count)

        # Force the first candidate name to collide with the file that already
        # owns it, so the real CreateFileW ERROR_FILE_EXISTS path runs.
        with patch(
            "core.platform.windows_private_storage.os.urandom",
            side_effect=fake_urandom,
        ):
            WindowsAtomicPublisher(storage=self.storage).write_bytes(
                target,
                b"new",
            )

        self.assertEqual(older.read_bytes(), b"older temporary")
        self.assertEqual(self.probe.sddl(older), older_sddl)
        self.assertEqual(target.read_bytes(), b"new")
        self.assertTrue(self.storage.verify(target))
        leftovers = [
            value
            for value in directory.iterdir()
            if value.name.startswith(".wgo-publish-")
        ]
        self.assertEqual(leftovers, [older])

    def test_ensure_file_requires_an_existing_regular_file(self):
        with self.assertRaises(PrivateStorageError) as missing:
            self.storage.ensure_file(self.root / "missing.bin")
        self.assertEqual(missing.exception.code, "private_storage_missing")
        with self.assertRaises(PrivateStorageError) as directory:
            self.storage.ensure_file(self.root)
        self.assertEqual(directory.exception.reason, "not_regular")

    def test_reparse_point_is_rejected_without_touching_the_target(self):
        outside = self.root / "outside"
        outside.mkdir()
        outside_file = outside / "value.bin"
        outside_file.write_bytes(b"outside")
        outside_sddl = self.probe.sddl(outside_file)
        link = self.root / "link"
        self._make_reparse_point(link, outside)

        self.assertFalse(self.storage.verify(link))
        with self.assertRaises(PrivateStorageError) as raised:
            self.storage.ensure_directory(link)
        self.assertEqual(raised.exception.reason, "reparse_point")
        with self.assertRaises(PrivateStorageError) as nested:
            self.storage.ensure_file(link / "value.bin")
        self.assertEqual(nested.exception.reason, "reparse_point")

        self.assertEqual(outside_file.read_bytes(), b"outside")
        self.assertEqual(self.probe.sddl(outside_file), outside_sddl)

    def test_filesystem_root_is_rejected(self):
        with self.assertRaises(PrivateStorageError) as raised:
            self.storage.ensure_directory(os.path.abspath(os.sep))
        self.assertEqual(raised.exception.reason, "filesystem_root")


class AllowedAceStructureTests(unittest.TestCase):
    """Local structural regression for the ACE layout and pointer rules.

    The real ACL proof runs on Windows; this covers the layout and the
    mask/flags decision locally so a ``byref(int)`` or ABI regression fails
    before the Windows job runs.
    """

    _SID = bytes.fromhex("010200000000000512000000")

    def _ace_buffer(self, *, ace_type=0, flags=0, mask=None, ace_size=None):
        if mask is None:
            mask = windows_storage._FILE_ALL_ACCESS
        offset = windows_storage._ACCESS_ALLOWED_ACE.SidStart.offset
        size = offset + len(self._SID)
        buffer = ctypes.create_string_buffer(size)
        ace = windows_storage._ACCESS_ALLOWED_ACE.from_buffer(buffer)
        ace.Header.AceType = ace_type
        ace.Header.AceFlags = flags
        ace.Header.AceSize = size if ace_size is None else ace_size
        ace.Mask = mask
        ctypes.memmove(ctypes.byref(buffer, offset), self._SID, len(self._SID))
        return buffer

    def test_ace_layout_matches_the_windows_abi(self):
        self.assertEqual(ctypes.sizeof(windows_storage._ACE_HEADER), 4)
        self.assertEqual(ctypes.sizeof(windows_storage._ACCESS_ALLOWED_ACE), 12)
        self.assertEqual(
            windows_storage._ACCESS_ALLOWED_ACE.SidStart.offset,
            8,
        )

    def test_sid_pointer_comes_from_the_ace_address(self):
        buffer = self._ace_buffer()
        address = ctypes.addressof(buffer)

        pointer = windows_storage._allowed_ace_sid(address)

        self.assertEqual(pointer.value, address + 8)
        self.assertEqual(
            ctypes.string_at(pointer, len(self._SID)),
            self._SID,
        )

    def test_only_full_access_allow_aces_are_private_evidence(self):
        allowed = self._ace_buffer()
        self.assertIsNotNone(
            windows_storage._private_allowed_ace_sid(
                ctypes.addressof(allowed)
            )
        )
        rejected = (
            ("deny", 1, 0, windows_storage._FILE_ALL_ACCESS),
            ("inherit_only", 0, 0x08, windows_storage._FILE_ALL_ACCESS),
            ("empty_mask", 0, 0, 0),
            (
                "partial_mask",
                0,
                0,
                windows_storage._FILE_ALL_ACCESS & ~0x1,
            ),
            ("unknown_type", 0x11, 0, windows_storage._FILE_ALL_ACCESS),
            ("short_ace", 0, 0, windows_storage._FILE_ALL_ACCESS),
        )
        for label, ace_type, flags, mask in rejected:
            with self.subTest(case=label):
                buffer = self._ace_buffer(
                    ace_type=ace_type,
                    flags=flags,
                    mask=mask,
                    ace_size=4 if label == "short_ace" else None,
                )
                self.assertIsNone(
                    windows_storage._private_allowed_ace_sid(
                        ctypes.addressof(buffer)
                    )
                )


class WindowsPermissionRequestContractTests(unittest.TestCase):
    """Existing ancestors must be inspected without requesting WRITE_DAC.

    The double refuses any DACL-write request the way a normal user's drive
    root does, so asking for more than the exact target fails this test.
    """

    def test_existing_ancestors_are_never_opened_for_dacl_write(self):
        native = StubNative(
            directories={"\\", "\\state"},
            deny_write_dacl={"\\", "\\state"},
        )
        storage = WindowsPrivateStorage(
            paths=StubPathService(),
            native=native,
        )

        storage.ensure_directory("/state/private/target")

        self.assertNotIn(("\\", True), native.open_calls)
        self.assertNotIn(("\\state", True), native.open_calls)
        self.assertEqual(
            sorted(native.created_directories),
            ["\\state\\private", "\\state\\private\\target"],
        )
        self.assertEqual(
            sorted(native.secured),
            ["\\state\\private", "\\state\\private\\target"],
        )

    def test_exact_existing_directory_is_still_secured(self):
        native = StubNative(
            directories={"\\", "\\state"},
            deny_write_dacl={"\\"},
        )
        storage = WindowsPrivateStorage(
            paths=StubPathService(),
            native=native,
        )

        storage.ensure_directory("/state")

        self.assertIn(("\\state", True), native.open_calls)
        self.assertEqual(native.secured, ["\\state"])
        self.assertEqual(native.created_directories, [])

    def test_verify_never_requests_dacl_write(self):
        native = StubNative(
            directories={"\\state"},
            files={"\\state\\value.bin": b"value"},
            deny_write_dacl={"\\state", "\\state\\value.bin"},
        )
        storage = WindowsPrivateStorage(
            paths=StubPathService(),
            native=native,
        )

        self.assertTrue(storage.verify("/state/value.bin"))

        self.assertEqual(
            native.open_calls,
            [("\\state\\value.bin", False)],
        )

    def test_ensure_file_requests_dacl_write_for_the_target_only(self):
        native = StubNative(
            directories={"\\state"},
            files={"\\state\\value.bin": b"value"},
            deny_write_dacl={"\\state"},
        )
        storage = WindowsPrivateStorage(
            paths=StubPathService(),
            native=native,
        )

        storage.ensure_file("/state/value.bin")

        self.assertEqual(
            native.open_calls,
            [("\\state\\value.bin", True)],
        )
        self.assertEqual(native.secured, ["\\state\\value.bin"])


class WindowsPublisherCleanupContractTests(unittest.TestCase):
    """A failed create is never cleanup evidence for the publisher.

    ``create_private_file`` removes the file it created itself when privacy
    application fails; the publisher only ever removes a temporary it received
    a handle for. The real post-create cleanup proof is the native Windows test
    ``test_temp_creation_failure_removes_its_own_file``.
    """

    def test_pre_create_failure_never_deletes_a_candidate(self):
        native = StubNative(
            directories={"\\", "\\state"},
            files={"\\state\\existing.bin": b"old"},
            create_private_file_error=OSError(5, "create failed"),
            create_private_file_error_is_pre_create=True,
        )
        storage = WindowsPrivateStorage(
            paths=StubPathService(),
            native=native,
        )
        publisher = WindowsAtomicPublisher(storage=storage)

        with self.assertRaises(OSError):
            publisher.write_bytes("/state/new.bin", b"payload")

        self.assertEqual(len(native.attempts), 1)
        self.assertEqual(native.deleted, [])
        self.assertEqual(native.created_files, [])
        self.assertEqual(native.files, {"\\state\\existing.bin": b"old"})

    def test_post_create_failure_is_cleaned_by_the_native_owner_only(self):
        native = StubNative(
            directories={"\\", "\\state"},
            files={"\\state\\existing.bin": b"old"},
            create_private_file_error=OSError(5, "acl failure"),
        )
        storage = WindowsPrivateStorage(
            paths=StubPathService(),
            native=native,
        )
        publisher = WindowsAtomicPublisher(storage=storage)

        with self.assertRaises(OSError):
            publisher.write_bytes("/state/new.bin", b"payload")

        self.assertEqual(len(native.attempts), 1)
        self.assertEqual(native.deleted, native.created_files)
        self.assertEqual(native.files, {"\\state\\existing.bin": b"old"})

    def test_candidate_collision_keeps_the_pre_existing_file(self):
        native = StubNative(
            directories={"\\", "\\state"},
            files={"\\state\\value.bin": b"old"},
            collide_attempts=1,
        )
        storage = WindowsPrivateStorage(
            paths=StubPathService(),
            native=native,
        )
        publisher = WindowsAtomicPublisher(storage=storage)

        publisher.write_bytes("/state/value.bin", b"new")

        collided = native.attempts[0]
        self.assertTrue(collided.startswith("\\state\\.wgo-publish-"))
        self.assertEqual(native.attempts[1], native.moves[-1][0])
        self.assertNotEqual(native.attempts[1], collided)
        # The older file that owned the colliding name is untouched.
        self.assertEqual(native.files[collided], b"pre-existing temporary")
        self.assertNotIn(collided, native.deleted)
        self.assertEqual(native.files["\\state\\value.bin"], b"new")
        self.assertEqual(native.moves[-1][1], "\\state\\value.bin")


if __name__ == "__main__":
    unittest.main()
