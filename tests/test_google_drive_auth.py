import json
import os
import stat
import tempfile
import unittest
import urllib.parse

import requests

from core.google_drive_auth import (
    GoogleDriveAuthError,
    DRIVE_FILE_SCOPE,
    GoogleDriveAuthRequired,
    GoogleDriveOAuth,
    install_client_config,
)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.responses.pop(0)


class FakeTokenStore:
    def __init__(self, token=""):
        self.token = token
        self.saved = []
        self.deleted = 0

    def load(self):
        return self.token

    def save(self, token):
        self.token = token
        self.saved.append(token)

    def delete(self):
        self.token = ""
        self.deleted += 1


class GoogleDriveOAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = os.path.join(self.tmp.name, "downloaded-client.json")
        self.target = os.path.join(self.tmp.name, "runtime", "oauth_client.json")
        self.client = {
            "installed": {
                "client_id": "fixture-client.apps.googleusercontent.com",
                "client_secret": "fixture-secret-not-real",
                "auth_uri": "https://accounts.google.invalid/o/oauth2/v2/auth",
                "token_uri": "https://oauth2.google.invalid/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        with open(self.source, "w", encoding="utf-8") as handle:
            json.dump(self.client, handle)

    def tearDown(self):
        self.tmp.cleanup()

    def test_client_config_is_normalized_to_private_runtime_file(self):
        result = install_client_config(self.source, self.target)

        self.assertEqual(result, self.target)
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o600)
        with open(self.target, encoding="utf-8") as handle:
            installed = json.load(handle)["installed"]
        self.assertEqual(
            set(installed),
            {"client_id", "client_secret", "auth_uri", "token_uri"},
        )

    def test_authorize_uses_drive_file_pkce_and_stores_only_refresh_token(self):
        token_store = FakeTokenStore()
        session = FakeSession([FakeResponse(200, {
            "access_token": "memory-access-token",
            "refresh_token": "keychain-refresh-token",
            "expires_in": 3600,
        })])
        oauth = GoogleDriveOAuth(
            client_config_path=self.target,
            token_store=token_store,
            session=session,
            now_func=lambda: 100,
        )
        received = {}

        def code_receiver(state, challenge):
            received["state"] = state
            received["challenge"] = challenge
            return "authorization-code", "http://127.0.0.1:32123/oauth/callback"

        status = oauth.authorize(self.source, code_receiver=code_receiver)
        url = oauth.authorization_url(
            "http://127.0.0.1:32123/oauth/callback",
            received["state"],
            received["challenge"],
        )
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

        self.assertEqual(query["scope"], [DRIVE_FILE_SCOPE])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("client_secret", query)
        self.assertEqual(token_store.saved, ["keychain-refresh-token"])
        self.assertNotIn("memory-access-token", token_store.saved)
        self.assertEqual(oauth.access_token(), "memory-access-token")
        self.assertEqual(status["state"], "connected")

    def test_refresh_invalid_grant_becomes_auth_required_without_losing_store(self):
        install_client_config(self.source, self.target)
        token_store = FakeTokenStore("persisted-refresh")
        oauth = GoogleDriveOAuth(
            client_config_path=self.target,
            token_store=token_store,
            session=FakeSession([FakeResponse(400, {"error": "invalid_grant"})]),
        )

        with self.assertRaisesRegex(GoogleDriveAuthRequired, "invalid_grant"):
            oauth.access_token()

        self.assertEqual(token_store.token, "")
        status = oauth.status()
        self.assertEqual(status["state"], "auth_required")
        self.assertFalse(status["connected"])
        self.assertFalse(status["token_present"])

    def test_status_distinguishes_token_present_from_refresh_valid(self):
        install_client_config(self.source, self.target)
        oauth = GoogleDriveOAuth(
            client_config_path=self.target,
            token_store=FakeTokenStore("persisted-refresh"),
            session=FakeSession([FakeResponse(200, {
                "access_token": "validated-access",
                "expires_in": 3600,
            })]),
            now_func=lambda: 100,
        )

        present = oauth.status()
        valid = oauth.status(validate=True)

        self.assertEqual(present["state"], "token_present")
        self.assertTrue(present["token_present"])
        self.assertIsNone(present["refresh_token_valid"])
        self.assertEqual(valid["state"], "connected")
        self.assertTrue(valid["refresh_token_valid"])

    def test_disconnect_deletes_refresh_token_and_clears_memory_access(self):
        install_client_config(self.source, self.target)
        token_store = FakeTokenStore("refresh")
        oauth = GoogleDriveOAuth(
            client_config_path=self.target,
            token_store=token_store,
            session=FakeSession([]),
        )
        oauth._access_token = "access"
        oauth._access_token_expires_at = 999999

        oauth.disconnect()

        self.assertEqual(token_store.deleted, 1)
        self.assertEqual(oauth._access_token, "")
        self.assertEqual(oauth.status()["state"], "auth_required")

    def test_refresh_network_failure_is_content_free_auth_error(self):
        install_client_config(self.source, self.target)

        class BrokenSession:
            def post(self, *_args, **_kwargs):
                raise requests.ConnectionError("offline with private details")

        oauth = GoogleDriveOAuth(
            client_config_path=self.target,
            token_store=FakeTokenStore("persisted-refresh"),
            session=BrokenSession(),
        )

        with self.assertRaises(GoogleDriveAuthError) as raised:
            oauth.access_token()

        self.assertEqual(raised.exception.code, "oauth_network_error")
        self.assertEqual(str(raised.exception), "oauth_network_error")


class ProtectedRefreshTokenStoreTests(unittest.TestCase):
    def test_native_store_failure_propagates_as_content_free_auth_error(self):
        from unittest.mock import Mock, patch
        from core.platform import SecretStoreError
        from core.google_drive_auth import ProtectedRefreshTokenStore, GoogleDriveAuthError
        native = Mock()
        native.load.side_effect = SecretStoreError("secret_store_unavailable")
        native.save.side_effect = SecretStoreError("secret_write_failed")
        native.delete.side_effect = SecretStoreError("secret_delete_failed")
        with patch("core.google_drive_auth.create_secret_store", return_value=native):
            store = ProtectedRefreshTokenStore()
            for action, code in ((store.load, "secret_store_unavailable"),
                                 (lambda: store.save("fixture"), "secret_write_failed"),
                                 (store.delete, "secret_delete_failed")):
                with self.assertRaisesRegex(GoogleDriveAuthError, f"^{code}$"):
                    action()

    def test_unavailable_status_is_not_missing_or_connected(self):
        from unittest.mock import Mock
        store = Mock()
        store.load.side_effect = GoogleDriveAuthError("secret_store_unavailable")
        oauth = GoogleDriveOAuth(client_config_path="/fixture/absent.json", token_store=store)
        state = oauth.status()
        self.assertEqual(state["state"], "validation_unavailable")
        self.assertIsNone(state["token_present"])
        self.assertFalse(state["connected"])

    def test_failed_disconnect_clears_cached_access_but_reports_failure(self):
        from unittest.mock import Mock
        store = Mock()
        store.delete.side_effect = GoogleDriveAuthError("secret_delete_failed")
        oauth = GoogleDriveOAuth(token_store=store)
        oauth._access_token = "synthetic-access"
        with self.assertRaisesRegex(GoogleDriveAuthError, "secret_delete_failed"):
            oauth.disconnect()
        self.assertEqual(oauth._access_token, "")


if __name__ == "__main__":
    unittest.main()
