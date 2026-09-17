"""macOS Keychain adapter; no credential access during construction/import."""
import subprocess

from .contracts import SecretStoreError
from ..project_identity import KEYCHAIN_SERVICE_NAME, LEGACY_KEYCHAIN_SERVICE_NAMES


class MacOSSecretStore:
    def __init__(self, *, service=KEYCHAIN_SERVICE_NAME,
                 legacy_services=LEGACY_KEYCHAIN_SERVICE_NAMES, runner=None):
        self.service = service
        self.legacy_services = tuple(legacy_services)
        self._runner = runner or subprocess.run

    def _run(self, operation, account, service, secret=None):
        args = ["security", operation, "-a", account, "-s", service]
        if secret is not None:
            args.extend(["-w", secret, "-U"])
        elif operation == "find-generic-password":
            args.append("-w")
        try:
            result = self._runner(args, capture_output=True, text=True,
                                  check=False, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            raise SecretStoreError("secret_store_unavailable") from None
        if result.returncode == 44:  # errSecItemNotFound, security(1) exit code
            return None
        if result.returncode:
            raise SecretStoreError("secret_store_unavailable") from None
        return result.stdout

    def save(self, account: str, secret: str) -> None:
        if not secret:
            raise SecretStoreError("secret_empty")
        if self._run("add-generic-password", account, self.service, secret) is None:
            raise SecretStoreError("secret_write_failed")

    def load(self, account: str) -> str | None:
        for service in (self.service, *self.legacy_services):
            value = self._run("find-generic-password", account, service)
            if value is not None:
                return value.rstrip("\n") or None
        return None

    def delete(self, account: str) -> None:
        # Remove fallback identities too, otherwise disconnect can resurrect one.
        for service in (self.service, *self.legacy_services):
            self._run("delete-generic-password", account, service)
