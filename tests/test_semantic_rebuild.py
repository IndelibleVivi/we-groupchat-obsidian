import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import contextmanager, closing
from unittest.mock import patch

from core.semantic.rebuild import (ACTIVE_SCHEMA, SCHEMA, GenerationRegistry, RebuildError,
    RebuildJournal, backup_files, backup_sqlite, fingerprint, read_json, read_regular, safe_path, write_json)
from core.semantic.rebuild import ManagedRuntime, RebuildCoordinator


PROFILE = {"schema": "we-groupchat-obsidian.semantic-providers.v1", "decision": "text",
           "embedding": "cloudflare", "vector_index": "local", "account_id": "0" * 32,
           "vectorize_index": "", "secret_name": "synthetic-key"}


def journal_details(root, run_id):
    journal = RebuildJournal(root, run_id)
    return journal.payloads.expand(journal.read())


class SyntheticManagedRuntime:
    def __init__(self):
        self.events = []
        self.pid = 100
        self.on_restore = lambda: None
        self.restore_ok = True

    def inspect(self):
        return {"label": "synthetic.agent", "plist_path": "synthetic.plist", "plist_sha256": "a" * 64,
                "was_loaded": True, "was_running": True, "program_arguments": ["/synthetic/app"],
                "working_directory": "/synthetic"}

    def observe_process(self, _):
        return {"pid": self.pid, "started_at": str(self.pid), "executable": "/synthetic/app"}

    def stop_and_lock(self, _):
        self.events.append("stopped")

    def release(self):
        self.events.append("app_released")

    def restore(self, _):
        self.events.append("restored")
        if not self.restore_ok:
            raise RebuildError("runtime_restore_failed")
        self.pid += 1
        self.on_restore()
        return True


