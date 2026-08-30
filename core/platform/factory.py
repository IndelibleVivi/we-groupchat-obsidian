"""Fail-closed registry for platform-service providers."""
from __future__ import annotations

import platform as runtime_platform
from collections.abc import Callable

from .contracts import FileLock, PathService, PlatformName, PlatformServices

PlatformServicesProvider = Callable[[], PlatformServices]


def _macos_services() -> PlatformServices:
    from .macos_locks import MacOSFileLock
    from .macos_paths import MacOSPathService

    return PlatformServices(
        platform=PlatformName.MACOS,
        locks=MacOSFileLock(),
        paths=MacOSPathService(),
    )


def _windows_services() -> PlatformServices:
    from .windows_locks import WindowsFileLock
    from .windows_paths import WindowsPathService

    return PlatformServices(
        platform=PlatformName.WINDOWS,
        locks=WindowsFileLock(),
        paths=WindowsPathService(),
    )


_PLATFORM_FACTORIES: dict[PlatformName, PlatformServicesProvider] = {
    PlatformName.MACOS: _macos_services,
    PlatformName.WINDOWS: _windows_services,
}


class PlatformServicesUnavailable(RuntimeError):
    code = "platform_services_unavailable"

    def __init__(self, platform_name: PlatformName):
        self.platform_name = platform_name
        super().__init__(f"{self.code}:{platform_name.value}")


class PlatformFactoryMismatch(RuntimeError):
    code = "platform_factory_mismatch"

    def __init__(self, expected: PlatformName, actual: PlatformName):
        self.expected = expected
        self.actual = actual
        super().__init__(f"{self.code}:{expected.value}:{actual.value}")


class InvalidPlatformServicesProviderResult(RuntimeError):
    code = "invalid_platform_services_provider_result"

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"{self.code}:{reason}")


def detect_platform(system_name: str | None = None) -> PlatformName:
    normalized = str(system_name or runtime_platform.system()).strip().casefold()
    if normalized == "darwin":
        return PlatformName.MACOS
    if normalized == "windows":
        return PlatformName.WINDOWS
    return PlatformName.UNSUPPORTED


def register_platform_services(
    platform_name: PlatformName,
    provider: PlatformServicesProvider,
    *,
    replace: bool = False,
) -> None:
    if platform_name is PlatformName.UNSUPPORTED:
        raise ValueError("unsupported platform cannot register services")
    if not callable(provider):
        raise TypeError("platform services provider must be callable")
    if platform_name in _PLATFORM_FACTORIES and not replace:
        raise ValueError(f"platform services already registered: {platform_name.value}")
    _PLATFORM_FACTORIES[platform_name] = provider


def create_platform_services(
    platform_name: PlatformName | None = None,
) -> PlatformServices:
    selected = platform_name or detect_platform()
    provider = _PLATFORM_FACTORIES.get(selected)
    if provider is None:
        raise PlatformServicesUnavailable(selected)
    services = provider()
    if not isinstance(services, PlatformServices):
        raise InvalidPlatformServicesProviderResult("provider_result_type")
    if not isinstance(services.platform, PlatformName):
        raise InvalidPlatformServicesProviderResult("platform_type")
    if services.platform is not selected:
        raise PlatformFactoryMismatch(selected, services.platform)
    return services


def create_file_lock(platform_name: PlatformName | None = None) -> FileLock:
    return create_platform_services(platform_name).require("locks")


def create_path_service(platform_name: PlatformName | None = None) -> PathService:
    return create_platform_services(platform_name).require("paths")
