"""Synthetic admission, durable replay and actual full-rebuild concurrency."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from ai.semantic_provider import ProviderError, ProviderRoute, TextResult, TextDecisionProvider
from ai.vector_index import LocalCosineIndex
from core.config import ConfigError, _sanitize_config
from core.semantic.engine import SemanticEngine, SemanticValidationError
from core.semantic.jobs import JobStore
from core.semantic.projections import ReadingLedger, accepted_text_artifact
from core.semantic.rebuild import RebuildCoordinator
from core.semantic.runtime import ProviderRoles, SemanticRuntime
from core.semantic.scheduling import (ProviderScheduler, ScheduledText,
    not_dispatched, validate_limits)
from core.semantic.store import SemanticStore
from tests.test_semantic_engine import FakeText, FakeDecision, capture
from tests.test_semantic_retrieval import FakeEmbedding, AsyncIndex


def wait_for_waiters(scheduler, count=1):
    deadline = time.monotonic() + 5
    with scheduler.condition:
        while len(scheduler.gates["text"].waiters) < count:
            if time.monotonic() >= deadline:
                raise AssertionError("synthetic admission waiter did not arrive")
            scheduler.condition.wait(.01)


class ConcurrentProbe:
    """A barrier proves overlapping callbacks without wall-clock speed claims."""
    def __init__(self, width):
        self.barrier = threading.Barrier(width)
        self.width, self.active, self.peak, self.count = width, 0, 0, 0
        self.lock = threading.Lock()

    def call(self, callback):
        with self.lock:
            self.count += 1
            ordinal = self.count
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if ordinal <= self.width:
                self.barrier.wait(5)
            return callback()
        finally:
            with self.lock:
                self.active -= 1


class ProviderSchedulerTests(unittest.TestCase):
    def test_bounded_real_parallelism_and_order(self):
        scheduler = ProviderScheduler({"text": {"max_inflight": 3}})
        probe = ConcurrentProbe(3)
        result = scheduler.map("text", range(11), lambda n:
            scheduler.call("text", 10, str(n), lambda: probe.call(lambda: n * 2)))
        self.assertEqual(result, [n * 2 for n in range(11)])
        self.assertEqual(probe.peak, 3)
        self.assertEqual(scheduler.status()["text"]["peak_inflight"], 3)

    def test_rate_windows_enforce_explicit_rpm_and_tpm(self):
        for limits in ({"requests_per_minute": 1}, {"tokens_per_minute": 10}):
            with self.subTest(limits=limits):
                clock = [0.]
                scheduler = ProviderScheduler({"text": {**limits, "max_inflight": 2}}, clock=lambda: clock[0])
                sent = []
                scheduler.call("text", 6, "first", lambda: sent.append("first"))
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(scheduler.call, "text", 6, "second", lambda: sent.append("second"))
                    wait_for_waiters(scheduler)
                    self.assertEqual(sent, ["first"])
                    with scheduler.condition:
                        clock[0] = 60.
                        scheduler.condition.notify_all()
                    future.result(5)
                self.assertEqual(sent, ["first", "second"])

    def test_unset_rates_are_reported_and_config_is_strict(self):
        self.assertEqual(validate_limits()["text"]["max_inflight"], 8)
        self.assertEqual(validate_limits()["decision"]["max_inflight"], 4)
        self.assertEqual(ProviderScheduler().status()["embedding"]["unset"],
                         ["requests_per_minute", "tokens_per_minute"])
        self.assertEqual(_sanitize_config({"semantic_provider_limits": {"text": {"max_inflight": 2}}})
                         ["semantic_provider_limits"]["text"]["max_inflight"], 2)
        for bad in ({"text": {"rpm": 2}}, {"decision": {"max_inflight": True}},
                    {"embedding": {"tokens_per_minute": 0}}, {"text": {"max_inflight": 65}}):
            with self.assertRaises(ConfigError):
                _sanitize_config({"semantic_provider_limits": bad})

    def test_separately_constructed_roles_share_route_account_admission(self):
        model = "scheduler-" + uuid.uuid4().hex
        config = {"semantic_provider_limits": {"text": {"max_inflight": 2}}}
        probe = ConcurrentProbe(2)
        def roles(credential):
            text = FakeText()
            text.route = ProviderRoute("synthetic", model, "text")
            text.client = SimpleNamespace(api_key=credential, base_url="https://synthetic.invalid")
            text.generate = lambda _prompt, **_kw: probe.call(lambda: TextResult("accepted", text.route))
            return ProviderRoles(text, TextDecisionProvider(text), FakeEmbedding(), LocalCosineIndex()).scheduled(config)
        first, second, different = roles("synthetic-a"), roles("synthetic-a"), roles("synthetic-b")
        self.assertIs(first.scheduler.gates["text"], second.scheduler.gates["text"])
        self.assertIsNot(first.scheduler.gates["text"], different.scheduler.gates["text"])
        self.assertIs(first.scheduler.gates["decision"], second.scheduler.gates["decision"])
        self.assertIsNot(first.scheduler.gates["decision"], different.scheduler.gates["decision"])
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit((first if i % 2 else second).text.generate, "input", request_id=str(i))
                       for i in range(6)]
            self.assertTrue(all(future.result(5).text == "accepted" for future in futures))
        self.assertEqual(probe.peak, 2)
        # A later timer instance retains the same rate-window reservations.
        self.assertEqual(roles("synthetic-a").scheduler.status()["text"]["dispatched"], 6)


class DurableSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = SemanticStore(self.root / "knowledge.sqlite", "schedule-test")

    def engine(self, scheduler):
        return SemanticEngine(self.store, ScheduledText(FakeText(), scheduler), FakeDecision())

    def test_unsent_claims_defer_and_unknown_dispatched_claim_does_not_retry(self):
        scheduler = ProviderScheduler({"text": {"max_inflight": 2, "requests_per_minute": 1}})
        engine = self.engine(scheduler)
        jobs = [engine.jobs.ensure("extract", {"input": n}, {"synthetic": "v1"}) for n in range(4)]
        release, sent = threading.Event(), []
        def invoke(job):
            def callback(request_id):
                def wire():
                    sent.append(request_id)
                    self.assertTrue(release.wait(5))
                    raise ProviderError("provider_timeout", retryable=True, outcome_unknown=True)
                return scheduler.call("text", 1, request_id, wire)
            return engine._accepted(job, callback)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(scheduler.map, "text", jobs, invoke)
            try:
                wait_for_waiters(scheduler)
            finally:
                release.set()
            with self.assertRaisesRegex(ProviderError, "provider_timeout"):
                future.result(5)
        self.assertEqual(len(sent), 1)
        with closing(self.store.connect()) as db:
            states = dict(db.execute("SELECT job_id,state FROM jobs"))
            attempts = [dict(r) for r in db.execute("SELECT * FROM job_attempts")]
        self.assertEqual(states[sent[0]], "unknown")
        self.assertEqual(list(states.values()).count("pending"), 3)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sum(row["outcome_unknown"] for row in attempts), 1)
        pending = next(job for job in jobs if states[job["job_id"]] == "pending")
        resumed = self.engine(ProviderScheduler())
        self.assertEqual(resumed._accepted(pending, lambda _id: {"ok": True}), {"ok": True})
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            resumed._accepted(next(job for job in jobs if job["job_id"] == sent[0]),
                              lambda _id: self.fail("unknown must not dispatch"))

    def test_throttle_stops_admission_drains_accepted_and_restart_reuses(self):
        scheduler = ProviderScheduler({"text": {"max_inflight": 2}})
        engine = self.engine(scheduler)
        jobs = [engine.jobs.ensure("extract", {"input": n}, {"synthetic": "v1"}) for n in range(6)]
        barrier, stopped = threading.Barrier(2), threading.Event()
        sent, lock = [], threading.Lock()
        def invoke(job):
            def callback(request_id):
                def wire():
                    with lock:
                        sent.append(request_id)
                        first = len(sent) == 1
                    barrier.wait(5)
                    if first:
                        raise ProviderError("provider_rate_limited", retryable=True)
                    self.assertTrue(stopped.wait(5))
                    return {"accepted": request_id}
                return scheduler.call("text", 1, request_id, wire)
            try:
                return engine._accepted(job, callback)
            except ProviderError:
                stopped.set()
                raise
        with self.assertRaisesRegex(ProviderError, "provider_rate_limited"):
            scheduler.map("text", jobs, invoke)
        self.assertEqual(len(sent), 2)
        with closing(self.store.connect()) as db:
            states = dict(db.execute("SELECT job_id,state FROM jobs"))
        self.assertEqual(states[sent[0]], "failed")
        self.assertEqual(states[sent[1]], "accepted")
        self.assertEqual(list(states.values()).count("pending"), 4)
        restarted = self.engine(ProviderScheduler())
        self.assertEqual(restarted._accepted(next(job for job in jobs if job["job_id"] == sent[1]),
                                            lambda _id: self.fail("accepted artifact replay")),
                         {"accepted": sent[1]})

    def test_consumer_unsent_is_pending_but_arbitrary_provider_error_is_not_proof(self):
        ledger = ReadingLedger(self.store.connect, self.store.generation_id)
        text = SimpleNamespace(route=ProviderRoute("synthetic", "consumer", "text"),
                               generate=lambda *_a, **_kw: TextResult('{"ok":true}',
                                           ProviderRoute("synthetic", "consumer", "text")))
        with self.assertRaises(ProviderError) as stopped:
            accepted_text_artifact(ledger, ScheduledText(text, ProviderScheduler({"text": {"tokens_per_minute": 1}})),
                                   prompt="input", request_id="consumer", prompt_version="test", validate=json.loads)
        self.assertTrue(not_dispatched(stopped.exception, "consumer"))
        self.assertEqual(ledger.one("SELECT status FROM reader_text_artifacts")[0], "pending")
        accepted_text_artifact(ledger, text, prompt="input", request_id="consumer", prompt_version="test",
                               validate=lambda value: json.dumps(json.loads(value)))
        self.assertEqual(ledger.one("SELECT status FROM reader_text_artifacts")[0], "accepted")
        jobs = JobStore(self.store)
        job = jobs.ensure("extract", {}, {"test": "forged"})
        jobs.claim(job["job_id"], "test")
        with self.assertRaisesRegex(Exception, "semantic_job_unsent_proof_required"):
            jobs.defer(job["job_id"], "test", ProviderError("provider_failed", outcome_unknown=False))


class FullPathSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = SemanticStore(self.root / "knowledge.sqlite", "schedule-path")

    def runtime(self, *, text=None, embedding=None, index=None, text_cap=2, embedding_cap=4):
        text = text or FakeText(noise=True)
        text.route = ProviderRoute("synthetic", uuid.uuid4().hex, "text")
        roles = ProviderRoles(text, FakeDecision(), embedding or FakeEmbedding(), index or LocalCosineIndex())
        return SemanticRuntime(self.store, roles, self.root / "vault/WGO", config={
            "semantic_provider_limits": {"text": {"max_inflight": text_cap},
                                         "embedding": {"max_inflight": embedding_cap}}})

    def test_rebuild_parallel_extracts_independent_batches_and_adopts_overlap(self):
        capture(self.store, "a", ["嗯"])
        capture(self.store, "b", ["好"])
        self.store.capture_batch("overlap", [{"source_id": "a-0", "logical_shard_id": "synthetic-shard",
            "physical_generation": "synthetic-generation", "source_cursor_token": "1", "source_sent_at": 10,
            "text": "嗯", "payload": {}}], source_namespace="synthetic", chat_id="group-a",
            inventory_digest="synthetic-inventory", inventory_revision=1, input_cursors={},
            output_cursors={"synthetic-shard": "1"}, source_eof=True)
        provider, probe = FakeText(noise=True), ConcurrentProbe(2)
        original = provider.generate
        def generate(prompt, **kwargs):
            return probe.call(lambda: original(prompt, **kwargs))
        provider.generate = generate
        runtime = self.runtime(text=provider)
        result = RebuildCoordinator._process_all(None, runtime, run_id="synthetic-parallel")
        self.assertTrue(result["index"]["complete"])
        self.assertEqual(probe.peak, 2)
        self.assertEqual(len(provider.calls), 2)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_core_jobs").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM processing_receipts").fetchone()[0], 3)

    def test_extract_failure_never_acks_a_later_completed_batch(self):
        capture(self.store, "a", ["failure"])
        capture(self.store, "b", ["success"])
        capture(self.store, "c", ["not admitted"])
        provider, barrier = FakeText(noise=True), threading.Barrier(2)
        original = provider.generate
        admitted = []
        def generate(prompt, **kwargs):
            sources = json.loads(prompt.rsplit("\n", 1)[-1])
            core = [span["excerpt"] for source in sources if source["core"] for span in source["spans"]]
            context = [span["excerpt"] for source in sources if not source["core"] for span in source["spans"]]
            admitted.append((core, context))
            barrier.wait(5)
            if "failure" in core:
                raise ProviderError("provider_timeout", retryable=True, outcome_unknown=True)
            return original(prompt, **kwargs)
        provider.generate = generate
        runtime = self.runtime(text=provider)
        with self.assertRaisesRegex(ProviderError, "provider_timeout"):
            RebuildCoordinator._process_all(None, runtime, run_id="failed-extraction")
        self.assertEqual({text for core, _ in admitted for text in core}, {"failure", "success"})
        self.assertIn((["success"], ["failure"]), admitted)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM capture_batches").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM processing_receipts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_dispositions").fetchone()[0], 1)
        accepted_calls = len(provider.calls)
        runtime.engine.extract_batch("b")
        self.assertEqual(len(provider.calls), accepted_calls)

    def test_full_index_over_100_documents_uses_one_snapshot_and_parallel_embeddings(self):
        capture(self.store, "large", ["独立片段 " + str(n) for n in range(205)])
        embedding, probe = FakeEmbedding(), ConcurrentProbe(4)
        original = embedding.embed
        embedding.embed = lambda texts, **kwargs: probe.call(lambda: original(texts, **kwargs))
        runtime = self.runtime(embedding=embedding)
        with patch.object(runtime.retrieval, "documents", wraps=runtime.retrieval.documents) as documents, \
             patch.object(runtime.retrieval, "_embeddings", wraps=runtime.retrieval._embeddings) as rows:
            result = runtime.index_all()
        self.assertTrue(result["complete"])
        self.assertEqual(documents.call_count, 1)
        self.assertEqual(rows.call_count, 1)
        self.assertEqual(probe.peak, 4)
        self.assertEqual(len(embedding.calls), 205)
        self.assertEqual(len(runtime.retrieval._confirmed_ids()), 205)

    def test_async_pending_guard_remains_serial_while_preparation_can_continue(self):
        capture(self.store, "large", ["异步片段 " + str(n) for n in range(105)])
        index = AsyncIndex()
        runtime = self.runtime(index=index)
        self.assertFalse(runtime.index_all()["complete"])
        self.assertEqual(index.calls, 1)
        self.assertEqual(len(runtime.retrieval._embeddings()), 105)
        index.ready = True
        # A newly accepted asynchronous second mutation needs its own later
        # confirmation; index_all deliberately does not poll it in a tight loop.
        self.assertFalse(runtime.index_all()["complete"])
        self.assertEqual(index.calls, 2)
        self.assertTrue(runtime.index_all()["complete"])
        self.assertEqual(index.calls, 2)

    def test_stale_index_snapshot_rejects_without_send_and_accepted_embedding_survives_crash(self):
        capture(self.store, "a", ["片段 A", "片段 B", "片段 C", "片段 D"])
        embedding = FakeEmbedding()
        runtime = self.runtime(embedding=embedding)
        prepared = runtime.retrieval.prepare_index()
        capture(self.store, "b", ["新增片段"])
        with self.assertRaisesRegex(SemanticValidationError, "search_snapshot_changed"):
            runtime.retrieval.index_pending(preparation=prepared)
        self.assertFalse(embedding.calls)
        original = runtime.retrieval._assert_preparation
        def crash(prepared, db=None):
            if db is not None:
                raise RuntimeError("synthetic interruption after accepted embedding")
            return original(prepared)
        with patch.object(runtime.retrieval, "_assert_preparation", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                runtime.index_all()
        accepted = len(embedding.calls)
        self.assertGreater(accepted, 0)
        self.assertFalse(runtime.retrieval._embeddings())
        self.assertTrue(runtime.index_all()["complete"])
        self.assertEqual(len(embedding.calls), 5)

    def test_body_preparation_parallel_outside_locks_and_replays_after_publish_crash(self):
        capture(self.store, "stories", ["花与散步 " + str(n) for n in range(4)])
        text, probe = FakeText(), ConcurrentProbe(2)
        original = text.generate
        runtime = self.runtime(text=text)
        def generate(prompt, **kwargs):
            if not prompt.startswith("{"):
                result = original(prompt, **kwargs)
                data = json.loads(prompt.rsplit("\n", 1)[-1])
                if isinstance(data, dict) and "observations" in data and "new_item_ids" in data:
                    payload = json.loads(result.text)
                    payload["actions"] = [{"action_key": kwargs["request_id"] + ":" + str(n),
                        "type": "CREATE_ITEM", "item_id": data["new_item_ids"][n], "kind": "story",
                        "title": ob["title"], "identity_scope": {"scene": ob["title"]},
                        "observation_ids": [ob["observation_id"]]} for n, ob in enumerate(data["observations"])]
                    return TextResult(json.dumps(payload), text.route)
                return result
            def body():
                self.assertFalse(getattr(runtime.publisher._lock_local, "held", False))
                # Provider work may independently obtain a write transaction;
                # the caller has not held one open across this invocation.
                with self.store.transaction():
                    pass
                snapshot = json.loads(prompt)["snapshot"]
                result = {"item_id": snapshot["item_id"], "content_revision": snapshot["content_revision"], "blocks": [
                    {"id": "paragraph-" + unit["id"], "role": unit["role"], "text": unit["text"],
                     "unit_ids": [unit["id"]], "evidence_ids": unit["evidence_ids"],
                     "resource_ids": unit["resource_ids"], "primary_home": snapshot["item_id"], "unit_statuses": {}}
                    for unit in snapshot["units"]]}
                return TextResult(json.dumps(result), text.route)
            return probe.call(body)
        text.generate = generate
        runtime.process("stories")
        self.assertEqual(len(self.store.list_items()), 4)
        with patch.object(runtime.publisher, "publish_prepared_item", side_effect=RuntimeError("publication interrupted")):
            with self.assertRaisesRegex(RuntimeError, "publication interrupted"):
                runtime.publish_all()
        self.assertEqual(probe.peak, 2)
        self.assertEqual(probe.count, 4)
        with patch.object(text, "generate", side_effect=AssertionError("accepted Body must be reused")):
            self.assertTrue(runtime.publish_all()["complete"])
        self.assertEqual(runtime.ledger.one("SELECT COUNT(*) FROM reader_text_artifacts WHERE status='accepted'")[0], 4)


if __name__ == "__main__":
    unittest.main()
