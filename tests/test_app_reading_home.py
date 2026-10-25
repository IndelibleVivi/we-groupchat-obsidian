"""The menu opens an identity-resolved home without source or provider work."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import WeGroupchatObsidianApp
from tests import test_semantic_reading_home as home_fixtures


class AppReadingHomeTests(unittest.TestCase):
    def test_menu_dispatches_home_lookup_to_a_daemon(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        with patch("app.threading.Thread") as thread:
            app._open_semantic_home(None)
        thread.assert_called_once_with(target=app._open_semantic_home_worker, daemon=True)
        thread.return_value.start.assert_called_once()

    def test_ready_home_rename_opens_without_provider_or_source(self):
        fixture = home_fixtures.ReadingHomeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.item()
        receipt = fixture.home.publish(now=fixture.NOW)
        renamed = fixture.publisher.root / "继续阅读.md"
        (fixture.publisher.root / receipt.path).rename(renamed)
        pin = SimpleNamespace(database=fixture.store.path, generation_id=fixture.store.generation_id,
                              projection_root=fixture.publisher.root)
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {"monitor_enabled": False}
        app.db = None
        with (patch("app._semantic_pin", return_value=pin),
              patch("core.semantic.runtime.ProviderRoles.from_profile", side_effect=AssertionError("provider")),
              patch("core.wechat_db.WeChatDB.for_runtime", side_effect=AssertionError("source")),
              patch("app.subprocess.run") as opened):
            app._open_semantic_home_worker()
        opened.assert_called_once_with(["open", str(renamed)])

    def test_absent_or_changed_generation_never_opens_a_stale_home(self):
        fixture = home_fixtures.ReadingHomeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        pin = SimpleNamespace(database=fixture.store.path, generation_id=fixture.store.generation_id,
                              projection_root=fixture.publisher.root)
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {}
        with patch("app._semantic_pin", return_value=pin), patch("app.subprocess.run") as opened:
            app._open_semantic_home_worker()
        opened.assert_not_called()
        fixture.home.publish(now=fixture.NOW)
        with patch("app._semantic_pin", side_effect=[pin, None]), patch("app.subprocess.run") as opened:
            app._open_semantic_home_worker()
        opened.assert_not_called()

    def test_failed_projection_open_reports_only_a_fixed_code(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {}
        with (patch("app._semantic_pin", side_effect=RuntimeError("private path and credential")),
              patch("app.print") as output, patch("app.subprocess.run") as opened):
            app._open_semantic_home_worker()
        output.assert_called_once_with("[semantic] home unavailable: semantic_home_unavailable")
        opened.assert_not_called()
