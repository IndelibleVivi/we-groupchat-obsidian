"""Single SQLite authority for a semantic knowledge generation."""
from __future__ import annotations

from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from core.platform import create_platform_services
from core.url_safety import redact_urls_in_text, redact_urls_with_offsets
from .attribution import EXTRACTION_PROMPT_VERSION, captured_source_metadata
from .contracts import (SemanticStoreError, DISPOSITIONS, PROVENANCE, digest, dumps,
                        ids, integer, text, timestamp, validate_action, validate_claim_relation)


SCHEMA = "we-groupchat-obsidian.semantic.v1"
DDL = """
CREATE TABLE IF NOT EXISTS generation_manifest(generation_id TEXT PRIMARY KEY,schema TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,metadata_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS source_records(capture_seq INTEGER PRIMARY KEY AUTOINCREMENT,source_version_id TEXT UNIQUE NOT NULL,source_id TEXT NOT NULL,source_namespace TEXT NOT NULL,chat_id TEXT NOT NULL,logical_shard_id TEXT NOT NULL,physical_generation TEXT NOT NULL,source_cursor_token TEXT NOT NULL,source_sent_at REAL NOT NULL,captured_at REAL NOT NULL,revision INTEGER NOT NULL,native_revision TEXT,kind TEXT NOT NULL,provenance_grade TEXT NOT NULL,content_hash TEXT NOT NULL,text TEXT NOT NULL,payload_json TEXT NOT NULL,supersedes_source_version_id TEXT REFERENCES source_records(source_version_id),UNIQUE(source_namespace,chat_id,source_id,revision));
CREATE TABLE IF NOT EXISTS source_heads(source_namespace TEXT NOT NULL,chat_id TEXT NOT NULL,source_id TEXT NOT NULL,source_version_id TEXT NOT NULL REFERENCES source_records(source_version_id),PRIMARY KEY(source_namespace,chat_id,source_id));
CREATE TABLE IF NOT EXISTS capture_batches(batch_id TEXT PRIMARY KEY,receipt_id TEXT UNIQUE NOT NULL,input_fingerprint TEXT NOT NULL,source_namespace TEXT NOT NULL,chat_id TEXT NOT NULL,inventory_digest TEXT NOT NULL,inventory_revision INTEGER NOT NULL,input_cursors_json TEXT NOT NULL,output_cursors_json TEXT NOT NULL,source_eof INTEGER NOT NULL CHECK(source_eof IN (0,1)),purpose TEXT NOT NULL,captured_at REAL NOT NULL,receipt_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capture_batch_members(batch_id TEXT NOT NULL REFERENCES capture_batches(batch_id),ordinal INTEGER NOT NULL,source_version_id TEXT NOT NULL REFERENCES source_records(source_version_id),core INTEGER NOT NULL CHECK(core IN (0,1)),PRIMARY KEY(batch_id,ordinal),UNIQUE(batch_id,source_version_id));
CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY,job_key TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,inputs_json TEXT NOT NULL,versions_json TEXT NOT NULL,state TEXT NOT NULL,worker_id TEXT,lease_until REAL,attempt INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,updated_at REAL NOT NULL,receipt_id TEXT);
CREATE TABLE IF NOT EXISTS job_attempts(job_id TEXT NOT NULL REFERENCES jobs(job_id),attempt INTEGER NOT NULL,worker_id TEXT NOT NULL,started_at REAL NOT NULL,finished_at REAL,error_code TEXT,outcome_unknown INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(job_id,attempt));
CREATE TABLE IF NOT EXISTS job_reconciliations(job_id TEXT NOT NULL REFERENCES jobs(job_id),attempt INTEGER NOT NULL,receipt_id TEXT UNIQUE NOT NULL,evidence_json TEXT NOT NULL,reconciled_at REAL NOT NULL,PRIMARY KEY(job_id,attempt),FOREIGN KEY(job_id,attempt) REFERENCES job_attempts(job_id,attempt));
CREATE TABLE IF NOT EXISTS accepted_job_artifacts(job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),artifact_json TEXT NOT NULL,artifact_hash TEXT NOT NULL,accepted_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS source_spans(span_id TEXT PRIMARY KEY,source_version_id TEXT NOT NULL REFERENCES source_records(source_version_id),start INTEGER NOT NULL,end INTEGER NOT NULL,raw_start INTEGER NOT NULL,raw_end INTEGER NOT NULL,excerpt TEXT NOT NULL,excerpt_hash TEXT NOT NULL,mapping_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations(observation_id TEXT PRIMARY KEY,accepted_job_id TEXT NOT NULL REFERENCES accepted_job_artifacts(job_id),local_key TEXT NOT NULL,title TEXT NOT NULL,scene_kind TEXT NOT NULL,source_start REAL NOT NULL,source_end REAL NOT NULL,recorded_at REAL NOT NULL,resolution_status TEXT NOT NULL,topics_json TEXT NOT NULL,UNIQUE(accepted_job_id,local_key));
CREATE TABLE IF NOT EXISTS observation_evidence(observation_id TEXT NOT NULL REFERENCES observations(observation_id),span_id TEXT NOT NULL REFERENCES source_spans(span_id),role TEXT NOT NULL,material_family_id TEXT,PRIMARY KEY(observation_id,span_id));
CREATE TABLE IF NOT EXISTS content_units(unit_id TEXT PRIMARY KEY,observation_id TEXT NOT NULL REFERENCES observations(observation_id),kind TEXT NOT NULL,text TEXT NOT NULL,span_ids_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities(entity_id TEXT PRIMARY KEY,kind TEXT NOT NULL,name TEXT NOT NULL,external_anchor TEXT,revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS entity_aliases(entity_id TEXT NOT NULL REFERENCES entities(entity_id),alias TEXT NOT NULL,scope TEXT NOT NULL,PRIMARY KEY(entity_id,alias,scope));
CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY,subject_id TEXT REFERENCES entities(entity_id),phase TEXT,valid_from REAL,valid_to REAL,evidence_after REAL,scope_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS claims(claim_id TEXT PRIMARY KEY,subject_id TEXT REFERENCES entities(entity_id),case_id TEXT REFERENCES cases(case_id),condition TEXT,channel TEXT,version TEXT,phase TEXT,observed_at REAL NOT NULL,predicates_json TEXT NOT NULL,span_ids_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',retracts_json TEXT NOT NULL DEFAULT '[]',material_id TEXT,valid_from REAL,valid_to REAL,polarity TEXT NOT NULL DEFAULT 'asserted',modality TEXT NOT NULL DEFAULT 'reported',normalizer_version TEXT NOT NULL DEFAULT 'v1');
CREATE TABLE IF NOT EXISTS observation_claims(observation_id TEXT NOT NULL REFERENCES observations(observation_id),assertion_slot INTEGER NOT NULL,claim_id TEXT NOT NULL REFERENCES claims(claim_id),stance TEXT NOT NULL DEFAULT 'asserts',PRIMARY KEY(observation_id,assertion_slot));
CREATE TABLE IF NOT EXISTS claim_relations(from_claim_id TEXT NOT NULL REFERENCES claims(claim_id),to_claim_id TEXT NOT NULL REFERENCES claims(claim_id),relation TEXT NOT NULL,reason TEXT NOT NULL,evidence_json TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(from_claim_id,to_claim_id,relation));
CREATE TABLE IF NOT EXISTS evidence_witnesses(witness_id TEXT PRIMARY KEY,case_id TEXT REFERENCES cases(case_id),claim_id TEXT NOT NULL REFERENCES claims(claim_id),span_id TEXT NOT NULL REFERENCES source_spans(span_id),as_of_capture_seq INTEGER NOT NULL,valid INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS items(item_id TEXT PRIMARY KEY,kind TEXT NOT NULL,identity_scope_json TEXT NOT NULL,title TEXT NOT NULL,content_revision INTEGER NOT NULL,lifecycle TEXT NOT NULL,semantic_updated_at REAL NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS item_observations(item_id TEXT NOT NULL REFERENCES items(item_id),observation_id TEXT NOT NULL REFERENCES observations(observation_id),role TEXT NOT NULL DEFAULT 'supports',added_by TEXT NOT NULL REFERENCES action_receipts(action_key) DEFERRABLE INITIALLY DEFERRED,PRIMARY KEY(item_id,observation_id));
CREATE TABLE IF NOT EXISTS item_claims(item_id TEXT NOT NULL REFERENCES items(item_id),claim_id TEXT NOT NULL REFERENCES claims(claim_id),role TEXT NOT NULL DEFAULT 'supports',PRIMARY KEY(item_id,claim_id));
CREATE TABLE IF NOT EXISTS item_revisions(item_id TEXT NOT NULL REFERENCES items(item_id),revision INTEGER NOT NULL,as_of_capture_seq INTEGER NOT NULL,snapshot_json TEXT NOT NULL,semantic_effect TEXT NOT NULL,transaction_id TEXT NOT NULL REFERENCES canonical_transactions(transaction_id) DEFERRABLE INITIALLY DEFERRED,PRIMARY KEY(item_id,revision));
CREATE TABLE IF NOT EXISTS item_relations(relation_id TEXT PRIMARY KEY,from_item_id TEXT NOT NULL REFERENCES items(item_id),to_item_id TEXT NOT NULL REFERENCES items(item_id),relation TEXT NOT NULL,direction TEXT NOT NULL,reason TEXT NOT NULL,evidence_json TEXT NOT NULL,read_revisions_json TEXT NOT NULL,receipt_id TEXT NOT NULL REFERENCES canonical_transactions(transaction_id) DEFERRABLE INITIALLY DEFERRED,UNIQUE(from_item_id,to_item_id,relation));
CREATE TABLE IF NOT EXISTS identity_operations(operation_id TEXT PRIMARY KEY,type TEXT NOT NULL,input_json TEXT NOT NULL,output_json TEXT NOT NULL,reason TEXT NOT NULL,before_json TEXT NOT NULL,receipt_id TEXT NOT NULL REFERENCES canonical_transactions(transaction_id) DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE IF NOT EXISTS identity_restorations(operation_id TEXT PRIMARY KEY REFERENCES identity_operations(operation_id),receipt_id TEXT UNIQUE NOT NULL,read_set_json TEXT NOT NULL,receipt_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS item_aliases(old_item_id TEXT NOT NULL REFERENCES items(item_id),new_item_id TEXT NOT NULL REFERENCES items(item_id),operation_id TEXT NOT NULL REFERENCES identity_operations(operation_id),PRIMARY KEY(old_item_id,new_item_id),CHECK(old_item_id<>new_item_id));
CREATE TABLE IF NOT EXISTS views(view_id TEXT PRIMARY KEY,title TEXT NOT NULL,definition TEXT NOT NULL,definition_revision INTEGER NOT NULL,override_revision INTEGER NOT NULL,origin TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS view_memberships(view_id TEXT NOT NULL REFERENCES views(view_id),item_id TEXT NOT NULL REFERENCES items(item_id),placement TEXT NOT NULL,origin TEXT NOT NULL,reason TEXT NOT NULL,pinned INTEGER NOT NULL DEFAULT 0,excluded INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(view_id,item_id));
CREATE TABLE IF NOT EXISTS resource_refs(resource_id TEXT NOT NULL,observation_id TEXT REFERENCES observations(observation_id),item_id TEXT REFERENCES items(item_id),occurrence_id TEXT NOT NULL,purpose TEXT NOT NULL,status TEXT NOT NULL,location TEXT,metadata_json TEXT NOT NULL,PRIMARY KEY(resource_id,occurrence_id,item_id,observation_id),CHECK(observation_id IS NOT NULL OR item_id IS NOT NULL));
CREATE TABLE IF NOT EXISTS source_resource_refs(source_version_id TEXT NOT NULL REFERENCES source_records(source_version_id),resource_id TEXT NOT NULL,occurrence_id TEXT NOT NULL,purpose TEXT NOT NULL,status TEXT NOT NULL,location TEXT,metadata_json TEXT NOT NULL,PRIMARY KEY(source_version_id,resource_id,occurrence_id));
CREATE TABLE IF NOT EXISTS source_core_jobs(source_version_id TEXT PRIMARY KEY REFERENCES source_records(source_version_id),job_id TEXT NOT NULL REFERENCES jobs(job_id));
CREATE TABLE IF NOT EXISTS canonical_transactions(transaction_id TEXT PRIMARY KEY,job_id TEXT REFERENCES jobs(job_id),actions_json TEXT NOT NULL,outbox_ids_json TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS source_dispositions(source_version_id TEXT PRIMARY KEY REFERENCES source_records(source_version_id),job_id TEXT NOT NULL REFERENCES accepted_job_artifacts(job_id),status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS extraction_receipts(job_id TEXT PRIMARY KEY REFERENCES accepted_job_artifacts(job_id),batch_id TEXT NOT NULL REFERENCES capture_batches(batch_id),receipt_id TEXT UNIQUE NOT NULL REFERENCES canonical_transactions(transaction_id) DEFERRABLE INITIALLY DEFERRED,input_fingerprint TEXT NOT NULL,receipt_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS processing_receipts(batch_id TEXT PRIMARY KEY REFERENCES capture_batches(batch_id),receipt_id TEXT UNIQUE NOT NULL,transaction_id TEXT NOT NULL REFERENCES canonical_transactions(transaction_id),outbox_id TEXT NOT NULL,receipt_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS semantic_migration_receipts(receipt_id TEXT PRIMARY KEY REFERENCES canonical_transactions(transaction_id),receipt_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS action_receipts(action_key TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES accepted_job_artifacts(job_id),transaction_id TEXT NOT NULL REFERENCES canonical_transactions(transaction_id),input_fingerprint TEXT NOT NULL,action_json TEXT NOT NULL,receipt_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(job_id TEXT PRIMARY KEY REFERENCES accepted_job_artifacts(job_id),transaction_id TEXT NOT NULL REFERENCES canonical_transactions(transaction_id),decision_json TEXT NOT NULL,read_set_json TEXT NOT NULL,receipt_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposal_supersessions(job_id TEXT PRIMARY KEY REFERENCES accepted_job_artifacts(job_id),replacement_job_id TEXT NOT NULL REFERENCES accepted_job_artifacts(job_id),receipt_id TEXT UNIQUE NOT NULL,transaction_id TEXT NOT NULL REFERENCES canonical_transactions(transaction_id),proof_json TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS change_events(change_id INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL REFERENCES items(item_id),revision INTEGER NOT NULL,semantic_effect TEXT NOT NULL,created_at REAL NOT NULL,transaction_id TEXT NOT NULL REFERENCES canonical_transactions(transaction_id) DEFERRABLE INITIALLY DEFERRED,dependencies_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projection_outbox(outbox_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,revision INTEGER NOT NULL,transaction_id TEXT NOT NULL REFERENCES canonical_transactions(transaction_id) DEFERRABLE INITIALLY DEFERRED,status TEXT NOT NULL,receipt_id TEXT,UNIQUE(owner_id,revision));
CREATE TABLE IF NOT EXISTS index_outbox(outbox_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,owner_type TEXT NOT NULL,revision INTEGER NOT NULL,transaction_id TEXT NOT NULL REFERENCES canonical_transactions(transaction_id) DEFERRABLE INITIALLY DEFERRED,status TEXT NOT NULL,receipt_id TEXT,UNIQUE(owner_type,owner_id,revision));
CREATE TABLE IF NOT EXISTS legacy_mappings(kind TEXT NOT NULL,legacy_id TEXT NOT NULL,target_type TEXT NOT NULL,target_id TEXT NOT NULL,disposition TEXT NOT NULL,metadata_json TEXT NOT NULL,PRIMARY KEY(kind,legacy_id,target_type,target_id));
CREATE TABLE IF NOT EXISTS annotation_preservation(path TEXT PRIMARY KEY,sha256 TEXT NOT NULL,backup_path TEXT NOT NULL,status TEXT NOT NULL,metadata_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS source_occurrence ON source_records(source_namespace,chat_id,source_id,revision);
CREATE INDEX IF NOT EXISTS source_batch_versions ON capture_batch_members(source_version_id);
CREATE INDEX IF NOT EXISTS semantic_jobs_batch ON jobs(kind,json_extract(inputs_json,'$.batch_id'));
CREATE INDEX IF NOT EXISTS semantic_jobs_parent ON jobs(kind,json_extract(inputs_json,'$.parent_job_id'));
CREATE INDEX IF NOT EXISTS reverse_item_relation ON item_relations(to_item_id);
CREATE INDEX IF NOT EXISTS observation_source ON observation_evidence(span_id);
CREATE UNIQUE INDEX IF NOT EXISTS observation_resource_identity ON resource_refs(resource_id,occurrence_id,observation_id) WHERE item_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS item_resource_identity ON resource_refs(resource_id,occurrence_id,item_id) WHERE observation_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS source_native_revision ON source_records(source_namespace,chat_id,source_id,native_revision) WHERE native_revision IS NOT NULL;
"""


