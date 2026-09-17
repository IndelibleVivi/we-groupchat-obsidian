"""Compatibility facade for current app/CLI credential callers.

The historical module/function names stay stable for those callers. Native
operations live only in core.platform; new strict consumers use SecretStore.
No plaintext config or file fallback is permitted.
"""
from .platform import SecretStoreError, create_secret_store
from .project_identity import KEYCHAIN_SERVICE_NAME, LEGACY_KEYCHAIN_SERVICE_NAMES

SERVICE_NAME = KEYCHAIN_SERVICE_NAME
LEGACY_SERVICE_NAMES = LEGACY_KEYCHAIN_SERVICE_NAMES


def save_key(account: str, password: str) -> bool:
    try:
        create_secret_store().save(account, password)
        return True
    except SecretStoreError:
        return False


def load_key(account: str) -> str | None:
    """Legacy UI convenience: unavailable storage supplies no credential."""
    try:
        return create_secret_store().load(account)
    except SecretStoreError:
        return None


def delete_key(account: str) -> bool:
    try:
        create_secret_store().delete(account)
        return True
    except SecretStoreError:
        return False


def secret_status(account: str) -> str:
    """Content-free presence, with unavailable distinct from absent."""
    try:
        return "present" if create_secret_store().load(account) is not None else "missing"
    except SecretStoreError:
        return "unavailable"
