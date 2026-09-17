"""macOS private storage and atomic byte publication for the W0.2B.2 boundary.

The concrete backend enforces ``0700`` application-owned directories and
``0600`` regular files owned by the current UID. Directories that this module
creates, plus the exact requested directory, are forced private; pre-existing
ancestors are never re-permissioned. The endpoint of every operation is opened
with ``O_NOFOLLOW`` so a symbolic link is rejected instead of followed, while
symlinked ancestors (for example ``/var``) still resolve normally.

Publication writes one same-directory private temporary file, flushes and
``fsync``s it, and publishes it with a single ``os.replace``. Nothing is
deleted before the replace, so a failure ahead of the replace preserves the
previous target bytes and a symbolic-link, directory or otherwise non-regular
target is rejected before any payload byte is written.
"""
from __future__ import annotations

import errno
import os
from os import PathLike
import stat

from .contracts import PrivateStorageError


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
PRIVACY_AUTHORITY = "posix-mode"

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_NOFOLLOW_DIRECTORY_FLAGS = _DIRECTORY_FLAGS | getattr(os, "O_NOFOLLOW", 0)
# ``O_NONBLOCK`` keeps a non-regular endpoint such as a FIFO from blocking this
# process while the endpoint type is re-checked through the descriptor.
_NOFOLLOW_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_CREATE_TEMP_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

_TEMP_PREFIX = ".wgo-publish-"
_TEMP_ATTEMPTS = 128
_WRITE_CHUNK_BYTES = 1 << 20


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
            native_error=exc.errno,
        ) from None


def _components(absolute_path: str) -> list[str]:
    _drive, tail = os.path.splitdrive(absolute_path)
    return [part for part in tail.split(os.sep) if part]


def _coerce_payload(data: bytes) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, (bytearray, memoryview)):
        return bytes(data)
    raise TypeError("atomic publication payload must be bytes")


def _translate(exc: OSError, *, missing_reason: str = "path") -> PrivateStorageError:
    if isinstance(exc, PrivateStorageError):
        return exc
    native_error = exc.errno
    if native_error == errno.ENOENT:
        return PrivateStorageError(
            "private_storage_missing",
            reason=missing_reason,
            native_error=native_error,
        )
    if native_error in (errno.EACCES, errno.EPERM):
        return PrivateStorageError(
            "private_storage_permission",
            reason="access_denied",
            native_error=native_error,
        )
    if native_error == errno.ELOOP:
        reason = "symbolic_link"
    elif native_error == errno.ENOTDIR:
        reason = "not_directory"
    elif native_error in (errno.EISDIR, errno.ENXIO):
        reason = "not_regular"
    else:
        reason = "io_error"
    return PrivateStorageError(
        "private_storage_path_rejected",
        reason=reason,
        native_error=native_error,
    )


def _directory_open_failure(
    component: str,
    parent_fd: int,
    exc: OSError,
) -> PrivateStorageError:
    """Classify a directory open failure, naming a symbolic link explicitly.

    macOS reports ``ENOTDIR`` rather than ``ELOOP`` when ``O_DIRECTORY`` meets a
    symbolic link, so the entry type is re-checked without following it.
    """
    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
        try:
            info = os.lstat(component, dir_fd=parent_fd)
        except OSError:
            return _translate(exc)
        if stat.S_ISLNK(info.st_mode):
            return PrivateStorageError(
                "private_storage_path_rejected",
                reason="symbolic_link",
                native_error=exc.errno,
            )
    return _translate(exc)


