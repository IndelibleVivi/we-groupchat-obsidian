"""Durable computation jobs in the canonical semantic generation database.

Callers validate stage semantics with ``accept(..., validate=callback)``. The
callback receives an isolated JSON object and must return a validated object.
A successful callback is not a database/core commit: ``finish`` separately
binds a real canonical receipt or an explicit accepted-artifact proof.

Unknown sent outcomes never become retryable on lease expiry. Reconciliation
requires an exact generation/job/key/attempt/worker/provider receipt, verified
by a caller-supplied evidence validator. Its durable evidence remains in the
existing generation database even after a later attempt or finish.

An operator may authorize one finite batch of exact unknown extract_page
attempts to return to pending. This is a separate audit decision, not proof of
the provider outcome. The authorization is consumed once per job; resubmission
is idempotent and never reopens later work.
"""
from __future__ import annotations

import json
import math
import re
import threading

from .contracts import SemanticStoreError, digest, dumps, integer, text, timestamp


RECONCILIATION_SCHEMA = "we-groupchat-obsidian.semantic-job-reconciliation.v1"
_RECONCILIATION_FIELDS = frozenset({
    "schema", "receipt_id", "generation_id", "job_id", "job_key", "attempt",
    "worker_id", "request_id", "provider_receipt_id", "outcome", "artifact_hash",
})
OPERATOR_RETRY_AUTHORIZATION_SCHEMA = "we-groupchat-obsidian.semantic-operator-retry-authorization.v1"
OPERATOR_RETRY_ACTION = "operator_authorized_unknown_extract_page_retry"
_OPERATOR_RETRY_FIELDS = frozenset({"schema", "receipt_id", "action", "generation_id", "jobs"})
_OPERATOR_RETRY_JOB_FIELDS = frozenset({"job_id", "job_key", "attempt", "worker_id", "finished_at"})
_STATES = frozenset({"pending", "running", "failed", "unknown", "accepted", "done"})


def _require(condition, code):
    if not condition:
        raise SemanticStoreError(code)


def _object(value):
    """Canonical JSON copy without stringifying keys or nonfinite numbers."""
    def check(item):
        if type(item) in (str, int, bool, type(None)):
            return
        if type(item) is float:
            _require(math.isfinite(item), "semantic_invalid_json")
        elif type(item) in (list, tuple):
            for child in item:
                check(child)
        elif type(item) is dict:
            _require(all(type(key) is str for key in item), "semantic_invalid_json")
            for child in item.values():
                check(child)
        else:
            raise SemanticStoreError("semantic_invalid_json")
    _require(type(value) is dict, "semantic_job_object_required")
    try:
        check(value)
        return json.loads(dumps(value))
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise SemanticStoreError("semantic_invalid_json") from exc


def accepted_artifact_receipt(job_id, artifact):
    """Reference an already accepted computation without claiming core apply."""
    return "accepted:" + text(job_id) + ":" + digest(_object(artifact))


