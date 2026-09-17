"""Independent native evidence helpers for the W0.2B.2 Windows tests.

These helpers intentionally use a different API surface from the production
backend: the named security descriptor APIs plus SDDL rendering for DACL
evidence, and a raw ``FSCTL_SET_REPARSE_POINT`` mount point for the reparse
case. A Windows assertion here is therefore real platform evidence instead of
a restatement of the implementation under test.

The module imports on every platform; ``WindowsNativeProbe`` can only be
constructed on Windows.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_SDDL_REVISION_1 = 1

_FILE_ALL_ACCESS = 0x001F01FF
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_SET_ACCESS = 0x02
_TRUSTEE_IS_SID = 0
_TRUSTEE_IS_USER = 1

_WIN_WORLD_SID = 1
_SECURITY_MAX_SID_SIZE = 68

_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_FSCTL_SET_REPARSE_POINT = 0x000900A4


class _TrusteeW(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", ctypes.c_void_p),
    ]


class _ExplicitAccessW(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", wintypes.DWORD),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", _TrusteeW),
    ]


class _MountPointReparseData(ctypes.Structure):
    _fields_ = [
        ("ReparseTag", wintypes.DWORD),
        ("ReparseDataLength", wintypes.WORD),
        ("Reserved", wintypes.WORD),
        ("SubstituteNameOffset", wintypes.WORD),
        ("SubstituteNameLength", wintypes.WORD),
        ("PrintNameOffset", wintypes.WORD),
        ("PrintNameLength", wintypes.WORD),
    ]


class WindowsNativeProbe:
    def __init__(self):
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise OSError("Windows native probe is unavailable")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._bind()

    def _bind(self) -> None:
        handle = wintypes.HANDLE
        self.kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self.kernel32.LocalFree.restype = wintypes.HLOCAL
        self.kernel32.CloseHandle.argtypes = [handle]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
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
        self.kernel32.DeviceIoControl.argtypes = [
            handle,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        self.kernel32.DeviceIoControl.restype = wintypes.BOOL

        self.advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.advapi32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        self.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = (
            wintypes.BOOL
        )
        self.advapi32.CreateWellKnownSid.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.CreateWellKnownSid.restype = wintypes.BOOL
        self.advapi32.SetEntriesInAclW.argtypes = [
            wintypes.ULONG,
            ctypes.POINTER(_ExplicitAccessW),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi32.SetEntriesInAclW.restype = wintypes.DWORD

    @staticmethod
    def _last_error() -> int:
        return int(ctypes.get_last_error())

    def sddl(self, path: str) -> str:
        """Render the owner plus DACL of a named object as SDDL."""
        information = _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION
        descriptor = ctypes.c_void_p()
        status = self.advapi32.GetNamedSecurityInfoW(
            path,
            _SE_FILE_OBJECT,
            information,
            None,
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if status != 0:
            raise ctypes.WinError(int(status))
        try:
            text = wintypes.LPWSTR()
            length = wintypes.DWORD(0)
            if not self.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor,
                _SDDL_REVISION_1,
                information,
                ctypes.byref(text),
                ctypes.byref(length),
            ):
                raise ctypes.WinError(self._last_error())
            try:
                return str(text.value)
            finally:
                self.kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            if descriptor.value:
                self.kernel32.LocalFree(descriptor)

    def grant_everyone(self, path: str, *, directory: bool) -> None:
        """Replace the DACL with one broad ``Everyone`` allow ACE."""
        size = wintypes.DWORD(_SECURITY_MAX_SID_SIZE)
        everyone = ctypes.create_string_buffer(_SECURITY_MAX_SID_SIZE)
        if not self.advapi32.CreateWellKnownSid(
            _WIN_WORLD_SID,
            None,
            everyone,
            ctypes.byref(size),
        ):
            raise ctypes.WinError(self._last_error())
        entries = (_ExplicitAccessW * 1)()
        inheritance = (
            _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE if directory else 0
        )
        entries[0] = _ExplicitAccessW(
            grfAccessPermissions=_FILE_ALL_ACCESS,
            grfAccessMode=_SET_ACCESS,
            grfInheritance=inheritance,
            Trustee=_TrusteeW(
                pMultipleTrustee=None,
                MultipleTrusteeOperation=0,
                TrusteeForm=_TRUSTEE_IS_SID,
                TrusteeType=_TRUSTEE_IS_USER,
                ptstrName=ctypes.cast(everyone, ctypes.c_void_p),
            ),
        )
        acl = ctypes.c_void_p()
        status = self.advapi32.SetEntriesInAclW(
            1,
            entries,
            None,
            ctypes.byref(acl),
        )
        if status != 0:
            raise ctypes.WinError(int(status))
        try:
            status = self.advapi32.SetNamedSecurityInfoW(
                os.path.abspath(path),
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION,
                None,
                None,
                acl,
                None,
            )
        finally:
            self.kernel32.LocalFree(acl)
        if status != 0:
            raise ctypes.WinError(int(status))

    def create_reparse_point(self, link_path: str, target_path: str) -> None:
        """Create a mount-point junction; needs no elevated privilege."""
        substitute = "\\??\\" + os.path.abspath(target_path)
        display = os.path.abspath(target_path)
        path_buffer = ctypes.create_unicode_buffer(substitute + display)
        data = _MountPointReparseData(
            ReparseTag=_IO_REPARSE_TAG_MOUNT_POINT,
            ReparseDataLength=8 + (ctypes.sizeof(path_buffer) - 2),
            Reserved=0,
            SubstituteNameOffset=0,
            SubstituteNameLength=len(substitute) * 2,
            PrintNameOffset=len(substitute) * 2,
            PrintNameLength=len(display) * 2,
        )
        buffer_size = ctypes.sizeof(data) + ctypes.sizeof(path_buffer) - 2
        buffer = ctypes.create_string_buffer(buffer_size)
        ctypes.memmove(buffer, ctypes.byref(data), ctypes.sizeof(data))
        ctypes.memmove(
            ctypes.byref(buffer, ctypes.sizeof(data)),
            path_buffer,
            ctypes.sizeof(path_buffer) - 2,
        )
        handle = self.kernel32.CreateFileW(
            os.path.abspath(link_path),
            _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(self._last_error())
        try:
            returned = wintypes.DWORD(0)
            if not self.kernel32.DeviceIoControl(
                handle,
                _FSCTL_SET_REPARSE_POINT,
                buffer,
                buffer_size,
                None,
                0,
                ctypes.byref(returned),
                None,
            ):
                raise ctypes.WinError(self._last_error())
        finally:
            self.kernel32.CloseHandle(handle)
