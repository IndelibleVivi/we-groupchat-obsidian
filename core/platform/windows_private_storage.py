"""Windows private storage and atomic byte publication for W0.2B.2.

Privacy on Windows is a per-object DACL question, so this backend uses native
``ctypes`` calls only: ``SetSecurityInfo`` applies a protected DACL that grants
the current user and LocalSystem full access, and ``GetSecurityInfo`` inspects
the DACL through a retained handle. ``chmod`` is never treated as Windows
privacy evidence and neither PowerShell nor ``icacls`` is invoked.

Path admission (local NTFS, reparse-point and case-sensitivity rejection, long
paths, reserved names) stays owned by :mod:`core.platform.windows_paths`; this
module only opens handles and requires the opened object to be a non-reparse
file or directory. Directories created by this module, plus the exact
requested directory, receive the private DACL; pre-existing ancestors are only
inspected and never re-ACLed.

Publication mirrors the macOS publisher: one private same-directory temporary
file, ``FlushFileBuffers`` before the move, and one ``MoveFileExW`` with
``MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH`` as the single publish
step. Atomic *visibility* therefore holds, but Windows exposes no user-mode
directory flush equivalent: the power-loss durability of the rename itself is
not guaranteed, which is a documented durability limit and not a visibility
limit.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import ntpath
import os
from os import PathLike

from .contracts import (
    PathIdentityError,
    PrivateStorageError,
    ReparsePointConflict,
)
from .windows_paths import (
    _ERROR_FILE_NOT_FOUND,
    _ERROR_PATH_NOT_FOUND,
    _FILE_ATTRIBUTE_DIRECTORY,
    _FILE_ATTRIBUTE_REPARSE_POINT,
    _FILE_ATTRIBUTE_TAG_INFO,
    _FILE_ATTRIBUTE_TAG_INFO_CLASS,
    _INVALID_HANDLE_VALUE,
    WindowsPathService,
)


PRIVACY_AUTHORITY = "windows-dacl"

# CreateDirectoryW reports an existing directory as ERROR_ALREADY_EXISTS;
# CreateFileW with CREATE_NEW reports an existing file as ERROR_FILE_EXISTS.
_ERROR_ALREADY_EXISTS = 183
_ERROR_FILE_EXISTS = 80
_TEMPORARY_NAME_COLLISIONS = frozenset(
    {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}
)

_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_CREATE_NEW = 1

_FILE_READ_ATTRIBUTES = 0x00000080
_GENERIC_WRITE = 0x40000000
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_FILE_ALL_ACCESS = 0x001F01FF

_FILE_ATTRIBUTE_TEMPORARY = 0x00000100
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

_MOVEFILE_REPLACE_EXISTING = 0x00000001
_MOVEFILE_WRITE_THROUGH = 0x00000008

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PRESENT = 0x0004
_SE_DACL_PROTECTED = 0x1000

_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0x00
_SET_ACCESS = 0x02
# An inherit-only ACE grants nothing to the object that stores it.
_ACE_INHERIT_ONLY = 0x08
_TRUSTEE_IS_SID = 0
_TRUSTEE_IS_USER = 1

_TOKEN_QUERY = 0x0008
_TOKEN_USER_INFORMATION_CLASS = 1
_SECURITY_MAX_SID_SIZE = 68
_WIN_LOCAL_SYSTEM_SID = 22
_WIN_BUILTIN_ADMINISTRATORS_SID = 26

_TEMP_PREFIX = ".wgo-publish-"
_TEMP_ATTEMPTS = 128
_WRITE_CHUNK_BYTES = 1 << 20

# ``ctypes.wintypes`` maps DWORD/ULONG to ``c_ulong``, which is eight bytes on
# 64-bit POSIX. Fixed-width fields keep these structures at their real Windows
# ABI layout on every host, so the ACE layout and pointer offsets can be
# regression-tested locally instead of only on Windows.
_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_BYTE = ctypes.c_int8


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", _DWORD),
    ]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", _DWORD),
        ("AclBytesInUse", _DWORD),
        ("AclBytesFree", _DWORD),
    ]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", _BYTE),
        ("AceFlags", _BYTE),
        ("AceSize", _WORD),
    ]


class _ACCESS_ALLOWED_ACE(ctypes.Structure):
    _fields_ = [
        ("Header", _ACE_HEADER),
        ("Mask", _DWORD),
        ("SidStart", _DWORD),
    ]


class _TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", ctypes.c_void_p),
    ]


class _EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", _DWORD),
        ("grfAccessMode", _DWORD),
        ("grfInheritance", _DWORD),
        ("Trustee", _TRUSTEE_W),
    ]


def _allowed_ace_sid(ace_address: int) -> ctypes.c_void_p:
    """Return a pointer to the SID stored in an ``ACCESS_ALLOWED_ACE``.

    ``ctypes`` structure field access returns a Python ``int`` for a DWORD
    field, so ``ctypes.byref(ace.contents.SidStart)`` cannot produce a pointer.
    The SID address is the ACE address plus the field offset instead.
    """
    return ctypes.c_void_p(
        int(ace_address) + _ACCESS_ALLOWED_ACE.SidStart.offset
    )


def _private_allowed_ace_sid(ace_address: int) -> ctypes.c_void_p | None:
    """Return the SID of an ACE that grants private full access.

    Returns ``None`` for a deny or unknown ACE type, for an inherit-only ACE
    that grants nothing to this object, and for an allow ACE whose mask is not
    full access. Verification must never treat an empty or partial grant as
    private access.
    """
    pointer = ctypes.c_void_p(int(ace_address))
    header = ctypes.cast(pointer, ctypes.POINTER(_ACE_HEADER)).contents
    if header.AceType != _ACCESS_ALLOWED_ACE_TYPE:
        return None
    if header.AceFlags & _ACE_INHERIT_ONLY:
        return None
    if header.AceSize < ctypes.sizeof(_ACCESS_ALLOWED_ACE):
        return None
    allowed = ctypes.cast(
        pointer, ctypes.POINTER(_ACCESS_ALLOWED_ACE)
    ).contents
    if allowed.Mask & _FILE_ALL_ACCESS != _FILE_ALL_ACCESS:
        return None
    return _allowed_ace_sid(ace_address)


def _coerce_target(path: str | PathLike[str]) -> str:
    try:
        value = os.fspath(path)
    except TypeError:
        raise PrivateStorageError(
            "private_storage_path_rejected",
            reason="path_type",
        ) from None
    if not isinstance(value, str):
        raise PrivateStorageError(
            "private_storage_path_rejected",
            reason="path_type",
        )
    if not value:
        raise PrivateStorageError(
            "private_storage_path_rejected",
            reason="empty_path",
        )
    if "\0" in value:
        raise PrivateStorageError(
            "private_storage_path_rejected",
            reason="nul_character",
        )
    try:
        return os.path.abspath(value)
    except OSError as exc:
        raise PrivateStorageError(
            "private_storage_unavailable",
            reason="working_directory",
            native_error=_native_code(exc),
        ) from None


def _coerce_payload(data: bytes) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, (bytearray, memoryview)):
        return bytes(data)
    raise TypeError("atomic publication payload must be bytes")


def _prefixes(absolute_display: str) -> list[str]:
    drive, tail = ntpath.splitdrive(absolute_display.replace("/", "\\"))
    root = drive + "\\"
    prefixes = [root]
    current = root
    for component in tail.split("\\"):
        if component:
            current = ntpath.join(current, component)
            prefixes.append(current)
    return prefixes


def _native_code(exc: OSError) -> int | None:
    winerror = getattr(exc, "winerror", None)
    if winerror:
        return int(winerror)
    return None if exc.errno is None else int(exc.errno)


def _is_missing(exc: OSError) -> bool:
    return _native_code(exc) in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}


def _translate_missing(reason: str) -> PrivateStorageError:
    return PrivateStorageError(
        "private_storage_missing",
        reason=reason,
        native_error=_ERROR_FILE_NOT_FOUND,
    )


def _translate_identity(exc: PathIdentityError) -> PrivateStorageError:
    if isinstance(exc, ReparsePointConflict):
        return PrivateStorageError(
            "private_storage_path_rejected",
            reason="reparse_point",
            native_error=exc.native_error,
        )
    return PrivateStorageError(
        "private_storage_path_rejected",
        reason=exc.reason,
        native_error=exc.native_error,
    )


class _WindowsPrivateStorageNative:
    """Retained-handle DACL and byte-publication primitives."""

    def __init__(self):
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise OSError("Windows private-storage APIs are unavailable")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._bind()
        self._user_sid = self._current_user_sid()
        self._system_sid = self._well_known_sid(_WIN_LOCAL_SYSTEM_SID)
        self._administrators_sid = self._well_known_sid(
            _WIN_BUILTIN_ADMINISTRATORS_SID
        )
        self._user_sid_buffer = ctypes.create_string_buffer(
            self._user_sid, len(self._user_sid)
        )
        self._system_sid_buffer = ctypes.create_string_buffer(
            self._system_sid, len(self._system_sid)
        )
        self._administrators_sid_buffer = ctypes.create_string_buffer(
            self._administrators_sid, len(self._administrators_sid)
        )

    def _bind(self) -> None:
        handle = wintypes.HANDLE
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            handle,
        ]
        self.kernel32.CreateFileW.restype = handle
        self.kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
        self.kernel32.CreateDirectoryW.restype = wintypes.BOOL
        self.kernel32.DeleteFileW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.DeleteFileW.restype = wintypes.BOOL
        self.kernel32.MoveFileExW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
        ]
        self.kernel32.MoveFileExW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [handle]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.WriteFile.argtypes = [
            handle,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        self.kernel32.WriteFile.restype = wintypes.BOOL
        self.kernel32.FlushFileBuffers.argtypes = [handle]
        self.kernel32.FlushFileBuffers.restype = wintypes.BOOL
        self.kernel32.GetFileInformationByHandleEx.argtypes = [
            handle,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self.kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.kernel32.GetCurrentProcess.argtypes = []
        self.kernel32.GetCurrentProcess.restype = handle
        # OpenProcessToken and GetTokenInformation live in Advapi32, not
        # Kernel32.
        self.advapi32.OpenProcessToken.argtypes = [
            handle,
            wintypes.DWORD,
            ctypes.POINTER(handle),
        ]
        self.advapi32.OpenProcessToken.restype = wintypes.BOOL
        self.advapi32.GetTokenInformation.argtypes = [
            handle,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetTokenInformation.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self.kernel32.LocalFree.restype = wintypes.HLOCAL

        self.advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
        self.advapi32.GetLengthSid.restype = wintypes.DWORD
        self.advapi32.CreateWellKnownSid.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.CreateWellKnownSid.restype = wintypes.BOOL
        self.advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.advapi32.EqualSid.restype = wintypes.BOOL
        self.advapi32.SetEntriesInAclW.argtypes = [
            _DWORD,
            ctypes.POINTER(_EXPLICIT_ACCESS_W),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi32.SetEntriesInAclW.restype = wintypes.DWORD
        self.advapi32.SetSecurityInfo.argtypes = [
            handle,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.advapi32.SetSecurityInfo.restype = wintypes.DWORD
        self.advapi32.GetSecurityInfo.argtypes = [
            handle,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi32.GetSecurityInfo.restype = wintypes.DWORD
        self.advapi32.GetSecurityDescriptorControl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        self.advapi32.GetAclInformation.argtypes = [
            ctypes.c_void_p,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.c_int,
        ]
        self.advapi32.GetAclInformation.restype = wintypes.BOOL
        self.advapi32.GetAce.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi32.GetAce.restype = wintypes.BOOL

    @staticmethod
    def _last_error() -> int:
        return int(ctypes.get_last_error())

    def _current_user_sid(self) -> bytes:
        token = wintypes.HANDLE()
        if not self.advapi32.OpenProcessToken(
            self.kernel32.GetCurrentProcess(),
            _TOKEN_QUERY,
            ctypes.byref(token),
        ):
            raise ctypes.WinError(self._last_error())
        try:
            required = wintypes.DWORD(0)
            self.advapi32.GetTokenInformation(
                token,
                _TOKEN_USER_INFORMATION_CLASS,
                None,
                0,
                ctypes.byref(required),
            )
            if required.value == 0:
                raise ctypes.WinError(self._last_error())
            buffer = ctypes.create_string_buffer(required.value)
            if not self.advapi32.GetTokenInformation(
                token,
                _TOKEN_USER_INFORMATION_CLASS,
                buffer,
                required.value,
                ctypes.byref(required),
            ):
                raise ctypes.WinError(self._last_error())
            user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER)).contents
            sid_length = int(self.advapi32.GetLengthSid(user.User.Sid))
            if sid_length <= 0:
                raise ctypes.WinError(self._last_error())
            return bytes(ctypes.string_at(user.User.Sid, sid_length))
        finally:
            self.kernel32.CloseHandle(token)

    def _well_known_sid(self, kind: int) -> bytes:
        size = wintypes.DWORD(_SECURITY_MAX_SID_SIZE)
        buffer = ctypes.create_string_buffer(_SECURITY_MAX_SID_SIZE)
        if not self.advapi32.CreateWellKnownSid(
            kind,
            None,
            buffer,
            ctypes.byref(size),
        ):
            raise ctypes.WinError(self._last_error())
        return bytes(buffer[: size.value])

    def open_existing(
        self,
        operational_path: str,
        *,
        write_dacl: bool = False,
    ) -> wintypes.HANDLE:
        access = _READ_CONTROL | _FILE_READ_ATTRIBUTES
        if write_dacl:
            access |= _WRITE_DAC
        handle = self.kernel32.CreateFileW(
            operational_path,
            access,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(self._last_error())
        return handle

    def create_directory(self, operational_path: str) -> None:
        if not self.kernel32.CreateDirectoryW(operational_path, None):
            raise ctypes.WinError(self._last_error())

    def delete_file(self, operational_path: str) -> None:
        if not self.kernel32.DeleteFileW(operational_path):
            raise ctypes.WinError(self._last_error())

    def move_file_replace(self, source: str, target: str) -> None:
        if not self.kernel32.MoveFileExW(
            source,
            target,
            _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH,
        ):
            raise ctypes.WinError(self._last_error())

    def close(self, handle: wintypes.HANDLE) -> None:
        self.kernel32.CloseHandle(handle)

    def attributes(self, handle: wintypes.HANDLE) -> int:
        value = _FILE_ATTRIBUTE_TAG_INFO()
        if not self.kernel32.GetFileInformationByHandleEx(
            handle,
            _FILE_ATTRIBUTE_TAG_INFO_CLASS,
            ctypes.byref(value),
            ctypes.sizeof(value),
        ):
            raise ctypes.WinError(self._last_error())
        return int(value.FileAttributes)

    def write_all(self, handle: wintypes.HANDLE, payload: bytes) -> None:
        written = wintypes.DWORD(0)
        offset = 0
        while offset < len(payload):
            chunk = payload[offset : offset + _WRITE_CHUNK_BYTES]
            buffer = ctypes.create_string_buffer(chunk, len(chunk))
            if not self.kernel32.WriteFile(
                handle,
                buffer,
                len(chunk),
                ctypes.byref(written),
                None,
            ):
                raise ctypes.WinError(self._last_error())
            if written.value == 0:
                raise ctypes.WinError(self._last_error())
            offset += written.value

    def flush(self, handle: wintypes.HANDLE) -> None:
        if not self.kernel32.FlushFileBuffers(handle):
            raise ctypes.WinError(self._last_error())

    def create_private_file(self, operational_path: str) -> wintypes.HANDLE:
        handle = self.kernel32.CreateFileW(
            operational_path,
            _GENERIC_WRITE | _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _CREATE_NEW,
            _FILE_ATTRIBUTE_TEMPORARY | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(self._last_error())
        try:
            # Runtime security attributes are ignored when opening an existing
            # object, so privacy is applied to the fresh handle before any
            # payload byte is written.
            self.apply_private_dacl(handle)
        except BaseException:
            self.close(handle)
            try:
                self.delete_file(operational_path)
            except OSError:
                pass
            raise
        return handle

    def _sid_buffer(self, sid: bytes) -> ctypes.Array:
        return ctypes.create_string_buffer(sid, len(sid))

    def _trustee(self, sid_buffer: ctypes.Array) -> _TRUSTEE_W:
        return _TRUSTEE_W(
            pMultipleTrustee=None,
            MultipleTrusteeOperation=0,
            TrusteeForm=_TRUSTEE_IS_SID,
            TrusteeType=_TRUSTEE_IS_USER,
            ptstrName=ctypes.cast(sid_buffer, ctypes.c_void_p),
        )

    def apply_private_dacl(
        self,
        handle: wintypes.HANDLE,
    ) -> None:
        # No ACE carries an inheritance flag. SetSecurityInfo propagates
        # inheritable ACEs to existing unprotected children, which would
        # re-ACL objects this call does not own; every object this module
        # creates is secured explicitly instead.
        entries = (_EXPLICIT_ACCESS_W * 2)()
        keepalive: list[ctypes.Array] = []
        for index, sid in enumerate((self._user_sid, self._system_sid)):
            buffer = self._sid_buffer(sid)
            keepalive.append(buffer)
            entries[index] = _EXPLICIT_ACCESS_W(
                grfAccessPermissions=_FILE_ALL_ACCESS,
                grfAccessMode=_SET_ACCESS,
                grfInheritance=0,
                Trustee=self._trustee(buffer),
            )
        acl = ctypes.c_void_p()
        status = self.advapi32.SetEntriesInAclW(
            2,
            entries,
            None,
            ctypes.byref(acl),
        )
        if status != 0:
            raise PrivateStorageError(
                "private_storage_permission",
                reason="acl_build_failed",
                native_error=int(status),
            )
        try:
            status = self.advapi32.SetSecurityInfo(
                handle,
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION
                | _PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                acl,
                None,
            )
        finally:
            self.kernel32.LocalFree(acl)
        if status != 0:
            raise PrivateStorageError(
                "private_storage_permission",
                reason="acl_apply_failed",
                native_error=int(status),
            )

    def owner_is_trusted(self, handle: wintypes.HANDLE) -> bool:
        owner = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        status = self.advapi32.GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if status != 0:
            return False
        try:
            if not owner.value:
                return False
            return any(
                self._sid_matches(owner, expected)
                for expected in (
                    self._user_sid,
                    self._system_sid,
                    self._administrators_sid,
                )
            )
        finally:
            if descriptor.value:
                self.kernel32.LocalFree(descriptor)

    def _sid_matches(self, sid: ctypes.c_void_p, expected: bytes) -> bool:
        if expected == self._user_sid:
            buffer = self._user_sid_buffer
        elif expected == self._system_sid:
            buffer = self._system_sid_buffer
        else:
            buffer = self._administrators_sid_buffer
        return bool(
            self.advapi32.EqualSid(
                sid,
                ctypes.cast(buffer, ctypes.c_void_p),
            )
        )

    def private_dacl_matches(self, handle: wintypes.HANDLE) -> bool:
        acl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        status = self.advapi32.GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(acl),
            None,
            ctypes.byref(descriptor),
        )
        if status != 0:
            return False
        try:
            if not descriptor.value or not acl.value:
                return False
            control = wintypes.WORD(0)
            revision = wintypes.DWORD(0)
            if not self.advapi32.GetSecurityDescriptorControl(
                descriptor,
                ctypes.byref(control),
                ctypes.byref(revision),
            ):
                return False
            if not control.value & _SE_DACL_PRESENT:
                return False
            if not control.value & _SE_DACL_PROTECTED:
                return False
            size_info = _ACL_SIZE_INFORMATION()
            if not self.advapi32.GetAclInformation(
                acl,
                ctypes.byref(size_info),
                ctypes.sizeof(size_info),
                _ACL_SIZE_INFORMATION_CLASS,
            ):
                return False
            user_present = False
            system_present = False
            for index in range(int(size_info.AceCount)):
                ace = ctypes.c_void_p()
                if not self.advapi32.GetAce(
                    acl,
                    index,
                    ctypes.byref(ace),
                ):
                    return False
                if not ace.value:
                    return False
                # Deny, unknown, inherit-only and partial-access ACEs all fail
                # closed; the private DACL this module publishes contains only
                # full-access allow ACEs for the current user and SYSTEM.
                sid = _private_allowed_ace_sid(ace.value)
                if sid is None:
                    return False
                if self._sid_matches(sid, self._user_sid):
                    user_present = True
                elif self._sid_matches(sid, self._system_sid):
                    system_present = True
                else:
                    return False
            return user_present and system_present
        finally:
            if descriptor.value:
                self.kernel32.LocalFree(descriptor)


class WindowsPrivateStorage:
    """Enforce and inspect a protected local-NTFS DACL."""

    def __init__(
        self,
        *,
        paths: WindowsPathService | None = None,
        native: _WindowsPrivateStorageNative | None = None,
    ):
        try:
            self._native = native if native is not None else _WindowsPrivateStorageNative()
            self._paths = paths if paths is not None else WindowsPathService()
        except OSError as exc:
            raise PrivateStorageError(
                "private_storage_unavailable",
                reason="platform",
                native_error=_native_code(exc),
            ) from None

    def _describe(self, path: str | PathLike[str]):
        try:
            return self._paths.describe(path)
        except PathIdentityError as exc:
            raise _translate_identity(exc) from None

    def _assert_directory(self, handle: wintypes.HANDLE) -> None:
        attributes = self._native.attributes(handle)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="reparse_point",
            )
        if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="not_directory",
            )

    def _assert_regular_file(self, handle: wintypes.HANDLE) -> None:
        attributes = self._native.attributes(handle)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="reparse_point",
            )
        if attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="not_regular",
            )

    def _require_private_owner(self, handle: wintypes.HANDLE) -> None:
        if not self._native.owner_is_trusted(handle):
            raise PrivateStorageError(
                "private_storage_permission",
                reason="not_owner",
            )

    def _secure_handle(self, handle: wintypes.HANDLE) -> None:
        self._require_private_owner(handle)
        self._native.apply_private_dacl(handle)
        if not self._native.private_dacl_matches(handle):
            raise PrivateStorageError(
                "private_storage_verification",
                reason="acl",
            )

    def _open_or_create_directory(
        self,
        operational_path: str,
        *,
        final: bool,
    ) -> tuple[wintypes.HANDLE, bool]:
        # Pre-existing ancestors are only inspected: a normal user holds no
        # WRITE_DAC on C:\ or C:\Users, and those objects must never be re-ACLed.
        # Only newly created directories and the exact requested directory ask
        # for the DACL write right.
        try:
            return (
                self._native.open_existing(
                    operational_path,
                    write_dacl=final,
                ),
                False,
            )
        except OSError as exc:
            if not _is_missing(exc):
                raise PrivateStorageError(
                    "private_storage_path_rejected",
                    reason="directory_unavailable",
                    native_error=_native_code(exc),
                ) from None
        try:
            self._native.create_directory(operational_path)
        except OSError as exc:
            if _native_code(exc) != _ERROR_ALREADY_EXISTS:
                raise PrivateStorageError(
                    "private_storage_permission",
                    reason="create_failed",
                    native_error=_native_code(exc),
                ) from None
        try:
            return (
                self._native.open_existing(
                    operational_path,
                    write_dacl=True,
                ),
                True,
            )
        except OSError as exc:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="directory_unavailable",
                native_error=_native_code(exc),
            ) from None

    def ensure_directory(self, path: str | PathLike[str]) -> None:
        absolute = _coerce_target(path)
        prefixes = _prefixes(absolute)
        if len(prefixes) == 1:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="filesystem_root",
            )
        for index, lexical in enumerate(prefixes):
            is_final = index == len(prefixes) - 1
            identity = self._describe(lexical)
            handle, created = self._open_or_create_directory(
                identity.operational_path,
                final=is_final,
            )
            try:
                self._assert_directory(handle)
                if created or is_final:
                    # Created directories and the exact requested directory are
                    # secured; unrelated pre-existing ancestors are not.
                    self._secure_handle(handle)
            finally:
                self._native.close(handle)

    def ensure_file(self, path: str | PathLike[str]) -> None:
        absolute = _coerce_target(path)
        prefixes = _prefixes(absolute)
        if len(prefixes) == 1:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="filesystem_root",
            )
        identity = self._describe(absolute)
        try:
            handle = self._native.open_existing(
                identity.operational_path,
                write_dacl=True,
            )
        except OSError as exc:
            if _is_missing(exc):
                raise _translate_missing("file") from None
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="file_unavailable",
                native_error=_native_code(exc),
            ) from None
        try:
            self._assert_regular_file(handle)
            self._secure_handle(handle)
        finally:
            self._native.close(handle)

    def verify(self, path: str | PathLike[str]) -> bool:
        try:
            absolute = _coerce_target(path)
            identity = self._describe(absolute)
        except PrivateStorageError:
            return False
        try:
            handle = self._native.open_existing(identity.operational_path)
        except OSError:
            return False
        try:
            attributes = self._native.attributes(handle)
            if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                return False
            return self._native.private_dacl_matches(handle)
        except OSError:
            return False
        finally:
            self._native.close(handle)


class WindowsAtomicPublisher:
    """Publish bytes with one ``MoveFileExW`` replace step."""

    def __init__(
        self,
        *,
        storage: WindowsPrivateStorage | None = None,
        replace=None,
    ):
        self._storage = storage if storage is not None else WindowsPrivateStorage()
        self._native = self._storage._native
        self._replace = replace

    def _reject_unusable_target(self, operational_path: str) -> None:
        try:
            handle = self._native.open_existing(operational_path)
        except OSError as exc:
            if _is_missing(exc):
                return
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="target_unavailable",
                native_error=_native_code(exc),
            ) from None
        try:
            attributes = self._native.attributes(handle)
        finally:
            self._native.close(handle)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="reparse_point",
            )
        if attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="not_regular",
            )

    def _discard_temporary(self, operational_path: str) -> None:
        # Only called for a temporary this call created and received a handle
        # for; a failed create, a colliding name or the pre-existing target is
        # never removed by cleanup.
        try:
            self._native.delete_file(operational_path)
        except OSError:
            pass

    def write_bytes(self, path: str | PathLike[str], data: bytes) -> None:
        payload = _coerce_payload(data)
        target = _coerce_target(path)
        prefixes = _prefixes(target)
        if len(prefixes) == 1:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="filesystem_root",
            )
        directory = ntpath.dirname(target)
        self._storage.ensure_directory(directory)
        directory_identity = self._storage._describe(directory)
        target_identity = self._storage._describe(target)
        self._reject_unusable_target(target_identity.operational_path)
        directory_operational = directory_identity.operational_path
        temp_name = ""
        handle = None
        published = False
        try:
            for _attempt in range(_TEMP_ATTEMPTS):
                candidate = _TEMP_PREFIX + os.urandom(8).hex()
                candidate_path = ntpath.join(
                    directory_operational,
                    candidate,
                )
                try:
                    handle = self._native.create_private_file(candidate_path)
                except OSError as exc:
                    if _native_code(exc) in _TEMPORARY_NAME_COLLISIONS:
                        # The exclusive name already existed; take another one.
                        continue
                    # A failed create is not proof that this call created the
                    # candidate, so the publisher never deletes it.
                    # create_private_file already removes the file it created
                    # itself when its privacy application fails.
                    raise
                temp_name = candidate
                break
            else:
                raise PrivateStorageError(
                    "private_storage_unavailable",
                    reason="temporary_name_exhausted",
                )
            self._native.write_all(handle, payload)
            self._native.flush(handle)
            if not self._native.private_dacl_matches(handle):
                raise PrivateStorageError(
                    "private_storage_verification",
                    reason="acl",
                )
            self._native.close(handle)
            handle = None
            source = ntpath.join(directory_operational, temp_name)
            replace = (
                self._replace
                if self._replace is not None
                else self._native.move_file_replace
            )
            replace(source, target_identity.operational_path)
            published = True
        finally:
            if handle is not None:
                try:
                    self._native.close(handle)
                except OSError:
                    pass
            if temp_name and not published:
                self._discard_temporary(
                    ntpath.join(directory_operational, temp_name)
                )
        if not self._storage.verify(target):
            raise PrivateStorageError(
                "private_storage_verification",
                reason="published_target",
            )
