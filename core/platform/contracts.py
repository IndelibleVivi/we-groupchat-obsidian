"""Behavioral contracts for platform-owned services.

W0.2A supplies concrete macOS and Windows file-lock adapters. W0.2B.1 adds
path identity without activating storage, source, monitor, or UI behavior.
W0.2B.2 adds concrete private-storage and atomic byte publication behind the
``private_storage`` and ``atomic_publisher`` capabilities. W0.3 wires native
protected credential stores for existing API/OAuth callers. Later platform
capabilities remain fail-closed until their owning phases.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from os import PathLike
from typing import Protocol, Sequence


_PLATFORM_SERVICE_NAMES = (
    "locks",
    "paths",
    "private_storage",
    "atomic_publisher",
    "secrets",
    "processes",
    "notifications",
    "open_targets",
    "autostart",
)


class PlatformName(str, Enum):
    MACOS = "macos"
    WINDOWS = "windows"
    UNSUPPORTED = "unsupported"


class LockMode(str, Enum):
    SHARED = "shared"
    EXCLUSIVE = "exclusive"


class LockBusy(RuntimeError):
    """Stable non-blocking lock conflict."""

    code = "worker_busy"


class PlatformCapabilityUnavailable(RuntimeError):
    code = "platform_capability_unavailable"

    def __init__(self, platform_name: PlatformName, capability: str):
        self.platform_name = platform_name
        self.capability = capability
        super().__init__(f"{self.code}:{platform_name.value}:{capability}")


class LockHandle(Protocol):
    """A retained operating-system lock handle."""

    def fileno(self) -> int: ...

    def close(self) -> None: ...

    def __enter__(self) -> "LockHandle": ...

    def __exit__(self, exc_type, exc, traceback) -> None: ...


class FileLock(Protocol):
    def acquire(
        self,
        path: str | PathLike[str],
        *,
        mode: LockMode,
        blocking: bool,
    ) -> LockHandle: ...


class PathIdentityError(RuntimeError):
    code = "path_identity_unknown"

    def __init__(self, reason: str, *, native_error: int | None = None):
        self.reason = reason
        self.native_error = native_error
        super().__init__(f"{self.code}:{reason}")


class ReparsePointConflict(PathIdentityError):
    code = "reparse_point_conflict"


@dataclass(frozen=True)
class PathIdentity:
    display_path: str
    operational_path: str
    identity_key: str
    source_relative_path: str = ""


class PathService(Protocol):
    def describe(
        self,
        path: str | PathLike[str],
        *,
        source_root: str | PathLike[str] | None = None,
    ) -> PathIdentity: ...


class PrivateStorage(Protocol):
    def ensure_directory(self, path: str | PathLike[str]) -> None: ...

    def ensure_file(self, path: str | PathLike[str]) -> None: ...

    def verify(self, path: str | PathLike[str]) -> bool: ...


class PrivateStorageError(OSError):
    """Content-free private-storage or publication-policy failure.

    ``code`` is a stable machine token, ``reason`` is a bounded token that
    never contains a path, and ``native_error`` carries the operating-system
    error number when one was reported. The exception subclasses ``OSError``
    so callers that already map operating-system failures keep working.
    """

    def __init__(
        self,
        code: str,
        *,
        reason: str = "",
        native_error: int | None = None,
    ):
        self.code = str(code)
        self.reason = str(reason)
        self.native_error = native_error
        super().__init__(
            self.code if not self.reason else f"{self.code}:{self.reason}"
        )
        if native_error is not None:
            self.errno = native_error


class AtomicPublisher(Protocol):
    """Replace one target's bytes through a single atomic publication step."""

    def write_bytes(self, path: str | PathLike[str], data: bytes) -> None: ...


class SecretStoreError(RuntimeError):
    """Bounded failure token; never includes account names or secret material."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SecretStore(Protocol):
    """Missing loads return None; inaccessible storage raises SecretStoreError.

    Save replaces one credential through the native protected store. Delete is
    idempotent and removes every legacy identity readable by this adapter.
    """
    def save(self, account: str, secret: str) -> None: ...

    def load(self, account: str) -> str | None: ...

    def delete(self, account: str) -> None: ...


@dataclass(frozen=True)
class ProcessIdentity:
    process_id: int
    created_at: str
    executable_path: str
    executable_sha256: str


class ProcessService(Protocol):
    def find_desktop_clients(self) -> Sequence[ProcessIdentity]: ...


class NotificationService(Protocol):
    def notify(
        self,
        *,
        title: str,
        body: str,
        open_target: str = "",
    ) -> None: ...


class OpenTargetService(Protocol):
    def open_target(self, target: str) -> None: ...


@dataclass(frozen=True)
class AutostartStatus:
    installed: bool
    enabled: bool
    state: str


class AutostartService(Protocol):
    def install(self) -> None: ...

    def uninstall(self) -> None: ...

    def status(self) -> AutostartStatus: ...


@dataclass(frozen=True)
class PlatformServices:
    platform: PlatformName
    locks: FileLock | None = None
    paths: PathService | None = None
    private_storage: PrivateStorage | None = None
    atomic_publisher: AtomicPublisher | None = None
    secrets: SecretStore | None = None
    processes: ProcessService | None = None
    notifications: NotificationService | None = None
    open_targets: OpenTargetService | None = None
    autostart: AutostartService | None = None

    def available_capabilities(self) -> frozenset[str]:
        return frozenset(
            name
            for name in _PLATFORM_SERVICE_NAMES
            if getattr(self, name) is not None
        )

    def require(self, capability: str):
        if capability not in _PLATFORM_SERVICE_NAMES:
            raise ValueError(f"unknown platform capability: {capability}")
        service = getattr(self, capability)
        if service is None:
            raise PlatformCapabilityUnavailable(self.platform, capability)
        return service
