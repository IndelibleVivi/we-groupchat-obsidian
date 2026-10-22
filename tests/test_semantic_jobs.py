"""Synthetic durable job, fencing, accepted-artifact and unknown-outcome tests."""
from contextlib import contextmanager
from copy import deepcopy
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.semantic.contracts import SemanticStoreError, digest
from core.semantic.jobs import (
    JobStore, OPERATOR_RETRY_ACTION, OPERATOR_RETRY_AUTHORIZATION_SCHEMA,
    RECONCILIATION_SCHEMA, accepted_artifact_receipt,
)
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

    def unknown_extract_page(self, index=0):
        self.now += 1
        job = self.ensure(
            kind="extract_page",
            inputs={"parent_job_id": "synthetic-parent", "page": index},
            versions={"prompt": "synthetic-page-v1", "profile": "fixture"},
        )
        worker = f"synthetic-page-worker-{index}"
        claim = self.jobs.claim(job["job_id"], worker)
        self.jobs.fail(job["job_id"], worker, "text_provider_failed", unknown=True)
        return job, claim

    def authorization(self, pairs, *, receipt_id="synthetic-operator-retry"):
        jobs = []
        for job, claim in pairs:
            attempt = self.attempt(job["job_id"])
            self.assertEqual(attempt["attempt"], claim["attempt"])
            jobs.append({
                "job_id": job["job_id"],
                "job_key": job["job_key"],
                "attempt": attempt["attempt"],
                "worker_id": attempt["worker_id"],
                "finished_at": attempt["finished_at"],
            })
        return {
            "schema": OPERATOR_RETRY_AUTHORIZATION_SCHEMA,
            "receipt_id": receipt_id,
            "action": OPERATOR_RETRY_ACTION,
            "generation_id": self.store.generation_id,
            "jobs": jobs,
        }

    def attempt(self, job_id):
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return dict(row)

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

    def test_operator_authorization_is_atomic_and_preserves_unknown_attempt_evidence(self):
        pairs = [self.unknown_extract_page(0), self.unknown_extract_page(1)]
        receipt = self.authorization(pairs)
        old_jobs = {row["job_id"]: row for row in self.rows("jobs")}
        old_attempts = {row["job_id"]: row for row in self.rows("job_attempts")}
        original = self.store.transaction

        @contextmanager
        def interrupted():
            with original() as conn:
                yield conn
                raise RuntimeError("synthetic interruption before commit")

        with patch.object(self.store, "transaction", interrupted), self.assertRaises(RuntimeError):
            self.jobs.authorize_unknown_retries(receipt, validate=self.evidence_validator(receipt))
        self.assertEqual(self.rows("job_operator_retry_authorizations"), [])
        self.assertEqual({row["state"] for row in self.rows("jobs")}, {"unknown"})

        authorized = self.jobs.authorize_unknown_retries(
            receipt, validate=self.evidence_validator(receipt))
        self.assertEqual([job["state"] for job in authorized], ["pending", "pending"])
        current_jobs = {row["job_id"]: row for row in self.rows("jobs")}
        for job, _claim in pairs:
            before, after = old_jobs[job["job_id"]], current_jobs[job["job_id"]]
            self.assertEqual(after["job_id"], before["job_id"])
            self.assertEqual(after["job_key"], before["job_key"])
            self.assertEqual(after["inputs_json"], before["inputs_json"])
            self.assertEqual(after["versions_json"], before["versions_json"])
            self.assertEqual(after["attempt"], before["attempt"])
            self.assertEqual((after["state"], after["worker_id"], after["lease_until"]),
                             ("pending", None, None))
            self.assertEqual(old_attempts[job["job_id"]], self.attempt(job["job_id"]))

        self.assertEqual(len(self.rows("job_operator_retry_authorizations")), 2)
        for job, _claim in pairs:
            next_claim = self.jobs.claim(job["job_id"], "retry-worker")
            self.assertEqual(next_claim["attempt"], 2)
            artifact = self.jobs.accept(job["job_id"], "retry-worker", {"count": 2},
                                        validate=validate_count)
            receipt_id = accepted_artifact_receipt(job["job_id"], artifact)
            self.assertEqual(self.jobs.finish(job["job_id"], receipt_id)["state"], "done")
        self.assertEqual({row["state"] for row in self.rows("jobs")}, {"done"})

    def test_operator_authorization_is_idempotent_and_future_unknown_stays_blocked(self):
        job, claim = self.unknown_extract_page()
        receipt = self.authorization([(job, claim)])
        validate = self.evidence_validator(receipt)
        repeated = self.jobs.authorize_unknown_retries(receipt, validate=validate)
        self.assertEqual([(entry["state"], entry["attempt"]) for entry in repeated],
                         [("pending", 1)])
        second = self.jobs.claim(job["job_id"], "retry-worker")
        self.assertEqual(second["attempt"], 2)
        self.jobs.fail(job["job_id"], "retry-worker", "text_provider_failed", unknown=True)

        repeated = self.jobs.authorize_unknown_retries(receipt, validate=validate)
        self.assertEqual([(entry["state"], entry["attempt"]) for entry in repeated],
                         [("unknown", 2)])
        self.assertIsNone(JobStore(self.store).claim(job["job_id"], "later-worker"))

        future = self.authorization(
            [(job, self.attempt(job["job_id"]))], receipt_id="synthetic-operator-retry-2")
        with self.assertRaisesRegex(
                SemanticStoreError, "semantic_job_operator_authorization_conflict"):
            self.jobs.authorize_unknown_retries(future, validate=self.evidence_validator(future))
        self.assertEqual(self.rows("jobs")[0]["state"], "unknown")
        self.assertEqual(self.rows("jobs")[0]["attempt"], 2)
        self.assertEqual(len(self.rows("job_operator_retry_authorizations")), 1)

    def test_operator_authorization_repeat_does_not_reopen_later_done(self):
        job, claim = self.unknown_extract_page()
        receipt = self.authorization([(job, claim)])
        validate = self.evidence_validator(receipt)
        self.jobs.authorize_unknown_retries(receipt, validate=validate)
        self.jobs.claim(job["job_id"], "retry-worker")
        artifact = self.jobs.accept(job["job_id"], "retry-worker", {"count": 3},
                                    validate=validate_count)
        self.jobs.finish(job["job_id"], accepted_artifact_receipt(job["job_id"], artifact))
        repeated = self.jobs.authorize_unknown_retries(receipt, validate=validate)
        self.assertEqual([(entry["state"], entry["attempt"]) for entry in repeated],
                         [("done", 2)])
        self.assertIsNone(self.jobs.claim(job["job_id"], "later-worker"))

    def test_operator_authorization_rejects_bad_bindings_without_partial_apply(self):
        pairs = [self.unknown_extract_page(0), self.unknown_extract_page(1)]
        valid = self.authorization(pairs)
        variants = []
        for key in ("generation_id", "action", "schema"):
            variants.append({**valid, key: "other"})
        for entry_index in (0, 1):
            for key in ("job_id", "job_key", "attempt", "worker_id", "finished_at"):
                jobs = deepcopy(valid["jobs"])
                jobs[entry_index][key] = "other" if key not in {"attempt", "finished_at"} else 2
                variants.append({**valid, "jobs": jobs})
        duplicate = deepcopy(valid)
        duplicate["jobs"][1] = deepcopy(duplicate["jobs"][0])
        variants.append(duplicate)
        variants.append({**valid, "jobs": [*valid["jobs"], {**valid["jobs"][0], "job_key": "other"}]})
        for receipt in variants:
            with self.subTest(receipt=receipt), self.assertRaises(SemanticStoreError):
                self.jobs.authorize_unknown_retries(
                    receipt, validate=self.evidence_validator(receipt))
        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_validator_required"):
            self.jobs.authorize_unknown_retries(valid)

        def boolean_validator(_value):
            return True

        with self.assertRaisesRegex(SemanticStoreError, "semantic_job_object_required"):
            self.jobs.authorize_unknown_retries(valid, validate=boolean_validator)

        def altered_validator(value):
            return {**value, "receipt_id": "altered"}

        with self.assertRaisesRegex(SemanticStoreError,
                                    "semantic_job_operator_authorization_invalid"):
            self.jobs.authorize_unknown_retries(valid, validate=altered_validator)
        self.assertEqual(self.rows("job_operator_retry_authorizations"), [])
        self.assertEqual({row["state"] for row in self.rows("jobs")}, {"unknown"})

        self.jobs.authorize_unknown_retries(valid, validate=self.evidence_validator(valid))
        partial = {**valid, "jobs": [valid["jobs"][0]]}
        with self.assertRaisesRegex(
                SemanticStoreError, "semantic_job_operator_authorization_conflict"):
            self.jobs.authorize_unknown_retries(
                partial, validate=self.evidence_validator(partial))
        self.assertEqual(len(self.rows("job_operator_retry_authorizations")), 2)
        self.assertEqual(
            next(row for row in self.rows("jobs")
                 if row["job_id"] == valid["jobs"][1]["job_id"])["state"],
            "pending",
        )

    def test_operator_authorization_rejects_running_known_artifact_and_scope_collisions(self):
        running = self.ensure(
            kind="extract_page", inputs={"parent_job_id": "running", "page": 0},
            versions={"prompt": "synthetic-page-v1", "profile": "fixture"})
        running_claim = self.jobs.claim(running["job_id"], "worker-running")

        known = self.unknown_extract_page(1)[0]
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE job_attempts SET error_code='provider_rejected',outcome_unknown=0 "
                "WHERE job_id=?", (known["job_id"],))
            conn.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (known["job_id"],))

        valid_job, _ = self.unknown_extract_page(2)
        artifact_job, artifact_claim = self.unknown_extract_page(3)
        artifact = {"count": 4}
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO accepted_job_artifacts VALUES(?,?,?,?)",
                         (artifact_job["job_id"], '{"count":4}', digest(artifact), self.now))

        def entry(job, claim):
            attempt = self.attempt(job["job_id"])
            return {"job_id": job["job_id"], "job_key": job["job_key"],
                    "attempt": claim["attempt"], "worker_id": attempt["worker_id"],
                    "finished_at": attempt["finished_at"]}

        for selected in (
            entry(running, running_claim),
            {**entry(valid_job, {"attempt": 1}), "job_id": known["job_id"],
             "job_key": known["job_key"]},
            {**entry(artifact_job, artifact_claim), "job_id": artifact_job["job_id"]},
        ):
            receipt = {**self.authorization([]), "jobs": [selected]}
            with self.subTest(job=selected), self.assertRaises(SemanticStoreError):
                self.jobs.authorize_unknown_retries(
                    receipt, validate=self.evidence_validator(receipt))
        self.assertEqual(self.rows("job_operator_retry_authorizations"), [])

        valid = self.authorization([(valid_job, self.attempt(valid_job["job_id"]))])
        self.jobs.authorize_unknown_retries(valid, validate=self.evidence_validator(valid))
        fresh, fresh_claim = self.unknown_extract_page(4)
        mixed = self.authorization(
            [(valid_job, self.attempt(valid_job["job_id"])), (fresh, fresh_claim)],
            receipt_id="synthetic-mixed")
        with self.assertRaisesRegex(
                SemanticStoreError,
                "semantic_job_operator_authorization_(binding_mismatch|conflict)"):
            self.jobs.authorize_unknown_retries(mixed, validate=self.evidence_validator(mixed))
        self.assertEqual(len(self.rows("job_operator_retry_authorizations")), 1)
        self.assertEqual(next(row for row in self.rows("jobs")
                              if row["job_id"] == fresh["job_id"])["state"], "unknown")

        modified = deepcopy(valid)
        modified["jobs"][0]["worker_id"] = "other-worker"
        with self.assertRaisesRegex(
                SemanticStoreError, "semantic_job_operator_authorization_conflict"):
            self.jobs.authorize_unknown_retries(
                modified, validate=self.evidence_validator(modified))

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
