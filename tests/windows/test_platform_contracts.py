import unittest
from unittest.mock import patch

from core.platform.contracts import (
    PlatformCapabilityUnavailable,
    PlatformName,
    PlatformServices,
)
from core.platform.factory import (
    InvalidPlatformServicesProviderResult,
    PlatformFactoryMismatch,
    PlatformServicesUnavailable,
    _PLATFORM_FACTORIES,
    create_path_service,
    create_platform_services,
    detect_platform,
    register_platform_services,
)


class PlatformFactoryTests(unittest.TestCase):
    def test_detect_platform_uses_closed_vocabulary(self):
        self.assertIs(detect_platform("Darwin"), PlatformName.MACOS)
        self.assertIs(detect_platform("WINDOWS"), PlatformName.WINDOWS)
        self.assertIs(detect_platform("Linux"), PlatformName.UNSUPPORTED)

    def test_unregistered_platform_fails_closed(self):
        with patch.dict(_PLATFORM_FACTORIES, {}, clear=True):
            with self.assertRaises(PlatformServicesUnavailable) as raised:
                create_platform_services(PlatformName.WINDOWS)
        self.assertEqual(raised.exception.code, "platform_services_unavailable")

    def test_registered_provider_is_selected(self):
        expected = PlatformServices(platform=PlatformName.WINDOWS)
        with patch.dict(_PLATFORM_FACTORIES, {}, clear=True):
            register_platform_services(PlatformName.WINDOWS, lambda: expected)
            self.assertIs(create_platform_services(PlatformName.WINDOWS), expected)

    def test_duplicate_registration_requires_explicit_replacement(self):
        first = PlatformServices(platform=PlatformName.WINDOWS)
        second = PlatformServices(platform=PlatformName.WINDOWS)
        with patch.dict(_PLATFORM_FACTORIES, {}, clear=True):
            register_platform_services(PlatformName.WINDOWS, lambda: first)
            with self.assertRaises(ValueError):
                register_platform_services(PlatformName.WINDOWS, lambda: second)
            register_platform_services(
                PlatformName.WINDOWS,
                lambda: second,
                replace=True,
            )
            self.assertIs(create_platform_services(PlatformName.WINDOWS), second)

    def test_provider_platform_mismatch_is_rejected(self):
        with patch.dict(_PLATFORM_FACTORIES, {}, clear=True):
            register_platform_services(
                PlatformName.WINDOWS,
                lambda: PlatformServices(platform=PlatformName.MACOS),
            )
            with self.assertRaises(PlatformFactoryMismatch) as raised:
                create_platform_services(PlatformName.WINDOWS)
        self.assertEqual(raised.exception.code, "platform_factory_mismatch")

    def test_unrelated_provider_result_is_rejected_with_stable_error(self):
        with patch.dict(_PLATFORM_FACTORIES, {}, clear=True):
            register_platform_services(PlatformName.WINDOWS, object)
            with self.assertRaises(
                InvalidPlatformServicesProviderResult
            ) as raised:
                create_platform_services(PlatformName.WINDOWS)
        self.assertEqual(
            raised.exception.code,
            "invalid_platform_services_provider_result",
        )
        self.assertEqual(raised.exception.reason, "provider_result_type")

    def test_malformed_platform_is_rejected_with_stable_error(self):
        malformed = PlatformServices(platform="windows")
        with patch.dict(_PLATFORM_FACTORIES, {}, clear=True):
            register_platform_services(PlatformName.WINDOWS, lambda: malformed)
            with self.assertRaises(
                InvalidPlatformServicesProviderResult
            ) as raised:
                create_platform_services(PlatformName.WINDOWS)
        self.assertEqual(
            raised.exception.code,
            "invalid_platform_services_provider_result",
        )
        self.assertEqual(raised.exception.reason, "platform_type")

    def test_partial_service_bundle_reports_capabilities_and_fails_closed(self):
        lock_service = object()
        services = PlatformServices(
            platform=PlatformName.WINDOWS,
            locks=lock_service,
        )
        self.assertEqual(services.available_capabilities(), frozenset({"locks"}))
        self.assertIs(services.require("locks"), lock_service)
        with self.assertRaises(PlatformCapabilityUnavailable) as raised:
            services.require("secrets")
        self.assertEqual(raised.exception.code, "platform_capability_unavailable")

    def test_concrete_desktop_provider_exposes_locks_and_paths_only(self):
        selected = detect_platform()
        if selected is PlatformName.UNSUPPORTED:
            self.skipTest("desktop provider gate targets macOS and Windows")
        services = create_platform_services(selected)
        self.assertEqual(
            services.available_capabilities(),
            frozenset({"locks", "paths"}),
        )
        self.assertIsNotNone(create_path_service(selected))


if __name__ == "__main__":
    unittest.main()
