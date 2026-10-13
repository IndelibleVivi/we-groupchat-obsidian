from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from ai.cloudflare_provider import CloudflareClient
from ai.semantic_provider import EmbeddingProfile, ProviderError
from ai.vector_index import CloudflareVectorizeIndex, IndexReceipt, LocalCosineIndex, VectorRecord
from core.provider_usage import ProviderUsageStore
from tests.test_provider_usage import DAY, NOW
from tests.test_semantic_providers import FakeTransport, envelope


class VectorizeFixture:
    def __init__(self):
        self.rows = {}
        self.mutation = str(uuid.uuid4())
        self.processed = str(uuid.uuid4())
        self.readback = None
        self.query_rows = None
        self.dimensions = 3
        self.metric = "cosine"

    def __call__(self, call):
        method, url, headers, body, _timeout = call
        if url.endswith("/synthetic"):
            return envelope({"config": {"dimensions": self.dimensions, "metric": self.metric}})
        if "/upsert?" in url:
            if 'name="vectors"' not in body.decode() or "unparsable-behavior=error" not in url:
                raise AssertionError("incorrect multipart or parse policy")
            lines = body.split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0].splitlines()
            self.rows.update({row["id"]: row for row in map(json.loads, lines)})
            return envelope({"mutationId": self.mutation})
        if url.endswith("/info"):
            return envelope({"dimensions": self.dimensions, "processedUpToMutation": self.processed})
        if url.endswith("/get_by_ids"):
            ids = json.loads(body)["ids"]
            rows = [self.rows[key] for key in reversed(ids) if key in self.rows]
            return envelope(rows if self.readback is None else self.readback)
        if url.endswith("/query"):
            payload = json.loads(body)
            rows = [{"id": row["id"], "namespace": row["namespace"], "metadata": row["metadata"], "score": .9}
                    for row in self.rows.values() if row["namespace"] == payload["namespace"]]
            rows = rows if self.query_rows is None else self.query_rows
            return envelope({"count": len(rows), "matches": rows})
        raise AssertionError("unexpected endpoint")


