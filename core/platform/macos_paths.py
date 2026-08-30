"""macOS path identity provider for the W0.2B.1 platform boundary."""
from __future__ import annotations

import ctypes
import hashlib
import os
from os import PathLike
import unicodedata

from .contracts import PathIdentity, PathIdentityError


_DARWIN_PC_CASE_SENSITIVE = 11
_NORMALIZATION_INSENSITIVE_FILESYSTEMS = frozenset({"apfs"})


class _DarwinFSID(ctypes.Structure):
    _fields_ = [("values", ctypes.c_int32 * 2)]


class _DarwinStatFS(ctypes.Structure):
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", _DarwinFSID),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_reserved", ctypes.c_uint32 * 8),
    ]


def _coerce_path(path: str | PathLike[str]) -> str:
    value = os.fspath(path)
    if not isinstance(value, str):
        raise PathIdentityError("path_type")
    if not value:
        raise PathIdentityError("empty_path")
    if "\0" in value:
        raise PathIdentityError("nul_character")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise PathIdentityError("invalid_utf8_name") from None
    return value


def _identity_from_stat(value: os.stat_result) -> str:
    return f"macos-file:v1:{int(value.st_dev):x}:{int(value.st_ino):x}"


def _filesystem_case_sensitive(parent_path: str) -> bool:
    try:
        value = os.pathconf(parent_path, _DARWIN_PC_CASE_SENSITIVE)
    except (AttributeError, OSError, ValueError) as exc:
        raise PathIdentityError(
            "filesystem_case_sensitivity_unknown",
            native_error=getattr(exc, "errno", None),
        ) from None
    if value not in {0, 1}:
        raise PathIdentityError("filesystem_case_sensitivity_unknown")
    return bool(value)


def _filesystem_name(parent_path: str) -> str:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        statfs = libc["statfs$INODE64"]
    except (KeyError, AttributeError):
        statfs = libc["statfs"]
    statfs.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(_DarwinStatFS),
    ]
    statfs.restype = ctypes.c_int
    value = _DarwinStatFS()
    if statfs(os.fsencode(parent_path), ctypes.byref(value)) != 0:
        raise PathIdentityError(
            "filesystem_type_unknown",
            native_error=ctypes.get_errno(),
        )
    try:
        return bytes(value.f_fstypename).split(b"\0", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        raise PathIdentityError("filesystem_type_unknown") from None


def _missing_child_identity(
    parent: os.stat_result,
    leaf: str,
    *,
    case_sensitive: bool,
) -> str:
    comparison_name = unicodedata.normalize("NFD", leaf)
    if not case_sensitive:
        comparison_name = comparison_name.casefold()
    digest = hashlib.sha256(os.fsencode(comparison_name)).hexdigest()
    return (
        f"macos-child:v1:{int(parent.st_dev):x}:{int(parent.st_ino):x}:"
        f"{digest}"
    )


class MacOSPathService:
    """Describe existing paths or one missing final component on macOS."""

    def describe(
        self,
        path: str | PathLike[str],
        *,
        source_root: str | PathLike[str] | None = None,
    ) -> PathIdentity:
        configured = _coerce_path(path)
        display_path = os.path.abspath(configured)
        operational_path = os.path.realpath(display_path)

        try:
            value = os.stat(operational_path)
        except FileNotFoundError:
            parent_path = os.path.dirname(operational_path)
            leaf = os.path.basename(operational_path)
            if not leaf:
                raise PathIdentityError("missing_final_component") from None
            try:
                parent = os.stat(parent_path)
            except OSError as exc:
                raise PathIdentityError(
                    "missing_parent",
                    native_error=exc.errno,
                ) from None
            if not os.path.isdir(parent_path):
                raise PathIdentityError("parent_not_directory")
            if (
                _filesystem_name(parent_path).casefold()
                not in _NORMALIZATION_INSENSITIVE_FILESYSTEMS
            ):
                raise PathIdentityError(
                    "filesystem_normalization_unsupported"
                )
            identity_key = _missing_child_identity(
                parent,
                leaf,
                case_sensitive=_filesystem_case_sensitive(parent_path),
            )
        except OSError as exc:
            raise PathIdentityError(
                "stat_failed",
                native_error=exc.errno,
            ) from None
        else:
            identity_key = _identity_from_stat(value)

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

    @staticmethod
    def _source_relative_path(
        operational_path: str,
        source_root: str | PathLike[str],
    ) -> str:
        configured_root = _coerce_path(source_root)
        root_path = os.path.realpath(os.path.abspath(configured_root))
        try:
            root_value = os.stat(root_path)
        except OSError as exc:
            raise PathIdentityError(
                "source_root_unavailable",
                native_error=exc.errno,
            ) from None
        if not os.path.isdir(root_path):
            raise PathIdentityError("source_root_not_directory")
        try:
            common = os.path.commonpath((root_path, operational_path))
        except ValueError:
            raise PathIdentityError("source_root_escape") from None
        if common != root_path:
            raise PathIdentityError("source_root_escape")
        relative = os.path.relpath(operational_path, root_path)
        if relative == ".":
            return ""
        return relative.replace(os.sep, "/")