class RebuildCoordinatorTests(unittest.TestCase):
    def setUp(self):
        from core.config import ConfigStore
        from core.semantic.runtime import ProviderRoles
        from ai.vector_index import LocalCosineIndex
        from tests.test_semantic_legacy import LegacyFixture
        from tests.test_semantic_engine import FakeText, FakeDecision
        from tests.test_semantic_retrieval import FakeEmbedding
        from tests.test_semantic_source import Source
        from tests.test_monitor_source import raw_message
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.legacy = LegacyFixture(self.root)
        self.runtime_root = self.root / "runtime"
        # ConfigStore intentionally drops test-only resource_capture_db keys;
        # bind the actual path owner so no fixture can inspect the app ledger.
        resource_path = patch("core.resource_capture.resource_capture_db_path", return_value=str(self.root / "resources.sqlite"))
        resource_path.start()
        self.addCleanup(resource_path.stop)
        for module in ("core.config", "core.resource_capture"):
            fallback = patch(module + ".DATA_DIR", str(self.root))
            fallback.start()
            self.addCleanup(fallback.stop)
        from urllib.parse import unquote, urlparse
        import core.semantic.rebuild as rebuild_module
        import core.semantic.legacy as legacy_module
        # Load direct-import aliases before installing per-fixture guards; no
        # module may retain a closure pointing at a previous temporary root.
        import core.semantic.verification as verification_module
        def admitted(path):
            value = str(path)
            if value == ":memory:":
                return
            if value.startswith("file:"):
                value = unquote(urlparse(value).path)
            if not Path(value).resolve().is_relative_to(self.root):
                raise AssertionError("synthetic_fixture_filesystem_escape")
        for module, name in ((rebuild_module, "read_regular"), (rebuild_module, "regular_exists"),
                             (rebuild_module, "file_fingerprint"),
                             (legacy_module, "_read_regular"), (verification_module, "_read_regular"),
                             (sqlite3, "connect")):
            original = getattr(module, name)
            def guarded(path, *args, original=original, **kwargs):
                admitted(path)
                return original(path, *args, **kwargs)
            boundary = patch.object(module, name, guarded)
            boundary.start()
            self.addCleanup(boundary.stop)
        self.state_path = self.root / "checkpoint.json"
        self.config_store = ConfigStore(self.root / "config.json")
        write_json(self.config_store.path, {"config_revision": 1,
            "monitor_knowledge_db": str(self.legacy.db), "monitor_obsidian_root": str(self.legacy.vault),
            "monitor_obsidian_subdir": "Old", "monitor_chats": [{"username": "chat", "display_name": "synthetic"}],
            "semantic_runtime_root": str(self.runtime_root), "resource_capture_db": str(self.root / "resources.sqlite"),
            "attachment_archive_root": str(self.root / "attachments")})
        self.source = Source({"shard": ("g", [raw_message("g", 1, 10, "嗯")])})
        self.text = FakeText(noise=True)
        self.roles = ProviderRoles(self.text, FakeDecision(), FakeEmbedding(), LocalCosineIndex())
        self.managed = SyntheticManagedRuntime()
        self.source_calls = 0
        @contextmanager
        def locks(_config, _states):
            self.managed.events.append("resource_locked")
            try:
                yield
            finally:
                self.managed.events.append("resource_released")
        def source_factory(_config):
            self.source_calls += 1
            self.assertIn("resource_locked", self.managed.events)
            return self.source
        self.coordinator = RebuildCoordinator(self.runtime_root, self.config_store,
            source_factory=source_factory, roles_factory=lambda _c, _p: self.roles,
            managed_runtime=self.managed, state_paths={"chat": self.state_path}, lock_context=locks,
            usage_path=self.root / "provider-usage.sqlite", review_queue_dir=self.root / "review_queue")

    def execute(self):
        return self.coordinator.execute("test-run", allow_source_read=True, allow_model_processing=True)

    def live_increment(self):
        from core.semantic.store import SemanticStore
        from core.semantic.runtime import SemanticRuntime
        from core.monitor import TopicMonitor
        from core.monitor_state import MonitorStateStore
        from tests.test_monitor_source import raw_message
        pin = GenerationRegistry(self.runtime_root).pin()
        if pin is None:
            return
        store = SemanticStore(pin.database, pin.generation_id)
        runtime = SemanticRuntime(store, self.roles, pin.projection_root, config=self.config_store.inspect())
        self.source.shards["shard"]["messages"].append(raw_message("g", 2, 20, "新消息"))
        monitor = TopicMonitor(self.source, {"monitor_chat_username": "chat"},
                               state_store=MonitorStateStore(self.state_path), semantic_runtime=runtime)
        self.assertEqual(monitor._check_once()["status"], "semantic_applied")

    def test_plan_is_source_free_and_grants_precede_config(self):
        self.config_store.update(lambda value: value.update(semantic_provider_limits={
            "text": {"max_inflight": 2, "requests_per_minute": 30}}))
        with patch.object(self.coordinator, "source_factory", side_effect=AssertionError("source")), \
             patch.object(self.coordinator, "_roles", side_effect=AssertionError("keys")):
            plan = self.coordinator.plan(PROFILE, run_id="test-run")
            self.assertEqual(plan["chat_ids"], ["chat"])
            self.assertFalse(plan["provider_budget"]["ready"])
            self.assertEqual(plan["configured_admission"]["text"], {
                "max_inflight": 2, "requests_per_minute": 30, "tokens_per_minute": None,
                "unset": ["tokens_per_minute"]})
            self.assertEqual(plan["configured_admission"]["embedding"]["unset"],
                             ["requests_per_minute", "tokens_per_minute"])
            self.config_store.update(lambda value: value.update(semantic_provider_limits={
                "text": {"max_inflight": 1, "requests_per_minute": 10}}))
            self.assertEqual(self.coordinator.status("test-run")["configured_admission"],
                             plan["configured_admission"])
        with patch.object(self.config_store, "inspect", side_effect=AssertionError("config before grant")):
            with self.assertRaisesRegex(RebuildError, "explicit_grants"):
                self.coordinator.execute("test-run", allow_source_read=True)
        self.assertEqual(self.managed.events, [])

    def test_full_rebuild_real_store_history_migration_and_live_completion(self):
        self.coordinator.plan(PROFILE, run_id="test-run")
        self.managed.on_restore = self.live_increment
        result = self.execute()
        self.assertEqual(result["state"], "run_complete", result)
        run = journal_details(self.runtime_root, "test-run")
        self.assertEqual(len(run["verification"]["capture_passes"]), 2)
        self.assertEqual((self.legacy.tree / "life.md").read_bytes(), self.legacy.page_bytes)
        self.assertEqual(run["migrations"].keys(), {"chat"})
        self.assertTrue(Path(run["freeze_receipts"][0]["databases"]["knowledge"]["path"]).is_file())
        self.assertLess(self.managed.events.index("resource_released"), self.managed.events.index("app_released"))
        self.assertLess(self.managed.events.index("app_released"), self.managed.events.index("restored"))
        with closing(sqlite3.connect(run["plan"]["database"])) as conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0])
        self.assertEqual(metadata["activation"]["delivery_floor_change_id"], 0)

    def use_semantic_text(self):
        from tests.test_semantic_engine import FakeText
        from core.semantic.runtime import ProviderRoles
        from ai.semantic_provider import TextResult
        class BodyText(FakeText):
            def generate(self, prompt, **kwargs):
                if prompt.startswith("{") and json.loads(prompt).get("phase") == "brief_write_v1":
                    changes = json.loads(prompt)["changes"]
                    return TextResult(json.dumps({"status": "success", "title": "今日生活",
                        "paragraphs": [{"text": c["after"], "change_ids": [c["id"]],
                            "evidence_refs": [{"item_id": c["item_id"], "evidence_id": e} for e in c["evidence_ids"]],
                            "scopes": [], "corrections": []} for c in changes]}), self.route)
                if prompt.startswith("{") and json.loads(prompt).get("task", "").startswith("Write an evolving"):
                    snapshot = json.loads(prompt)["snapshot"]
                    blocks = [{"id": "unit-" + unit["id"], "role": "narrative", "text": unit["text"],
                               "unit_ids": [unit["id"]], "evidence_ids": unit["evidence_ids"],
                               "resource_ids": unit["resource_ids"], "primary_home": snapshot["item_id"]}
                              for unit in snapshot["units"]]
                    return TextResult(json.dumps({"item_id": snapshot["item_id"],
                        "content_revision": snapshot["content_revision"], "blocks": blocks}), self.route)
                return super().generate(prompt, **kwargs)
        self.text = BodyText()
        self.roles = ProviderRoles(self.text, self.roles.decision, self.roles.embedding, self.roles.index)

    def test_accepted_proposal_before_commit_is_reconciled_after_fresh_freeze(self):
        from core.semantic.store import SemanticStore
        self.use_semantic_text()
        self.coordinator.plan(PROFILE, run_id="test-run")
        with patch.object(SemanticStore, "apply_proposal", side_effect=RebuildError("synthetic_crash_before_commit")):
            first = self.execute()
        self.assertTrue(first["restored"], first)
        self.assertNotEqual(first["state"], "run_complete")
        self.managed.on_restore = self.live_increment
        second = self.execute()
        self.assertEqual(second["state"], "run_complete", second)

    def test_rollback_intent_overrides_cached_completion_until_exact_rollback_resume(self):
        self.check_rollback_crash("after_rollback_cas")

    def test_external_rollback_intent_precedes_cached_local_completion(self):
        self.check_rollback_crash("after_rollback_intent")

    def check_rollback_crash(self, fault_phase):
        from core.monitor_state import MonitorStateStore
        self.coordinator.plan(PROFILE, run_id="test-run")
        self.managed.on_restore = self.live_increment
        self.assertEqual(self.execute()["state"], "run_complete")
        def crash(phase):
            if phase == fault_phase:
                raise RebuildError("synthetic_rollback_crash")
        self.coordinator.fault_hook = crash
        with self.assertRaisesRegex(RebuildError, "synthetic_rollback_crash"):
            self.coordinator.rollback("test-run", apply=True, allow_runtime_changes=True)
        self.coordinator.fault_hook = lambda _phase: None
        if fault_phase == "after_rollback_intent":
            self.assertEqual(journal_details(self.runtime_root, "test-run")["phase"], "run_complete")
        self.assertNotEqual(self.coordinator.status("test-run")["state"], "run_complete")
        with self.assertRaisesRegex(RebuildError, "rebuild_rollback_recovery_required"):
            self.execute()
        restored = self.managed.events.count("restored")
        result = self.coordinator.rollback("test-run", apply=True, allow_runtime_changes=True)
        self.assertEqual(result["state"], "rolled_back", result)
        self.assertEqual(self.managed.events.count("restored"), restored + 1)
        self.assertNotEqual(MonitorStateStore(self.state_path).inspect().schema,
                            "we-groupchat-obsidian.monitor-state.v3")
        with self.assertRaisesRegex(RebuildError, "rebuild_run_rolled_back"):
            self.execute()

    def test_semantic_items_require_actual_dense_index_and_current_readable_projections(self):
        self.use_semantic_text()
        self.coordinator.plan(PROFILE, run_id="test-run")
        result = self.execute()
        run = journal_details(self.runtime_root, "test-run")
        self.assertIn("live_check", result, (result, run.get("acceptance")))
        self.assertTrue(run["acceptance"]["complete"], run["acceptance"])
        self.assertGreater(run["acceptance"]["counts"]["items"], 0)
        self.assertTrue(self.coordinator.verify("test-run")["complete"])
        with closing(sqlite3.connect(run["plan"]["database"])) as conn:
            edition = conn.execute("SELECT edition_id,request_id FROM reader_brief_editions WHERE status='success'").fetchone()
            self.assertIsNotNone(edition)
            artifact = conn.execute("SELECT plan_json FROM reader_brief_attempts WHERE request_id=?", (edition[1],)).fetchone()[0]
            brief_path = conn.execute("SELECT path FROM reader_projections WHERE object_id=?", (edition[0],)).fetchone()[0]
            conn.execute("UPDATE reader_brief_attempts SET plan_json='' WHERE request_id=?", (edition[1],))
            conn.commit()
        lost_artifact = self.coordinator.verify("test-run")
        self.assertIn("brief_accepted_plan_missing", {i["code"] for i in lost_artifact["issues"]})
        with closing(sqlite3.connect(run["plan"]["database"])) as conn:
            conn.execute("UPDATE reader_brief_attempts SET plan_json=? WHERE request_id=?", (artifact, edition[1]))
            provider_artifact = conn.execute("SELECT * FROM reader_text_artifacts WHERE request_id=?", (edition[1],)).fetchone()
            self.assertIsNotNone(provider_artifact)
            conn.execute("DELETE FROM reader_text_artifacts WHERE request_id=?", (edition[1],))
            conn.commit()
        missing_provider = self.coordinator.verify("test-run")
        self.assertIn("brief_provider_artifact_missing", {i["code"] for i in missing_provider["issues"]})
        with closing(sqlite3.connect(run["plan"]["database"])) as conn:
            conn.execute("INSERT INTO reader_text_artifacts VALUES(?,?,?,?,?,?,?)", provider_artifact)
            conn.commit()
        brief_file = Path(run["plan"]["projection_root"], brief_path)
        brief_bytes = brief_file.read_bytes()
        brief_file.unlink()
        missing_brief = self.coordinator.verify("test-run")
        self.assertIn("brief_current_projection_missing", {i["code"] for i in missing_brief["issues"]})
        brief_file.write_bytes(brief_bytes)
        with closing(sqlite3.connect(run["plan"]["database"])) as conn:
            item = conn.execute("SELECT path FROM reader_projections WHERE object_id IN (SELECT item_id FROM items) LIMIT 1").fetchone()
        Path(run["plan"]["projection_root"], item[0]).unlink()
        broken = self.coordinator.verify("test-run")
        self.assertFalse(broken["complete"])
        self.assertTrue(any(issue["category"] == "projection_pending" for issue in broken["issues"]), broken)

    def test_required_brief_cannot_disappear_from_manifest_or_canonical_edition(self):
        from core.semantic.verification import RebuildVerifier
        self.coordinator.plan(PROFILE, run_id="test-run")
        self.execute()
        run = journal_details(self.runtime_root, "test-run")
        omitted = dict(run["verification"])
        omitted.pop("required_brief")
        report = RebuildVerifier(run["plan"]["database"], omitted,
                                projection_root=run["plan"]["projection_root"]).verify().to_dict()
        self.assertIn("required_brief_manifest_missing", {i["code"] for i in report["issues"]})
        with closing(sqlite3.connect(run["plan"]["database"])) as conn:
            self.assertEqual(conn.execute("SELECT status FROM reader_brief_editions").fetchone()[0], "silent")
            conn.execute("DELETE FROM reader_brief_editions")
            conn.commit()
        report = self.coordinator.verify("test-run")
        self.assertIn("required_brief_missing", {i["code"] for i in report["issues"]})

    def test_no_cloudflare_role_needs_no_cloudflare_budget_or_ledger_read(self):
        from core.semantic.rebuild import _budget_snapshot
        local = {"embedding": "local", "vector_index": "local", "decision": "text"}
        with patch("core.semantic.rebuild.regular_exists", side_effect=AssertionError("unrelated CF ledger")):
            self.assertEqual(_budget_snapshot(local, self.root / "unused-budget.sqlite", self.coordinator.clock()),
                             {"ready": True, "accounts": []})

    def test_budget_snapshot_uses_canonical_account_case_and_only_selected_services(self):
        from datetime import datetime, timezone
        from ai.cloudflare_provider import CloudflareClient
        from core.provider_usage import ProviderUsageStore
        from core.semantic.rebuild import _budget_snapshot
        path = self.root / "provider-usage.sqlite"
        now = self.coordinator.clock()
        usage = ProviderUsageStore(str(path), clock=lambda: now)
        client = CloudflareClient("AB" * 16, "synthetic-unused-key", usage)
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        midnight = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86400
        usage.confirm_budget(client.vectorize_account_key, utc_day=day, daily_limit_units=50,
                             external_usage_units=7, confirmation_id="synthetic-budget", valid_until=min(now + 60, midnight))
        profile = {**PROFILE, "account_id": "AB" * 16, "embedding": "local", "vector_index": "cloudflare"}
        snapshot = _budget_snapshot(profile, path, now)
        self.assertTrue(snapshot["ready"], snapshot)
        self.assertEqual(len(snapshot["accounts"]), 1)
        self.assertEqual(snapshot["accounts"][0]["remaining_units"], 43)
        profile["embedding"] = "cloudflare"
        self.assertFalse(_budget_snapshot(profile, path, now)["ready"])

    def test_new_runtime_evidence_required_and_finalization_resume_is_read_only(self):
        self.coordinator.plan(PROFILE, run_id="test-run")
        result = self.execute()
        self.assertEqual(result["state"], "incomplete", result)
        self.assertEqual(result["live_check"]["code"], "rebuild_live_increment_pending")
        self.live_increment()
        with patch.object(self.coordinator, "source_factory", side_effect=AssertionError("source")), \
             patch.object(self.coordinator, "_roles", side_effect=AssertionError("providers")):
            result = self.execute()
        self.assertEqual(result["state"], "run_complete", result)

    def test_budget_failure_does_not_stop_runtime(self):
        self.coordinator.plan(PROFILE, run_id="test-run")
        with patch.object(type(self.roles), "preflight", return_value={"ready": False}):
            with self.assertRaisesRegex(RebuildError, "budget_unready"):
                self.execute()
        self.assertEqual(self.managed.events, [])
        self.assertEqual(self.source_calls, 0)

    def test_second_full_pass_captures_old_time_late_row_and_changed_content(self):
        from tests.test_monitor_source import raw_message
        self.coordinator.plan(PROFILE, run_id="test-run")
        def late(phase):
            if phase == "after_freeze-1-processed-C0":
                self.source.shards["shard"]["messages"][0]["text"] = "B"
                self.source.shards["shard"]["messages"].append(raw_message("g", 3, 5, "late old timestamp"))
                self.source.shards["shard"]["messages"].sort(key=lambda r: (r["timestamp"], r["source_envelope"]["rowid"]))
        self.coordinator.fault_hook = late
        result = self.execute()
        self.assertEqual(result["live_check"]["code"], "rebuild_live_increment_pending", result)
        run = journal_details(self.runtime_root, "test-run")
        with closing(sqlite3.connect(run["plan"]["database"])) as conn:
            rows = [r[0] for r in conn.execute("SELECT text FROM source_records WHERE source_namespace='source-fixture' ORDER BY capture_seq")]
        self.assertEqual(rows, ["嗯", "late old timestamp", "B"])
        self.assertTrue(run["acceptance"]["complete"])

    def test_resume_refreezes_intervening_legacy_note_without_replaying_historical_revisions(self):
        self.coordinator.plan(PROFILE, run_id="test-run")
        def fault(phase):
            if phase == "after_freeze-1-processed-C0":
                raise RebuildError("synthetic_before_cutover")
        self.coordinator.fault_hook = fault
        first = self.execute()
        self.assertTrue(first["restored"], first)
        before = journal_details(self.runtime_root, "test-run")
        with closing(sqlite3.connect(before["plan"]["database"])) as conn:
            old_count = conn.execute("SELECT COUNT(*) FROM source_records WHERE source_namespace LIKE 'legacy:%' AND provenance_grade='raw_exact'").fetchone()[0]
        self.assertEqual(old_count, 3)
        (self.legacy.tree / "life.md").write_bytes(self.legacy.page_bytes + b"\nnew personal paragraph\n")
        self.coordinator.fault_hook = lambda _: None
        second = self.execute()
        self.assertIn("live_check", second, second)
        run = journal_details(self.runtime_root, "test-run")
        self.assertEqual(len(run["freeze_receipts"]), 2)
        self.assertTrue(run["acceptance"]["complete"], run["acceptance"])
        with closing(sqlite3.connect(run["plan"]["database"])) as conn:
            count = conn.execute("SELECT COUNT(*) FROM source_records WHERE source_namespace LIKE 'legacy:%' AND provenance_grade='raw_exact'").fetchone()[0]
            old = conn.execute("SELECT metadata_json FROM legacy_mappings WHERE kind='annotation_history'").fetchone()
        self.assertEqual(count, old_count)
        self.assertEqual(Path(json.loads(old[0])["backup_path"]).read_bytes(), self.legacy.page_bytes)

    def test_restore_failure_and_provisional_write_failure_stay_incomplete(self):
        self.coordinator.plan(PROFILE, run_id="test-run")
        self.managed.restore_ok = False
        result = self.execute()
        self.assertEqual(result["state"], "incomplete", result)
        self.assertFalse(result["restored"])
        self.assertEqual(GenerationRegistry(self.runtime_root).pin().generation_id,
                         journal_details(self.runtime_root, "test-run")["plan"]["generation_id"])

    def test_interrupted_capture_prefix_is_retained_then_new_full_pass_proves_h1(self):
        from core.semantic.source import SemanticCapture
        from tests.test_monitor_source import raw_message
        self.source.shards["shard"]["messages"].append(raw_message("g", 2, 20, "second"))
        self.coordinator.plan(PROFILE, run_id="test-run")
        def interrupted(capture, chat_ids, *, snapshot_id):
            capture.page_size = 1
            capture.page(chat_ids[0], {"shard": {"generation_id": "g", "cursor_token": "[0,0]", "start_cursor": "[0,0]"}},
                         snapshot_id=snapshot_id, purpose="snapshot:" + snapshot_id)
            raise RebuildError("synthetic_capture_interrupted")
        with patch.object(SemanticCapture, "snapshot", interrupted):
            failed = self.execute()
        self.assertTrue(failed["restored"], failed)
        self.source.shards["shard"]["messages"][0]["text"] = "changed after interruption"
        result = self.execute()
        self.assertIn("live_check", result, result)
        run = journal_details(self.runtime_root, "test-run")
        abandoned = run["verification"]["abandoned_capture_passes"]
        self.assertEqual(len(abandoned), 1)
        self.assertFalse(abandoned[0]["complete"])
        self.assertFalse(abandoned[0]["batches"][0]["source_eof"])
        self.assertTrue(self.coordinator.verify("test-run")["complete"])
        changed = dict(run["verification"], abandoned_capture_passes=[])
        from core.semantic.verification import RebuildVerifier
        invalid = RebuildVerifier(run["plan"]["database"], changed, projection_root=run["plan"]["projection_root"]).verify().to_dict()
        self.assertTrue(any(issue["code"] == "capture_pass_undeclared_reset" for issue in invalid["issues"]), invalid)

    def test_pending_intent_requires_original_policy_and_exact_content_before_provider(self):
        from core.monitor_state import MonitorStateStore
        from core.monitor_source import read_monitor_source_batch, freeze_monitor_source_batch
        from core.monitor import TopicMonitor
        state = MonitorStateStore(self.state_path)
        config = dict(self.config_store.inspect(), monitor_chat_username="chat")
        monitor = TopicMonitor(self.source, config, state_store=state)
        old = state.inspect()
        batch = read_monitor_source_batch(self.source, "chat", old.data, raw_limit=1)
        pending = freeze_monitor_source_batch(batch, old.data, monitor._batch_policy_fingerprint())
        state.commit(old.revision, dict(old.data, pending_source_batch=pending))
        self.coordinator.plan(PROFILE, run_id="test-run")
        self.source.shards["shard"]["messages"][0]["text"] = "changed pending bytes"
        result = self.execute()
        self.assertEqual(result["state"], "incomplete")
        self.assertEqual(len(self.text.calls), 0)
        self.assertEqual(state.inspect().data["pending_source_batch"], pending)
        self.source.shards["shard"]["messages"][0]["text"] = "嗯"
        result = self.execute()
        self.assertIn("live_check", result, result)
        self.assertNotIn("pending_source_batch", state.inspect().data)
        self.assertEqual(state.inspect().data["migration_receipt"]["pending_resolution"]["pending_descriptor"], pending)

    def test_provisional_failure_is_sticky_even_after_later_live_success(self):
        self.coordinator.plan(PROFILE, run_id="test-run")
        def fail(phase):
            if phase == "before_cutover_provisional":
                raise OSError("synthetic disk failure")
        self.coordinator.fault_hook = fail
        failed = self.execute()
        self.assertTrue(failed["restored"], failed)
        self.coordinator.fault_hook = lambda _: None
        self.live_increment()
        result = self.execute()
        self.assertEqual(result["state"], "incomplete", result)
        self.assertTrue(result["provisional_failure"])

    def test_review_queue_append_history_is_imported_on_fresh_resume(self):
        queue = self.root / "review_queue/pending.jsonl"
        queue.parent.mkdir()
        first = {"schema_version": "review_queue.v1", "id": "old-queue-id", "status": "pending",
                 "title": "来源 https://example.test/?token=private-value", "summary": "记得回看这份资料",
                 "knowledge_topic_id": 1, "resources": {"links": ["https://example.test/?token=private-value"], "files": []}}
        queue.write_text(json.dumps(first, ensure_ascii=False) + "\n")
        self.coordinator.plan(PROFILE, run_id="test-run")
        def fault(phase):
            if phase == "after_freeze-1-processed-C0":
                raise RebuildError("synthetic_before_cutover")
        self.coordinator.fault_hook = fault
        self.assertTrue(self.execute()["restored"])
        second = dict(first, status="reviewed", summary="已经读过，保留这个人工判断")
        with queue.open("a") as file:
            file.write(json.dumps(second, ensure_ascii=False) + "\n")
        expected = queue.read_bytes()
        self.coordinator.fault_hook = lambda _: None
        result = self.execute()
        self.assertIn("live_check", result, result)
        run = journal_details(self.runtime_root, "test-run")
        self.assertTrue(self.coordinator.verify("test-run")["complete"])
        with closing(sqlite3.connect(run["plan"]["database"])) as conn:
            rows = [json.loads(r[0])["legacy_row"]["queue_item"] for r in conn.execute("SELECT payload_json FROM source_records WHERE kind='legacy_queue' ORDER BY capture_seq")]
        self.assertEqual(rows, [first, second])
        self.assertEqual(queue.read_bytes(), expected)
        visible = Path(run["plan"]["projection_root"], "Legacy/Review Queue.md").read_text()
        self.assertIn("reviewed", visible)
        self.assertNotIn("private-value", visible)

    def test_another_generation_plan_is_rejected_after_initial_activation(self):
        self.coordinator.plan(PROFILE, run_id="test-run")
        self.execute()
        with patch.object(self.coordinator, "source_factory", side_effect=AssertionError("source")), \
             patch.object(self.coordinator, "_roles", side_effect=AssertionError("model")):
            with self.assertRaisesRegex(RebuildError, "initial_migration_only"):
                self.coordinator.plan(PROFILE, run_id="another-run")

    def test_old_plan_cannot_start_after_another_generation_activates(self):
        self.coordinator.plan(PROFILE, run_id="old-plan")
        self.coordinator.plan(PROFILE, run_id="test-run")
        self.managed.on_restore = self.live_increment
        self.assertEqual(self.execute()["state"], "run_complete")
        before = list(self.managed.events)
        with patch.object(self.coordinator, "_roles", side_effect=AssertionError("provider before admission")):
            with self.assertRaisesRegex(RebuildError, "initial_migration_only"):
                self.coordinator.execute("old-plan", allow_source_read=True, allow_model_processing=True)
        self.assertEqual(self.managed.events, before)

    def test_source_overlap_rejected_before_path_metadata_inspection(self):
        config = self.config_store.inspect()
        config["db_dir"] = str(self.legacy.vault)
        with patch.object(self.config_store, "inspect", return_value=config), \
             patch("core.semantic.rebuild.safe_path", side_effect=AssertionError("protected path stat")):
            with self.assertRaisesRegex(RebuildError, "private_source_overlap"):
                self.coordinator.plan(PROFILE, run_id="test-run")

    def test_cutover_crash_preserves_fail_closed_pointer_then_resumes_cas(self):
        self.coordinator.plan(PROFILE, run_id="test-run")
        def fault(phase):
            if phase == "after_checkpoint_cas":
                raise RebuildError("synthetic_crash")
        self.coordinator.fault_hook = fault
        failed = self.execute()
        self.assertEqual(failed["code"], "synthetic_crash", failed)
        self.assertTrue(failed["recovery_required"])
        self.assertNotIn("restored", self.managed.events)
        with self.assertRaisesRegex(RebuildError, "recovery_required"):
            GenerationRegistry(self.runtime_root).pin()
        self.coordinator.fault_hook = lambda _: None
        self.managed.on_restore = self.live_increment
        result = self.execute()
        self.assertEqual(result["state"], "run_complete", result)
        self.assertEqual(self.source_calls, 1)

    def test_rollback_retains_raw_and_uses_monitor_state_restore(self):
        from core.monitor_state import MonitorStateStore
        self.coordinator.plan(PROFILE, run_id="test-run")
        self.execute()
        self.live_increment()
        self.assertEqual(self.coordinator.rollback("test-run")["state"], "rollback_planned")
        result = self.coordinator.rollback("test-run", apply=True, allow_runtime_changes=True)
        self.assertEqual(result["state"], "rolled_back", result)
        self.assertIsNone(GenerationRegistry(self.runtime_root).pin())
        state = MonitorStateStore(self.state_path).inspect()
        self.assertNotIn("semantic_generation_id", state.data)
        replay = read_json(self.runtime_root / "runs/test-run/rollback-replay.json")
        self.assertGreater(len(replay["retained_capture_receipt_ids"]), 2)
        self.assertTrue(Path(replay["retained_database"]).is_file())
        self.assertEqual(self.coordinator.status("test-run")["state"], "rolled_back")
        with self.assertRaisesRegex(RebuildError, "run_rolled_back"):
            self.execute()

    def test_rollback_restore_failure_requires_observed_restore_on_same_run(self):
        self.coordinator.plan(PROFILE, run_id="test-run")
        self.execute()
        self.managed.restore_ok = False
        failed = self.coordinator.rollback("test-run", apply=True, allow_runtime_changes=True)
        self.assertEqual(failed["state"], "incomplete")
        self.assertEqual(self.coordinator.rollback("test-run")["code"], "runtime_restore_pending")
        self.managed.restore_ok = True
        with patch.object(self.coordinator, "source_factory", side_effect=AssertionError("source")), \
             patch.object(self.coordinator, "_roles", side_effect=AssertionError("provider")):
            complete = self.coordinator.rollback("test-run", apply=True, allow_runtime_changes=True)
        self.assertEqual(complete["state"], "rolled_back")
        self.assertTrue(complete["restored"])


    def test_interrupt_before_provider_releases_locks_and_restores_exact_app(self):
        self.coordinator.plan(PROFILE, run_id="test-run")
        with patch.object(self.coordinator, '_freeze', side_effect=KeyboardInterrupt):
            result = self.execute()
        self.assertEqual(result['state'], 'incomplete')
        self.assertTrue(result['restored'])
        run = RebuildJournal(self.runtime_root, 'test-run').read()
        self.assertIn('rebuild_interrupted', [entry['code'] for entry in run['errors']])
        self.assertIsNone(GenerationRegistry(self.runtime_root).pin())


