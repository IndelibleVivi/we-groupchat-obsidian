import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from unittest.mock import patch

from core.quiet_archive_handoff import QuietArchiveHandoff, HANDOFF_SCHEMA
from core.resource_backup import MountedResourceBackup
from core.resource_capture import SelectedResourceCapture
from scripts import quiet_archive_handoff as cli
from tests.test_resource_backup import KeysetSource, InventoryAwareSource


def message(index, text=None, resources=None):
    return {"source_message_id": f"wgmsg_fixture_{index}", "timestamp": 100,
            "time_str": "1970-01-01 00:01", "type": 1,
            "sender": "private-internal-sender", "raw_sender_id": "private-raw-id",
            "text": f"完整原始经验 {index}" if text is None else text,
            "resources": resources or []}


class QuietArchiveHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "handoff").mkdir()
        self.config = {
            "quiet_archive_handoff_enabled": True,
            "quiet_archive_handoff_target": str(self.root / "handoff"),
            "resource_capture_db": str(self.root / "capture.db"),
            "monitor_knowledge_db": str(self.root / "knowledge.db"),
            "monitor_obsidian_root": str(self.root / "obsidian"),
            "attachment_archive_root": str(self.root / "archive"),
            "attachment_archive_min_free_bytes": 0, "resource_backup_min_free_bytes": 0,
            "monitor_chats": [{"username": "fixture@chatroom", "name": "合成群聊"}],
            "resource_backup_selected_chats": [{"username": "fixture@chatroom", "alias": "合成群聊",
                "selection_id": "00000000-0000-0000-0000-000000000001", "selected_since": 1}],
            "resource_backup_max_messages_per_scan": 2,
        }
        self.source = KeysetSource({"fixture-shard": [message(1), message(2), message(3)]})
        self.capture = self.make_capture()
        self.capture.initialize_selected_chat_cursors(start_timestamp=0)

    def make_capture(self, source=None):
        return SelectedResourceCapture(self.config, source=self.source if source is None else source,
                                       now_func=lambda: 200)

    def exporter(self, capture=None):
        return QuietArchiveHandoff(self.config, capture=capture or self.capture, now_func=lambda: 200)

    def read_snapshot(self, result):
        path = self.root / "handoff/wgo-resource-backup/v3/snapshots" / result["snapshot"]["snapshot_id"]
        manifest_bytes = (path / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        complete = json.loads((path / "COMPLETE").read_bytes())
        self.assertEqual(complete["manifest_sha256"], hashlib.sha256(manifest_bytes).hexdigest())
        ext = manifest["machine_handoff"]
        for key in ("contexts", "coverage"):
            self.assertEqual(ext[f"{key}_sha256"], hashlib.sha256((path / ext[f"{key}_file"]).read_bytes()).hexdigest())
        contexts = [json.loads(line) for line in (path / "contexts.jsonl").read_text().splitlines()]
        return path, manifest, contexts, json.loads((path / "coverage.json").read_text())

    def test_bounded_raw_eof_and_all_visible_contexts_across_restart(self):
        first = self.capture.scan()
        self.assertFalse(first["raw_eof"])
        self.assertEqual(first["pending_shards"], 1)
        self.assertTrue(self.capture.context_coverage()["inventory"]["complete"])
        restarted = self.make_capture()
        last = restarted.scan()
        self.assertTrue(last["raw_eof"])
        self.assertEqual(len(restarted.contexts()), 3)
        self.assertEqual(restarted.occurrences(), [])
        with closing(sqlite3.connect(self.config["resource_capture_db"])) as conn, conn:
            self.assertEqual(conn.execute("SELECT cursor_message_ids_json FROM resource_shards").fetchone()[0], "[]")

    def test_disabled_capture_keeps_old_retention_and_marks_enablement_gap(self):
        self.config["quiet_archive_handoff_enabled"] = False
        old = self.make_capture()
        old.drain()
        self.assertEqual(old.contexts(), [])
        self.assertTrue(old.context_coverage()["contexts"]["unmeasured_history_gap"])
        self.config["quiet_archive_handoff_enabled"] = True
        enabled = self.make_capture()
        enabled.scan()
        self.assertEqual(enabled.contexts(), [])
        self.assertTrue(enabled.context_coverage()["contexts"]["unmeasured_history_gap"])

    def test_context_failure_rolls_back_occurrence_and_cursor(self):
        self.source.messages_by_shard["fixture-shard"] = [message(1, "https://example.test/guide")]
        with patch.object(self.capture, "_insert_context", side_effect=sqlite3.OperationalError("synthetic")):
            with self.assertRaises(sqlite3.OperationalError):
                self.capture.scan()
        self.assertEqual(self.capture.contexts(), [])
        self.assertEqual(self.capture.occurrences(), [])
        receipt = self.capture.context_coverage()["source_scan"]
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["raw_rows_scanned"], 0)
        self.assertEqual(receipt["shards"][0]["cursor_after"], "")
        self.assertTrue(self.capture.scan()["raw_eof"])

    def test_context_backfill_is_staged_bound_idempotent_and_preserves_live_cursor(self):
        self.config["quiet_archive_handoff_enabled"] = False
        old = self.make_capture()
        old.drain()
        with closing(sqlite3.connect(self.config["resource_capture_db"])) as conn, conn:
            before = conn.execute("SELECT * FROM resource_shards").fetchall()
        plan = old.backfill_contexts(0)
        self.assertEqual(plan["discovered_contexts"], 3)
        self.assertEqual(old.contexts(), [])
        applied = old.backfill_contexts(0, apply=True, run_id=plan["run_id"])
        self.assertEqual(applied["inserted_contexts"], 3)
        self.assertEqual(old.backfill_contexts(0, apply=True, run_id=plan["run_id"]), applied)
        self.assertFalse(old.context_coverage()["contexts"]["unmeasured_history_gap"])
        self.assertEqual(old.occurrences(), [])
        with closing(sqlite3.connect(self.config["resource_capture_db"])) as conn, conn:
            self.assertEqual(conn.execute("SELECT * FROM resource_shards").fetchall(), before)

    def test_context_stage_tamper_and_selection_change_fail_closed(self):
        plan = self.capture.backfill_contexts(0)
        with closing(sqlite3.connect(self.config["resource_capture_db"])) as conn, conn:
            conn.execute("UPDATE resource_backfill_staged_contexts SET payload=replace(payload, '经验', '改写')")
        applied = self.capture.backfill_contexts(0, apply=True, run_id=plan["run_id"])
        self.assertEqual(applied["state"], "candidate_mismatch")
        self.assertEqual(self.capture.contexts(), [])
        plan = self.capture.backfill_contexts(0)
        self.capture.config["resource_backup_selected_chats"] = []
        self.assertEqual(self.capture.backfill_contexts(0, apply=True, run_id=plan["run_id"])["state"], "selection_changed")

    def test_machine_export_is_complete_redacted_untruncated_and_projection_free(self):
        text = "前后连续经验\n" * 2000 + " https://example.test/guide?token=secret-value"
        self.source.messages_by_shard["fixture-shard"] = [message(1, text, [
            {"kind": "file", "resource_index": 0, "original_name": "guide.pdf"}
        ])]
        self.capture.scan()
        exporter = self.exporter()
        with (patch.object(exporter, "_render_obsidian_indexes_safely", side_effect=AssertionError("projection")),
              patch.object(exporter, "_render_target_indexes", side_effect=AssertionError("projection"))):
            result = exporter.run()
        self.assertEqual(result["state"], "written")
        path, manifest, contexts, coverage = self.read_snapshot(result)
        self.assertEqual(manifest["machine_handoff"]["schema"], HANDOFF_SCHEMA)
        self.assertEqual(manifest["machine_handoff"]["context_count"], len(contexts))
        self.assertEqual(contexts[0]["text"].count("前后连续经验"), 2000)
        self.assertNotIn("secret-value", contexts[0]["text"])
        self.assertEqual(len(contexts[0]["resource_occurrence_ids"]), 2)
        self.assertEqual(coverage["files"]["pending_occurrences"], 1)
        self.assertTrue(coverage["source_scan"]["raw_eof"])
        for file in path.iterdir():
            text = file.read_text()
            self.assertNotIn("private-internal-sender", text)
            self.assertNotIn("private-raw-id", text)
            self.assertNotIn("fixture@chatroom", text)
        self.assertFalse((self.root / "obsidian").exists())
        self.assertEqual(self.exporter().run()["state"], "unchanged")

    def test_context_only_or_coverage_change_publishes_new_snapshot_with_stable_event(self):
        self.capture.drain()
        first = self.exporter().run()
        _, _, first_contexts, _ = self.read_snapshot(first)
        self.source.messages_by_shard["fixture-shard"][0]["text"] = "更新正文，不改事件"
        plan = self.capture.backfill_contexts(0)
        self.capture.backfill_contexts(0, apply=True, run_id=plan["run_id"])
        second = self.exporter().run()
        self.assertNotEqual(first["snapshot"]["snapshot_id"], second["snapshot"]["snapshot_id"])
        _, _, second_contexts, _ = self.read_snapshot(second)
        self.assertEqual(first_contexts[0]["event_id"], second_contexts[0]["event_id"])
        self.capture.scan()
        third = self.exporter().run()
        self.assertNotEqual(second["snapshot"]["snapshot_id"], third["snapshot"]["snapshot_id"])

    def test_empty_selection_exports_explicit_empty_snapshot(self):
        self.capture.drain()
        self.capture.config["resource_backup_selected_chats"] = []
        result = self.exporter().run()
        _, manifest, contexts, coverage = self.read_snapshot(result)
        self.assertEqual(manifest["resource_count"], 0)
        self.assertEqual(contexts, [])
        self.assertEqual(coverage["selection"], [])

    def test_legacy_schema_migration_marks_unknown_history_without_reset(self):
        self.config["quiet_archive_handoff_enabled"] = False
        old = self.make_capture()
        old.drain()
        with closing(sqlite3.connect(self.config["resource_capture_db"])) as conn, conn:
            before = conn.execute("SELECT * FROM resource_shards").fetchall()
            conn.execute("ALTER TABLE resource_chats DROP COLUMN context_history_gap")
            conn.execute("PRAGMA user_version=3")
        migrated = self.make_capture()
        self.assertTrue(migrated.context_coverage()["contexts"]["unmeasured_history_gap"])
        with closing(sqlite3.connect(self.config["resource_capture_db"])) as conn, conn:
            self.assertEqual(conn.execute("SELECT * FROM resource_shards").fetchall(), before)

    def test_machine_destination_cannot_reuse_mounted_backup_identity(self):
        self.capture.drain()
        mounted = MountedResourceBackup(self.config, capture=self.capture,
                                        target=self.config["quiet_archive_handoff_target"])
        with self.capture.canonical_operation(), mounted._target_worker_lock():
            mounted._ensure_destination_identity_owned()
        result = self.exporter().run()
        self.assertEqual(result["state"], "target_failed")
        self.assertIn("destination_purpose_mismatch", result["error_codes"])

    def test_incomplete_inventory_and_changed_inventory_never_claim_raw_eof(self):
        source = InventoryAwareSource({"present": [message(1)]}, complete=False, digest="partial", missing=1)
        partial = self.make_capture(source)
        self.assertFalse(partial.scan()["raw_eof"])
        self.assertFalse(partial.context_coverage()["inventory"]["complete"])
        source.complete = True
        original = source.get_source_inventory
        calls = []
        def changing(**kwargs):
            calls.append(1)
            source.digest = "first" if len(calls) == 1 else "second"
            return original(**kwargs)
        with patch.object(source, "get_source_inventory", side_effect=changing):
            result = partial.scan()
        self.assertFalse(result["raw_eof"])
        self.assertEqual(result["state"], "source_degraded")

    def run_cli(self, arguments):
        output = io.StringIO()
        with (patch("scripts.quiet_archive_handoff.load_config", return_value=self.config),
              patch("core.resource_capture.load_config", return_value=self.config),
              patch("scripts.quiet_archive_handoff._source", return_value=self.source),
              redirect_stdout(output)):
            code = cli.main(arguments)
        return code, json.loads(output.getvalue())

    def test_refresh_fixed_json_and_exact_snapshot_for_pending_then_eof(self):
        flag = "--allow-transient-wechat-source-read"
        code, first = self.run_cli(["refresh", flag, "--max-rounds", "1"])
        self.assertEqual(code, 2)
        self.assertEqual(first["schema"], cli.REFRESH_SCHEMA)
        self.assertEqual(first["state"], "pending")
        self.assertEqual(first["snapshot_id"], first["handoff"]["snapshot"]["snapshot_id"])
        _, _, _, coverage = self.read_snapshot(first["handoff"])
        self.assertEqual(first["coverage"]["capture_run_id"], coverage["capture_run_id"])
        code, last = self.run_cli(["refresh", flag, "--max-rounds", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(last["state"], "eof")

    def test_cli_without_source_grant_never_loads_config_keys_or_source(self):
        with (patch("scripts.quiet_archive_handoff.load_config", side_effect=AssertionError("config")),
              patch("scripts.quiet_archive_handoff._source", side_effect=AssertionError("source")),
              redirect_stdout(io.StringIO())):
            self.assertEqual(cli.main(["refresh"]), 2)

    def test_cli_export_status_never_open_source(self):
        with (patch("scripts.quiet_archive_handoff.load_config", return_value=self.config),
              patch("core.resource_capture.load_config", return_value=self.config),
              patch("scripts.quiet_archive_handoff._source", side_effect=AssertionError("source")),
              redirect_stdout(io.StringIO())):
            self.assertEqual(cli.main(["export"]), 0)
            self.assertEqual(cli.main(["status"]), 0)


if __name__ == "__main__":
    unittest.main()
