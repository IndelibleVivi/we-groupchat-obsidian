"""Operational freshness, bounded failures and source-free inspection."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import WeGroupchatObsidianApp
from core.config import ConfigError, ConfigStore
from core.monitor import MonitorProviderError, TopicMonitor, state_file_for_chat
from core.monitor_ops import (
    MonitorOpsError, MonitorOpsStore, classify_attempt, monitor_operational_status,
    observe_monitor_attempt,
)
from core.monitor_source import MonitorSourceError
from core.monitor_state import MonitorStateStore
from core.source_device_binding import source_namespace
from core.source_inventory import SourceInventoryStore
from scripts import health_check


class MonitorOpsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.states = str(self.root / "states")
        self.state = MonitorStateStore(state_file_for_chat("synthetic@chatroom", self.states))
        self.state.initialize_if_absent({"last_checked_ts": 100})
        self.ops = MonitorOpsStore(self.root / "ops.json")
        self.cfg = {"monitor_enabled": True, "monitor_interval_minutes": 3,
                    "monitor_chats": [{"username": "synthetic@chatroom"}]}

    def status(self, now=1000):
        return monitor_operational_status(self.cfg, state_dir=self.states,
                                          ops_path=self.ops.path, now=now)

    def observe(self, result, now=1000):
        observe_monitor_attempt(self.state, result, ops_store=self.ops, now=now)

    def test_valid_checkpoint_does_not_prove_running_then_eof_refreshes_success(self):
        self.assertEqual(self.status()["state"], "unknown")
        self.observe({"status": "no_messages", "source_eof": True})
        result = self.status()
        self.assertEqual((result["state"], result["last_success_ts"]), ("healthy", 1000))
        self.assertEqual(result["progress_watermark_ts"], 100)
        self.assertEqual(self.status(now=2000)["state"], "stale")

    def test_new_blocked_attempt_never_reuses_older_success(self):
        self.observe({"status": "no_match"}, 900)
        self.observe(MonitorSourceError("source_generation_admission_required"), 1000)
        result = self.status()
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["last_success_ts"], 900)
        self.assertEqual(result["blocked_codes"], ["source_generation_admission_required"])
        self.assertNotIn("synthetic@chatroom", str(result))
        self.assertNotIn(str(self.root), str(result))

    def test_provider_reason_survives_exception_wrapping_without_error_content(self):
        monitor = TopicMonitor.__new__(TopicMonitor)
        monitor.config = {"ai_provider": "custom", "ai_model": "synthetic"}
        for message, code in [("Missing credentials", "ai_credential_unavailable"),
                              ("401 authentication failed", "ai_auth_failed"),
                              ("timeout", "ai_timeout")]:
            with patch("ai.factory.create_provider", side_effect=RuntimeError(message)):
                with self.assertRaises(MonitorProviderError) as failed:
                    monitor._call_ai_provider("synthetic")
            self.assertEqual(classify_attempt(failed.exception), (code, "blocked"))
            self.observe(failed.exception)
            self.assertEqual(self.status()["blocked_codes"], [code])

    def test_eof_must_be_verified_and_busy_does_not_replace_attempt(self):
        self.observe({"status": "no_messages", "source_eof": "true"})
        self.assertEqual(self.status()["blocked_codes"], ["source_eof_unverified"])
        before = Path(self.ops.path).read_bytes()
        self.observe({"status": "monitor_worker_busy"}, 1001)
        self.assertEqual(Path(self.ops.path).read_bytes(), before)

    def test_corrupt_future_and_nonfinite_evidence_never_becomes_healthy(self):
        self.observe({"status": "no_match"}, 5000)
        self.assertEqual(self.status()["state"], "unknown")
        for value in [float("inf"), float("nan")]:
            data = json.loads(Path(self.ops.path).read_text())
            record = next(iter(data["chats"].values()))
            record["last_attempt_ts"] = value
            Path(self.ops.path).write_text(json.dumps(data))
            self.assertEqual(self.status()["state"], "unknown")
        Path(self.ops.path).write_text("{broken")
        self.assertEqual(self.status()["state"], "unknown")

    def test_inspection_does_not_initialize_and_rejects_symlinks(self):
        absent = self.root / "absent" / "ops.json"
        self.assertFalse(MonitorOpsStore(absent).inspect().existed)
        self.assertFalse(absent.parent.exists())
        self.observe({"status": "no_match"})
        link = self.root / "link.json"
        link.symlink_to(self.ops.path)
        with self.assertRaises(MonitorOpsError):
            MonitorOpsStore(link).inspect()

    def test_production_check_records_and_dry_run_never_records(self):
        monitor = TopicMonitor.__new__(TopicMonitor)
        monitor.state_store = self.state
        monitor._check_once = lambda **kw: {"status": "no_messages", "source_eof": True}
        with patch("core.monitor_ops.MonitorOpsStore", return_value=self.ops):
            monitor.check_once(dry_run=True)
            self.assertFalse(Path(self.ops.path).exists())
            monitor.check_once()
            self.assertTrue(Path(self.ops.path).is_file())
            before = Path(self.state.path).read_bytes()
            with patch.object(self.ops, "record_attempt", side_effect=MonitorOpsError("monitor_ops_write_failed")):
                self.assertEqual(monitor.check_once()["status"], "no_messages")
            self.assertEqual(Path(self.state.path).read_bytes(), before)

    def test_menu_uses_prepared_evidence_and_reports_stale_without_source_io(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app._monitor_ops_status = {"state": "healthy", "last_attempt_ts": 100,
                                   "last_success_ts": 100, "stale_after_seconds": 900}
        with patch("app.time.time", return_value=2000), \
                patch("app.monitor_operational_status", side_effect=AssertionError("disk IO")):
            self.assertIn("检查停滞", app._monitor_ops_detail_label())
            self.assertIn("最近成功检查", str(app._monitor_ops_evidence_lines()))
        app._monitor_ops_status = {"state": "blocked", "blocked_codes": ["source_generation_admission_required"]}
        self.assertIn("已阻断", app._monitor_ops_detail_label())
        app._monitor_ops_status = {"state": "unknown", "last_attempt_ts": 1e308}
        self.assertIn("最近检查: 未知", app._monitor_ops_evidence_lines())

    def test_config_inspect_is_nonmutating_and_invalid_config_fails_closed(self):
        path = self.root / "new" / "config.json"
        self.assertIsNone(ConfigStore(path).inspect())
        self.assertFalse(path.parent.exists())
        path.parent.mkdir()
        path.write_text("[]")
        with self.assertRaises(ConfigError):
            ConfigStore(path).inspect()
        link = self.root / "config-link.json"
        link.symlink_to(path)
        with self.assertRaises(ConfigError):
            ConfigStore(link).inspect()

    def test_bound_inventory_health_uses_stored_identity_without_source_stat(self):
        inventory = SourceInventoryStore(str(self.root / "inventory.json"))
        root = "/nonexistent/protected/wechat"
        namespace = source_namespace(root, 1, 2)
        inventory.reconcile(namespace, [{"relative_path": "message/message_0.db",
                                         "generation_id": "synthetic", "state": "present"}])
        cfg = {"db_dir": root, "wechat_source_identity": {"source_db_dir": root,
            "source_namespace": namespace, "binding": {"root_inode": 2,
                "original_device": 1, "volume_identity": "a" * 32}}}
        with patch("core.source_inventory.source_namespaces_for_root", side_effect=AssertionError("source stat")), \
                patch("core.config.auto_detect_db_dir", side_effect=AssertionError("source discovery")):
            self.assertEqual(health_check.source_inventory_health(cfg, store=inventory)["state"], "complete")
        cfg["wechat_source_identity"] = {"source_namespace": namespace}
        self.assertEqual(health_check.source_inventory_health(cfg, store=inventory)["state"], "degraded")

    def test_legacy_multiple_namespaces_remain_ambiguous_and_inspect_is_read_only(self):
        inventory = SourceInventoryStore(str(self.root / "inventory.json"))
        for namespace in ["a" * 32, "b" * 32]:
            inventory.reconcile(namespace, [{"relative_path": "message/message_0.db",
                                            "generation_id": namespace, "state": "present"}])
        before = Path(inventory.path).read_bytes()
        result = health_check.source_inventory_health({"db_dir": "/protected/source"}, store=inventory)
        self.assertEqual(result["state"], "ambiguous")
        self.assertEqual(Path(inventory.path).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
