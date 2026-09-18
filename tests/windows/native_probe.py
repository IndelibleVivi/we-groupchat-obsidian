"""Independent native evidence helpers for the W0.2B.2 Windows tests.

These helpers intentionally use a different API surface from the production
backend: ``GetFileSecurityW`` plus SDDL rendering of the stored descriptor
for DACL evidence, and the ``mklink /J`` shell built-in for the reparse
case. A Windows assertion here is therefore real platform evidence instead of
a restatement of the implementation under test.

The module imports on every platform; ``WindowsNativeProbe`` can only be
constructed on Windows.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import subprocess


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


class WindowsNativeProbe:
    def __init__(self):
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise OSError("Windows native probe is unavailable")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._bind()

    def _bind(self) -> None:
        self.kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self.kernel32.LocalFree.restype = wintypes.HLOCAL

        self.advapi32.GetFileSecurityW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetFileSecurityW.restype = wintypes.BOOL
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

    def sddl(self, path) -> str:
        """Render the stored owner/DACL, without aclapi inheritance conversion.

        Native CI proved GetNamedSecurityInfo can clear displayed INHERITED_ACE
        flags after a parent's inheritance model changes even when the child's
        stored descriptor is byte-identical. Read the stored descriptor first
        so these assertions compare the object actually being protected.
        """
        information = _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION
        descriptor = ctypes.create_string_buffer(self.descriptor_bytes(path))
        text = wintypes.LPWSTR()
        length = wintypes.DWORD(0)
        if not self.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, _SDDL_REVISION_1, information,
            ctypes.byref(text), ctypes.byref(length),
        ):
            raise ctypes.WinError(self._last_error())
        try:
            return str(text.value)
        finally:
            self.kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def descriptor_bytes(self, path) -> bytes:
        """Read the stored owner/DACL descriptor independently of aclapi SDDL."""
        target = os.fspath(path)
        information = _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION
        size = wintypes.DWORD(0)
        self.advapi32.GetFileSecurityW(target, information, None, 0, ctypes.byref(size))
        if self._last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise ctypes.WinError(self._last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not self.advapi32.GetFileSecurityW(
            target, information, buffer, len(buffer), ctypes.byref(size),
        ):
            raise ctypes.WinError(self._last_error())
        return bytes(buffer[:size.value])

    def grant_everyone(self, path, *, directory: bool) -> None:
        """Replace the DACL with one broad ``Everyone`` allow ACE."""
        target = os.path.abspath(os.fspath(path))
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
                target,
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

    def create_directory_junction(self, link_path, target_path) -> None:
        """Create a real NTFS directory junction; needs no elevated privilege.

        Uses the same ``mklink /J`` route as the Windows path-identity
        fixtures: the shell built-in creates a genuine mount-point reparse
        point through the NTFS driver, so the rejection evidence stays native
        while a standard user can still build the fixture. This replaces the
        former hand-built ``FSCTL_SET_REPARSE_POINT`` buffer, which was never
        exercised by CI and failed with ``ERROR_INVALID_REPARSE_DATA`` on a
        standard-user host.
        """
        command_processor = os.environ.get("COMSPEC")
        if not command_processor:
            raise OSError("COMSPEC is unavailable for the junction fixture")
        completed = subprocess.run(
            [
                command_processor,
                "/d",
                "/c",
                "mklink",
                "/J",
                os.path.abspath(os.fspath(link_path)),
                os.path.abspath(os.fspath(target_path)),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(
                "directory junction fixture failed: "
                f"returncode={completed.returncode}"
            )