def _open_directory_chain(
    components: list[str],
    *,
    create: bool,
    nofollow_final: bool = True,
    recover_final: bool = False,
) -> tuple[int, list[str]]:
    """Open every directory component from the filesystem root.

    The endpoint is opened with ``O_NOFOLLOW`` when ``nofollow_final`` is set.
    Directories created by this call are immediately forced to 0700; every
    other existing component is only inspected. Returns the endpoint
    descriptor plus the display names this call created.
    """
    directory_fd = os.open(os.sep, _DIRECTORY_FLAGS)
    created: list[str] = []
    current = os.sep
    last_index = len(components) - 1
    try:
        for index, component in enumerate(components):
            current = os.path.join(current, component)
            final = index == last_index
            flags = (
                _NOFOLLOW_DIRECTORY_FLAGS
                if final and nofollow_final
                else _DIRECTORY_FLAGS
            )
            try:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    raise PrivateStorageError(
                        "private_storage_missing",
                        reason="directory",
                        native_error=errno.ENOENT,
                    ) from None
                try:
                    os.mkdir(
                        component,
                        PRIVATE_DIRECTORY_MODE,
                        dir_fd=directory_fd,
                    )
                except FileExistsError:
                    raise PrivateStorageError(
                        "private_storage_path_rejected",
                        reason="create_conflict",
                    ) from None
                except OSError as exc:
                    raise _translate(exc) from None
                try:
                    child_fd = os.open(component, flags, dir_fd=directory_fd)
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EPERM):
                        raise _directory_open_failure(
                            component,
                            directory_fd,
                            exc,
                        ) from None
                    child_fd = _reopen_created_directory(
                        component,
                        directory_fd,
                        flags,
                    )
                try:
                    _force_private_fd(child_fd, directory=True)
                except BaseException:
                    os.close(child_fd)
                    raise
                created.append(current)
            except OSError as exc:
                if final and recover_final and exc.errno in (
                    errno.EACCES,
                    errno.EPERM,
                ):
                    child_fd = _reopen_created_directory(
                        component,
                        directory_fd,
                        flags,
                    )
                else:
                    raise _directory_open_failure(
                        component,
                        directory_fd,
                        exc,
                    ) from None
            os.close(directory_fd)
            directory_fd = child_fd
        return directory_fd, created
    except BaseException:
        os.close(directory_fd)
        raise


def _reopen_created_directory(
    component: str,
    parent_fd: int,
    flags: int,
) -> int:
    """Restore a private mode on a directory descriptor that cannot be opened.

    ``mkdir`` mode bits are masked by the process umask, so an unusual umask can
    leave an entry this call just created without owner access. The same applies
    to the exact requested directory, whose contract is to be made private.
    The entry is re-checked as a real directory and the private mode is restored
    through the parent descriptor before the descriptor is opened. Unrelated
    pre-existing ancestors never take this path.
    """
    try:
        info = os.lstat(component, dir_fd=parent_fd)
    except OSError as exc:
        raise _translate(exc) from None
    if not stat.S_ISDIR(info.st_mode):
        raise PrivateStorageError(
            "private_storage_path_rejected",
            reason="create_conflict",
        )
    try:
        os.chmod(component, PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
    except OSError as exc:
        raise _translate(exc) from None
    try:
        return os.open(component, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _translate(exc) from None


def _force_private_fd(fd: int, *, directory: bool) -> None:
    expected = PRIVATE_DIRECTORY_MODE if directory else PRIVATE_FILE_MODE
    info = os.fstat(fd)
    if info.st_uid != os.getuid():
        raise PrivateStorageError(
            "private_storage_permission",
            reason="not_owner",
        )
    if stat.S_IMODE(info.st_mode) != expected:
        try:
            os.fchmod(fd, expected)
        except OSError as exc:
            raise PrivateStorageError(
                "private_storage_permission",
                reason="chmod_failed",
                native_error=exc.errno,
            ) from None
        info = os.fstat(fd)
    _assert_private_fd(info, directory=directory)


def _assert_private_fd(info: os.stat_result, *, directory: bool) -> None:
    expected = PRIVATE_DIRECTORY_MODE if directory else PRIVATE_FILE_MODE
    if stat.S_IMODE(info.st_mode) != expected:
        raise PrivateStorageError(
            "private_storage_verification",
            reason="mode",
        )
    if info.st_uid != os.getuid():
        raise PrivateStorageError(
            "private_storage_verification",
            reason="owner",
        )


class MacOSPrivateStorage:
    """Enforce and inspect 0700/0600 ownership on the current volume."""

    def ensure_directory(self, path: str | PathLike[str]) -> None:
        target = _coerce_target(path)
        components = _components(target)
        if not components:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="filesystem_root",
            )
        directory_fd, _created = _open_directory_chain(
            components,
            create=True,
            recover_final=True,
        )
        try:
            _force_private_fd(directory_fd, directory=True)
        finally:
            os.close(directory_fd)

    def ensure_file(self, path: str | PathLike[str]) -> None:
        target = _coerce_target(path)
        components = _components(target)
        if not components:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="filesystem_root",
            )
        parent_fd, _created = _open_directory_chain(
            components[:-1],
            create=False,
        )
        try:
            try:
                entry = os.lstat(components[-1], dir_fd=parent_fd)
            except OSError as exc:
                raise _translate(exc, missing_reason="file") from None
            if stat.S_ISLNK(entry.st_mode):
                raise PrivateStorageError(
                    "private_storage_path_rejected",
                    reason="symbolic_link",
                )
            if not stat.S_ISREG(entry.st_mode):
                raise PrivateStorageError(
                    "private_storage_path_rejected",
                    reason="not_regular",
                )
            try:
                file_fd = os.open(
                    components[-1],
                    _NOFOLLOW_FILE_FLAGS,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise _translate(exc, missing_reason="file") from None
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode):
                    raise PrivateStorageError(
                        "private_storage_path_rejected",
                        reason="not_regular",
                    )
                _force_private_fd(file_fd, directory=False)
            finally:
                os.close(file_fd)
        finally:
            os.close(parent_fd)

    def verify(self, path: str | PathLike[str]) -> bool:
        try:
            target = _coerce_target(path)
        except PrivateStorageError:
            return False
        try:
            info = os.lstat(target)
        except OSError:
            return False
        if info.st_uid != os.getuid():
            return False
        if stat.S_ISDIR(info.st_mode):
            expected = PRIVATE_DIRECTORY_MODE
        elif stat.S_ISREG(info.st_mode):
            expected = PRIVATE_FILE_MODE
        else:
            return False
        return stat.S_IMODE(info.st_mode) == expected