class JobStore:
    def __init__(self, store):
        self.store = store
        # Attempt fencing also distinguishes a restarted instance that happens
        # to reuse the same worker id. One instance cannot silently overwrite
        # its own still-in-flight claim after expiry.
        self._claims = {}
        self._claims_lock = threading.RLock()

    def _now(self):
        return timestamp(self.store.now_func())

    def _key(self, kind, inputs, versions):
        return digest({"generation_id": self.store.generation_id, "kind": kind,
                       "inputs": inputs, "versions": versions})

    def _job(self, conn, job_id):
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (text(job_id),)).fetchone()
        _require(row is not None, "semantic_job_missing")
        result = dict(row)
        try:
            result["inputs"] = _object(json.loads(result.pop("inputs_json")))
            result["versions"] = _object(json.loads(result.pop("versions_json")))
            _require(result["state"] in _STATES, "semantic_job_corrupt")
            integer(result["attempt"])
            _require(result["job_key"] == self._key(result["kind"], result["inputs"], result["versions"]),
                     "semantic_job_corrupt")
        except (ValueError, TypeError, KeyError) as exc:
            raise SemanticStoreError("semantic_job_corrupt") from exc
        return result

    @staticmethod
    def _artifact(conn, job):
        row = conn.execute("SELECT * FROM accepted_job_artifacts WHERE job_id=?", (job["job_id"],)).fetchone()
        if row is None:
            _require(job["state"] not in {"accepted", "done"}, "semantic_job_corrupt")
            return None
        _require(job["state"] in {"accepted", "done"}, "semantic_job_corrupt")
        try:
            value = _object(json.loads(row["artifact_json"]))
            _require(digest(value) == row["artifact_hash"], "semantic_job_artifact_corrupt")
            return value
        except (TypeError, ValueError) as exc:
            raise SemanticStoreError("semantic_job_artifact_corrupt") from exc

    def ensure(self, kind, inputs, versions):
        with self.store.transaction() as conn:
            return self._ensure(conn, kind, inputs, versions)

    def _ensure(self, conn, kind, inputs, versions):
        kind, inputs, versions = text(kind), _object(inputs), _object(versions)
        key = self._key(kind, inputs, versions)
        identity, now = "job_" + key, self._now()
        conn.execute(
            "INSERT OR IGNORE INTO jobs(job_id,job_key,kind,inputs_json,versions_json,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'pending',?,?)",
            (identity, key, kind, dumps(inputs), dumps(versions), now, now))
        job = self._job(conn, identity)
        _require(job["kind"] == kind and dumps(job["inputs"]) == dumps(inputs)
                 and dumps(job["versions"]) == dumps(versions), "semantic_job_key_conflict")
        return job

    def _accept_view_projection(self, conn, kind, inputs, artifact):
        """Store-only, unsent deterministic projection inside its canonical commit.

        No claim/lease or provider attempt is manufactured. A caller must retain
        this transaction through canonical apply and finish; rollback is atomic.
        """
        from .live_views import MEMBERS
        _require(conn.in_transaction and kind in {"propose", "decision"}, "semantic_program_projection_invalid")
        plan = self._job(conn, inputs["source_plan_job_id"])
        choice = self._job(conn, inputs["source_choice_job_id"])
        accepted_choice = self._artifact(conn, choice)
        _require(plan["kind"] == "live_view_induction" and choice["kind"] == "live_view_choice"
                 and choice["inputs"].get("proposal_job_id") == plan["job_id"]
                 and self._artifact(conn, plan) is not None
                 and accepted_choice is not None and accepted_choice.get("choice") == "apply", "semantic_program_projection_invalid")
        job = self._ensure(conn, kind, inputs, {"contract": MEMBERS})
        value = _object(artifact)
        accepted = self._artifact(conn, job)
        if accepted is not None:
            _require(accepted == value, "semantic_job_artifact_conflict")
            return job
        _require(job["state"] == "pending" and job["attempt"] == 0, "semantic_program_projection_invalid")
        self._publish_artifact(conn, job["job_id"], value, self._now())
        return self._job(conn, job["job_id"])

    def _finish_in_transaction(self, conn, job_id, receipt_id):
        _require(conn.in_transaction, "semantic_program_projection_invalid")
        job = self._job(conn, job_id)
        artifact = self._artifact(conn, job)
        _require(artifact is not None, "semantic_job_artifact_required")
        if job["state"] == "done":
            _require(job["receipt_id"] == receipt_id, "semantic_job_finish_receipt_conflict")
            return job
        self._finish_proof(conn, job, artifact, receipt_id)
        conn.execute("UPDATE jobs SET state='done',receipt_id=?,updated_at=? WHERE job_id=?",
                     (receipt_id, self._now(), job_id))
        return self._job(conn, job_id)

    def claim(self, job_id, worker_id, lease_seconds=300):
        job_id, worker_id = text(job_id), text(worker_id)
        _require(type(lease_seconds) in (int, float) and lease_seconds > 0
                 and math.isfinite(lease_seconds), "semantic_job_lease_invalid")
        now = self._now()
        deadline = timestamp(now + lease_seconds)
        key = (job_id, worker_id)
        with self._claims_lock:
            if key in self._claims:
                return None
            self._claims[key] = None
        claimed, committed = None, False
        try:
            with self.store.transaction() as conn:
                job = self._job(conn, job_id)
                if self._artifact(conn, job) is not None or job["state"] == "unknown":
                    return None
                if job["state"] == "running":
                    _require(job["lease_until"] is not None, "semantic_job_corrupt")
                    if job["lease_until"] > now:
                        return None
                    conn.execute(
                        "UPDATE job_attempts SET finished_at=?,error_code='semantic_job_lease_expired' "
                        "WHERE job_id=? AND attempt=? AND finished_at IS NULL",
                        (now, job_id, job["attempt"]),
                    )
                attempt = job["attempt"] + 1
                conn.execute("UPDATE jobs SET state='running',worker_id=?,lease_until=?,attempt=?,updated_at=? WHERE job_id=?",
                             (worker_id, deadline, attempt, now, job_id))
                conn.execute("INSERT INTO job_attempts(job_id,attempt,worker_id,started_at) VALUES(?,?,?,?)",
                             (job_id, attempt, worker_id, now))
                claimed = self._job(conn, job_id)
            committed = True
            return claimed
        finally:
            with self._claims_lock:
                if committed:
                    self._claims[key] = claimed["attempt"]
                else:
                    self._claims.pop(key, None)

    def _attempt(self, job_id, worker_id, attempt):
        text(worker_id)
        with self._claims_lock:
            claimed = self._claims.get((job_id, worker_id)) if attempt is None else attempt
        _require(claimed is not None, "semantic_job_claim_required")
        return integer(claimed, 1)

    @staticmethod
    def _owns(job, worker_id, attempt):
        _require(job["state"] == "running" and job["worker_id"] == worker_id
                 and job["attempt"] == attempt, "semantic_job_lease_lost")

    def _release(self, job_id, worker_id, attempt):
        with self._claims_lock:
            if self._claims.get((job_id, worker_id)) == attempt:
                self._claims.pop((job_id, worker_id), None)

    @staticmethod
    def _validated(artifact, validate):
        _require(callable(validate), "semantic_job_validator_required")
        return _object(validate(_object(artifact)))

    @staticmethod
    def _publish_artifact(conn, job_id, artifact, now):
        conn.execute("INSERT INTO accepted_job_artifacts VALUES(?,?,?,?)",
                     (job_id, dumps(artifact), digest(artifact), now))
        conn.execute("UPDATE jobs SET state='accepted',lease_until=NULL,updated_at=? WHERE job_id=?", (now, job_id))

    def accept(self, job_id, worker_id, artifact, *, validate=None, attempt=None):
        job_id, worker_id = text(job_id), text(worker_id)
        artifact = self._validated(artifact, validate)
        with self.store.transaction() as conn:
            job = self._job(conn, job_id)
            accepted = self._artifact(conn, job)
            if accepted is not None:
                _require(dumps(accepted) == dumps(artifact), "semantic_job_artifact_conflict")
                return accepted
            claimed = self._attempt(job_id, worker_id, attempt)
            self._owns(job, worker_id, claimed)
            # An expired claimant can finish only while no later attempt has
            # replaced it. BEGIN IMMEDIATE makes acceptance vs takeover atomic.
            now = self._now()
            self._publish_artifact(conn, job_id, artifact, now)
            conn.execute("UPDATE job_attempts SET finished_at=? WHERE job_id=? AND attempt=?", (now, job_id, claimed))
        self._release(job_id, worker_id, claimed)
        return artifact

    def accepted(self, job_id):
        with self.store.transaction() as conn:
            return self._artifact(conn, self._job(conn, job_id))

    def fail(self, job_id, worker_id, code, unknown=False, *, attempt=None):
        job_id, worker_id = text(job_id), text(worker_id)
        _require(isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code) is not None,
                 "semantic_job_error_code_invalid")
        _require(type(unknown) is bool, "semantic_job_outcome_invalid")
        claimed = self._attempt(job_id, worker_id, attempt)
        with self.store.transaction() as conn:
            job = self._job(conn, job_id)
            if self._artifact(conn, job) is not None:
                return job
            self._owns(job, worker_id, claimed)
            now = self._now()
            conn.execute("UPDATE job_attempts SET finished_at=?,error_code=?,outcome_unknown=? WHERE job_id=? AND attempt=?",
                         (now, code, int(unknown), job_id, claimed))
            conn.execute("UPDATE jobs SET state=?,lease_until=NULL,updated_at=? WHERE job_id=?",
                         ("unknown" if unknown else "failed", now, job_id))
            result = self._job(conn, job_id)
        self._release(job_id, worker_id, claimed)
        return result

    def renew(self, job_id, worker_id, *, attempt=None):
        """Keep an owned attempt fenced while it waits for provider admission."""
        claimed = self._attempt(job_id, worker_id, attempt)
        with self.store.transaction() as conn:
            job = self._job(conn, job_id)
            self._owns(job, worker_id, claimed)
            now = self._now()
            conn.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE job_id=?", (now + 300, now, job_id))

    def defer(self, job_id, worker_id, evidence, *, attempt=None):
        """Return only a scheduler-proven unsent attempt to pending, not failed."""
        from .scheduling import not_dispatched
        _require(not_dispatched(evidence) and (evidence.request_id == job_id or
                 evidence.request_id.startswith(job_id + ":")), "semantic_job_unsent_proof_required")
        claimed = self._attempt(job_id, worker_id, attempt)
        with self.store.transaction() as conn:
            job = self._job(conn, job_id)
            _require(self._artifact(conn, job) is None, "semantic_job_already_accepted")
            self._owns(job, worker_id, claimed)
            now = self._now()
            conn.execute("UPDATE job_attempts SET finished_at=?,error_code=?,outcome_unknown=0 WHERE job_id=? AND attempt=?",
                         (now, evidence.code, job_id, claimed))
            conn.execute("UPDATE jobs SET state='pending',worker_id=NULL,lease_until=NULL,updated_at=? WHERE job_id=?",
                         (now, job_id))
            result = self._job(conn, job_id)
        self._release(job_id, worker_id, claimed)
        return result

    def _finish_proof(self, conn, job, artifact, receipt_id):
        if receipt_id == accepted_artifact_receipt(job["job_id"], artifact):
            return
        superseded_id = job["inputs"].get("proposal_job_id") if job["kind"] == "decision" else job["job_id"]
        supersession = conn.execute("SELECT receipt_id,proof_json FROM proposal_supersessions WHERE job_id=?",
                                    (superseded_id,)).fetchone()
        if supersession and supersession["receipt_id"] == receipt_id:
            _require(any(value == {"job_id": job["job_id"], "artifact_hash": digest(artifact)}
                         for value in json.loads(supersession["proof_json"])["terminal_jobs"]),
                     "semantic_job_finish_receipt_invalid")
            self.store._verify_proposal_supersession(conn, superseded_id)
            return
        own = conn.execute("SELECT 1 FROM extraction_receipts WHERE job_id=? AND receipt_id=?",
                           (job["job_id"], receipt_id)).fetchone()
        canonical = conn.execute("SELECT job_id FROM canonical_transactions WHERE transaction_id=?", (receipt_id,)).fetchone()
        if own or (canonical and canonical["job_id"] == job["job_id"]):
            return
        if job["kind"] == "decision" and canonical and canonical["job_id"] == job["inputs"].get("proposal_job_id"):
            decision = conn.execute("SELECT decision_json FROM decision_log WHERE job_id=?", (canonical["job_id"],)).fetchone()
            if decision and dumps(json.loads(decision["decision_json"])) == dumps(artifact):
                return
        batch_id = job["inputs"].get("batch_id")
        if batch_id and conn.execute("SELECT 1 FROM processing_receipts WHERE batch_id=? AND receipt_id=?",
                                     (batch_id, receipt_id)).fetchone():
            return
        raise SemanticStoreError("semantic_job_finish_receipt_invalid")

    def finish(self, job_id, receipt_id):
        receipt_id = text(receipt_id)
        with self.store.transaction() as conn:
            job = self._job(conn, job_id)
            artifact = self._artifact(conn, job)
            _require(artifact is not None, "semantic_job_artifact_required")
            if job["state"] == "done":
                _require(job["receipt_id"] == receipt_id, "semantic_job_finish_receipt_conflict")
                return job
            self._finish_proof(conn, job, artifact, receipt_id)
            conn.execute("UPDATE jobs SET state='done',receipt_id=?,updated_at=? WHERE job_id=?",
                         (receipt_id, self._now(), job_id))
            return self._job(conn, job_id)

    def finish_applied_proposal(self, job_id, receipt_id):
        """Finish the actual committed plan and its exact accepted choice.

        Recovery through an obsolete context can reach the replacement after
        its transaction committed but before either finish call ran.
        """
        job_id, receipt_id = text(job_id), text(receipt_id)
        with self.store.transaction() as conn:
            proposal = self._job(conn, job_id)
            artifact = self._artifact(conn, proposal)
            decision = conn.execute("SELECT decision_json FROM decision_log WHERE job_id=? AND transaction_id=?",
                                    (job_id, receipt_id)).fetchone()
            _require(proposal["kind"] == "propose" and artifact is not None and decision is not None,
                     "semantic_job_finish_receipt_invalid")
            selected = [(proposal, artifact)]
            for row in conn.execute("SELECT job_id FROM jobs WHERE kind='decision'"):
                job = self._job(conn, row[0])
                if job["inputs"].get("proposal_job_id") != job_id or job["inputs"].get("proposal") != artifact:
                    continue
                choice = self._artifact(conn, job)
                if choice is not None and dumps(choice) == decision["decision_json"]:
                    selected.append((job, choice))
            for job, accepted in selected:
                _require(job["state"] != "done" or job["receipt_id"] == receipt_id,
                         "semantic_job_finish_receipt_conflict")
                self._finish_proof(conn, job, accepted, receipt_id)
                conn.execute("UPDATE jobs SET state='done',receipt_id=?,updated_at=? WHERE job_id=?",
                             (receipt_id, self._now(), job["job_id"]))

    def reconcile_unknown(self, job_id, evidence_receipt, *, validate=None,
                          artifact=None, validate_artifact=None):
        """Resolve one unknown attempt with durable, independently checked proof.

        Evidence keys are RECONCILIATION_FIELDS; outcome is not_sent, failed or
        completed. For completed, artifact_hash must match validate_artifact's
        validated dict. Other outcomes require artifact_hash=null and no
        artifact, and permit a new attempt. validate(evidence)->same dict must
        verify the provider/usage receipt; a boolean is insufficient.
        """
        receipt = _object(evidence_receipt)
        _require(set(receipt) == _RECONCILIATION_FIELDS and receipt["schema"] == RECONCILIATION_SCHEMA,
                 "semantic_job_reconciliation_invalid")
        for key in ("receipt_id", "generation_id", "job_id", "job_key", "worker_id", "request_id", "provider_receipt_id"):
            text(receipt[key])
        integer(receipt["attempt"], 1)
        _require(receipt["outcome"] in ("not_sent", "failed", "completed"), "semantic_job_reconciliation_invalid")
        checked = self._validated(receipt, validate)
        _require(dumps(checked) == dumps(receipt), "semantic_job_reconciliation_invalid")
        if receipt["outcome"] == "completed":
            artifact = self._validated(artifact, validate_artifact)
            _require(receipt["artifact_hash"] == digest(artifact), "semantic_job_reconciliation_invalid")
        else:
            _require(artifact is None and receipt["artifact_hash"] is None, "semantic_job_reconciliation_invalid")
        with self.store.transaction() as conn:
            job = self._job(conn, job_id)
            _require(receipt["generation_id"] == self.store.generation_id and receipt["job_id"] == job_id
                     and receipt["job_key"] == job["job_key"], "semantic_job_reconciliation_binding_mismatch")
            prior = conn.execute("SELECT * FROM job_reconciliations WHERE receipt_id=? OR (job_id=? AND attempt=?)",
                                 (receipt["receipt_id"], job_id, receipt["attempt"])).fetchall()
            if prior:
                _require(len(prior) == 1 and prior[0]["evidence_json"] == dumps(receipt),
                         "semantic_job_reconciliation_conflict")
                return job
            _require(job["state"] == "unknown" and job["attempt"] == receipt["attempt"]
                     and job["worker_id"] == receipt["worker_id"], "semantic_job_reconciliation_binding_mismatch")
            attempt = conn.execute("SELECT outcome_unknown FROM job_attempts WHERE job_id=? AND attempt=?",
                                   (job_id, receipt["attempt"])).fetchone()
            _require(attempt is not None and attempt["outcome_unknown"] == 1, "semantic_job_reconciliation_binding_mismatch")
            now = self._now()
            conn.execute("INSERT INTO job_reconciliations VALUES(?,?,?,?,?)",
                         (job_id, receipt["attempt"], receipt["receipt_id"], dumps(receipt), now))
            if artifact is not None:
                self._publish_artifact(conn, job_id, artifact, now)
            else:
                conn.execute("UPDATE jobs SET state='pending',worker_id=NULL,lease_until=NULL,updated_at=? WHERE job_id=?",
                             (now, job_id))
            return self._job(conn, job_id)

    @staticmethod
    def _operator_retry_authorization(value):
        receipt = _object(value)
        _require(set(receipt) == _OPERATOR_RETRY_FIELDS
                 and receipt["schema"] == OPERATOR_RETRY_AUTHORIZATION_SCHEMA
                 and receipt["action"] == OPERATOR_RETRY_ACTION,
                 "semantic_job_operator_authorization_invalid")
        text(receipt["receipt_id"])
        text(receipt["generation_id"])
        jobs = receipt["jobs"]
        _require(type(jobs) is list and jobs, "semantic_job_operator_authorization_invalid")
        normalized, job_ids = [], set()
        for entry in jobs:
            entry = _object(entry)
            _require(set(entry) == _OPERATOR_RETRY_JOB_FIELDS,
                     "semantic_job_operator_authorization_invalid")
            job_id = text(entry["job_id"])
            _require(job_id not in job_ids, "semantic_job_operator_authorization_invalid")
            job_ids.add(job_id)
            normalized.append({
                "job_id": job_id,
                "job_key": text(entry["job_key"]),
                "attempt": integer(entry["attempt"], 1),
                "worker_id": text(entry["worker_id"]),
                "finished_at": timestamp(entry["finished_at"]),
            })
        return {**receipt, "jobs": normalized}

    def authorize_unknown_retries(self, authorization, *, validate=None):
        """Apply one explicit operator retry authorization atomically.

        Only current, finished, unknown ``extract_page`` attempts can be
        selected. The receipt binds every exact job/attempt/worker/finished-at
        identity. A successful apply only returns those jobs to ``pending``;
        the normal claim path creates the next attempt. Repeating an exact
        receipt returns current job state without reopening work.
        """
        receipt = self._operator_retry_authorization(authorization)
        checked = self._validated(receipt, validate)
        _require(dumps(checked) == dumps(receipt), "semantic_job_operator_authorization_invalid")
        _require(receipt["generation_id"] == self.store.generation_id,
                 "semantic_job_operator_authorization_binding_mismatch")
        now = self._now()
        with self.store.transaction() as conn:
            prior = conn.execute(
                "SELECT * FROM job_operator_retry_authorizations WHERE receipt_id=? ORDER BY ordinal",
                (receipt["receipt_id"],),
            ).fetchall()
            if prior:
                _require(
                    len(prior) == len(receipt["jobs"])
                    and all(
                        row["ordinal"] == ordinal
                        and row["generation_id"] == receipt["generation_id"]
                        and row["action"] == receipt["action"]
                        and row["job_id"] == entry["job_id"]
                        and row["attempt"] == entry["attempt"]
                        and row["worker_id"] == entry["worker_id"]
                        and row["finished_at"] == entry["finished_at"]
                        and row["receipt_json"] == dumps(receipt)
                        for ordinal, (row, entry) in enumerate(zip(prior, receipt["jobs"]))
                    ),
                    "semantic_job_operator_authorization_conflict",
                )
                return tuple(self._job(conn, entry["job_id"]) for entry in receipt["jobs"])

            selected = []
            for entry in receipt["jobs"]:
                job = self._job(conn, entry["job_id"])
                _require(job["kind"] == "extract_page"
                         and job["state"] == "unknown"
                         and job["job_key"] == entry["job_key"]
                         and job["attempt"] == entry["attempt"]
                         and job["worker_id"] == entry["worker_id"],
                         "semantic_job_operator_authorization_binding_mismatch")
                _require(self._artifact(conn, job) is None,
                         "semantic_job_operator_authorization_conflict")
                attempt = conn.execute(
                    "SELECT worker_id,finished_at,error_code,outcome_unknown "
                    "FROM job_attempts WHERE job_id=? AND attempt=?",
                    (entry["job_id"], entry["attempt"]),
                ).fetchone()
                _require(attempt is not None
                         and attempt["worker_id"] == entry["worker_id"]
                         and attempt["finished_at"] == entry["finished_at"]
                         and attempt["error_code"] == "text_provider_failed"
                         and attempt["outcome_unknown"] == 1,
                         "semantic_job_operator_authorization_binding_mismatch")
                _require(conn.execute(
                    "SELECT 1 FROM job_operator_retry_authorizations WHERE job_id=?",
                    (entry["job_id"],),
                ).fetchone() is None, "semantic_job_operator_authorization_conflict")
                selected.append(entry)

            for ordinal, entry in enumerate(selected):
                conn.execute(
                    "INSERT INTO job_operator_retry_authorizations VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (receipt["receipt_id"], ordinal, receipt["generation_id"], receipt["action"],
                     entry["job_id"], entry["attempt"], entry["worker_id"], entry["finished_at"],
                     dumps(receipt), now),
                )
            for entry in selected:
                changed = conn.execute(
                    "UPDATE jobs SET state='pending',worker_id=NULL,lease_until=NULL,updated_at=? "
                    "WHERE job_id=? AND state='unknown' AND attempt=? AND worker_id=?",
                    (now, entry["job_id"], entry["attempt"], entry["worker_id"]),
                ).rowcount
                _require(changed == 1, "semantic_job_operator_authorization_binding_mismatch")
            return tuple(self._job(conn, entry["job_id"]) for entry in selected)
