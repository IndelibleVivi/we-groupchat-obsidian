"""Independent, read-only acceptance of a staged semantic generation.

The verifier reads canonical SQLite rows and immutable expected membership. It
never calls producer completion counters, constructs a source/provider, repairs
data, acknowledges an outbox, or initializes a consumer schema.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from itertools import zip_longest
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from core.url_safety import redact_urls_in_text
from .attribution import EXTRACTION_PROMPT_VERSION, captured_source_metadata, source_attribution
from .legacy import (LegacyError, LegacyInventory, legacy_resources,
                     HISTORY_SCHEMA, HISTORY_PUBLICATION_SCHEMA, SOURCE_FIRST_POLICY, LEGACY_SEMANTIC_POLICY,
                     TABLE_KEYS,
                     history_policy, build_history_publication_plan, history_document_body, history_root_body,
                     _checked_path, _read_regular, _relative, _under, _opaque)
from .projections import (ReadingError, property_value, region_body, reading_policy,
                          LEGACY_READING_POLICY, CONTENT_READING_POLICY, SOURCE_READING_POLICY, EDITORIAL_READING_POLICY,
                          EDITORIAL_V2_READING_POLICY, _contains_verbatim)
from .rebuild_payloads import is_ref
from .verification_storage import GroupMap, RowMap, Rows, ScratchMap, freeze_json, workspace


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _bytes_hash(value):
    return hashlib.sha256(value).hexdigest()


def _load(value):
    return json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


@dataclass(frozen=True)
class VerificationIssue:
    code: str
    category: str
    count: int = 1


@dataclass(frozen=True)
class VerificationReport:
    complete: bool
    issues: tuple[VerificationIssue, ...]
    counts: dict[str, int]

    def to_dict(self):
        return asdict(self)


class RebuildVerifier:
    """Read a consistent DB snapshot plus a private staged membership manifest."""

    def __init__(self, store_path, manifest: Mapping[str, Any], *, projection_root=None,
                 history_payload_loader=None):
        self.store_path = store_path
        # Freeze the caller's nested object; validation is reported content-free.
        try:
            self.manifest = freeze_json(dict(manifest))
        except (TypeError, ValueError):
            self.manifest = {}
        self.projection_root = projection_root
        self.history_payload_loader = history_payload_loader
        self._issues = Counter()
        self.counts = Counter()

    def issue(self, code, category="invalid", count=1):
        if count:
            self._issues[(category, code)] += count

    def _has(self, name):
        return name in self.tables

    def _rows(self, table):
        return Rows(self.db, f'SELECT * FROM "{table}"')

    def verify(self) -> VerificationReport:
        self._issues.clear()
        self.counts.clear()
        source = None
        try:
            path = _checked_path(self.store_path)
            source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            source.execute("PRAGMA query_only=ON")
            source.execute("PRAGMA cache_size=-4096")
            with workspace(source) as (self.db, self.scratch):
                source.close()
                source = None
                self.tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required = {"generation_manifest", "source_records", "source_heads", "capture_batches", "capture_batch_members",
                            "source_spans", "observations", "observation_evidence", "content_units",
                            "items", "item_observations", "item_revisions", "source_dispositions",
                            "accepted_job_artifacts", "extraction_receipts", "processing_receipts",
                            "action_receipts", "decision_log", "projection_outbox", "index_outbox",
                            "source_resource_refs", "resource_refs", "canonical_transactions",
                            "source_core_jobs", "change_events", "jobs"}
                if not required <= self.tables:
                    self.issue("canonical_schema_missing")
                else:
                    # These indexes exist only on the disposable copy. Point and
                    # group lookups must not trade corpus RAM for repeated scans.
                    for table, columns in (("action_receipts", "job_id"), ("canonical_transactions", "job_id"),
                            ("processing_receipts", "transaction_id"), ("extraction_receipts", "batch_id"),
                            ("item_observations", "observation_id"), ("legacy_mappings", "legacy_id"),
                            ("legacy_mappings", "target_id,kind"), ("projection_outbox", "transaction_id"),
                            ("index_outbox", "transaction_id")):
                        if table in self.tables:
                            name = "verification_" + table + "_" + columns.replace(",", "_")
                            self.db.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}"({columns})')
                    self.db.execute("PRAGMA query_only=ON")
                    self._verify_canonical()
                    self._verify_legacy()
        except (LegacyError, OSError):
            self.issue("verification_input_unavailable")
        except sqlite3.Error as exc:
            # SQLite primary codes: NOMEM, IOERR, FULL and CANTOPEN describe
            # unavailable verification storage rather than invalid evidence.
            code = (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xff
            self.issue("verification_input_unavailable" if code in {7, 10, 13, 14}
                       else "verification_evidence_malformed")
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError):
            self.issue("verification_evidence_malformed")
        finally:
            if source is not None:
                source.close()
            # A retained verifier can be called again, but must not retain the
            # previous run's rows, decoded payloads, metadata sets or connections.
            for key in ("db", "scratch", "tables", "generation", "history_policy", "reading_policy", "sources", "source_resources", "batches", "members",
                        "accepted", "extractions", "extraction_inputs", "processing", "transactions", "outboxes",
                        "dispositions", "actions", "observations", "items", "spans", "evidence", "observed_versions",
                        "item_observations", "item_content_observations", "projection_id_paths"):
                self.__dict__.pop(key, None)
        issues = tuple(VerificationIssue(code, category, count)
                       for (category, code), count in sorted(self._issues.items()))
        return VerificationReport(not issues, issues, dict(sorted(self.counts.items())))

    def _verify_canonical(self):
        generations = list(self._rows("generation_manifest"))
        self.generation = self.manifest.get("generation_id")
        if (len(generations) != 1 or not self.generation
                or generations[0]["generation_id"] != self.generation
                or generations[0]["schema"] != "we-groupchat-obsidian.semantic.v1"):
            self.issue("generation_binding_mismatch")
        try:
            self.reading_policy = reading_policy(self.manifest)
            if len(generations) == 1 and reading_policy(_load(generations[0]["metadata_json"])) != self.reading_policy:
                self.issue("reading_policy_binding_mismatch")
        except ReadingError:
            self.issue("reading_policy_invalid")
            self.reading_policy = LEGACY_READING_POLICY
        try:
            self.history_policy = history_policy(self.manifest)
            if len(generations) == 1 and history_policy(_load(generations[0]["metadata_json"])) != self.history_policy:
                self.issue("legacy_history_policy_mismatch")
        except LegacyError:
            self.issue("legacy_history_policy_invalid")
            self.history_policy = LEGACY_SEMANTIC_POLICY
        if self.history_policy == SOURCE_FIRST_POLICY:
            if (self.db.execute("SELECT 1 FROM capture_batches WHERE purpose='legacy_import' LIMIT 1").fetchone()
                    or self._has("legacy_mappings") and self.db.execute(
                        "SELECT 1 FROM legacy_mappings WHERE target_type='source_version' LIMIT 1").fetchone()):
                self.issue("legacy_history_semantic_contamination")
            if self.manifest.get("legacy_capture_passes"):
                self.issue("legacy_history_semantic_contamination")
        elif (self.manifest.get("history_preservation_passes") or self._has("legacy_mappings") and self.db.execute(
                "SELECT 1 FROM legacy_mappings WHERE kind='history_pass' LIMIT 1").fetchone()):
            self.issue("legacy_history_policy_mismatch")
        self.issue("foreign_key_violation", count=sum(1 for _ in self.db.execute("PRAGMA foreign_key_check")))
        if [r[0] for r in self.db.execute("PRAGMA quick_check")] != ["ok"]:
            self.issue("canonical_integrity_failed")
        def rows(table, key, order=""):
            return RowMap(self.db, f'SELECT * FROM "{table}"' + order, key)
        self.sources = rows("source_records", "source_version_id")
        def resource(row):
            value = {k: row[k] for k in ("resource_id", "occurrence_id", "purpose", "status", "location")}
            value["metadata"] = _load(row["metadata_json"])
            return value
        for row in self.db.execute("SELECT * FROM source_resource_refs"):
            value = resource(row)
            if (any(not isinstance(value[k], str) or not value[k].strip()
                    for k in ("resource_id", "occurrence_id", "purpose", "status"))
                    or value["location"] is not None and not isinstance(value["location"], str)
                    or not isinstance(value["metadata"], dict)):
                self.issue("source_resource_invalid")
        self.source_resources = GroupMap(self.db, "source_resource_refs", "source_version_id",
                                         order="resource_id,occurrence_id", transform=resource)
        self.batches = rows("capture_batches", "batch_id", " ORDER BY rowid")
        if not self.batches:
            self.issue("capture_evidence_missing", "source_pending")
        self.members = GroupMap(self.db, "capture_batch_members", "batch_id", order="ordinal")
        self.accepted = rows("accepted_job_artifacts", "job_id")
        self.extractions = rows("extraction_receipts", "job_id")
        self.extraction_inputs = ScratchMap(self.scratch, "extraction_inputs")
        self.processing = rows("processing_receipts", "batch_id")
        self.transactions = rows("canonical_transactions", "transaction_id")
        self.outboxes = RowMap(self.db, "SELECT p.outbox_id,p.owner_id,NULL AS owner_type,p.revision,p.transaction_id,"
            "p.status,p.receipt_id,'projection' AS kind FROM projection_outbox p WHERE NOT EXISTS "
            "(SELECT 1 FROM index_outbox i WHERE i.outbox_id=p.outbox_id) UNION ALL "
            "SELECT outbox_id,owner_id,owner_type,revision,transaction_id,status,receipt_id,'index' AS kind FROM index_outbox",
            "outbox_id")
        self.dispositions = rows("source_dispositions", "source_version_id")
        self.actions = rows("action_receipts", "action_key")
        self.observations = rows("observations", "observation_id")
        self.items = rows("items", "item_id")
        self.spans = rows("source_spans", "span_id")
        self.evidence = GroupMap(self.db, "observation_evidence", "observation_id",
                                 transform=lambda r: r["span_id"], collection=set)
        self.observed_versions = {r[0] for r in self.db.execute("SELECT DISTINCT s.source_version_id "
            "FROM source_spans s JOIN observation_evidence e USING(span_id)")}
        self.item_observations = GroupMap(self.db, "item_observations", "item_id",
                                         transform=lambda r: r["observation_id"], collection=set)
        self.item_content_observations = GroupMap(self.db, "item_observations", "item_id", clause="role<>'evidence_only'",
                                                 transform=lambda r: r["observation_id"], collection=set)
        for row in self._rows("item_observations"):
            if row["added_by"] not in self.actions:
                self.issue("item_action_receipt_missing")
        self.counts.update(captured_versions=len(self.sources), capture_batches=len(self.batches),
                           observations=len(self.observations), items=len(self.items))
        self._verify_members()
        self._verify_source_versions()
        self._verify_evidence()
        self._verify_resources()
        self._verify_actions()
        self._verify_transactions()
        self._verify_prefix()
        active = RowMap(self.db, "SELECT * FROM items WHERE lifecycle='active' AND EXISTS "
                        "(SELECT 1 FROM item_observations m WHERE m.item_id=items.item_id)", "item_id").values()
        self._verify_index(active)
        self._verify_projections(active)
        self._verify_brief()

    def _verify_members(self):
        expected = self.manifest.get("expected_batches")
        if not isinstance(expected, list):
            self.issue("expected_capture_manifest_missing", "source_pending")
            expected = []
        expected_ids = [b["batch_id"] for b in expected]
        if len(expected_ids) != len(set(expected_ids)) or set(expected_ids) != self.batches.keys():
            self.issue("capture_batch_set_mismatch")
        for entry in expected:
            batch_id = entry["batch_id"]
            actual = [r["source_version_id"] for r in self.members[batch_id]]
            if entry["member_version_ids"] != actual:
                self.issue("capture_member_identity_mismatch")
        dispositions = self.dispositions
        known_members = set()
        for bid, batch in self.batches.items():
            members = self.members[bid]
            receipt = _load(batch["receipt_json"])
            if (receipt.get("receipt_id") != batch["receipt_id"] or receipt.get("batch_id") != bid
                    or receipt.get("generation_id") != self.generation
                    or receipt.get("member_version_ids") != [r["source_version_id"] for r in members]
                    or receipt.get("input_cursors") != _load(batch["input_cursors_json"])
                    or receipt.get("output_cursors") != _load(batch["output_cursors_json"])):
                self.issue("capture_receipt_invalid")
            if (any(receipt.get(key) != batch[key] for key in (
                    "source_namespace", "chat_id", "inventory_digest", "inventory_revision", "purpose"))
                    or type(receipt.get("source_eof")) is not bool
                    or receipt["source_eof"] != bool(batch["source_eof"])):
                self.issue("capture_receipt_binding_mismatch")
            if [r["ordinal"] for r in members] != list(range(len(members))):
                self.issue("capture_ordinal_gap")
            for member in members:
                vid = member["source_version_id"]
                known_members.add(vid)
                source = self.sources.get(vid)
                if not source:
                    self.issue("capture_source_missing")
                    continue
                if (source["source_namespace"], source["chat_id"]) != (batch["source_namespace"], batch["chat_id"]):
                    self.issue("capture_source_binding_mismatch")
                if not member["core"]:
                    continue
                disposition = dispositions.get(vid)
                if disposition is None:
                    processed = self.db.execute("SELECT 1 FROM source_core_jobs WHERE source_version_id=?", (vid,)).fetchone()
                    self.issue("source_disposition_pending" if processed else "source_disposition_missing", "semantic_pending")
                    continue
                extraction = self.extractions.get(disposition["job_id"])
                if (disposition["job_id"] not in self.accepted or extraction is None or
                        not any(m["source_version_id"] == vid for m in self.members[extraction["batch_id"]])):
                    self.issue("source_disposition_receipt_missing")
                if disposition["status"] not in {"observed", "context_only", "repetition_only", "noise", "unsupported_type"} or not disposition["reason"]:
                    self.issue("source_disposition_invalid")
                if disposition["status"] == "observed" and vid not in self.observed_versions:
                    self.issue("source_observation_missing")
        if known_members != self.sources.keys():
            self.issue("unaccounted_source_version")
        for row in self.sources.values():
            if row["content_hash"] != _hash({"text": row["text"], "payload": _load(row["payload_json"]),
                                            "kind": row["kind"], "grade": row["provenance_grade"],
                                            "sent_at": row["source_sent_at"],
                                            "resources": self.source_resources[row["source_version_id"]]}):
                self.issue("source_payload_identity_mismatch")
        for jid, accepted in self.accepted.items():
            if _hash(_load(accepted["artifact_json"])) != accepted["artifact_hash"]:
                self.issue("accepted_artifact_identity_mismatch")
            job = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (jid,)).fetchone()
            if (job is None or job["state"] not in {"accepted", "done"} or job["job_key"] !=
                    _hash({"generation_id": self.generation, "kind": job["kind"],
                           "inputs": _load(job["inputs_json"]), "versions": _load(job["versions_json"])})):
                self.issue("accepted_job_identity_mismatch")
            if job is not None and job["kind"] == "extract":
                self._verify_extraction_inputs(job)
        for jid, row in self.extractions.items():
            receipt = _load(row["receipt_json"])
            if (receipt.get("receipt_id") != row["receipt_id"] or receipt.get("job_id") != jid
                    or receipt.get("batch_id") != row["batch_id"] or receipt.get("generation_id") != self.generation
                    or set(receipt.get("observation_ids", ())) - self.observations.keys()):
                self.issue("extraction_receipt_invalid")

    def _verify_extraction_inputs(self, job):
        inputs, versions = _load(job["inputs_json"]), _load(job["versions_json"])
        version = versions.get("prompt")
        if version not in {EXTRACTION_PROMPT_VERSION, "semantic-extraction-v1"}:
            self.issue("extraction_prompt_version_conflict")
            return
        attributed = version == EXTRACTION_PROMPT_VERSION
        members = inputs.get("members")
        actual = {r["source_version_id"]: r for r in self.members[inputs.get("batch_id")]}
        if (not isinstance(members, list) or any(not isinstance(m, dict) for m in members)
                or len(members) != len(actual) or {m.get("source_id") for m in members} != actual.keys()):
            self.issue("extraction_input_mismatch")
            return
        self.extraction_inputs[job["job_id"]] = {m["source_id"]: {field: m.get(field)
            for field in ("subject_id", "case_id", "speaker_id")} for m in members}
        for member in members:
            source = self.sources.get(member["source_id"])
            if source is None:
                self.issue("extraction_input_mismatch")
                continue
            metadata = captured_source_metadata(source["source_namespace"], source["chat_id"],
                                                 _load(source["payload_json"]), legacy=not attributed)
            if (member.get("text") != source["text"] or type(member.get("core")) is not bool
                    or member["core"] != bool(actual[member["source_id"]]["core"])
                    or type(member.get("occurred_at")) not in (float, int)
                    or member["occurred_at"] != source["source_sent_at"]
                    or any(member.get(key) != value for key, value in metadata.items())
                    or attributed and not {"speaker_id", "speaker_name"} <= member.keys()
                    or not attributed and any(member.get(key) is not None for key in ("speaker_id", "speaker_name"))):
                self.issue("extraction_input_mismatch")

    def _verify_source_versions(self):
        missing_head = self.db.execute("SELECT 1 FROM source_heads h WHERE NOT EXISTS (SELECT 1 FROM source_records s "
            "WHERE s.source_namespace=h.source_namespace AND s.chat_id=h.chat_id AND s.source_id=h.source_id) LIMIT 1").fetchone() is not None
        key, previous, position, head = None, None, 0, None
        # Only lineage metadata crosses this traversal, never source text/payload.
        for source in self.db.execute("SELECT source_namespace,chat_id,source_id,source_version_id,revision,"
                "supersedes_source_version_id,content_hash,native_revision FROM source_records "
                "ORDER BY source_namespace,chat_id,source_id,revision"):
            current = (source["source_namespace"], source["chat_id"], source["source_id"])
            if current != key:
                if previous is not None and head != previous["source_version_id"]:
                    self.issue("source_head_not_latest")
                key, previous, position = current, None, 0
                row = self.db.execute("SELECT source_version_id FROM source_heads WHERE source_namespace=? AND chat_id=? AND source_id=?", key).fetchone()
                head = row[0] if row else None
                missing_head |= row is None
            position += 1
            if (source["revision"] != position or source["supersedes_source_version_id"] !=
                    (previous["source_version_id"] if previous else None)):
                self.issue("source_version_lineage_invalid")
            if (previous is not None and source["content_hash"] == previous["content_hash"]
                    and source["native_revision"] == previous["native_revision"]):
                self.issue("source_repetition_created_version")
            previous = source
        if previous is not None and head != previous["source_version_id"]:
            self.issue("source_head_not_latest")
        if missing_head:
            self.issue("source_head_set_mismatch")

    def _verify_evidence(self):
        for sid, span in self.spans.items():
            source = self.sources.get(span["source_version_id"])
            if source is None:
                self.issue("evidence_source_missing")
                continue
            visible = redact_urls_in_text(source["text"])
            start, end = span["start"], span["end"]
            mapping = _load(span["mapping_json"])
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(visible)
                    or visible[start:end] != span["excerpt"] or _hash(span["excerpt"]) != span["excerpt_hash"]
                    or not 0 <= span["raw_start"] < span["raw_end"] <= len(source["text"])
                    or not mapping or mapping[0]["visible_start"] != start or mapping[-1]["visible_end"] != end):
                self.issue("evidence_span_invalid")
        for oid, observation in self.observations.items():
            spans = self.evidence[oid]
            extraction = self.extractions.get(observation["accepted_job_id"])
            if not spans or not spans <= self.spans.keys() or extraction is None:
                self.issue("observation_evidence_missing")
                continue
            core = {m["source_version_id"] for m in self.members[extraction["batch_id"]] if m["core"]}
            if not any(self.spans[s]["source_version_id"] in core for s in spans):
                self.issue("observation_core_evidence_missing")
            if (observation["resolution_status"] == "unresolved" or
                    not self.db.execute("SELECT 1 FROM item_observations WHERE observation_id=? LIMIT 1", (oid,)).fetchone()):
                self.issue("observation_resolution_pending", "semantic_pending")
            units = self.db.execute("SELECT * FROM content_units WHERE observation_id=?", (oid,)).fetchall()
            if not units:
                self.issue("observation_content_missing")
            artifact = self.accepted.get(observation["accepted_job_id"])
            accepted_observations = _load(artifact["artifact_json"]).get("observations", []) if artifact else []
            accepted = next((o for o in accepted_observations if o.get("observation_id") == oid), None)
            expected_units = [{"unit_id": u["unit_id"], "kind": u["kind"], "text": u["text"],
                               "span_ids": _load(u["span_ids_json"])} for u in units]
            expected_spans = [{"span_id": s, "source_id": self.spans[s]["source_version_id"],
                               "start": self.spans[s]["start"], "end": self.spans[s]["end"],
                               "excerpt": self.spans[s]["excerpt"]} for s in sorted(spans)]
            if (accepted is None or any(accepted.get(k) != observation[k] for k in ("title", "scene_kind"))
                    or accepted.get("topics", []) != _load(observation["topics_json"])
                    or sorted(accepted.get("units", []), key=lambda u: u["unit_id"]) != sorted(expected_units, key=lambda u: u["unit_id"])
                    or sorted(accepted.get("spans", []), key=lambda s: s["span_id"]) != expected_spans):
                self.issue("observation_accepted_artifact_mismatch")
            for unit in units:
                cited = _load(unit["span_ids_json"])
                if not cited or not set(cited) <= spans or not unit["text"].strip():
                    self.issue("content_unit_evidence_invalid")
                if redact_urls_in_text(unit["text"]) != unit["text"]:
                    self.issue("semantic_url_unredacted")
        if self._has("claims"):
            for claim in self._rows("claims"):
                spans = _load(claim["span_ids_json"])
                if not spans or not set(spans) <= self.spans.keys():
                    self.issue("claim_evidence_invalid")
                    continue
                source_ids = {self.spans[s]["source_version_id"] for s in spans}
                jobs = self.db.execute("SELECT o.accepted_job_id FROM observation_claims c "
                    "JOIN observations o USING(observation_id) WHERE c.claim_id=?", (claim["claim_id"],)).fetchall()
                for job in jobs:
                    inputs = self.extraction_inputs.get(job[0], {})
                    cited = [inputs[s] for s in source_ids if s in inputs]
                    for field in ("subject_id", "case_id"):
                        allowed = {m.get(field) for m in cited}
                        if field == "subject_id":
                            allowed.update(m.get("speaker_id") for m in cited)
                        if claim[field] is not None and claim[field] not in allowed:
                            self.issue("claim_unprovided_anchor")

    def _verify_proposal_supersessions(self, jobs, decisions):
        """Independently prove obsolete resolver plans against committed rows."""
        valid = set()
        if not self._has("proposal_supersessions"):
            return valid
        for row in self._rows("proposal_supersessions"):
            try:
                proof = _load(row["proof_json"])
                old_id, new_id = row["job_id"], row["replacement_job_id"]
                old_job, new_job = jobs[old_id], jobs[new_id]
                old, new = [_load(self.accepted[identity]["artifact_json"]) for identity in (old_id, new_id)]
                old_inputs, new_inputs = [_load(job["inputs_json"]) for job in (old_job, new_job)]
                scopes = [{"batch_id": inputs["batch_id"],
                           "observation_ids": sorted(value["observation_id"] for value in inputs["state"]["observations"])}
                          for inputs in (old_inputs, new_inputs)]
                scope = scopes[0]
                if (old_id == new_id or old_job["kind"] != "propose" or new_job["kind"] != "propose"
                        or scopes[0] != scopes[1] or not scope["observation_ids"]
                        or len(set(scope["observation_ids"])) != len(scope["observation_ids"])
                        or scope["batch_id"] not in self.batches or old_id in decisions
                        or bool(self.actions.values().where("job_id=?", (old_id,)))
                        or bool(self.transactions.values().where("job_id=?", (old_id,)))):
                    raise ValueError
                old_read, new_read = old["read_set"], new["read_set"]
                for read, inputs in ((old_read, old_inputs), (new_read, new_inputs)):
                    if (set(read) != {"store_revision", "items", "views"}
                            or type(read["store_revision"]) is not int or read["store_revision"] < 0
                            or not isinstance(read["items"], dict) or not isinstance(read["views"], dict)
                            or inputs["state"]["read_set"] != read):
                        raise ValueError
                if old_read["store_revision"] >= new_read["store_revision"]:
                    raise ValueError
                old_actions = old.get("actions", old.get("proposal", {}).get("actions"))
                new_actions = new.get("actions", new.get("proposal", {}).get("actions"))
                if not old_actions or not new_actions:
                    raise ValueError
                decision = decisions[new_id]
                tx, receipt = decision["transaction_id"], _load(decision["receipt_json"])
                transaction = self.transactions[tx]
                actual_actions = [self.actions[value["action_key"]] for value in new_actions]
                if (transaction["job_id"] != new_id or _load(transaction["actions_json"]) != new_actions
                        or _load(decision["read_set_json"]) != new_read
                        or receipt.get("receipt_id") != tx or receipt.get("transaction_id") != tx
                        or receipt.get("job_id") != new_id or receipt.get("generation_id") != self.generation
                        or receipt.get("store_revision") != new_read["store_revision"] + 1
                        or receipt.get("input_fingerprint") != _hash([new_actions, new_read, _load(decision["decision_json"]) or None])
                        or any(value["job_id"] != new_id or value["transaction_id"] != tx
                               or _load(value["action_json"]) != action for value, action in zip(actual_actions, new_actions))):
                    raise ValueError
                terminal = [{"job_id": old_id, "artifact_hash": _hash(old)}]
                for job in jobs.values():
                    inputs = _load(job["inputs_json"])
                    if job["kind"] != "decision" or inputs.get("proposal_job_id") != old_id:
                        continue
                    if inputs.get("proposal") != old or job["job_id"] not in self.accepted:
                        raise ValueError
                    terminal.append({"job_id": job["job_id"], "artifact_hash": _hash(_load(self.accepted[job["job_id"]]["artifact_json"]))})
                for value in terminal:
                    job = jobs[value["job_id"]]
                    if job["state"] != "done" or job["receipt_id"] != row["receipt_id"]:
                        raise ValueError
                memberships, lineage, transactions = [], [], {tx}
                frozen = {value["observation_id"]: value for value in proof["observation_memberships"]}
                if set(frozen) != set(scope["observation_ids"]) or len(frozen) != len(proof["observation_memberships"]):
                    raise ValueError
                for oid in scope["observation_ids"]:
                    observation, binding = self.observations[oid], frozen[oid]
                    if (observation["resolution_status"] != "resolved"
                            or not self.db.execute("SELECT 1 FROM item_observations m JOIN items i USING(item_id) "
                                "WHERE m.observation_id=? AND i.lifecycle='active' LIMIT 1", (oid,)).fetchone()):
                        raise ValueError
                    extraction = self.extractions[observation["accepted_job_id"]]
                    sources = sorted({self.spans[s]["source_version_id"] for s in self.evidence[oid]})
                    if not sources or extraction["batch_id"] != scope["batch_id"]:
                        raise ValueError
                    lineage.append({"observation_id": oid, "extraction_job_id": observation["accepted_job_id"],
                                    "extraction_receipt_id": extraction["receipt_id"], "source_version_ids": sources})
                    historical = self.db.execute("SELECT * FROM item_revisions WHERE item_id=? AND revision=?",
                                                 (binding["item_id"], binding["revision"])).fetchone()
                    if historical is None or oid not in _load(historical["snapshot_json"])["observation_ids"]:
                        raise ValueError
                    memberships.append({"observation_id": oid, "item_id": binding["item_id"],
                                        "revision": binding["revision"], "transaction_id": historical["transaction_id"]})
                    transactions.update((historical["transaction_id"], extraction["receipt_id"]))
                outboxes = sorted({identity for tx_id in transactions
                                   for identity in _load(self.transactions[tx_id]["outbox_ids_json"])})
                if any(identity not in self.outboxes for identity in outboxes):
                    raise ValueError
                expected = {"schema": "we-groupchat-obsidian.proposal-supersession.v1", "generation_id": self.generation,
                            "reason": "semantic_stale_read_set", "job_id": old_id, "replacement_job_id": new_id,
                            "original_artifact_hash": _hash(old), "replacement_artifact_hash": _hash(new),
                            "original_read_set": old_read, "replacement_read_set": new_read, "scope": scope,
                            "replacement_receipt_id": tx, "terminal_jobs": sorted(terminal, key=lambda value: value["job_id"]),
                            "source_lineage": lineage, "observation_memberships": memberships,
                            "canonical_receipt_ids": sorted(transactions), "outbox_ids": outboxes}
                if (proof != expected or row["receipt_id"] != "supersession_" + _hash(proof)
                        or row["transaction_id"] != tx):
                    raise ValueError
                valid.add(old_id)
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                self.issue("proposal_supersession_unproved")
        self.counts["superseded_proposals"] = len(valid)
        return valid

    def _verify_actions(self):
        decisions = RowMap(self.db, "SELECT * FROM decision_log", "job_id")
        jobs = RowMap(self.db, "SELECT * FROM jobs", "job_id")
        for job in jobs.values():
            if job["kind"] in {"propose", "decision"} and job["state"] == "unknown":
                self.issue("proposal_outcome_unknown", "semantic_pending")
        superseded = self._verify_proposal_supersessions(jobs, decisions)
        for job_id, artifact in self.accepted.items():
            if job_id in superseded:
                # The replacement may legitimately reuse an uncommitted key.
                # Its receipt belongs to that new plan; the entire old plan is
                # discharged only by the independently reconstructed proof.
                continue
            value = _load(artifact["artifact_json"])
            proposal = value.get("proposal", value) if isinstance(value, dict) else {}
            actions = proposal.get("actions", ()) if isinstance(proposal, dict) else ()
            for action in actions:
                receipt = self.actions.get(action.get("action_key"))
                if receipt is None:
                    self.issue("accepted_action_pending", "semantic_pending")
                elif receipt["job_id"] != job_id or _load(receipt["action_json"]) != action:
                    self.issue("accepted_action_binding_mismatch")
        for key, row in self.actions.items():
            action, receipt = _load(row["action_json"]), _load(row["receipt_json"])
            if (action.get("action_key") != key or row["input_fingerprint"] != _hash(action)
                    or row["job_id"] not in self.accepted or row["job_id"] not in decisions
                    or receipt.get("transaction_id") != row["transaction_id"]
                    or receipt.get("receipt_id") != row["transaction_id"]
                    or receipt.get("outbox_id") != row["transaction_id"]
                    or receipt.get("generation_id") != self.generation):
                self.issue("action_receipt_invalid")
        for job_id, decision in decisions.items():
            rows = list(Rows(self.db, "SELECT * FROM action_receipts WHERE job_id=?", (job_id,)))
            if not rows:
                self.issue("accepted_action_receipt_missing")
            receipt = _load(decision["receipt_json"])
            for item_id in receipt.get("affected_item_ids", ()):
                if item_id not in self.items:
                    self.issue("action_item_missing")
                elif not self.db.execute("SELECT 1 FROM projection_outbox WHERE owner_id=? AND transaction_id=?",
                                         (item_id, decision["transaction_id"])).fetchone():
                    # A later navigation transaction can supersede the same
                    # revision row, but its immutable Item revision still binds
                    # the original transaction and a current outbox must exist.
                    revision = self.db.execute("SELECT revision FROM item_revisions WHERE item_id=? AND transaction_id=?",
                                               (item_id, decision["transaction_id"])).fetchone()
                    if not revision or not self.db.execute("SELECT 1 FROM projection_outbox WHERE owner_id=? AND revision>=?",
                                                          (item_id, revision[0])).fetchone():
                        self.issue("action_projection_outbox_missing")
        for row in self._rows("item_revisions"):
            if row["transaction_id"] not in self.transactions:
                self.issue("item_revision_receipt_missing")
            if not self.db.execute("SELECT 1 FROM index_outbox WHERE owner_type='item' AND owner_id=? AND revision=?",
                                   (row["item_id"], row["revision"])).fetchone():
                self.issue("item_index_outbox_missing")
        for item in self.items.values():
            if not self.db.execute("SELECT 1 FROM item_revisions WHERE item_id=? AND revision=?",
                                   (item["item_id"], item["content_revision"])).fetchone():
                self.issue("item_current_revision_missing")

    def _verify_transactions(self):
        for outbox in self.outboxes.values():
            if outbox["status"] not in {"pending", "done", "superseded"}:
                self.issue("outbox_status_invalid")
            elif outbox["status"] == "done" and not outbox["receipt_id"]:
                self.issue("outbox_receipt_missing")
            elif outbox["status"] == "superseded" and (outbox["kind"] != "projection" or
                    not outbox["receipt_id"] or not self.outboxes.values().where("kind='projection' AND owner_id=? AND revision>? AND status='done' AND receipt_id=?",
                        (outbox["owner_id"], outbox["revision"], outbox["receipt_id"]))):
                self.issue("outbox_supersession_unproved")
        for tx, row in self.transactions.items():
            actions, outboxes = _load(row["actions_json"]), _load(row["outbox_ids_json"])
            if (not isinstance(actions, list) or not actions or any(not isinstance(a, dict) or not a.get("type") for a in actions)
                    or not isinstance(outboxes, list) or len(outboxes) != len(set(outboxes))):
                self.issue("canonical_transaction_invalid")
                continue
            if not set(outboxes) <= self.outboxes.keys():
                self.issue("canonical_outbox_missing")
            if actions[0]["type"] == "COMPLETE_BATCH":
                if not self.processing.values().where("transaction_id=?", (tx,)):
                    self.issue("processing_transaction_orphaned")
                continue
            if actions[0]["type"] == "MIGRATION":
                migration = self.db.execute("SELECT receipt_json FROM semantic_migration_receipts WHERE receipt_id=?",
                                            (tx,)).fetchone() if self._has("semantic_migration_receipts") else None
                receipt = _load(migration[0]) if migration else {}
                processing = RowMap(self.db, "SELECT * FROM processing_receipts", "receipt_id")
                references = receipt.get("processing_receipt_ids", [])
                if (not migration or receipt.get("semantic_generation_id") != self.generation
                        or any(receipt.get(k) != tx for k in ("receipt_id", "migration_id", "canonical_receipt_id", "outbox_receipt_id"))
                        or len(references) != len(set(references)) or not set(references) <= processing.keys()):
                    self.issue("migration_receipt_invalid")
                    continue
                capture_ids, borrowed_ids = [], set()
                for pid in references:
                    proof = _load(processing[pid]["receipt_json"])
                    capture_ids.extend(proof["capture_receipt_ids"])
                    borrowed_ids.update(proof["outbox_ids"])
                borrowed = sorted(borrowed_ids)
                expected = {"type": "MIGRATION", "receipt_id": tx,
                            "processing_receipt_ids": references, "capture_receipt_ids": capture_ids}
                if receipt.get("capture_receipt_ids") != capture_ids or actions != [expected] or outboxes != borrowed:
                    self.issue("migration_proof_membership_mismatch")
                continue
            actual = {o["outbox_id"] for o in self.outboxes.values().where("transaction_id=?", (tx,))}
            if set(outboxes) != actual:
                self.issue("canonical_outbox_membership_mismatch")
            if row["job_id"] is not None:
                artifact_row = self.accepted.get(row["job_id"])
                if artifact_row is None:
                    self.issue("canonical_accepted_artifact_missing")
                    continue
                artifact = _load(artifact_row["artifact_json"])
                if actions[0]["type"] == "COMMIT_EXTRACTION":
                    extraction = self.extractions.get(row["job_id"])
                    receipt = _load(extraction["receipt_json"]) if extraction else {}
                    expected = {"type": "COMMIT_EXTRACTION", "batch_id": receipt.get("batch_id"),
                                "observation_ids": receipt.get("observation_ids"), "dispositions": receipt.get("dispositions")}
                    if (not extraction or extraction["receipt_id"] != tx or actions != [expected]
                            or receipt.get("outbox_ids") != outboxes):
                        self.issue("extraction_transaction_invalid")
                else:
                    expected = artifact.get("actions", artifact.get("proposal", {}).get("actions"))
                    persisted = [_load(a[0]) for a in self.db.execute(
                        "SELECT action_json FROM action_receipts WHERE transaction_id=? ORDER BY rowid", (tx,))]
                    if expected != actions or persisted != actions:
                        self.issue("canonical_action_membership_mismatch")
            for revision in self.db.execute("SELECT * FROM item_revisions WHERE transaction_id=?", (tx,)):
                for kind in ("projection", "index"):
                    if not self.outboxes.values().where("kind=? AND owner_id=? AND revision=? AND transaction_id=?",
                            (kind, revision["item_id"], revision["revision"], tx)):
                        self.issue("item_transaction_outbox_missing")
                if not self.db.execute("SELECT 1 FROM change_events WHERE item_id=? AND revision=? AND transaction_id=?",
                    (revision["item_id"], revision["revision"], tx)).fetchone():
                    self.issue("item_change_receipt_missing")

    def _verify_processing_proof(self, batch, receipt):
        proof = receipt.get("proof")
        if not isinstance(proof, dict):
            self.issue("processing_proof_missing")
            return
        core, observed = [], set()
        extraction_ids = {r[0] for r in self.db.execute("SELECT receipt_id FROM extraction_receipts WHERE batch_id=?", (batch["batch_id"],))}
        if not extraction_ids:
            self.issue("processing_extraction_receipt_missing")
        for member in self.members[batch["batch_id"]]:
            if not member["core"]:
                continue
            vid = member["source_version_id"]
            disposition = self.dispositions.get(vid)
            owner = self.db.execute("SELECT job_id FROM source_core_jobs WHERE source_version_id=?", (vid,)).fetchone()
            extraction = self.extractions.get(owner[0]) if owner else None
            if disposition is None or extraction is None or disposition["job_id"] != owner[0]:
                self.issue("processing_core_evidence_missing")
                continue
            status = {key: disposition[key] for key in ("status", "reason")}
            if _load(extraction["receipt_json"]).get("dispositions", {}).get(vid) != status:
                self.issue("processing_disposition_mismatch")
            extraction_ids.add(extraction["receipt_id"])
            source_observations = {oid for oid, obs in self.observations.items() if obs["accepted_job_id"] == owner[0]
                                   and any(self.spans[s]["source_version_id"] == vid for s in self.evidence[oid] if s in self.spans)}
            if bool(source_observations) != (status["status"] == "observed"):
                self.issue("processing_observation_mismatch")
            observed.update(source_observations)
            core.append({"source_version_id": vid, "job_id": owner[0], **status,
                         "extraction_receipt_id": extraction["receipt_id"]})
        memberships = proof.get("observation_memberships", [])
        if (len(memberships) != len({m["observation_id"] for m in memberships})
                or {m["observation_id"] for m in memberships} != observed):
            self.issue("processing_observation_membership_mismatch")
        transactions = set(extraction_ids)
        for member in memberships:
            historical = self.db.execute("SELECT * FROM item_revisions WHERE item_id=? AND revision=?",
                                         (member["item_id"], member["revision"])).fetchone()
            if (historical is None or member["transaction_id"] != historical["transaction_id"]
                    or member["observation_id"] not in _load(historical["snapshot_json"]).get("observation_ids", ())):
                self.issue("processing_historical_membership_missing")
            transactions.add(member["transaction_id"])
        outboxes = set()
        for tx in transactions:
            row = self.transactions.get(tx)
            if row is None:
                self.issue("processing_canonical_receipt_missing")
            else:
                outboxes.update(_load(row["outbox_ids_json"]))
        expected = {"core_sources": core, "observation_memberships": memberships,
                    "extraction_receipt_ids": sorted(extraction_ids), "canonical_receipt_ids": sorted(transactions),
                    "outbox_ids": sorted(outboxes)}
        if (proof != expected or receipt.get("proof_fingerprint") != _hash(expected)
                or any(receipt.get(k) != expected[k] for k in ("extraction_receipt_ids", "canonical_receipt_ids", "outbox_ids"))):
            self.issue("processing_proof_mismatch")
        tx = self.transactions.get(receipt.get("transaction_id"))
        action = {"type": "COMPLETE_BATCH", "batch_id": batch["batch_id"], "proof_fingerprint": _hash(expected)}
        if tx is None or _load(tx["actions_json"]) != [action] or _load(tx["outbox_ids_json"]) != expected["outbox_ids"]:
            self.issue("processing_transaction_mismatch")

    def _verify_resources(self):
        expected = set()
        for oid, spans in self.evidence.items():
            source_ids = {self.spans[s]["source_version_id"] for s in spans if s in self.spans}
            resources = {_json(r) for vid in source_ids for r in self.source_resources[vid]}
            expected.update((oid, None, r) for r in resources)
            for item, observations in self.item_observations.items():
                if oid in observations:
                    expected.update((oid, item, r) for r in resources)
        actual = set()
        for row in self._rows("resource_refs"):
            resource = {k: row[k] for k in ("resource_id", "occurrence_id", "purpose", "status", "location")}
            resource["metadata"] = _load(row["metadata_json"])
            actual.add((row["observation_id"], row["item_id"], _json(resource)))
        if expected != actual:
            self.issue("resource_evidence_conservation_mismatch")

    @staticmethod
    def _vector(value):
        if not isinstance(value, dict) or not value:
            raise ValueError()
        result = {}
        for shard, row in value.items():
            token = _load(row["cursor_token"])
            if (not isinstance(token, list) or len(token) != 2
                    or any(type(x) is not int or x < 0 for x in token)
                    or not isinstance(row["generation_id"], str) or not row["generation_id"]):
                raise ValueError()
            result[shard] = (row["generation_id"], tuple(token))
        return result

    def _verify_prefix(self):
        inventory = self.manifest.get("source_inventory", {})
        if (inventory.get("complete") is not True or not inventory.get("digest")
                or type(inventory.get("revision")) is not int):
            self.issue("source_inventory_incomplete", "source_pending")
        passes = self._verify_capture_passes()
        legacy_passes = self.manifest.get("legacy_capture_passes", [])
        declared_legacy = set()
        for receipt in legacy_passes:
            ids = receipt.get("batch_ids", [])
            if (not receipt.get("receipt_id") or not ids or len(ids) != len(set(ids))
                    or any(bid not in self.batches or self.batches[bid]["purpose"] != "legacy_import"
                           or bid in declared_legacy for bid in ids)):
                self.issue("legacy_capture_pass_invalid")
                continue
            first, last = self.batches[ids[0]], self.batches[ids[-1]]
            if (any(v[1] != (0, 0) for v in self._vector(_load(first["input_cursors_json"])).values())
                    or last["source_eof"] != 1):
                self.issue("legacy_capture_pass_incomplete")
            for bid in ids:
                passes[bid] = {"receipt_id": "legacy:" + receipt["receipt_id"], "h1": []}
                declared_legacy.add(bid)
        if legacy_passes and any(row["purpose"] == "legacy_import" and bid not in declared_legacy
                                 for bid, row in self.batches.items()):
            self.issue("legacy_capture_pass_undeclared")
        first_batch = {}
        for bid in self.batches:
            for member in self.members[bid]:
                first_batch.setdefault(member["source_version_id"], bid)
        groups = defaultdict(list)
        for batch in self.batches.values():
            pass_id = passes.get(batch["batch_id"], {}).get("receipt_id", "continuous")
            groups[(batch["source_namespace"], batch["chat_id"], pass_id)].append(batch["batch_id"])
        h1 = self.manifest.get("h1")
        if not isinstance(h1, list):
            self.issue("source_h1_missing", "source_pending")
            h1 = []
        expected = {(r["source_namespace"], r["chat_id"]): r for r in h1}
        live_groups = {key[:2] for key, rows in groups.items() if any(self.batches[bid]["purpose"] != "legacy_import" for bid in rows)}
        if len(expected) != len(h1) or expected.keys() != live_groups:
            self.issue("source_h1_chat_set_mismatch", "source_pending")
        positions = {bid: position for position, bid in enumerate(self.batches)}
        ordered_groups = sorted(groups.items(), key=lambda pair: positions[pair[1][-1]])
        last_group = {key[:2]: key for key, rows in ordered_groups if any(self.batches[bid]["purpose"] != "legacy_import" for bid in rows)}
        for key, batches in groups.items():
            previous, applied = None, None
            gap = False
            seen_versions = set()
            for bid in batches:
                batch = self.batches[bid]
                incoming, outgoing = _load(batch["input_cursors_json"]), _load(batch["output_cursors_json"])
                a, b = self._vector(incoming), self._vector(outgoing)
                if a.keys() != b.keys() or any(a[s][0] != b[s][0] or a[s][1] > b[s][1] for s in a):
                    self.issue("capture_cursor_order_invalid")
                if previous is not None and previous != incoming:
                    self.issue("capture_prefix_gap")
                for member in self.members[batch["batch_id"]]:
                    vid = member["source_version_id"]
                    source = self.sources.get(vid)
                    if source is None or not member["core"] or vid in seen_versions or first_batch[vid] != batch["batch_id"]:
                        continue
                    shard = source["logical_shard_id"]
                    token = _load(source["source_cursor_token"])
                    if (shard not in a or shard not in b or not isinstance(token, list) or len(token) != 2
                            or any(type(x) is not int or x < 0 for x in token)
                            or source["physical_generation"] != b[shard][0]
                            or not a[shard][1] <= tuple(token) <= b[shard][1]):
                        self.issue("capture_member_cursor_mismatch")
                    seen_versions.add(vid)
                previous = outgoing
                processing = self.processing.get(batch["batch_id"])
                if processing is None:
                    gap = True
                    self.issue("processing_receipt_missing", "semantic_pending")
                else:
                    receipt = _load(processing["receipt_json"])
                    if (receipt.get("receipt_id") != processing["receipt_id"]
                            or receipt.get("transaction_id") != processing["transaction_id"]
                            or receipt.get("outbox_id") != processing["outbox_id"]
                            or receipt.get("capture_receipt_ids") != [batch["receipt_id"]]
                            or receipt.get("input_cursors") != incoming or receipt.get("output_cursors") != outgoing
                            or receipt.get("batch_id") != batch["batch_id"]
                            or receipt.get("generation_id") != self.generation):
                        self.issue("processing_receipt_invalid")
                    self._verify_processing_proof(batch, receipt)
                    if gap:
                        self.issue("applied_prefix_gap")
                    else:
                        applied = outgoing
                expected_inventory = passes.get(batch["batch_id"], {}).get("source_inventory", inventory)
                if batch["purpose"] != "legacy_import" and (
                        batch["inventory_digest"] != expected_inventory.get("digest") or
                        batch["inventory_revision"] != expected_inventory.get("revision")):
                    self.issue("source_inventory_binding_mismatch")
            if applied is not None:
                a, b = self._vector(applied), self._vector(previous)
                if a.keys() != b.keys() or any(a[s][0] != b[s][0] or a[s][1] > b[s][1] for s in a):
                    self.issue("applied_exceeds_captured")
            tail = self.batches[batches[-1]]
            pass_receipt = passes.get(tail["batch_id"])
            pass_h1 = {(r["source_namespace"], r["chat_id"]): r for r in pass_receipt["h1"]} if pass_receipt else {}
            cutoff = expected.get(key[:2]) if last_group.get(key[:2]) == key else pass_h1.get(key[:2])
            if cutoff is not None:
                if (previous != cutoff["cursors"] or tail["source_eof"] != 1
                        or tail["inventory_digest"] != cutoff["inventory_digest"]
                        or tail["inventory_revision"] != cutoff["inventory_revision"]):
                    self.issue("source_h1_not_reached", "source_pending")
                if applied != cutoff["cursors"]:
                    self.issue("semantic_h1_not_applied", "semantic_pending")

    def _verify_capture_passes(self):
        """Permit only explicitly frozen complete snapshot passes to restart at zero."""
        passes = self.manifest.get("capture_passes", [])
        if not isinstance(passes, list):
            self.issue("capture_pass_manifest_invalid")
            return {}
        by_batch, identities = {}, set()
        sequence = list(self.batches)
        batch_positions = {bid: position for position, bid in enumerate(sequence)}
        previous_end = -1
        for receipt in passes:
            identity = receipt.get("receipt_id")
            if (not isinstance(identity, str) or not identity or identity in identities
                    or receipt.get("snapshot_id", identity) != identity
                    or receipt.get("generation_id") != self.generation):
                self.issue("capture_pass_identity_invalid")
                continue
            identities.add(identity)
            declared = receipt.get("batches", [])
            ids = [r["batch_id"] for r in declared]
            expected_set = {bid for bid, row in self.batches.items() if row["purpose"] == "snapshot:" + identity}
            if not ids or len(ids) != len(set(ids)) or set(ids) != expected_set:
                self.issue("capture_pass_batch_set_mismatch")
                continue
            positions = [batch_positions[bid] for bid in ids]
            if positions != sorted(positions) or positions[0] <= previous_end:
                self.issue("capture_pass_order_invalid")
            previous_end = positions[-1]
            valid = True
            for declared_batch in declared:
                batch = self.batches[declared_batch["batch_id"]]
                if declared_batch != _load(batch["receipt_json"]):
                    self.issue("capture_pass_receipt_mismatch")
                    valid = False
            if not valid:
                continue
            inventory = receipt.get("source_inventory", {})
            if inventory.get("complete") is not True or not inventory.get("digest") or type(inventory.get("revision")) is not int:
                self.issue("capture_pass_inventory_incomplete", "source_pending")
            groups = defaultdict(list)
            for bid in ids:
                row = self.batches[bid]
                groups[(row["source_namespace"], row["chat_id"])].append(bid)
                by_batch[bid] = receipt
            boundaries = receipt.get("h1", [])
            h1 = {(r["source_namespace"], r["chat_id"]): r for r in boundaries}
            if len(h1) != len(boundaries) or h1.keys() != groups.keys():
                self.issue("capture_pass_h1_chat_set_mismatch")
            for key, rows in groups.items():
                initial = _load(self.batches[rows[0]]["input_cursors_json"])
                if any(position[1] != (0, 0) for position in self._vector(initial).values()):
                    self.issue("capture_pass_initial_cursor_invalid")
                if any(_load(row.get("start_cursor", "[0,0]")) != [0, 0] for row in initial.values()):
                    self.issue("capture_pass_initial_cursor_invalid")
                boundary = h1.get(key)
                tail = self.batches[rows[-1]]
                if (boundary is None or tail["source_eof"] != 1
                        or _load(tail["output_cursors_json"]) != boundary["cursors"]
                        or tail["inventory_digest"] != boundary["inventory_digest"]
                        or tail["inventory_revision"] != boundary["inventory_revision"]):
                    self.issue("capture_pass_h1_not_reached", "source_pending")
            prior_versions = {member["source_version_id"] for bid in sequence[:positions[-1] + 1]
                              for member in self.members[bid]}
            maximum = max((self.sources[vid]["capture_seq"] for vid in prior_versions if vid in self.sources), default=0)
            if type(receipt.get("capture_seq")) is not int or receipt["capture_seq"] != maximum:
                self.issue("capture_pass_sequence_mismatch")
        if passes and (passes[-1].get("h1") != self.manifest.get("h1")
                       or passes[-1].get("source_inventory") != self.manifest.get("source_inventory")):
            self.issue("capture_pass_final_cutoff_mismatch")
        for receipt in self.manifest.get("abandoned_capture_passes", []):
            identity = receipt.get("receipt_id")
            declared = receipt.get("batches", [])
            ids = [row.get("batch_id") for row in declared]
            expected_set = {bid for bid, row in self.batches.items() if row["purpose"] == "snapshot:" + str(identity)}
            if (not isinstance(identity, str) or not identity or identity in identities
                    or receipt.get("generation_id") != self.generation or receipt.get("complete") is not False
                    or not ids or len(ids) != len(set(ids)) or set(ids) != expected_set):
                self.issue("abandoned_capture_pass_invalid")
                continue
            identities.add(identity)
            if passes and any(batch_positions[bid] >= batch_positions[passes[-1]["batches"][-1]["batch_id"]] for bid in ids):
                self.issue("capture_pass_final_order_invalid")
            if any(value != _load(self.batches[value["batch_id"]]["receipt_json"]) for value in declared):
                self.issue("abandoned_capture_pass_receipt_mismatch")
                continue
            if [batch_positions[bid] for bid in ids] != sorted(batch_positions[bid] for bid in ids):
                self.issue("abandoned_capture_pass_order_invalid")
            groups = defaultdict(list)
            for bid in ids:
                row = self.batches[bid]
                groups[(row["source_namespace"], row["chat_id"])].append(bid)
                by_batch[bid] = {"receipt_id": identity, "h1": [],
                    "source_inventory": {"complete": False, "digest": row["inventory_digest"], "revision": row["inventory_revision"]}}
            for rows in groups.values():
                if any(position[1] != (0, 0) for position in self._vector(_load(self.batches[rows[0]]["input_cursors_json"])).values()):
                    self.issue("abandoned_capture_pass_initial_cursor_invalid")
                if any(self.batches[bid]["inventory_digest"] != self.batches[rows[0]]["inventory_digest"]
                       or self.batches[bid]["inventory_revision"] != self.batches[rows[0]]["inventory_revision"] for bid in rows):
                    self.issue("abandoned_capture_pass_inventory_changed")
        if any(row["purpose"].startswith("snapshot:") and bid not in by_batch for bid, row in self.batches.items()):
            self.issue("capture_pass_undeclared_reset")
        return by_batch

    def _verify_index(self, items):
        if not items:
            return
        profile_id, index_id = self.manifest.get("embedding_profile_id"), self.manifest.get("index_generation_id")
        required = {"index_profiles", "embedding_records", "semantic_index_receipts"}
        if not required <= self.tables or not profile_id or not index_id:
            self.issue("embedding_profile_missing", "index_pending", len(items))
            return
        from ai.semantic_provider import EmbeddingProfile, ProviderError, validate_vector
        from ai.vector_index import IndexReceipt
        row = self.db.execute("SELECT * FROM index_profiles WHERE profile_id=? AND generation_id=?",
                              (profile_id, self.generation)).fetchone()
        if row is None:
            self.issue("embedding_profile_missing", "index_pending", len(items))
            return
        try:
            profile = EmbeddingProfile(**_load(row["profile_json"]))
            if profile.profile_id != profile_id:
                self.issue("embedding_profile_binding_mismatch")
        except (ProviderError, TypeError, ValueError):
            self.issue("embedding_profile_invalid")
            return
        confirmed = ScratchMap(self.scratch, "confirmed_vectors")
        for row in self.db.execute("SELECT * FROM semantic_index_receipts WHERE profile_id=? AND generation_id=? AND index_identity=? ORDER BY sequence",
                                   (profile_id, self.generation, index_id)):
            if row["status"] != "indexed" or row["receipt_json"] is None:
                continue
            try:
                receipt = IndexReceipt.from_dict(_load(row["receipt_json"]))
                submitted = _load(row["records_json"])
                if (not receipt.indexed or receipt.profile_id != profile_id
                        or receipt.generation_id != self.generation or receipt.index_identity != index_id
                        or {r.id for r in receipt.records} != {r["id"] for r in submitted}):
                    self.issue("dense_receipt_binding_mismatch")
                    continue
                for record in receipt.records:
                    confirmed[record.id] = validate_vector(record.values, profile)
            except (ProviderError, ValueError, TypeError):
                self.issue("dense_receipt_invalid")
        for item in items:
            oid = item["item_id"]
            # Match the actual canonical document order without asking the
            # retriever to declare its own readiness.
            texts = []
            for observation_id in sorted(self.item_content_observations[oid]):
                texts.extend(r[0] for r in self.db.execute("SELECT text FROM content_units WHERE observation_id=? ORDER BY rowid", (observation_id,)))
            body = redact_urls_in_text(item["title"] + "\n" + "\n".join(texts))
            document_id = "item:" + oid
            fingerprint = _hash([document_id, item["content_revision"], body])
            rows = self.db.execute("SELECT * FROM embedding_records WHERE profile_id=? AND generation_id=? AND document_id=? AND document_revision=? AND fingerprint=?",
                                    (profile_id, self.generation, document_id, item["content_revision"], fingerprint)).fetchall()
            if not rows:
                self.issue("item_embedding_pending", "index_pending")
                continue
            valid = False
            for record in rows:
                try:
                    vector = validate_vector(_load(record["artifact_json"])["vector"], profile)
                    if record["job_id"] not in self.accepted:
                        self.issue("embedding_artifact_receipt_missing")
                        continue
                    dense = confirmed.get(record["vector_id"])
                    if dense is not None and all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-7)
                                                 for a, b in zip(vector, dense)):
                        valid = True
                except (ProviderError, ValueError, TypeError):
                    self.issue("embedding_vector_invalid")
            if not valid:
                self.issue("item_dense_index_pending", "index_pending")

    def _projection_bytes(self, relative):
        if self.projection_root is None:
            raise LegacyError("projection_root_missing")
        root = _checked_path(self.projection_root, directory=True)
        return _read_regular(_under(root, relative), 64 * 1024 * 1024)

    def _projection_ready(self, object_id, revision, *, allow_personal=False):
        if not self._has("reader_projections"):
            return False
        row = self.db.execute("SELECT * FROM reader_projections WHERE generation_id=? AND object_id=?",
                              (self.generation, object_id)).fetchone()
        proposals = [] if not self._has("reader_proposals") else self.db.execute(
            "SELECT * FROM reader_proposals WHERE generation_id=? AND object_id=? AND content_revision=?",
            (self.generation, object_id, revision)).fetchall()
        if allow_personal:
            for proposal in proposals:
                if proposal["status"] != "keep_personal":
                    continue
                try:
                    original = self._projection_bytes(proposal["path"])
                    backup = self._projection_bytes(proposal["backup_path"])
                    draft = self._projection_bytes(proposal["draft_path"])
                    draft_text = draft.decode()
                    if (_bytes_hash(original) == proposal["file_hash"] == _bytes_hash(backup)
                            and region_body(draft_text, proposal["region"]) == proposal["proposed_body"]
                            and property_value(draft_text, "wgo_content_revision") == revision
                            and property_value(draft_text, "wgo_generation") == self.generation
                            and property_value(draft_text, "wgo_id") == object_id):
                        self.counts["user_preserved_conflicts"] += 1
                        return True
                except (LegacyError, ReadingError, UnicodeError):
                    continue
        if row is None or row["status"] != "ready" or row["content_revision"] != revision:
            return False
        if any(p["status"] == "pending" for p in proposals):
            return False
        try:
            _relative(row["path"])
            paths = getattr(self, "projection_id_paths", {}).get(object_id, ())
            if len(paths) != 1:
                return False
            raw = self._projection_bytes(paths[0])
            text = raw.decode()
            body = region_body(text, row["region"])
            if body is None:
                return False
            if redact_urls_in_text(body) != body:
                self.issue("projection_url_unredacted")
                return False
            return (property_value(text, "wgo_id") == object_id
                    and property_value(text, "wgo_generation") == self.generation
                    and property_value(text, "wgo_content_revision") == revision
                    and _bytes_hash(body.encode()) == row["machine_hash"])
        except (LegacyError, ReadingError, UnicodeError):
            return False

    def _verify_projections(self, items):
        self.projection_id_paths = defaultdict(list)
        self.scratch.execute("CREATE TABLE body_artifact_refs(item_id TEXT,request_id TEXT)")
        self.scratch.execute("CREATE INDEX body_artifact_item ON body_artifact_refs(item_id)")
        if self.manifest.get("rebuild_reading_required") and self._has("reader_text_artifacts"):
            for row in self.db.execute("SELECT * FROM reader_text_artifacts WHERE generation_id=?", (self.generation,)):
                if not row["artifact_json"]:
                    continue
                body = _load(row["artifact_json"])
                if isinstance(body, dict) and isinstance(body.get("item_id"), str) and "blocks" in body:
                    self.scratch.execute("INSERT INTO body_artifact_refs VALUES (?,?)", (body["item_id"], row["request_id"]))
        def body_artifacts(item_id):
            for reference in self.scratch.execute("SELECT request_id FROM body_artifact_refs WHERE item_id=?", (item_id,)):
                row = self.db.execute("SELECT * FROM reader_text_artifacts WHERE request_id=?", (reference[0],)).fetchone()
                yield row, _load(row["artifact_json"])
        if (items or self.manifest.get("legacy_inventory") or self.manifest.get("history_preservation_passes")
                or self.manifest.get("required_brief")) and self.projection_root is not None:
            try:
                root = _checked_path(self.projection_root, directory=True)
                for directory, dirs, files in os.walk(root, followlinks=False):
                    if any(Path(directory, name).is_symlink() for name in (*dirs, *files)):
                        raise LegacyError("projection_symlink")
                    dirs[:] = [name for name in dirs if name != ".wgo"]
                    for name in files:
                        if not name.endswith(".md"):
                            continue
                        path = Path(directory, name)
                        try:
                            body = _read_regular(path, 64 * 1024 * 1024).decode()
                            identity = property_value(body, "wgo_id")
                            if isinstance(identity, str) and identity:
                                self.projection_id_paths[identity].append(path.relative_to(root).as_posix())
                        except (ReadingError, UnicodeError):
                            continue  # Unknown notes do not acquire machine ownership.
                self.issue("projection_identity_collision", count=sum(len(paths) > 1 for paths in self.projection_id_paths.values()))
            except LegacyError:
                self.issue("projection_path_unsafe")
        for item in items:
            iid, revision = item["item_id"], item["content_revision"]
            if not self._projection_ready(iid, revision, allow_personal=True):
                self.issue("item_current_projection_missing", "projection_pending")
            if self.manifest.get("rebuild_reading_required"):
                plan = self.db.execute("SELECT plan_json FROM reader_body_plans WHERE generation_id=? AND item_id=? "
                    "AND content_revision=?", (self.generation, iid, revision)).fetchone() if self._has("reader_body_plans") else None
                matches = []
                expected = _load(plan[0]) if plan is not None else None
                if expected is not None and (expected.get("item_id") != iid or expected.get("content_revision") != revision):
                    self.issue("body_plan_binding_mismatch")
                    expected = None
                if expected is not None:
                    self._verify_body_responsibilities(iid, expected)
                for artifact, body in body_artifacts(iid) if expected is not None else ():
                    old_revision = body.get("content_revision")
                    # An unchanged body can be carried to a later Item revision
                    # without another provider call. Preserve exact blocks and
                    # unit fingerprints under the same generation/route.
                    if (type(old_revision) is int and 0 < old_revision <= revision
                            and body == {**expected, "content_revision": old_revision}
                            and artifact["provider_route"] == self.manifest.get("required_brief", {}).get("provider_route")):
                        matches.append({key: artifact[key] for key in ("status", "prompt_version")})
                if not matches or not any(a["status"] == "accepted" for a in matches):
                    self.issue("body_provider_artifact_missing", "semantic_pending")
                else:
                    versions = ({"body-plan-v15", "body-paged-v15"} if self.reading_policy == EDITORIAL_V2_READING_POLICY else
                        {"body-plan-v14", "body-paged-v14"} if self.reading_policy == EDITORIAL_READING_POLICY else
                        {"body-plan-v13", "body-paged-v13"} if self.reading_policy == SOURCE_READING_POLICY else
                        {"body-plan-v12", "body-paged-v12"} if self.reading_policy == CONTENT_READING_POLICY else {
                        "body-plan-v2", "body-paged-v2", "body-plan-v3", "body-paged-v3",
                        "body-plan-v4", "body-paged-v4", "body-plan-v5", "body-paged-v5",
                        "body-plan-v6", "body-paged-v6", "body-plan-v7", "body-paged-v7",
                        "body-plan-v8", "body-paged-v8", "body-plan-v9", "body-paged-v9",
                        "body-plan-v10", "body-paged-v10", "body-plan-v11", "body-paged-v11"})
                    if not any(a["status"] == "accepted" and a["prompt_version"] in versions for a in matches):
                        self.issue("body_provider_version_conflict", "semantic_pending")
            required_spans = set().union(*(self.evidence[oid] for oid in self.item_observations[iid]))
            if required_spans and not self._has("reader_source_locations"):
                self.issue("item_evidence_projection_missing", "projection_pending")
                continue
            for sid in required_spans:
                row = self.db.execute("SELECT page_number FROM reader_source_locations WHERE generation_id=? AND item_id=? AND evidence_id=?",
                                      (self.generation, iid, sid)).fetchone()
                if row is None or not self._projection_ready("source:" + iid + ":" + str(row[0]), revision):
                    self.issue("item_evidence_projection_missing", "projection_pending")
        if self._has("views"):
            for view in self._rows("views"):
                if not self._projection_ready("view:" + view["view_id"], view["definition_revision"] + view["override_revision"]):
                    self.issue("view_current_projection_missing", "projection_pending")

    def _verify_body_responsibilities(self, item_id, plan):
        """Check canonical obligations independently of the writing adapter.

        Fresh derived wording is editable; this proves unit homes, associations
        and typed status, not natural-language semantic completeness. Missing
        policy metadata retains the historical derived-literal guarantee.
        """
        blocks = plan["blocks"]
        units = [dict(row) for row in self.db.execute(
            "SELECT u.* FROM content_units u JOIN item_observations i USING(observation_id) "
            "WHERE i.item_id=? AND i.role<>'evidence_only' ORDER BY u.rowid", (item_id,))]
        homes_by_unit = defaultdict(list)
        for block in blocks:
            for unit_id in block["unit_ids"]:
                homes_by_unit[unit_id].append(block)
        assigned = Counter({unit_id: len(homes) for unit_id, homes in homes_by_unit.items()})
        if assigned != Counter(unit["unit_id"] for unit in units):
            self.issue("body_plan_unit_coverage")
        if any(block["primary_home"] != item_id for block in blocks):
            self.issue("body_plan_binding_mismatch")
        claims = [dict(row) for row in self.db.execute(
            "SELECT c.* FROM claims c JOIN item_claims i USING(claim_id) "
            "WHERE i.item_id=? AND c.status IN ('retracted','contested')", (item_id,))]
        first_by_observation = {}
        for unit in units:
            first_by_observation.setdefault(unit["observation_id"], unit["unit_id"])
            homes = homes_by_unit[unit["unit_id"]]
            if len(homes) != 1:
                continue
            home = homes[0]
            spans = set(_load(unit["span_ids_json"]))
            if not spans <= set(home["evidence_ids"]):
                self.issue("body_plan_evidence_missing")
            affected = [claim for claim in claims if spans.intersection(_load(claim["span_ids_json"]))]
            status = "retracted" if any(claim["status"] == "retracted" for claim in affected) else "contested" if affected else "current"
            statuses = home.get("unit_statuses", {})
            if ((status != "current" and (statuses.get(unit["unit_id"]) != status
                    or home["role"] not in {"correction", "historical", "counterevidence"}))
                    or unit["unit_id"] in statuses and statuses[unit["unit_id"]] != status):
                self.issue("body_plan_claim_status_missing")
            if (self.reading_policy == LEGACY_READING_POLICY
                    and (unit["kind"] in {"step", "counterevidence"} or affected)
                    and not _contains_verbatim(redact_urls_in_text(home["text"]), redact_urls_in_text(unit["text"]))):
                self.issue("body_plan_literal_obligation_missing")
            if first_by_observation[unit["observation_id"]] == unit["unit_id"]:
                resources = {row[0] for row in self.db.execute(
                    "SELECT resource_id FROM resource_refs WHERE observation_id=?", (unit["observation_id"],))}
                if not resources <= set(home.get("resource_ids", ())):
                    self.issue("body_plan_resource_missing")

    def _verify_brief(self):
        required = self.manifest.get("required_brief")
        if not required and not self.manifest.get("rebuild_reading_required"):
            return  # A bounded store-only verification has no edition cutoff.
        if not isinstance(required, dict) or not required.get("edition_id"):
            self.issue("required_brief_manifest_missing", "projection_pending")
            return
        tables = {"reader_brief_editions", "reader_brief_attempts", "reader_brief_inputs",
                  "reader_brief_history", "reader_preferences", "reader_known"}
        if not tables <= self.tables:
            self.issue("required_brief_missing", "projection_pending")
            return
        zone = ZoneInfo(required["timezone"])
        cutoff = datetime.fromisoformat(required["as_of"])
        if cutoff.tzinfo is None or cutoff.astimezone(zone).date().isoformat() != required["local_day"]:
            self.issue("brief_cutoff_invalid")
            return
        edition = self.db.execute("SELECT * FROM reader_brief_editions WHERE generation_id=? AND timezone=? AND local_day=?",
            (self.generation, required["timezone"], required["local_day"])).fetchone()
        if edition is None:
            self.issue("required_brief_missing", "projection_pending")
            return
        if edition["edition_id"] != required["edition_id"] or edition["content_revision"] != required["content_revision"]:
            self.issue("brief_edition_binding_mismatch")
        attempt = self.db.execute("SELECT * FROM reader_brief_attempts WHERE request_id=?", (edition["request_id"],)).fetchone()
        inputs = self.db.execute("SELECT * FROM reader_brief_inputs WHERE generation_id=? AND input_hash=?",
            (self.generation, edition["input_hash"])).fetchone()
        if (attempt is None or inputs is None or attempt["generation_id"] != self.generation
                or attempt["edition_id"] != edition["edition_id"] or attempt["input_hash"] != edition["input_hash"]):
            self.issue("brief_artifact_missing", "semantic_pending")
            return
        candidates, bindings = _load(inputs["inputs_json"]), _load(inputs["bindings_json"])
        if not isinstance(candidates, list) or not isinstance(bindings, dict):
            self.issue("brief_input_invalid")
            return
        hash_input = {"changes": candidates, "bindings": bindings, "generation": self.generation,
                      "day": required["local_day"], "timezone": required["timezone"],
                      "provider_route": required.get("provider_route"), "max_input_bytes": required.get("max_input_bytes")}
        input_versions = [version for version in ("brief-plan-v2", "brief-plan-v3")
                          if _hash({**hash_input, "prompt_version": version}) == edition["input_hash"]]
        if len(input_versions) != 1:
            self.issue("brief_input_identity_mismatch")
            return
        attributed = input_versions[0] == "brief-plan-v3"
        start = cutoff.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        changes = {}
        for row in self.db.execute("SELECT * FROM change_events WHERE created_at>=? AND created_at<=? ORDER BY change_id",
                                   (start, cutoff.timestamp())):
            changes[row["item_id"]] = dict(row)
        by_id = {c["id"]: c for c in candidates}
        if len(by_id) != len(candidates) or len({c["item_id"] for c in candidates}) != len(candidates):
            self.issue("brief_input_identity_duplicate")
        candidate_items = {c["item_id"] for c in candidates}
        if set(bindings) != candidate_items:
            self.issue("brief_input_binding_set_mismatch")
        eligible = set()
        by_item = {c["item_id"]: c for c in candidates}
        for iid, change in changes.items():
            item = self.items[iid]
            pref = self.db.execute("SELECT * FROM reader_preferences WHERE generation_id=? AND item_id=?", (self.generation, iid)).fetchone()
            known = self.db.execute("SELECT MAX(content_revision) FROM reader_known WHERE generation_id=? AND item_id=?", (self.generation, iid)).fetchone()[0] or 0
            if item["lifecycle"] != "active" or pref and pref["mute"] or known >= item["content_revision"]:
                if iid in candidate_items:
                    self.issue("brief_ignored_reader_disposition")
                continue
            current = self.db.execute("SELECT snapshot_json FROM item_revisions WHERE item_id=? AND revision=?", (iid, item["content_revision"])).fetchone()
            first = self.db.execute("SELECT MIN(revision) FROM change_events WHERE item_id=? AND created_at>=? AND created_at<=?",
                (iid, start, cutoff.timestamp())).fetchone()[0]
            baseline = by_item[iid].get("before_revision") if iid in by_item else first - 1
            if type(baseline) is not int or not 0 <= baseline < item["content_revision"]:
                self.issue("brief_baseline_invalid")
                continue
            prior = self.db.execute("SELECT snapshot_json FROM item_revisions WHERE item_id=? AND revision=?", (iid, baseline)).fetchone()
            if baseline and prior is None:
                self.issue("brief_baseline_missing")
            now_snapshot, old_snapshot = _load(current[0]) if current else {}, _load(prior[0]) if prior else {}
            # New content, claim changes and relation changes cannot silently
            # disappear before the editor sees the bounded candidate pool.
            if any(now_snapshot.get(key, []) != old_snapshot.get(key, []) for key in ("units", "claims", "relations")):
                eligible.add(iid)
            if iid in candidate_items and bindings[iid] != [pref["revision"] if pref else 0, known, item["content_revision"]]:
                self.issue("brief_input_stale", "semantic_pending")
        if not eligible <= candidate_items:
            self.issue("brief_candidate_omitted", "semantic_pending")
        for change in candidates:
            iid = change["item_id"]
            item, actual = self.items.get(iid), changes.get(iid)
            spans = set().union(*(self.evidence[oid] for oid in self.item_observations[iid]))
            if (item is None or actual is None or change["id"] != str(actual["change_id"])
                    or change["content_revision"] != item["content_revision"]
                    or change["title"] != redact_urls_in_text(item["title"])
                    or not set(change["evidence_ids"]) <= spans or not change["evidence_ids"]
                    or change["known_at"] != datetime.fromtimestamp(actual["created_at"], timezone.utc).isoformat()
                    or change.get("manual") in {"exclude", "defer"}):
                self.issue("brief_input_source_mismatch")
            if attributed:
                expected_attributions = []
                for sid in change["evidence_ids"]:
                    span = self.spans.get(sid)
                    source = self.sources.get(span["source_version_id"]) if span else None
                    if source is None:
                        continue
                    expected_attributions.append({"evidence_id": sid, "excerpt": redact_urls_in_text(span["excerpt"]),
                        **source_attribution(source["source_namespace"], source["chat_id"], _load(source["payload_json"]))})
                if (len(expected_attributions) != len(change["evidence_ids"])
                        or change.get("attributions") != expected_attributions):
                    self.issue("brief_attribution_source_mismatch")
            elif "attributions" in change:
                self.issue("brief_attribution_version_mismatch")
            baseline = change.get("before_revision")
            if type(baseline) is int and item is not None:
                contributed = [str(row[0]) for row in self.db.execute(
                    "SELECT change_id FROM change_events WHERE item_id=? AND revision>? AND revision<=? ORDER BY revision",
                    (iid, baseline, item["content_revision"]))]
                if not contributed or change.get("contributing_change_ids") != contributed:
                    self.issue("brief_contributing_changes_mismatch")
        status = edition["status"]
        if status not in {"success", "silent"} or attempt["status"] != status:
            self.issue("brief_editorial_pending", "semantic_pending")
            return
        if not attempt["plan_json"]:
            if status != "silent" or candidates or attempt["code"] != "no_eligible_change":
                self.issue("brief_accepted_plan_missing", "semantic_pending")
            return
        plan = _load(attempt["plan_json"])
        if plan.get("status") != status or not isinstance(plan.get("paragraphs"), list):
            self.issue("brief_accepted_plan_invalid")
            return
        self._verify_brief_artifacts(required, attempt, candidates, attributed=attributed)
        if status == "silent":
            if plan["paragraphs"] or not isinstance(plan.get("reason"), str) or not plan["reason"].strip():
                self.issue("brief_silent_disposition_invalid")
            return
        if not plan["paragraphs"]:
            self.issue("brief_accepted_plan_invalid")
        for paragraph in plan["paragraphs"]:
            ids = paragraph.get("change_ids", [])
            if not ids or not set(ids) <= by_id.keys() or not isinstance(paragraph.get("text"), str) or not paragraph["text"].strip():
                self.issue("brief_plan_reference_invalid")
                continue
            cited = {(r["item_id"], r["evidence_id"]) for r in paragraph.get("evidence_refs", [])}
            allowed = {(by_id[c]["item_id"], e) for c in ids for e in by_id[c]["evidence_ids"]}
            if not cited or not cited <= allowed or any(not cited.intersection({(by_id[c]["item_id"], e) for e in by_id[c]["evidence_ids"]}) for c in ids):
                self.issue("brief_plan_evidence_invalid")
            for key, field in (("scopes", "scope"), ("corrections", "correction")):
                expected = {by_id[c][field] for c in ids if by_id[c].get(field)}
                if set(paragraph.get(key, [])) != expected:
                    self.issue("brief_plan_scope_or_correction_lost")
        history = self.db.execute("SELECT * FROM reader_brief_history WHERE generation_id=? AND edition_id=? AND content_revision=?",
            (self.generation, edition["edition_id"], edition["content_revision"])).fetchone()
        if (history is None or history["plan_json"] != attempt["plan_json"] or edition["successful_plan"] != attempt["plan_json"]
                or history["snapshot_text"] != edition["successful_text"] or _load(history["inputs_json"]) != candidates
                or not edition["successful_text"] or redact_urls_in_text(edition["successful_text"]) != edition["successful_text"]):
            self.issue("brief_history_artifact_mismatch")
        projection = self.db.execute("SELECT machine_body FROM reader_projections WHERE generation_id=? AND object_id=?",
            (self.generation, edition["edition_id"])).fetchone() if self._has("reader_projections") else None
        prepared = "\n" + edition["successful_text"].strip("\n") + "\n"
        body_bound = projection is not None and projection[0] == prepared
        if not body_bound and self._has("reader_proposals"):
            body_bound = self.db.execute("SELECT 1 FROM reader_proposals WHERE generation_id=? AND object_id=? "
                "AND content_revision=? AND status='keep_personal' AND proposed_body=?",
                (self.generation, edition["edition_id"], edition["content_revision"], prepared)).fetchone() is not None
        if (not body_bound
                or not self._projection_ready(edition["edition_id"], edition["content_revision"], allow_personal=True)):
            self.issue("brief_current_projection_missing", "projection_pending")
        self.counts["verified_brief_editions"] += 1

    def _verify_brief_artifacts(self, required, attempt, candidates, *, attributed):
        if not self._has("reader_text_artifacts"):
            self.issue("brief_provider_artifact_missing", "semantic_pending")
            return
        request = attempt["request_id"]
        rows = [dict(row) for row in self.db.execute("SELECT * FROM reader_text_artifacts WHERE generation_id=?",
                (self.generation,)) if row["request_id"] == request or row["request_id"].startswith(request + ":")]
        if any(row["status"] != "accepted" or row["provider_route"] != required.get("provider_route") for row in rows):
            self.issue("brief_provider_artifact_unaccepted", "semantic_pending")
        writer_version = "brief-write-v3" if attributed else "brief-write-v2"
        selector_version = "brief-select-v2" if attributed else "brief-select-v1"
        page_version = "brief-compact-page-v2" if attributed else "brief-compact-page-v1"
        merge_version = "brief-compact-merge-v2" if attributed else "brief-compact-merge-v1"
        if any(row["prompt_version"] not in {writer_version, selector_version, page_version, merge_version} for row in rows):
            self.issue("brief_provider_version_conflict", "semantic_pending")
        writers = [row for row in rows if row["prompt_version"] == writer_version and row["request_id"] == request]
        if not writers:
            # The only non-writer editorial outcome is an explicitly accepted
            # final selection level which selected no original changes.
            selectors = [row for row in rows if row["prompt_version"] == selector_version]
            if attempt["status"] != "silent" or not selectors:
                self.issue("brief_provider_artifact_missing", "semantic_pending")
            else:
                last_level = max(int(row["request_id"].rsplit(":", 2)[1]) for row in selectors)
                last = [row for row in selectors if int(row["request_id"].rsplit(":", 2)[1]) == last_level]
                if any(_load(row["artifact_json"]).get("selected_change_ids") != [] for row in last):
                    self.issue("brief_silent_selection_unproved")
        elif len(writers) != 1 or _load(writers[0]["artifact_json"]) != _load(attempt["plan_json"]):
            self.issue("brief_provider_artifact_mismatch")
        originals = {c["id"]: c for c in candidates}
        for row in rows:
            if row["prompt_version"] == selector_version:
                value = _load(row["artifact_json"])
                selected = value.get("selected_change_ids", [])
                if (len(selected) != len(set(selected)) or not set(selected) <= originals.keys()
                        or not isinstance(value.get("reason"), str) or not value["reason"].strip()):
                    self.issue("brief_selection_artifact_invalid")
        compacted = defaultdict(list)
        for row in rows:
            if row["prompt_version"] in {page_version, merge_version}:
                value = _load(row["artifact_json"])
                binding = value.get("binding", {})
                original = originals.get(binding.get("change_id"))
                expected_binding = {} if original is None else {"change_id": original["id"], "item_id": original["item_id"],
                        "content_revision": original["content_revision"], "input_hash": _hash(original)}
                if original is not None and attributed:
                    expected_binding.update(before_revision=original["before_revision"], relation_change=original["relation_change"])
                if (original is None or binding != expected_binding
                        or not set(value.get("evidence_ids", [])) <= set(original["evidence_ids"])
                        or not isinstance(value.get("summary"), str) or not value["summary"].strip()):
                    self.issue("brief_compaction_binding_invalid")
                    continue
                if row["prompt_version"] == page_version:
                    compacted[original["id"]].append(value["coverage"])
        for identity, ranges in compacted.items():
            maximum = required.get("max_input_bytes")
            if type(maximum) is not int or maximum < 6000:
                self.issue("brief_compaction_budget_missing")
                continue
            original, size = originals[identity], max(1, maximum // 24)
            fields = [(key, value) for key, value in original.items() if isinstance(value, str)]
            fields += [(key, value) for key in ("conditions", "evidence_ids", "contributing_change_ids") for value in original[key]]
            if attributed:
                fields += [("attributions." + key, value) for attribution in original["attributions"]
                           for key, value in attribution.items() if isinstance(value, str)]
            parts = sum(1 if key == "evidence_ids" or not value else math.ceil(len(value) / size) for key, value in fields)
            position = 0
            for lower, upper in sorted(ranges):
                if type(lower) is not int or type(upper) is not int or lower != position or upper <= lower:
                    self.issue("brief_compaction_coverage_incomplete")
                    break
                position = upper
            if position != parts:
                self.issue("brief_compaction_coverage_incomplete")

    def _verify_history(self):
        """Read each frozen history pass; none can supply semantic completion."""
        if not {"legacy_mappings", "annotation_preservation"} <= self.tables:
            self.issue("legacy_conservation_tables_missing")
            return
        passes = self.manifest.get("history_preservation_passes")
        if not isinstance(passes, list) or not passes:
            self.issue("legacy_history_passes_missing")
            return
        ids, declared_freezes, freeze_ids = [], [], set()
        namespace = None
        for index, declaration in enumerate(passes):
            if not isinstance(declaration, dict) or not isinstance(declaration.get("legacy_inventory"), dict):
                self.issue("legacy_history_pass_invalid")
                continue
            value = declaration["legacy_inventory"]
            publication = declaration.get("publication_plan")
            if is_ref(value) or is_ref(publication):
                if self.history_payload_loader is None:
                    self.issue("legacy_history_payload_unavailable")
                    continue
                try:
                    value = self.history_payload_loader(value)
                    publication = self.history_payload_loader(publication)
                except (RuntimeError, OSError, ValueError, TypeError):
                    self.issue("legacy_history_payload_unavailable")
                    continue
            inventory = LegacyInventory.from_dict(value)
            manifest_id = inventory.manifest_id
            if declaration.get("receipt_id") != manifest_id or manifest_id in ids:
                self.issue("legacy_history_pass_invalid")
            ids.append(manifest_id)
            if namespace is None:
                namespace = inventory.namespace
            elif inventory.namespace != namespace:
                self.issue("legacy_history_namespace_mismatch")
            self._verify_history_inventory(inventory, publication, current=index == len(passes) - 1)
            self._verify_history_databases(inventory, declaration["legacy_inventory"],
                                           declaration.get("freeze_receipts"), declared_freezes, freeze_ids)
            if index == len(passes) - 1 and (
                    self.manifest.get("legacy_inventory") != declaration["legacy_inventory"]
                    or self.manifest.get("publication_plan") != declaration.get("publication_plan")):
                self.issue("legacy_history_final_binding_mismatch")
            del inventory, value, publication
        expected_freezes = self.manifest.get("history_freeze_receipts")
        if not isinstance(expected_freezes, list) or not expected_freezes:
            self.issue("legacy_history_freeze_manifest_missing")
        elif (len(expected_freezes) != len(declared_freezes)
                or any(sum(reference == actual for actual in declared_freezes) != 1 for reference in expected_freezes)):
            self.issue("legacy_history_freeze_set_mismatch")
        actual = {row[0] for row in self.db.execute("SELECT legacy_id FROM legacy_mappings WHERE kind='history_pass'")}
        if actual != set(ids):
            self.issue("legacy_history_pass_set_mismatch")
        # Extra record/pass evidence is never silently detached from the run.
        if any(row[0] not in set(ids) for row in self.db.execute(
                "SELECT DISTINCT target_id FROM legacy_mappings WHERE target_type IN ('history_record','history_file','file_backup')")):
            self.issue("legacy_history_pass_set_mismatch")
        if namespace is not None:
            self._verify_history_root(namespace, ids)
        self.counts["legacy_history_passes"] = len(ids)

    def _verify_history_databases(self, inventory, inventory_binding, references, declared, freeze_ids):
        """Every freeze owns a distinct whole-DB receipt, even for equal rows."""
        if not isinstance(references, list) or not references:
            self.issue("legacy_history_database_backup_manifest_missing")
            return
        for reference in references:
            declared.append(reference)
            try:
                if is_ref(reference):
                    if self.history_payload_loader is None:
                        self.issue("legacy_history_payload_unavailable")
                        continue
                    frozen = self.history_payload_loader(reference)
                else:
                    frozen = reference
            except (RuntimeError, OSError, ValueError, TypeError):
                self.issue("legacy_history_payload_unavailable")
                continue
            if (not isinstance(frozen, dict) or not isinstance(frozen.get("receipt_id"), str)
                    or not frozen["receipt_id"] or frozen["receipt_id"] in freeze_ids
                    or frozen.get("legacy_inventory") != inventory_binding):
                self.issue("legacy_history_freeze_binding_invalid")
                continue
            freeze_ids.add(frozen["receipt_id"])
            self._verify_history_database_backup(inventory, frozen.get("databases", {}).get("knowledge"))
            del frozen

    def _verify_history_database_backup(self, inventory, receipt):
        if (not isinstance(receipt, dict) or not isinstance(receipt.get("path"), str)
                or not isinstance(receipt.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"]) is None):
            self.issue("legacy_history_database_backup_manifest_invalid")
            return
        connection = None
        fd = None
        def identity(observation):
            return (observation.st_dev, observation.st_ino, observation.st_mode, observation.st_size,
                    observation.st_mtime_ns, observation.st_ctime_ns)
        try:
            path = _checked_path(receipt["path"])
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                fd = None
                initial = os.fstat(stream.fileno())
                if not stat.S_ISREG(initial.st_mode):
                    self.issue("legacy_history_database_backup_invalid")
                    return
                expected_identity = identity(initial)
                checksum = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    checksum.update(chunk)
                if (checksum.hexdigest() != receipt["sha256"]
                        or identity(os.fstat(stream.fileno())) != expected_identity
                        or identity(path.lstat()) != expected_identity):
                    self.issue("legacy_history_database_backup_changed")
                    return
                # Online Backup seals a standalone database. Read those exact
                # hashed bytes; a later sidecar must not supply unbound rows.
                connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA cache_size=-4096")
                connection.execute("PRAGMA mmap_size=0")
                connection.execute("BEGIN")
                if any(row[0] != "ok" for row in connection.execute("PRAGMA quick_check")):
                    self.issue("legacy_history_database_backup_invalid")
                names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "topics" not in names or set(inventory.tables) != names.intersection(TABLE_KEYS):
                    self.issue("legacy_history_database_rows_mismatch")
                missing = object()
                for table, key in TABLE_KEYS.items():
                    if table not in names:
                        continue
                    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                    if key not in columns or table == "topics" and "obsidian_path" not in columns:
                        self.issue("legacy_history_database_rows_mismatch")
                        continue
                    expected = (record for record in inventory.records if record.table == table)
                    actual = connection.execute(f'SELECT * FROM "{table}" ORDER BY "{key}"')
                    try:
                        for record, row in zip_longest(expected, actual, fillvalue=missing):
                            if record is missing or row is missing:
                                self.issue("legacy_history_database_rows_mismatch")
                                break
                            payload = {name: ({"sqlite_blob_base64": base64.b64encode(value).decode("ascii")}
                                              if isinstance(value, bytes) else value)
                                       for name in row.keys() for value in (row[name],)}
                            if record.old_id != str(row[key]) or record.payload != payload:
                                self.issue("legacy_history_database_rows_mismatch")
                                break
                    finally:
                        actual.close()
                if (identity(os.fstat(stream.fileno())) != expected_identity
                        or identity(path.lstat()) != expected_identity):
                    self.issue("legacy_history_database_backup_changed")
                self.counts["legacy_database_backups"] += 1
        except LegacyError as exc:
            self.issue("legacy_history_database_backup_missing" if exc.code == "legacy_input_missing"
                       else "legacy_history_database_backup_invalid")
        except FileNotFoundError:
            self.issue("legacy_history_database_backup_missing")
        except (sqlite3.Error, OSError):
            self.issue("legacy_history_database_backup_invalid")
        finally:
            if connection is not None:
                connection.close()
            if fd is not None:
                os.close(fd)

    def _verify_history_inventory(self, inventory, plan, *, current):
        manifest_id = inventory.manifest_id
        expected = {"schema": HISTORY_SCHEMA, "history_policy": SOURCE_FIRST_POLICY,
            "receipt_id": manifest_id, "manifest_id": manifest_id, "namespace": inventory.namespace,
            "database_snapshot_digest": inventory.database_snapshot_digest,
            "record_count": len(inventory.records), "file_count": len(inventory.files),
            "external_file_count": len(inventory.external_files)}
        row = self.db.execute("SELECT * FROM legacy_mappings WHERE kind='history_pass' AND legacy_id=? "
            "AND target_type='history_inventory' AND target_id=?", (manifest_id, manifest_id)).fetchone()
        if row is None or row["disposition"] != "history_preserved" or _load(row["metadata_json"]) != expected:
            self.issue("legacy_history_pass_receipt_invalid")
        for record in inventory.records:
            row = self.db.execute("SELECT * FROM legacy_mappings WHERE kind='history_record' AND legacy_id=? "
                "AND target_type='history_record' AND target_id=?", (record.legacy_ref, manifest_id)).fetchone()
            metadata = {"schema": HISTORY_SCHEMA, "history_policy": SOURCE_FIRST_POLICY,
                "manifest_id": manifest_id, "input": asdict(record), "resources": list(legacy_resources(record))}
            if row is None or row["disposition"] != "history_preserved" or _load(row["metadata_json"]) != metadata:
                self.issue("legacy_history_record_unaccounted")
        if self.db.execute("SELECT count(*) FROM legacy_mappings WHERE target_type='history_record' AND target_id=?",
                           (manifest_id,)).fetchone()[0] != len(inventory.records):
            self.issue("legacy_history_record_set_mismatch")
        selection = self.db.execute("SELECT disposition,metadata_json FROM legacy_mappings WHERE kind='selection' "
            "AND legacy_id=? AND target_id=?", (inventory.namespace, manifest_id)).fetchone()
        if (selection is None or selection[0] != "preserved_no_subscription"
                or _load(selection[1]) != inventory.selection_snapshot):
            self.issue("legacy_selection_unaccounted")
        for file in inventory.files:
            row = self.db.execute("SELECT * FROM legacy_mappings WHERE kind='history_file' AND legacy_id=? "
                "AND target_type='history_file' AND target_id=?",
                (_opaque(inventory.namespace, "file-inventory", file.relative_path), manifest_id)).fetchone()
            metadata = {"manifest_id": manifest_id, "file": _load(_json(asdict(file))),
                "preservation": "unknown_preserve_entire_page" if file.owned else "unowned_in_place"}
            if row is None or row["disposition"] != "history_preserved" or _load(row["metadata_json"]) != metadata:
                self.issue("legacy_history_file_unaccounted")
            if current:
                try:
                    path = _under(_checked_path(inventory.managed_root, directory=True), file.relative_path)
                    raw = _read_regular(path, max(1, file.byte_length))
                    if len(raw) != file.byte_length or _bytes_hash(raw) != file.sha256:
                        self.issue("legacy_original_bytes_changed" if file.owned else "unowned_legacy_file_changed")
                except LegacyError:
                    self.issue("legacy_original_bytes_changed" if file.owned else "unowned_legacy_file_changed")
            if file.owned:
                self._verify_history_backup(inventory, "file", file.relative_path, file.sha256,
                                            file.byte_length, file.original_bytes())
                annotation = self.db.execute("SELECT * FROM annotation_preservation WHERE path=? AND sha256=?",
                    (file.relative_path, file.sha256)).fetchone()
                if annotation is None:
                    historical = self.db.execute("SELECT metadata_json FROM legacy_mappings WHERE kind='annotation_history' "
                        "AND legacy_id=? AND target_type='backup' AND target_id=?", (file.relative_path, file.sha256)).fetchone()
                    annotation = _load(historical[0]) if historical else None
                baseline = _load(annotation["metadata_json"]).get("file", {}) if annotation else {}
                if (annotation is None or annotation["status"] not in {"preserved", "preserved_in_place", "user_preserved"}
                        or any(baseline.get(key) != getattr(file, key) for key in
                               ("relative_path", "sha256", "byte_length", "bytes_base64"))):
                    self.issue("legacy_annotation_unaccounted")
                elif current:
                    try:
                        if _read_regular(_checked_path(annotation["backup_path"]), max(1, file.byte_length)) != file.original_bytes():
                            self.issue("legacy_backup_bytes_mismatch")
                    except LegacyError:
                        self.issue("legacy_backup_missing", "projection_pending")
        for external in inventory.external_files:
            row = self.db.execute("SELECT disposition,metadata_json FROM legacy_mappings WHERE kind='history_external_file' "
                "AND legacy_id=? AND target_type='history_file' AND target_id=?",
                (_opaque(inventory.namespace, external["path"]), manifest_id)).fetchone()
            if (row is None or row[0] != "history_preserved"
                    or _load(row[1]) != {"manifest_id": manifest_id, "file": external}):
                self.issue("legacy_external_file_unaccounted")
            import base64
            raw = base64.b64decode(external["bytes_base64"], validate=True)
            self._verify_history_backup(inventory, "external", external["path"], external["sha256"], external["byte_length"], raw)
            if current:
                try:
                    if _read_regular(_checked_path(external["path"]), max(1, len(raw))) != raw:
                        self.issue("legacy_external_bytes_changed")
                except LegacyError:
                    self.issue("legacy_external_bytes_changed")
        if self.db.execute("SELECT count(*) FROM legacy_mappings WHERE target_type='history_file' AND target_id=?",
                           (manifest_id,)).fetchone()[0] != len(inventory.files) + len(inventory.external_files):
            self.issue("legacy_history_file_set_mismatch")
        if self.db.execute("SELECT count(*) FROM legacy_mappings WHERE target_type='file_backup' AND target_id=?",
                           (manifest_id,)).fetchone()[0] != sum(f.owned for f in inventory.files) + len(inventory.external_files):
            self.issue("legacy_history_backup_set_mismatch")
        self.counts["legacy_records"] += len(inventory.records)
        self.counts["legacy_files"] += len(inventory.files)
        self._verify_history_publication(inventory, plan)

    def _verify_history_backup(self, inventory, role, path, sha256, byte_length, expected):
        row = self.db.execute("SELECT * FROM legacy_mappings WHERE kind='history_backup' AND legacy_id=? "
            "AND target_type='file_backup' AND target_id=?",
            (_opaque(inventory.namespace, role, path), inventory.manifest_id)).fetchone()
        if row is None:
            self.issue("legacy_history_backup_missing", "projection_pending")
            return
        metadata = _load(row["metadata_json"])
        if (row["disposition"] != "preserved_in_place" or set(metadata) != {
                "manifest_id", "role", "path", "sha256", "byte_length", "backup_path"}
                or {k: v for k, v in metadata.items() if k != "backup_path"} != {
                    "manifest_id": inventory.manifest_id, "role": role, "path": path,
                    "sha256": sha256, "byte_length": byte_length}):
            self.issue("legacy_history_backup_invalid")
            return
        try:
            if _read_regular(_checked_path(metadata["backup_path"]), max(1, byte_length)) != expected:
                self.issue("legacy_backup_bytes_mismatch")
        except LegacyError:
            self.issue("legacy_history_backup_missing", "projection_pending")

    def _verify_history_publication(self, inventory, plan):
        if not isinstance(plan, dict) or plan.get("schema") != HISTORY_PUBLICATION_SCHEMA:
            self.issue("legacy_history_publication_missing", "projection_pending")
            return
        if plan != build_history_publication_plan(inventory):
            self.issue("legacy_history_publication_invalid")
            return
        if self.db.execute("SELECT count(*) FROM legacy_mappings WHERE kind='history_navigation' AND legacy_id=?",
                           (inventory.manifest_id,)).fetchone()[0] != len(plan["documents"]):
            self.issue("legacy_history_navigation_set_mismatch")
        for page in plan["pages"]:
            file = next(f for f in inventory.files if f.relative_path == page["old_path"])
            try:
                if self._projection_bytes(page["history_backup_path"]) != file.original_bytes():
                    self.issue("legacy_history_copy_missing", "projection_pending")
            except LegacyError:
                self.issue("legacy_history_copy_missing", "projection_pending")
        for document in plan["documents"]:
            body = history_document_body(inventory, plan, document)
            row = self.db.execute("SELECT * FROM legacy_mappings WHERE kind='history_navigation' AND legacy_id=? "
                "AND target_type='projection' AND target_id=?", (inventory.manifest_id, document["object_id"])).fetchone()
            if row is None:
                self.issue("legacy_history_navigation_receipt_missing", "projection_pending")
                continue
            metadata = _load(row["metadata_json"])
            if (metadata.get("manifest_id") != inventory.manifest_id or metadata.get("document") != document
                    or metadata.get("body_sha256") != _bytes_hash(body.encode())):
                self.issue("legacy_history_navigation_invalid")
            self._verify_history_document(document, body, metadata.get("receipt", {}))

    def _verify_history_document(self, document, body, receipt):
        if (receipt.get("object_id") != document["object_id"] or receipt.get("path") != document["path"]
                or receipt.get("status") not in {"published", "unchanged"}
                or not self._projection_ready(document["object_id"], receipt.get("content_revision"))):
            self.issue("legacy_history_navigation_receipt_missing", "projection_pending")
        try:
            actual = region_body(self._projection_bytes(document["path"]).decode(), "body")
            if actual != "\n" + body.strip("\n") + "\n" or redact_urls_in_text(actual) != actual:
                self.issue("legacy_history_navigation_content_mismatch")
        except (LegacyError, ReadingError, UnicodeError):
            self.issue("legacy_history_navigation_missing", "projection_pending")

    def _verify_history_root(self, namespace, ids):
        document = {"path": "Legacy/Index.md", "object_id": "control:legacy-history:" + namespace + ":root"}
        body = history_root_body(ids)
        row = self.db.execute("SELECT metadata_json FROM legacy_mappings WHERE kind='history_root_navigation' "
            "AND legacy_id=? AND target_type='projection' AND target_id=?", (namespace, document["object_id"])).fetchone()
        if row is None:
            self.issue("legacy_history_root_missing", "projection_pending")
            return
        metadata = _load(row[0])
        if (metadata.get("manifest_ids") != sorted(ids) or metadata.get("root_navigation") != document
                or metadata.get("body_sha256") != _bytes_hash(body.encode())):
            self.issue("legacy_history_root_invalid")
        self._verify_history_document(document, body, metadata.get("receipt", {}))

    def _verify_legacy(self):
        if self.history_policy == SOURCE_FIRST_POLICY:
            self._verify_history()
            return
        value = self.manifest.get("legacy_inventory")
        if value is None:
            return
        inventory = LegacyInventory.from_dict(value)
        manifest_id = inventory.manifest_id
        if not {"legacy_mappings", "annotation_preservation"} <= self.tables:
            self.issue("legacy_conservation_tables_missing")
            return
        def legacy_row(row):
            return {**dict(row), "metadata": _load(row["metadata_json"])}
        mappings = Rows(self.db, "SELECT * FROM legacy_mappings", transform=legacy_row)
        by_legacy = GroupMap(self.db, "legacy_mappings", "legacy_id", transform=legacy_row)
        for record in inventory.records:
            targets = [r for r in by_legacy[record.legacy_ref] if r["target_type"] == "source_version"]
            valid = False
            for target in targets:
                metadata = target["metadata"]
                source = self.sources.get(target["target_id"])
                if (source is not None and metadata.get("manifest_id") == manifest_id
                        and metadata.get("input") == asdict(record)
                        and source["text"] == record.text
                        and source["provenance_grade"] == ("raw_exact" if record.raw_level == "raw" else record.raw_level)
                        and self.source_resources[target["target_id"]] == sorted(legacy_resources(record),
                            key=lambda r: (r["resource_id"], r["occurrence_id"]))
                        and _load(source["payload_json"]) == ({"text": record.text} if record.raw_level == "raw"
                                                              else {"legacy_row": record.payload})):
                    valid = True
            if not valid:
                self.issue("legacy_record_unaccounted")
        for external in inventory.external_files:
            targets = [r for r in mappings.where("kind='external_file' AND target_id=?", (external["sha256"],)) if r["kind"] == "external_file" and r["target_id"] == external["sha256"]
                       and r["metadata"].get("file") == external and r["disposition"] == "preserved_in_place"]
            if not targets:
                self.issue("legacy_external_file_unaccounted")
                continue
            try:
                raw = _read_regular(_checked_path(external["path"]), max(1, external["byte_length"]))
                recovery = _read_regular(_checked_path(targets[0]["metadata"]["backup_path"]), max(1, external["byte_length"]))
                if _bytes_hash(raw) != external["sha256"] or raw != recovery:
                    self.issue("legacy_external_bytes_changed")
            except LegacyError:
                self.issue("legacy_external_backup_missing", "projection_pending")
        selection = [r for r in mappings.where("kind='selection' AND legacy_id=?", (inventory.namespace,)) if r["kind"] == "selection" and r["legacy_id"] == inventory.namespace
                     and r["target_id"] == manifest_id and r["disposition"] == "preserved_no_subscription"]
        if not any(r["metadata"] == inventory.selection_snapshot for r in selection):
            self.issue("legacy_selection_unaccounted")
        for file in inventory.files:
            if not any(r["kind"] == "file" and r["target_id"] == file.sha256
                       and r["metadata"].get("file") == _load(_json(asdict(file)))
                       for r in mappings.where("kind='file' AND target_id=?", (file.sha256,))):
                self.issue("legacy_file_unaccounted")
            try:
                root = _checked_path(inventory.managed_root, directory=True)
                current = _read_regular(_under(root, file.relative_path), max(file.byte_length, 1))
                unchanged = current == file.original_bytes() if file.owned else _bytes_hash(current) == file.sha256
            except LegacyError:
                unchanged = False
            if not file.owned:
                if not unchanged:
                    self.issue("unowned_legacy_file_changed")
                continue
            row = self.db.execute("SELECT * FROM annotation_preservation WHERE path=?", (file.relative_path,)).fetchone()
            if row is None or row["sha256"] != file.sha256:
                self.issue("legacy_annotation_unaccounted")
                continue
            if row["status"] not in {"preserved", "preserved_in_place", "user_preserved"}:
                self.issue("legacy_backup_pending", "projection_pending")
            try:
                backup = _read_regular(_checked_path(row["backup_path"]), max(file.byte_length, 1))
                if backup != file.original_bytes():
                    self.issue("legacy_backup_bytes_mismatch")
            except LegacyError:
                self.issue("legacy_backup_missing", "projection_pending")
            if not unchanged:
                self.issue("legacy_original_bytes_changed")
        self.counts["legacy_records"] = len(inventory.records)
        self.counts["legacy_files"] = len(inventory.files)
        self._verify_legacy_maps(inventory, mappings)

    def _verify_legacy_maps(self, inventory, mappings):
        plan = self.manifest.get("publication_plan")
        if not isinstance(plan, dict) or plan.get("manifest_id") != inventory.manifest_id:
            self.issue("legacy_publication_plan_missing", "projection_pending")
            return
        if plan.get("subscriptions") != []:
            self.issue("legacy_subscription_escalation")
        queue_records = [r for r in inventory.records if r.table == "review_queue"]
        if queue_records:
            queue = plan.get("queue_history")
            if not isinstance(queue, dict) or queue.get("record_count") != len(queue_records):
                self.issue("legacy_queue_navigation_missing", "projection_pending")
            else:
                try:
                    body = region_body(self._projection_bytes(queue["path"]).decode(), "body")
                    if body != "\n" + queue["body"].strip("\n") + "\n" or redact_urls_in_text(body) != body:
                        self.issue("legacy_queue_navigation_invalid")
                    for record in queue_records:
                        item = record.payload["queue_item"]
                        label = redact_urls_in_text(str(item.get("title", "历史待办")) + " · " + str(item.get("status", "unknown")))
                        label = label.replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")
                        if body.count("- " + label + " ^queue-" + record.legacy_ref[:20]) != 1:
                            self.issue("legacy_queue_transition_unaccounted")
                    receipts = [r for r in mappings.where("kind='queue_navigation'") if r["kind"] == "queue_navigation"
                                and r["metadata"].get("manifest_id") == inventory.manifest_id
                                and r["metadata"].get("queue") == queue]
                    if not any(self._projection_ready(r["target_id"], r["metadata"]["receipt"]["content_revision"]) for r in receipts):
                        self.issue("legacy_queue_navigation_receipt_missing", "projection_pending")
                except (KeyError, LegacyError, TypeError, ReadingError, UnicodeError):
                    self.issue("legacy_queue_navigation_missing", "projection_pending")
        topics = {r.legacy_ref for r in inventory.records if r.kind == "topic"}
        mapping = plan.get("topic_item_map", {})
        item_paths = plan.get("item_paths", {})
        if item_paths.keys() != {i for targets in mapping.values() for i in targets}:
            self.issue("legacy_item_path_set_mismatch")
        for iid, path in item_paths.items():
            current_paths = self.projection_id_paths.get(iid, [])
            row = self.db.execute("SELECT path,status FROM reader_projections WHERE generation_id=? AND object_id=?",
                                  (self.generation, iid)).fetchone() if self._has("reader_projections") else None
            if path not in current_paths and not (row and row["status"] == "personal" and path == row["path"]):
                self.issue("legacy_item_path_binding_mismatch")
        captured_versions = defaultdict(set)
        for row in mappings:
            if row["target_type"] == "source_version" and row["metadata"].get("manifest_id") == inventory.manifest_id:
                captured_versions[row["legacy_id"]].add(row["target_id"])
        if mapping.keys() != topics:
            self.issue("legacy_topic_mapping_pending", "semantic_pending")
        for topic, targets in mapping.items():
            if (not isinstance(targets, list) or len(targets) != len(set(targets))
                    or not set(targets) <= self.items.keys()):
                self.issue("legacy_topic_mapping_invalid")
            family = {topic} | {r.legacy_ref for r in inventory.records if r.topic_ref == topic or
                               topic in r.payload.get("topic_refs", ())}
            allowed_versions = set().union(*(captured_versions[ref] for ref in family))
            if not targets:
                dispositions = self.dispositions
                history = [r for r in mappings.where("legacy_id=?", (topic,)) if r["kind"] == "topic" and r["legacy_id"] == topic
                           and r["target_type"] == "history_topic" and r["disposition"] == "history_preserved"]
                valid = bool(allowed_versions) and any(
                    set(_load(r["metadata_json"]).get("source_version_ids", ())) == allowed_versions
                    and _load(r["metadata_json"]).get("manifest_id") == inventory.manifest_id for r in history)
                for vid in allowed_versions:
                    disposition = dispositions.get(vid)
                    if (disposition is None or disposition["status"] not in {
                            "noise", "context_only", "repetition_only", "unsupported_type"}
                            or vid in self.observed_versions):
                        valid = False
                if not valid:
                    self.issue("legacy_history_disposition_missing")
            current = {r["target_id"] for r in mappings.where("legacy_id=?", (topic,)) if r["kind"] == "topic" and r["legacy_id"] == topic
                       and r["target_type"] == "item" and r["disposition"] == "resolved"}
            if current != set(targets):
                self.issue("legacy_topic_mapping_cardinality_mismatch")
            for target in targets:
                links = [r for r in mappings.where("legacy_id=?", (topic,)) if r["legacy_id"] == topic and r["target_type"] == "item"
                         and r["target_id"] == target and r["disposition"] == "resolved"]
                if not links:
                    self.issue("legacy_topic_mapping_receipt_missing")
                for link in links:
                    details = _load(link["metadata_json"])
                    cited = {self.spans[s]["source_version_id"] for oid in self.item_observations[target]
                             for s in self.evidence[oid] if s in self.spans}
                    witnesses = set(details.get("source_version_ids", ()))
                    if not witnesses or not witnesses <= cited or not witnesses <= allowed_versions:
                        self.issue("legacy_topic_mapping_evidence_missing")
        pages = {p["old_path"]: p for p in plan.get("pages", ())}
        owned = {f.relative_path: f for f in inventory.files if f.owned}
        if len(pages) != len(plan.get("pages", ())) or pages.keys() != owned.keys():
            self.issue("legacy_page_mapping_set_mismatch")
        for path, file in owned.items():
            page = pages.get(path)
            if page is None:
                continue
            ids = sorted({i for topic in file.topic_refs for i in mapping.get(topic, ())})
            unresolved = sorted(ref for ref in file.topic_refs if ref not in mapping)
            mode = "pending" if unresolved else "redirect" if len(ids) == 1 else "hub" if ids else "history"
            expected_anchors = [{"old_anchor": a["target"], "history_path": path,
                                 "history_anchor": a["target"], "item_ids": ids} for a in file.anchors]
            if (page.get("old_sha256") != file.sha256 or page.get("old_byte_length") != file.byte_length
                    or page.get("preserve_original") is not True or page.get("history_path") != path
                    or page.get("item_ids") != ids or page.get("mode") != mode
                    or sorted(page.get("unresolved_topic_refs", ())) != unresolved
                    or page.get("anchor_maps") != expected_anchors
                    or page.get("references") != list(file.references) or page.get("aliases") != list(file.aliases)):
                self.issue("legacy_reference_mapping_invalid")
            try:
                history_path = page["history_backup_path"]
                if not history_path.startswith(".wgo/legacy-history/") or self._projection_bytes(history_path) != file.original_bytes():
                    self.issue("legacy_history_copy_missing", "projection_pending")
                _relative(page["navigation_path"])
                navigation = page["navigation_body"]
                expected_links = {redact_urls_in_text(path).removesuffix(".md")} | {
                    "WGO/" + redact_urls_in_text(item_paths[i]).removesuffix(".md") for i in ids}
                links = re.findall(r"\[\[([^|\]]+)(?:\|[^\]]*)?\]\]", navigation)
                if set(links) != expected_links or len(links) != len(expected_links):
                    self.issue("legacy_navigation_target_mismatch")
                if redact_urls_in_text(navigation) != navigation:
                    self.issue("legacy_navigation_url_unredacted")
                current = self._projection_bytes(page["navigation_path"]).decode()
                if navigation.strip() != current.strip() and region_body(current, "body") != "\n" + navigation.strip("\n") + "\n":
                    self.issue("legacy_navigation_content_mismatch")
                published = [r for r in mappings.where("kind='navigation' AND target_type='projection' "
                    "AND json_extract(metadata_json,'$.page.old_path')=?", (page["old_path"],)) if r["kind"] == "navigation" and r["target_type"] == "projection"
                             and _load(r["metadata_json"]).get("page") == page
                             and _load(r["metadata_json"]).get("manifest_id") == inventory.manifest_id]
                if not any(self._projection_ready(r["target_id"],
                    _load(r["metadata_json"])["receipt"]["content_revision"]) for r in published):
                    self.issue("legacy_navigation_receipt_missing", "projection_pending")
            except (KeyError, LegacyError, TypeError, ReadingError, UnicodeError):
                self.issue("legacy_navigation_missing", "projection_pending")