class RebuildPreparationMemoryTests(unittest.TestCase):
    def test_full_extraction_wave_releases_persisted_artifacts(self):
        import tracemalloc
        from core.semantic.scheduling import ProviderScheduler

        batches = [{"batch_id": str(index)} for index in range(48)]
        extracted, verified, published = [], [], []

        def connect():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.executescript("""
                CREATE TABLE capture_batch_members(batch_id TEXT, source_version_id TEXT, core INTEGER);
                CREATE TABLE processing_receipts(batch_id TEXT, receipt_json TEXT);
            """)
            conn.executemany("INSERT INTO processing_receipts VALUES(?,?)",
                             [(row["batch_id"], json.dumps(row)) for row in batches])
            return conn

        def extract(batch_id):
            extracted.append(batch_id)
            # Stand in for an already durable, resumed model artifact. The
            # coordinator has no reason to retain every returned payload.
            return {"observations": bytearray(512 * 1024)}

        runtime = SimpleNamespace(
            store=SimpleNamespace(list_batches=lambda: batches, connect=connect,
                                  verify_processing_receipt=verified.append),
            scheduler=ProviderScheduler(),
            engine=SimpleNamespace(extract_batch=extract,
                                   consolidate=lambda _: {"status": "unchanged"},
                                   reconcile_views=lambda _: {"status": "unchanged"}),
            index_all=lambda: {"complete": True},
            publish_all=lambda: published.append(True),
        )
        tracemalloc.start()
        try:
            result = RebuildCoordinator.__new__(RebuildCoordinator)._process_all(runtime, run_id="synthetic")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(sorted(extracted), sorted(row["batch_id"] for row in batches))
        self.assertEqual(verified, batches)
        self.assertEqual(published, [True])
        self.assertTrue(result["index"]["complete"])
        self.assertLess(peak, 12 * 1024 * 1024)


class ManagedRuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.lock_path = self.root / "app.lock"
        self.lock_path.write_text("123\n")
        self.executable = str(self.root / "WGO.app/Contents/MacOS/WeGroupchatObsidian")
        self.observation = {"label": "synthetic.agent", "plist_path": str(self.root / "agent.plist"),
            "plist_sha256": "a" * 64, "program_arguments": [self.executable],
            "working_directory": str(self.root), "was_loaded": True, "was_running": True}
        self.commands = []
        def runner(argv, **kwargs):
            self.commands.append(argv)
            if argv[:2] == ["launchctl", "print"]:
                return SimpleNamespace(returncode=0, stdout="  pid = 123\n")
            if argv[0] == "/bin/ps":
                return SimpleNamespace(returncode=0, stdout="Tue Oct 13 12:00:00 2026 " + self.executable)
            raise AssertionError("unexpected runtime mutation")
        self.runtime = ManagedRuntime(self.root, runner=runner,
            lock_factory=lambda: SimpleNamespace(path=self.lock_path))
        observation = patch.object(self.runtime, "inspect", return_value=self.observation)
        observation.start()
        self.addCleanup(observation.stop)

    def test_exact_launch_pid_start_executable_and_app_lock_owner_required(self):
        result = self.runtime.observe_process(self.observation)
        self.assertEqual(result["pid"], 123)
        self.assertEqual(result["executable"], self.executable)
        self.lock_path.write_text("456\n")
        self.assertIsNone(self.runtime.observe_process(self.observation))

    def test_other_executable_and_shell_wrapper_never_count_as_app_identity(self):
        expected = dict(self.observation, program_arguments=[str(self.root / "Other.app/Contents/MacOS/other")])
        with patch.object(self.runtime, "inspect", return_value=expected):
            with self.assertRaisesRegex(RebuildError, "process_identity_mismatch"):
                self.runtime.observe_process(expected)
        with self.assertRaisesRegex(RebuildError, "app_bundle_binding_required"):
            self.runtime.validate_execution(dict(self.observation, program_arguments=["/bin/bash", "启动.command"]))


class RebuildJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.journal = RebuildJournal(self.root, "run-1")
        self.journal.create({"schema": SCHEMA, "run_id": "run-1", "generation_id": "gen-1"})
        db = self.root / "generations/gen-1/knowledge.sqlite"
        db.parent.mkdir(parents=True)
        sqlite3.connect(db).close()
        self.manifest = {"schema": ACTIVE_SCHEMA, "generation_id": "gen-1", "database": str(db),
            "embedding_profile_id": "profile-1", "index_generation_id": "local-1",
            "projection_root": str(self.root / "vault/WGO"), "projection_receipt_id": "projection-1",
            "migration_receipt_ids": ["migration-1"], "config_revision": 1,
            "acceptance_receipt_id": "accept-1", "run_id": "run-1"}

    def begin(self):
        self.journal.begin_cutover(self.manifest, {"complete": True, "receipt_id": "accept-1"})

    def activate(self):
        self.begin()
        for phase in ("acceptance", "projections", "checkpoints"):
            self.journal.cutover_step(phase, {"receipt_id": phase})
        receipt = self.journal.activate_pointer()
        self.journal.cutover_step("active_manifest", receipt)
        self.journal.cutover_step("consistency", {"receipt_id": "consistent"})
        self.journal.cutover_step("activation_committed", {"receipt_id": "activated"})

    def test_every_partial_cutover_blocks_startup_until_activation_committed(self):
        self.begin()
        with self.assertRaisesRegex(RebuildError, "cutover_recovery_required"):
            GenerationRegistry(self.root).pin()
        self.activate()
        self.assertEqual(GenerationRegistry(self.root).pin().generation_id, "gen-1")

    def test_pointer_replacement_requires_checkpoints_and_exact_old_manifest(self):
        self.begin()
        with self.assertRaisesRegex(RebuildError, "checkpoints_required"):
            self.journal.activate_pointer()
        self.journal.cutover_step("acceptance", {"receipt_id": "acceptance"})
        self.journal.cutover_step("projections", {"receipt_id": "projections"})
        self.journal.cutover_step("checkpoints", {"receipt_id": "checkpoints"})
        write_json(self.root / "active.json", {"unexpected": True})
        with self.assertRaisesRegex(RebuildError, "active_generation_changed"):
            self.journal.activate_pointer()

    def test_restore_failure_and_sticky_provisional_failure_cannot_be_complete(self):
        self.activate()
        self.journal.cutover_step("provisional", {"receipt_id": "provisional"})
        failed = self.journal.finalize(restored=False, live_check={"complete": True})
        self.assertEqual(failed["state"], "incomplete")
        self.journal.error("provisional_receipt_failed", "provisional")
        retried = self.journal.finalize(restored=True, live_check={"complete": True})
        self.assertEqual(retried["state"], "incomplete")
        self.assertTrue(retried["provisional_failure"])

    def test_active_projection_and_index_are_one_manifest_pin(self):
        self.activate()
        changed = dict(self.manifest, index_generation_id="other-index")
        write_json(self.root / "active.json", changed)
        with self.assertRaisesRegex(RebuildError, "active_journal_mismatch"):
            GenerationRegistry(self.root).pin()

    def test_unknown_file_bytes_are_preserved_and_changed_file_fails(self):
        source = self.root / "note.md"
        source.write_bytes(b"personal\x00\xff exact bytes")
        receipts = backup_files([{"path": str(source)}], self.root / "backup")
        self.assertEqual(Path(receipts[0]["backup"]).read_bytes(), source.read_bytes())
        source.write_text("edited")
        with self.assertRaisesRegex(RebuildError, "backup_file_changed"):
            backup_files([receipts[0]], self.root / "other")

    def test_backup_includes_live_wal_commits(self):
        path = self.root / "old.sqlite"
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE events(value)")
        db.execute("INSERT INTO events VALUES('committed-in-wal')")
        db.commit()
        destination = self.root / "backup.sqlite"
        backup_sqlite(path, destination)
        result = sqlite3.connect(destination)
        self.addCleanup(result.close)
        self.assertEqual(result.execute("SELECT value FROM events").fetchone()[0], "committed-in-wal")
        self.assertEqual(destination.stat().st_mode & 0o077, 0)

    def test_symlink_and_escape_fail_before_copy_or_write(self):
        (self.root / "alias").symlink_to(self.root, target_is_directory=True)
        for path in ("alias/file", "../outside"):
            with self.assertRaises(RebuildError):
                safe_path(self.root, path)

    def test_present_empty_cutover_journal_never_falls_back_to_legacy(self):
        write_json(self.root / "cutover.json", {})
        with self.assertRaisesRegex(RebuildError, "cutover_journal_invalid"):
            GenerationRegistry(self.root).pin()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO check")
    def test_fifo_metadata_is_rejected_without_blocking_open(self):
        fifo = self.root / "cutover.json"
        os.mkfifo(fifo)
        with patch("core.semantic.rebuild.os.open", wraps=os.open) as opened:
            with self.assertRaisesRegex(RebuildError, "not_regular"):
                read_regular(fifo)
        self.assertTrue(opened.call_args.args[1] & os.O_NONBLOCK)


if __name__ == "__main__":
    unittest.main()
