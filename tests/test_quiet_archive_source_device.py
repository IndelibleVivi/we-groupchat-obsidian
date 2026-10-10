"""Synthetic regression tests for explicit source device-renumber recovery.

These tests use only synthetic fixtures. A "device renumber" is simulated by
wrapping ``os.stat``/``os.lstat``/``os.fstat`` so that paths under the source
root report a shifted ``st_dev`` while the real path, inode and stable volume
identity are unchanged. That is exactly the abstract defect: the durable source
namespace must not change and the producer must recover explicitly.
"""
import io
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager, redirect_stdout
from unittest.mock import patch

from core.source_device_binding import (
    SourceDeviceBindingError,
    source_namespace,
    resolve_root_identity,
)
from core.source_inventory import source_namespaces_for_root
from core.quiet_archive_producer import ProducerProfile
from scripts import quiet_archive_handoff as cli

from tests.test_quiet_archive_producer import (
    _synthetic_profile_value,
    _write_synthetic_source,
)


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


class StandaloneProducerSourceDeviceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile_file = self.root / "profile.json"
        self.value = _synthetic_profile_value(self.root)
        (self.root / "handoff").mkdir()
        self.profile_file.write_text(json.dumps(self.value), encoding="utf-8")
        self.profile_file.chmod(0o600)
        self.profile = ProducerProfile.read(str(self.profile_file))
        self.traps = ExitStack()
        self.traps.enter_context(patch("core.source_device_binding.read_volume_identity",
                                      return_value="a" * 32))
        self.addCleanup(self.traps.close)
        for target in ("scripts.quiet_archive_handoff.load_config",
                       "scripts.quiet_archive_handoff.update_config",
                       "scripts.quiet_archive_handoff._source",
                       "core.resource_capture.load_config",
                       "core.config.load_config",
                       "core.key_extractor.get_cached_keys"):
            self.traps.enter_context(patch(target, side_effect=AssertionError(target)))

    def run_cli(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["--profile", str(self.profile_file), *arguments])
        return code, json.loads(output.getvalue())

    def initialize_and_capture(self):
        _write_synthetic_source(self.value)
        code, result = self.run_cli("init")
        self.assertEqual((code, result["state"]), (0, "initialized"))
        code, refreshed = self.run_cli(
            "refresh", "--allow-transient-wechat-source-read", "--max-rounds", "3")
        self.assertEqual(code, 0, refreshed)
        # Mirror an adopted producer, which pins the durable source namespace
        # in its marker (a fresh ``init`` marker leaves it absent).
        marker_path = Path(self.profile.paths()["marker"])
        marker = json.loads(marker_path.read_text())
        marker["source_namespace"] = self.durable_namespace()
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        marker_path.chmod(0o600)
        return refreshed

    def capture_state(self):
        from core.quiet_archive_handoff import QuietArchiveHandoff
        capture = self.profile.capture()
        records = QuietArchiveHandoff(
            self.profile.config(), capture=capture)._context_records(capture.occurrences())
        with closing(sqlite3.connect(self.profile.paths()["ledger"])) as conn:
            conn.row_factory = sqlite3.Row
            shards = [dict(row) for row in conn.execute(
                "SELECT chat_username, source_shard_id, cursor_timestamp, "
                "cursor_message_ids_json, source_cursor_token FROM resource_shards")]
        return {
            "context_ids": sorted(row["event_id"] for row in records),
            "source_message_ids": sorted(row["source_message_id"] for row in records),
            "shards": shards,
        }

    def durable_namespace(self):
        with closing(sqlite3.connect(self.profile.paths()["ledger"])) as conn:
            row = conn.execute(
                "SELECT value FROM resource_meta WHERE key='source_inventory_evidence'"
            ).fetchone()
        return json.loads(row[0])["source_namespace"]

    def observed_device(self):
        return int(os.stat(os.path.realpath(self.value["source"]["db_dir"])).st_dev)

    def test_defect_then_recovery_preserves_namespace_ids_and_cursor(self):
        self.initialize_and_capture()
        before = self.capture_state()
        durable = self.durable_namespace()
        self.assertTrue(durable)
        device = self.observed_device()

        with renumbered_device(self.value["source"]["db_dir"], 2):
            shifted_ns = source_namespaces_for_root(self.value["source"]["db_dir"])[1]
            self.assertNotEqual(shifted_ns, durable)
            code, blocked = self.run_cli(
                "refresh", "--allow-transient-wechat-source-read", "--max-rounds", "1")
            self.assertEqual(
                (code, blocked["error_code"]),
                (2, "producer_source_namespace_mismatch"))

            code, recovered = self.run_cli(
                "recover-source-device",
                "--previous-device", str(device),
                "--expected-namespace", durable,
                "--allow-transient-wechat-source-read")
            self.assertEqual((code, recovered["state"]), (0, "recovered"), recovered)
            marker = json.loads(Path(self.profile.paths()["marker"]).read_text())
            self.assertEqual(marker["source_identity"]["original_device"], device)
            self.assertEqual(recovered["verified_shards"], 1)
            self.assertEqual(set(self.profile.paths()), set(ProducerProfile.read(str(self.profile_file)).paths()))

            code, again = self.run_cli(
                "recover-source-device",
                "--previous-device", str(device),
                "--expected-namespace", durable,
                "--allow-transient-wechat-source-read")
            self.assertEqual((code, again["state"]), (0, "already_recovered"), again)

            source = self.profile.source()
            self.assertEqual(source.source_namespace, durable)
            code, refreshed = self.run_cli(
                "refresh", "--allow-transient-wechat-source-read", "--max-rounds", "3")
            self.assertEqual((code, refreshed["state"]), (0, "eof"), refreshed)
            after = self.capture_state()
            self.assertEqual(after["context_ids"], before["context_ids"])
            self.assertEqual(after["source_message_ids"], before["source_message_ids"])
            self.assertEqual(after["shards"], before["shards"])

    def test_recovery_is_automatic_for_later_renumbering(self):
        self.initialize_and_capture()
        durable = self.durable_namespace()
        device = self.observed_device()
        root = os.path.realpath(self.value["source"]["db_dir"])
        with renumbered_device(root, 2):
            code, recovered = self.run_cli(
                "recover-source-device", "--previous-device", str(device),
                "--expected-namespace", durable,
                "--allow-transient-wechat-source-read")
            self.assertEqual(recovered["state"], "recovered")
            with renumbered_device(root, 7):
                source = self.profile.source()
                self.assertEqual(source.source_namespace, durable)
                self.assertEqual(
                    source.cache_namespace,
                    source_namespaces_for_root(root)[0])

    def test_recovery_rejects_wrong_namespace_and_wrong_previous_device(self):
        self.initialize_and_capture()
        durable = self.durable_namespace()
        device = self.observed_device()
        with renumbered_device(self.value["source"]["db_dir"], 2):
            code, result = self.run_cli(
                "recover-source-device", "--previous-device", str(device),
                "--expected-namespace", "0" * 32,
                "--allow-transient-wechat-source-read")
            self.assertEqual(
                (code, result["error_code"]),
                (2, "producer_recovery_namespace_mismatch"))
            code, result = self.run_cli(
                "recover-source-device", "--previous-device", str(device + 100),
                "--expected-namespace", durable,
                "--allow-transient-wechat-source-read")
            self.assertEqual(
                (code, result["error_code"]),
                (2, "producer_recovery_namespace_mismatch"))
            self.assertNotIn("source_identity", self.profile.marker(required=True))

    def test_recovery_requires_source_read_grant_and_fails_on_db_replacement(self):
        self.initialize_and_capture()
        durable = self.durable_namespace()
        device = self.observed_device()
        code, denied = self.run_cli(
            "recover-source-device", "--previous-device", str(device),
            "--expected-namespace", durable)
        self.assertEqual(
            (code, denied["error_code"]),
            (2, "allow_transient_wechat_source_read_required"))
        # Replace the shard at the same path: a new inode is a real DB
        # replacement and must fail the lineage check.
        source_db = Path(self.value["source"]["db_dir"]) / "message/message_0.db"
        replacement = Path(self.value["source"]["db_dir"]) / "message/replacement.db"
        with closing(sqlite3.connect(replacement)) as conn, conn:
            conn.execute("CREATE TABLE Msg_fixture (local_type INTEGER, create_time INTEGER, "
                         "message_content TEXT, WCDB_CT_message_content INTEGER, status INTEGER)")
        os.replace(replacement, source_db)
        with renumbered_device(self.value["source"]["db_dir"], 2):
            code, result = self.run_cli(
                "recover-source-device", "--previous-device", str(device),
                "--expected-namespace", durable,
                "--allow-transient-wechat-source-read")
            self.assertEqual(
                (code, result["error_code"]), (2, "source_device_lineage_mismatch"))
            self.assertNotIn("source_identity", self.profile.marker(required=True))

    def test_recovery_serializes_with_capture_and_binding_guard(self):
        from core.resource_capture import resource_capture_operation_lock
        self.initialize_and_capture()
        durable = self.durable_namespace()
        device = self.observed_device()
        with renumbered_device(self.value["source"]["db_dir"], 2):
            with resource_capture_operation_lock(self.profile.config()):
                code, busy = self.run_cli(
                    "recover-source-device", "--previous-device", str(device),
                    "--expected-namespace", durable,
                    "--allow-transient-wechat-source-read")
            self.assertEqual((code, busy["error_code"]), (2, "capture_worker_busy"))
            self.assertNotIn("source_identity", self.profile.marker(required=True))
            marker_path = Path(self.profile.paths()["marker"])
            marker = self.profile.marker(required=True)
            marker["source_identity"] = {"root_inode": 1, "volume_identity": "a" * 32,
                                         "original_device": device}
            marker_path.write_text(json.dumps(marker))
            from core.source_adapter import SourceUnavailableError
            with self.assertRaises(SourceUnavailableError) as raised:
                self.profile.source()
            self.assertEqual(raised.exception.code, "source_device_binding_inode_changed")
            code, conflict = self.run_cli(
                "recover-source-device", "--previous-device", str(device),
                "--expected-namespace", durable, "--allow-transient-wechat-source-read")
            self.assertEqual((code, conflict["error_code"]),
                             (2, "producer_device_binding_conflict"))

    def test_status_plan_export_never_stat_or_open_the_source(self):
        self.initialize_and_capture()
        source_root = os.path.realpath(self.value["source"]["db_dir"])
        real_stat, real_lstat, real_open = os.stat, os.lstat, os.open

        def guard(path, *args, **kwargs):
            candidate = os.path.abspath(os.fspath(path)) if not isinstance(path, int) else ""
            if candidate == source_root or candidate.startswith(source_root + os.sep):
                raise AssertionError("read-only command touched the source: " + candidate)
            return path

        def stat_guard(path, *args, **kwargs):
            guard(path)
            return real_stat(path, *args, **kwargs)

        def lstat_guard(path, *args, **kwargs):
            guard(path)
            return real_lstat(path, *args, **kwargs)

        def open_guard(path, *args, **kwargs):
            guard(path)
            return real_open(path, *args, **kwargs)

        with (patch("os.stat", side_effect=stat_guard),
              patch("os.lstat", side_effect=lstat_guard),
              patch("os.open", side_effect=open_guard)):
            for command in ("status", "plan", "export"):
                code, result = self.run_cli(command)
                self.assertEqual(code, 0, (command, result))

    def test_missing_volume_identity_fails_closed(self):
        self.initialize_and_capture()
        durable = self.durable_namespace()
        device = self.observed_device()
        with (renumbered_device(self.value["source"]["db_dir"], 2),
              patch("core.quiet_archive_producer.resolve_root_identity",
                    side_effect=SourceDeviceBindingError("volume_identity_unavailable"))):
            code, result = self.run_cli(
                "recover-source-device", "--previous-device", str(device),
                "--expected-namespace", durable,
                "--allow-transient-wechat-source-read")
            self.assertEqual((code, result["error_code"]), (2, "volume_identity_unavailable"))
            self.assertNotIn("source_identity", self.profile.marker(required=True))

    def test_recovered_binding_fails_closed_on_volume_or_inode_change(self):
        self.initialize_and_capture()
        durable = self.durable_namespace()
        device = self.observed_device()
        with renumbered_device(self.value["source"]["db_dir"], 2):
            self.assertEqual(
                self.run_cli("recover-source-device", "--previous-device", str(device),
                             "--expected-namespace", durable,
                             "--allow-transient-wechat-source-read")[1]["state"],
                "recovered")
            with patch("core.source_device_binding.read_volume_identity",
                       return_value="different-volume"):
                from core.source_adapter import SourceUnavailableError
                with self.assertRaises(SourceUnavailableError) as raised:
                    self.profile.source()
                self.assertEqual(raised.exception.code,
                                 "source_device_binding_volume_changed")

    def test_volume_identity_read_reconstructs_existing_namespace_formula(self):
        _write_synthetic_source(self.value)
        root = os.path.realpath(self.value["source"]["db_dir"])
        identity = resolve_root_identity(root)
        self.assertTrue(identity["volume_identity"])
        self.assertEqual(
            source_namespace(root, identity["device"], identity["root_inode"]),
            source_namespaces_for_root(root)[1])

    def test_missing_corrupt_and_empty_inventory_cannot_authorize_recovery(self):
        self.initialize_and_capture()
        durable, device = self.durable_namespace(), self.observed_device()
        inventory_path = Path(self.profile.paths()["inventory"])
        saved = inventory_path.read_text()
        for bad in (None, "{", json.dumps({"schema": "we-groupchat-obsidian.source-inventory.v1",
                                          "revision": 1, "sources": {}})):
            with self.subTest(bad=bad):
                if bad is None:
                    inventory_path.unlink()
                else:
                    inventory_path.write_text(bad)
                code, result = self.run_cli(
                    "recover-source-device", "--previous-device", str(device),
                    "--expected-namespace", durable, "--allow-transient-wechat-source-read")
                self.assertEqual(code, 2, result)
                self.assertNotIn("source_identity", self.profile.marker(required=True))
                inventory_path.write_text(saved)

    def test_binding_keeps_cache_physical_and_detects_later_db_replacement(self):
        self.initialize_and_capture()
        durable, device = self.durable_namespace(), self.observed_device()
        old_source = self.profile.source()
        cache_before = old_source.cache_namespace
        path = Path(self.value["source"]["db_dir"]) / "message/message_0.db"
        key_before = old_source._db_shard_identity(str(path))
        generation_before = old_source._source_generation_marker("message/message_0.db")
        with renumbered_device(self.value["source"]["db_dir"], 2):
            self.assertEqual(self.run_cli(
                "recover-source-device", "--previous-device", str(device),
                "--expected-namespace", durable, "--allow-transient-wechat-source-read")[0], 0)
            source = self.profile.source()
            self.assertNotEqual(source.cache_namespace, cache_before)
            self.assertNotEqual(source._db_shard_identity(str(path)), key_before)
            self.assertEqual(source._source_generation_marker("message/message_0.db"), generation_before)
            replacement = path.with_suffix(".new")
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
            self.assertNotEqual(source._source_generation_marker("message/message_0.db"), generation_before)

    def test_failed_marker_replace_preserves_original_and_retry_is_idempotent(self):
        self.initialize_and_capture()
        durable, device = self.durable_namespace(), self.observed_device()
        path = Path(self.profile.paths()["marker"])
        original = path.read_bytes()
        args = ("recover-source-device", "--previous-device", str(device),
                "--expected-namespace", durable, "--allow-transient-wechat-source-read")
        with patch("core.quiet_archive_producer.os.replace", side_effect=OSError("interrupted")):
            self.assertEqual(self.run_cli(*args)[0], 2)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.run_cli(*args)[1]["state"], "recovered")
        recovered = path.read_bytes()
        self.assertEqual(self.run_cli(*args)[1]["state"], "already_recovered")
        self.assertEqual(path.read_bytes(), recovered)

    def test_malformed_binding_rejected_without_source_read(self):
        self.initialize_and_capture()
        path = Path(self.profile.paths()["marker"])
        marker = self.profile.marker(required=True)
        marker["source_identity"] = {"root_inode": True, "volume_identity": "../x", "original_device": 1}
        path.write_text(json.dumps(marker))
        with patch("core.source_device_binding.resolve_root_identity", side_effect=AssertionError("source")):
            self.assertEqual(self.run_cli("status")[1]["error_code"], "source_device_binding_invalid")



if __name__ == "__main__":
    unittest.main()
