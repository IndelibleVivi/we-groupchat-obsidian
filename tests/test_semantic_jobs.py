"""Synthetic durable job, fencing, accepted-artifact and unknown-outcome tests."""
from contextlib import contextmanager
from copy import deepcopy
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.semantic.contracts import SemanticStoreError, digest
from core.semantic.jobs import JobStore, RECONCILIATION_SCHEMA, accepted_artifact_receipt
from core.semantic.store import SemanticStore


def validate_count(value):
    if set(value) != {"count"} or type(value["count"]) is not int or value["count"] < 0:
        raise SemanticStoreError("synthetic_artifact_invalid")
    return value


def _race_claim(path, job_id, identity, ready, start, results):
    store = SemanticStore(path, "synthetic-generation", now_func=lambda: 100)
    jobs = JobStore(store)
    ready.put(True)
    start.wait(10)
    result = jobs.claim(job_id, identity)
    results.put(None if result is None else result["worker_id"])


class SemanticJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 100
        self.path = Path(self.tmp.name).resolve() / "generation.sqlite3"
        self.store = SemanticStore(self.path, "synthetic-generation", now_func=lambda: self.now)
        self.jobs = JobStore(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def ensure(self, **kwargs):
        return self.jobs.ensure(kwargs.get("kind", "extract"), kwargs.get("inputs", {"source": "synthetic", "version": 1}),
                                kwargs.get("versions", {"prompt": "v1", "profile": "fixture"}))

    def accept(self, job=None, worker="worker-a", count=1):
        job = job or self.ensure()
        self.assertIsNotNone(self.jobs.claim(job["job_id"], worker))
        return job, self.jobs.accept(job["job_id"], worker, {"count": count}, validate=validate_count)

    def unknown(self):
        job = self.ensure()
        claim = self.jobs.claim(job["job_id"], "worker-a")
        self.jobs.fail(job["job_id"], "worker-a", "provider_timeout", unknown=True)
        return job, claim

    def receipt(self, job, claim, *, outcome="not_sent", artifact=None):
        return {
            "schema": RECONCILIATION_SCHEMA, "receipt_id": "synthetic-provider-reconciliation",
            "generation_id": self.store.generation_id, "job_id": job["job_id"], "job_key": job["job_key"],
            "attempt": claim["attempt"], "worker_id": claim["worker_id"],
            "request_id": "synthetic-provider-request", "provider_receipt_id": "synthetic-provider-receipt",
            "outcome": outcome, "artifact_hash": digest(artifact) if artifact is not None else None,
        }

    @staticmethod
    def evidence_validator(expected):
        def validate(value):
            if value != expected:
                raise SemanticStoreError("synthetic_evidence_mismatch")
            return deepcopy(expected)
        return validate

    def rows(self, table):
        with self.store.transaction() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM " + table)]

    def test_key_binds_all_inputs_versions_kind_and_generation(self):
        first = self.ensure()
        same = self.jobs.ensure("extract", {"version": 1, "source": "synthetic"}, {"profile": "fixture", "prompt": "v1"})
        self.assertEqual(first["job_id"], same["job_id"])
        self.assertEqual(len(self.rows("jobs")), 1)
        others = [self.ensure(kind="propose"), self.ensure(inputs={"source": "synthetic", "version": 2}),
                  self.ensure(versions={"prompt": "v2", "profile": "fixture"})]
        self.assertEqual(len({first["job_id"], *(job["job_id"] for job in others)}), 4)
        other_store = SemanticStore(Path(self.tmp.name).resolve() / "other.sqlite3", "other-generation")
        other = JobStore(other_store).ensure("extract", first["inputs"], first["versions"])
        self.assertNotEqual(other["job_key"], first["job_key"])

    def test_inputs_are_copied_and_invalid_json_cannot_create_a_job(self):
        inputs = {"members": [{"source": "synthetic"}], "views": ("a", "b")}
        job = self.ensure(inputs=inputs)
        inputs["members"][0]["source"] = "changed"
        self.assertEqual(job["inputs"]["members"][0]["source"], "synthetic")
        self.assertEqual(job["inputs"]["views"], ["a", "b"])
        for value in ({1: "coerced-key"}, {"invalid": float("nan")}, {"invalid": object()}, "model text"):
            with self.subTest(value=value), self.assertRaises(SemanticStoreError):
                self.ensure(inputs=value)
        self.assertEqual(len(self.rows("jobs")), 1)

    def test_one_live_claim_and_known_failure_allows_new_attempt(self):
        job = self.ensure()
        first = self.jobs.claim(job["job_id"], "worker-a")
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(first["lease_until"], 400)
        self.assertIsNone(self.jobs.claim(job["job_id"], "worker-a"))
        self.assertIsNone(JobStore(self.store).claim(job["job_id"], "worker-b"))
        failed = self.jobs.fail(job["job_id"], "worker-a", "provider_rejected")
        self.assertEqual(failed["state"], "failed")
        second = self.jobs.claim(job["job_id"], "worker-a")
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(self.rows("job_attempts")[0]["error_code"], "provider_rejected")

    def test_restart_reclaims_expired_lease_and_fences_old_same_named_worker(self):
        job = self.ensure()
        self.jobs.claim(job["job_id"], "worker-a", lease_seconds=1)
        self.now = 102
        # The same instance cannot erase a still-running local callback.
        self.assertIsNone(self.jobs.claim(job["job_id"], "worker-a"))
        restarted = JobStore(self.store)
        fresh = restarted.claim(job["job_id"], "worker-a")
        self.assertEqual(fresh["attempt"], 2)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_lease_lost"):
            self.jobs.accept(job["job_id"], "worker-a", {"count": 1}, validate=validate_count)
        self.assertIsNone(restarted.accepted(job["job_id"]))
        restarted.accept(job["job_id"], "worker-a", {"count": 2}, validate=validate_count)
        self.assertEqual(self.jobs.accepted(job["job_id"]), {"count": 2})
        self.assertEqual(self.rows("job_attempts")[0]["error_code"], "semantic_job_lease_expired")

    def test_valid_artifact_survives_restart_without_reclaim_or_overwrite(self):
        job, accepted = self.accept()
        restarted = JobStore(SemanticStore(self.path, "synthetic-generation", now_func=lambda: self.now))
        self.assertEqual(restarted.accepted(job["job_id"]), {"count": 1})
        self.assertIsNone(restarted.claim(job["job_id"], "worker-b"))
        self.assertEqual(restarted.accept(job["job_id"], "worker-b", accepted, validate=validate_count), accepted)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_artifact_conflict"):
            restarted.accept(job["job_id"], "worker-b", {"count": 2}, validate=validate_count)
        accepted["count"] = 3
        self.assertEqual(restarted.accepted(job["job_id"]), {"count": 1})
        self.assertEqual(len(self.rows("accepted_job_artifacts")), 1)

    def test_accept_requires_explicit_validator_and_owned_attempt(self):
        job = self.ensure()
        self.jobs.claim(job["job_id"], "worker-a")
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_validator_required"):
            self.jobs.accept(job["job_id"], "worker-a", {"count": 1})
        with self.assertRaisesRegex(SemanticStoreError, "synthetic_artifact_invalid"):
            self.jobs.accept(job["job_id"], "worker-a", {"count": True}, validate=validate_count)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_object_required"):
            self.jobs.accept(job["job_id"], "worker-a", '{"count":1}', validate=validate_count)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_claim_required"):
            self.jobs.accept(job["job_id"], "worker-b", {"count": 1}, validate=validate_count)
        self.assertIsNone(self.jobs.accepted(job["job_id"]))

    def test_accept_is_atomic_with_state_and_attempt_completion(self):
        job = self.ensure()
        self.jobs.claim(job["job_id"], "worker-a")
        original = self.store.transaction
        @contextmanager
        def interrupted():
            with original() as conn:
                yield conn
                raise RuntimeError("synthetic interruption before commit")
        with patch.object(self.store, "transaction", interrupted), self.assertRaises(RuntimeError):
            self.jobs.accept(job["job_id"], "worker-a", {"count": 1}, validate=validate_count)
        self.assertIsNone(self.jobs.accepted(job["job_id"]))
        self.assertEqual(self.rows("jobs")[0]["state"], "running")
        self.assertIsNone(self.rows("job_attempts")[0]["finished_at"])
        self.assertEqual(self.jobs.accept(job["job_id"], "worker-a", {"count": 1}, validate=validate_count), {"count": 1})

    def test_unknown_outcome_never_reclaims_on_expiry_or_restart(self):
        job, _claim = self.unknown()
        self.now = 100000
        restarted = JobStore(self.store)
        self.assertIsNone(restarted.claim(job["job_id"], "worker-b"))
        self.assertIsNone(restarted.accepted(job["job_id"]))
        self.assertEqual(self.rows("jobs")[0]["state"], "unknown")
        self.assertEqual(self.rows("job_attempts")[0]["outcome_unknown"], 1)

    def test_exact_not_sent_reconciliation_permits_retry_and_retains_evidence(self):
        job, claim = self.unknown()
        receipt = self.receipt(job, claim)
        validate = self.evidence_validator(receipt)
        restored = self.jobs.reconcile_unknown(job["job_id"], receipt, validate=validate)
        self.assertEqual(restored["state"], "pending")
        next_claim = self.jobs.claim(job["job_id"], "worker-b")
        self.assertEqual(next_claim["attempt"], 2)
        self.jobs.accept(job["job_id"], "worker-b", {"count": 1}, validate=validate_count)
        self.jobs.finish(job["job_id"], accepted_artifact_receipt(job["job_id"], {"count": 1}))
        self.assertEqual(self.jobs.reconcile_unknown(job["job_id"], receipt, validate=validate)["state"], "done")
        self.assertEqual(len(self.rows("job_reconciliations")), 1)
        self.assertEqual(self.rows("job_attempts")[0]["outcome_unknown"], 1)

    def test_completed_reconciliation_accepts_verified_artifact_without_replay(self):
        job, claim = self.unknown()
        artifact = {"count": 7}
        receipt = self.receipt(job, claim, outcome="completed", artifact=artifact)
        self.jobs.reconcile_unknown(job["job_id"], receipt, validate=self.evidence_validator(receipt),
                                    artifact=artifact, validate_artifact=validate_count)
        self.assertEqual(self.jobs.accepted(job["job_id"]), artifact)
        self.assertIsNone(JobStore(self.store).claim(job["job_id"], "worker-b"))
        self.assertEqual(len(self.rows("job_attempts")), 1)

    def test_reconciliation_rejects_wrong_attempt_generation_evidence_and_artifact(self):
        job, claim = self.unknown()
        valid = self.receipt(job, claim)
        for key, value in (("generation_id", "other"), ("job_id", "other"), ("job_key", "other"),
                           ("attempt", 2), ("attempt", True), ("worker_id", "other"),
                           ("provider_receipt_id", ""), ("artifact_hash", "invented")):
            wrong = {**valid, key: value}
            with self.subTest(key=key), self.assertRaises(SemanticStoreError):
                self.jobs.reconcile_unknown(job["job_id"], wrong, validate=self.evidence_validator(wrong))
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_validator_required"):
            self.jobs.reconcile_unknown(job["job_id"], valid)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_object_required"):
            self.jobs.reconcile_unknown(job["job_id"], valid, validate=lambda _: True)
        completed = self.receipt(job, claim, outcome="completed", artifact={"count": 1})
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_reconciliation_invalid"):
            self.jobs.reconcile_unknown(job["job_id"], completed, validate=self.evidence_validator(completed),
                                        artifact={"count": 2}, validate_artifact=validate_count)
        self.assertEqual(self.rows("job_reconciliations"), [])
        self.assertEqual(self.rows("jobs")[0]["state"], "unknown")

    def test_finish_requires_accepted_artifact_and_real_bound_receipt(self):
        job = self.ensure()
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_artifact_required"):
            self.jobs.finish(job["job_id"], "invented")
        self.accept(job)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_finish_receipt_invalid"):
            self.jobs.finish(job["job_id"], "invented")
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO canonical_transactions VALUES(?,?,?,?,?)", ("tx", job["job_id"], "[]", "[]", self.now))
        self.assertEqual(self.jobs.finish(job["job_id"], "tx")["state"], "done")
        self.assertEqual(self.jobs.finish(job["job_id"], "tx")["receipt_id"], "tx")
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_finish_receipt_conflict"):
            self.jobs.finish(job["job_id"], accepted_artifact_receipt(job["job_id"], {"count": 1}))

    def test_finish_cannot_adopt_an_unrelated_jobs_canonical_receipt(self):
        job, _artifact = self.accept()
        other = self.ensure(kind="other")
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO canonical_transactions VALUES(?,?,?,?,?)", ("other-tx", other["job_id"], "[]", "[]", self.now))
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_finish_receipt_invalid"):
            self.jobs.finish(job["job_id"], "other-tx")
        self.assertEqual(self.rows("jobs")[0]["state"], "accepted")

    def test_reconciliation_receipt_identity_cannot_be_rebound(self):
        job, claim = self.unknown()
        receipt = self.receipt(job, claim)
        self.jobs.reconcile_unknown(job["job_id"], receipt, validate=self.evidence_validator(receipt))
        other = self.ensure(inputs={"source": "other"})
        claim2 = self.jobs.claim(other["job_id"], "worker-a")
        self.jobs.fail(other["job_id"], "worker-a", "provider_timeout", unknown=True)
        reused = self.receipt(other, claim2)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_reconciliation_conflict"):
            self.jobs.reconcile_unknown(other["job_id"], reused, validate=self.evidence_validator(reused))
        self.assertIsNone(self.jobs.claim(other["job_id"], "worker-b"))
        self.assertEqual(len(self.rows("job_reconciliations")), 1)

    def test_artifact_only_compute_has_explicit_finish_proof(self):
        job, artifact = self.accept(self.ensure(kind="query_embedding"))
        proof = accepted_artifact_receipt(job["job_id"], artifact)
        self.assertEqual(self.jobs.finish(job["job_id"], proof)["receipt_id"], proof)
        self.assertEqual(self.store.read_revision(), 0)

    def test_job_writes_do_not_advance_canonical_revision(self):
        job, artifact = self.accept()
        self.jobs.finish(job["job_id"], accepted_artifact_receipt(job["job_id"], artifact))
        self.assertEqual(self.store.read_revision(), 0)
        self.assertEqual(self.rows("canonical_transactions"), [])

    def test_corrupt_accepted_artifact_is_not_returned_or_overwritten(self):
        job, _artifact = self.accept()
        with self.store.transaction() as conn:
            conn.execute("UPDATE accepted_job_artifacts SET artifact_json=? WHERE job_id=?", ('{"count":2}', job["job_id"]))
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_artifact_corrupt"):
            self.jobs.accepted(job["job_id"])
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_artifact_corrupt"):
            self.jobs.claim(job["job_id"], "worker-b")

    def test_invalid_lease_and_error_metadata_fail_before_mutation(self):
        job = self.ensure()
        for value in (True, 0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(SemanticStoreError):
                self.jobs.claim(job["job_id"], "worker-a", lease_seconds=value)
        self.assertEqual(self.rows("job_attempts"), [])
        self.jobs.claim(job["job_id"], "worker-a")
        for code, unknown in (("raw provider body", False), ("timeout", 1)):
            with self.subTest(code=code), self.assertRaises(SemanticStoreError):
                self.jobs.fail(job["job_id"], "worker-a", code, unknown=unknown)
        self.assertEqual(self.rows("jobs")[0]["state"], "running")

    def test_two_processes_claim_once(self):
        job = self.ensure()
        context = multiprocessing.get_context("spawn")
        ready, results, start = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=_race_claim, args=(str(self.path), job["job_id"], worker, ready, start, results))
                     for worker in ("worker-a", "worker-b")]
        for process in processes:
            process.start()
        try:
            self.assertEqual([ready.get(timeout=10) for _ in processes], [True, True])
            start.set()
            outcomes = [results.get(timeout=10) for _ in processes]
            self.assertEqual(outcomes.count(None), 1)
            self.assertEqual(sum(value in {"worker-a", "worker-b"} for value in outcomes), 1)
        finally:
            start.set()
            for process in processes:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
        self.assertTrue(all(process.exitcode == 0 for process in processes))
        self.assertEqual(len(self.rows("job_attempts")), 1)


if __name__ == "__main__":
    unittest.main()
