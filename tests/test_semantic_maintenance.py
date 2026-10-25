"""Source-free derived recovery against temporary canonical stores and vaults."""
from contextlib import closing
from pathlib import Path
import json
import re
import tempfile
import unittest
from unittest.mock import patch

from ai.semantic_provider import ProviderError
from ai.vector_index import LocalCosineIndex
from core.semantic.projections import ProjectionReceipt, ReadingError
from core.semantic.reader_runtime import ReaderRuntime
from core.semantic.runtime import ProviderRoles, SemanticRuntime
from core.semantic.store import SemanticStore
from tests.test_semantic_engine import FakeDecision
from tests import test_semantic_projections as projection_fixtures
from tests.test_semantic_reader_runtime import PipelineText
from tests.test_semantic_retrieval import FakeEmbedding


class MaintenanceTests(unittest.TestCase):
    canonical_extract = projection_fixtures.ReadingHarness.canonical_extract
    canonical_actions = projection_fixtures.ReadingHarness.canonical_actions

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = SemanticStore(self.root / "canonical.sqlite", "maintenance-generation")
        self.text = PipelineText()
        self.runtime = self.restart()

    def restart(self):
        return SemanticRuntime(self.store,
            ProviderRoles(self.text, FakeDecision(), FakeEmbedding(), LocalCosineIndex()),
            self.root / "vault/WGO")

    def item(self, item_id="cat"):
        observation, _ = self.canonical_extract(self.store, "capture-" + item_id, "猫在窗边睡着了。")
        self.canonical_actions(self.store, [{"action_key": "create-" + item_id, "type": "CREATE_ITEM",
            "item_id": item_id, "kind": "story", "title": "窗边的猫", "identity_scope": {"scene": item_id},
            "observation_ids": [observation["observation_id"]]}])

    def pending(self):
        with closing(self.store.connect()) as conn:
            return [tuple(row) for row in conn.execute(
                "SELECT owner_id,status FROM projection_outbox ORDER BY owner_id")]

    def test_index_failure_still_publishes_real_markdown_acks_and_restart_reuses_body(self):
        self.item()
        revision = self.store.read_revision()
        with patch.object(self.runtime, "index_all", side_effect=OSError("private endpoint and content")):
            result = self.runtime.maintain_derived()
        self.assertEqual(result["index"]["state"], "blocked")
        self.assertEqual(result["projection"]["state"], "ready")
        self.assertTrue(self.runtime.publisher.ready("cat", 1))
        self.assertIn("猫在窗边睡着了", self.runtime.publisher.read(self.runtime.publisher.resolve("cat")).decode())
        self.assertEqual(self.pending(), [("cat", "done")])
        self.assertTrue(self.runtime.publisher.ready("control:home"))
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(self.store.read_revision(), revision)
        runtime = self.restart()
        with patch.object(self.text, "generate", side_effect=AssertionError("accepted Body replay")), \
             patch.object(runtime, "index_all", side_effect=OSError("still offline")):
            replay = runtime.maintain_derived()
        self.assertEqual(replay["projection"]["state"], "ready")
        self.assertEqual(self.pending(), [("cat", "done")])
        self.assertEqual(self.store.read_revision(), revision)

    def test_local_item_failure_does_not_prevent_other_pages_and_strict_still_raises(self):
        self.item("one")
        self.item("two")
        bad = self.store.list_items()[0]["item_id"]
        good = "two" if bad == "one" else "one"
        original = self.runtime._prepare_item
        def prepare(item):
            if item["item_id"] == bad:
                raise ReadingError("synthetic_local_failure")
            return original(item)
        with patch.object(self.runtime, "_prepare_item", side_effect=prepare):
            result = self.runtime.maintain_derived()
            with self.assertRaisesRegex(ReadingError, "synthetic_local_failure"):
                self.runtime.publish_all()
        self.assertTrue(self.runtime.publisher.ready(good, 1))
        self.assertFalse(self.runtime.publisher.ready(bad, 1))
        self.assertEqual(dict(self.pending()), {bad: "pending", good: "done"})
        self.assertEqual(result["projection"]["codes"]["synthetic_local_failure"], 1)
        self.assertFalse(result["projection"]["complete"])

    def test_provider_failure_stops_item_phase_and_unknown_never_redispatches_after_restart(self):
        self.item()
        with patch.object(self.text, "generate", side_effect=ProviderError(
                "synthetic_provider_unknown", outcome_unknown=True)) as provider:
            first = self.runtime.maintain_derived()
        self.assertEqual(first["projection"]["state"], "blocked")
        provider.assert_called_once()
        before = [tuple(row) for row in self.runtime.ledger.all("SELECT * FROM reader_text_artifacts")]
        runtime = self.restart()
        with patch.object(self.text, "generate", side_effect=AssertionError("unknown must not resend")) as provider:
            again = runtime.maintain_derived()
        self.assertEqual(again["projection"]["state"], "blocked")
        provider.assert_not_called()
        self.assertEqual([tuple(row) for row in runtime.ledger.all("SELECT * FROM reader_text_artifacts")], before)
        self.assertEqual(self.pending(), [("cat", "pending")])

    def test_provider_and_trusted_predispatch_stop_do_not_attempt_later_items(self):
        self.item("one")
        self.item("two")
        scheduler = self.runtime.scheduler
        with scheduler.phase() as phase:
            phase.stop(ProviderError("synthetic_stopped"))
            try:
                scheduler.call("text", 1, "synthetic-request", lambda: self.fail("dispatch"))
            except ProviderError as exc:
                unsent = exc
        for error in (ProviderError("synthetic_provider_failure", outcome_unknown=False), unsent):
            with self.subTest(error=error.code), \
                 patch.object(self.runtime, "_prepare_item", side_effect=error) as prepare:
                result = self.runtime.maintain_derived()
            prepare.assert_called_once()
            self.assertEqual(result["projection"]["unprocessed"], 1)
            self.assertEqual(result["projection"]["state"], "blocked")

    def test_projection_pass_or_summary_failure_never_skips_index(self):
        for boundary in ("_publish_all", "_projection_summary"):
            with self.subTest(boundary=boundary), \
                 patch.object(self.runtime, boundary, side_effect=OSError("private failure")), \
                 patch.object(self.runtime, "index_all", return_value={"complete": True, "pending": 0}) as index:
                result = self.runtime.maintain_derived()
            index.assert_called_once_with(max_rounds=1)
            self.assertEqual(result["index"]["state"], "ready")
            self.assertEqual(result["projection"]["state"], "blocked")
            self.assertIsNone(result["projection"]["pending_outbox"])
            self.assertNotIn("private", json.dumps(result))

    def test_view_query_management_and_home_receipts_all_affect_completion(self):
        self.store.create_view("life", "日常", "日常")
        self.assertTrue(self.runtime.publish_all()["complete"])
        for target in ("view:life", "control:query", "control:management", "control:home"):
            with self.subTest(target=target):
                receipt = ProjectionReceipt(target, 1, "conflict", "synthetic.md")
                if target == "view:life":
                    boundary = patch.object(self.runtime.publisher, "publish_view", return_value=receipt)
                elif target == "control:query":
                    boundary = patch.object(self.runtime.controls, "initialize_query", return_value=receipt)
                elif target == "control:management":
                    boundary = patch.object(self.runtime.controls, "refresh_management", return_value=receipt)
                else:
                    boundary = patch("core.semantic.runtime.ReadingHome.publish", return_value=receipt)
                with boundary:
                    self.assertFalse(self.runtime.publish_all()["complete"])
                    result = self.runtime.maintain_derived()
                self.assertFalse(result["projection"]["complete"])
                self.assertGreaterEqual(result["projection"]["conflicts"], 1)

    def test_real_manual_home_and_view_conflicts_preserve_bytes_and_cannot_report_complete(self):
        self.store.create_view("life", "日常", "日常")
        self.assertTrue(self.runtime.publish_all()["complete"])
        for target in ("view:life", "control:home"):
            path = self.runtime.publisher.root / self.runtime.publisher.resolve(target)
            path.write_text(re.sub(r"(<!-- wgo:begin body -->)", r"\1\n个人安排", path.read_text(), count=1))
            before = path.read_bytes()
            result = self.runtime.maintain_derived()
            self.assertFalse(result["projection"]["complete"])
            self.assertGreaterEqual(result["projection"]["conflicts"], 1)
            self.assertEqual(path.read_bytes(), before)

    def test_pending_index_is_not_counted_as_a_failure(self):
        with patch.object(self.runtime, "index_all", return_value={
                "complete": False, "pending": 2, "code": "index_confirmation_pending"}):
            result = self.runtime.maintain_derived()
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["index"]["failed"], 0)
        self.assertEqual(result["index"]["pending"], 2)

    def test_reader_controls_precede_maintenance_and_status_survives_ready_probe_failure(self):
        self.item()
        self.runtime.publish_all()
        reader = ReaderRuntime(self.runtime, device_id="synthetic-device")
        reader.poll()
        # Register a real user property edit. The maintenance entry must see
        # its consumed canonical preference before any embedding work.
        from core.semantic.projections import patch_properties
        path = self.runtime.publisher.root / self.runtime.publisher.resolve("cat")
        path.write_text(patch_properties(path.read_text(), {reader.properties["follow"]: True}, machine_only=False))
        original = self.runtime.index_all
        def index(**kwargs):
            self.assertTrue(self.runtime.controls.preference("cat").follow)
            return original(**kwargs)
        with patch.object(self.runtime, "index_all", side_effect=index):
            result = reader.poll(maintain_derived=True)
        self.assertIn("derived", result)
        status = self.runtime.publisher.read("阅读状态.md").decode()
        self.assertIn("索引维护", status)
        self.assertIn("正文维护", status)
        ready = self.runtime.publisher.ready
        def probe(object_id, revision=None):
            if object_id == "cat":
                raise PermissionError("private path")
            return ready(object_id, revision)
        with patch.object(self.runtime.publisher, "ready", side_effect=probe):
            result = reader.poll(maintain_derived=True)
        self.assertIn("reader_operation_deferred", result["codes"])
        self.assertIn("正文维护", self.runtime.publisher.read("阅读状态.md").decode())
        self.assertNotIn("private path", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
