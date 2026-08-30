"""Windows path identity provider for the W0.2B.1 platform boundary."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import ntpath
import os
import re
from os import PathLike

from .contracts import PathIdentity, PathIdentityError, ReparsePointConflict


_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_CS_FLAG_CASE_SENSITIVE_DIR = 0x00000001
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_OPEN = 1
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_NAME_NORMALIZED = 0x0
_VOLUME_NAME_DOS = 0x0
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_ID_INFO_CLASS = 18
_FILE_CASE_SENSITIVE_INFO_CLASS = 23
_LCMAP_UPPERCASE = 0x00000200
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_INVALID_NAME = 123
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_WIN32_PATH_CHARS = 32768
_OBJ_CASE_INSENSITIVE = 0x00000040

_DRIVE_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:\\")
_DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:(?!\\)")
_RESERVED_NAMES = {
    "aux",
    "con",
    "conin$",
    "conout$",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
    "com¹",
    "com²",
    "com³",
    "lpt¹",
    "lpt²",
    "lpt³",
}


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", wintypes.BYTE * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", _FILE_ID_128),
    ]


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("ReparseTag", wintypes.DWORD),
    ]


class _FILE_CASE_SENSITIVE_INFO(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD)]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID),
        ("SecurityQualityOfService", wintypes.LPVOID),
    ]


class _IO_STATUS_VALUE(ctypes.Union):
    _fields_ = [
        ("Status", wintypes.LONG),
        ("Pointer", wintypes.LPVOID),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("value", _IO_STATUS_VALUE),
        ("Information", ctypes.c_size_t),
    ]


@dataclass(frozen=True)
class _NormalizedWindowsPath:
    display_path: str
    operational_path: str
    is_unc: bool


@dataclass(frozen=True)
class _ExistingWindowsPath:
    operational_path: str
    volume_serial: int
    file_id: bytes
    attributes: int
    reparse_tag: int
    filesystem_name: str
    case_sensitive: bool


class _NativePathError(OSError):
    def __init__(self, native_error: int):
        self.native_error = native_error
        super().__init__(native_error, "Windows path operation failed")


def _coerce_path(path: str | PathLike[str]) -> str:
    value = os.fspath(path)
    if not isinstance(value, str):
        raise PathIdentityError("path_type")
    if not value:
        raise PathIdentityError("empty_path")
    if "\0" in value:
        raise PathIdentityError("nul_character")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise PathIdentityError("unpaired_surrogate")
    return value


def _validate_component(component: str) -> None:
    if component in {".", ".."}:
        return
    if not component:
        raise PathIdentityError("empty_component")
    if component.endswith((" ", ".")):
        raise PathIdentityError("trailing_dot_or_space")
    if any(ord(character) < 32 for character in component):
        raise PathIdentityError("control_character")
    if any(character in '<>:"|?*' for character in component):
        raise PathIdentityError("reserved_character")
    stem = component.partition(".")[0].casefold()
    if stem in _RESERVED_NAMES:
        raise PathIdentityError("reserved_name")


def _validate_components(path: str) -> None:
    normalized = path.replace("/", "\\")
    if normalized.startswith("\\\\"):
        parts = normalized[2:].split("\\")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise PathIdentityError("unc_share_missing")
        components = parts[2:]
    else:
        drive, tail = ntpath.splitdrive(normalized)
        components = tail.split("\\")
        if drive and not re.fullmatch(r"[A-Za-z]:", drive):
            raise PathIdentityError("unsupported_namespace")
    for component in components:
        if component:
            _validate_component(component)


def _strip_extended_prefix(path: str) -> str:
    if path.casefold().startswith("\\\\?\\unc\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _trim_nonroot_trailing_separators(path: str) -> str:
    drive, tail = ntpath.splitdrive(path)
    if tail == "\\":
        return drive + "\\"
    return path.rstrip("\\")


def _to_extended_path(display_path: str) -> str:
    if display_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + display_path[2:]
    return "\\\\?\\" + display_path


def _normalize_windows_syntax(
    path: str | PathLike[str],
    *,
    get_full_path,
) -> _NormalizedWindowsPath:
    configured = _coerce_path(path)
    lowered = configured.casefold()
    if lowered.startswith("\\\\.\\"):
        raise PathIdentityError("device_namespace")
    if lowered.startswith("\\\\?\\"):
        if lowered.startswith("\\\\?\\unc\\"):
            configured = "\\\\" + configured[8:]
        elif re.match(r"^\\\\\?\\[A-Za-z]:\\", configured):
            configured = configured[4:]
        else:
            raise PathIdentityError("unsupported_extended_namespace")
    configured = configured.replace("/", "\\")
    if _DRIVE_RELATIVE_RE.match(configured):
        raise PathIdentityError("drive_relative_path")
    if configured.startswith("\\") and not configured.startswith("\\\\"):
        raise PathIdentityError("root_relative_path")
    _validate_components(configured)

    try:
        absolute = get_full_path(configured).replace("/", "\\")
    except _NativePathError as exc:
        raise PathIdentityError(
            "absolute_path_failed",
            native_error=exc.native_error,
        ) from None
    absolute = _trim_nonroot_trailing_separators(
        _strip_extended_prefix(absolute)
    )
    _validate_components(absolute)
    is_unc = absolute.startswith("\\\\")
    if not is_unc and not _DRIVE_ABSOLUTE_RE.match(absolute):
        raise PathIdentityError("absolute_path_required")
    return _NormalizedWindowsPath(
        display_path=absolute,
        operational_path=_to_extended_path(absolute),
        is_unc=is_unc,
    )


def _display_from_operational(path: str) -> str:
    display_path = _strip_extended_prefix(path)
    if display_path.startswith("\\\\") or _DRIVE_ABSOLUTE_RE.match(
        display_path
    ):
        return display_path
    raise PathIdentityError("unsupported_final_namespace")


def _local_path_chain(display_path: str) -> list[str]:
    drive, tail = ntpath.splitdrive(display_path)
    if not re.fullmatch(r"[A-Za-z]:", drive):
        raise PathIdentityError("local_drive_required")
    root = drive + "\\"
    components = [part for part in tail.split("\\") if part]
    paths = [root]
    current = root
    for component in components:
        current = ntpath.join(current, component)
        paths.append(current)
    return paths


def _identity_key(value: _ExistingWindowsPath) -> str:
    return (
        f"windows-file:v1:{value.volume_serial:016x}:"
        f"{value.file_id.hex()}"
    )


def _missing_child_identity(
    parent: _ExistingWindowsPath,
    folded_leaf: str,
) -> str:
    folded = folded_leaf.encode("utf-16le")
    digest = hashlib.sha256(folded).hexdigest()
    return (
        f"windows-child:v1:{parent.volume_serial:016x}:"
        f"{parent.file_id.hex()}:{digest}"
    )


class _WindowsNativePaths:
    def __init__(self):
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise OSError("Windows path APIs are unavailable")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll")
        self._bind()

    def _bind(self) -> None:
        self.kernel32.GetFullPathNameW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        self.kernel32.GetFullPathNameW.restype = wintypes.DWORD
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self.kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self.kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self.kernel32.GetVolumeInformationByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        self.kernel32.GetVolumeInformationByHandleW.restype = wintypes.BOOL
        self.kernel32.LCMapStringEx.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPARAM,
        ]
        self.kernel32.LCMapStringEx.restype = ctypes.c_int
        self.ntdll.NtCreateFile.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(_OBJECT_ATTRIBUTES),
            ctypes.POINTER(_IO_STATUS_BLOCK),
            wintypes.LPVOID,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.LPVOID,
            wintypes.ULONG,
        ]
        self.ntdll.NtCreateFile.restype = wintypes.LONG
        self.ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
        self.ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

    @staticmethod
    def _last_error() -> int:
        return int(ctypes.get_last_error())

    def get_full_path(self, path: str) -> str:
        buffer = ctypes.create_unicode_buffer(_MAX_WIN32_PATH_CHARS)
        result = self.kernel32.GetFullPathNameW(
            path,
            len(buffer),
            buffer,
            None,
        )
        if result == 0 or result >= len(buffer):
            raise _NativePathError(self._last_error())
        return buffer.value

    def open_existing(
        self,
        operational_path: str,
    ) -> tuple[_ExistingWindowsPath, wintypes.HANDLE]:
        handle = self.kernel32.CreateFileW(
            operational_path,
            0,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise _NativePathError(self._last_error())
        try:
            return self._describe_handle(handle), handle
        except BaseException:
            self.kernel32.CloseHandle(handle)
            raise

    def open_relative(
        self,
        parent_handle: wintypes.HANDLE,
        component: str,
    ) -> tuple[_ExistingWindowsPath, wintypes.HANDLE]:
        buffer = ctypes.create_unicode_buffer(component)
        character_size = ctypes.sizeof(ctypes.c_wchar)
        name = _UNICODE_STRING(
            Length=(len(buffer) - 1) * character_size,
            MaximumLength=len(buffer) * character_size,
            Buffer=ctypes.cast(buffer, wintypes.LPWSTR),
        )
        attributes = _OBJECT_ATTRIBUTES(
            Length=ctypes.sizeof(_OBJECT_ATTRIBUTES),
            RootDirectory=parent_handle,
            ObjectName=ctypes.pointer(name),
            Attributes=_OBJ_CASE_INSENSITIVE,
            SecurityDescriptor=None,
            SecurityQualityOfService=None,
        )
        io_status = _IO_STATUS_BLOCK()
        handle = wintypes.HANDLE()
        status = self.ntdll.NtCreateFile(
            ctypes.byref(handle),
            _FILE_READ_ATTRIBUTES,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            _FILE_OPEN,
            _FILE_OPEN_REPARSE_POINT,
            None,
            0,
        )
        if status < 0:
            native_error = int(
                self.ntdll.RtlNtStatusToDosError(status)
            )
            raise _NativePathError(native_error)
        try:
            return self._describe_handle(handle), handle
        except BaseException:
            self.kernel32.CloseHandle(handle)
            raise

    def close_existing(self, handle: wintypes.HANDLE) -> None:
        self.kernel32.CloseHandle(handle)

    def refresh_existing(
        self,
        handle: wintypes.HANDLE,
    ) -> _ExistingWindowsPath:
        return self._describe_handle(handle)

    def _describe_handle(
        self,
        handle: wintypes.HANDLE,
    ) -> _ExistingWindowsPath:
        tag_info = self._query(
            handle,
            _FILE_ATTRIBUTE_TAG_INFO_CLASS,
            _FILE_ATTRIBUTE_TAG_INFO,
        )
        file_id_info = self._query(
            handle,
            _FILE_ID_INFO_CLASS,
            _FILE_ID_INFO,
        )
        case_sensitive = False
        if tag_info.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
            case_info = self._query(
                handle,
                _FILE_CASE_SENSITIVE_INFO_CLASS,
                _FILE_CASE_SENSITIVE_INFO,
            )
            case_sensitive = bool(
                case_info.Flags & _FILE_CS_FLAG_CASE_SENSITIVE_DIR
            )
        return _ExistingWindowsPath(
            operational_path=self._final_path(handle),
            volume_serial=int(file_id_info.VolumeSerialNumber),
            file_id=bytes(file_id_info.FileId.Identifier),
            attributes=int(tag_info.FileAttributes),
            reparse_tag=int(tag_info.ReparseTag),
            filesystem_name=self._filesystem_name(handle),
            case_sensitive=case_sensitive,
        )

    def fold_file_name(self, value: str) -> str:
        required = self.kernel32.LCMapStringEx(
            "",
            _LCMAP_UPPERCASE,
            value,
            len(value),
            None,
            0,
            None,
            None,
            0,
        )
        if required == 0:
            raise _NativePathError(self._last_error())
        buffer = ctypes.create_unicode_buffer(required + 1)
        result = self.kernel32.LCMapStringEx(
            "",
            _LCMAP_UPPERCASE,
            value,
            len(value),
            buffer,
            required,
            None,
            None,
            0,
        )
        if result == 0:
            raise _NativePathError(self._last_error())
        return buffer[:result]

    def _query(self, handle, info_class: int, structure_type):
        value = structure_type()
        if not self.kernel32.GetFileInformationByHandleEx(
            handle,
            info_class,
            ctypes.byref(value),
            ctypes.sizeof(value),
        ):
            raise _NativePathError(self._last_error())
        return value

    def _final_path(self, handle) -> str:
        required = self.kernel32.GetFinalPathNameByHandleW(
            handle,
            None,
            0,
            _FILE_NAME_NORMALIZED | _VOLUME_NAME_DOS,
        )
        if required == 0:
            raise _NativePathError(self._last_error())
        buffer = ctypes.create_unicode_buffer(required + 1)
        result = self.kernel32.GetFinalPathNameByHandleW(
            handle,
            buffer,
            len(buffer),
            _FILE_NAME_NORMALIZED | _VOLUME_NAME_DOS,
        )
        if result == 0 or result >= len(buffer):
            raise _NativePathError(self._last_error())
        return buffer.value

    def _filesystem_name(self, handle) -> str:
        filesystem = ctypes.create_unicode_buffer(64)
        if not self.kernel32.GetVolumeInformationByHandleW(
            handle,
            None,
            0,
            None,
            None,
            None,
            filesystem,
            len(filesystem),
        ):
            raise _NativePathError(self._last_error())
        return filesystem.value


class WindowsPathService:
    """Describe local NTFS paths without shell-style path transformation."""

    def __init__(self, native: _WindowsNativePaths | None = None):
        self._native = native or _WindowsNativePaths()

    def describe(
        self,
        path: str | PathLike[str],
        *,
        source_root: str | PathLike[str] | None = None,
    ) -> PathIdentity:
        normalized = _normalize_windows_syntax(
            path,
            get_full_path=self._native.get_full_path,
        )
        if normalized.is_unc:
            raise PathIdentityError("remote_path_unsupported")

        target, exists = self._resolve_local(normalized.display_path)
        if exists:
            operational_path = target.operational_path
            identity_key = _identity_key(target)
        else:
            leaf = ntpath.basename(normalized.display_path)
            if not leaf:
                raise PathIdentityError("missing_final_component")
            operational_path = ntpath.join(target.operational_path, leaf)
            try:
                folded_leaf = self._native.fold_file_name(leaf)
            except _NativePathError as exc:
                raise PathIdentityError(
                    "case_mapping_failed",
                    native_error=exc.native_error,
                ) from None
            identity_key = _missing_child_identity(target, folded_leaf)

        display_path = _display_from_operational(operational_path)
        source_relative_path = ""
        if source_root is not None:
            source_relative_path = self._source_relative_path(
                operational_path,
                source_root,
            )
        return PathIdentity(
            display_path=display_path,
            operational_path=operational_path,
            identity_key=identity_key,
            source_relative_path=source_relative_path,
        )

    def _resolve_local(
        self,
        display_path: str,
    ) -> tuple[_ExistingWindowsPath, bool]:
        chain = _local_path_chain(display_path)
        last_index = len(chain) - 1
        parent: _ExistingWindowsPath | None = None
        held_handles: list[wintypes.HANDLE] = []
        try:
            for index, lexical_path in enumerate(chain):
                try:
                    if not held_handles:
                        current, handle = self._native.open_existing(
                            _to_extended_path(lexical_path)
                        )
                    else:
                        current, handle = self._native.open_relative(
                            held_handles[-1],
                            ntpath.basename(lexical_path),
                        )
                except _NativePathError as exc:
                    if (
                        index == last_index
                        and parent is not None
                        and exc.native_error
                        in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}
                    ):
                        parent = self._native.refresh_existing(
                            held_handles[-1]
                        )
                        self._validate_existing(parent)
                        return parent, False
                    reason = (
                        "invalid_name"
                        if exc.native_error == _ERROR_INVALID_NAME
                        else "path_unavailable"
                    )
                    raise PathIdentityError(
                        reason,
                        native_error=exc.native_error,
                    ) from None
                held_handles.append(handle)
                self._validate_existing(current)
                if index < last_index and not (
                    current.attributes & _FILE_ATTRIBUTE_DIRECTORY
                ):
                    raise PathIdentityError("ancestor_not_directory")
                parent = current
            if parent is None:
                raise PathIdentityError("path_unavailable")
            return parent, True
        finally:
            for handle in reversed(held_handles):
                self._native.close_existing(handle)

    @staticmethod
    def _validate_existing(value: _ExistingWindowsPath) -> None:
        if value.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ReparsePointConflict(
                f"tag_{value.reparse_tag:08x}"
            )
        if value.case_sensitive:
            raise PathIdentityError("case_sensitive_directory_unsupported")
        if value.filesystem_name.casefold() != "ntfs":
            raise PathIdentityError("unsupported_filesystem")

    def _source_relative_path(
        self,
        operational_path: str,
        source_root: str | PathLike[str],
    ) -> str:
        normalized_root = _normalize_windows_syntax(
            source_root,
            get_full_path=self._native.get_full_path,
        )
        if normalized_root.is_unc:
            raise PathIdentityError("remote_source_root_unsupported")
        root, exists = self._resolve_local(normalized_root.display_path)
        if not exists:
            raise PathIdentityError("source_root_unavailable")
        if not root.attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise PathIdentityError("source_root_not_directory")

        target_display = _display_from_operational(operational_path)
        root_display = _display_from_operational(root.operational_path)
        target_drive, _ = ntpath.splitdrive(target_display)
        root_drive, _ = ntpath.splitdrive(root_display)
        if target_drive.casefold() != root_drive.casefold():
            raise PathIdentityError("source_root_escape")
        relative = ntpath.relpath(target_display, root_display)
        if relative == ".":
            return ""
        if relative == ".." or relative.startswith("..\\"):
            raise PathIdentityError("source_root_escape")
        return relative.replace("\\", "/")
