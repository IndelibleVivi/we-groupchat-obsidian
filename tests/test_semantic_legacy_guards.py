"""Synthetic legacy-entrypoint retirement and semantic snapshot retry checks."""
from argparse import Namespace
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from core.config import ConfigStore
from core.semantic.contracts import SemanticStoreError
from core.semantic.engine import SemanticEngine, SemanticValidationError
from core.semantic.rebuild import ACTIVE_SCHEMA, JOURNAL_SCHEMA
from core.semantic import runtime
from core.semantic.store import SemanticStore
from scripts import (backfill_history, catch_up_monitor, configure_monitor,
                     daily_digest, organize_obsidian, review_queue)
from tests.test_semantic_engine import FakeDecision, FakeText, capture


class LegacyEntrypointGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.config_path = self.root / "config.json"
        self.semantic_root = self.root / "semantic"
        self.source_root = self.root / "protected-source"
        self.legacy_data_config = self.root / "legacy-data/config.json"
        self.legacy_config = self.root / "legacy-config.json"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target, value in (
            ("core.config.CONFIG_FILE", str(self.config_path)),
            ("core.semantic.runtime.DATA_DIR", str(self.root / "data")),
            ("core.semantic.runtime.LEGACY_DATA_CONFIG_FILE", str(self.legacy_data_config)),
            ("core.semantic.runtime.LEGACY_CONFIG_FILE", str(self.legacy_config)),
        ):
            self.stack.enter_context(patch(target, value))
        self.config = {"semantic_runtime_root": str(self.semantic_root), "db_dir": str(self.source_root),
                       "monitor_chat_display_name": "private-synthetic-chat"}
        ConfigStore().replace(self.config)

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def activate(self):
        database = self.semantic_root / "generations/synthetic-generation/knowledge.sqlite"
        database.parent.mkdir(parents=True, exist_ok=True)
        # A valid pin needs a regular file; the guard must never open its body.
        database.write_bytes(b"private-synthetic-database-not-for-inspection")
        self.manifest = {
            "schema": ACTIVE_SCHEMA, "generation_id": "synthetic-generation", "database": str(database),
            "embedding_profile_id": "synthetic-embedding", "index_generation_id": "synthetic-index",
            "projection_root": str(self.root / "private-vault"), "projection_receipt_id": "projection",
            "migration_receipt_ids": ["migration"], "config_revision": 1,
            "acceptance_receipt_id": "acceptance", "run_id": "private-synthetic-run",
        }
        self.write_json(self.semantic_root / "active.json", self.manifest)

    def cutover(self, phase):
        self.write_json(self.semantic_root / "cutover.json", {"schema": JOURNAL_SCHEMA, "phase": phase})

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.stat().st_mode, path.read_bytes())
                for path in self.root.rglob("*") if path.is_file()}

    def invoke(self, name):
        if name == "configure":
            return configure_monitor.configure(Namespace())
        if name == "backfill":
            return backfill_history.backfill(Namespace())
        if name == "catch-up":
            return catch_up_monitor.main(["--allow-transient-wechat-source-read"])
        if name.startswith("review-"):
            args = {"review-list": ["list"], "review-mark": ["mark", "synthetic-id", "reviewed"],
                    "review-audit": ["audit"], "review-cleanup": ["cleanup", "--apply"]}[name]
            return review_queue.main(args)
        module, args = ((daily_digest, []) if name == "digest" else
                        (organize_obsidian, ["--knowledge-audit"] if name == "organize-audit" else []))
        with patch("sys.argv", [module.__name__, *args]):
            return module.main()

    def assert_blocked_entries(self, expected_code):
        before = self.snapshot()
        with ExitStack() as stack:
            spies = []
            for module in (configure_monitor, backfill_history, daily_digest, organize_obsidian,
                           review_queue, catch_up_monitor):
                for name in ("load_config", "get_cached_keys", "load_key", "save_key", "update_config",
                             "KnowledgeStore", "WeChatDB", "ReviewQueue", "create_provider", "_load_runtime",
                             "write_daily_digest", "refresh_pending_daily_digests", "plan_source_metadata_regeneration"):
                    if hasattr(module, name):
                        spies.append(stack.enter_context(patch.object(module, name, side_effect=AssertionError(name))))
            for target in ("sqlite3.connect", "socket.create_connection", "core.config.auto_detect_db_dir",
                           "core.semantic.runtime.create_provider", "core.semantic.runtime.load_key",
                           "core.semantic.runtime.ProviderRoles.from_profile", "core.config.ConfigStore.read",
                           "core.config.ConfigStore.update", "core.config.ConfigStore.replace"):
                spies.append(stack.enter_context(patch(target, side_effect=AssertionError(target))))
            actual_stat, actual_lstat = os.stat, os.lstat

            def no_source(original):
                def check(path, *args, **kwargs):
                    if not isinstance(path, int) and str(path).startswith(str(self.source_root)):
                        raise AssertionError("protected source metadata")
                    return original(path, *args, **kwargs)
                return check

            stack.enter_context(patch("os.stat", side_effect=no_source(actual_stat)))
            stack.enter_context(patch("os.lstat", side_effect=no_source(actual_lstat)))
            for entry in ("configure", "backfill", "digest", "organize", "organize-audit", "review-list",
                          "review-mark", "review-audit", "review-cleanup", "catch-up"):
                with self.subTest(entry=entry), redirect_stderr(StringIO()) as stderr, redirect_stdout(StringIO()) as stdout:
                    self.assertEqual(self.invoke(entry), 2)
                self.assertEqual(stdout.getvalue(), "")
                result = json.loads(stderr.getvalue())
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["code"], expected_code)
                self.assertIn("scripts/rebuild_knowledge.py status <run_id>", result["next"])
                for private in (str(self.root), "private-synthetic-chat", "private-synthetic-run", "synthetic-generation"):
                    self.assertNotIn(private, stderr.getvalue())
            for spy in spies:
                spy.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_active_generation_blocks_every_legacy_entry_before_side_effects(self):
        self.activate()
        self.assert_blocked_entries("semantic_legacy_command_blocked")

    def test_half_cutover_blocks_every_legacy_entry_before_side_effects(self):
        self.cutover("checkpoints")
        self.assert_blocked_entries("semantic_legacy_cutover_blocked")

    def test_invalid_existing_config_fails_closed_without_legacy_fallback(self):
        self.config_path.write_text("{broken", encoding="utf-8")
        self.assert_blocked_entries("semantic_legacy_config_unavailable")

    def test_invalid_or_missing_active_binding_never_reopens_legacy_commands(self):
        self.activate()
        active = self.semantic_root / "active.json"
        for value in ("{broken", "[]", '{"schema":"unknown"}'):
            with self.subTest(value=value), redirect_stderr(StringIO()) as output:
                active.write_text(value, encoding="utf-8")
                self.assertTrue(runtime.legacy_command_blocked())
            self.assertEqual(json.loads(output.getvalue())["code"], "semantic_legacy_cutover_blocked")
        self.write_json(active, self.manifest)
        Path(self.manifest["database"]).unlink()
        self.assert_blocked_entries("semantic_legacy_cutover_blocked")

    def test_symlink_metadata_is_rejected_without_following_it(self):
        self.semantic_root.mkdir()
        (self.semantic_root / "active.json").symlink_to(self.source_root / "protected-metadata.json")
        self.assert_blocked_entries("semantic_legacy_cutover_blocked")

    def test_metadata_root_inside_protected_source_is_rejected_before_stat(self):
        self.config["semantic_runtime_root"] = str(self.source_root / "semantic")
        ConfigStore().replace(self.config)
        self.assert_blocked_entries("semantic_legacy_cutover_blocked")

    def test_active_pin_is_not_bypassed_by_rolled_back_journal(self):
        self.activate()
        self.cutover("rolled_back")
        with redirect_stderr(StringIO()):
            self.assertTrue(runtime.legacy_command_blocked())

    def test_missing_primary_inspects_legacy_config_without_migrating_it(self):
        self.activate()
        self.config_path.unlink()
        self.write_json(self.legacy_data_config, self.config)
        before = self.snapshot()
        with redirect_stderr(StringIO()) as output:
            self.assertTrue(runtime.legacy_command_blocked())
        self.assertEqual(json.loads(output.getvalue())["code"], "semantic_legacy_command_blocked")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.config_path.exists())

    def test_missing_generation_and_candidate_do_not_create_or_open_storage(self):
        self.semantic_root.mkdir()
        before = self.snapshot()
        self.assertFalse(runtime.legacy_command_blocked())
        self.assertEqual(self.snapshot(), before)

        candidate = self.semantic_root / "generations/candidate/knowledge.sqlite"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"unactivated candidate")
        before = self.snapshot()
        with patch("sqlite3.connect", side_effect=AssertionError("candidate database opened")):
            self.assertFalse(runtime.legacy_command_blocked())
        self.assertEqual(self.snapshot(), before)

    def test_unavailable_configured_root_blocks_every_legacy_writer(self):
        self.assert_blocked_entries("semantic_legacy_cutover_blocked")

    def test_unconfigured_default_root_preserves_unmigrated_installation(self):
        self.config.pop("semantic_runtime_root")
        ConfigStore().replace(self.config)
        before = self.snapshot()
        self.assertFalse(runtime.legacy_command_blocked())
        self.assertEqual(self.snapshot(), before)

    def test_missing_grant_still_precedes_semantic_inspection(self):
        with patch.object(catch_up_monitor, "legacy_command_blocked", side_effect=AssertionError("guard")), \
             patch.object(catch_up_monitor, "_load_runtime", side_effect=AssertionError("source")), \
             redirect_stderr(StringIO()) as output:
            self.assertEqual(catch_up_monitor.main([]), 2)
        self.assertIn("--allow-transient-wechat-source-read", output.getvalue())

    def test_unmigrated_and_explicit_rollback_preserve_legacy_dispatch(self):
        self.semantic_root.mkdir()
        for phase in (None, "rolled_back"):
            with self.subTest(phase=phase), redirect_stdout(StringIO()):
                if phase:
                    self.cutover(phase)
                for module, invoke in ((configure_monitor, "configure"), (backfill_history, "backfill")):
                    with patch.object(module, "load_config", return_value={}) as load, \
                         patch.object(module, "get_cached_keys", return_value={}) as keys:
                        self.assertEqual(self.invoke(invoke), 1)
                        load.assert_called_once_with()
                        keys.assert_called_once_with()
                with patch.object(daily_digest, "load_config", return_value={}), \
                     patch.object(daily_digest, "write_daily_digest", return_value={"path": "synthetic", "new_notes_count": 0}) as write, \
                     patch.object(daily_digest, "refresh_pending_daily_digests", return_value={"state": "idle"}) as repair:
                    self.assertEqual(self.invoke("digest"), 0)
                    write.assert_called_once_with({}, target_date=None)
                    repair.assert_called_once_with({})
                with patch.object(organize_obsidian, "load_config", return_value={}), \
                     patch.object(organize_obsidian, "plan_source_metadata_regeneration", return_value={}) as plan, \
                     patch.object(organize_obsidian, "format_source_metadata_plan", return_value=[]), \
                     patch("sys.argv", ["organize_obsidian", "--source-metadata-dry-run"]):
                    self.assertEqual(organize_obsidian.main(), 0)
                    plan.assert_called_once_with({})
                queue = Mock()
                queue.list.return_value = []
                with patch.object(review_queue, "ReviewQueue", return_value=queue) as constructor:
                    self.assertEqual(self.invoke("review-list"), 0)
                    constructor.assert_called_once_with(review_queue.QUEUE_DIR)
                    queue.list.assert_called_once_with(review_queue.ACTIVE_STATUS)
                with patch.object(catch_up_monitor, "_load_runtime", return_value=({}, [], "synthetic-source")) as load, \
                     patch.object(catch_up_monitor, "audit_pending", return_value=[]) as audit, \
                     patch.object(catch_up_monitor, "_print_audit"):
                    self.assertEqual(self.invoke("catch-up"), 0)
                    load.assert_called_once_with()
                    audit.assert_called_once_with("synthetic-source", [], limit=100000)


class SemanticProcessRetryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SemanticStore(Path(temp.name).resolve() / "knowledge.sqlite", "synthetic-generation")
        capture(self.store, "batch", ["今天散步买了一束花。"])
        self.text = FakeText()
        self.engine = SemanticEngine(self.store, self.text, FakeDecision())
        # process needs only the canonical store/engine, not runtime providers,
        # projections, credentials, a source reader or an active deployment.
        self.runtime = object.__new__(runtime.SemanticRuntime)
        self.runtime.store, self.runtime.engine = self.store, self.engine

    def test_changed_snapshot_retries_new_context_and_reuses_accepted_extraction(self):
        original = self.engine._identity_catalog
        catalogs = []

        def concurrent_view():
            result = original()
            catalogs.append(result)
            if len(catalogs) == 1:
                self.store.create_view("concurrent", "Synthetic view", "A concurrent definition")
            return result

        with patch.object(self.engine, "_identity_catalog", side_effect=concurrent_view), \
             patch.object(self.engine, "process_batch", wraps=self.engine.process_batch) as process:
            receipt = self.runtime.process("batch")
        self.assertEqual(receipt["batch_id"], "batch")
        self.assertEqual(process.call_args_list, [call("batch", plan_revision=1), call("batch", plan_revision=2)])
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM extraction_receipts").fetchone()[0], 1)
            kinds = dict(conn.execute("SELECT kind,COUNT(*) FROM jobs GROUP BY kind"))
            self.assertEqual(kinds["extract"], 1)
            self.assertEqual(kinds["extract_page"], 1)
            self.assertEqual(kinds["proposal_context"], 2)
            failures = conn.execute("SELECT error_code FROM job_attempts WHERE error_code IS NOT NULL").fetchall()
            self.assertEqual([row[0] for row in failures], ["semantic_snapshot_changed"])
        calls = list(self.text.calls)
        self.assertEqual(len(self.store.list_items()), 1)
        self.assertEqual(self.runtime.process("batch"), receipt)
        self.assertEqual(self.text.calls, calls)

    def test_snapshot_retry_is_bounded_to_three_attempts(self):
        error = SemanticValidationError("semantic_snapshot_changed")
        with patch.object(self.engine, "process_batch", side_effect=error) as process, \
             patch.object(self.store, "complete_batch", side_effect=AssertionError("premature ACK")):
            with self.assertRaises(SemanticValidationError) as caught:
                self.runtime.process("batch")
        self.assertIs(caught.exception, error)
        self.assertEqual(process.call_args_list, [call("batch", plan_revision=n) for n in (1, 2, 3)])

    def test_stale_commit_and_snapshot_change_share_one_retry_budget(self):
        effects = [SemanticStoreError("semantic_stale_read_set"),
                   SemanticValidationError("semantic_snapshot_changed"), {"status": "complete"}]
        with patch.object(self.engine, "process_batch", side_effect=effects) as process, \
             patch.object(self.store, "complete_batch", return_value={"synthetic": "receipt"}) as complete:
            self.assertEqual(self.runtime.process("batch"), {"synthetic": "receipt"})
        self.assertEqual(process.call_count, 3)
        complete.assert_called_once_with("batch")

    def test_unrelated_and_wrong_type_codes_are_not_retried(self):
        for error in (SemanticValidationError("semantic_job_busy_or_unknown"),
                      SemanticValidationError("semantic_stale_read_set"),
                      SemanticStoreError("semantic_snapshot_changed"),
                      SemanticStoreError("semantic_batch_incomplete"), RuntimeError("semantic_snapshot_changed")):
            with self.subTest(error=repr(error)), patch.object(self.engine, "process_batch", side_effect=error) as process:
                with self.assertRaises(type(error)) as caught:
                    self.runtime.process("batch")
                self.assertIs(caught.exception, error)
                process.assert_called_once_with("batch", plan_revision=1)


if __name__ == "__main__":
    unittest.main()
