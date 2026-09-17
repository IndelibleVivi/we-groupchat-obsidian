"""Portable test doubles and guards for the W0.2B.2 storage tests.

``StubNative`` is deliberately *not* ACL evidence: it never proves Windows
privacy, which only real native Windows DACL checks can do. It exists to pin
the storage layer's access *requests* (for example that an existing ancestor is
opened without ``WRITE_DAC``, the way a normal user's C:\\ behaves), and it can
refuse any request the implementation should not be making. Real ACL proof
lives in the Windows-only tests and in ``native_probe``.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import types

from core.platform.contracts import PathIdentity
from core.platform.windows_private_storage import (
    _SECURITY_DESCRIPTOR,
    _WindowsPrivateStorageNative,
)


_DIRECTORY_ATTRIBUTE = 0x10
_FILE_NOT_FOUND = 2
_ACCESS_DENIED = 5
_FILE_EXISTS = 80
_ALREADY_EXISTS = 183


@contextlib.contextmanager
def no_blocking_wait(seconds: float = 5.0):
    """Turn a blocking call into a test failure instead of a hung suite.

    ``SIGALRM`` interrupts the blocked syscall and the handler raises, so a
    regression that opens a FIFO for reading fails with a clear message. The
    guard is a no-op on platforms without interval timers.
    """
    if not hasattr(signal, "setitimer") or not hasattr(signal, "SIGALRM"):
        yield
        return

    def handler(_signum, _frame):
        raise AssertionError("operation blocked on a non-regular endpoint")

    previous = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class StubHandle:
    def __init__(self, path: str, *, is_directory: bool):
        self.path = path
        self.attributes = _DIRECTORY_ATTRIBUTE if is_directory else 0


class StubPathService:
    """Minimal path service that maps a host-absolute path to a Windows form.

    The input is whatever ``os.path.abspath`` produced on the running host, so
    the conversion only replaces separators. It must not add a drive or a
    leading backslash: on Windows the host already supplies the drive.
    """

    def describe(self, path, *, source_root=None) -> PathIdentity:
        display = os.fspath(path)
        operational = display.replace("/", "\\")
        return PathIdentity(
            display_path=display,
            operational_path=operational,
            identity_key="stub:" + operational,
        )


def operational_path(path) -> str:
    """Return the Windows-form path the storage layer will derive on this host."""
    return os.path.abspath(os.fspath(path)).replace("/", "\\")


def temporary_prefix(directory) -> str:
    return operational_path(directory) + "\\" + ".wgo-publish-"


def _minimal_acl_bytes(ace_count: int = 1) -> bytes:
    """Return a well-formed ACL buffer with ``ace_count`` placeholder ACEs."""
    ace = bytes([0x00, 0x00]) + (0).to_bytes(2, "little") + bytes(8)
    body = ace * ace_count
    size = 8 + len(body)
    header = bytes([0x02, 0x00]) + size.to_bytes(2, "little")
    header += ace_count.to_bytes(2, "little") + bytes(2)
    return header + body


class AclApplicationApi:
    """advapi32/kernel32 pair that records the DACL application path.

    This is not ACL evidence. It exists so a test running on any host can pin
    which native API and which descriptor the storage layer hands to Windows:
    the raw handle-based descriptor setter with a request that carries no
    auto-inheritance bits, and never the propagating ``aclapi`` setter.
    """

    def __init__(self, acl_bytes: bytes | None = None):
        self.acl_bytes = (
            _minimal_acl_bytes() if acl_bytes is None else bytes(acl_bytes)
        )
        self.entries_in_acl_calls = 0
        self.aclapi_setter_calls: list[tuple] = []
        self.applications: list[tuple[int, int, int]] = []
        self.descriptors: list[bytes] = []
        self.freed = 0
        self.failure_status = 0
        self._acl_buffer = None
        self.advapi32 = types.SimpleNamespace(
            SetEntriesInAclW=self._set_entries_in_acl,
            SetSecurityInfo=self._aclapi_set_security_info,
        )
        self.ntdll = types.SimpleNamespace(
            NtSetSecurityObject=self._set_native_object_security,
            RtlNtStatusToDosError=lambda status: 5,
        )
        self.kernel32 = types.SimpleNamespace(LocalFree=self._local_free)

    def _set_entries_in_acl(self, count, entries, old_acl, new_acl):
        self.entries_in_acl_calls += 1
        self._acl_buffer = ctypes.create_string_buffer(
            self.acl_bytes,
            len(self.acl_bytes),
        )
        new_acl._obj.value = ctypes.addressof(self._acl_buffer)
        return self.failure_status

    def _set_native_object_security(self, handle, information, descriptor):
        raw = bytes(descriptor)
        self.descriptors.append(raw)
        view = _SECURITY_DESCRIPTOR.from_buffer_copy(raw)
        self.applications.append(
            (int(information), int(view.Control), int(view.OffsetDacl))
        )
        return 0

    def _aclapi_set_security_info(self, *args):
        self.aclapi_setter_calls.append(args)
        return 0

    def _local_free(self, pointer):
        self.freed += 1
        return None


def build_native_double(api: AclApplicationApi):
    """Build the real native layer over the recording API surface."""
    native = object.__new__(_WindowsPrivateStorageNative)
    native.kernel32 = api.kernel32
    native.advapi32 = api.advapi32
    native.ntdll = api.ntdll
    native._user_sid = bytes.fromhex("010100000000000100000000")
    native._system_sid = bytes.fromhex("010100000000000512000000")
    native._administrators_sid = bytes.fromhex("010200000000000520000000")
    return native


class StubNative:
    """Records access requests and can refuse DACL-write requests."""

    def __init__(
        self,
        *,
        directories=(),
        files=None,
        deny_write_dacl=(),
        create_private_file_error: OSError | None = None,
        create_private_file_error_is_pre_create: bool = False,
        collide_attempts: int = 0,
    ):
        self.directories = set(directories)
        self.files = dict(files or {})
        self.deny_write_dacl = set(deny_write_dacl)
        self.create_private_file_error = create_private_file_error
        self.create_private_file_error_is_pre_create = (
            create_private_file_error_is_pre_create
        )
        self.collide_attempts = collide_attempts
        self.attempts: list[str] = []
        self.open_calls: list[tuple[str, bool]] = []
        self.created_directories: list[str] = []
        self.created_files: list[str] = []
        self.secured: list[str] = []
        self.deleted: list[str] = []
        self.moves: list[tuple[str, str]] = []

    # -- handle operations -------------------------------------------------
    def open_existing(self, operational_path: str, *, write_dacl: bool = False):
        self.open_calls.append((operational_path, write_dacl))
        if write_dacl and operational_path in self.deny_write_dacl:
            raise OSError(_ACCESS_DENIED, "access denied")
        if operational_path in self.directories:
            return StubHandle(operational_path, is_directory=True)
        if operational_path in self.files:
            return StubHandle(operational_path, is_directory=False)
        raise OSError(_FILE_NOT_FOUND, "not found")

    def close(self, handle) -> None:
        pass

    def attributes(self, handle) -> int:
        return handle.attributes

    def owner_is_trusted(self, handle) -> bool:
        return True

    def apply_private_dacl(self, handle) -> None:
        self.secured.append(handle.path)

    def private_dacl_matches(self, handle) -> bool:
        return True

    # -- byte operations ---------------------------------------------------
    def create_directory(self, operational_path: str) -> None:
        if operational_path in self.directories:
            raise OSError(_ALREADY_EXISTS, "already exists")
        self.directories.add(operational_path)
        self.created_directories.append(operational_path)

    def create_private_file(self, operational_path: str):
        self.attempts.append(operational_path)
        if self.collide_attempts > 0:
            # The exclusive name already belonged to an older file.
            self.collide_attempts -= 1
            self.files.setdefault(operational_path, b"pre-existing temporary")
            raise OSError(_FILE_EXISTS, "file exists")
        if self.create_private_file_error is not None:
            if not self.create_private_file_error_is_pre_create:
                # The exclusive create succeeded before privacy was applied and
                # the native owner removes its own file again.
                self.files[operational_path] = b""
                self.created_files.append(operational_path)
                self.delete_file(operational_path)
            raise self.create_private_file_error
        self.files[operational_path] = b""
        self.created_files.append(operational_path)
        return StubHandle(operational_path, is_directory=False)

    def write_all(self, handle, payload: bytes) -> None:
        self.files[handle.path] = bytes(payload)

    def flush(self, handle) -> None:
        pass

    def move_file_replace(self, source: str, target: str) -> None:
        self.moves.append((source, target))
        self.files[target] = self.files.pop(source, b"")

    def delete_file(self, operational_path: str) -> None:
        self.deleted.append(operational_path)
        self.files.pop(operational_path, None)
