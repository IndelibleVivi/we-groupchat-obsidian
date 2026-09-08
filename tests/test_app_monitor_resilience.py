import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from app import WeGroupchatObsidianApp


class RefreshingDB:
    def __init__(self):
        self.refreshes = 0

    def refresh_cache_view(self):
        self.refreshes += 1


class AppMonitorResilienceTests(unittest.TestCase):
    def test_one_chat_failure_does_not_skip_later_chats(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app._monitor_lock = threading.Lock()
        app.config = {}
        app.db = RefreshingDB()
        app._monitor_chats = lambda: [
            {"username": "first@chatroom", "name": "First"},
            {"username": "second@chatroom", "name": "Second"},
        ]
        handled_results = []
        handled_errors = []
        app._handle_monitor_result = lambda result, **_kwargs: handled_results.append(result)
        app._handle_monitor_error = lambda message, _manual: handled_errors.append(message)

        calls = []

        class FakeMonitor:
            def __init__(self, _db, config, **_kwargs):
                self.username = config["monitor_chat_username"]

            def check_once(self, dry_run=False):
                calls.append((self.username, dry_run))
                if self.username == "first@chatroom":
                    raise RuntimeError("provider timeout")
                return {"status": "no_messages"}

        with patch("app.TopicMonitor", FakeMonitor):
            app._run_monitor_check(manual=False, dry_run=False)

        self.assertEqual(
            calls,
            [("first@chatroom", False), ("second@chatroom", False)],
        )
        self.assertEqual(handled_results, [{"status": "no_messages"}])
        self.assertEqual(len(handled_errors), 1)
        self.assertIn("First", handled_errors[0])
        self.assertEqual(app.db.refreshes, 1)

    def test_invalid_response_is_reported_as_failure_without_erasing_error_episode(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app._monitor_lock = threading.Lock()
        app._monitor_last_error = ""
        app.config = {"background_notifications_enabled": True}
        app.db = RefreshingDB()
        app._monitor_chats = lambda: [{"username": "fixture@chatroom", "name": "Synthetic"}]
        with patch("app.TopicMonitor") as monitor, patch("app._notify") as notify:
            monitor.return_value.check_once.side_effect = [
                {"status": "ai_invalid_response"},
                {"status": "ai_backoff"},
                {"status": "ai_invalid_response"},
            ]
            app._run_monitor_check()
            app._run_monitor_check()
            app._run_monitor_check()
        self.assertIn("ai_invalid_response", app._monitor_last_error)
        self.assertEqual(notify.call_count, 1)

    def test_pending_batch_block_and_busy_have_distinct_result_semantics(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {}
        app._monitor_last_error = ""
        with patch("app._notify"):
            self.assertIs(app._handle_monitor_result({"status": "monitor_pending_policy_changed"}), False)
            self.assertIsNone(app._handle_monitor_result({"status": "monitor_worker_busy"}))

    def test_canonical_write_refreshes_all_existing_source_day_digests(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {"monitor_notify_writes": False}

        with patch(
            "app.refresh_pending_daily_digests",
            return_value={"state": "refreshed", "written_dates": ["2026-08-02", "2026-08-03"]},
        ) as refresh:
            app._handle_monitor_result({
                "status": "duplicate",
                "knowledge_event_written": True,
                "affected_dates": ["2026-08-02", "2026-08-03"],
                "title": "New note",
                "summary": "Summary",
            })

        refresh.assert_called_once_with(app.config)

    def test_status_without_canonical_write_does_not_refresh_digest(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {"monitor_notify_writes": False}

        with patch("app.refresh_pending_daily_digests") as refresh:
            app._handle_monitor_result({
                "status": "notified",
                "last_msg_ts": 1234,
                "title": "No canonical event",
                "summary": "Summary",
            })

        refresh.assert_not_called()

    def test_canonical_write_starts_nonblocking_attachment_consumer_after_commit(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {
            "monitor_notify_writes": False,
            "attachment_archive_enabled": True,
        }
        app._start_attachment_archive_consumer = unittest.mock.Mock()

        with patch(
            "app.refresh_pending_daily_digests",
            return_value={"state": "idle", "written_dates": []},
        ):
            app._handle_monitor_result({
                "status": "duplicate",
                "knowledge_event_written": True,
                "affected_dates": [],
            })

        app._start_attachment_archive_consumer.assert_called_once_with()

    def test_dry_run_never_starts_attachment_consumer(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {
            "monitor_notify_writes": False,
            "attachment_archive_enabled": True,
        }
        app._start_attachment_archive_consumer = unittest.mock.Mock()

        with patch("app.refresh_pending_daily_digests") as refresh, patch("app._notify"):
            app._handle_monitor_result(
                {"status": "matched", "knowledge_event_written": True},
                dry_run=True,
            )

        app._start_attachment_archive_consumer.assert_not_called()
        refresh.assert_not_called()

    def test_background_notification_toggle_mutes_automatic_hit_banner(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {
            "background_notifications_enabled": False,
            "monitor_notify_writes": True,
        }

        with patch("app._notify") as notify:
            app._handle_monitor_result({
                "status": "notified",
                "notify_now": True,
                "title": "High-signal update",
                "summary": "Saved normally, but do not show a banner.",
            })

        notify.assert_not_called()

    def test_background_notification_toggle_mutes_only_automatic_errors(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {"background_notifications_enabled": False}
        app._monitor_last_error = ""

        with patch("app._notify") as notify:
            app._handle_monitor_error("provider timeout", manual=False)
            notify.assert_not_called()

            app._handle_monitor_error("manual provider timeout", manual=True)
            notify.assert_called_once()

    def test_monitor_error_redacts_compound_credentials_in_log_and_notification(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {"background_notifications_enabled": True}
        app._monitor_last_error = ""
        output = StringIO()

        with redirect_stdout(output), patch("app._notify") as notify:
            app._handle_monitor_error(
                "检查失败: https://example.test/callback?shareToken=PRIVATE_SENTINEL",
                manual=False,
            )

        self.assertNotIn("PRIVATE_SENTINEL", output.getvalue())
        self.assertIn("monitor_runtime_error", output.getvalue())
        notify.assert_called_once()
        self.assertNotIn("PRIVATE_SENTINEL", str(notify.call_args))
        self.assertIn("shareToken=REDACTED", str(notify.call_args))
        self.assertNotIn("PRIVATE_SENTINEL", app._monitor_last_error)

    def test_monitor_traceback_redacts_compound_credentials(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app._monitor_lock = threading.Lock()
        app.config = {"background_notifications_enabled": False}
        app._monitor_last_error = ""
        app.db = RefreshingDB()
        app._monitor_chats = lambda: [
            {"username": "synthetic@chatroom", "name": "Synthetic"},
        ]
        app._handle_monitor_result = unittest.mock.Mock()

        class FailingMonitor:
            def __init__(self, *_args, **_kwargs):
                pass

            def check_once(self, dry_run=False):
                raise RuntimeError(
                    "https://example.test/callback?accessJWT2=PRIVATE_SENTINEL"
                )

        stdout = StringIO()
        stderr = StringIO()
        with (
            patch("app.TopicMonitor", FailingMonitor),
            patch("app._notify"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            app._run_monitor_check(manual=False, dry_run=False)

        rendered = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn("PRIVATE_SENTINEL", rendered)
        self.assertIn("accessJWT2=REDACTED", rendered)

    def test_toggle_background_notifications_keeps_monitor_running(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {
            "background_notifications_enabled": True,
            "monitor_enabled": True,
        }
        app._rebuild_monitor_menu = unittest.mock.Mock()
        app._update_config = unittest.mock.Mock(
            side_effect=lambda *, patch=None, mutator=None: (
                app.config.update(patch or {}) or app.config
            )
        )

        with patch("app._notify") as notify:
            app._toggle_background_notifications(None)

        self.assertFalse(app.config["background_notifications_enabled"])
        self.assertTrue(app.config["monitor_enabled"])
        app._update_config.assert_called_once_with(
            patch={"background_notifications_enabled": False}
        )
        notify.assert_called_once()
        app._rebuild_monitor_menu.assert_called_once()

    def test_muted_background_notifications_still_write_daily_digest(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {
            "background_notifications_enabled": False,
            "daily_digest_notify": True,
        }
        app._daily_digest_lock = threading.Lock()
        digest = {
            "path": "/tmp/digest.md",
            "new_notes_count": 2,
            "today_action_count": 1,
            "today_risk_count": 0,
        }

        with patch("app.should_run_daily_digest", return_value=True), \
             patch("app.write_daily_digest", return_value=digest) as write, \
             patch("app.refresh_pending_daily_digests", return_value={"state": "idle"}) as repair, \
             patch("app.mark_daily_digest_success") as mark, \
             patch("app._notify") as notify:
            app._run_daily_digest()

        write.assert_called_once_with(app.config)
        repair.assert_called_once_with(app.config)
        mark.assert_called_once()
        notify.assert_not_called()

    def test_not_due_timer_still_drains_durable_digest_repairs(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {"daily_digest_enabled": True}
        app._daily_digest_lock = threading.Lock()

        with patch("app.should_run_daily_digest", return_value=False), \
             patch("app.write_daily_digest") as write, \
             patch("app.refresh_pending_daily_digests", return_value={"state": "refreshed"}) as repair, \
             patch("app.mark_daily_digest_success") as mark:
            app._run_daily_digest()

        write.assert_not_called()
        repair.assert_called_once_with(app.config)
        mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