def _reject_unusable_target(parent_fd: int, name: str) -> None:
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _translate(exc) from None
    if stat.S_ISLNK(info.st_mode):
        raise PrivateStorageError(
            "private_storage_path_rejected",
            reason="symbolic_link",
        )
    if stat.S_ISDIR(info.st_mode):
        raise PrivateStorageError(
            "private_storage_path_rejected",
            reason="not_regular",
        )
    if not stat.S_ISREG(info.st_mode):
        raise PrivateStorageError(
            "private_storage_path_rejected",
            reason="unsupported_type",
        )


def _create_private_temp(parent_fd: int) -> tuple[str, int]:
    for _attempt in range(_TEMP_ATTEMPTS):
        name = _TEMP_PREFIX + os.urandom(8).hex()
        try:
            temp_fd = os.open(
                name,
                _CREATE_TEMP_FLAGS,
                PRIVATE_FILE_MODE,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise _translate(exc) from None
        try:
            _force_private_fd(temp_fd, directory=False)
        except BaseException:
            os.close(temp_fd)
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        return name, temp_fd
    raise PrivateStorageError(
        "private_storage_unavailable",
        reason="temporary_name_exhausted",
    )


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        written = os.write(fd, view[offset : offset + _WRITE_CHUNK_BYTES])
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        offset += written


class MacOSAtomicPublisher:
    """Publish bytes with one same-directory ``os.replace`` step."""

    def __init__(
        self,
        *,
        storage: MacOSPrivateStorage | None = None,
        replace=None,
    ):
        self._storage = storage if storage is not None else MacOSPrivateStorage()
        self._replace = replace if replace is not None else os.replace

    def write_bytes(self, path: str | PathLike[str], data: bytes) -> None:
        payload = _coerce_payload(data)
        target = _coerce_target(path)
        components = _components(target)
        if not components:
            raise PrivateStorageError(
                "private_storage_path_rejected",
                reason="filesystem_root",
            )
        directory = os.path.dirname(target)
        self._storage.ensure_directory(directory)
        parent_fd, _created = _open_directory_chain(
            components[:-1],
            create=False,
        )
        name = components[-1]
        temp_name = ""
        temp_fd = -1
        try:
            _reject_unusable_target(parent_fd, name)
            temp_name, temp_fd = _create_private_temp(parent_fd)
            _write_all(temp_fd, payload)
            os.fsync(temp_fd)
            _assert_private_fd(os.fstat(temp_fd), directory=False)
            os.close(temp_fd)
            temp_fd = -1
            # Injected failures land before the target is touched.
            self._replace(os.path.join(directory, temp_name), target)
            temp_name = ""
        finally:
            if temp_fd >= 0:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            if temp_name:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except OSError:
                    pass
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            os.close(parent_fd)
        if not self._storage.verify(target):
            raise PrivateStorageError(
                "private_storage_verification",
                reason="published_target",
            )
