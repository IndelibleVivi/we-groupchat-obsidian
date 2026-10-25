from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai.semantic_provider import ProviderError
from ai.semantic_provider import TextResult
from ai.vector_index import LocalCosineIndex
from core.monitor import TopicMonitor
from core.monitor_state import MonitorStateStore, MonitorStateError
from core.semantic.engine import SemanticEngine
from core.semantic.runtime import SemanticRuntime, ProviderRoles
from core.semantic.source import SemanticCapture, MonitorReceiptBridge
from core.semantic.store import SemanticStore
from tests.test_semantic_engine import FakeText, FakeDecision
from tests.test_semantic_retrieval import FakeEmbedding
from tests.test_semantic_source import Source
from tests.test_monitor_source import raw_message


class ProviderThinkingConfigTests(unittest.TestCase):
    def test_persisted_monitor_thinking_reaches_runtime_and_real_factory_kwargs(self):
        from ai.factory import create_provider
        from core.config import ConfigStore
        profile = {"schema": "we-groupchat-obsidian.semantic-providers.v1", "decision": "text",
                   "embedding": "local", "vector_index": "local", "local_embedding": {
                       "endpoint": "http://127.0.0.1:12345/v1/embeddings", "model": "synthetic-embedding",
                       "dimensions": 2, "context_window_tokens": 8192}}
        native = SimpleNamespace(model="synthetic-text", summarize=Mock(side_effect=AssertionError("provider request")))
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(Path(folder) / "config.json")
            store.replace({"ai_provider": "custom", "ai_model": "synthetic-text",
                           "ai_base_url": "https://synthetic.invalid/v1", "custom_thinking_parameter": "thinking"})
            for value in (None, True, False):
                with self.subTest(value=value):
                    if value is not None:
                        store.update(lambda config: config.update(monitor_ai_thinking=value))
                    config = store.inspect()
                    if value is None:
                        self.assertNotIn("monitor_ai_thinking", config)
                    with patch("core.semantic.runtime.create_provider", wraps=create_provider) as factory, \
                         patch("ai.factory.load_key", return_value="synthetic-key"), \
                         patch("core.semantic.runtime.load_key", side_effect=AssertionError("secret access")), \
                         patch("ai.openai_provider.OpenAIProvider", return_value=native) as provider, \
                         patch("core.semantic.runtime.LocalEmbeddingProvider.from_config", return_value=FakeEmbedding()):
                        ProviderRoles.from_profile(config, profile)
                    expected = False if value is None else value
                    self.assertIs(factory.call_args.args[0]["ai_thinking"], expected)
                    self.assertIs(provider.call_args.kwargs["thinking"], expected)
                    self.assertEqual(provider.call_args.kwargs["thinking_parameter"], "thinking")
            native.summarize.assert_not_called()


class SemanticRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = SemanticStore(self.root / "generation/knowledge.sqlite", "generation")
        self.state = MonitorStateStore(self.root / "checkpoint.json")
        self.source = Source({"shard": ("g", [raw_message("g", 1, 10, "嗯")])})
        self.text, self.decision = FakeText(noise=True), FakeDecision()
        roles = ProviderRoles(self.text, self.decision, FakeEmbedding(), LocalCosineIndex())
        self.runtime = SemanticRuntime(self.store, roles, self.root / "vault/WGO")
        full = SemanticCapture(self.store, self.source).snapshot(["chat"], snapshot_id="C0")
        for batch in full["batches"]:
            self.runtime.process(batch["batch_id"])
        new, receipt = MonitorReceiptBridge(self.store).migration(self.state.inspect(), full["h1"][0],
                                                                 [b["batch_id"] for b in full["batches"]])
        self.state.install_v3(0, new, receipt)
        self.monitor = TopicMonitor(self.source, {"monitor_chat_username": "chat", "monitor_topic": ""},
                                    state_store=self.state, semantic_runtime=self.runtime)

    def append(self, text="新消息", timestamp=20):
        self.source.shards["shard"]["messages"].append(raw_message("g", 2, timestamp, text))

    def test_active_generation_processes_without_legacy_interest_gate(self):
        self.append()
        result = self.monitor._check_once()
        self.assertEqual(result["status"], "semantic_applied")
        self.assertEqual(self.state.inspect().data["last_checked_ts"], 20)
        self.assertEqual(self.runtime.store.list_items(), [])
        self.assertEqual(self.monitor._check_once()["status"], "no_messages")

    def test_missing_active_pin_never_falls_back_with_semantic_checkpoint(self):
        (self.root / "missing-runtime").mkdir()
        monitor = TopicMonitor(self.source, {
            "monitor_chat_username": "chat", "monitor_topic": "old preference",
            "semantic_runtime_root": str(self.root / "missing-runtime"),
            "monitor_knowledge_enabled": False,
        }, state_store=self.state)
        before = self.state.inspect()
        calls = len(self.source.page_calls)
        for dry_run in (False, True):
            self.assertEqual(monitor._check_once(dry_run=dry_run)["status"],
                             "semantic_active_generation_missing")
        self.assertEqual(self.state.inspect(), before)
        self.assertEqual(len(self.source.page_calls), calls)

    def test_unavailable_explicit_runtime_root_blocks_before_source_or_provider(self):
        from core.semantic.rebuild import RebuildError
        config = {"semantic_runtime_root": str(self.root / "unmounted-runtime")}
        with patch.object(ProviderRoles, "from_profile", side_effect=AssertionError("provider")), \
             self.assertRaisesRegex(RebuildError, "semantic_runtime_root_unavailable"):
            SemanticRuntime.active(config)
        self.assertFalse((self.root / "unmounted-runtime").exists())

    def test_provider_failure_keeps_capture_but_does_not_advance_applied(self):
        self.append()
        before = self.state.inspect().data["source_cursors"]
        with patch.object(self.text, "generate", side_effect=ProviderError("provider_timeout", retryable=True)):
            result = self.monitor._check_once()
        self.assertEqual(result["status"], "provider_timeout")
        state = self.state.inspect().data
        self.assertEqual(state["source_cursors"], before)
        self.assertNotEqual(state["captured_cursors"], before)
        self.assertGreater(state["ai_next_retry_after"], 0)
        with patch.object(self.text, "generate", side_effect=AssertionError("provider in backoff")):
            self.assertEqual(self.monitor._check_once()["status"], "ai_backoff")

    def test_deferred_prefix_allows_later_context_but_ack_waits_for_the_gap(self):
        class ContextDecision(FakeDecision):
            def decide(self, request, **kwargs):
                state = request.state.get("state", {})
                self.defer = (any(o["title"] == "ambiguous" for o in state.get("observations", ()))
                              and not state.get("items"))
                return super().decide(request, **kwargs)
        self.text.noise = False
        decision = ContextDecision()
        self.runtime.engine.decision_provider = decision
        self.monitor.config["monitor_max_messages_per_run"] = 1
        self.append("ambiguous")
        before = self.state.inspect().data["source_cursors"]
        self.assertEqual(self.monitor._check_once()["status"], "semantic_processing_pending")
        counts = len(self.text.calls), len(decision.calls)
        self.assertEqual(self.monitor._check_once()["status"], "semantic_processing_pending")
        self.assertEqual((len(self.text.calls), len(decision.calls)), counts)
        self.source.shards["shard"]["messages"].append(raw_message("g", 3, 30, "evidence"))
        with patch.object(self.runtime, "maintain_derived", return_value={"state": "ready"}):
            self.assertEqual(self.monitor._check_once()["status"], "semantic_processing_pending")
        self.assertEqual(self.state.inspect().data["source_cursors"], before)
        self.assertEqual(len(self.store.list_items()), 1)
        # Restart keeps epoch/accepted artifacts; ACK moves only in raw order.
        self.runtime.engine = SemanticEngine(self.store, self.text, decision)
        with patch.object(self.runtime, "maintain_derived", return_value={"state": "ready"}):
            for _ in range(4):
                self.monitor._check_once()
        self.assertEqual(self.state.inspect().data["last_checked_ts"], 30)
        self.assertEqual(len(self.store.list_items()), 1)
        calls = len(self.text.calls), len(decision.calls)
        with patch.object(self.runtime, "maintain_derived", return_value={"state": "ready"}):
            self.monitor._check_once()
        self.assertEqual((len(self.text.calls), len(decision.calls)), calls)

    def test_capture_cas_failure_adopts_exact_retained_batch(self):
        self.append()
        with patch.object(self.state, "commit_capture", side_effect=MonitorStateError("monitor_state_conflict")):
            with self.assertRaises(MonitorStateError):
                self.monitor._check_once()
        count = len(self.source.page_calls)
        calls = 0
        original = self.store.get_batch

        def counted(batch_id):
            nonlocal calls
            calls += 1
            return original(batch_id)

        with patch.object(self.store, "get_batch", side_effect=counted):
            self.assertEqual(self.monitor._check_once()["status"], "semantic_applied")
        self.assertEqual(len(self.source.page_calls), count)
        self.assertLessEqual(calls, 5)
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM capture_batches WHERE purpose='live'").fetchone()[0], 1)

    def test_idle_runtime_keeps_batch_hydration_bounded_by_applied_history(self):
        snapshot = self.state.inspect()
        for index in range(25):
            output = {key: dict(value, cursor_token=json.dumps([10 + index, 0]))
                      for key, value in snapshot.data["captured_cursors"].items()}
            batch = self.store.capture_batch(
                "fixture-live-" + str(index), [], source_namespace=snapshot.data["source_namespace"],
                chat_id=snapshot.data["chat_id"], inventory_digest=snapshot.data["source_inventory_digest"],
                inventory_revision=snapshot.data["source_inventory_revision"],
                input_cursors=output, output_cursors=output, source_eof=True)
            with self.store.transaction() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS semantic_applied_batches(batch_id TEXT PRIMARY KEY,state_revision INTEGER NOT NULL)")
                conn.execute("INSERT INTO semantic_applied_batches VALUES(?,?)", (batch["batch_id"], index))
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM capture_batches WHERE purpose='live'").fetchone()[0], 25)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM semantic_applied_batches").fetchone()[0], 25)
        calls = []
        original = self.store.get_batch
        real_capture = self.runtime._unapplied_live_batches
        unapplied = []

        def capture_candidates(username):
            unapplied.append(real_capture(username))
            return unapplied[-1]

        def counted(batch_id):
            calls.append(batch_id)
            return original(batch_id)

        with patch.object(self.store, "get_batch", side_effect=counted), \
             patch.object(self.runtime, "_unapplied_live_batches", side_effect=capture_candidates):
            for _ in range(3):
                self.assertEqual(self.monitor._check_once()["status"], "no_messages")
                self.assertLessEqual(len(calls), 4 * (_ + 1))
                self.assertEqual([len(value) for value in unapplied[2 * _:2 * (_ + 1)]], [0, 1])
        self.assertEqual(len(calls), 12)

    def test_unapplied_selection_keeps_later_deferred_context_batch(self):
        snapshot = self.state.inspect()
        cursors = snapshot.data["captured_cursors"]
        first_output = {key: dict(value, cursor_token=json.dumps([20, 0]))
                        for key, value in cursors.items()}
        later_output = {key: dict(value, cursor_token=json.dumps([30, 0]))
                        for key, value in cursors.items()}
        first = self.store.capture_batch(
            "fixture-prefix", [], source_namespace=snapshot.data["source_namespace"],
            chat_id=snapshot.data["chat_id"], inventory_digest=snapshot.data["source_inventory_digest"],
            inventory_revision=snapshot.data["source_inventory_revision"],
            input_cursors=cursors, output_cursors=first_output, source_eof=False)
        later = self.store.capture_batch(
            "fixture-later", [], source_namespace=snapshot.data["source_namespace"],
            chat_id=snapshot.data["chat_id"], inventory_digest=snapshot.data["source_inventory_digest"],
            inventory_revision=snapshot.data["source_inventory_revision"],
            input_cursors=first_output, output_cursors=later_output, source_eof=True)
        selected = self.runtime._unapplied_live_batches(snapshot.data["chat_id"])
        self.assertEqual([entry["batch_id"] for entry in selected],
                         [first["batch_id"], later["batch_id"]])

    def test_projection_failure_does_not_replay_canonical_provider_work(self):
        self.append()
        with patch.object(self.runtime, "_publish_all", side_effect=OSError("synthetic projection failure")):
            result = self.monitor._check_once()
        self.assertEqual(result["status"], "semantic_applied")
        self.assertEqual(result["derived_state"], "blocked")
        self.assertFalse(result["derived"]["projection"]["complete"])
        self.assertEqual(self.state.inspect().data["last_checked_ts"], 20)
        called = len(self.text.calls)
        self.monitor._check_once()
        self.assertEqual(len(self.text.calls), called)

    def test_projection_summary_failure_after_ack_still_attempts_independent_index(self):
        self.append()
        with patch.object(self.runtime, "_projection_summary", side_effect=OSError("private inspection failure")), \
             patch.object(self.runtime, "index_all", wraps=self.runtime.index_all) as index:
            result = self.monitor._check_once()
        self.assertEqual(result["status"], "semantic_applied")
        self.assertEqual(result["derived_state"], "blocked")
        self.assertEqual(result["derived"]["index"]["state"], "ready")
        self.assertEqual(self.state.inspect().data["last_checked_ts"], 20)
        index.assert_called_once_with(max_rounds=1)

    def test_dry_run_has_zero_source_or_provider_side_effects(self):
        before = self.state.inspect()
        with patch.object(self.source, "get_source_inventory", side_effect=AssertionError("source")), \
             patch.object(self.text, "generate", side_effect=AssertionError("model")):
            self.assertEqual(self.monitor._check_once(dry_run=True)["status"], "semantic_dry_run")
        self.assertEqual(self.state.inspect(), before)

    def test_new_selection_starts_now_and_keeps_same_second_rows(self):
        new_state = MonitorStateStore(self.root / "new-checkpoint.json")
        self.monitor.state_store = new_state
        self.monitor.now_func = lambda: 20
        self.append(timestamp=20)
        self.assertEqual(self.monitor._check_once()["status"], "initialized")
        self.assertEqual(new_state.inspect().data["last_checked_ts"], 20)
        self.assertEqual(self.monitor._check_once()["status"], "semantic_applied")
        with closing(self.store.connect()) as conn:
            boundary = conn.execute("SELECT source_eof FROM capture_batches WHERE purpose LIKE 'from-now:%'").fetchone()
        self.assertEqual(boundary[0], 0)

    def test_failed_initial_state_cas_reuses_original_from_now_boundary(self):
        new_state = MonitorStateStore(self.root / "new-checkpoint.json")
        self.monitor.state_store = new_state
        self.monitor.now_func = lambda: 20
        with patch.object(new_state, "install_v3", side_effect=MonitorStateError("monitor_state_conflict")):
            with self.assertRaises(MonitorStateError):
                self.monitor._check_once()
        self.monitor.now_func = lambda: 100
        self.assertEqual(self.monitor._check_once()["status"], "initialized")
        self.assertEqual(new_state.inspect().data["last_checked_ts"], 20)

    def test_existing_legacy_progress_requires_selection_or_full_rebuild(self):
        legacy = MonitorStateStore(self.root / "old-checkpoint.json")
        legacy.initialize_if_absent({"last_checked_ts": 5})
        self.monitor.state_store = legacy
        self.assertEqual(self.monitor._check_once()["status"], "semantic_checkpoint_migration_required")
        self.monitor.config["semantic_selection_started_at"] = 20
        self.assertEqual(self.monitor._check_once()["status"], "initialized")
        self.assertEqual(legacy.inspect().data["last_checked_ts"], 20)

    def test_brief_revises_existing_day_even_before_changed_schedule(self):
        now = datetime(2026, 10, 13, 10, tzinfo=timezone.utc)
        self.runtime.config["daily_digest_time"] = "23:30"
        with patch.object(self.runtime, "brief", return_value="edition") as build:
            self.assertIsNone(self.runtime.brief_if_due(now=now))
            with patch.object(self.runtime.briefing, "edition", return_value={"content_revision": 1}):
                self.assertEqual(self.runtime.brief_if_due(now=now), "edition")
                self.assertEqual(self.runtime.brief_if_due(now=now), "edition")
            self.assertEqual(build.call_count, 2)

    def test_life_story_runs_source_to_item_body_brief_and_reader_without_legacy_gate(self):
        from core.semantic.reader_runtime import ReaderRuntime
        self.text.noise = False
        original = self.text.generate

        def generate(prompt, *, request_id, generation_id=""):
            if not prompt.startswith("{"):
                return original(prompt, request_id=request_id, generation_id=generation_id)
            supplied = json.loads(prompt)
            if "snapshot" in supplied:
                snapshot = supplied["snapshot"]
                result = {"item_id": snapshot["item_id"], "content_revision": snapshot["content_revision"], "blocks": [
                    {"id": "paragraph-" + unit["id"], "role": unit["role"], "text": unit["text"],
                     "unit_ids": [unit["id"]], "evidence_ids": unit["evidence_ids"], "resource_ids": unit["resource_ids"],
                     "primary_home": snapshot["item_id"], "unit_statuses": {}}
                    for unit in snapshot["units"]]}
            elif supplied.get("phase") == "brief_write_v1":
                changes = supplied["changes"]
                result = {"status": "success", "title": "今天的花", "paragraphs": [
                    {"text": "散步回来带了一束花，是今天值得留下的小故事。", "change_ids": [c["id"] for c in changes],
                     "evidence_refs": [{"item_id": c["item_id"], "evidence_id": c["evidence_ids"][0]} for c in changes],
                     "scopes": [], "corrections": []}]}
            else:
                raise AssertionError("unexpected synthetic prompt")
            return TextResult(json.dumps(result, ensure_ascii=False), self.text.route)

        with patch.object(self.text, "generate", side_effect=generate):
            self.append("沿河散步，回家前买了一束花。")
            result = self.monitor._check_once()
            self.assertEqual(result["status"], "semantic_applied")
            self.assertEqual(result["derived_state"], "ready")
            item = self.store.list_items()[0]
            body_path = self.runtime.publisher.resolve(item["item_id"])
            self.assertIn("买了一束花", self.runtime.publisher.read(body_path).decode())
            now = datetime.now(timezone.utc)
            brief = self.runtime.brief(now=now)
            self.assertEqual(brief.status, "success")
            self.assertEqual(self.runtime.brief(now=now).revision, brief.revision)
            reader = ReaderRuntime(self.runtime, device_id="test-device", live=False)
            status = reader.poll()
            self.assertFalse(status.get("codes"))
            self.assertEqual(self.runtime.controls.preference(item["item_id"]).known_revision, 0)
            self.assertIsNotNone(self.runtime.publisher.read("检索.md"))
            self.assertIsNotNone(self.runtime.publisher.read("管理.md"))
            # Registering legal reader properties changes file bytes, not the
            # canonical projection receipt. Only the publisher owns its ACK.
            self.assertEqual(self.runtime.publish_all()["conflicts"], 0)
            self.assertFalse(reader.poll().get("codes"))


if __name__ == "__main__":
    unittest.main()
