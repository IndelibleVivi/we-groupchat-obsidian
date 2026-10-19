"""Independent acceptance against synthetic real canonical transactions."""
from dataclasses import asdict
from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from ai.semantic_provider import EmbeddingProfile
from ai.vector_index import LocalCosineIndex, VectorRecord
from core.semantic.contracts import digest, dumps
from core.semantic.attribution import EXTRACTION_PROMPT_VERSION, captured_source_metadata
from core.semantic.engine import CapturedSource, prepare_spans, validate_accepted_artifact
from core.semantic.jobs import JobStore
from core.semantic.projections import (
    BodyBlock, BodyPlan, BodyPlanWriter, CanonicalReadingAdapter, ProjectionPublisher, ReadingLedger, provider_binding,
    bind_reading_policy, CONTENT_READING_POLICY, SOURCE_READING_POLICY, EDITORIAL_READING_POLICY, EDITORIAL_V2_READING_POLICY,
)
from core.semantic.retrieval import _INDEX_SCHEMA
from core.semantic.store import SemanticStore, CAPTURE_CONTEXT_VERSION
from core.semantic.verification import RebuildVerifier
from core.semantic.legacy import (import_legacy, record_legacy_backups, record_legacy_resolution,
                                  derive_topic_item_map, publish_legacy_navigation, SOURCE_FIRST_POLICY,
                                  build_publication_plan)
from core.url_safety import redact_urls_in_text
from tests.test_semantic_legacy import LegacyFixture, preserve_history_fixture


def accept_fixture(store, artifact, kind="fixture", *, inputs=None, versions=None):
    """Accept an explicit synthetic artifact through the real fenced job API."""
    jobs = JobStore(store)
    job = jobs.ensure(kind, inputs or {}, versions or ({"prompt": EXTRACTION_PROMPT_VERSION} if kind == "extract" else {"fixture": "v1"}))
    jobs.claim(job["job_id"], "fixture-worker")
    jobs.accept(job["job_id"], "fixture-worker", artifact,
                validate=lambda value: validate_accepted_artifact(value, artifact))
    return job["job_id"]


def adopt_fixture_batch(store, batch_id):
    sources = [CapturedSource(r["source_version_id"], r["text"], r["core"], r["source_sent_at"])
               for r in store.get_batch(batch_id)["members"]]
    job = accept_fixture(store, {"observations": [], "dispositions": {}}, "extract",
                         inputs={"batch_id": batch_id, "members": [asdict(s) for s in sources]})
    store.commit_extraction(job, batch_id, [], {})


class VerificationFixture:
    def __init__(self, root, *, purpose="live", stale_proposal=False, unknown_decision=False,
                 reuse_action_key=False, same_read_set=False, reading_policy=None,
                 unit_kind="scene", unit_text=None, body_text=None, context_text=None):
        self.root = Path(root).resolve()
        self.store = SemanticStore(self.root / "generation.sqlite", "synthetic-generation", now_func=lambda: 10.0)
        if reading_policy is not None:
            with self.store.transaction() as conn:
                bind_reading_policy(conn, {"reading_policy": reading_policy})
        self.projections = self.root / "WGO"
        self.projections.mkdir()
        self.profile = EmbeddingProfile("synthetic", "unit-vectors", 2)
        self.index = LocalCosineIndex()
        self.start = {"shard": {"generation_id": "physical", "cursor_token": "[0,0]"}}
        self.end = {"shard": {"generation_id": "physical", "cursor_token": "[2,0]"}}
        previous = None
        if context_text is not None:
            previous = self.store.capture_batch("earlier-capture", [
                {"source_id": "earlier-source", "logical_shard_id": "shard", "physical_generation": "physical",
                 "source_cursor_token": "[0,0]", "source_sent_at": 0, "text": context_text,
                 "payload": {"raw_sender_id": "synthetic-speaker", "sender": "阿岚"}}],
                source_namespace="source", chat_id="chat", inventory_digest="inventory", inventory_revision=1,
                input_cursors=self.start, output_cursors=self.start, source_eof=True, purpose=purpose)
            vid = previous["member_version_ids"][0]
            source = CapturedSource(vid, context_text, True, 0,
                **captured_source_metadata("source", "chat", {"raw_sender_id": "synthetic-speaker", "sender": "阿岚"}))
            disposition = {vid: {"status": "context_only", "reason": "synthetic earlier capture classified without an Observation"}}
            job = accept_fixture(self.store, {"observations": [], "dispositions": disposition}, "extract",
                inputs={"batch_id": "earlier-capture", "members": [asdict(source)]})
            self.store.commit_extraction(job, "earlier-capture", [], disposition)
            self.store.complete_batch("earlier-capture")
        self.capture = self.store.capture_batch("batch", [
            {"source_id": "one", "logical_shard_id": "shard", "physical_generation": "physical",
             "source_cursor_token": "[1,0]", "source_sent_at": 1.0,
             "text": "周末在河边散步。", "payload": {"raw": "周末在河边散步。"},
             "resources": [{"resource_id": "photo", "occurrence_id": "one", "purpose": "attachment",
                            "status": "pending", "location": None, "metadata": {"name": "memory.jpg"}}]},
            {"source_id": "two", "logical_shard_id": "shard", "physical_generation": "physical",
             "source_cursor_token": "[2,0]", "source_sent_at": 2.0,
             "text": "天气转晴。", "payload": {"raw": "天气转晴。"}},
        ], source_namespace="source", chat_id="chat", inventory_digest="inventory",
            inventory_revision=1, input_cursors=self.start, output_cursors=self.end, source_eof=True,
            purpose=purpose)
        first, second = self.capture["member_version_ids"]
        members = self.store.get_batch("batch")["members"]
        sources = tuple(CapturedSource(r["source_version_id"], r["text"], r["core"], r["source_sent_at"])
                        for r in members)
        source_span = prepare_spans(sources, namespace=self.store.generation_id)[0]
        self.span_id = source_span.span_id
        self.observation = {"observation_id": "observation", "title": "周末散步", "scene_kind": "life",
            "spans": [asdict(source_span)],
            "units": [{"unit_id": "unit", "kind": unit_kind, "text": unit_text or "周末在河边散步。", "span_ids": [self.span_id]}],
            "topics": ["生活"], "claims": []}
        # len in code points, not visual glyph count.
        self.observation["spans"][0]["end"] = len(self.observation["spans"][0]["excerpt"])
        inputs = {"batch_id": "batch", "members": [asdict(s) for s in sources]}
        versions = {"prompt": EXTRACTION_PROMPT_VERSION}
        if previous is not None:
            inputs["preceding_context"] = self.store.preceding_context("batch")
            versions["capture_context"] = CAPTURE_CONTEXT_VERSION
            span = inputs["preceding_context"]["spans"][-1]
            self.context_span_id = span["span_id"]
            self.observation["spans"].append(span)
            self.observation["units"][0]["span_ids"].append(self.context_span_id)
            speaker = captured_source_metadata("source", "chat", {"raw_sender_id": "synthetic-speaker"})["speaker_id"]
            self.observation["claims"] = [{"claim_id": "earlier-claim", "subject_id": speaker, "case_id": None,
                "condition": None, "channel": None, "version": None, "phase": None, "observed_at": 0,
                "predicates": {"reported": "a synthetic first-person experience"}, "span_ids": [self.context_span_id],
                "material_id": None, "valid_from": None, "valid_to": None, "status": "active", "retracts": [],
                "polarity": "asserted", "modality": "reported", "normalizer_version": "v1"}]
        disposition = {second: {"status": "noise", "reason": "explicit_synthetic_fixture_decision"}}
        self.extract_job = accept_fixture(self.store, {"observations": [self.observation], "dispositions": disposition}, "extract",
            inputs=inputs, versions=versions)
        self.store.commit_extraction(self.extract_job, "batch", [self.observation], disposition)
        self.action = {"action_key": "create", "type": "CREATE_ITEM", "item_id": "item",
                       "kind": "life", "title": "周末散步", "identity_scope": {"scene": "weekend"},
                       "observation_ids": ["observation"]}
        read_set = {"store_revision": self.store.read_revision(), "items": {}, "views": {}}
        if stale_proposal:
            old_action = {**self.action, "action_key": self.action["action_key"] if reuse_action_key else "stale-create", "item_id": "stale-item"}
            self.old_artifact = {"proposal": {"actions": [old_action], "novelty": {
                "considered_item_ids": [], "reason": "synthetic original resolution"}}, "read_set": read_set}
            self.old_propose_job = accept_fixture(self.store, self.old_artifact, "propose",
                inputs={"batch_id": "batch", "state": {"read_set": read_set,
                    "observations": [self.store.get_observation("observation")], "items": {}, "views": {}, "new_item_ids": ["stale-item"]}})
            decision_inputs = {"proposal_job_id": self.old_propose_job, "proposal": self.old_artifact}
            if unknown_decision:
                jobs = JobStore(self.store)
                decision = jobs.ensure("decision", decision_inputs, {"fixture": "v1"})
                jobs.claim(decision["job_id"], "fixture-worker")
                jobs.fail(decision["job_id"], "fixture-worker", "synthetic_sent_unknown", unknown=True)
                self.old_decision_job = decision["job_id"]
            else:
                self.old_decision_job = accept_fixture(self.store, {"choice": "apply"}, "decision", inputs=decision_inputs)
            if not same_read_set:
                self.store.create_view("changed-context", "新视图", "synthetic read-set change")
            read_set = {"store_revision": self.store.read_revision(), "items": {}, "views": {}}
        self.propose_job = accept_fixture(self.store,
            {"proposal": {"actions": [self.action], "novelty": {"considered_item_ids": [], "reason": "grounded new scene"}}, "read_set": read_set}, "propose",
            inputs={"batch_id": "batch", "state": {"read_set": read_set,
                    "observations": [self.store.get_observation("observation")], "items": {}, "views": {}, "new_item_ids": ["item"]}})
        self.action_receipt = self.store.apply_proposal(self.propose_job, [self.action], read_set=read_set)
        self.store.complete_batch("batch")
        self.adapter = CanonicalReadingAdapter(self.store)
        self.ledger = ReadingLedger(self.store.connect, self.store.generation_id)
        self.publisher = ProjectionPublisher(self.ledger, self.projections,
                                             current_revision=self.adapter.current_revision)
        self.snapshot = self.adapter.snapshot("item")
        self.plan = BodyPlan("item", self.snapshot.content_revision, (
            BodyBlock("paragraph", "scene", body_text or unit_text or "周末在河边散步。", ("unit",),
                      tuple(s["span_id"] for s in self.observation["spans"]),
                      resource_ids=("photo",), primary_home="item"),))
        self.publisher.publish_item(self.snapshot, self.plan)
        if stale_proposal and not same_read_set:
            self.publisher.publish_view("changed-context", "新视图", [])
        self._index()
        self.manifest = {
            "generation_id": self.store.generation_id,
            "expected_batches": [{"batch_id": "batch", "member_version_ids": list(self.capture["member_version_ids"])}],
            "source_inventory": {"complete": True, "digest": "inventory", "revision": 1},
            "h1": [{"source_namespace": "source", "chat_id": "chat", "inventory_digest": "inventory",
                    "inventory_revision": 1, "cursors": self.end}],
            "embedding_profile_id": self.profile.profile_id, "index_generation_id": self.index.index_identity,
        }

        if reading_policy is not None:
            self.manifest["reading_policy"] = reading_policy
        if previous is not None:
            self.manifest["expected_batches"].insert(0, {"batch_id": "earlier-capture", "member_version_ids": list(previous["member_version_ids"])})

    def _index(self):
        docs = [r for r in self.store.documents() if r["owner_type"] == "item"]
        document = docs[0]
        vector_id = str(uuid.uuid4())
        vector = (1.0, 0.0)
        record = VectorRecord(vector_id, vector)
        receipt = self.index.upsert((record,), profile=self.profile,
                                   generation_id=self.store.generation_id, request_id="synthetic-index")
        embed_job = accept_fixture(self.store, {"vectors": [vector], "profile_id": self.profile.profile_id}, "document_embedding",
                                   inputs={"texts": [document["text"]], "purpose": "document"},
                                   versions={"profile_id": self.profile.profile_id, "generation_id": self.store.generation_id})
        with closing(self.store.connect()) as conn:
            conn.executescript(_INDEX_SCHEMA)
            conn.execute("INSERT INTO index_profiles VALUES(?,?,?)", (
                self.profile.profile_id, dumps(asdict(self.profile)), self.store.generation_id))
            conn.execute("INSERT INTO embedding_records VALUES(?,?,?,?,?,?,?,?)", (
                vector_id, self.profile.profile_id, self.store.generation_id, "item:item", document["revision"],
                digest(["item:item", document["revision"], redact_urls_in_text(document["text"])]),
                dumps({"vector": vector}), embed_job))
            conn.execute("INSERT INTO semantic_index_receipts(intent_id,profile_id,generation_id,index_identity,status,records_json,receipt_json) VALUES(?,?,?,?,?,?,?)", (
                "intent", self.profile.profile_id, self.store.generation_id, self.index.index_identity,
                "indexed", dumps([asdict(record)]), dumps(receipt.to_dict())))
            conn.commit()

    def verify(self):
        return RebuildVerifier(self.store.path, self.manifest, projection_root=self.projections).verify()


class SemanticVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = VerificationFixture(self.temp.name)

    def codes(self, report):
        return {issue.code for issue in report.issues}

    @unittest.skipIf(os.name == "nt", "POSIX subprocess RSS evidence")
    def test_corpus_payload_growth_does_not_become_resident_verifier_memory(self):
        script = '''
import json, resource, sqlite3, sys, tempfile
from pathlib import Path
from core.semantic.contracts import digest
from tests.test_semantic_verification import VerificationFixture
with tempfile.TemporaryDirectory() as directory:
    fixture = VerificationFixture(Path(directory).resolve())
    count = int(sys.argv[1])
    text = 'x' * 32768
    fixture.manifest['large_frozen_notes'] = [text] * count
    payload = {'raw': text}
    with fixture.store.transaction() as db:
        original = dict(db.execute('SELECT * FROM source_records LIMIT 1').fetchone())
        columns = list(original)
        for index in range(count):
            row = dict(original, source_version_id='bulk-'+str(index), source_id='bulk-'+str(index),
                text=text, payload_json=json.dumps(payload), revision=1, supersedes_source_version_id=None,
                native_revision=None, capture_seq=index+3)
            row['content_hash'] = digest({'text':text,'payload':payload,'kind':row['kind'],
                'grade':row['provenance_grade'],'sent_at':row['source_sent_at'],'resources':[]})
            if index == count-1:
                row['content_hash'] = 'corrupt-tail'
            db.execute('INSERT INTO source_records('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                       [row[key] for key in columns])
            db.execute('INSERT INTO source_heads VALUES (?,?,?,?)',
                       (row['source_namespace'],row['chat_id'],row['source_id'],row['source_version_id']))
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = fixture.verify()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    unit = 1 if sys.platform == 'darwin' else 1024
    print(json.dumps({'growth':(peak-before)*unit,'captured':report.counts['captured_versions'],
                      'issues':sorted(issue.code for issue in report.issues)}))
'''
        samples = []
        for count in (64, 640):
            result = subprocess.run([sys.executable, "-c", script, str(count)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            sample = json.loads(result.stdout)
            self.assertEqual(sample["captured"], count + 2)
            self.assertIn("source_payload_identity_mismatch", sample["issues"])
            self.assertIn("unaccounted_source_version", sample["issues"])
            samples.append(sample["growth"])
        self.assertLess(samples[1] - samples[0], 24 * 1024 * 1024, samples)

    def test_verifier_observes_uncheckpointed_wal_and_repeated_verification(self):
        verifier = RebuildVerifier(self.fixture.store.path, self.fixture.manifest,
                                   projection_root=self.fixture.projections)
        self.assertTrue(verifier.verify().complete)
        with closing(sqlite3.connect(self.fixture.store.path)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE source_records SET text='changed in WAL' WHERE source_id='one'")
            writer.commit()
            self.assertGreater(Path(str(self.fixture.store.path) + "-wal").stat().st_size, 0)
            self.assertIn("source_payload_identity_mismatch", self.codes(verifier.verify()))

    def test_nested_manifest_is_frozen_without_retaining_caller_containers(self):
        verifier = RebuildVerifier(self.fixture.store.path, self.fixture.manifest,
                                   projection_root=self.fixture.projections)
        self.fixture.manifest["expected_batches"][0]["member_version_ids"].clear()
        self.assertTrue(verifier.verify().complete)

    def test_source_write_during_verification_cannot_change_its_snapshot(self):
        verifier = RebuildVerifier(self.fixture.store.path, self.fixture.manifest,
                                   projection_root=self.fixture.projections)
        members = verifier._verify_members
        def mutate_source():
            with self.fixture.store.transaction() as db:
                db.execute("UPDATE source_records SET text='changed after snapshot' WHERE source_id='one'")
            members()
        with patch.object(verifier, "_verify_members", side_effect=mutate_source):
            self.assertTrue(verifier.verify().complete)
        self.assertIn("source_payload_identity_mismatch", self.codes(verifier.verify()))

    def test_private_verification_scratch_is_removed_after_unexpected_failure(self):
        verifier = RebuildVerifier(self.fixture.store.path, self.fixture.manifest,
                                   projection_root=self.fixture.projections)
        original = sqlite3.connect
        scratch, connections = [], []
        def connect(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            connections.append(result)
            if not str(path).startswith("file:") and str(path) != ":memory:":
                scratch.append(Path(path))
                self.assertEqual(Path(path).stat().st_mode & 0o077, 0)
                self.assertEqual(Path(path).parent.stat().st_mode & 0o077, 0)
            return result
        with patch("sqlite3.connect", side_effect=connect), \
                patch.object(verifier, "_verify_members", side_effect=RuntimeError("synthetic interruption")):
            with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                verifier.verify()
        self.assertTrue(scratch)
        self.assertTrue(all(not path.parent.exists() for path in scratch))
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_unavailable_scratch_cannot_be_a_successful_acceptance(self):
        for failure in (OSError("synthetic scratch unavailable"), sqlite3.OperationalError("synthetic full")):
            if isinstance(failure, sqlite3.Error):
                failure.sqlite_errorcode = 13  # SQLITE_FULL, without filling a real volume.
            with self.subTest(failure=type(failure).__name__), \
                    patch("core.semantic.verification.workspace", side_effect=failure):
                report = self.fixture.verify()
            self.assertFalse(report.complete)
            self.assertEqual(self.codes(report), {"verification_input_unavailable"})

    def test_complete_requires_actual_receipts_projections_and_index(self):
        report = self.fixture.verify()
        self.assertTrue(report.complete, report.to_dict())
        self.assertEqual(2, report.counts["captured_versions"])
        self.assertEqual(1, report.counts["items"])

    def test_frozen_capture_context_verifies_independently_with_exact_claim_anchor(self):
        fixture = VerificationFixture(Path(self.temp.name) / "context-valid", context_text="前次捕获的具体经历。")
        with patch("core.semantic.store.preceding_capture", side_effect=AssertionError("trusted producer selection")), \
             patch.object(SemanticStore, "preceding_context", side_effect=AssertionError("called producer context admission")):
            report = fixture.verify()
        self.assertTrue(report.complete, report.to_dict())
        with closing(fixture.store.connect()) as db:
            self.assertEqual(db.execute("SELECT status FROM source_dispositions WHERE source_version_id=(SELECT source_version_id "
                "FROM capture_batch_members WHERE batch_id='earlier-capture')").fetchone()[0], "context_only")
            self.assertEqual(db.execute("SELECT count(*) FROM source_core_jobs WHERE job_id=?", (fixture.extract_job,)).fetchone()[0], 2)

    def test_rehashed_capture_context_cannot_forge_binding_or_hide_external_evidence(self):
        changes = {
            "generation": lambda inputs, versions: inputs["preceding_context"].update(generation_id="foreign-generation"),
            "capture": lambda inputs, versions: inputs["preceding_context"].update(batch_id="foreign-capture"),
            "receipt": lambda inputs, versions: inputs["preceding_context"].update(receipt_id="foreign-receipt"),
            "source": lambda inputs, versions: inputs["preceding_context"]["source_ids"].append("foreign-source"),
            "span": lambda inputs, versions: inputs["preceding_context"]["spans"][0].update(excerpt="invented text"),
            "version": lambda inputs, versions: versions.update(capture_context="unknown-contract"),
            "false-continuity": lambda inputs, versions: inputs["preceding_context"].update(cursor_continuity="exact"),
            "remove-contract": lambda inputs, versions: (inputs.pop("preceding_context"), versions.pop("capture_context")),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                fixture = VerificationFixture(Path(self.temp.name) / ("context-" + name), context_text="只可从原冻结语境引用的经历。")
                with fixture.store.transaction() as db:
                    row = db.execute("SELECT * FROM jobs WHERE job_id=?", (fixture.extract_job,)).fetchone()
                    inputs, versions = json.loads(row["inputs_json"]), json.loads(row["versions_json"])
                    change(inputs, versions)
                    db.execute("UPDATE jobs SET inputs_json=?,versions_json=?,job_key=? WHERE job_id=?", (dumps(inputs), dumps(versions),
                        digest({"generation_id": fixture.store.generation_id, "kind": "extract", "inputs": inputs, "versions": versions}),
                        fixture.extract_job))
                report = fixture.verify()
                self.assertFalse(report.complete)
                self.assertIn("observation_context_unbound", self.codes(report))
                if name != "remove-contract":
                    self.assertIn("extraction_context_invalid", self.codes(report))
                self.assertNotIn("accepted_job_identity_mismatch", self.codes(report))

    def test_context_raw_mapping_tampering_cannot_pass_generic_span_endpoints(self):
        fixture = VerificationFixture(Path(self.temp.name) / "context-mapping",
            context_text="先前参考 https://example.test/a?token=synthetic-secret 然后继续。")
        self.assertTrue(fixture.verify().complete)
        with fixture.store.transaction() as db:
            row = db.execute("SELECT mapping_json FROM source_spans WHERE span_id=?", (fixture.context_span_id,)).fetchone()
            mapping = json.loads(row[0])
            self.assertGreater(len(mapping), 1)
            mapping[1]["raw_start"] += 1
            db.execute("UPDATE source_spans SET mapping_json=? WHERE span_id=?", (dumps(mapping), fixture.context_span_id))
        report = fixture.verify()
        self.assertIn("evidence_context_span_invalid", self.codes(report))
        self.assertNotIn("evidence_span_invalid", self.codes(report))

    def test_rehashed_accepted_extraction_cannot_forge_attribution(self):
        with self.fixture.store.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (self.fixture.extract_job,)).fetchone()
            inputs, versions = json.loads(row["inputs_json"]), json.loads(row["versions_json"])
            inputs["members"][0]["speaker_id"] = "invented-speaker"
            inputs["members"][0]["speaker_name"] = "编造名字"
            db.execute("UPDATE jobs SET inputs_json=?,job_key=? WHERE job_id=?", (dumps(inputs),
                digest({"generation_id": self.fixture.store.generation_id, "kind": "extract", "inputs": inputs, "versions": versions}),
                self.fixture.extract_job))
        report = self.fixture.verify()
        self.assertIn("extraction_input_mismatch", self.codes(report))
        self.assertNotIn("accepted_job_identity_mismatch", self.codes(report))

    def brief_fixture(self, *, large=False, require_body=False):
        from core.semantic.briefing import BriefChange, BriefingService
        from core.semantic.controls import ReaderControls, SearchAnswer
        from tests.test_semantic_briefing import bounded_editor_response
        from tests.test_semantic_projections import ScriptedTextProvider
        f = self.fixture
        with closing(f.store.connect()) as db:
            change = dict(db.execute("SELECT * FROM change_events WHERE item_id='item' ORDER BY change_id DESC LIMIT 1").fetchone())
        if require_body:
            body_provider = ScriptedTextProvider(f.plan.to_json())
            writer = BodyPlanWriter(body_provider, f.store.generation_id, f.ledger)
            f.publisher.publish_item(f.snapshot, writer.plan(f.snapshot, request_id="synthetic-body"))
            f.manifest["rebuild_reading_required"] = True
        provider = ScriptedTextProvider(*([bounded_editor_response] * 100))
        controls = ReaderControls(f.ledger, f.publisher, search=lambda _: SearchAnswer("", ()))
        service = BriefingService(f.ledger, f.publisher, controls, provider, timezone="UTC", max_input_bytes=6000)
        candidate = BriefChange(str(change["change_id"]), "item", 1, "周末散步", "",
            "周末在河边散步。" * (1500 if large else 1), (f.span_id,),
            datetime.fromtimestamp(1, timezone.utc).isoformat(), datetime.fromtimestamp(change["created_at"], timezone.utc).isoformat(),
            before_revision=0, contributing_change_ids=(str(change["change_id"]),))
        now = datetime.fromtimestamp(20, timezone.utc)
        receipt = service.build((candidate,), now=now)
        self.assertEqual(receipt.status, "success")
        f.manifest["required_brief"] = {"edition_id": receipt.edition_id, "local_day": receipt.local_day,
            "timezone": "UTC", "as_of": now.isoformat(), "content_revision": receipt.revision,
            "max_input_bytes": service.max_input_bytes, "provider_route": provider_binding(provider)}
        return service, provider

    def test_fresh_reading_policy_verifies_v12_without_derived_literal_and_rejects_legacy_downgrade(self):
        self.fixture = VerificationFixture(self.fixture.root / "fresh", reading_policy=CONTENT_READING_POLICY,
            unit_kind="step", unit_text="派生操作说明：周末前往河边，进行散步活动。", body_text="周末去河边散步。")
        self.brief_fixture(require_body=True)
        f = self.fixture
        self.assertTrue(f.verify().complete, f.verify().to_dict())
        with closing(f.store.connect()) as db:
            self.assertEqual(db.execute("SELECT prompt_version FROM reader_text_artifacts WHERE request_id='synthetic-body'").fetchone()[0],
                             "body-plan-v12")
        f.manifest.pop("reading_policy")
        self.assertIn("reading_policy_binding_mismatch", self.codes(f.verify()))
        # Even relabeling both metadata and artifact version as legacy cannot
        # make its historical derived-literal guarantee disappear.
        with f.store.transaction() as db:
            db.execute("UPDATE generation_manifest SET metadata_json='{}'")
            db.execute("UPDATE reader_text_artifacts SET prompt_version='body-plan-v11' WHERE request_id='synthetic-body'")
        self.assertIn("body_plan_literal_obligation_missing", self.codes(f.verify()))

    def test_source_policy_verifies_v13_and_cannot_use_v12_artifact_or_metadata(self):
        self.fixture = VerificationFixture(self.fixture.root / "source-v13", reading_policy=SOURCE_READING_POLICY,
            unit_kind="step", unit_text="HIDDEN derived wording", body_text="周末去河边散步。")
        self.brief_fixture(require_body=True)
        f = self.fixture
        self.assertTrue(f.verify().complete, f.verify().to_dict())
        with f.store.transaction() as db:
            row = db.execute("SELECT prompt_version FROM reader_text_artifacts WHERE request_id='synthetic-body'").fetchone()
            self.assertEqual(row[0], "body-plan-v13")
            db.execute("UPDATE reader_text_artifacts SET prompt_version='body-plan-v12' WHERE request_id='synthetic-body'")
        self.assertIn("body_provider_version_conflict", self.codes(f.verify()))
        f.manifest["reading_policy"] = CONTENT_READING_POLICY
        self.assertIn("reading_policy_binding_mismatch", self.codes(f.verify()))

    def test_editorial_policy_verifies_v14_and_rejects_other_mode_versions_and_binding(self):
        self.check_editorial_policy(EDITORIAL_READING_POLICY, "v14")

    def test_editorial_v2_verifies_v15_and_rejects_v14_or_other_mode_proof(self):
        self.check_editorial_policy(EDITORIAL_V2_READING_POLICY, "v15")

    def check_editorial_policy(self, policy, version):
        self.fixture = VerificationFixture(self.fixture.root / ("editorial-" + version), reading_policy=policy,
            unit_kind="step", unit_text="派生说明：周末前往河边散步。", body_text="周末去河边散步。")
        self.brief_fixture(require_body=True)
        f = self.fixture
        self.assertTrue(f.verify().complete, f.verify().to_dict())
        with f.store.transaction() as db:
            self.assertEqual(db.execute("SELECT prompt_version FROM reader_text_artifacts WHERE request_id='synthetic-body'").fetchone()[0], "body-plan-" + version)
        other_versions = ("body-plan-v13", "body-plan-v12", "body-plan-v11") + (("body-plan-v14",) if version == "v15" else ("body-plan-v15",))
        for other_version in other_versions:
            with f.store.transaction() as db:
                db.execute("UPDATE reader_text_artifacts SET prompt_version=? WHERE request_id='synthetic-body'", (other_version,))
            self.assertIn("body_provider_version_conflict", self.codes(f.verify()))
        f.manifest["reading_policy"] = SOURCE_READING_POLICY
        self.assertIn("reading_policy_binding_mismatch", self.codes(f.verify()))
        # Relabeling both generation and artifact as legacy still restores its
        # real historical derived-literal obligation; no mode bypass exists.
        f.manifest.pop("reading_policy")
        with f.store.transaction() as db:
            db.execute("UPDATE generation_manifest SET metadata_json='{}'")
            db.execute("UPDATE reader_text_artifacts SET prompt_version='body-plan-v11' WHERE request_id='synthetic-body'")
        self.assertIn("body_plan_literal_obligation_missing", self.codes(f.verify()))

    def test_fresh_reading_verifier_keeps_unit_home_evidence_and_resource_obligations(self):
        self.fixture = VerificationFixture(self.fixture.root / "fresh-obligations", reading_policy=CONTENT_READING_POLICY)
        self.brief_fixture(require_body=True)
        f = self.fixture
        original = json.loads(f.plan.to_json())
        for field, value, code in (("unit_ids", [], "body_plan_unit_coverage"),
                                   ("evidence_ids", [], "body_plan_evidence_missing"),
                                   ("resource_ids", [], "body_plan_resource_missing"),
                                   ("primary_home", "wrong", "body_plan_binding_mismatch")):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(original))
                changed["blocks"][0][field] = value
                with f.store.transaction() as db:
                    db.execute("UPDATE reader_body_plans SET plan_json=?", (json.dumps(changed),))
                    db.execute("UPDATE reader_text_artifacts SET artifact_json=? WHERE request_id='synthetic-body'", (json.dumps(changed),))
                self.assertIn(code, self.codes(f.verify()))

    def test_attributed_brief_and_body_versions_have_independent_current_proof(self):
        self.brief_fixture(require_body=True)
        report = self.fixture.verify()
        self.assertTrue(report.complete, report.to_dict())
        with self.fixture.store.transaction() as db:
            db.execute("UPDATE reader_text_artifacts SET prompt_version='body-plan-unrecognized' WHERE prompt_version='body-plan-v11'")
        self.assertIn("body_provider_version_conflict", self.codes(self.fixture.verify()))

    def test_navigation_revision_can_reuse_exact_accepted_body_without_provider(self):
        from tests.test_semantic_projections import ScriptedTextProvider
        self.brief_fixture(require_body=True)
        f = self.fixture
        f.store.create_view("navigation", "生活", "生活经历")
        read_set = {"store_revision": f.store.read_revision(), "items": {"item": 1},
                    "views": {"navigation": {"definition_revision": 1, "override_revision": 0}}}
        actions = [{"action_key": "navigation", "type": "ADD_MEMBERSHIP", "item_id": "item", "view_id": "navigation",
                    "origin": "model", "placement": "正文", "reason": "合成导航分类"}]
        job = accept_fixture(f.store, {"actions": actions, "read_set": read_set}, "propose", inputs={"state": {
            "read_set": read_set, "observations": [], "items": {"item": f.store.get_item("item")},
            "views": {"navigation": f.store.get_view("navigation")}}})
        f.store.apply_proposal(job, actions, read_set=read_set)
        self.assertEqual(f.store.get_item("item")["content_revision"], 2)
        provider = ScriptedTextProvider()  # Any invocation would fail, proving reuse.
        writer = BodyPlanWriter(provider, f.store.generation_id, f.ledger)
        f.publisher.publish_canonical_item(f.store, "item", writer, request_id="synthetic-navigation-body")
        self.assertEqual(provider.calls, [])
        report = f.verify()
        self.assertNotIn("body_provider_artifact_missing", self.codes(report))
        self.assertNotIn("body_provider_version_conflict", self.codes(report))

    def test_brief_version_mismatch_and_missing_accepted_writer_do_not_complete(self):
        self.brief_fixture()
        self.assertTrue(self.fixture.verify().complete)
        with self.fixture.store.transaction() as db:
            db.execute("UPDATE reader_text_artifacts SET prompt_version='brief-write-v2' WHERE prompt_version='brief-write-v3'")
        report = self.fixture.verify()
        self.assertIn("brief_provider_version_conflict", self.codes(report))
        self.assertIn("brief_provider_artifact_missing", self.codes(report))

    def test_rehashed_brief_names_still_require_canonical_source_attribution(self):
        self.brief_fixture()
        f = self.fixture
        with f.store.transaction() as db:
            inputs = dict(db.execute("SELECT * FROM reader_brief_inputs").fetchone())
            candidates = json.loads(inputs["inputs_json"])
            candidates[0]["attributions"][0]["speaker_name"] = "不是来源中的名字"
            required = f.manifest["required_brief"]
            new_hash = digest({"changes": candidates, "bindings": json.loads(inputs["bindings_json"]),
                "generation": f.store.generation_id, "day": required["local_day"], "timezone": "UTC",
                "provider_route": required["provider_route"], "prompt_version": "brief-plan-v3", "max_input_bytes": 6000})
            db.execute("UPDATE reader_brief_inputs SET input_hash=?,inputs_json=?", (new_hash, dumps(candidates)))
            db.execute("UPDATE reader_brief_editions SET input_hash=?", (new_hash,))
            db.execute("UPDATE reader_brief_attempts SET input_hash=?", (new_hash,))
            db.execute("UPDATE reader_brief_history SET inputs_json=?", (dumps(candidates),))
        report = f.verify()
        self.assertIn("brief_attribution_source_mismatch", self.codes(report))
        self.assertNotIn("brief_input_identity_mismatch", self.codes(report))

    def test_brief_v2_compaction_coverage_includes_attribution_fields(self):
        _, provider = self.brief_fixture(large=True)
        report = self.fixture.verify()
        self.assertTrue(report.complete, report.to_dict())
        self.assertTrue(any(json.loads(prompt)["phase"] == "brief_compact_page_v1" for prompt, *_ in provider.calls))
        with self.fixture.store.transaction() as db:
            row = db.execute("SELECT job_key,artifact_json FROM reader_text_artifacts WHERE prompt_version='brief-compact-page-v2' ORDER BY rowid DESC LIMIT 1").fetchone()
            artifact = json.loads(row["artifact_json"])
            artifact["coverage"][1] -= 1
            db.execute("UPDATE reader_text_artifacts SET artifact_json=? WHERE job_key=?", (dumps(artifact), row["job_key"]))
        self.assertIn("brief_compaction_coverage_incomplete", self.codes(self.fixture.verify()))

    def superseded_fixture(self, *, unknown_decision=False, reuse_action_key=False):
        return VerificationFixture(Path(self.temp.name) / "superseded", stale_proposal=True,
                                   unknown_decision=unknown_decision, reuse_action_key=reuse_action_key)

    def test_superseded_proposal_requires_committed_replacement_and_preserves_artifacts(self):
        fixture = self.superseded_fixture(reuse_action_key=True)
        report = fixture.verify()
        self.assertTrue(report.complete, report.to_dict())
        self.assertEqual(report.counts["superseded_proposals"], 1)
        with closing(fixture.store.connect()) as conn:
            proof = conn.execute("SELECT * FROM proposal_supersessions").fetchone()
            for job_id in (fixture.old_propose_job, fixture.old_decision_job):
                row = conn.execute("SELECT state,receipt_id FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                self.assertEqual(tuple(row), ("done", proof["receipt_id"]))
            retained = json.loads(conn.execute("SELECT artifact_json FROM accepted_job_artifacts WHERE job_id=?",
                                               (fixture.old_propose_job,)).fetchone()[0])
            self.assertEqual(retained, fixture.old_artifact)
        self.assertEqual(fixture.store.proposal_supersession(fixture.old_propose_job), fixture.action_receipt)

    def test_missing_supersession_cannot_discharge_historical_accepted_actions(self):
        fixture = self.superseded_fixture()
        with fixture.store.transaction() as conn:
            conn.execute("DELETE FROM proposal_supersessions")
        report = fixture.verify()
        self.assertFalse(report.complete)
        self.assertIn("accepted_action_pending", self.codes(report))

    def test_rehashed_supersession_cannot_fabricate_resolution_membership(self):
        fixture = self.superseded_fixture()
        with fixture.store.transaction() as conn:
            proof = json.loads(conn.execute("SELECT proof_json FROM proposal_supersessions").fetchone()[0])
            proof["observation_memberships"][0]["revision"] = 999
            receipt_id = "supersession_" + digest(proof)
            conn.execute("UPDATE proposal_supersessions SET proof_json=?,receipt_id=?", (dumps(proof), receipt_id))
            for job in proof["terminal_jobs"]:
                conn.execute("UPDATE jobs SET receipt_id=? WHERE job_id=?", (receipt_id, job["job_id"]))
        report = fixture.verify()
        self.assertFalse(report.complete)
        self.assertIn("proposal_supersession_unproved", self.codes(report))
        self.assertIn("accepted_action_pending", self.codes(report))

    def test_missing_replacement_receipt_blocks_supersession(self):
        fixture = self.superseded_fixture()
        with fixture.store.transaction() as conn:
            conn.execute("DELETE FROM decision_log WHERE job_id=?", (fixture.propose_job,))
        report = fixture.verify()
        self.assertFalse(report.complete)
        self.assertIn("proposal_supersession_unproved", self.codes(report))

    def test_unresolved_observation_cannot_be_hidden_by_supersession(self):
        fixture = self.superseded_fixture()
        with fixture.store.transaction() as conn:
            conn.execute("UPDATE observations SET resolution_status='unresolved'")
        report = fixture.verify()
        self.assertFalse(report.complete)
        self.assertIn("proposal_supersession_unproved", self.codes(report))
        self.assertIn("observation_resolution_pending", self.codes(report))

    def test_unknown_decision_is_not_superseded_even_with_a_committed_new_plan(self):
        fixture = self.superseded_fixture(unknown_decision=True)
        self.assertIsNone(fixture.store.proposal_supersession(fixture.old_propose_job))
        with closing(fixture.store.connect()) as conn:
            state = conn.execute("SELECT state FROM jobs WHERE job_id=?", (fixture.old_decision_job,)).fetchone()[0]
        self.assertEqual(state, "unknown")
        report = fixture.verify()
        self.assertFalse(report.complete)
        self.assertIn("proposal_outcome_unknown", self.codes(report))
        self.assertIn("accepted_action_pending", self.codes(report))

    def test_same_read_set_is_not_a_stale_replanning_proof(self):
        fixture = VerificationFixture(Path(self.temp.name) / "same-read", stale_proposal=True, same_read_set=True)
        self.assertIsNone(fixture.store.proposal_supersession(fixture.old_propose_job))
        report = fixture.verify()
        self.assertFalse(report.complete)
        self.assertIn("accepted_action_pending", self.codes(report))

    def test_lost_member_with_same_count_is_detected_by_identity(self):
        members = self.fixture.manifest["expected_batches"][0]["member_version_ids"]
        members[1] = members[0]
        report = self.fixture.verify()
        self.assertFalse(report.complete)
        self.assertIn("capture_member_identity_mismatch", self.codes(report))

    def test_missing_receipt_and_outbox_cannot_be_masked_by_success_counts(self):
        with self.fixture.store.transaction() as conn:
            conn.execute("DELETE FROM processing_receipts")
            conn.execute("DELETE FROM projection_outbox")
        self.fixture.manifest["producer_counts"] = {"success": True, "processed": 2, "items": 1}
        report = self.fixture.verify()
        self.assertFalse(report.complete)
        self.assertIn("processing_receipt_missing", self.codes(report))
        self.assertIn("action_projection_outbox_missing", self.codes(report))

    def test_same_count_processing_proof_cannot_substitute_one_source(self):
        with self.fixture.store.transaction() as conn:
            row = conn.execute("SELECT * FROM processing_receipts WHERE batch_id='batch'").fetchone()
            receipt = json.loads(row["receipt_json"])
            receipt["proof"]["core_sources"][1] = dict(receipt["proof"]["core_sources"][0])
            receipt["proof_fingerprint"] = digest(receipt["proof"])
            conn.execute("UPDATE processing_receipts SET receipt_json=? WHERE batch_id='batch'", (dumps(receipt),))
            conn.execute("UPDATE canonical_transactions SET actions_json=? WHERE transaction_id=?", (
                dumps([{"type": "COMPLETE_BATCH", "batch_id": "batch", "proof_fingerprint": receipt["proof_fingerprint"]}]),
                row["transaction_id"]))
        self.assertIn("processing_proof_mismatch", self.codes(self.fixture.verify()))

    def test_missing_change_receipt_and_unproved_index_supersession_fail(self):
        with self.fixture.store.transaction() as conn:
            conn.execute("DELETE FROM change_events")
            conn.execute("UPDATE index_outbox SET status='superseded',receipt_id='unproved'")
        codes = self.codes(self.fixture.verify())
        self.assertIn("item_change_receipt_missing", codes)
        self.assertIn("outbox_supersession_unproved", codes)

    def test_accepted_job_hash_binds_exact_original_inputs(self):
        with self.fixture.store.transaction() as conn:
            conn.execute("UPDATE jobs SET inputs_json='{}' WHERE job_id=?", (self.fixture.extract_job,))
        self.assertIn("accepted_job_identity_mismatch", self.codes(self.fixture.verify()))

    def test_migration_aggregate_borrows_exact_processing_outboxes(self):
        processing = self.fixture.store.complete_batch("batch")
        receipt = {"receipt_id": "migration", "migration_id": "migration", "canonical_receipt_id": "migration",
                   "outbox_receipt_id": "migration", "semantic_generation_id": self.fixture.store.generation_id,
                   "processing_receipt_ids": [processing["receipt_id"]],
                   "capture_receipt_ids": processing["capture_receipt_ids"]}
        self.fixture.store.record_migration_receipt(receipt, [processing])
        report = self.fixture.verify()
        self.assertTrue(report.complete, report.to_dict())
        with self.fixture.store.transaction() as conn:
            conn.execute("UPDATE canonical_transactions SET outbox_ids_json='[]' WHERE transaction_id='migration'")
        self.assertIn("migration_proof_membership_mismatch", self.codes(self.fixture.verify()))

    def test_missing_projection_is_pending_even_when_old_row_says_ready(self):
        path = self.fixture.publisher.resolve("item")
        (self.fixture.projections / path).unlink()
        report = self.fixture.verify()
        self.assertFalse(report.complete)
        self.assertIn("item_current_projection_missing", self.codes(report))

    def test_missing_source_evidence_page_prevents_completion(self):
        path = self.fixture.publisher.resolve("source:item:1")
        (self.fixture.projections / path).unlink()
        self.assertIn("item_evidence_projection_missing", self.codes(self.fixture.verify()))

    def test_unconfirmed_dense_receipt_is_index_pending(self):
        with self.fixture.store.transaction() as conn:
            conn.execute("UPDATE semantic_index_receipts SET status='pending'")
        report = self.fixture.verify()
        self.assertIn("item_dense_index_pending", self.codes(report))

    def test_cited_resource_must_survive_both_observation_and_item_attachment(self):
        self.assertTrue(self.fixture.verify().complete)
        with self.fixture.store.transaction() as conn:
            conn.execute("DELETE FROM resource_refs WHERE item_id='item'")
        self.assertIn("resource_evidence_conservation_mismatch", self.codes(self.fixture.verify()))

    def test_h1_and_inventory_are_exact_not_truthy_counts(self):
        self.fixture.manifest["source_inventory"]["complete"] = 1
        self.fixture.manifest["h1"][0]["cursors"] = {"shard": {"generation_id": "physical", "cursor_token": "[3,0]"}}
        report = self.fixture.verify()
        self.assertIn("source_inventory_incomplete", self.codes(report))
        self.assertIn("source_h1_not_reached", self.codes(report))

    def test_personal_conflict_needs_explicit_current_preservation_choice(self):
        path = self.fixture.publisher.resolve("item")
        target = self.fixture.projections / path
        text = target.read_text()
        target.write_text(text.replace("周末在河边散步。", "我还想保留这段私人的回忆。"))
        result = self.fixture.publisher.publish_item(self.fixture.snapshot, self.fixture.plan)
        self.assertEqual("conflict", result.status)
        self.assertIn("item_current_projection_missing", self.codes(self.fixture.verify()))
        self.fixture.publisher.resolve_proposal(result.proposal_id, "keep_personal",
            expected_revision=self.fixture.snapshot.content_revision, expected_file_hash=result.file_hash)
        report = self.fixture.verify()
        self.assertTrue(report.complete, report.to_dict())
        self.assertEqual(1, report.counts["user_preserved_conflicts"])
        self.assertIn("私人的回忆", target.read_text())

    def test_wrong_span_and_no_core_reference_fail_independently(self):
        with self.fixture.store.transaction() as conn:
            conn.execute("UPDATE source_spans SET excerpt='invented'")
            conn.execute("UPDATE capture_batch_members SET core=0")
        report = self.fixture.verify()
        self.assertIn("evidence_span_invalid", self.codes(report))
        self.assertIn("observation_core_evidence_missing", self.codes(report))

    def test_report_is_content_free_and_read_does_not_create_database(self):
        missing = self.fixture.root / "does-not-exist.sqlite"
        report = RebuildVerifier(missing, self.fixture.manifest).verify()
        self.assertFalse(missing.exists())
        rendered = json.dumps(report.to_dict())
        self.assertNotIn(str(self.fixture.root), rendered)
        self.assertNotIn("周末", rendered)
        self.assertIn("verification_input_unavailable", self.codes(report))

    def test_later_processing_receipt_cannot_jump_a_missing_prefix(self):
        receipt = self.fixture.store.capture_batch("later", [], source_namespace="source", chat_id="chat",
            inventory_digest="inventory", inventory_revision=1, input_cursors=self.fixture.end,
            output_cursors=self.fixture.end, source_eof=True)
        adopt_fixture_batch(self.fixture.store, "later")
        self.fixture.store.complete_batch("later")
        self.fixture.manifest["expected_batches"].append({"batch_id": "later", "member_version_ids": receipt["member_version_ids"]})
        with self.fixture.store.transaction() as conn:
            conn.execute("DELETE FROM processing_receipts WHERE batch_id='batch'")
        self.assertIn("applied_prefix_gap", self.codes(self.fixture.verify()))

    def test_source_payload_and_generation_binding_are_independently_checked(self):
        with self.fixture.store.transaction() as conn:
            conn.execute("UPDATE source_records SET physical_generation='other',text='changed' WHERE source_id='one'")
        codes = self.codes(self.fixture.verify())
        self.assertIn("source_payload_identity_mismatch", codes)
        self.assertIn("capture_member_cursor_mismatch", codes)

    def test_duplicate_projection_identity_fails_even_with_good_registered_page(self):
        path = self.fixture.publisher.resolve("item")
        (self.fixture.projections / "duplicate.md").write_bytes((self.fixture.projections / path).read_bytes())
        self.assertIn("projection_identity_collision", self.codes(self.fixture.verify()))

    def test_unredacted_machine_url_fails_even_when_stored_hash_matches(self):
        path = self.fixture.publisher.resolve("item")
        target = self.fixture.projections / path
        text = target.read_text()
        from core.semantic.projections import patch_region, region_body
        text = patch_region(text, "body", "\nhttps://example.test/?token=private\n")
        target.write_text(text)
        with self.fixture.store.transaction() as conn:
            conn.execute("UPDATE reader_projections SET machine_hash=? WHERE object_id='item'",
                         (hashlib.sha256(region_body(text, "body").encode()).hexdigest(),))
        self.assertIn("projection_url_unredacted", self.codes(self.fixture.verify()))

    def test_every_legacy_record_and_manual_page_needs_exact_conservation(self):
        old = LegacyFixture(self.fixture.root / "old")
        inventory = old.plan()
        imported = import_legacy(self.fixture.store, inventory)
        self.fixture.manifest["legacy_inventory"] = inventory.to_dict()
        for batch_id in imported.batch_ids:
            receipt = self.fixture.store.get_batch(batch_id)["receipt"]
            self.fixture.manifest["expected_batches"].append({"batch_id": batch_id, "member_version_ids": receipt["member_version_ids"]})
        original = self.fixture.verify()
        self.assertNotIn("legacy_record_unaccounted", self.codes(original))
        self.assertIn("legacy_backup_pending", self.codes(original))
        backups = {}
        backup_root = self.fixture.root / "backups"
        backup_root.mkdir()
        for index, file in enumerate(inventory.files):
            if file.owned:
                path = backup_root / (str(index) + ".md")
                path.write_bytes(file.original_bytes())
                backups[file.relative_path] = path
        record_legacy_backups(self.fixture.store, inventory, backups)
        report = self.fixture.verify()
        self.assertNotIn("legacy_backup_pending", self.codes(report))
        self.assertNotIn("legacy_backup_missing", self.codes(report))
        with self.fixture.store.transaction() as conn:
            target = next(r.legacy_ref for r in inventory.records if r.kind == "event")
            conn.execute("DELETE FROM legacy_mappings WHERE legacy_id=? AND target_type='source_version'", (target,))
        self.assertIn("legacy_record_unaccounted", self.codes(self.fixture.verify()))
        (old.tree / "life.md").write_text("Manual notes were lost")
        self.assertIn("legacy_original_bytes_changed", self.codes(self.fixture.verify()))

    def test_legacy_resource_metadata_loss_invalidates_capture_and_conservation(self):
        inventory = LegacyFixture(self.fixture.root / "old").plan()
        result = import_legacy(self.fixture.store, inventory)
        self.fixture.manifest["legacy_inventory"] = inventory.to_dict()
        for batch_id in result.batch_ids:
            receipt = self.fixture.store.get_batch(batch_id)["receipt"]
            self.fixture.manifest["expected_batches"].append({
                "batch_id": batch_id, "member_version_ids": receipt["member_version_ids"]})
        self.assertNotIn("legacy_record_unaccounted", self.codes(self.fixture.verify()))
        with self.fixture.store.transaction() as conn:
            conn.execute("UPDATE source_resource_refs SET metadata_json='{}' WHERE purpose='legacy_attachment'")
        codes = self.codes(self.fixture.verify())
        self.assertIn("legacy_record_unaccounted", codes)
        self.assertIn("source_payload_identity_mismatch", codes)

    def test_recorded_topic_mapping_requires_real_cited_legacy_source(self):
        inventory = LegacyFixture(self.fixture.root / "old").plan()
        imported = import_legacy(self.fixture.store, inventory)
        topics = [r for r in inventory.records if r.kind == "topic"]
        batch_id = imported.batch_ids[0]
        members = self.fixture.store.get_batch(batch_id)["members"]
        sources = tuple(CapturedSource(r["source_version_id"], r["text"], r["core"], r["source_sent_at"])
                        for r in members)
        spans = {s.source_id: s for s in prepare_spans(sources, namespace=self.fixture.store.generation_id)}
        observations = []
        for index, topic in enumerate(topics):
            span = spans[imported.record_versions[topic.legacy_ref]]
            observations.append({"observation_id": "legacy-observation-" + str(index),
                "title": topic.text, "scene_kind": "life", "spans": [asdict(span)],
                "units": [{"unit_id": "legacy-unit-" + str(index), "kind": "scene", "text": span.excerpt,
                           "span_ids": [span.span_id]}], "topics": [], "claims": []})
        observed = {imported.record_versions[t.legacy_ref] for t in topics}
        dispositions = {s.source_id: {"status": "noise", "reason": "synthetic_mapping_fixture"}
                        for s in sources if s.source_id not in observed}
        job_id = accept_fixture(self.fixture.store, {"observations": observations, "dispositions": dispositions},
            "extract", inputs={"batch_id": batch_id, "members": [asdict(s) for s in sources]})
        self.fixture.store.commit_extraction(job_id, batch_id, observations, dispositions)
        actions = [{"action_key": "legacy-create-" + str(i), "type": "CREATE_ITEM", "item_id": "legacy-item-" + str(i),
                    "kind": "life", "title": row["title"], "identity_scope": {"legacy": str(i)},
                    "observation_ids": [row["observation_id"]]} for i, row in enumerate(observations)]
        read_set = {"store_revision": self.fixture.store.read_revision(), "items": {}, "views": {}}
        job_id = accept_fixture(self.fixture.store,
            {"proposal": {"actions": actions, "novelty": {"considered_item_ids": [], "reason": "synthetic topic evidence"}},
             "read_set": read_set}, "propose", inputs={"batch_id": batch_id, "state": {"read_set": read_set,
                "observations": [self.fixture.store.get_observation(o["observation_id"]) for o in observations],
                "items": {}, "views": {}, "new_item_ids": [a["item_id"] for a in actions]}})
        self.fixture.store.apply_proposal(job_id, actions, read_set=read_set)
        for action in actions:
            snapshot = self.fixture.adapter.snapshot(action["item_id"])
            body = BodyPlan(snapshot.item_id, snapshot.content_revision, tuple(
                BodyBlock("block-" + str(i), "scene", unit.text, (unit.id,), unit.evidence_ids,
                          resource_ids=unit.resource_ids, primary_home=snapshot.item_id)
                for i, unit in enumerate(snapshot.units)))
            self.fixture.publisher.publish_item(snapshot, body)
        mapping = {t.legacy_ref: [actions[i]["item_id"]] for i, t in enumerate(topics)}
        paths = {a["item_id"]: self.fixture.publisher.resolve(a["item_id"]) for a in actions}
        publication = record_legacy_resolution(self.fixture.store, inventory, mapping, paths)
        self.assertEqual({"redirect"}, {p["mode"] for p in publication["pages"]})
        self.assertEqual(mapping, derive_topic_item_map(self.fixture.store, inventory))
        with closing(self.fixture.store.connect()) as conn:
            self.assertEqual(2, conn.execute("SELECT count(*) FROM legacy_mappings WHERE kind='topic' AND disposition='resolved'").fetchone()[0])
        from core.semantic.legacy import LegacyError
        with self.assertRaisesRegex(LegacyError, "legacy_backup_missing"):
            publish_legacy_navigation(self.fixture.store, inventory, publication, self.fixture.publisher)
        backups = {}
        backup_root = self.fixture.root / "backups"
        backup_root.mkdir()
        for i, file in enumerate(inventory.files):
            if file.owned:
                path = backup_root / (str(i) + ".bin")
                path.write_bytes(file.original_bytes())
                backups[file.relative_path] = path
        record_legacy_backups(self.fixture.store, inventory, backups)
        result = publish_legacy_navigation(self.fixture.store, inventory, publication, self.fixture.publisher)
        self.assertEqual("published", result["status"])
        replay = publish_legacy_navigation(self.fixture.store, inventory, publication, self.fixture.publisher)
        self.assertEqual({"unchanged"}, {r["status"] for r in replay["receipts"]})
        files = {f.relative_path: f for f in inventory.files}
        for page in publication["pages"]:
            self.assertEqual(files[page["old_path"]].original_bytes(),
                (self.fixture.projections / page["history_backup_path"]).read_bytes())
            self.assertNotIn("token=private", (self.fixture.projections / page["navigation_path"]).read_text())
        (self.fixture.projections / publication["pages"][0]["history_backup_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(LegacyError, "legacy_history_copy_collision"):
            publish_legacy_navigation(self.fixture.store, inventory, publication, self.fixture.publisher)
        with self.assertRaisesRegex(LegacyError, "legacy_mapping_evidence_missing"):
            record_legacy_resolution(self.fixture.store, inventory,
                {topics[0].legacy_ref: ["item"]}, {"item": "Items/unrelated.md"})

    def test_history_only_mapping_requires_explicit_terminal_receipts(self):
        inventory = LegacyFixture(self.fixture.root / "old").plan()
        imported = import_legacy(self.fixture.store, inventory)
        self.assertEqual({}, derive_topic_item_map(self.fixture.store, inventory))
        for batch_id in imported.batch_ids:
            members = self.fixture.store.get_batch(batch_id)["members"]
            sources = tuple(CapturedSource(r["source_version_id"], r["text"], r["core"], r["source_sent_at"])
                            for r in members)
            dispositions = {s.source_id: {"status": "context_only", "reason": "synthetic_history_fixture"} for s in sources}
            job = accept_fixture(self.fixture.store, {"observations": [], "dispositions": dispositions}, "extract",
                                 inputs={"batch_id": batch_id, "members": [asdict(s) for s in sources]})
            self.fixture.store.commit_extraction(job, batch_id, [], dispositions)
            self.fixture.store.complete_batch(batch_id)
        mapping = derive_topic_item_map(self.fixture.store, inventory)
        self.assertEqual({r.legacy_ref: [] for r in inventory.records if r.kind == "topic"}, mapping)
        plan = record_legacy_resolution(self.fixture.store, inventory, mapping, {})
        self.assertEqual({"history"}, {p["mode"] for p in plan["pages"]})
        backups = {}
        backup_root = self.fixture.root / "backups"
        backup_root.mkdir()
        for index, file in enumerate(inventory.files):
            if file.owned:
                path = backup_root / (str(index) + ".bin")
                path.write_bytes(file.original_bytes())
                backups[file.relative_path] = path
        record_legacy_backups(self.fixture.store, inventory, backups)
        published = publish_legacy_navigation(self.fixture.store, inventory, plan, self.fixture.publisher)
        self.assertEqual("published", published["status"])
        self.fixture.manifest.update(legacy_inventory=inventory.to_dict(), publication_plan=plan)
        for batch_id in imported.batch_ids:
            receipt = self.fixture.store.get_batch(batch_id)["receipt"]
            self.fixture.manifest["expected_batches"].append({
                "batch_id": batch_id, "member_version_ids": receipt["member_version_ids"]})
        report = self.fixture.verify()
        self.assertTrue(report.complete, report.to_dict())

    def test_explicit_complete_capture_pass_can_rescan_from_zero_but_undeclared_reset_fails(self):
        fixture = VerificationFixture(self.fixture.root / "passes", purpose="snapshot:first")
        original = fixture.store.get_batch("batch")
        records = [{key: row[key] for key in ("source_id", "logical_shard_id", "physical_generation",
                                            "source_cursor_token", "source_sent_at", "text", "payload", "resources")}
                   for row in original["members"]]
        second = fixture.store.capture_batch("rescan", records, source_namespace="source", chat_id="chat",
            inventory_digest="inventory", inventory_revision=1, input_cursors=fixture.start,
            output_cursors=fixture.end, source_eof=True, purpose="snapshot:second")
        adopt_fixture_batch(fixture.store, "rescan")
        fixture.store.complete_batch("rescan")
        fixture.manifest["expected_batches"].append({"batch_id": "rescan", "member_version_ids": second["member_version_ids"]})
        fixture.manifest["capture_passes"] = [
            {"receipt_id": identity, "generation_id": fixture.store.generation_id,
             "batches": [batch], "h1": fixture.manifest["h1"], "capture_seq": 2,
             "source_inventory": fixture.manifest["source_inventory"]}
            for identity, batch in (("first", fixture.capture), ("second", second))]
        report = fixture.verify()
        self.assertTrue(report.complete, report.to_dict())
        del fixture.manifest["capture_passes"]
        report = fixture.verify()
        self.assertIn("capture_pass_undeclared_reset", self.codes(report))
        self.assertIn("capture_prefix_gap", self.codes(report))


class SourceFirstHistoryVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = VerificationFixture(self.temp.name)
        self.old = LegacyFixture(self.fixture.root / "old")
        with self.fixture.store.transaction() as conn:
            conn.execute("UPDATE generation_manifest SET metadata_json=?", (json.dumps({"history_policy": SOURCE_FIRST_POLICY}),))
        self.fixture.manifest["history_policy"] = SOURCE_FIRST_POLICY
        self.fixture.manifest["history_preservation_passes"] = []
        self.fixture.manifest["history_freeze_receipts"] = []
        self.add_history()

    def add_history(self):
        inventory = self.old.plan()
        number = len(self.fixture.manifest["history_freeze_receipts"])
        plan, backups = preserve_history_fixture(self.fixture.store, inventory,
            self.fixture.root / ("history-backups-" + str(number)), self.fixture.publisher)
        from core.semantic.rebuild import backup_sqlite
        frozen = {"receipt_id": "freeze-" + str(number + 1), "legacy_inventory": inventory.to_dict(),
            "databases": {"knowledge": backup_sqlite(self.old.db,
                self.fixture.root / ("history-backups-" + str(number)) / "knowledge.sqlite")}}
        existing = next((d for d in self.fixture.manifest["history_preservation_passes"]
                         if d["receipt_id"] == inventory.manifest_id), None)
        if existing is None:
            self.fixture.manifest["history_preservation_passes"].append({"receipt_id": inventory.manifest_id,
                "legacy_inventory": inventory.to_dict(), "publication_plan": plan, "freeze_receipts": [frozen]})
        else:
            existing["freeze_receipts"].append(frozen)
        self.fixture.manifest["history_freeze_receipts"].append(frozen)
        self.fixture.manifest.update(legacy_inventory=inventory.to_dict(), publication_plan=plan)
        return inventory, plan, backups

    def codes(self):
        return {issue.code for issue in self.fixture.verify().issues}

    def test_complete_history_needs_no_semantic_dispositions_for_old_summaries_or_pages(self):
        report = self.fixture.verify()
        self.assertTrue(report.complete, report.to_dict())
        self.assertEqual(1, report.counts["capture_batches"])
        self.assertEqual(1, report.counts["items"])
        self.assertEqual(1, report.counts["legacy_history_passes"])
        path = self.fixture.publisher.resolve("item")
        (self.fixture.projections / path).unlink()
        self.assertIn("item_current_projection_missing", self.codes())

    def test_missing_or_tampered_history_records_and_resources_are_not_conserved(self):
        with self.fixture.store.transaction() as conn:
            row = dict(conn.execute("SELECT * FROM legacy_mappings WHERE target_type='history_record' "
                "AND json_array_length(json_extract(metadata_json,'$.resources'))>0 LIMIT 1").fetchone())
            metadata = json.loads(row["metadata_json"])
            metadata["resources"][0]["metadata"] = {}
            conn.execute("UPDATE legacy_mappings SET metadata_json=? WHERE kind=? AND legacy_id=? AND target_id=?",
                (json.dumps(metadata), row["kind"], row["legacy_id"], row["target_id"]))
        self.assertIn("legacy_history_record_unaccounted", self.codes())
        with self.fixture.store.transaction() as conn:
            conn.execute("DELETE FROM legacy_mappings WHERE kind=? AND legacy_id=? AND target_id=?",
                (row["kind"], row["legacy_id"], row["target_id"]))
        self.assertIn("legacy_history_record_set_mismatch", self.codes())

    def test_all_passes_remain_bound_and_earlier_backup_is_checked_after_refresh(self):
        old = self.fixture.manifest["history_preservation_passes"][0]
        (self.old.tree / "life.md").write_bytes(self.old.page_bytes + "后来补充的个人文字\n".encode())
        self.add_history()
        report = self.fixture.verify()
        self.assertTrue(report.complete, report.to_dict())
        with self.fixture.store.transaction() as conn:
            row = conn.execute("SELECT metadata_json FROM legacy_mappings WHERE kind='history_backup' "
                "AND target_id=? AND json_extract(metadata_json,'$.path')='Old/life.md'", (old["receipt_id"],)).fetchone()
            path = Path(json.loads(row[0])["backup_path"])
        path.write_bytes(b"tampered earlier copy")
        self.assertIn("legacy_backup_bytes_mismatch", self.codes())
        self.fixture.manifest["history_preservation_passes"] = self.fixture.manifest["history_preservation_passes"][1:]
        self.assertIn("legacy_history_pass_set_mismatch", self.codes())

    def test_each_history_projection_and_private_copy_requires_real_bytes_and_receipt(self):
        plan = self.fixture.manifest["publication_plan"]
        document = next(d for d in plan["documents"] if d["kind"] == "topic")
        (self.fixture.projections / document["path"]).write_text("tampered history")
        self.assertIn("legacy_history_navigation_receipt_missing", self.codes())
        with self.fixture.store.transaction() as conn:
            conn.execute("DELETE FROM legacy_mappings WHERE kind='history_navigation' AND target_id=?", (document["object_id"],))
        self.assertIn("legacy_history_navigation_receipt_missing", self.codes())
        (self.fixture.projections / plan["pages"][0]["history_backup_path"]).unlink()
        self.assertIn("legacy_history_copy_missing", self.codes())

    def test_empty_old_topic_map_is_not_a_history_publication_contract(self):
        from core.semantic.legacy import LegacyInventory
        inventory = LegacyInventory.from_dict(self.fixture.manifest["legacy_inventory"])
        wrong = build_publication_plan(inventory, {}, {})
        self.fixture.manifest["publication_plan"] = wrong
        self.fixture.manifest["history_preservation_passes"][0]["publication_plan"] = wrong
        self.assertIn("legacy_history_publication_missing", self.codes())

    def test_last_original_stays_required_and_policy_cannot_be_unknown(self):
        (self.old.tree / "life.md").write_text("lost whole personal page")
        self.assertIn("legacy_original_bytes_changed", self.codes())
        self.fixture.manifest["history_policy"] = "unrecognized-policy"
        self.assertIn("legacy_history_policy_invalid", self.codes())

    def test_earlier_publication_and_resource_receipt_remain_independent(self):
        first = self.fixture.manifest["history_preservation_passes"][0]
        (self.old.tree / "life.md").write_bytes(self.old.page_bytes + b"\nnew personal page\n")
        self.add_history()
        document = next(d for d in first["publication_plan"]["documents"] if d["kind"] == "topic")
        (self.fixture.projections / document["path"]).unlink()
        self.assertIn("legacy_history_navigation_missing", self.codes())
        with self.fixture.store.transaction() as conn:
            conn.execute("UPDATE legacy_mappings SET metadata_json='{}' WHERE target_type='history_record' AND target_id=?",
                (first["receipt_id"],))
        self.assertIn("legacy_history_record_unaccounted", self.codes())

    def test_run_bound_history_payloads_are_loaded_one_pass_at_a_time_and_tampering_is_rejected(self):
        from core.semantic.legacy import LegacyInventory
        from core.semantic.rebuild_payloads import Payloads
        import weakref
        (self.old.tree / "life.md").write_bytes(self.old.page_bytes + b"\nnext freeze\n")
        self.add_history()
        payloads = Payloads(self.fixture.root / "run", "synthetic-run")
        manifest = dict(self.fixture.manifest)
        declarations, frozen_references = [], []
        for declaration in manifest["history_preservation_passes"]:
            inventory_reference = payloads.put(declaration["legacy_inventory"])
            freezes = [payloads.put(dict(frozen, legacy_inventory=inventory_reference))
                       for frozen in declaration["freeze_receipts"]]
            frozen_references.extend(freezes)
            declarations.append({"receipt_id": declaration["receipt_id"],
                "legacy_inventory": inventory_reference,
                "publication_plan": payloads.put(declaration["publication_plan"]), "freeze_receipts": freezes})
        manifest.update(history_preservation_passes=declarations,
            history_freeze_receipts=frozen_references,
            legacy_inventory=declarations[-1]["legacy_inventory"], publication_plan=declarations[-1]["publication_plan"])
        decoded = []
        original = LegacyInventory.from_dict
        def one_pass(value):
            self.assertTrue(all(reference() is None for reference in decoded), "earlier corpus stayed resident")
            inventory = original(value)
            decoded.append(weakref.ref(inventory))
            return inventory
        with patch.object(LegacyInventory, "from_dict", side_effect=one_pass):
            report = RebuildVerifier(self.fixture.store.path, manifest, projection_root=self.fixture.projections,
                history_payload_loader=payloads.get).verify()
        self.assertTrue(report.complete, report.to_dict())
        self.assertEqual(len(decoded), 2)
        payloads.path(declarations[0]["legacy_inventory"]).write_text("{}")
        report = RebuildVerifier(self.fixture.store.path, manifest, projection_root=self.fixture.projections,
            history_payload_loader=payloads.get).verify()
        self.assertIn("legacy_history_payload_unavailable", {issue.code for issue in report.issues})

    def test_changed_topic_file_binding_retains_same_exact_page_baseline(self):
        with closing(sqlite3.connect(self.old.db)) as conn:
            conn.execute("UPDATE topics SET obsidian_path='Old/life.md' WHERE topic_id=2")
            conn.commit()
        self.add_history()
        report = self.fixture.verify()
        self.assertTrue(report.complete, report.to_dict())

    def test_missing_or_changed_earlier_whole_database_backup_blocks_unknown_table_only_freeze(self):
        first = self.fixture.manifest["history_freeze_receipts"][0]
        with closing(sqlite3.connect(self.old.db)) as conn:
            conn.execute("CREATE TABLE unknown_extension(extension_id TEXT PRIMARY KEY, exact_payload BLOB)")
            conn.execute("INSERT INTO unknown_extension VALUES('one',?)", (b"unknown-table-only-change",))
            conn.commit()
        self.add_history()
        self.assertEqual(len(self.fixture.manifest["history_preservation_passes"]), 1)
        self.assertEqual(len(self.fixture.manifest["history_freeze_receipts"]), 2)
        self.assertTrue(self.fixture.verify().complete)
        path = Path(first["databases"]["knowledge"]["path"])
        original = path.read_bytes()
        path.unlink()
        self.assertIn("legacy_history_database_backup_missing", self.codes())
        path.write_bytes(original)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE tampered_unknown(payload TEXT)")
            conn.execute("INSERT INTO tampered_unknown VALUES('unbound unknown-table bytes')")
            conn.commit()
        self.assertIn("legacy_history_database_backup_changed", self.codes())

    def test_rehashed_backup_still_requires_exact_known_rows(self):
        receipt = self.fixture.manifest["history_freeze_receipts"][0]["databases"]["knowledge"]
        with closing(sqlite3.connect(receipt["path"])) as conn:
            conn.execute("UPDATE topics SET summary='wrong backup, valid SQLite' WHERE topic_id=1")
            conn.commit()
        receipt["sha256"] = hashlib.sha256(Path(receipt["path"]).read_bytes()).hexdigest()
        self.assertIn("legacy_history_database_rows_mismatch", self.codes())

    def test_every_expected_freeze_and_regular_file_backup_remains_required(self):
        declaration = self.fixture.manifest["history_preservation_passes"][0]
        frozen = declaration["freeze_receipts"].pop()
        self.assertIn("legacy_history_freeze_set_mismatch", self.codes())
        self.assertIn("legacy_history_database_backup_manifest_missing", self.codes())
        declaration["freeze_receipts"].append(frozen)
        receipt = frozen["databases"]["knowledge"]
        path = Path(receipt["path"])
        destination = path.with_name("actual-database.sqlite")
        path.rename(destination)
        path.symlink_to(destination)
        self.assertIn("legacy_history_database_backup_invalid", self.codes())

    def test_policy_mismatch_and_semantic_legacy_contamination_are_rejected(self):
        with self.fixture.store.transaction() as conn:
            conn.execute("UPDATE generation_manifest SET metadata_json='{}'")
        self.assertIn("legacy_history_policy_mismatch", self.codes())
        with self.fixture.store.transaction() as conn:
            conn.execute("UPDATE generation_manifest SET metadata_json=?", (json.dumps({"history_policy": SOURCE_FIRST_POLICY}),))
        self.fixture.store.capture_batch("forbidden-legacy", [], source_namespace="legacy:source", chat_id="old",
            inventory_digest="old", inventory_revision=1, input_cursors={}, output_cursors={},
            source_eof=True, purpose="legacy_import")
        self.assertIn("legacy_history_semantic_contamination", self.codes())


if __name__ == "__main__":
    unittest.main()
