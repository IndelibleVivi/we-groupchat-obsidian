"""Current-user Windows Credential Manager, via the native generic API.

No plaintext sidecar, roaming credential, subprocess or import-time OS access.
Credential blobs are application-defined UTF-8 (at most 2560 bytes).
"""
import ctypes
from ctypes import wintypes
import sys

from .contracts import SecretStoreError
from ..project_identity import KEYCHAIN_SERVICE_NAME

_GENERIC = 1
_LOCAL_MACHINE = 2  # Persists for this user on this computer, not all users.
_NOT_FOUND = 1168
_MAX_BLOB = 2560


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _native_api():
    if sys.platform != "win32":
        raise SecretStoreError("secret_store_unavailable")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.POINTER(_Credential)
    api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                             wintypes.DWORD, ctypes.POINTER(pointer)]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


class WindowsSecretStore:
    def __init__(self, *, service=KEYCHAIN_SERVICE_NAME):
        self.service = service

    def _target(self, account):
        # Accounts are internal identifiers, never user-provided paths.
        if not isinstance(account, str) or not account or "\x00" in account:
            raise SecretStoreError("secret_account_invalid")
        return f"{self.service}/{account}"

    def save(self, account: str, secret: str) -> None:
        payload = secret.encode("utf-8")
        if not payload:
            raise SecretStoreError("secret_empty")
        if len(payload) > _MAX_BLOB:
            raise SecretStoreError("secret_too_large")
        credential = _Credential()
        credential.Type = _GENERIC
        credential.TargetName = self._target(account)
        credential.UserName = self.service
        credential.Persist = _LOCAL_MACHINE
        blob = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        credential.CredentialBlobSize = len(payload)
        credential.CredentialBlob = blob
        try:
            if not _native_api().CredWriteW(ctypes.byref(credential), 0):
                raise SecretStoreError("secret_write_failed")
        finally:
            ctypes.memset(blob, 0, len(payload))

    def load(self, account: str) -> str | None:
        target = self._target(account)
        api = _native_api()
        pointer = ctypes.POINTER(_Credential)()
        if not api.CredReadW(target, _GENERIC, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == _NOT_FOUND:
                return None
            raise SecretStoreError("secret_store_unavailable")
        try:
            payload = ctypes.string_at(pointer.contents.CredentialBlob,
                                       pointer.contents.CredentialBlobSize)
            try:
                return payload.decode("utf-8")
            except UnicodeError:
                raise SecretStoreError("secret_record_invalid") from None
        finally:
            api.CredFree(pointer)

    def delete(self, account: str) -> None:
        target = self._target(account)
        if not _native_api().CredDeleteW(target, _GENERIC, 0):
            if ctypes.get_last_error() != _NOT_FOUND:
                raise SecretStoreError("secret_delete_failed")
