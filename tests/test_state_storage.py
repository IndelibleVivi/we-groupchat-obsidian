"""Behavior of the three W0.2B.2 private state owners."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.config import ConfigStore, normalize_path_value
from core.monitor_state import MonitorStateError, MonitorStateStore
from core.source_inventory import SourceInventoryError, SourceInventoryStore


class StateStorageTests(unittest.TestCase):
    def test_three_owners_use_private_publication_and_preserve_revisions_on_failure(self):
        from core.platform import create_platform_services

        services = create_platform_services()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "私有 state"
            config = ConfigStore(root / "config.json", platform_services=services)
            monitor = MonitorStateStore(root / "monitor.json", platform_services=services)
            inventory = SourceInventoryStore(root / "inventory.json", platform_services=services)
            config.replace({"monitor_enabled": False})
            monitor.initialize_if_absent({"last_checked_ts": 10})
            inventory.reconcile("synthetic", [{
                "relative_path": "message/message_1.db", "generation_id": "one", "state": "present",
            }])
            files = [root / name for name in ("config.json", "monitor.json", "inventory.json")]
            before = [path.read_bytes() for path in files]
            self.assertTrue(services.private_storage.verify(root))
            for path in files:
                self.assertTrue(services.private_storage.verify(path))

            with patch.object(services.atomic_publisher, "write_bytes", side_effect=OSError("synthetic")):
                with self.assertRaises(OSError):
                    config.update(lambda value: {**value, "monitor_enabled": True})
                with self.assertRaisesRegex(MonitorStateError, "monitor_state_write_failed"):
                    monitor.update(lambda value: {**value, "last_checked_ts": 11})
                with self.assertRaisesRegex(SourceInventoryError, "source_inventory_write_failed"):
                    inventory.reconcile("synthetic", [{
                        "relative_path": "message/message_2.db", "generation_id": "two", "state": "present",
                    }])
            self.assertEqual([path.read_bytes() for path in files], before)
            self.assertEqual(config.read()["config_revision"], 1)
            self.assertEqual(monitor.read().revision, 1)
            self.assertEqual(inventory.inspect("synthetic").inventory_revision, 1)

    def test_read_only_inspection_does_not_create_storage_or_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "absent" / "nested"
            self.assertFalse(MonitorStateStore(root / "monitor.json").inspect().existed)
            self.assertFalse(SourceInventoryStore(root / "inventory.json").inspect("synthetic").complete)
            self.assertFalse(root.parent.exists())

    def test_inspection_of_existing_state_does_not_restrict_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "monitor.json"
            path.write_text('{"last_checked_ts": 10}', encoding="utf-8")
            before = (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            with patch("core.state_storage.StateStorage.prepare", side_effect=AssertionError("inspection mutated storage")):
                self.assertEqual(MonitorStateStore(path).inspect().data["last_checked_ts"], 10)
            self.assertEqual((path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns), before)

    @unittest.skipUnless(os.name == "nt", "Windows native path input")
    def test_windows_config_path_values_preserve_native_backslashes(self):
        path = r"C:\Users\Example User\微信\db_storage"
        self.assertEqual(normalize_path_value(path), path)

    @unittest.skipUnless(os.name == "nt", "Windows long-path storage")
    def test_windows_owners_publish_and_inspect_beyond_legacy_path_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).joinpath(*(["state-" + "x" * 45] * 6))
            self.assertGreater(len(str(root)), 260)
            ConfigStore(root / "config.json").replace({"monitor_enabled": False})
            monitor = MonitorStateStore(root / "monitor.json")
            monitor.initialize_if_absent({"last_checked_ts": 10})
            self.assertEqual(monitor.inspect().data["last_checked_ts"], 10)
            inventory = SourceInventoryStore(root / "inventory.json")
            inventory.reconcile("synthetic", [{
                "relative_path": "message/message_1.db", "generation_id": "one", "state": "present",
            }])
            self.assertTrue(inventory.inspect("synthetic").complete)


if __name__ == "__main__":
    unittest.main()