def _decode(row):
    if row is None:
        return None
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json"):
            result[key[:-5]] = json.loads(result.pop(key))
    return result


class SemanticStore:
    def __init__(self, path, generation_id, now_func=time.time):
        self.path = os.path.abspath(os.path.expanduser(os.fspath(path)))
        self.generation_id, self.now_func = text(generation_id), now_func
        current = Path(self.path)
        if any(p.is_symlink() for p in (current, *current.parents)):
            raise SemanticStoreError("semantic_store_symlink")
        services = create_platform_services()
        services.require("private_storage").ensure_directory(str(current.parent))
        if not current.exists():
            fd = os.open(current, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        elif not current.is_file():
            raise SemanticStoreError("semantic_store_not_regular")
        with closing(self.connect()) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables:
                if "generation_manifest" not in tables:
                    raise SemanticStoreError("semantic_generation_mismatch")
                rows = conn.execute("SELECT generation_id,schema FROM generation_manifest").fetchall()
                if len(rows) != 1 or tuple(rows[0]) != (generation_id, SCHEMA):
                    raise SemanticStoreError("semantic_generation_mismatch")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(DDL)
            row = conn.execute("SELECT generation_id,schema FROM generation_manifest").fetchall()
            if row and (len(row) != 1 or tuple(row[0]) != (generation_id, SCHEMA)):
                raise SemanticStoreError("semantic_generation_mismatch")
            conn.execute("INSERT OR IGNORE INTO generation_manifest(generation_id,schema,created_at) VALUES(?,?,?)",
                         (generation_id, SCHEMA, self.now_func()))
            conn.commit()

    def connect(self):
        current = Path(self.path)
        if any(p.is_symlink() for p in (current, *current.parents)):
            raise SemanticStoreError("semantic_store_symlink")
        if not current.is_file():
            raise SemanticStoreError("semantic_store_not_regular")
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
        except sqlite3.DatabaseError as exc:
            conn.close()
            # Busy, full and I/O failures do not prove corruption. Preserve the
            # SQLite result class without exposing its path-bearing message.
            result = getattr(exc, "sqlite_errorcode", None)
            primary = result & 0xff if type(result) is int else None
            code = {
                sqlite3.SQLITE_BUSY: "semantic_database_busy",
                sqlite3.SQLITE_LOCKED: "semantic_database_busy",
                sqlite3.SQLITE_FULL: "semantic_storage_full",
                sqlite3.SQLITE_IOERR: "semantic_database_io_error",
                sqlite3.SQLITE_CORRUPT: "semantic_database_invalid",
                sqlite3.SQLITE_NOTADB: "semantic_database_invalid",
            }.get(primary, "semantic_database_unavailable")
            raise SemanticStoreError(code) from exc
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise SemanticStoreError("semantic_constraint_failed") from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def read_revision(self):
        with closing(self.connect()) as conn:
            return conn.execute("SELECT revision FROM generation_manifest").fetchone()[0]

    def capture_batch(self, batch_id, records, *, source_namespace, chat_id, inventory_digest,
                      inventory_revision, input_cursors, output_cursors, source_eof, purpose="live"):
        text(batch_id); text(source_namespace); text(chat_id); text(inventory_digest)
        integer(inventory_revision)
        if type(source_eof) is not bool or not isinstance(records, (list, tuple)):
            raise SemanticStoreError("semantic_invalid_capture")
        binding = dict(source_namespace=source_namespace, chat_id=chat_id,
                       inventory_digest=inventory_digest, inventory_revision=inventory_revision,
                       input_cursors=input_cursors, output_cursors=output_cursors,
                       source_eof=source_eof, purpose=purpose)
        signature = digest([binding, records])
        now, members = timestamp(self.now_func()), []
        with self.transaction() as conn:
            prior = conn.execute("SELECT * FROM capture_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if prior:
                if prior["input_fingerprint"] != signature:
                    raise SemanticStoreError("capture_batch_identity_conflict")
                return json.loads(prior["receipt_json"])
            source_ids = set()
            for record in records:
                if not isinstance(record, dict):
                    raise SemanticStoreError("semantic_invalid_capture")
                source_id = text(record.get("source_id"))
                if source_id in source_ids:
                    raise SemanticStoreError("capture_duplicate_source")
                source_ids.add(source_id)
                grade = record.get("provenance_grade", "raw_exact")
                if grade not in PROVENANCE or not isinstance(record.get("text"), str):
                    raise SemanticStoreError("capture_invalid_provenance")
                if type(record.get("core", True)) is not bool:
                    raise SemanticStoreError("capture_invalid_core")
                head = conn.execute("SELECT r.* FROM source_heads h JOIN source_records r USING(source_version_id) WHERE h.source_namespace=? AND h.chat_id=? AND h.source_id=?",
                                    (source_namespace, chat_id, source_id)).fetchone()
                payload = record.get("payload", {})
                if not isinstance(payload, dict):
                    raise SemanticStoreError("capture_invalid_payload")
                resources = self._resources(record.get("resources", payload.get("resources", [])))
                material = {"text": record["text"], "payload": payload, "kind": record.get("kind", "message"),
                            "grade": grade, "sent_at": float(timestamp(record.get("source_sent_at", 0))),
                            "resources": resources}
                content_hash = digest(material)
                native = record.get("native_revision")
                if native is not None:
                    text(native)
                if head and head["content_hash"] == content_hash and head["native_revision"] == native:
                    version_id = head["source_version_id"]
                else:
                    if native is not None and conn.execute("SELECT 1 FROM source_records WHERE source_namespace=? AND chat_id=? AND source_id=? AND native_revision=?", (source_namespace, chat_id, source_id, native)).fetchone():
                        raise SemanticStoreError("source_native_revision_reused")
                    version_id = str(uuid.uuid4())
                    conn.execute("""INSERT INTO source_records(source_version_id,source_id,source_namespace,chat_id,logical_shard_id,physical_generation,source_cursor_token,source_sent_at,captured_at,revision,native_revision,kind,provenance_grade,content_hash,text,payload_json,supersedes_source_version_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                 (version_id, source_id, source_namespace, chat_id, text(record.get("logical_shard_id")),
                                  text(record.get("physical_generation")), text(record.get("source_cursor_token")), material["sent_at"], now,
                                  head["revision"] + 1 if head else 1, native, material["kind"], grade, content_hash,
                                  record["text"], dumps(payload), head["source_version_id"] if head else None))
                    for resource in resources:
                        conn.execute("INSERT INTO source_resource_refs VALUES(?,?,?,?,?,?,?)",
                                     (version_id, resource["resource_id"], resource["occurrence_id"], resource["purpose"],
                                      resource["status"], resource["location"], dumps(resource["metadata"])))
                    conn.execute("INSERT INTO source_heads VALUES(?,?,?,?) ON CONFLICT(source_namespace,chat_id,source_id) DO UPDATE SET source_version_id=excluded.source_version_id",
                                 (source_namespace, chat_id, source_id, version_id))
                members.append((version_id, record.get("core", True)))
            receipt = {"receipt_id": str(uuid.uuid4()), "batch_id": batch_id, "generation_id": self.generation_id,
                       **binding, "member_version_ids": [m[0] for m in members]}
            conn.execute("INSERT INTO capture_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (batch_id, receipt["receipt_id"], signature, source_namespace, chat_id, inventory_digest,
                          inventory_revision, dumps(input_cursors), dumps(output_cursors), int(source_eof), purpose, now, dumps(receipt)))
            conn.executemany("INSERT INTO capture_batch_members VALUES(?,?,?,?)",
                             [(batch_id, i, version, int(core)) for i, (version, core) in enumerate(members)])
            conn.execute("UPDATE generation_manifest SET revision=revision+1")
            return receipt

    def get_source(self, source_version_id):
        with closing(self.connect()) as conn:
            conn.execute("BEGIN")
            result = _decode(conn.execute("SELECT * FROM source_records WHERE source_version_id=?", (source_version_id,)).fetchone())
            if result:
                result["resources"] = self._source_resources(conn, source_version_id)
            return result

    def get_batch(self, batch_id):
        with closing(self.connect()) as conn:
            conn.execute("BEGIN")
            result = _decode(conn.execute("SELECT * FROM capture_batches WHERE batch_id=?", (batch_id,)).fetchone())
            if result is not None:
                result["source_eof"] = bool(result["source_eof"])
                result["members"] = [_decode(row) for row in conn.execute("SELECT r.*,m.core FROM capture_batch_members m JOIN source_records r USING(source_version_id) WHERE batch_id=? ORDER BY ordinal", (batch_id,))]
                for member in result["members"]:
                    member["core"] = bool(member["core"])
                    member["resources"] = self._source_resources(conn, member["source_version_id"])
            return result

    def list_batches(self):
        with closing(self.connect()) as conn:
            return [_decode(row) for row in conn.execute("SELECT * FROM capture_batches ORDER BY captured_at,rowid")]

    @staticmethod
    def _resources(value):
        if not isinstance(value, list):
            raise SemanticStoreError("capture_invalid_resources")
        result, seen = [], set()
        for resource in value:
            if not isinstance(resource, dict) or set(resource) != {"resource_id", "occurrence_id", "purpose", "status", "location", "metadata"}:
                raise SemanticStoreError("capture_invalid_resources")
            for field in ("resource_id", "occurrence_id", "purpose", "status"):
                text(resource[field])
            if resource["location"] is not None:
                text(resource["location"])
            if not isinstance(resource["metadata"], dict):
                raise SemanticStoreError("capture_invalid_resources")
            key = resource["resource_id"], resource["occurrence_id"]
            if key in seen:
                raise SemanticStoreError("capture_duplicate_resource")
            seen.add(key)
            result.append(json.loads(dumps(resource)))
        return sorted(result, key=lambda r: (r["resource_id"], r["occurrence_id"]))

    @staticmethod
    def _source_resources(conn, version_id):
        return [{k: v for k, v in _decode(row).items() if k != "source_version_id"}
                for row in conn.execute("SELECT * FROM source_resource_refs WHERE source_version_id=? ORDER BY resource_id,occurrence_id", (version_id,))]

    def _accepted(self, conn, job_id):
        row = conn.execute("SELECT a.*,j.job_key,j.kind,j.inputs_json,j.versions_json,j.state FROM accepted_job_artifacts a JOIN jobs j USING(job_id) WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise SemanticStoreError("accepted_artifact_required")
        artifact, inputs, versions = (json.loads(row[k]) for k in ("artifact_json", "inputs_json", "versions_json"))
        key = digest({"generation_id": self.generation_id, "kind": row["kind"], "inputs": inputs, "versions": versions})
        if (not isinstance(artifact, dict) or row["artifact_hash"] != digest(artifact) or
                row["job_key"] != key or row["state"] not in {"accepted", "done"}):
            raise SemanticStoreError("accepted_artifact_corrupt")
        return artifact

    def _extraction_inputs(self, conn, job_id, batch_id, members):
        job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        inputs = json.loads(job["inputs_json"])
        prompt_version = json.loads(job["versions_json"]).get("prompt")
        if prompt_version not in {EXTRACTION_PROMPT_VERSION, "semantic-extraction-v1"}:
            raise SemanticStoreError("extraction_prompt_version_conflict")
        attributed = prompt_version == EXTRACTION_PROMPT_VERSION
        supplied = inputs.get("members")
        if job["kind"] != "extract" or inputs.get("batch_id") != batch_id or not isinstance(supplied, list):
            raise SemanticStoreError("extraction_input_mismatch")
        if len(supplied) != len(members) or {m.get("source_id") for m in supplied} != set(members):
            raise SemanticStoreError("extraction_input_mismatch")
        for value in supplied:
            source = members[value["source_id"]]
            if (value.get("text") != source["text"] or type(value.get("core")) is not bool or
                    value["core"] != bool(source["core"]) or
                    type(value.get("occurred_at")) not in (float, int) or
                    value["occurred_at"] != source["source_sent_at"]):
                raise SemanticStoreError("extraction_input_mismatch")
            payload = json.loads(source["payload_json"])
            anchors = captured_source_metadata(source["source_namespace"], source["chat_id"], payload, legacy=not attributed)
            if (attributed and not {"speaker_id", "speaker_name"} <= set(value)
                    or not attributed and any(value.get(field) is not None for field in ("speaker_id", "speaker_name"))):
                raise SemanticStoreError("extraction_input_mismatch")
            if any(value.get(field) != expected for field, expected in anchors.items()):
                raise SemanticStoreError("extraction_input_mismatch")
        return supplied

    @staticmethod
    def _insert_resource(conn, resource, *, observation_id, item_id=None):
        values = (resource["resource_id"], observation_id, item_id, resource["occurrence_id"],
                  resource["purpose"], resource["status"], resource["location"], dumps(resource["metadata"]))
        old = conn.execute("SELECT * FROM resource_refs WHERE resource_id=? AND observation_id IS ? AND item_id IS ? AND occurrence_id=?", values[:4]).fetchone()
        if old and tuple(old) != values:
            raise SemanticStoreError("resource_identity_conflict")
        if not old:
            conn.execute("INSERT INTO resource_refs VALUES(?,?,?,?,?,?,?,?)", values)

    def commit_extraction(self, job_id, batch_id, observations, dispositions=None):
        observations = json.loads(dumps(observations))
        supplied = json.loads(dumps(dispositions or {}))
        if not isinstance(observations, list) or not isinstance(supplied, dict):
            raise SemanticStoreError("extraction_invalid_shape")
        signature = digest([batch_id, observations, supplied])
        now = timestamp(self.now_func())
        with self.transaction() as conn:
            accepted = self._accepted(conn, job_id)
            if accepted.get("observations") != observations or accepted.get("dispositions", {}) != supplied:
                raise SemanticStoreError("extraction_artifact_mismatch")
            members = {row["source_version_id"]: dict(row) for row in conn.execute("SELECT r.*,m.core FROM capture_batch_members m JOIN source_records r USING(source_version_id) WHERE batch_id=? ORDER BY m.ordinal", (batch_id,))}
            if not conn.execute("SELECT 1 FROM capture_batches WHERE batch_id=?", (batch_id,)).fetchone():
                raise SemanticStoreError("capture_batch_missing")
            input_members = self._extraction_inputs(conn, job_id, batch_id, members)
            if not set(supplied) <= set(members):
                raise SemanticStoreError("extraction_disposition_source_mismatch")
            prior = conn.execute("SELECT * FROM extraction_receipts WHERE job_id=?", (job_id,)).fetchone()
            if prior:
                if prior["input_fingerprint"] != signature:
                    raise SemanticStoreError("extraction_replay_conflict")
                return json.loads(prior["receipt_json"])
            adopted_jobs = set()
            for vid, member in members.items():
                if member["core"]:
                    owner = conn.execute("SELECT job_id FROM source_core_jobs WHERE source_version_id=?", (vid,)).fetchone()
                    if owner and owner[0] != job_id:
                        member["core"] = 0
                        adopted_jobs.add(owner[0])
                    elif not owner:
                        conn.execute("INSERT INTO source_core_jobs VALUES(?,?)", (vid, job_id))
            cited, observation_ids, outboxes = set(), [], []
            redacted_sources = {}
            for index, obs in enumerate(observations):
                if not isinstance(obs, dict):
                    raise SemanticStoreError("extraction_invalid_shape")
                oid = text(obs.get("observation_id"))
                if oid in observation_ids:
                    raise SemanticStoreError("observation_identity_conflict")
                spans = obs.get("spans")
                if not isinstance(spans, list) or not spans or not any(members.get(s.get("source_id"), {}).get("core") for s in spans):
                    raise SemanticStoreError("observation_core_evidence_required")
                source_times, span_ids = [], set()
                for span in spans:
                    source = members.get(span.get("source_id"))
                    if source is None:
                        raise SemanticStoreError("observation_unknown_source")
                    a, b = integer(span.get("start")), integer(span.get("end"))
                    vid = source["source_version_id"]
                    if vid not in redacted_sources:
                        redacted_sources[vid] = redact_urls_with_offsets(source["text"])
                    visible, replacement_ranges = redacted_sources[vid]
                    expected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, dumps([self.generation_id, source["source_version_id"], a, b])))
                    if (a >= b or a % 2000 or b != min(a + 2000, len(visible)) or
                            span.get("span_id") != expected_id or visible[a:b] != span.get("excerpt")):
                        raise SemanticStoreError("observation_invalid_span")
                    if expected_id in span_ids:
                        raise SemanticStoreError("observation_invalid_span")
                    span_ids.add(expected_id)
                    mapping = []
                    for tag, i, j, x, y in replacement_ranges:
                        if x < b and y > a:
                            mapping.append({"raw_start": i + max(0, a-x) if tag == "equal" else i,
                                            "raw_end": j - max(0, y-b) if tag == "equal" else j,
                                            "visible_start": max(a, x), "visible_end": min(b, y)})
                    if not mapping:
                        raise SemanticStoreError("observation_unmapped_span")
                    values = (expected_id, source["source_version_id"], a, b, mapping[0]["raw_start"], mapping[-1]["raw_end"], span["excerpt"], digest(span["excerpt"]), dumps(mapping))
                    old = conn.execute("SELECT * FROM source_spans WHERE span_id=?", (expected_id,)).fetchone()
                    if old and tuple(old) != values:
                        raise SemanticStoreError("source_span_identity_conflict")
                    if not old:
                        conn.execute("INSERT INTO source_spans VALUES(?,?,?,?,?,?,?,?,?)", values)
                    source_times.append(source["source_sent_at"])
                    cited.add(source["source_version_id"])
                existing = conn.execute("SELECT * FROM observations WHERE observation_id=?", (oid,)).fetchone()
                if existing:
                    original = self._accepted(conn, existing["accepted_job_id"])
                    if obs not in original.get("observations", []):
                        raise SemanticStoreError("observation_identity_conflict")
                    observation_ids.append(oid)
                    continue
                if obs.get("scene_kind") not in {"technical", "story", "gossip", "life", "quote", "other"}:
                    raise SemanticStoreError("observation_invalid_scene")
                ids(obs.get("topics", []), empty=True)
                conn.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)", (oid, job_id, str(index), text(obs.get("title")), obs["scene_kind"], min(source_times), max(source_times), now, "unresolved", dumps(obs.get("topics", []))))
                conn.executemany("INSERT INTO observation_evidence VALUES(?,?,?,?)", [(oid, sid, "supports", None) for sid in sorted(span_ids)])
                units = obs.get("units")
                if not isinstance(units, list) or not units:
                    raise SemanticStoreError("observation_content_required")
                for unit in units:
                    if not isinstance(unit, dict) or not set(ids(unit.get("span_ids"))) <= span_ids:
                        raise SemanticStoreError("content_unit_evidence_invalid")
                    if unit.get("kind") not in {"fact", "step", "quote", "scene", "counterevidence", "resource"}:
                        raise SemanticStoreError("content_unit_kind_invalid")
                    body = text(unit.get("text"))
                    if body != redact_urls_in_text(body):
                        raise SemanticStoreError("content_unit_unredacted")
                    if unit["kind"] == "quote":
                        passages, fragments, prior = [], [], None
                        for span in sorted((span for span in spans if span["span_id"] in unit["span_ids"]),
                                           key=lambda span: (span["source_id"], span["start"])):
                            if prior is not None and (prior["source_id"] != span["source_id"] or prior["end"] != span["start"]):
                                passages.append("".join(fragments))
                                fragments = []
                            fragments.append(span["excerpt"])
                            prior = span
                        passages.append("".join(fragments))
                        if not any(body in passage for passage in passages):
                            raise SemanticStoreError("content_unit_quote_invalid")
                    if conn.execute("SELECT 1 FROM content_units WHERE unit_id=?", (text(unit.get("unit_id")),)).fetchone():
                        raise SemanticStoreError("content_unit_identity_conflict")
                    conn.execute("INSERT INTO content_units VALUES(?,?,?,?,?)", (unit["unit_id"], oid, unit["kind"], body, dumps(unit["span_ids"])))
                claims = obs.get("claims", [])
                if not isinstance(claims, list):
                    raise SemanticStoreError("claim_invalid_shape")
                for slot, claim in enumerate(claims):
                    source_ids = {span["source_id"] for span in spans if span["span_id"] in claim.get("span_ids", [])}
                    cited_members = [m for m in input_members if m["source_id"] in source_ids]
                    for field in ("subject_id", "case_id"):
                        candidates = {m.get(field) for m in cited_members}
                        if field == "subject_id":
                            candidates.update(m.get("speaker_id") for m in cited_members)
                        if claim.get(field) is not None and claim[field] not in candidates:
                            raise SemanticStoreError("claim_unprovided_anchor")
                    materials = {m.get("material_id") for m in input_members if m["source_id"] in source_ids}
                    if claim.get("material_id") != (next(iter(materials)) if len(materials) == 1 else None):
                        raise SemanticStoreError("claim_unprovided_material")
                    self._insert_claim(conn, oid, slot, claim, span_ids)
                for vid in {s["source_id"] for s in spans}:
                    for resource in self._source_resources(conn, vid):
                        self._insert_resource(conn, resource, observation_id=oid)
                outbox = str(uuid.uuid4())
                conn.execute("INSERT INTO index_outbox VALUES(?,?,?,?,?,?,?)", (outbox, oid, "observation", 1, job_id, "pending", None))
                outboxes.append(outbox)
                observation_ids.append(oid)
            written_dispositions = {}
            for vid, source in members.items():
                if not source["core"]:
                    continue
                status = supplied.get(vid)
                if status and status.get("status") == "unclassified":
                    text(status.get("reason"))
                    continue
                if vid in cited:
                    status = {"status": "observed", "reason": "grounded_observation"}
                if not isinstance(status, dict) or status.get("status") not in DISPOSITIONS:
                    raise SemanticStoreError("source_disposition_required")
                text(status.get("reason"))
                if status["status"] == "observed" and vid not in cited:
                    raise SemanticStoreError("source_observation_missing")
                old = conn.execute("SELECT * FROM source_dispositions WHERE source_version_id=?", (vid,)).fetchone()
                values = (vid, job_id, status["status"], status["reason"])
                if old and tuple(old) != values:
                    raise SemanticStoreError("source_disposition_identity_conflict")
                if not old:
                    conn.execute("INSERT INTO source_dispositions VALUES(?,?,?,?)", values)
                written_dispositions[vid] = status
            tx = str(uuid.uuid4())
            conn.execute("UPDATE index_outbox SET transaction_id=? WHERE transaction_id=?", (tx, job_id))
            receipt = {"receipt_id": tx, "batch_id": batch_id, "job_id": job_id,
                       "observation_ids": observation_ids, "dispositions": written_dispositions,
                       "adopted_core_job_ids": sorted(adopted_jobs), "outbox_ids": outboxes,
                       "input_fingerprint": signature, "generation_id": self.generation_id}
            conn.execute("INSERT INTO extraction_receipts VALUES(?,?,?,?,?)", (job_id, batch_id, tx, signature, dumps(receipt)))
            conn.execute("INSERT INTO canonical_transactions VALUES(?,?,?,?,?)", (tx, job_id,
                         dumps([{"type": "COMMIT_EXTRACTION", "batch_id": batch_id, "observation_ids": observation_ids,
                                 "dispositions": written_dispositions}]), dumps(outboxes), now))
            conn.execute("UPDATE generation_manifest SET revision=revision+1")
            return receipt

    @staticmethod
    def _insert_claim(conn, oid, slot, claim, span_ids):
        if not isinstance(claim, dict) or not set(ids(claim.get("span_ids"))) <= span_ids:
            raise SemanticStoreError("claim_evidence_invalid")
        predicates = claim.get("predicates")
        if (not isinstance(predicates, dict) or not predicates or
                any(not isinstance(key, str) or not key.strip() or value is not None and type(value) not in (str, bool, int, float) for key, value in predicates.items())):
            raise SemanticStoreError("claim_invalid_predicates")
        if claim.get("status", "active") != "active" or claim.get("retracts", []):
            raise SemanticStoreError("claim_relation_required")
        observed_at = timestamp(claim.get("observed_at", 0))
        source_times = {row[0] for span_id in claim["span_ids"] for row in conn.execute(
            "SELECT r.source_sent_at FROM source_spans s JOIN source_records r USING(source_version_id) WHERE s.span_id=?", (span_id,))}
        if observed_at not in source_times:
            raise SemanticStoreError("claim_time_not_grounded")
        subject, case = claim.get("subject_id"), claim.get("case_id")
        for key in ("subject_id", "case_id", "condition", "channel", "version", "phase", "material_id"):
            if claim.get(key) is not None:
                text(claim[key])
        for key in ("valid_from", "valid_to"):
            if claim.get(key) is not None:
                timestamp(claim[key])
        if claim.get("valid_from") is not None and claim.get("valid_to") is not None and claim["valid_from"] > claim["valid_to"]:
            raise SemanticStoreError("claim_invalid_interval")
        if subject and not conn.execute("SELECT 1 FROM entities WHERE entity_id=?", (subject,)).fetchone():
            conn.execute("INSERT INTO entities(entity_id,kind,name) VALUES(?,?,?)", (subject, "unresolved", subject))
        if case:
            old = conn.execute("SELECT * FROM cases WHERE case_id=?", (case,)).fetchone()
            if old and old["subject_id"] != subject:
                raise SemanticStoreError("case_identity_conflict")
            if not old:
                conn.execute("INSERT INTO cases(case_id,subject_id,phase) VALUES(?,?,?)", (case, subject, claim.get("phase")))
        values = (text(claim.get("claim_id")), subject, case, claim.get("condition"), claim.get("channel"), claim.get("version"), claim.get("phase"),
                  observed_at, dumps(predicates), dumps(claim["span_ids"]), "active", "[]", claim.get("material_id"),
                  claim.get("valid_from"), claim.get("valid_to"), text(claim.get("polarity", "asserted")), text(claim.get("modality", "reported")), "v1")
        old = conn.execute("SELECT * FROM claims WHERE claim_id=?", (values[0],)).fetchone()
        if old and any(old[index] != value for index, value in enumerate(values) if index not in {10, 11, 12}):
            raise SemanticStoreError("claim_identity_conflict")
        if not old:
            conn.execute("INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
        conn.execute("INSERT INTO observation_claims(observation_id,assertion_slot,claim_id) VALUES(?,?,?)", (oid, slot, claim["claim_id"]))

    def get_observation(self, observation_id):
        with closing(self.connect()) as conn:
            conn.execute("BEGIN")
            return self._observation(conn, observation_id)

    @staticmethod
    def _observation(conn, observation_id):
        row = _decode(conn.execute("SELECT * FROM observations WHERE observation_id=?", (observation_id,)).fetchone())
        if row is None:
            return None
        row["units"] = [_decode(r) for r in conn.execute("SELECT * FROM content_units WHERE observation_id=? ORDER BY rowid", (observation_id,))]
        row["spans"] = [_decode(r) for r in conn.execute("SELECT s.* FROM source_spans s JOIN observation_evidence e USING(span_id) WHERE observation_id=?", (observation_id,))]
        row["claims"] = [_decode(r) for r in conn.execute("SELECT c.* FROM claims c JOIN observation_claims o USING(claim_id) WHERE observation_id=?", (observation_id,))]
        row["source_ids"] = list(dict.fromkeys(s["source_version_id"] for s in row["spans"]))
        row["resources"] = [_decode(r) for r in conn.execute("SELECT * FROM resource_refs WHERE observation_id=? AND item_id IS NULL ORDER BY resource_id,occurrence_id", (observation_id,))]
        return row

    @staticmethod
    def _item(conn, item_id):
        row = _decode(conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone())
        if row is None:
            return None
        row["revision"] = row["content_revision"]
        members = list(conn.execute("SELECT observation_id,role FROM item_observations WHERE item_id=? ORDER BY observation_id", (item_id,)))
        row["observation_ids"] = [r[0] for r in members]
        row["observations"] = [{**SemanticStore._observation(conn, member[0]), "role": member[1]} for member in members]
        row["units"] = [unit for obs in row["observations"] if obs["role"] != "evidence_only" for unit in obs["units"]]
        row["claims"] = [_decode(r) for r in conn.execute("SELECT c.*,i.role FROM claims c JOIN item_claims i USING(claim_id) WHERE item_id=? ORDER BY c.claim_id", (item_id,))]
        row["claim_ids"] = [c["claim_id"] for c in row["claims"]]
        row["relations"] = [_decode(r) for r in conn.execute("SELECT * FROM item_relations WHERE from_item_id=? OR to_item_id=?", (item_id, item_id))]
        row["aliases"] = [r[0] for r in conn.execute("SELECT new_item_id FROM item_aliases WHERE old_item_id=?", (item_id,))]
        row["memberships"] = [_decode(r) for r in conn.execute("SELECT * FROM view_memberships WHERE item_id=? ORDER BY view_id", (item_id,))]
        row["resources"] = [_decode(r) for r in conn.execute("SELECT * FROM resource_refs WHERE item_id=? ORDER BY resource_id,occurrence_id,observation_id", (item_id,))]
        return row

    def get_item(self, item_id):
        with closing(self.connect()) as conn:
            conn.execute("BEGIN")
            return self._item(conn, item_id)

    def list_items(self, *, include_retired=False):
        with closing(self.connect()) as conn:
            conn.execute("BEGIN")
            rows = conn.execute("SELECT item_id FROM items" + ("" if include_retired else " WHERE lifecycle='active'") + " ORDER BY item_id").fetchall()
            return [self._item(conn, row[0]) for row in rows]

    def list_claims(self):
        with closing(self.connect()) as conn:
            conn.execute("BEGIN")
            rows = [_decode(r) for r in conn.execute("SELECT * FROM claims ORDER BY claim_id")]
            for row in rows:
                row["source_ids"] = [r[0] for r in conn.execute("SELECT DISTINCT source_version_id FROM source_spans WHERE span_id IN (" + ",".join("?" for _ in row["span_ids"]) + ")", row["span_ids"])]
                row["item_ids"] = [r[0] for r in conn.execute("SELECT item_id FROM item_claims WHERE claim_id=?", (row["claim_id"],))]
                row["item_roles"] = dict(conn.execute("SELECT item_id,role FROM item_claims WHERE claim_id=?", (row["claim_id"],)).fetchall())
            return rows

    def list_relations(self):
        with closing(self.connect()) as conn:
            return [_decode(r) for r in conn.execute("SELECT * FROM item_relations ORDER BY relation_id")]

    def coverage(self):
        with closing(self.connect()) as conn:
            conn.execute("BEGIN")
            unread = conn.execute("SELECT count(*) FROM source_records s LEFT JOIN source_dispositions d USING(source_version_id) WHERE d.source_version_id IS NULL").fetchone()[0]
            unclassified = conn.execute("SELECT count(*) FROM observations WHERE resolution_status='unresolved'").fetchone()[0]
            tails = conn.execute("SELECT source_namespace,chat_id,max(rowid) FROM capture_batches GROUP BY source_namespace,chat_id").fetchall()
            complete = bool(tails) and all(conn.execute("SELECT source_eof FROM capture_batches WHERE rowid=?", (r[2],)).fetchone()[0] == 1 for r in tails)
            return {"source_complete": complete, "unread_sources": unread, "unclassified_sources": unclassified}

    def documents(self):
        with closing(self.connect()) as conn:
            conn.execute("BEGIN")
            docs = []
            for row in conn.execute("SELECT * FROM source_records ORDER BY capture_seq"):
                payload = json.loads(row["payload_json"])
                docs.append({"owner_type": "source", "owner_id": row["source_version_id"], "revision": row["revision"], "text": redact_urls_in_text(row["text"]), "capture_seq": row["capture_seq"], "metadata": {"source_ids": [row["source_version_id"]], "case_id": payload.get("case_id"), "reply_to": payload.get("reply_to"), "related_ids": payload.get("related_ids", [])}})
            for row in conn.execute("SELECT observation_id FROM observations"):
                obs = self._observation(conn, row[0])
                seq = max((conn.execute("SELECT capture_seq FROM source_records WHERE source_version_id=?", (sid,)).fetchone()[0] for sid in obs["source_ids"]), default=0)
                docs.append({"owner_type": "observation", "owner_id": row[0], "revision": 1, "text": obs["title"] + "\n" + "\n".join(u["text"] for u in obs["units"]), "capture_seq": seq, "metadata": {"source_ids": obs["source_ids"], "related_ids": []}})
            for row in conn.execute("SELECT item_id FROM items WHERE lifecycle='active'"):
                item = self._item(conn, row[0])
                source_ids = list(dict.fromkeys(sid for obs in item["observations"] for sid in obs["source_ids"]))
                seq = max((conn.execute("SELECT capture_seq FROM source_records WHERE source_version_id=?", (sid,)).fetchone()[0] for sid in source_ids), default=0)
                docs.append({"owner_type": "item", "owner_id": row[0], "revision": item["content_revision"], "text": item["title"] + "\n" + "\n".join(u["text"] for u in item["units"]), "capture_seq": seq, "metadata": {"item_id": row[0], "source_ids": source_ids, "related_ids": [r["to_item_id"] if r["from_item_id"] == row[0] else r["from_item_id"] for r in item["relations"]]}})
            for row in conn.execute("SELECT * FROM item_relations"):
                docs.append({"owner_type": "relation", "owner_id": row["relation_id"], "revision": 1, "text": row["relation"] + " " + row["reason"], "capture_seq": 0, "metadata": {"related_ids": [row["from_item_id"], row["to_item_id"]], "source_ids": []}})
            return docs

    def create_view(self, view_id, title, definition, *, origin="user"):
        if origin not in {"user", "model", "rule"}:
            raise SemanticStoreError("semantic_invalid_membership_origin")
        with self.transaction() as conn:
            conn.execute("INSERT INTO views VALUES(?,?,?,?,?,?)", (text(view_id), text(title), text(definition), 1, 0, origin))
            conn.execute("UPDATE generation_manifest SET revision=revision+1")
        return self.get_view(view_id)

    def get_view(self, view_id):
        with closing(self.connect()) as conn:
            return _decode(conn.execute("SELECT * FROM views WHERE view_id=?", (view_id,)).fetchone())

    def update_view(self, view_id, definition, expected_revision):
        with self.transaction() as conn:
            result = conn.execute("UPDATE views SET definition=?,definition_revision=definition_revision+1 WHERE view_id=? AND definition_revision=?", (text(definition), view_id, integer(expected_revision, 1)))
            if result.rowcount != 1:
                raise SemanticStoreError("semantic_stale_read_set")
            conn.execute("UPDATE generation_manifest SET revision=revision+1")
        return self.get_view(view_id)

    def _attach(self, conn, item_id, observation_ids, action_key, *, role="supports"):
        changed = False
        for oid in ids(observation_ids):
            if not conn.execute("SELECT 1 FROM observations WHERE observation_id=?", (oid,)).fetchone():
                raise SemanticStoreError("semantic_observation_missing")
            old = conn.execute("SELECT role FROM item_observations WHERE item_id=? AND observation_id=?", (item_id, oid)).fetchone()
            if old is None or old[0] == "evidence_only" and role == "supports":
                conn.execute("INSERT INTO item_observations VALUES(?,?,?,?) ON CONFLICT(item_id,observation_id) DO UPDATE SET role=excluded.role,added_by=excluded.added_by", (item_id, oid, role, action_key))
                changed = True
            actual_role = "supports" if old and old[0] == "supports" else role
            for claim in conn.execute("SELECT claim_id FROM observation_claims WHERE observation_id=?", (oid,)).fetchall():
                conn.execute("INSERT INTO item_claims VALUES(?,?,?) ON CONFLICT(item_id,claim_id) DO UPDATE SET role=CASE WHEN item_claims.role='supports' THEN 'supports' ELSE excluded.role END", (item_id, claim[0], actual_role))
            for resource in conn.execute("SELECT * FROM resource_refs WHERE observation_id=? AND item_id IS NULL", (oid,)).fetchall():
                self._insert_resource(conn, _decode(resource), observation_id=oid, item_id=item_id)
            conn.execute("UPDATE observations SET resolution_status='resolved' WHERE observation_id=?", (oid,))
        return changed

    @staticmethod
    def _check_read_set(conn, read_set):
        if not isinstance(read_set, dict) or set(read_set) != {"store_revision", "items", "views"}:
            raise SemanticStoreError("semantic_stale_read_set")
        revision = conn.execute("SELECT revision FROM generation_manifest").fetchone()[0]
        if type(read_set["store_revision"]) is not int or read_set["store_revision"] != revision:
            raise SemanticStoreError("semantic_stale_read_set")
        if not isinstance(read_set["items"], dict) or not isinstance(read_set["views"], dict):
            raise SemanticStoreError("semantic_stale_read_set")
        for iid, expected in read_set["items"].items():
            row = conn.execute("SELECT content_revision FROM items WHERE item_id=?", (text(iid),)).fetchone()
            if type(expected) is not int or row is None or row[0] != expected:
                raise SemanticStoreError("semantic_stale_read_set")
        for vid, expected in read_set["views"].items():
            row = conn.execute("SELECT definition_revision,override_revision FROM views WHERE view_id=?", (text(vid),)).fetchone()
            if (not isinstance(expected, dict) or any(type(v) is not int for v in expected.values()) or
                    row is None or dict(row) != expected):
                raise SemanticStoreError("semantic_stale_read_set")
        return revision

    @staticmethod
    def _dependent_items(conn, item_ids):
        affected, pending = set(item_ids), list(item_ids)
        while pending:
            iid = pending.pop()
            rows = conn.execute("SELECT to_item_id AS item_id FROM item_relations WHERE from_item_id=? UNION SELECT from_item_id FROM item_relations WHERE to_item_id=? AND direction='symmetric' UNION SELECT old_item_id FROM item_aliases WHERE new_item_id=?", (iid, iid, iid))
            for row in rows:
                if row[0] not in affected:
                    affected.add(row[0]); pending.append(row[0])
        return affected

    def _publish_items(self, conn, effects, tx, now):
        effects = dict(effects)
        for iid in self._dependent_items(conn, effects):
            effects.setdefault(iid, "none")
        # All snapshots observe the post-transaction revisions of their peers.
        for iid, effect in effects.items():
            if effect == "none":
                conn.execute("UPDATE items SET content_revision=content_revision+1 WHERE item_id=?", (iid,))
            else:
                conn.execute("UPDATE items SET content_revision=content_revision+1,semantic_updated_at=? WHERE item_id=?", (now, iid))
        outboxes, seq = [], conn.execute("SELECT max(capture_seq) FROM source_records").fetchone()[0] or 0
        for iid, effect in sorted(effects.items()):
            item = self._item(conn, iid)
            conn.execute("INSERT INTO item_revisions VALUES(?,?,?,?,?,?)", (iid, item["content_revision"], seq, dumps(item), effect, tx))
            dependencies = {"observations": item["observation_ids"], "claims": item["claim_ids"],
                            "relations": [r["relation_id"] for r in item["relations"]], "aliases": item["aliases"],
                            "resources": [r["resource_id"] for r in item["resources"]]}
            conn.execute("INSERT INTO change_events(item_id,revision,semantic_effect,created_at,transaction_id,dependencies_json) VALUES(?,?,?,?,?,?)", (iid, item["content_revision"], effect, now, tx, dumps(dependencies)))
            index, projection = str(uuid.uuid4()), str(uuid.uuid4())
            conn.execute("INSERT INTO index_outbox VALUES(?,?,?,?,?,?,?)", (index, iid, "item", item["content_revision"], tx, "pending", None))
            conn.execute("INSERT INTO projection_outbox VALUES(?,?,?,?,?,?)", (projection, iid, item["content_revision"], tx, "pending", None))
            outboxes.extend((index, projection))
        return sorted(effects), outboxes

    def _claim_relation(self, conn, value, revision):
        validate_claim_relation(value)
        left, right, relation = (value[k] for k in ("from_claim_id", "to_claim_id", "relation"))
        claims = [conn.execute("SELECT * FROM claims WHERE claim_id=?", (iid,)).fetchone() for iid in (left, right)]
        if any(row is None for row in claims):
            raise SemanticStoreError("claim_relation_claim_missing")
        evidence = ids(json.loads(claims[0]["span_ids_json"]))
        for claim in claims:
            for sid in ids(json.loads(claim["span_ids_json"])):
                if not conn.execute("SELECT 1 FROM source_spans WHERE span_id=?", (sid,)).fetchone():
                    raise SemanticStoreError("claim_relation_evidence_missing")
        old = conn.execute("SELECT * FROM claim_relations WHERE from_claim_id=? AND to_claim_id=? AND relation=?", (left, right, relation)).fetchone()
        reason = value.get("reason", "")
        if old:
            if old["reason"] != reason or json.loads(old["evidence_json"]) != evidence:
                raise SemanticStoreError("claim_relation_identity_conflict")
            return {}
        conn.execute("INSERT INTO claim_relations VALUES(?,?,?,?,?,?)", (left, right, relation, reason, dumps(evidence), revision))
        changed = {left, right}
        effect = "none"
        if relation in {"corrects_or_retracts", "corrects", "retracts"}:
            conn.execute("UPDATE claims SET status='retracted' WHERE claim_id=?", (right,))
            retracts = sorted(set(json.loads(claims[0]["retracts_json"])) | {right})
            conn.execute("UPDATE claims SET retracts_json=? WHERE claim_id=?", (dumps(retracts), left))
            conn.execute("UPDATE evidence_witnesses SET valid=0 WHERE claim_id=?", (right,))
            effect = "state_changed"
        elif relation in {"disputes", "contradicts"}:
            conn.execute("UPDATE claims SET status='contested' WHERE claim_id=? AND status!='retracted'", (right,))
            conn.execute("UPDATE evidence_witnesses SET valid=0 WHERE claim_id=?", (right,))
            effect = "state_changed"
        elif relation == "same_material":
            families = {row["material_id"] for row in claims if row["material_id"]}
            family = min(families) if families else "material:" + digest(sorted((left, right)))
            for previous in families:
                changed.update(r[0] for r in conn.execute("SELECT claim_id FROM claims WHERE material_id=?", (previous,)))
            for cid in changed:
                conn.execute("UPDATE claims SET material_id=? WHERE claim_id=?", (family, cid))
                for sid in json.loads(conn.execute("SELECT span_ids_json FROM claims WHERE claim_id=?", (cid,)).fetchone()[0]):
                    conn.execute("UPDATE observation_evidence SET material_family_id=? WHERE span_id=?", (family, sid))
        elif relation in {"independent_support", "refines_scope", "changes_over_time"}:
            effect = "evidence_changed"
        # Claim dependencies and Item dependencies are enumerated, never ranked.
        pending = list(changed)
        while pending:
            cid = pending.pop()
            for row in conn.execute("SELECT to_claim_id FROM claim_relations WHERE from_claim_id=?", (cid,)):
                if row[0] not in changed:
                    changed.add(row[0]); pending.append(row[0])
        item_ids = {row[0] for cid in changed for row in conn.execute("SELECT item_id FROM item_claims WHERE claim_id=?", (cid,))}
        return {iid: effect for iid in item_ids}

    def record_claim_relation(self, from_claim_id, to_claim_id, relation, reason="", expected_revision=None):
        value = {"from_claim_id": from_claim_id, "to_claim_id": to_claim_id, "relation": relation, "reason": reason}
        validate_claim_relation(value)
        tx, now = str(uuid.uuid4()), timestamp(self.now_func())
        with self.transaction() as conn:
            revision = conn.execute("SELECT revision FROM generation_manifest").fetchone()[0]
            if expected_revision is not None and integer(expected_revision) != revision:
                raise SemanticStoreError("semantic_stale_read_set")
            effects = self._claim_relation(conn, value, revision + 1)
            affected, outboxes = self._publish_items(conn, effects, tx, now)
            conn.execute("UPDATE generation_manifest SET revision=revision+1")
            conn.execute("INSERT INTO canonical_transactions VALUES(?,?,?,?,?)", (tx, None, dumps([{"type": "CLAIM_RELATION", **value}]), dumps(outboxes), now))
            return {"receipt_id": tx, "transaction_id": tx, "outbox_id": tx, "outbox_ids": outboxes,
                    "affected_item_ids": affected, "generation_id": self.generation_id, "store_revision": revision + 1}

    @staticmethod
    def _identity_snapshot(conn, item_ids):
        item_ids = sorted(set(item_ids))
        marks = ",".join("?" for _ in item_ids)
        result = {"items": [dict(r) for r in conn.execute(f"SELECT * FROM items WHERE item_id IN ({marks}) ORDER BY item_id", item_ids)]}
        for table in ("item_observations", "item_claims", "view_memberships", "resource_refs"):
            result[table] = sorted((dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE item_id IN ({marks})", item_ids)), key=dumps)
        result["item_aliases"] = sorted((dict(r) for r in conn.execute(f"SELECT * FROM item_aliases WHERE old_item_id IN ({marks})", item_ids)), key=dumps)
        result["item_relations"] = sorted((dict(r) for r in conn.execute(f"SELECT * FROM item_relations WHERE from_item_id IN ({marks}) OR to_item_id IN ({marks})", item_ids + item_ids)), key=dumps)
        return result

    @staticmethod
    def _identity_fingerprint(snapshot):
        # Freshness/revision changes caused by dependencies do not alter identity
        # associations. Actual later body, alias or user-choice changes do.
        snapshot = json.loads(dumps(snapshot))
        for item in snapshot["items"]:
            item.pop("content_revision", None)
            item.pop("semantic_updated_at", None)
        return digest(snapshot)

    @staticmethod
    def _alias(conn, old, new, operation_id):
        if old == new:
            raise SemanticStoreError("semantic_alias_cycle")
        pending, visited = [new], set()
        while pending:
            identity = pending.pop()
            if identity == old:
                raise SemanticStoreError("semantic_alias_cycle")
            if identity not in visited:
                visited.add(identity)
                pending.extend(r[0] for r in conn.execute("SELECT new_item_id FROM item_aliases WHERE old_item_id=?", (identity,)))
        existing = conn.execute("SELECT operation_id FROM item_aliases WHERE old_item_id=? AND new_item_id=?", (old, new)).fetchone()
        if not existing:
            conn.execute("INSERT INTO item_aliases VALUES(?,?,?)", (old, new, operation_id))

    @staticmethod
    def _copy_memberships(conn, source, target, view_reads):
        for member in conn.execute("SELECT * FROM view_memberships WHERE item_id=?", (source,)).fetchall():
            if member["view_id"] not in view_reads:
                raise SemanticStoreError("semantic_view_read_required")
            old = conn.execute("SELECT * FROM view_memberships WHERE view_id=? AND item_id=?", (member["view_id"], target)).fetchone()
            if old:
                source_user = member["origin"] == "user" or member["pinned"] or member["excluded"]
                target_user = old["origin"] == "user" or old["pinned"] or old["excluded"]
                if source_user and target_user and any(member[k] != old[k] for k in ("placement", "pinned", "excluded")):
                    raise SemanticStoreError("semantic_user_membership_conflict")
                if target_user or not source_user:
                    continue
            conn.execute("INSERT INTO view_memberships VALUES(?,?,?,?,?,?,?) ON CONFLICT(view_id,item_id) DO UPDATE SET placement=excluded.placement,origin=excluded.origin,reason=excluded.reason,pinned=excluded.pinned,excluded=excluded.excluded", (member["view_id"], target, member["placement"], member["origin"], member["reason"], member["pinned"], member["excluded"]))
            if member["origin"] == "user":
                conn.execute("UPDATE views SET override_revision=override_revision+1 WHERE view_id=?", (member["view_id"],))

    def _copy_identity_edges(self, conn, source, target, operation_id, tx, view_reads):
        self._copy_memberships(conn, source, target, view_reads)
        # Direct resource edges without an observation still belong to identity.
        for row in conn.execute("SELECT * FROM resource_refs WHERE item_id=? AND observation_id IS NULL", (source,)).fetchall():
            self._insert_resource(conn, _decode(row), observation_id=None, item_id=target)
        for relation in conn.execute("SELECT * FROM item_relations WHERE from_item_id=? OR to_item_id=?", (source, source)).fetchall():
            left = target if relation["from_item_id"] == source else relation["from_item_id"]
            right = target if relation["to_item_id"] == source else relation["to_item_id"]
            if left == right:
                continue
            old = conn.execute("SELECT * FROM item_relations WHERE from_item_id=? AND to_item_id=? AND relation=?", (left, right, relation["relation"])).fetchone()
            if not old:
                conn.execute("INSERT INTO item_relations VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), left, right, relation["relation"], relation["direction"], relation["reason"], relation["evidence_json"], relation["read_revisions_json"], tx))
            # An existing edge is retained alongside the original source edge;
            # no reason/evidence is silently overwritten during identity change.

    def _proposal_inputs(self, conn, job_id, accepted, actions, read_set, decision):
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        inputs = json.loads(row["inputs_json"])
        state = inputs.get("state")
        if (row["kind"] != "propose" or not isinstance(state, dict) or
                accepted.get("read_set") != read_set or state.get("read_set") != read_set):
            raise SemanticStoreError("proposal_input_mismatch")
        incoming = state.get("observations", [])
        candidates = state.get("items", {})
        if not isinstance(incoming, list) or not isinstance(candidates, dict):
            raise SemanticStoreError("proposal_input_mismatch")
        allowed_obs, allowed_claims = set(), set()
        incoming_claims = set()
        for value in incoming:
            if not isinstance(value, dict):
                raise SemanticStoreError("proposal_input_mismatch")
            oid = text(value.get("observation_id"))
            canonical = self._observation(conn, oid)
            if canonical is None:
                raise SemanticStoreError("semantic_observation_missing")
            # The resolver may omit verbatim spans for token bounds, but any
            # supplied semantic fields still bind to their canonical versions.
            for field in ("title", "scene_kind", "units"):
                if field in value and value[field] != canonical[field]:
                    # Engine extraction units omit storage-only observation_id.
                    expected = [{k: v for k, v in u.items() if k != "observation_id"} for u in canonical["units"]] if field == "units" else canonical[field]
                    if value[field] != expected:
                        raise SemanticStoreError("proposal_observation_mismatch")
            allowed_obs.add(oid)
            incoming_claims.update(c["claim_id"] for c in canonical["claims"])
            allowed_claims.update(c["claim_id"] for c in canonical["claims"])
        for iid, candidate in candidates.items():
            if iid not in read_set["items"] or not isinstance(candidate, dict):
                raise SemanticStoreError("proposal_item_read_mismatch")
            canonical = self._item(conn, iid)
            if canonical is None or any(field in candidate and candidate[field] != canonical[field] for field in ("item_id", "kind", "title", "content_revision", "identity_scope")):
                raise SemanticStoreError("proposal_item_read_mismatch")
            allowed_obs.update(r[0] for r in conn.execute("SELECT observation_id FROM item_observations WHERE item_id=?", (iid,)))
            allowed_claims.update(canonical["claim_ids"])
        for action in actions:
            for oid in action.get("observation_ids", []):
                if oid not in allowed_obs:
                    raise SemanticStoreError("proposal_observation_mismatch")
            for output in action.get("outputs", []):
                if not set(output["observation_ids"]) <= allowed_obs:
                    raise SemanticStoreError("proposal_observation_mismatch")
            for relation in action.get("claim_relations", []):
                if relation["from_claim_id"] not in incoming_claims or relation["to_claim_id"] not in allowed_claims:
                    raise SemanticStoreError("proposal_claim_input_mismatch")
        if decision is not None:
            found = False
            for job in conn.execute("SELECT job_id,inputs_json FROM jobs WHERE kind='decision'"):
                decision_inputs = json.loads(job["inputs_json"])
                if decision_inputs.get("proposal_job_id") != job_id or decision_inputs.get("proposal") != accepted:
                    continue
                if self._accepted(conn, job["job_id"]) == decision and decision.get("choice") == "apply":
                    found = True; break
            if not found:
                raise SemanticStoreError("decision_artifact_required")

    @staticmethod
    def _resolution_scope(job):
        """Only exact resolver input identity can replace a resolver proposal."""
        inputs = json.loads(job["inputs_json"])
        incoming = inputs.get("state", {}).get("observations")
        if job["kind"] != "propose" or not inputs.get("batch_id") or not incoming:
            return None
        return {"batch_id": text(inputs["batch_id"]),
                "observation_ids": sorted(ids([value["observation_id"] for value in incoming]))}

    def _proposal_supersession_proof(self, conn, old_id, new_id, *, frozen=None):
        """Reconstruct proof from immutable jobs, receipts and Item revisions.

        A later read set alone does not discharge any action. Every exact input
        Observation must have a real resolved membership and source extraction.
        Frozen memberships keep this evidence valid after later identity edits.
        """
        invalid = "semantic_proposal_supersession_unproved"
        old, new = [conn.execute("SELECT * FROM jobs WHERE job_id=?", (identity,)).fetchone()
                    for identity in (old_id, new_id)]
        if old is None or new is None or old_id == new_id:
            raise SemanticStoreError(invalid)
        scope = self._resolution_scope(old)
        if scope is None or scope != self._resolution_scope(new):
            raise SemanticStoreError(invalid)
        artifacts = [self._accepted(conn, identity) for identity in (old_id, new_id)]
        old_artifact, new_artifact = artifacts
        reads = [artifact.get("read_set") for artifact in artifacts]
        for job, artifact, read in zip((old, new), artifacts, reads):
            if (not isinstance(read, dict) or set(read) != {"store_revision", "items", "views"}
                    or type(read["store_revision"]) is not int or read["store_revision"] < 0
                    or not isinstance(read["items"], dict) or not isinstance(read["views"], dict)
                    or read != json.loads(job["inputs_json"])["state"].get("read_set")):
                raise SemanticStoreError(invalid)
            actions = artifact.get("actions", (artifact.get("proposal") or {}).get("actions"))
            if not isinstance(actions, list) or not actions:
                raise SemanticStoreError(invalid)
            for action in actions:
                validate_action(action)
        if (reads[0]["store_revision"] >= reads[1]["store_revision"]
                or conn.execute("SELECT 1 FROM decision_log WHERE job_id=?", (old_id,)).fetchone()
                or conn.execute("SELECT 1 FROM action_receipts WHERE job_id=?", (old_id,)).fetchone()
                or conn.execute("SELECT 1 FROM canonical_transactions WHERE job_id=?", (old_id,)).fetchone()):
            raise SemanticStoreError(invalid)
        decision = conn.execute("SELECT * FROM decision_log WHERE job_id=?", (new_id,)).fetchone()
        if decision is None or json.loads(decision["read_set_json"]) != reads[1]:
            raise SemanticStoreError(invalid)
        receipt = json.loads(decision["receipt_json"])
        tx = decision["transaction_id"]
        new_actions = new_artifact.get("actions", (new_artifact.get("proposal") or {}).get("actions"))
        if (receipt.get("job_id") != new_id or receipt.get("receipt_id") != tx
                or receipt.get("transaction_id") != tx or receipt.get("generation_id") != self.generation_id
                or receipt.get("store_revision") != reads[1]["store_revision"] + 1
                or receipt.get("input_fingerprint") != digest([new_actions, reads[1], json.loads(decision["decision_json"]) or None])):
            raise SemanticStoreError(invalid)
        terminal = [{"job_id": old_id, "artifact_hash": digest(old_artifact)}]
        for job in conn.execute("SELECT * FROM jobs WHERE kind='decision' ORDER BY job_id"):
            inputs = json.loads(job["inputs_json"])
            if inputs.get("proposal_job_id") != old_id:
                continue
            if job["state"] not in {"accepted", "done"} or inputs.get("proposal") != old_artifact:
                raise SemanticStoreError("semantic_proposal_supersession_pending")
            terminal.append({"job_id": job["job_id"], "artifact_hash": digest(self._accepted(conn, job["job_id"]))})
        memberships, lineage, transactions = [], [], {tx}
        prior_members = {} if frozen is None else {value["observation_id"]: value for value in frozen["observation_memberships"]}
        if frozen is not None and set(prior_members) != set(scope["observation_ids"]):
            raise SemanticStoreError(invalid)
        for oid in scope["observation_ids"]:
            observation = conn.execute("SELECT * FROM observations WHERE observation_id=?", (oid,)).fetchone()
            current = conn.execute("SELECT i.item_id,i.content_revision FROM items i JOIN item_observations m USING(item_id) WHERE m.observation_id=? AND i.lifecycle='active' ORDER BY i.item_id LIMIT 1", (oid,)).fetchone()
            if observation is None or observation["resolution_status"] != "resolved" or current is None:
                raise SemanticStoreError("semantic_proposal_supersession_pending")
            extraction = conn.execute("SELECT * FROM extraction_receipts WHERE job_id=?", (observation["accepted_job_id"],)).fetchone()
            sources = sorted(r[0] for r in conn.execute("SELECT DISTINCT s.source_version_id FROM observation_evidence e JOIN source_spans s USING(span_id) WHERE e.observation_id=?", (oid,)))
            if extraction is None or extraction["batch_id"] != scope["batch_id"] or not sources:
                raise SemanticStoreError(invalid)
            lineage.append({"observation_id": oid, "extraction_job_id": observation["accepted_job_id"],
                            "extraction_receipt_id": extraction["receipt_id"], "source_version_ids": sources})
            binding = prior_members.get(oid, {"observation_id": oid, "item_id": current[0], "revision": current[1]})
            historical = conn.execute("SELECT * FROM item_revisions WHERE item_id=? AND revision=?", (binding["item_id"], binding["revision"])).fetchone()
            if historical is None or oid not in json.loads(historical["snapshot_json"])["observation_ids"]:
                raise SemanticStoreError(invalid)
            memberships.append({**binding, "transaction_id": historical["transaction_id"]})
            transactions.update((extraction["receipt_id"], historical["transaction_id"]))
        outboxes = set()
        for identity in sorted(transactions):
            outboxes.update(self._verify_transaction(conn, identity))
        return {"schema": "we-groupchat-obsidian.proposal-supersession.v1", "generation_id": self.generation_id,
                "reason": "semantic_stale_read_set", "job_id": old_id, "replacement_job_id": new_id,
                "original_artifact_hash": digest(old_artifact), "replacement_artifact_hash": digest(new_artifact),
                "original_read_set": reads[0], "replacement_read_set": reads[1], "scope": scope,
                "replacement_receipt_id": tx, "terminal_jobs": sorted(terminal, key=lambda value: value["job_id"]),
                "source_lineage": lineage, "observation_memberships": memberships,
                "canonical_receipt_ids": sorted(transactions), "outbox_ids": sorted(outboxes)}

    def _record_proposal_supersessions(self, conn, replacement_id, now):
        replacement = conn.execute("SELECT * FROM jobs WHERE job_id=?", (replacement_id,)).fetchone()
        scope = self._resolution_scope(replacement)
        if scope is None:
            return
        for old in conn.execute("SELECT * FROM jobs j WHERE kind='propose' AND state='accepted' AND receipt_id IS NULL AND NOT EXISTS(SELECT 1 FROM canonical_transactions t WHERE t.job_id=j.job_id) AND NOT EXISTS(SELECT 1 FROM action_receipts a WHERE a.job_id=j.job_id) ORDER BY job_id").fetchall():
            if old["job_id"] == replacement_id or self._resolution_scope(old) != scope:
                continue
            old_read = self._accepted(conn, old["job_id"]).get("read_set", {})
            new_read = self._accepted(conn, replacement_id).get("read_set", {})
            if old_read.get("store_revision", -1) >= new_read.get("store_revision", -1):
                continue
            try:
                proof = self._proposal_supersession_proof(conn, old["job_id"], replacement_id)
            except SemanticStoreError as exc:
                if exc.code == "semantic_proposal_supersession_pending":
                    continue
                raise
            if any(tuple(conn.execute("SELECT state,receipt_id FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()) != ("accepted", None)
                   for job in proof["terminal_jobs"]):
                continue
            receipt_id = "supersession_" + digest(proof)
            conn.execute("INSERT INTO proposal_supersessions VALUES(?,?,?,?,?,?)",
                         (old["job_id"], replacement_id, receipt_id, proof["replacement_receipt_id"], dumps(proof), now))
            for job in proof["terminal_jobs"]:
                conn.execute("UPDATE jobs SET state='done',receipt_id=?,updated_at=? WHERE job_id=?",
                             (receipt_id, now, job["job_id"]))

    def _verify_proposal_supersession(self, conn, job_id):
        row = conn.execute("SELECT * FROM proposal_supersessions WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        proof = json.loads(row["proof_json"])
        expected = self._proposal_supersession_proof(conn, job_id, row["replacement_job_id"], frozen=proof)
        if (proof != expected or row["receipt_id"] != "supersession_" + digest(proof)
                or row["transaction_id"] != proof["replacement_receipt_id"]):
            raise SemanticStoreError("semantic_proposal_supersession_unproved")
        for job in proof["terminal_jobs"]:
            terminal = conn.execute("SELECT state,receipt_id FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            if terminal is None or tuple(terminal) != ("done", row["receipt_id"]):
                raise SemanticStoreError("semantic_proposal_supersession_unproved")
        return json.loads(conn.execute("SELECT receipt_json FROM decision_log WHERE job_id=?", (row["replacement_job_id"],)).fetchone()[0])

    def proposal_supersession(self, job_id):
        """Return a verified committed replacement receipt, never a pending plan."""
        with closing(self.connect()) as conn:
            return self._verify_proposal_supersession(conn, text(job_id))

    def apply_proposal(self, job_id, actions, *, read_set, decision=None):
        if not isinstance(actions, (list, tuple)) or not actions:
            raise SemanticStoreError("semantic_actions_required")
        actions, read_set = json.loads(dumps(actions)), json.loads(dumps(read_set))
        for action in actions:
            validate_action(action)
        ids([a["action_key"] for a in actions])
        signature = digest([actions, read_set, decision])
        now, tx = timestamp(self.now_func()), str(uuid.uuid4())
        with self.transaction() as conn:
            accepted = self._accepted(conn, job_id)
            accepted_actions = accepted.get("actions", (accepted.get("proposal") or {}).get("actions"))
            if accepted_actions != actions:
                raise SemanticStoreError("decision_artifact_mismatch")
            previous = conn.execute("SELECT * FROM decision_log WHERE job_id=?", (job_id,)).fetchone()
            if previous:
                receipt = json.loads(previous["receipt_json"])
                if receipt["input_fingerprint"] != signature:
                    raise SemanticStoreError("semantic_action_replay_conflict")
                return receipt
            revision = self._check_read_set(conn, read_set)
            self._proposal_inputs(conn, job_id, accepted, actions, read_set, decision)
            item_reads, view_reads = read_set["items"], read_set["views"]
            proposed_ids = [a["item_id"] for a in actions if a["type"] == "CREATE_ITEM"]
            proposed_ids += [o["item_id"] for a in actions if a["type"] == "SPLIT_ITEM" for o in a["outputs"]]
            ids(proposed_ids, empty=True)
            created, effects = set(), {}

            def affect(iid, effect="none"):
                if iid not in effects or effect != "none":
                    effects[iid] = effect

            def require(iid):
                row = conn.execute("SELECT * FROM items WHERE item_id=?", (iid,)).fetchone()
                if row is None:
                    raise SemanticStoreError("semantic_item_missing")
                if iid not in created and iid not in item_reads:
                    raise SemanticStoreError("semantic_item_read_required")
                if row["lifecycle"] != "active":
                    raise SemanticStoreError("semantic_item_retired")
                return row

            def create(value, action_key, roles=None):
                iid = value["item_id"]
                if conn.execute("SELECT 1 FROM items WHERE item_id=?", (iid,)).fetchone():
                    raise SemanticStoreError("semantic_item_identity_conflict")
                conn.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?)", (iid, value["kind"], dumps(value["identity_scope"]), value["title"], 0, "active", now, now))
                for oid in value["observation_ids"]:
                    self._attach(conn, iid, [oid], action_key, role=(roles or {}).get(oid, "supports"))
                created.add(iid); affect(iid, "detail_added")

            for action in actions:
                kind, key = action["type"], action["action_key"]
                if conn.execute("SELECT 1 FROM action_receipts WHERE action_key=?", (key,)).fetchone():
                    raise SemanticStoreError("semantic_action_key_conflict")
                if kind == "CREATE_ITEM":
                    create(action, key)
                elif kind == "MERGE_INTO_ITEM":
                    iid, effect = action["item_id"], action["semantic_effect"]
                    require(iid)
                    changed = self._attach(conn, iid, action["observation_ids"], key, role="evidence_only" if effect == "none" else "supports")
                    if action.get("title") and effect != "none":
                        conn.execute("UPDATE items SET title=? WHERE item_id=?", (text(action["title"]), iid))
                    if changed or effect != "none":
                        affect(iid, effect)
                elif kind == "RELATE_ITEMS":
                    left, right = require(action["from_item_id"]), require(action["to_item_id"])
                    if left["item_id"] == right["item_id"]:
                        raise SemanticStoreError("semantic_self_relation")
                    for identity in action["evidence_ids"]:
                        if not any(conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (identity,)).fetchone()
                                   for table, column in (("observations", "observation_id"), ("claims", "claim_id"), ("source_spans", "span_id"), ("source_records", "source_version_id"))):
                            raise SemanticStoreError("semantic_relation_evidence_missing")
                    conn.execute("INSERT INTO item_relations VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(from_item_id,to_item_id,relation) DO UPDATE SET direction=excluded.direction,reason=excluded.reason,evidence_json=excluded.evidence_json,receipt_id=excluded.receipt_id,read_revisions_json=excluded.read_revisions_json", (str(uuid.uuid4()), action["from_item_id"], action["to_item_id"], action["relation"], action["direction"], action["reason"], dumps(action["evidence_ids"]), dumps(item_reads), tx))
                    affect(left["item_id"]); affect(right["item_id"])
                elif kind == "ADD_MEMBERSHIP":
                    require(action["item_id"])
                    if action["view_id"] not in view_reads:
                        raise SemanticStoreError("semantic_view_read_required")
                    old = conn.execute("SELECT * FROM view_memberships WHERE view_id=? AND item_id=?", (action["view_id"], action["item_id"])).fetchone()
                    if not (old and (old["pinned"] or old["excluded"] or old["origin"] == "user") and action["origin"] != "user"):
                        values = (action["view_id"], action["item_id"], action.get("placement", ""), action["origin"], action.get("reason", ""), int(action.get("pinned", False)), int(action.get("excluded", False)))
                        if old is None or tuple(old) != values:
                            conn.execute("INSERT INTO view_memberships VALUES(?,?,?,?,?,?,?) ON CONFLICT(view_id,item_id) DO UPDATE SET placement=excluded.placement,origin=excluded.origin,reason=excluded.reason,pinned=excluded.pinned,excluded=excluded.excluded", values)
                            if action["origin"] == "user":
                                conn.execute("UPDATE views SET override_revision=override_revision+1 WHERE view_id=?", (action["view_id"],))
                            affect(action["item_id"])
                elif kind == "MERGE_ITEMS":
                    for iid in action["item_ids"]:
                        require(iid)
                    target = action["target_item_id"]
                    before = self._identity_snapshot(conn, action["item_ids"])
                    conn.execute("INSERT INTO identity_operations VALUES(?,?,?,?,?,?,?)", (key, kind, dumps(action["item_ids"]), dumps([target]), action["reason"], dumps({"before": before}), tx))
                    for iid in action["item_ids"]:
                        if iid == target:
                            continue
                        for member in conn.execute("SELECT observation_id,role FROM item_observations WHERE item_id=?", (iid,)).fetchall():
                            self._attach(conn, target, [member[0]], key, role=member[1])
                        self._copy_identity_edges(conn, iid, target, key, tx, view_reads)
                        self._alias(conn, iid, target, key)
                        conn.execute("UPDATE items SET lifecycle='merged' WHERE item_id=?", (iid,))
                        affect(iid)
                    affect(target, "evidence_changed")
                    after = self._identity_snapshot(conn, action["item_ids"])
                    conn.execute("UPDATE identity_operations SET before_json=? WHERE operation_id=?", (dumps({"before": before, "after": after, "after_fingerprint": self._identity_fingerprint(after)}), key))
                elif kind == "SPLIT_ITEM":
                    old_id = action["item_id"]
                    require(old_id)
                    roles = dict(conn.execute("SELECT observation_id,role FROM item_observations WHERE item_id=?", (old_id,)).fetchall())
                    assigned = set().union(*(set(output["observation_ids"]) for output in action["outputs"]))
                    outputs = [output["item_id"] for output in action["outputs"]]
                    if set(roles) != assigned or old_id in outputs:
                        raise SemanticStoreError("semantic_split_conservation_failed")
                    before = self._identity_snapshot(conn, [old_id])
                    conn.execute("INSERT INTO identity_operations VALUES(?,?,?,?,?,?,?)", (key, kind, dumps([old_id]), dumps(outputs), action["reason"], dumps({"before": before}), tx))
                    for output in action["outputs"]:
                        create(output, key, roles)
                        self._copy_identity_edges(conn, old_id, output["item_id"], key, tx, view_reads)
                        self._alias(conn, old_id, output["item_id"], key)
                    conn.execute("UPDATE items SET lifecycle='split' WHERE item_id=?", (old_id,))
                    affect(old_id)
                    after = self._identity_snapshot(conn, [old_id, *outputs])
                    conn.execute("UPDATE identity_operations SET before_json=? WHERE operation_id=?", (dumps({"before": before, "after": after, "after_fingerprint": self._identity_fingerprint(after)}), key))
                for relation in action.get("claim_relations", []):
                    for iid, effect in self._claim_relation(conn, relation, revision + 1).items():
                        affect(iid, effect)
            affected, outboxes = self._publish_items(conn, effects, tx, now)
            conn.execute("UPDATE generation_manifest SET revision=revision+1")
            receipt = {"receipt_id": tx, "transaction_id": tx, "outbox_id": tx, "outbox_ids": outboxes, "job_id": job_id,
                       "affected_item_ids": affected, "store_revision": revision + 1,
                       "input_fingerprint": signature, "generation_id": self.generation_id}
            conn.execute("INSERT INTO canonical_transactions VALUES(?,?,?,?,?)", (tx, job_id, dumps(actions), dumps(outboxes), now))
            conn.execute("INSERT INTO decision_log VALUES(?,?,?,?,?)", (job_id, tx, dumps(decision or {}), dumps(read_set), dumps(receipt)))
            for action in actions:
                conn.execute("INSERT INTO action_receipts VALUES(?,?,?,?,?,?)", (action["action_key"], job_id, tx, digest(action), dumps(action), dumps(receipt)))
            self._record_proposal_supersessions(conn, job_id, now)
            return receipt

    def restore_identity_operation(self, operation_id, expected_read_set):
        tx, now = str(uuid.uuid4()), timestamp(self.now_func())
        with self.transaction() as conn:
            operation = conn.execute("SELECT * FROM identity_operations WHERE operation_id=?", (text(operation_id),)).fetchone()
            if operation is None:
                raise SemanticStoreError("semantic_identity_operation_missing")
            prior = conn.execute("SELECT * FROM identity_restorations WHERE operation_id=?", (operation_id,)).fetchone()
            if prior:
                if json.loads(prior["read_set_json"]) != expected_read_set:
                    raise SemanticStoreError("semantic_identity_restore_replay_conflict")
                return json.loads(prior["receipt_json"])
            revision = self._check_read_set(conn, expected_read_set)
            evidence = json.loads(operation["before_json"])
            before, after = evidence["before"], evidence["after"]
            affected = {item["item_id"] for item in after["items"]}
            if not affected <= set(expected_read_set["items"]):
                raise SemanticStoreError("semantic_item_read_required")
            current = self._identity_snapshot(conn, affected)
            if self._identity_fingerprint(current) != evidence["after_fingerprint"]:
                raise SemanticStoreError("semantic_identity_restore_changed")
            views = {row["view_id"] for row in current["view_memberships"] + before["view_memberships"]}
            if not views <= set(expected_read_set["views"]):
                raise SemanticStoreError("semantic_view_read_required")
            original = {row["item_id"]: row for row in before["items"]}
            # Remove only derived identity associations covered by the exact
            # operation snapshot, retaining raw sources, claims and all history.
            for table in ("item_observations", "item_claims", "view_memberships", "resource_refs"):
                for iid in affected:
                    conn.execute(f"DELETE FROM {table} WHERE item_id=?", (iid,))
                for row in before[table]:
                    conn.execute(f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
            for iid in affected:
                conn.execute("DELETE FROM item_aliases WHERE old_item_id=?", (iid,))
                conn.execute("DELETE FROM item_relations WHERE from_item_id=? OR to_item_id=?", (iid, iid))
            for table in ("item_aliases", "item_relations"):
                for row in before[table]:
                    conn.execute(f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
            for iid in affected:
                if iid in original:
                    row = original[iid]
                    conn.execute("UPDATE items SET kind=?,identity_scope_json=?,title=?,lifecycle=? WHERE item_id=?", (row["kind"], row["identity_scope_json"], row["title"], row["lifecycle"], iid))
                else:
                    conn.execute("UPDATE items SET lifecycle='restored' WHERE item_id=?", (iid,))
                    for restored in original:
                        self._alias(conn, iid, restored, operation_id)
            for view in views:
                conn.execute("UPDATE views SET override_revision=override_revision+1 WHERE view_id=?", (view,))
            published, outboxes = self._publish_items(conn, {iid: "state_changed" for iid in affected}, tx, now)
            conn.execute("UPDATE generation_manifest SET revision=revision+1")
            action = {"type": "RESTORE_IDENTITY", "operation_id": operation_id, "before_fingerprint": digest(before), "restored_item_ids": sorted(original)}
            receipt = {"receipt_id": tx, "transaction_id": tx, "operation_id": operation_id,
                       "generation_id": self.generation_id, "outbox_id": tx, "outbox_ids": outboxes,
                       "affected_item_ids": published, "store_revision": revision + 1,
                       "compensates_receipt_id": operation["receipt_id"], "expected_read_set": expected_read_set}
            conn.execute("INSERT INTO canonical_transactions VALUES(?,?,?,?,?)", (tx, None, dumps([action]), dumps(outboxes), now))
            conn.execute("INSERT INTO identity_restorations VALUES(?,?,?,?)", (operation_id, tx, dumps(expected_read_set), dumps(receipt)))
            return receipt

    def pending_outbox(self, kind, limit=100):
        if kind not in {"projection", "index"}:
            raise SemanticStoreError("semantic_outbox_kind_invalid")
        with closing(self.connect()) as conn:
            return [_decode(r) for r in conn.execute(f"SELECT * FROM {kind}_outbox WHERE status='pending' ORDER BY rowid LIMIT ?", (integer(limit, 1),))]

    def ack_outbox(self, kind, outbox_id, owner_id, revision, receipt_id):
        if kind not in {"projection", "index"}:
            raise SemanticStoreError("semantic_outbox_kind_invalid")
        integer(revision, 1)
        with self.transaction() as conn:
            row = conn.execute(f"SELECT * FROM {kind}_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
            if row is None or row["owner_id"] != owner_id or row["revision"] != revision:
                raise SemanticStoreError("semantic_outbox_receipt_mismatch")
            if kind == "projection":
                current = conn.execute("SELECT content_revision FROM items WHERE item_id=?", (owner_id,)).fetchone()
                if row["status"] == "superseded" or current is None or current[0] != revision:
                    raise SemanticStoreError("semantic_outbox_receipt_mismatch")
            if row["status"] == "done" and row["receipt_id"] != receipt_id:
                raise SemanticStoreError("semantic_outbox_receipt_conflict")
            conn.execute(f"UPDATE {kind}_outbox SET status='done',receipt_id=? WHERE outbox_id=?", (text(receipt_id), outbox_id))

    def supersede_projection_outbox(self, owner_id, published_revision, receipt_id):
        integer(published_revision, 1); text(receipt_id)
        with self.transaction() as conn:
            current = conn.execute("SELECT content_revision FROM items WHERE item_id=?", (text(owner_id),)).fetchone()
            published = conn.execute("SELECT * FROM projection_outbox WHERE owner_id=? AND revision=?", (owner_id, published_revision)).fetchone()
            if (current is None or current[0] != published_revision or published is None or
                    published["status"] != "done" or published["receipt_id"] != receipt_id):
                raise SemanticStoreError("semantic_projection_supersession_unproved")
            rows = conn.execute("SELECT outbox_id FROM projection_outbox WHERE owner_id=? AND revision<? AND status='pending'", (owner_id, published_revision)).fetchall()
            conn.execute("UPDATE projection_outbox SET status='superseded',receipt_id=? WHERE owner_id=? AND revision<? AND status='pending'", (receipt_id, owner_id, published_revision))
            return [row[0] for row in rows]

    @staticmethod
    def _verify_outbox(conn, outbox_id):
        row = conn.execute("SELECT *, 'projection' AS kind FROM projection_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        if row is None:
            row = conn.execute("SELECT *, 'index' AS kind FROM index_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        if row is None or row["status"] not in {"pending", "done", "superseded"}:
            raise SemanticStoreError("semantic_outbox_evidence_missing")
        if row["status"] == "done" and not row["receipt_id"]:
            raise SemanticStoreError("semantic_outbox_evidence_missing")
        if row["status"] == "superseded":
            if row["kind"] != "projection" or not row["receipt_id"] or not conn.execute("SELECT 1 FROM projection_outbox WHERE owner_id=? AND revision>? AND status='done' AND receipt_id=?", (row["owner_id"], row["revision"], row["receipt_id"])).fetchone():
                raise SemanticStoreError("semantic_outbox_supersession_unproved")
        return row

    def _verify_transaction(self, conn, transaction_id):
        row = conn.execute("SELECT * FROM canonical_transactions WHERE transaction_id=?", (transaction_id,)).fetchone()
        if row is None:
            raise SemanticStoreError("semantic_canonical_receipt_missing")
        outboxes = ids(json.loads(row["outbox_ids_json"]), empty=True)
        actual = {r[0] for r in conn.execute("SELECT outbox_id FROM index_outbox WHERE transaction_id=? UNION ALL SELECT outbox_id FROM projection_outbox WHERE transaction_id=?", (transaction_id, transaction_id))}
        if set(outboxes) != actual:
            raise SemanticStoreError("semantic_outbox_evidence_missing")
        for outbox in outboxes:
            self._verify_outbox(conn, outbox)
        for item in conn.execute("SELECT item_id,revision FROM item_revisions WHERE transaction_id=?", (transaction_id,)):
            for table in ("index_outbox", "projection_outbox"):
                if not conn.execute(f"SELECT 1 FROM {table} WHERE owner_id=? AND revision=? AND transaction_id=?", (item[0], item[1], transaction_id)).fetchone():
                    raise SemanticStoreError("semantic_outbox_evidence_missing")
            if not conn.execute("SELECT 1 FROM change_events WHERE item_id=? AND revision=? AND transaction_id=?", (item[0], item[1], transaction_id)).fetchone():
                raise SemanticStoreError("semantic_change_evidence_missing")
        if row["job_id"] is not None:
            artifact = self._accepted(conn, row["job_id"])
            actions = json.loads(row["actions_json"])
            if actions and actions[0].get("type") == "COMMIT_EXTRACTION":
                extraction = conn.execute("SELECT * FROM extraction_receipts WHERE job_id=? AND receipt_id=?", (row["job_id"], transaction_id)).fetchone()
                if extraction is None:
                    raise SemanticStoreError("semantic_extraction_receipt_missing")
                receipt = json.loads(extraction["receipt_json"])
                members = {r["source_version_id"]: dict(r) for r in conn.execute("SELECT r.*,m.core FROM capture_batch_members m JOIN source_records r USING(source_version_id) WHERE batch_id=?", (extraction["batch_id"],))}
                self._extraction_inputs(conn, row["job_id"], extraction["batch_id"], members)
                expected_signature = digest([extraction["batch_id"], artifact.get("observations"), artifact.get("dispositions", {})])
                expected_action = {"type": "COMMIT_EXTRACTION", "batch_id": extraction["batch_id"],
                                   "observation_ids": receipt.get("observation_ids"), "dispositions": receipt.get("dispositions")}
                if (actions != [expected_action] or extraction["input_fingerprint"] != expected_signature or
                        receipt.get("input_fingerprint") != expected_signature or
                        receipt.get("observation_ids") != [obs["observation_id"] for obs in artifact["observations"]]):
                    raise SemanticStoreError("semantic_extraction_evidence_mismatch")
                if receipt["outbox_ids"] != outboxes:
                    raise SemanticStoreError("semantic_outbox_evidence_missing")
                for oid in receipt["observation_ids"]:
                    if not conn.execute("SELECT 1 FROM index_outbox WHERE owner_type='observation' AND owner_id=? AND revision=1", (oid,)).fetchone():
                        raise SemanticStoreError("semantic_outbox_evidence_missing")
            else:
                accepted_actions = artifact.get("actions", (artifact.get("proposal") or {}).get("actions"))
                persisted = conn.execute("SELECT * FROM action_receipts WHERE transaction_id=? ORDER BY rowid", (transaction_id,)).fetchall()
                if accepted_actions != actions or [json.loads(r["action_json"]) for r in persisted] != actions:
                    raise SemanticStoreError("semantic_action_evidence_missing")
                if any(r["job_id"] != row["job_id"] or r["input_fingerprint"] != digest(json.loads(r["action_json"])) for r in persisted):
                    raise SemanticStoreError("semantic_action_evidence_missing")
        return outboxes

    def _batch_proof(self, conn, batch_id, frozen=None):
        batch = conn.execute("SELECT * FROM capture_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if batch is None:
            raise SemanticStoreError("capture_batch_missing")
        own = conn.execute("SELECT * FROM extraction_receipts WHERE batch_id=? ORDER BY job_id", (batch_id,)).fetchall()
        if not own:
            raise SemanticStoreError("semantic_batch_incomplete")
        extraction_ids = {row["receipt_id"] for row in own if frozen is None or row["receipt_id"] in frozen["extraction_receipt_ids"]}
        if not extraction_ids:
            raise SemanticStoreError("semantic_extraction_receipt_missing")
        core, observed = [], set()
        for member in conn.execute("SELECT source_version_id FROM capture_batch_members WHERE batch_id=? AND core=1 ORDER BY ordinal", (batch_id,)):
            vid = member[0]
            owner = conn.execute("SELECT job_id FROM source_core_jobs WHERE source_version_id=?", (vid,)).fetchone()
            disposition = conn.execute("SELECT * FROM source_dispositions WHERE source_version_id=?", (vid,)).fetchone()
            if owner is None or disposition is None or disposition["job_id"] != owner[0] or disposition["status"] not in DISPOSITIONS or not disposition["reason"]:
                raise SemanticStoreError("semantic_batch_incomplete")
            extraction = conn.execute("SELECT * FROM extraction_receipts WHERE job_id=?", (owner[0],)).fetchone()
            if extraction is None:
                raise SemanticStoreError("semantic_extraction_receipt_missing")
            extraction_receipt = json.loads(extraction["receipt_json"])
            status = {"status": disposition["status"], "reason": disposition["reason"]}
            if extraction_receipt.get("dispositions", {}).get(vid) != status:
                raise SemanticStoreError("semantic_disposition_evidence_missing")
            extraction_ids.add(extraction["receipt_id"])
            source_observations = {r[0] for r in conn.execute("SELECT DISTINCT o.observation_id FROM observations o JOIN observation_evidence e USING(observation_id) JOIN source_spans s USING(span_id) WHERE s.source_version_id=? AND o.accepted_job_id=?", (vid, owner[0]))}
            if disposition["status"] == "observed" and not source_observations:
                raise SemanticStoreError("semantic_batch_incomplete")
            if disposition["status"] != "observed" and source_observations:
                raise SemanticStoreError("semantic_disposition_evidence_missing")
            observed.update(source_observations)
            core.append({"source_version_id": vid, "job_id": owner[0], **status, "extraction_receipt_id": extraction["receipt_id"]})
        memberships, transactions = [], set(extraction_ids)
        frozen_members = {} if frozen is None else {entry["observation_id"]: entry for entry in frozen["observation_memberships"]}
        if frozen is not None and set(frozen_members) != observed:
            raise SemanticStoreError("semantic_processing_proof_mismatch")
        for oid in sorted(observed):
            if frozen is None:
                item = conn.execute("SELECT i.item_id,i.content_revision FROM items i JOIN item_observations m USING(item_id) WHERE m.observation_id=? AND i.lifecycle='active' ORDER BY i.item_id LIMIT 1", (oid,)).fetchone()
                if item is None:
                    raise SemanticStoreError("semantic_batch_incomplete")
                binding = {"observation_id": oid, "item_id": item[0], "revision": item[1]}
            else:
                binding = {k: frozen_members[oid][k] for k in ("observation_id", "item_id", "revision")}
            historical = conn.execute("SELECT * FROM item_revisions WHERE item_id=? AND revision=?", (binding["item_id"], integer(binding["revision"], 1))).fetchone()
            if historical is None or oid not in json.loads(historical["snapshot_json"])["observation_ids"]:
                raise SemanticStoreError("semantic_membership_evidence_missing")
            binding["transaction_id"] = historical["transaction_id"]
            transactions.add(historical["transaction_id"])
            memberships.append(binding)
        outboxes = set()
        for tx in sorted(transactions):
            outboxes.update(self._verify_transaction(conn, tx))
        proof = {"core_sources": core, "observation_memberships": memberships,
                 "extraction_receipt_ids": sorted(extraction_ids), "canonical_receipt_ids": sorted(transactions),
                 "outbox_ids": sorted(outboxes)}
        if frozen is not None and proof != frozen:
            raise SemanticStoreError("semantic_processing_proof_mismatch")
        return proof

    def _verify_processing(self, conn, receipt):
        if not isinstance(receipt, dict):
            raise SemanticStoreError("semantic_processing_proof_mismatch")
        stored = conn.execute("SELECT * FROM processing_receipts WHERE receipt_id=?", (receipt.get("receipt_id"),)).fetchone()
        if stored is None or json.loads(stored["receipt_json"]) != receipt or receipt.get("generation_id") != self.generation_id:
            raise SemanticStoreError("semantic_processing_proof_mismatch")
        batch = conn.execute("SELECT * FROM capture_batches WHERE batch_id=?", (stored["batch_id"],)).fetchone()
        if (receipt.get("batch_id") != stored["batch_id"] or receipt.get("capture_receipt_ids") != [batch["receipt_id"]] or
                receipt.get("input_cursors") != json.loads(batch["input_cursors_json"]) or
                receipt.get("output_cursors") != json.loads(batch["output_cursors_json"])):
            raise SemanticStoreError("semantic_processing_proof_mismatch")
        proof = self._batch_proof(conn, stored["batch_id"], receipt.get("proof"))
        if receipt.get("proof_fingerprint") != digest(proof) or any(receipt.get(key) != proof[key] for key in ("extraction_receipt_ids", "canonical_receipt_ids", "outbox_ids")):
            raise SemanticStoreError("semantic_processing_proof_mismatch")
        tx = conn.execute("SELECT * FROM canonical_transactions WHERE transaction_id=?", (stored["transaction_id"],)).fetchone()
        action = {"type": "COMPLETE_BATCH", "batch_id": stored["batch_id"], "proof_fingerprint": digest(proof)}
        if tx is None or json.loads(tx["actions_json"]) != [action] or json.loads(tx["outbox_ids_json"]) != proof["outbox_ids"]:
            raise SemanticStoreError("semantic_processing_proof_mismatch")
        return receipt

    def verify_processing_receipt(self, receipt):
        """Read-only structural proof; no counter or success flag is authority."""
        with closing(self.connect()) as conn:
            conn.execute("BEGIN")
            return self._verify_processing(conn, receipt)

    def complete_batch(self, batch_id):
        with self.transaction() as conn:
            prior = conn.execute("SELECT receipt_json FROM processing_receipts WHERE batch_id=?", (batch_id,)).fetchone()
            if prior:
                return self._verify_processing(conn, json.loads(prior[0]))
            batch = conn.execute("SELECT rowid AS sequence,* FROM capture_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                raise SemanticStoreError("capture_batch_missing")
            # A later raw capture cannot conceal an earlier core backlog.
            earlier = conn.execute("SELECT b.batch_id FROM capture_batches b LEFT JOIN processing_receipts p USING(batch_id) WHERE b.source_namespace=? AND b.chat_id=? AND b.rowid<? AND p.batch_id IS NULL AND EXISTS(SELECT 1 FROM capture_batch_members m WHERE m.batch_id=b.batch_id AND m.core=1) ORDER BY b.rowid", (batch["source_namespace"], batch["chat_id"], batch["sequence"])).fetchall()
            for previous in earlier:
                try:
                    self._batch_proof(conn, previous[0])
                except SemanticStoreError as exc:
                    raise SemanticStoreError("semantic_prior_batch_incomplete") from exc
            proof = self._batch_proof(conn, batch_id)
            tx = str(uuid.uuid4())
            receipt = {"receipt_id": tx, "transaction_id": tx, "outbox_id": tx, "batch_id": batch_id,
                       "capture_receipt_ids": [batch["receipt_id"]],
                       **{key: proof[key] for key in ("extraction_receipt_ids", "canonical_receipt_ids", "outbox_ids")},
                       "proof": proof, "proof_fingerprint": digest(proof),
                       "generation_id": self.generation_id, "input_cursors": json.loads(batch["input_cursors_json"]),
                       "output_cursors": json.loads(batch["output_cursors_json"])}
            conn.execute("INSERT INTO canonical_transactions VALUES(?,?,?,?,?)", (tx, None,
                         dumps([{"type": "COMPLETE_BATCH", "batch_id": batch_id, "proof_fingerprint": digest(proof)}]), dumps(proof["outbox_ids"]), self.now_func()))
            conn.execute("INSERT INTO processing_receipts VALUES(?,?,?,?,?)", (batch_id, tx, tx, tx, dumps(receipt)))
            return receipt

    def record_migration_receipt(self, receipt, processing_receipts):
        receipt = json.loads(dumps(receipt))
        if not isinstance(receipt, dict) or not isinstance(processing_receipts, (list, tuple)):
            raise SemanticStoreError("semantic_migration_receipt_invalid")
        receipt_id = text(receipt.get("receipt_id"))
        if (receipt.get("semantic_generation_id") != self.generation_id or
                receipt.get("migration_id") != receipt_id or
                receipt.get("canonical_receipt_id") != receipt_id or receipt.get("outbox_receipt_id") != receipt_id):
            raise SemanticStoreError("semantic_migration_receipt_invalid")
        processing_ids = ids([value.get("receipt_id") for value in processing_receipts], empty=True)
        if receipt.get("processing_receipt_ids") != processing_ids:
            raise SemanticStoreError("semantic_migration_receipt_invalid")
        with self.transaction() as conn:
            capture_ids, outboxes = [], set()
            for value in processing_receipts:
                self._verify_processing(conn, value)
                capture_ids.extend(value["capture_receipt_ids"])
                outboxes.update(value["outbox_ids"])
            if receipt.get("capture_receipt_ids") != capture_ids:
                raise SemanticStoreError("semantic_migration_receipt_invalid")
            action = {"type": "MIGRATION", "receipt_id": receipt_id,
                      "processing_receipt_ids": processing_ids, "capture_receipt_ids": capture_ids}
            old = conn.execute("SELECT * FROM semantic_migration_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
            if old:
                transaction = conn.execute("SELECT * FROM canonical_transactions WHERE transaction_id=?", (receipt_id,)).fetchone()
                if (json.loads(old["receipt_json"]) != receipt or transaction is None or
                        json.loads(transaction["actions_json"]) != [action] or
                        json.loads(transaction["outbox_ids_json"]) != sorted(outboxes)):
                    raise SemanticStoreError("semantic_migration_receipt_conflict")
                return receipt
            if conn.execute("SELECT 1 FROM canonical_transactions WHERE transaction_id=?", (receipt_id,)).fetchone():
                raise SemanticStoreError("semantic_migration_receipt_conflict")
            conn.execute("INSERT INTO canonical_transactions VALUES(?,?,?,?,?)", (receipt_id, None, dumps([action]), dumps(sorted(outboxes)), self.now_func()))
            conn.execute("INSERT INTO semantic_migration_receipts VALUES(?,?)", (receipt_id, dumps(receipt)))
            return receipt
