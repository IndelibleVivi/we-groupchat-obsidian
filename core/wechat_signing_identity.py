"""Persistent per-machine self-signed identity for WeChat re-signing.

macOS TCC does not retain consent granted to a bare ad-hoc cdhash, so a
WeChat bundle re-signed with ``codesign --sign -`` keeps re-prompting for
App Data and folder access. Signing with a stable self-signed certificate
gives that consent a durable identity. This module creates the identity on
first use and locates it afterwards; nothing runs at import time and every
external command goes through the injectable runner.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import tempfile


IDENTITY_COMMON_NAME = "WGO WeChat Stable Identity"
_KEYCHAIN_NAME = "wgo-wechat-identity.keychain-db"
_PASSWORD_NAME = "wgo-wechat-identity.pw"
_CERTIFICATE_DAYS = "3650"

_OPENSSL_CONFIG = """[req]
distinguished_name = dn
x509_extensions = codesign
[dn]
[codesign]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = codeSigning
"""


class WeChatSigningIdentityError(RuntimeError):
    def __init__(self, code: str):
        self.code = str(code or "wechat_signing_identity_unavailable")
        super().__init__(self.code)


@dataclass(frozen=True)
class WeChatSigningIdentity:
    common_name: str
    keychain_path: str
    certificate_root: str


def identity_keychain_path(home=None) -> Path:
    base = Path(home) if home is not None else Path.home()
    return base / "Library" / "Keychains" / _KEYCHAIN_NAME


def identity_password_path(home=None) -> Path:
    base = Path(home) if home is not None else Path.home()
    return base / "Library" / "Keychains" / _PASSWORD_NAME


def _run_checked(run, args, *, timeout: int = 60, capture: bool = False):
    kwargs = {"timeout": timeout}
    if capture:
        kwargs.update(capture_output=True, text=True)
    try:
        result = run(args, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WeChatSigningIdentityError(
            "wechat_signing_identity_unavailable"
        ) from exc
    if result.returncode:
        raise WeChatSigningIdentityError("wechat_signing_identity_unavailable")
    return result


def _certificate_root_from_pem(pem_text: str) -> str | None:
    body = []
    inside = False
    for line in pem_text.splitlines():
        marker = line.strip()
        if marker == "-----BEGIN CERTIFICATE-----":
            inside = True
            continue
        if marker == "-----END CERTIFICATE-----":
            break
        if inside:
            body.append(marker)
    if not body:
        return None
    try:
        der = base64.b64decode("".join(body), validate=True)
    except (ValueError, binascii.Error):
        return None
    return hashlib.sha1(der).hexdigest()


def managed_certificate_root_hash(
    *,
    runner=None,
    keychain_path=None,
) -> str | None:
    run = runner if runner is not None else subprocess.run
    keychain = str(keychain_path or identity_keychain_path())
    try:
        found = run(
            [
                "security", "find-certificate",
                "-c", IDENTITY_COMMON_NAME,
                "-p", keychain,
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode != 0:
        return None
    return _certificate_root_from_pem(found.stdout or "")


def _read_password(path: Path) -> str:
    try:
        existing = os.lstat(path)
    except FileNotFoundError as exc:
        raise WeChatSigningIdentityError(
            "wechat_signing_identity_password_missing"
        ) from exc
    if not stat.S_ISREG(existing.st_mode):
        raise WeChatSigningIdentityError(
            "wechat_signing_identity_password_invalid"
        )
    password = path.read_text(encoding="ascii").strip()
    if not password:
        raise WeChatSigningIdentityError(
            "wechat_signing_identity_password_invalid"
        )
    return password


def _read_or_create_password(path: Path) -> str:
    try:
        return _read_password(path)
    except WeChatSigningIdentityError as exc:
        if exc.code != "wechat_signing_identity_password_missing":
            raise
    password = secrets.token_hex(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, password.encode("ascii"))
    finally:
        os.close(descriptor)
    return password


def _openssl_supports_legacy_flag(run, openssl: str) -> bool:
    version = _run_checked(run, [openssl, "version"], timeout=30, capture=True)
    return (version.stdout or "").startswith("OpenSSL 3")


def _ensure_user_search_list(run, keychain: Path) -> None:
    try:
        listed = run(
            ["security", "list-keychains", "-d", "user"],
            capture_output=True, text=True, timeout=30,
        )
        if listed.returncode != 0:
            return
        entries = [
            line.strip().strip('"')
            for line in (listed.stdout or "").splitlines()
            if line.strip()
        ]
        if str(keychain) in entries:
            return
        run(
            ["security", "list-keychains", "-d", "user", "-s", *entries, str(keychain)],
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return


def ensure_stable_signing_identity(
    *,
    runner=None,
    home=None,
) -> WeChatSigningIdentity:
    run = runner if runner is not None else subprocess.run
    keychain = identity_keychain_path(home)
    password_path = identity_password_path(home)
    existing = managed_certificate_root_hash(runner=run, keychain_path=keychain)
    if existing:
        return WeChatSigningIdentity(
            IDENTITY_COMMON_NAME, str(keychain), existing
        )
    if keychain.exists() and not password_path.exists():
        raise WeChatSigningIdentityError(
            "wechat_signing_identity_password_missing"
        )
    password = _read_or_create_password(password_path)
    openssl = shutil.which("openssl") or "/usr/bin/openssl"
    workspace = Path(tempfile.mkdtemp(prefix="wgo-signing-identity-"))
    try:
        config = workspace / "openssl.cnf"
        config.write_text(_OPENSSL_CONFIG, encoding="ascii")
        key = workspace / "identity.key.pem"
        certificate = workspace / "identity.cert.pem"
        package = workspace / "identity.p12"
        _run_checked(
            run,
            [
                openssl, "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(key), "-out", str(certificate),
                "-days", _CERTIFICATE_DAYS, "-nodes",
                "-subj", f"/CN={IDENTITY_COMMON_NAME}",
                "-config", str(config),
            ],
            timeout=120,
        )
        export = [
            openssl, "pkcs12", "-export",
            "-out", str(package),
            "-inkey", str(key), "-in", str(certificate),
            "-passout", f"pass:{password}",
        ]
        if _openssl_supports_legacy_flag(run, openssl):
            export.append("-legacy")
        _run_checked(run, export, timeout=60)
        if not keychain.exists():
            _run_checked(
                run,
                ["security", "create-keychain", "-p", password, str(keychain)],
            )
        _run_checked(
            run,
            [
                "security", "import", str(package),
                "-k", str(keychain), "-P", password,
                "-T", "/usr/bin/codesign",
            ],
        )
        _run_checked(
            run,
            ["security", "unlock-keychain", "-p", password, str(keychain)],
        )
        _run_checked(
            run,
            [
                "security", "set-key-partition-list",
                "-S", "apple-tool:,apple:,codesign:",
                "-k", password, str(keychain),
            ],
        )
        _ensure_user_search_list(run, keychain)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    root = managed_certificate_root_hash(runner=run, keychain_path=keychain)
    if not root:
        raise WeChatSigningIdentityError("wechat_signing_identity_unavailable")
    return WeChatSigningIdentity(IDENTITY_COMMON_NAME, str(keychain), root)


def unlock_signing_keychain(
    identity: WeChatSigningIdentity,
    *,
    runner=None,
    home=None,
) -> None:
    run = runner if runner is not None else subprocess.run
    password_path = identity_password_path(home)
    password = _read_password(password_path)
    _run_checked(
        run,
        ["security", "unlock-keychain", "-p", password, identity.keychain_path],
    )
