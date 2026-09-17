"""Installed-app Google Drive OAuth with PKCE and native protected refresh-token storage."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from .config import DATA_DIR, ensure_private_dir
from .platform import SecretStoreError, create_secret_store


DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
REFRESH_TOKEN_ACCOUNT = "google-drive-file-sync-refresh-token"
OAUTH_DIR = os.path.join(DATA_DIR, "google_drive")
CLIENT_CONFIG_PATH = os.path.join(OAUTH_DIR, "oauth_client.json")


class GoogleDriveAuthError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class GoogleDriveAuthRequired(GoogleDriveAuthError):
    pass


class ProtectedRefreshTokenStore:
    def load(self):
        try:
            return create_secret_store().load(REFRESH_TOKEN_ACCOUNT)
        except SecretStoreError:
            raise GoogleDriveAuthError("secret_store_unavailable") from None

    def save(self, token):
        if not token:
            raise GoogleDriveAuthError("secret_empty")
        try:
            create_secret_store().save(REFRESH_TOKEN_ACCOUNT, str(token))
        except SecretStoreError:
            raise GoogleDriveAuthError("secret_write_failed") from None

    def delete(self):
        try:
            create_secret_store().delete(REFRESH_TOKEN_ACCOUNT)
        except SecretStoreError:
            raise GoogleDriveAuthError("secret_delete_failed") from None


def _atomic_private_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path)
    ensure_private_dir(directory)
    fd, temp_path = tempfile.mkstemp(prefix=".oauth-client.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = ""
        os.chmod(path, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _normalized_client_config(payload: dict) -> dict:
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, dict):
        raise GoogleDriveAuthError("installed_client_required")
    required = ("client_id", "auth_uri", "token_uri")
    if any(not isinstance(installed.get(key), str) or not installed[key].strip() for key in required):
        raise GoogleDriveAuthError("invalid_client_config")
    return {
        "installed": {
            "client_id": installed["client_id"].strip(),
            "client_secret": str(installed.get("client_secret") or ""),
            "auth_uri": installed["auth_uri"].strip(),
            "token_uri": installed["token_uri"].strip(),
        }
    }


def install_client_config(source_path: str, target_path: str = CLIENT_CONFIG_PATH) -> str:
    try:
        with open(os.path.expanduser(source_path), encoding="utf-8") as handle:
            normalized = _normalized_client_config(json.load(handle))
    except GoogleDriveAuthError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise GoogleDriveAuthError("client_config_unreadable") from exc
    _atomic_private_json(target_path, normalized)
    return target_path


def load_client_config(path: str = CLIENT_CONFIG_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return _normalized_client_config(json.load(handle))["installed"]
    except GoogleDriveAuthError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise GoogleDriveAuthRequired("oauth_client_missing") from exc


class GoogleDriveOAuth:
    def __init__(
        self,
        *,
        client_config_path: str = CLIENT_CONFIG_PATH,
        token_store=None,
        session=None,
        browser_open=None,
        now_func=time.time,
    ):
        self.client_config_path = client_config_path
        self.token_store = token_store or ProtectedRefreshTokenStore()
        self.session = session or requests.Session()
        self.browser_open = browser_open or webbrowser.open
        self.now_func = now_func
        self._access_token = ""
        self._access_token_expires_at = 0.0

    def status(self, *, validate=False) -> dict:
        configured = os.path.isfile(self.client_config_path)
        try:
            token_present = bool(self.token_store.load())
        except GoogleDriveAuthError as exc:
            return {
                "state": "validation_unavailable", "client_configured": configured,
                "token_present": None, "refresh_token_valid": None,
                "connected": False, "scope": DRIVE_FILE_SCOPE, "error_code": exc.code,
            }
        cached_valid = bool(
            self._access_token and self.now_func() < self._access_token_expires_at
        )
        if not configured or not token_present:
            return {
                "state": "auth_required",
                "client_configured": configured,
                "token_present": token_present,
                "refresh_token_valid": False if not token_present else None,
                "connected": False,
                "scope": DRIVE_FILE_SCOPE,
            }
        if cached_valid:
            return {
                "state": "connected",
                "client_configured": True,
                "token_present": True,
                "refresh_token_valid": True,
                "connected": True,
                "scope": DRIVE_FILE_SCOPE,
            }
        if validate:
            try:
                self.access_token()
            except GoogleDriveAuthRequired as exc:
                return {
                    "state": "auth_required",
                    "client_configured": True,
                    "token_present": bool(self.token_store.load()),
                    "refresh_token_valid": False,
                    "connected": False,
                    "scope": DRIVE_FILE_SCOPE,
                    "error_code": exc.code,
                }
            except GoogleDriveAuthError as exc:
                return {
                    "state": "validation_unavailable",
                    "client_configured": True,
                    "token_present": True,
                    "refresh_token_valid": None,
                    "connected": False,
                    "scope": DRIVE_FILE_SCOPE,
                    "error_code": exc.code,
                }
            return {
                "state": "connected",
                "client_configured": True,
                "token_present": True,
                "refresh_token_valid": True,
                "connected": True,
                "scope": DRIVE_FILE_SCOPE,
            }
        return {
            "state": "token_present",
            "client_configured": True,
            "token_present": True,
            "refresh_token_valid": None,
            "connected": False,
            "scope": DRIVE_FILE_SCOPE,
        }

    def disconnect(self) -> None:
        self.invalidate_access_token()
        self.token_store.delete()

    @staticmethod
    def _pkce_pair():
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        return verifier, challenge

    def authorization_url(self, redirect_uri: str, state: str, challenge: str) -> str:
        client = load_client_config(self.client_config_path)
        query = urllib.parse.urlencode({
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": DRIVE_FILE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        })
        return client["auth_uri"] + "?" + query

    def _receive_code(self, state: str, challenge: str, timeout: int = 180):
        result = {"code": "", "error": ""}
        finished = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler_self):
                parsed = urllib.parse.urlparse(handler_self.path)
                params = urllib.parse.parse_qs(parsed.query)
                if parsed.path != "/oauth/callback" or params.get("state", [""])[0] != state:
                    result["error"] = "oauth_state_mismatch"
                    status = 400
                elif params.get("error"):
                    result["error"] = "oauth_denied"
                    status = 400
                else:
                    result["code"] = params.get("code", [""])[0]
                    status = 200 if result["code"] else 400
                body = (
                    "Google Drive authorization received. You can close this window."
                    if status == 200
                    else "Google Drive authorization failed. Return to the app."
                ).encode("utf-8")
                handler_self.send_response(status)
                handler_self.send_header("Content-Type", "text/plain; charset=utf-8")
                handler_self.send_header("Content-Length", str(len(body)))
                handler_self.end_headers()
                handler_self.wfile.write(body)
                finished.set()

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = timeout
        redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth/callback"
        url = self.authorization_url(redirect_uri, state, challenge)
        self.browser_open(url)
        try:
            server.handle_request()
        finally:
            server.server_close()
        if not finished.is_set():
            raise GoogleDriveAuthError("oauth_timeout")
        if result["error"]:
            raise GoogleDriveAuthError(result["error"])
        return result["code"], redirect_uri

    def authorize(self, client_secrets_path: str, *, code_receiver=None) -> dict:
        install_client_config(client_secrets_path, self.client_config_path)
        verifier, challenge = self._pkce_pair()
        state = secrets.token_urlsafe(24)
        receiver = code_receiver or self._receive_code
        code, redirect_uri = receiver(state, challenge)
        client = load_client_config(self.client_config_path)
        try:
            response = self.session.post(
                client["token_uri"],
                data={
                    "client_id": client["client_id"],
                    "client_secret": client["client_secret"],
                    "code": code,
                    "code_verifier": verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise GoogleDriveAuthError("oauth_network_error") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise GoogleDriveAuthError("oauth_token_response_invalid") from exc
        if response.status_code >= 400:
            raise GoogleDriveAuthError(str(payload.get("error") or "oauth_token_exchange_failed"))
        refresh_token = str(payload.get("refresh_token") or "")
        if not refresh_token:
            raise GoogleDriveAuthError("refresh_token_missing")
        self.token_store.save(refresh_token)
        self._cache_access_token(payload)
        return self.status()

    def _cache_access_token(self, payload: dict) -> str:
        token = str(payload.get("access_token") or "")
        if not token:
            raise GoogleDriveAuthRequired("access_token_missing")
        expires_in = max(1, int(payload.get("expires_in") or 3600))
        self._access_token = token
        self._access_token_expires_at = self.now_func() + expires_in - 30
        return token

    def invalidate_access_token(self) -> None:
        self._access_token = ""
        self._access_token_expires_at = 0.0

    def access_token(self) -> str:
        if self._access_token and self.now_func() < self._access_token_expires_at:
            return self._access_token
        refresh_token = self.token_store.load()
        if not refresh_token:
            raise GoogleDriveAuthRequired("refresh_token_missing")
        client = load_client_config(self.client_config_path)
        try:
            response = self.session.post(
                client["token_uri"],
                data={
                    "client_id": client["client_id"],
                    "client_secret": client["client_secret"],
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise GoogleDriveAuthError("oauth_network_error") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise GoogleDriveAuthRequired("refresh_response_invalid") from exc
        if response.status_code >= 400:
            code = str(payload.get("error") or "refresh_failed")
            if code in {"invalid_grant", "invalid_client", "unauthorized_client"}:
                self.invalidate_access_token()
                self.token_store.delete()
                raise GoogleDriveAuthRequired(code)
            raise GoogleDriveAuthError(code)
        return self._cache_access_token(payload)