class VectorIndexTests(unittest.TestCase):
    def setUp(self):
        self.profile = EmbeddingProfile("synthetic", "fixture", 3, normalization="none")
        self.record = VectorRecord(str(uuid.uuid4()), (1., .3, 0.))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ProviderUsageStore(Path(self.tmp.name) / "usage.sqlite3", clock=lambda: NOW)
        self.fixture = VectorizeFixture()
        self.transport = FakeTransport(self.fixture)
        self.client = CloudflareClient("a" * 32, "synthetic-token", self.store, transport=self.transport)
        self.store.confirm_budget(self.client.vectorize_account_key, utc_day=DAY, daily_limit_units=10**9,
                                  external_usage_units=0, confirmation_id="synthetic-budget", valid_until=NOW + 3600)
        self.remote = CloudflareVectorizeIndex(self.client, "synthetic", pending_receipt_check=lambda _identity: False)

    def upsert(self, records=None, generation="g1", request="upsert"):
        return self.remote.upsert(records or [self.record], profile=self.profile, generation_id=generation, request_id=request)

    def confirmed(self):
        pending = self.upsert()
        self.fixture.processed = self.fixture.mutation
        return self.remote.confirm(pending, profile=self.profile, request_id="confirm")

    def test_local_cosine_results_are_exact_and_isolate_profile_and_generation(self):
        index = LocalCosineIndex()
        row_b = VectorRecord(str(uuid.uuid4()), (0., 1., 0.))
        receipt = index.upsert([self.record, row_b], profile=self.profile, generation_id="g1", request_id="local")
        self.assertTrue(receipt.indexed)
        result = index.query((1., 0., 0.), profile=self.profile, generation_id="g1", request_id="q")
        self.assertEqual(result[0].id, self.record.id)
        self.assertAlmostEqual(result[0].score, 1 / (1.09 ** .5))
        self.assertEqual(index.query((1., 0., 0.), profile=self.profile, generation_id="g2", request_id="q2"), ())
        changed = replace(self.profile, query_template="other: {text}")
        self.assertEqual(index.query((1., 0., 0.), profile=changed, generation_id="g1", request_id="q3"), ())
        with self.assertRaisesRegex(ProviderError, "vector_receipt_binding_mismatch"):
            index.confirm(receipt, profile=changed, request_id="wrong-profile")

    def test_local_same_opaque_id_cannot_change_vector_in_generation(self):
        index = LocalCosineIndex()
        index.upsert([self.record], profile=self.profile, generation_id="g1", request_id="first")
        with self.assertRaisesRegex(ProviderError, "vector_immutable_id_conflict"):
            index.upsert([replace(self.record, values=(0., 1., 0.))], profile=self.profile, generation_id="g1", request_id="second")

    def test_vectorize_mutation_acceptance_is_not_indexed(self):
        pending = self.upsert()
        self.assertFalse(pending.indexed)
        waiting = self.remote.confirm(pending, profile=self.profile, request_id="wait1")
        self.assertFalse(waiting.indexed)
        self.assertFalse(any(call[1].endswith("/get_by_ids") for call in self.transport.calls))
        self.fixture.processed = pending.mutation_id
        self.fixture.readback = []
        waiting = self.remote.confirm(waiting, profile=self.profile, request_id="wait2")
        self.assertFalse(waiting.indexed)
        self.fixture.readback = None
        complete = self.remote.confirm(waiting, profile=self.profile, request_id="wait3")
        self.assertTrue(complete.indexed)

    def test_readback_order_is_matched_by_id_and_float32_is_canonicalized(self):
        second = VectorRecord(str(uuid.uuid4()), (0., .2, 1.))
        pending = self.upsert([self.record, second])
        self.assertNotEqual(pending.records[0].values[1], self.record.values[1])
        self.fixture.processed = pending.mutation_id
        complete = self.remote.confirm(pending, profile=self.profile, request_id="confirm-reversed")
        self.assertTrue(complete.indexed)
        self.assertEqual(IndexReceipt.from_dict(json.loads(json.dumps(complete.to_dict()))), complete)

    def test_generation_uses_distinct_namespace_and_remote_id_without_raw_identifiers(self):
        with self.assertRaisesRegex(ProviderError, "vector_id_not_opaque"):
            self.upsert([VectorRecord("chat-local-source-1", (1., 0., 0.))], request="unsafe")
        first = self.upsert(generation="g1", request="g1-write")
        self.fixture.processed = first.mutation_id
        self.remote.confirm(first, profile=self.profile, request_id="g1-confirm")
        self.upsert(generation="g2", request="g2-write")
        rows = list(self.fixture.rows.values())
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        self.assertNotEqual(rows[0]["namespace"], rows[1]["namespace"])
        for row in rows:
            self.assertEqual(set(row["metadata"]), {"profile_id", "generation_id"})
            self.assertEqual(len(row["metadata"]["generation_id"]), 64)
            self.assertNotIn(self.record.id, json.dumps(row))

    def test_query_refuses_unconfirmed_or_wrong_profile_metadata(self):
        pending = self.upsert()
        with self.assertRaisesRegex(ProviderError, "vector_query_unconfirmed"):
            self.remote.query((1., 0., 0.), profile=self.profile, generation_id="g1", request_id="early-query")
        self.fixture.processed = pending.mutation_id
        receipt = self.remote.confirm(pending, profile=self.profile, request_id="ready")
        result = self.remote.query((1., 0., 0.), profile=self.profile, generation_id="g1", request_id="query")
        self.assertEqual(result[0].id, self.record.id)
        row = next(iter(self.fixture.rows.values()))
        row["metadata"]["profile_id"] = "b" * 64
        with self.assertRaisesRegex(ProviderError, "vector_query_binding_mismatch"):
            self.remote.query((1., 0., 0.), profile=self.profile, generation_id="g1", request_id="wrong-metadata")
        self.assertTrue(receipt.indexed)

    def test_readback_rejects_wrong_vectors_even_after_marker_matches(self):
        pending = self.upsert()
        self.fixture.processed = pending.mutation_id
        next(iter(self.fixture.rows.values()))["values"] = [0., 1., 0.]
        with self.assertRaisesRegex(ProviderError, "vector_readback_values_mismatch"):
            self.remote.confirm(pending, profile=self.profile, request_id="wrong-values")

    def test_restore_receipt_is_bound_to_actual_index_and_profile(self):
        receipt = self.confirmed()
        fresh = CloudflareVectorizeIndex(self.client, "synthetic")
        fresh.restore_receipt(IndexReceipt.from_dict(receipt.to_dict()))
        result = fresh.query((1., 0., 0.), profile=self.profile, generation_id="g1", request_id="restored-query")
        self.assertEqual(result[0].id, self.record.id)
        other = CloudflareVectorizeIndex(self.client, "other-index")
        with self.assertRaisesRegex(ProviderError, "vector_receipt_binding_mismatch"):
            other.restore_receipt(receipt)

    def test_remote_metric_is_checked_before_any_upsert(self):
        self.fixture.metric = "euclidean"
        with self.assertRaisesRegex(ProviderError, "vector_index_profile_mismatch"):
            self.upsert()
        self.assertFalse(any("/upsert" in call[1] for call in self.transport.calls))

    def test_pending_guard_requires_durable_check_and_prevents_second_mutation(self):
        missing = CloudflareVectorizeIndex(self.client, "synthetic")
        with self.assertRaisesRegex(ProviderError, "vector_pending_guard_required"):
            missing.upsert([self.record], profile=self.profile, generation_id="g1", request_id="unguarded")
        self.remote.pending_receipt_check = lambda _identity: True
        with self.assertRaisesRegex(ProviderError, "vector_mutation_pending"):
            self.upsert(request="durable-pending")
        self.assertEqual(self.transport.calls, [])
        self.remote.pending_receipt_check = lambda _identity: False
        pending = self.upsert()
        with self.assertRaisesRegex(ProviderError, "vector_mutation_pending"):
            self.upsert(generation="g2", request="second")
        fresh = CloudflareVectorizeIndex(self.client, "synthetic", pending_receipt_check=lambda _identity: False)
        fresh.restore_receipt(pending)
        with self.assertRaisesRegex(ProviderError, "vector_mutation_pending"):
            fresh.upsert([self.record], profile=self.profile, generation_id="g2", request_id="restored-second")


if __name__ == "__main__":
    unittest.main()
