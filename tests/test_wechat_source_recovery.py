"""Synthetic menu-app identity recovery; no live source, keys or API."""
import json
import hashlib
import os
import sqlite3
from contextlib import closing, contextmanager
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import ConfigError, ConfigStore
from core.monitor import state_file_for_chat
from core.monitor_source import MonitorSourceError, initialize_monitor_source_state, read_monitor_source_batch
from core.monitor_state import MonitorStateStore
from core.source_device_binding import validate_runtime_identity
from core.source_inventory import SourceInventoryStore
from core.wechat_db import WeChatDB, WeChatSourceDegraded
from core.wechat_source_recovery import SourceRecoveryError, recover_source_device
from scripts import recover_source_device as cli
def _shift_stat(value, delta):
    fields = list(value)
    fields[2] = int(fields[2]) + int(delta)
    return os.stat_result(tuple(fields))

@contextmanager
def renumbered_device(root, delta):
    """Report ``st_dev`` shifted by ``delta`` for paths under ``root``.

    ``st_dev`` is a whole-volume property, so every entry below the root (and
    every open descriptor to one of those files) reports the shifted number.
    Real path, inode, size and the stable volume UUID are untouched.
    """
    root = os.path.realpath(root)
    inodes = set()

    def collect(path):
        try:
            inodes.add(int(os.lstat(path).st_ino))
        except OSError:
            pass

    collect(root)
    for base, _dirs, files in os.walk(root):
        for name in files:
            collect(os.path.join(base, name))

    real_stat, real_lstat, real_fstat = os.stat, os.lstat, os.fstat

    def _resolve(path):
        # Realpath that only uses os.readlink/os.path, so comparing a path never
        # re-enters the patched stat/lstat functions.
        parts = os.path.abspath(os.fspath(path)).split(os.sep)
        resolved = os.sep
        for part in parts:
            if not part:
                continue
            resolved = os.path.join(resolved, part)
            target = ""
            try:
                target = os.readlink(resolved)
            except OSError:
                target = ""
            if target:
                resolved = target if os.path.isabs(target) else os.path.join(
                    os.path.dirname(resolved), target)
                resolved = os.path.normpath(resolved)
        return os.path.normpath(resolved)

    def under(path):
        try:
            candidate = _resolve(path)
        except (TypeError, OSError):
            return False
        return candidate == root or candidate.startswith(root + os.sep)

    def fake_stat(path, *args, **kwargs):
        value = real_stat(path, *args, **kwargs)
        return _shift_stat(value, delta) if under(path) else value

    def fake_lstat(path, *args, **kwargs):
        value = real_lstat(path, *args, **kwargs)
        return _shift_stat(value, delta) if under(path) else value

    def fake_fstat(fd):
        value = real_fstat(fd)
        return _shift_stat(value, delta) if int(value.st_ino) in inodes else value

    with (patch("os.stat", side_effect=fake_stat),
          patch("os.lstat", side_effect=fake_lstat),
          patch("os.fstat", side_effect=fake_fstat)):
        yield


class RuntimeSourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        capture_root = patch("core.resource_capture.DATA_DIR", str(self.root))
        capture_root.start()
        self.addCleanup(capture_root.stop)
        self.db_dir = str(self.root / "wechat/db_storage")
        path = Path(self.db_dir) / "message/message_0.db"
        path.parent.mkdir(parents=True)
        table = "Msg_" + hashlib.md5(b"synthetic@chatroom").hexdigest()
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(f"CREATE TABLE [{table}] (local_type INTEGER, create_time INTEGER, "
                         "message_content TEXT, WCDB_CT_message_content INTEGER, status INTEGER)")
            for index in range(3):
                conn.execute(f"INSERT INTO [{table}] VALUES (1,100,?,0,0)",
                             (f"sender:\nSynthetic message {index}",))
        self.states = str(self.root / "states")
        self.config = ConfigStore(str(self.root / "config.json"))
        # A missing custom store normally imports legacy app config. Seed an
        # explicit synthetic snapshot so this fixture cannot read live config.
        self.config.replace({
            "db_dir": self.db_dir, "monitor_chats": [{"username": "synthetic@chatroom",
                "name": "Synthetic"}]})
        self.inventory = SourceInventoryStore(str(self.root / "inventory.json"))
        self.reader = self.source(self.db_dir, {}, source_inventory_store=self.inventory)
        self.device = Path(self.db_dir).stat().st_dev
        self.namespace = self.reader.source_namespace
        state = initialize_monitor_source_state(self.reader, 99)
        state["last_checked_ts"] = 99
        self.state = MonitorStateStore(state_file_for_chat("synthetic@chatroom", state_dir=self.states))
        self.state.commit(0, state)

    def source(self, *args, **kwargs):
        return WeChatDB(*args, cache_root=str(self.root / "cache"), **kwargs)

    def recover(self, **kwargs):
        return recover_source_device(previous_device=self.device,
                                     expected_namespace=self.namespace, keys={},
                                     config_store=self.config, inventory_store=self.inventory,
                                     state_dir=self.states, source_factory=self.source, **kwargs)

    def test_device_recovery_keeps_checkpoint_and_resumes_exact_rows(self):
        before = Path(self.state.path).read_bytes()
        inventory_before = Path(self.inventory.path).read_bytes()
        original_batch = read_monitor_source_batch(self.reader, "synthetic@chatroom",
                                                   self.state.inspect().data, raw_limit=10)
        with patch("core.source_device_binding.read_volume_identity", return_value="a" * 32), \
                renumbered_device(self.db_dir, 2):
            unbound = self.source(self.db_dir, {}, source_inventory_store=self.inventory)
            self.assertNotEqual(unbound.source_namespace, self.namespace)
            with self.assertRaises(MonitorSourceError) as blocked:
                read_monitor_source_batch(unbound, "synthetic@chatroom", self.state.inspect().data,
                                          raw_limit=10)
            self.assertIn(blocked.exception.code, {"source_generation_admission_required"})
            # The unbound read creates a separate observed namespace; recovery
            # must retain both evidence sets rather than editing inventory.
            inventory_before = Path(self.inventory.path).read_bytes()
            original_config = Path(self.config.path).read_bytes()
            self.assertEqual(self.recover()["state"], "verified")
            self.assertEqual(Path(self.config.path).read_bytes(), original_config)
            result = self.recover(apply=True)
            self.assertEqual((result["state"], result["verified_shards"]), ("recovered", 1))
            identity = self.config.read()["wechat_source_identity"]
            resumed = self.source(self.db_dir, {}, source_inventory_store=self.inventory,
                                  source_binding=identity["binding"])
            batch = read_monitor_source_batch(resumed, "synthetic@chatroom",
                                              self.state.inspect().data, raw_limit=10)
            self.assertEqual(batch.raw_count, 3)
            self.assertTrue(batch.source_eof)
            self.assertEqual([m["source_message_id"] for m in batch.raw_messages],
                             [m["source_message_id"] for m in original_batch.raw_messages])
            self.assertEqual(self.recover(apply=True)["state"], "already_recovered")
        self.assertEqual(Path(self.state.path).read_bytes(), before)
        self.assertEqual(Path(self.inventory.path).read_bytes(), inventory_before)

    def test_changed_volume_rejects_installed_binding(self):
        with patch("core.source_device_binding.read_volume_identity", return_value="a" * 32):
            self.recover(apply=True)
        cfg = self.config.read()
        with patch("core.source_device_binding.read_volume_identity", return_value="b" * 32):
            with self.assertRaises(WeChatSourceDegraded) as failure:
                WeChatDB.for_runtime(self.db_dir, {}, config=cfg)
        self.assertEqual(failure.exception.code, "source_device_binding_volume_changed")

    def test_real_shard_replacement_rejects_without_state_changes(self):
        before = Path(self.config.path).read_bytes(), Path(self.state.path).read_bytes()
        path = Path(self.db_dir) / "message/message_0.db"
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        with patch("core.source_device_binding.read_volume_identity", return_value="a" * 32):
            with self.assertRaises(SourceRecoveryError) as failure:
                self.recover(apply=True)
        self.assertEqual(failure.exception.code, "source_device_lineage_mismatch")
        self.assertEqual((Path(self.config.path).read_bytes(), Path(self.state.path).read_bytes()), before)

    def test_wrong_namespace_and_new_shard_fail_closed(self):
        with patch("core.source_device_binding.read_volume_identity", return_value="a" * 32):
            with self.assertRaises(SourceRecoveryError):
                recover_source_device(previous_device=self.device, expected_namespace="wrong", keys={},
                                      config_store=self.config)
            source = Path(self.db_dir) / "message/message_0.db"
            (source.parent / "message_1.db").write_bytes(source.read_bytes())
            with self.assertRaises(SourceRecoveryError) as failure:
                self.recover(apply=True)
        self.assertEqual(failure.exception.code, "source_device_lineage_mismatch")

    def test_config_keeps_identity_on_disjoint_update_and_rejects_path_change(self):
        with patch("core.source_device_binding.read_volume_identity", return_value="a" * 32):
            self.recover(apply=True)
        original = self.config.read()["wechat_source_identity"]
        self.config.update(lambda cfg: cfg.update({"monitor_interval_minutes": 15}))
        self.assertEqual(self.config.read()["wechat_source_identity"], original)
        with self.assertRaises(ConfigError):
            self.config.update(lambda cfg: cfg.update({"db_dir": str(self.root / "other-source")}))

    def test_recorded_identity_validation_never_observes_source(self):
        with patch("core.source_device_binding.read_volume_identity", return_value="a" * 32):
            self.recover(apply=True)
        identity = self.config.read()["wechat_source_identity"]
        with patch("core.source_device_binding.os.stat", side_effect=AssertionError("source stat")), \
                patch("core.source_device_binding.os.path.realpath", side_effect=AssertionError("source realpath")):
            self.assertEqual(validate_runtime_identity(identity, self.db_dir), identity)

    def test_cli_denies_before_config_keys_and_source(self):
        with patch("core.key_extractor.get_cached_keys", side_effect=AssertionError("keys")), \
                patch("core.wechat_source_recovery.recover_source_device", side_effect=AssertionError("source")):
            self.assertEqual(cli.main(["--previous-device", "1", "--expected-namespace", "synthetic"]), 2)


if __name__ == "__main__":
    unittest.main()
