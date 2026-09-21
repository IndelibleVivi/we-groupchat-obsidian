import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import ANY, Mock, patch

from app import WeGroupchatObsidianApp
from core.app_runtime import AppAlreadyRunning, AppInstanceLock


class AppResourceBackupTests(unittest.TestCase):
    def make_app(self, *, resolve_files=False):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {
            "resource_backup_enabled": True,
        }
        app._resource_file_resolution_session_enabled = resolve_files
        app.db = object()
        app._resource_backup_lock = threading.Lock()
        app._source_guard_lock = threading.Lock()
        app._run_on_main = Mock()
        return app

    def test_long_lived_consumer_skips_attachment_cache_by_default(self):
        app = self.make_app(resolve_files=False)
        capture = Mock()
        capture.run.return_value = {
            "state": "healthy",
            "scan": {"captured_links": 1, "captured_files": 1},
            "resolve": {"state": "skipped"},
        }
        app._resource_capture_service = Mock(return_value=capture)
        backup = Mock()
        backup.run.return_value = {"state": "sync_delegated"}

        with (
            patch("app.load_config", return_value=app.config),
            patch("app.MountedResourceBackup.from_config", return_value=backup),
        ):
            app._run_resource_backup_consumer(manual=False)

        capture.run.assert_called_once_with(resolve_limit=50, resolve_files=False)
        backup.run.assert_called_once_with()
        self.assertFalse(app._resource_backup_lock.locked())

    def test_quiet_archive_only_cycle_captures_once_without_old_projection(self):
        app = self.make_app()
        app.config.update(resource_backup_enabled=False, quiet_archive_handoff_enabled=True)
        capture = Mock()
        capture.run.return_value = {"state": "healthy", "scan": {}, "resolve": {"state": "skipped"}}
        app._resource_capture_service = Mock(return_value=capture)
        handoff = Mock()
        handoff.run.return_value = {"state": "written"}
        with (patch("app.load_config", return_value=app.config),
              patch("app.QuietArchiveHandoff.from_config", return_value=handoff),
              patch("app.MountedResourceBackup.from_config", side_effect=AssertionError("projection"))):
            app._run_resource_backup_consumer(manual=False)
        capture.run.assert_called_once_with(resolve_limit=50, resolve_files=False)
        handoff.run.assert_called_once_with()
        self.assertEqual(app._run_on_main.call_args.args[1]["backup"]["state"], "disabled")

    def test_downstream_failures_do_not_block_the_other_consumer(self):
        for failing in ("handoff", "backup"):
            with self.subTest(failing=failing):
                app = self.make_app()
                app.config["quiet_archive_handoff_enabled"] = True
                capture = Mock()
                capture.run.return_value = {"state": "healthy", "scan": {}, "resolve": {"state": "skipped"}}
                app._resource_capture_service = Mock(return_value=capture)
                handoff, backup = Mock(), Mock()
                handoff.run.return_value = {"state": "written"}
                backup.run.return_value = {"state": "idle"}
                (handoff if failing == "handoff" else backup).run.side_effect = RuntimeError("synthetic")
                with (patch("app.load_config", return_value=app.config),
                      patch("app.QuietArchiveHandoff.from_config", return_value=handoff),
                      patch("app.MountedResourceBackup.from_config", return_value=backup)):
                    app._run_resource_backup_consumer(manual=False)
                capture.run.assert_called_once()
                handoff.run.assert_called_once()
                backup.run.assert_called_once()

    def test_resource_cycle_reports_actual_projection_publish_bytes(self):
        app = self.make_app(resolve_files=False)
        capture = Mock()
        capture.run.return_value = {
            "state": "healthy",
            "scan": {"captured_links": 0, "captured_files": 0},
            "resolve": {"state": "skipped"},
        }
        app._resource_capture_service = Mock(return_value=capture)
        backup = Mock()
        backup.run.return_value = {
            "state": "sync_delegated",
            "obsidian": {
                "state": "written",
                "projection_files_written": 3,
                "projection_bytes_written": 8192,
            },
        }
        output = StringIO()

        with (
            patch("app.load_config", return_value=app.config),
            patch("app.MountedResourceBackup.from_config", return_value=backup),
            redirect_stdout(output),
        ):
            app._run_resource_backup_consumer(manual=False)

        rendered = output.getvalue()
        self.assertEqual(rendered.count("[source-stage] cycle=resource"), 1)
        self.assertIn("projection_files=3", rendered)
        self.assertIn("projection_bytes=8192", rendered)

    def test_file_resolution_requires_explicit_session_opt_in(self):
        app = self.make_app(resolve_files=True)
        capture = Mock()
        capture.run.return_value = {
            "state": "healthy",
            "scan": {},
            "resolve": {"state": "healthy"},
        }
        app._resource_capture_service = Mock(return_value=capture)
        backup = Mock()
        backup.run.return_value = {"state": "idle"}

        with (
            patch("app.load_config", return_value=app.config),
            patch("app.MountedResourceBackup.from_config", return_value=backup),
        ):
            app._run_resource_backup_consumer(manual=False)

        capture.run.assert_called_once_with(
            resolve_limit=50,
            resolve_files=True,
            consent_check=ANY,
        )
        consent_check = capture.run.call_args.kwargs["consent_check"]
        self.assertTrue(consent_check())
        app._resource_file_resolution_session_enabled = False
        self.assertFalse(consent_check())

    def test_restart_resets_file_resolution_session_grant(self):
        restarted = self.make_app(resolve_files=False)

        self.assertFalse(restarted._resource_file_resolution_session_enabled)

    def test_menu_exposes_the_human_file_backup_entry(self):
        app = self.make_app()
        capture = Mock()
        capture.status.return_value = {
            "counts": {"link:ready_metadata": 2, "file:ready_local": 1},
            "pending_files": 0,
            "selected_chats": 1,
        }
        app._resource_capture_service = Mock(return_value=capture)
        backup = Mock()
        backup.status.return_value = {
            "coverage": {
                "delivered_objects": 5,
                "delivered_occurrences": 8,
                "non_delivered_occurrences": 3,
            },
        }

        with patch(
            "app.MountedResourceBackup.from_config", return_value=backup
        ):
            menu = app._build_resource_backup_menu()

        self.assertIn("📂 在 Finder 打开文件备份", set(menu.keys()))
        self.assertIn(
            "已备份: 5 个文件 · 8 次出现 · 待补齐: 3 条",
            set(menu.keys()),
        )
        self.assertIn("附件解析: 本次会话未允许", set(menu.keys()))

    def test_open_file_backup_entry_reveals_the_generated_portal(self):
        app = self.make_app()
        backup = Mock()
        backup.existing_target_portal_path.return_value = "/tmp/portal.md"

        with (
            patch("app.load_config", return_value=app.config),
            patch("app.MountedResourceBackup.from_config", return_value=backup),
            patch("app.subprocess.run") as run,
        ):
            app._open_resource_backup_portal(None)

        run.assert_called_once_with(["open", "-R", "/tmp/portal.md"])

    def test_open_file_backup_entry_explains_when_no_portal_exists(self):
        app = self.make_app()
        backup = Mock()
        backup.existing_target_portal_path.return_value = ""

        with (
            patch("app.load_config", return_value=app.config),
            patch("app.MountedResourceBackup.from_config", return_value=backup),
            patch("app.subprocess.run") as run,
            patch("app._notify") as notify,
        ):
            app._open_resource_backup_portal(None)

        run.assert_not_called()
        self.assertEqual(notify.call_args.args[1], "还没有可打开的文件备份入口")

    def test_backfill_does_not_report_success_when_projection_failed(self):
        app = self.make_app()
        app._finish_task = Mock()
        app._rebuild_resource_backup_menu = Mock()
        result = {
            "state": "applied",
            "source_complete": True,
            "discovered_links": 2,
            "inserted_links": 2,
        }
        projection = {
            "state": "sync_delegated",
            "obsidian": {"state": "projection_failed"},
        }

        with (
            patch("app.load_config", return_value=app.config),
            patch("app._notify") as notify,
        ):
            app._finish_link_backfill(result, projection)

        self.assertEqual(notify.call_args.args[1], "历史链接未写完")
        self.assertIn("projection_failed", notify.call_args.args[2])

    def test_disabled_background_worker_does_not_touch_source_or_backup(self):
        app = self.make_app()
        app._resource_capture_service = Mock(
            side_effect=AssertionError("disabled worker touched source")
        )

        with (
            patch("app.load_config", return_value={"resource_backup_enabled": False}),
            patch(
                "app.MountedResourceBackup.from_config",
                side_effect=AssertionError("disabled worker touched backup"),
            ),
        ):
            app._run_resource_backup_consumer(manual=False)

        self.assertFalse(app._resource_backup_lock.locked())

    def test_app_singleton_lock_rejects_second_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "menu-app.lock")
            first = AppInstanceLock(path).acquire()
            try:
                with self.assertRaisesRegex(
                    AppAlreadyRunning, "menu_app_already_running"
                ):
                    AppInstanceLock(path).acquire()
            finally:
                first.release()

    def test_link_backfill_apply_never_calls_file_resolver(self):
        app = self.make_app(resolve_files=False)
        capture = Mock()
        capture.backfill_links.return_value = {
            "state": "applied",
            "source_complete": True,
            "discovered_links": 3,
            "inserted_links": 2,
        }
        capture.resolve_pending_files.side_effect = AssertionError(
            "links-only entry must not touch attachment cache"
        )
        app._resource_capture_service = Mock(return_value=capture)
        backup = Mock()
        backup.run.return_value = {"state": "sync_delegated"}

        with (
            patch("app.load_config", return_value=app.config),
            patch("app.MountedResourceBackup.from_config", return_value=backup),
        ):
            app._apply_link_backfill(
                0,
                "00000000-0000-0000-0000-000000000099",
            )

        capture.backfill_links.assert_called_once_with(
            0,
            apply=True,
            run_id="00000000-0000-0000-0000-000000000099",
        )
        capture.resolve_pending_files.assert_not_called()
        backup.run.assert_called_once_with()

    def test_source_guard_runs_inside_long_lived_app(self):
        app = self.make_app()
        guard = Mock()
        guard.check.return_value = {"state": "healthy", "last_result": "healthy"}

        with (
            patch("app.load_config", return_value=app.config),
            patch("app.WeChatSourceGuard", return_value=guard),
        ):
            app._run_source_guard_consumer()

        guard.check.assert_called_once_with()
        self.assertFalse(app._source_guard_lock.locked())

    def test_link_backfill_reports_busy_without_overlapping_live_worker(self):
        app = self.make_app()
        app._resource_backup_lock.acquire()
        app._plan_link_backfill(0, "all")

        app._run_on_main.assert_called_once_with(
            app._confirm_link_backfill_plan,
            0,
            "all",
            {"state": "busy", "source_complete": False},
        )

    def test_config_revision_watcher_reconciles_cli_runtime_toggle(self):
        app = self.make_app()
        app.config = {
            "config_revision": 1,
            "resource_backup_enabled": False,
            "resource_backup_interval_seconds": 300,
        }
        current = {
            "config_revision": 2,
            "resource_backup_enabled": True,
            "resource_backup_interval_seconds": 300,
        }
        for name in (
            "_configure_monitor_timer", "_configure_daily_digest_timer",
            "_configure_resource_backup_timer", "_configure_drive_sync_timer",
            "_configure_source_guard_timer", "_rebuild_settings_menu",
            "_rebuild_monitor_menu", "_rebuild_resource_backup_menu",
            "_rebuild_drive_sync_menu",
        ):
            setattr(app, name, Mock())

        with patch("app.load_config", return_value=current):
            app._on_config_reconcile_timer(None)

        self.assertEqual(app.config, current)
        app._configure_resource_backup_timer.assert_called_once_with()

    def test_manual_notification_requires_capture_projection_and_handoff_success(self):
        app = self.make_app()
        app._rebuild_resource_backup_menu = Mock()
        with (
            patch("app.load_config", return_value=app.config),
            patch("app._notify") as notify,
        ):
            app._finish_resource_backup_run({
                "capture": {
                    "state": "degraded",
                    "scan": {"state": "source_degraded"},
                    "resolve": {"state": "skipped"},
                },
                "backup": {
                    "state": "target_failed",
                    "obsidian": {"state": "written"},
                },
            }, True)

        self.assertEqual(notify.call_args.args[1], "本轮未完成")
        self.assertIn("capture=degraded", notify.call_args.args[2])
        self.assertIn("handoff=target_failed", notify.call_args.args[2])

        with patch("app._notify") as notify:
            app._finish_resource_backup_run({
                "capture": {
                    "state": "healthy",
                    "scan": {"state": "healthy"},
                    "resolve": {"state": "skipped"},
                },
                "backup": {"state": "idle", "obsidian": {}},
            }, True)
        self.assertEqual(notify.call_args.args[1], "本轮未完成")

        with patch("app._notify") as notify:
            app._finish_resource_backup_run({
                "capture": {
                    "state": "healthy",
                    "scan": {"state": "healthy"},
                    "resolve": {"state": "skipped"},
                },
                "backup": {
                    "state": "sync_delegated",
                    "obsidian": {"state": "written"},
                },
            }, True)
        self.assertEqual(notify.call_args.args[1], "更新完成")

        pending_backup = {
            "state": "pending_resources",
            "obsidian": {"state": "written"},
            "coverage": {
                "delivered_objects": 2,
                "delivered_occurrences": 3,
                "non_delivered_occurrences": 4,
            },
            "coverage_complete": False,
        }
        healthy_capture = {
            "state": "healthy",
            "scan": {"state": "healthy"},
            "resolve": {"state": "skipped"},
        }
        with patch("app._notify") as notify:
            app._finish_resource_backup_run({
                "capture": healthy_capture,
                "backup": pending_backup,
            }, True)
        self.assertEqual(
            notify.call_args.args[1],
            "索引已更新，附件仍待补齐",
        )
        self.assertIn("已备份 2 个文件 / 3 次出现", notify.call_args.args[2])
        self.assertIn("待补齐 4 条", notify.call_args.args[2])

        with patch("app._notify") as notify:
            app._finish_resource_backup_run({
                "capture": healthy_capture,
                "backup": {**pending_backup, "copied": 1},
            }, True)
        self.assertEqual(notify.call_args.args[1], "附件备份有进展")


if __name__ == "__main__":
    unittest.main()
