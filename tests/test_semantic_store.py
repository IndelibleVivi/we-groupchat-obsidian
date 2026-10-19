import copy
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid
from pathlib import Path

from core.semantic.contracts import SemanticStoreError, digest, dumps
from core.semantic.jobs import JobStore
from core.semantic.engine import CapturedSource, prepare_spans, validate_extraction
from core.semantic.attribution import EXTRACTION_PROMPT_VERSION, captured_source_metadata
from core.semantic.store import (SemanticStore, CAPTURE_CONTEXT_VERSION, CAPTURE_CONTEXT_MAX_BYTES,
                                 CAPTURE_CONTEXT_MAX_PACKETS, context_source_tail)
from core.url_safety import redact_urls_with_offsets


class SemanticStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 100.0
        self.store = SemanticStore(Path(self.temp.name).resolve() / "knowledge.sqlite", "test-gen", now_func=lambda: self.now)
        self.jobs = JobStore(self.store)
        self.number = 0

    def tearDown(self):
        self.temp.cleanup()

    def test_connection_errors_do_not_mislabel_operational_failures_as_corruption(self):
        cases = (
            (sqlite3.SQLITE_BUSY, "semantic_database_busy"),
            (sqlite3.SQLITE_LOCKED, "semantic_database_busy"),
            (sqlite3.SQLITE_FULL, "semantic_storage_full"),
            (sqlite3.SQLITE_IOERR | (2 << 8), "semantic_database_io_error"),
            (sqlite3.SQLITE_CORRUPT, "semantic_database_invalid"),
            (sqlite3.SQLITE_NOTADB, "semantic_database_invalid"),
            (None, "semantic_database_unavailable"),
        )
        for result, expected in cases:
            with self.subTest(result=result):
                failure = sqlite3.DatabaseError("private path or content must not surface")
                if result is not None:
                    failure.sqlite_errorcode = result
                connection = Mock()
                connection.execute.side_effect = failure
                with patch("core.semantic.store.sqlite3.connect", return_value=connection):
                    with self.assertRaises(SemanticStoreError) as raised:
                        self.store.connect()
                self.assertEqual(raised.exception.code, expected)
                self.assertIs(raised.exception.__cause__, failure)
                self.assertNotIn("private", str(raised.exception))
                connection.close.assert_called_once_with()

    def test_existing_job_ledger_gets_bounded_batch_lookup_without_changing_evidence(self):
        target_state = {"identity": "frozen synthetic context"}
        versions = {"prompt": "semantic-proposal-v1", "route": "original-synthetic-route"}
        unknown = self.jobs.ensure("propose", {"batch_id": "target", "state": target_state}, versions)
        self.jobs.claim(unknown["job_id"], "original-worker")
        self.jobs.fail(unknown["job_id"], "original-worker", "synthetic_unknown", unknown=True)
        accepted = self.jobs.ensure("propose", {"batch_id": "accepted", "state": {}}, versions)
        self.jobs.claim(accepted["job_id"], "accepted-worker")
        self.jobs.accept(accepted["job_id"], "accepted-worker", {"synthetic": "accepted evidence"},
                         validate=lambda artifact: artifact)
        with self.store.transaction() as db:
            # Model the pre-index schema in this disposable fixture only.
            db.execute("DROP INDEX IF EXISTS semantic_jobs_batch")
            fillers = []
            for index in range(1000):
                inputs = {"batch_id": "other-" + str(index), "state": {"detail": "合成背景" * 500}}
                key = self.jobs._key("propose", inputs, versions)
                fillers.append(("job_" + key, key, "propose", dumps(inputs), dumps(versions), self.now, self.now))
            db.executemany("INSERT INTO jobs(job_id,job_key,kind,inputs_json,versions_json,state,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,'pending',?,?)", fillers)
            tables = ("generation_manifest", "jobs", "job_attempts", "accepted_job_artifacts")
            before = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                      for table in tables}
        reopened = SemanticStore(self.store.path, self.store.generation_id, now_func=lambda: self.now)
        with closing(reopened.connect()) as db:
            after = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                     for table in tables}
            self.assertEqual(after, before)
            # A table/JSON scan exceeds this VM budget; the indexed batch seek
            # still returns the original unknown row, not an empty success.
            steps = 0
            def bounded():
                nonlocal steps
                steps += 1
                return int(steps > 20)
            db.set_progress_handler(bounded, 100)
            try:
                rows = db.execute("SELECT job_id,state FROM jobs WHERE kind='propose' "
                    "AND json_extract(inputs_json,'$.batch_id')=? AND json_extract(inputs_json,'$.state')=?",
                    ("target", dumps(target_state))).fetchall()
            finally:
                db.set_progress_handler(None, 0)
            self.assertEqual([tuple(row) for row in rows], [(unknown["job_id"], "unknown")])

    def test_existing_child_ledger_gets_bounded_parent_lookup_without_changing_evidence(self):
        versions = {"prompt": "synthetic-frozen-page"}
        target = self.jobs.ensure("extract_page", {"parent_job_id": "wanted-parent", "page": 0}, versions)
        self.jobs.claim(target["job_id"], "original-worker")
        self.jobs.fail(target["job_id"], "original-worker", "synthetic_unknown", unknown=True)
        with self.store.transaction() as db:
            db.execute("DROP INDEX IF EXISTS semantic_jobs_parent")
            fillers = []
            for index in range(1000):
                inputs = {"parent_job_id": "unrelated-" + str(index), "detail": "合成背景" * 500}
                key = self.jobs._key("extract_page", inputs, versions)
                fillers.append(("job_" + key, key, "extract_page", dumps(inputs), dumps(versions), self.now, self.now))
            db.executemany("INSERT INTO jobs(job_id,job_key,kind,inputs_json,versions_json,state,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,'pending',?,?)", fillers)
            tables = ("generation_manifest", "jobs", "job_attempts", "accepted_job_artifacts")
            before = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                      for table in tables}
        reopened = SemanticStore(self.store.path, self.store.generation_id, now_func=lambda: self.now)
        with closing(reopened.connect()) as db:
            after = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                     for table in tables}
            self.assertEqual(after, before)
            steps = 0
            def bounded():
                nonlocal steps
                steps += 1
                return int(steps > 20)
            db.set_progress_handler(bounded, 100)
            try:
                rows = db.execute("SELECT j.job_id,j.state,a.job_id AS accepted FROM jobs j "
                    "LEFT JOIN accepted_job_artifacts a USING(job_id) "
                    "WHERE j.kind IN ('extract_page','extract_coverage') "
                    "AND json_extract(j.inputs_json,'$.parent_job_id')=?", ("wanted-parent",)).fetchall()
            finally:
                db.set_progress_handler(None, 0)
            self.assertEqual([tuple(row) for row in rows], [(target["job_id"], "unknown", None)])

    def accept(self, artifact, kind="test", inputs=None, versions=None):
        self.number += 1
        versions = versions or ({"prompt": EXTRACTION_PROMPT_VERSION, "test": self.number} if kind == "extract" else {"v": 1, "test": self.number})
        job = self.jobs.ensure(kind, inputs or {"n": self.number}, versions)
        self.jobs.claim(job["job_id"], "worker")
        expected = json.loads(dumps(artifact))
        def validate(value):
            self.assertEqual(value, expected)
            return value
        self.jobs.accept(job["job_id"], "worker", artifact, validate=validate)
        return job["job_id"]

    def capture(self, body, *, source_id="source", batch_id=None, core=True, **extras):
        self.number += 1
        return self.store.capture_batch(batch_id or f"batch-{self.number}", [
            {"source_id": source_id, "logical_shard_id": "shard", "physical_generation": "physical",
             "source_cursor_token": "[1,1]", "source_sent_at": 1, "text": body, "core": core, **extras}],
            source_namespace="namespace", chat_id="chat", inventory_digest="d" * 64,
            inventory_revision=1, input_cursors={}, output_cursors={}, source_eof=True)

    def extraction_job(self, batch, observations, dispositions=None):
        captured = self.store.get_batch(batch["batch_id"])
        members = [asdict(CapturedSource(r["source_version_id"], r["text"], r["core"], r["source_sent_at"],
                   **captured_source_metadata(captured["source_namespace"], captured["chat_id"], r["payload"])))
                   for r in captured["members"]]
        return self.accept({"observations": observations, "dispositions": dispositions or {}}, "extract",
                           {"batch_id": batch["batch_id"], "members": members})

    def context_fixture(self, *, prior_text="前序的完整经历", prefix="context"):
        previous = self.capture(prior_text, source_id=prefix + "-before", batch_id=prefix + "-before",
            payload={"raw_sender_id": "synthetic-speaker", "sender": "阿岚", "subject_anchor": "synthetic-subject"},
            resources=[{"resource_id": prefix + "-resource", "occurrence_id": "synthetic-occurrence", "purpose": "attachment",
                        "status": "pending", "location": None, "metadata": {"name": "synthetic.png"}}])
        current = self.capture("当前接续的内容", source_id=prefix + "-now", batch_id=prefix + "-now")
        binding = self.store.preceding_context(current["batch_id"])
        current_source = self.store.get_batch(current["batch_id"])["members"][0]
        source = CapturedSource(current_source["source_version_id"], current_source["text"], True, current_source["source_sent_at"],
            **captured_source_metadata("namespace", "chat", current_source["payload"]))
        spans = [copy.deepcopy(binding["spans"][-1]), asdict(prepare_spans([source], namespace=self.store.generation_id)[0])]
        observation = {"observation_id": prefix + "-observation", "title": "接续的场景", "scene_kind": "life",
            "spans": spans, "units": [{"unit_id": prefix + "-unit", "kind": "scene", "text": "保留前文和当前的具体内容",
                                       "span_ids": [span["span_id"] for span in spans]}], "topics": [], "claims": []}
        inputs = {"batch_id": current["batch_id"], "members": [asdict(source)], "preceding_context": binding}
        versions = {"prompt": EXTRACTION_PROMPT_VERSION, "capture_context": CAPTURE_CONTEXT_VERSION}
        return previous, current, observation, inputs, versions

    def test_context_commit_checks_exact_tail_and_retains_resources_without_core_ownership(self):
        previous, current, observation, inputs, versions = self.context_fixture()
        job = self.accept({"observations": [observation], "dispositions": {}}, "extract", inputs, versions)
        self.store.commit_extraction(job, current["batch_id"], [observation])
        with closing(self.store.connect()) as db:
            self.assertEqual([row[0] for row in db.execute("SELECT source_version_id FROM source_core_jobs")], current["member_version_ids"])
            self.assertEqual([row[0] for row in db.execute("SELECT source_version_id FROM source_dispositions")], current["member_version_ids"])
            self.assertEqual([row[0] for row in db.execute("SELECT resource_id FROM resource_refs")], ["context-resource"])
            prior_span = observation["spans"][0]
            stored = db.execute("SELECT * FROM source_spans WHERE span_id=?", (prior_span["span_id"],)).fetchone()
            expected = context_source_tail(db, previous["member_version_ids"][0], self.store.generation_id)[2][prior_span["span_id"]]
            self.assertEqual(tuple(stored), expected)

    def test_context_binding_cannot_change_generation_receipt_order_or_selection(self):
        mutations = {
            "generation": lambda binding: binding.update(generation_id="foreign-generation"),
            "batch": lambda binding: binding.update(batch_id="foreign-batch"),
            "receipt": lambda binding: binding.update(receipt_id="foreign-receipt"),
            "source": lambda binding: binding["source_ids"].append("unbound-source"),
            "span": lambda binding: binding["spans"][0].update(excerpt="changed source words"),
            "continuity": lambda binding: binding.update(cursor_continuity="exact"),
            "reason": lambda binding: binding.update(unavailable_reason="invented"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                _, current, observation, inputs, versions = self.context_fixture(prefix=name)
                mutate(inputs["preceding_context"])
                job = self.accept({"observations": [observation], "dispositions": {}}, "extract", inputs, versions)
                with self.assertRaisesRegex(SemanticStoreError, "extraction_context_invalid"):
                    self.store.commit_extraction(job, current["batch_id"], [observation])
                with closing(self.store.connect()) as db:
                    self.assertIsNone(db.execute("SELECT 1 FROM source_core_jobs WHERE job_id=?", (job,)).fetchone())

    def test_context_cannot_admit_other_source_or_unselected_span_or_own_disposition(self):
        for invalid in ("foreign-source", "unselected-span", "changed-excerpt", "context-only", "context-disposition"):
            with self.subTest(invalid=invalid):
                previous, current, observation, inputs, versions = self.context_fixture(prior_text="旧材料" * 40000, prefix=invalid)
                dispositions = {}
                error = "observation_invalid_context_span"
                if invalid == "foreign-source":
                    observation["spans"][0]["source_id"] = "not-in-this-context"
                    error = "observation_unknown_source"
                elif invalid == "unselected-span":
                    observation["spans"][0] = asdict(prepare_spans([CapturedSource(previous["member_version_ids"][0], "旧材料" * 40000)],
                        namespace=self.store.generation_id)[0])
                elif invalid == "changed-excerpt":
                    observation["spans"][0]["excerpt"] = "程序不能代填或者接受改写后的前文"
                elif invalid == "context-only":
                    observation["spans"] = observation["spans"][:1]
                    error = "observation_core_evidence_required"
                else:
                    dispositions = {previous["member_version_ids"][0]: {"status": "noise", "reason": "not this capture's work"}}
                    error = "extraction_disposition_source_mismatch"
                job = self.accept({"observations": [observation], "dispositions": dispositions}, "extract", inputs, versions)
                with self.assertRaisesRegex(SemanticStoreError, error):
                    self.store.commit_extraction(job, current["batch_id"], [observation], dispositions)

    def test_streamed_tail_has_exact_canonical_span_ids_offsets_and_url_mappings(self):
        # Cross every possible scheme boundary in an 8192-byte raw chunk and
        # put another credential URL across the retained tail window boundary.
        for offset in range(9):
            with self.subTest(offset=offset):
                text = ("a" * (8192 - offset) + "https://example.test/a?token=synthetic-secret&ok=1 "
                    + "普通原文" * 30000 + " https://example.test/z#token=second-secret&ok=2 " + "尾" * 2007)
                captured = self.capture(text, source_id="stream-" + str(offset))
                version = captured["member_version_ids"][0]
                full = prepare_spans([CapturedSource(version, text)], namespace=self.store.generation_id)
                visible, ranges = redact_urls_with_offsets(text)
                with closing(self.store.connect()) as db:
                    source, actual, stored = context_source_tail(db, version, self.store.generation_id)
                    class ByteProjectionOnly:
                        # Exercise the Python 3.10 fallback, which must project
                        # only a fixed byte chunk rather than fetch the TEXT.
                        def execute(_, query, args=()):
                            if query.startswith("SELECT substr("):
                                self.assertIn(",8192)", query)
                            return db.execute(query, args)
                    fallback = context_source_tail(ByteProjectionOnly(), version, self.store.generation_id)
                self.assertEqual(source["text"], "")
                self.assertEqual(actual, [asdict(span) for span in full[-CAPTURE_CONTEXT_MAX_PACKETS:]])
                self.assertEqual((source, actual, stored), fallback)
                for span in actual:
                    a, b = span["start"], span["end"]
                    mapping = [{"raw_start": i + max(0, a-x) if tag == "equal" else i,
                                "raw_end": j - max(0, y-b) if tag == "equal" else j,
                                "visible_start": max(a, x), "visible_end": min(b, y)}
                               for tag, i, j, x, y in ranges if x < b and y > a]
                    self.assertEqual(stored[span["span_id"]], (span["span_id"], version, a, b,
                        mapping[0]["raw_start"], mapping[-1]["raw_end"], visible[a:b], digest(visible[a:b]), dumps(mapping)))

    def test_streamed_tail_keeps_whole_url_mapping_across_tail_window_and_span_boundaries(self):
        url = "https://example.test/" + "p" * 3000 + "?token=synthetic-secret&ok=1"
        displayed_url, _ = redact_urls_with_offsets(url)
        # Visible URL [99980,103000+) crosses both the 48k suffix boundary
        # at 99999 and the first retained canonical span at 100000.
        text = "x" * 99980 + url + " " + "y" * (147999 - 99980 - len(displayed_url) - 1)
        captured = self.capture(text, source_id="window-url")
        vid = captured["member_version_ids"][0]
        full = prepare_spans([CapturedSource(vid, text)], namespace=self.store.generation_id)
        visible, ranges = redact_urls_with_offsets(text)
        with closing(self.store.connect()) as db:
            _, spans, stored = context_source_tail(db, vid, self.store.generation_id)
        self.assertEqual(len(visible), 147999)
        self.assertEqual(spans, [asdict(span) for span in full[-24:]])
        first = spans[0]
        self.assertEqual(first["start"], 100000)
        mapping = json.loads(stored[first["span_id"]][-1])
        url_range = next(entry for entry in ranges if entry[0] == "replace")
        self.assertEqual(mapping[0], {"raw_start": url_range[1], "raw_end": url_range[2],
            "visible_start": first["start"], "visible_end": first["end"]})

    def test_cursor_continuity_is_exact_only_for_nonempty_matching_advanced_vectors(self):
        for name, before, after, incoming, expected in (
                ("exact", {"shard": "1"}, {"shard": "2"}, {"shard": "2"}, "exact"),
                ("mismatch", {"shard": "1"}, {"shard": "2"}, {"shard": "3"}, "unknown"),
                ("unchanged", {"shard": "2"}, {"shard": "2"}, {"shard": "2"}, "unknown"),
                ("empty", {}, {}, {}, "unknown")):
            records = [{"source_id": "continuity-" + name, "text": "具体前文", "logical_shard_id": "shard",
                "physical_generation": "physical", "source_cursor_token": "1", "source_sent_at": 1}]
            for batch, inputs, outputs in (("before", before, after), ("after", incoming, incoming)):
                self.store.capture_batch("continuity-" + name + batch, records if batch == "before" else [],
                    source_namespace=name, chat_id="chat", inventory_digest="synthetic", inventory_revision=1,
                    input_cursors=inputs, output_cursors=outputs, source_eof=True)
            self.assertEqual(self.store.preceding_context("continuity-" + name + "after")["cursor_continuity"], expected)

    def test_context_window_bounds_packets_and_attribution_before_python_payload_reads(self):
        records = [{"source_id": "bounded-" + str(index), "logical_shard_id": "shard", "physical_generation": "physical",
            "source_cursor_token": str(index), "source_sent_at": index, "text": "合成原文" * 300,
            "payload": {"sender": "展示名" * 200, "unrelated": "large unused payload" * 20000}}
            for index in range(30)]
        previous = self.store.capture_batch("bounded-before", records, source_namespace="namespace", chat_id="chat",
            inventory_digest="synthetic", inventory_revision=1, input_cursors={}, output_cursors={}, source_eof=True)
        current = self.capture("当前", batch_id="bounded-now", source_id="bounded-now")
        original, queries = self.store.connect, []
        def traced():
            db = original()
            db.set_trace_callback(queries.append)
            return db
        with patch.object(self.store, "connect", side_effect=traced):
            binding = self.store.preceding_context(current["batch_id"])
            groups = self.store.extraction_context_sources({"inputs": {"batch_id": current["batch_id"], "preceding_context": binding},
                "versions": {"capture_context": CAPTURE_CONTEXT_VERSION}})
        self.assertLess(len(binding["source_ids"]), 30)
        self.assertEqual(binding["source_ids"], previous["member_version_ids"][-len(groups):])
        self.assertLessEqual(len(binding["spans"]), CAPTURE_CONTEXT_MAX_PACKETS)
        wire_cost = 2 + sum(len(dumps({k: v for k, v in source.items() if k != "spans"}).encode()) + 1
                            + sum(len(dumps(span).encode()) + 1 for span in source["spans"]) for source in groups)
        self.assertLessEqual(wire_cost, CAPTURE_CONTEXT_MAX_BYTES)
        self.assertTrue(all(source["text"] == "" and not source["core"] for source in groups))
        self.assertFalse(any("SELECT r.*" in query or "SELECT * FROM source_records" in query for query in queries))
        self.assertTrue(any("CASE WHEN length(CAST(metadata AS BLOB))<=8192" in query for query in queries))

    def test_context_metadata_or_indivisible_url_over_budget_is_explicit_and_never_partial(self):
        for text, payload, reason in (("普通原文", {"sender": "x" * 8193}, "extraction_context_metadata_limit"),
                                      ("https://example.test/?token=" + "s" * 65537, {}, "extraction_context_token_limit")):
            previous = self.capture(text, source_id=reason + "-before", payload=payload)
            current = self.capture("当前原文", source_id=reason + "-now")
            binding = self.store.preceding_context(current["batch_id"])
            self.assertEqual(binding["batch_id"], previous["batch_id"])
            self.assertEqual(binding["unavailable_reason"], reason)
            self.assertEqual((binding["source_ids"], binding["spans"]), ([], []))

    def test_preceding_capture_lookup_uses_scoped_reverse_rowid_seek(self):
        previous = self.capture("同域前序", batch_id="index-before")
        with self.store.transaction() as db:
            original = dict(db.execute("SELECT * FROM capture_batches WHERE batch_id=?", (previous["batch_id"],)).fetchone())
            columns = list(original)
            for index in range(1000):
                row = dict(original, batch_id="unrelated-" + str(index), receipt_id="unrelated-receipt-" + str(index),
                           source_namespace="unrelated-" + str(index), receipt_json=dumps({"wide": "synthetic" * 1000}))
                db.execute("INSERT INTO capture_batches(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")",
                           [row[key] for key in columns])
        current = self.capture("同域当前", batch_id="index-now", source_id="index-now")
        original_connect, queries = self.store.connect, []
        def bounded():
            db = original_connect()
            db.set_trace_callback(queries.append)
            calls = 0
            def progress():
                nonlocal calls
                calls += 1
                return int(calls > 20)
            db.set_progress_handler(progress, 100)
            return db
        with patch.object(self.store, "connect", side_effect=bounded):
            self.assertEqual(self.store.preceding_context(current["batch_id"])["batch_id"], previous["batch_id"])
        query = next(query for query in queries if query.startswith("SELECT p.batch_id"))
        with closing(original_connect()) as db:
            plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query))
        self.assertIn("USING INDEX capture_context_order (source_namespace=? AND chat_id=? AND rowid<?)", plan)
        self.assertNotIn("USE TEMP B-TREE", plan)

    def observation(self, body="猫守着箱子", *, source_id="source", core=True, claims=None, **extras):
        capture = self.capture(body, source_id=source_id, core=core, **extras)
        vid = capture["member_version_ids"][0]
        span = asdict(prepare_spans([CapturedSource(vid, body, core)], namespace=self.store.generation_id)[0])
        obs = {"observation_id": "obs-" + vid, "title": body, "scene_kind": "life",
               "spans": [span],
               "units": [{"unit_id": "unit-" + vid, "kind": "scene", "text": body,
                          "span_ids": [span["span_id"]]}], "claims": [], "topics": ["日常"]}
        obs["claims"] = [{"claim_id": claim, "subject_id": None, "case_id": None,
                          "observed_at": 1, "predicates": {"value": body}, "span_ids": [span["span_id"]]} for claim in claims or []]
        job_id = self.extraction_job(capture, [obs])
        self.store.commit_extraction(job_id, capture["batch_id"], [obs])
        return capture, obs

    def action(self, actions, *, items=None, views=None):
        read_set = {"store_revision": self.store.read_revision(), "items": items or {}, "views": views or {}}
        observation_ids = {oid for action in actions for oid in action.get("observation_ids", [])}
        observation_ids.update(oid for action in actions for output in action.get("outputs", []) for oid in output["observation_ids"])
        with closing(self.store.connect()) as conn:
            for action in actions:
                for relation in action.get("claim_relations", []):
                    observation_ids.update(row[0] for row in conn.execute("SELECT observation_id FROM observation_claims WHERE claim_id=?", (relation["from_claim_id"],)))
        state = {"read_set": read_set, "items": {iid: self.store.get_item(iid) for iid in items or {}},
                 "observations": [self.store.get_observation(oid) for oid in sorted(observation_ids)]}
        job_id = self.accept({"actions": actions, "read_set": read_set}, "propose", {"state": state})
        return self.store.apply_proposal(job_id, actions, read_set=read_set)

    @staticmethod
    def create(iid, oid):
        return {"action_key": "create-" + iid, "type": "CREATE_ITEM", "item_id": iid,
                "kind": "story", "title": "猫守着箱子", "identity_scope": {"event": iid},
                "observation_ids": [oid]}

    def test_source_transitions_preserve_a_b_a_but_adopt_current(self):
        values = [self.capture(s)["member_version_ids"][0] for s in ("A", "A", "B", "A")]
        self.assertEqual(values[0], values[1])
        self.assertEqual(len(set(values)), 3)
        final = self.store.get_source(values[-1])
        self.assertEqual(final["revision"], 3)
        self.assertEqual(final["supersedes_source_version_id"], values[-2])

    def test_batch_id_reuse_cannot_replace_members(self):
        first = self.capture("A", batch_id="stable")
        self.assertEqual(first, self.capture("A", batch_id="stable"))
        with self.assertRaisesRegex(SemanticStoreError, "capture_batch_identity_conflict"):
            self.capture("B", batch_id="stable")

    def test_context_only_observation_is_rejected_without_partial_write(self):
        with self.assertRaisesRegex(SemanticStoreError, "observation_core_evidence_required"):
            self.observation(core=False)
        self.assertFalse(any(d["owner_type"] == "observation" for d in self.store.documents()))

    def test_accepted_artifact_precedes_extraction_and_replay_is_exact(self):
        batch, obs = self.observation()
        with closing(self.store.connect()) as conn:
            row = conn.execute("SELECT job_id FROM extraction_receipts").fetchone()
        a = self.store.commit_extraction(row[0], batch["batch_id"], [obs])
        b = self.store.commit_extraction(row[0], batch["batch_id"], [obs])
        self.assertEqual(a, b)
        changed = copy.deepcopy(obs)
        changed["title"] = "不存在的话"
        with self.assertRaisesRegex(SemanticStoreError, "extraction_artifact_mismatch"):
            self.store.commit_extraction(row[0], batch["batch_id"], [changed])

    def test_multi_actions_are_atomic_and_stale_context_rejected(self):
        _, obs = self.observation()
        self.action([self.create("one", obs["observation_id"])])
        actions = [self.create("two", obs["observation_id"]),
                   {"action_key": "bad", "type": "MERGE_INTO_ITEM", "item_id": "missing",
                    "observation_ids": [obs["observation_id"]], "modes": ["continue"], "semantic_effect": "continuation"}]
        with self.assertRaisesRegex(SemanticStoreError, "semantic_item_missing"):
            self.action(actions)
        self.assertIsNone(self.store.get_item("two"))
        action = self.create("two", obs["observation_id"])
        job = self.accept({"actions": [action]})
        with self.assertRaisesRegex(SemanticStoreError, "semantic_stale_read_set"):
            self.store.apply_proposal(job, [action], read_set={"store_revision": 0, "items": {}, "views": {}})

    def test_noop_merge_does_not_make_item_fresh(self):
        _, obs = self.observation()
        self.action([self.create("one", obs["observation_id"])])
        self.now = 200
        self.action([{"action_key": "replay", "type": "MERGE_INTO_ITEM", "item_id": "one",
                      "observation_ids": [obs["observation_id"]], "modes": ["add_evidence"], "semantic_effect": "none"}], items={"one": 1})
        item = self.store.get_item("one")
        self.assertEqual(item["content_revision"], 1)
        self.assertEqual(item["semantic_updated_at"], 100)

    def test_relation_and_multiview_membership_are_real_and_user_pins_win(self):
        _, obs = self.observation()
        self.store.create_view("view", "生活", "完整经历")
        actions = [self.create("one", obs["observation_id"]), self.create("two", obs["observation_id"]),
                   {"action_key": "relation", "type": "RELATE_ITEMS", "from_item_id": "one", "to_item_id": "two",
                    "relation": "changes_over_time", "direction": "directed", "reason": "后续", "evidence_ids": [obs["observation_id"]]},
                   {"action_key": "pin", "type": "ADD_MEMBERSHIP", "item_id": "one", "view_id": "view",
                    "origin": "user", "placement": "开头", "reason": "选择", "pinned": True}]
        self.action(actions, views={"view": {"definition_revision": 1, "override_revision": 0}})
        self.action([{"action_key": "move", "type": "ADD_MEMBERSHIP", "item_id": "one", "view_id": "view",
                      "origin": "model", "placement": "结尾", "reason": "建议"}], items={"one": 1},
                    views={"view": {"definition_revision": 1, "override_revision": 1}})
        with closing(self.store.connect()) as conn:
            placement = conn.execute("SELECT placement FROM view_memberships").fetchone()[0]
        self.assertEqual(placement, "开头")
        self.assertEqual(len(self.store.list_relations()), 1)

    def test_merge_split_keep_original_observations_and_hub(self):
        _, a = self.observation("周五纸箱", source_id="first")
        _, b = self.observation("周一窗边", source_id="second")
        self.action([self.create("one", a["observation_id"]), self.create("two", b["observation_id"])])
        self.action([{"action_key": "merge", "type": "MERGE_ITEMS", "item_ids": ["one", "two"],
                      "target_item_id": "one", "reason": "同一次搬家"}], items={"one": 1, "two": 1})
        self.assertEqual(self.store.get_item("two")["aliases"], ["one"])
        self.action([{"action_key": "split", "type": "SPLIT_ITEM", "item_id": "one", "reason": "两个阶段分开保留",
                      "outputs": [self.create("a", a["observation_id"]), self.create("b", b["observation_id"])]}], items={"one": 2})
        self.assertEqual(set(self.store.get_item("one")["aliases"]), {"a", "b"})
        self.assertIsNotNone(self.store.get_observation(a["observation_id"]))

    def test_processing_and_outbox_ack_require_actual_canonical_work(self):
        batch, obs = self.observation()
        with self.assertRaisesRegex(SemanticStoreError, "semantic_batch_incomplete"):
            self.store.complete_batch(batch["batch_id"])
        self.action([self.create("one", obs["observation_id"])])
        done = self.store.complete_batch(batch["batch_id"])
        self.assertEqual(done, self.store.complete_batch(batch["batch_id"]))
        outbox = self.store.pending_outbox("projection")[0]
        with self.assertRaisesRegex(SemanticStoreError, "semantic_outbox_receipt_mismatch"):
            self.store.ack_outbox("projection", outbox["outbox_id"], "one", 99, "published")
        self.store.ack_outbox("projection", outbox["outbox_id"], "one", 1, "published")
        self.assertEqual(self.store.pending_outbox("projection"), [])

    def read_set(self, *item_ids):
        with closing(self.store.connect()) as conn:
            views = {r["view_id"]: {"definition_revision": r["definition_revision"], "override_revision": r["override_revision"]}
                     for r in conn.execute("SELECT * FROM views")}
        return {"store_revision": self.store.read_revision(),
                "items": {iid: self.store.get_item(iid)["content_revision"] for iid in item_ids}, "views": views}

    def test_native_revision_is_unique_across_nonadjacent_transitions(self):
        first = self.capture("A", native_revision="r1")
        self.capture("B", native_revision="r2")
        with self.assertRaisesRegex(SemanticStoreError, "source_native_revision_reused"):
            self.capture("A", native_revision="r1")
        self.assertEqual(self.store.get_source(first["member_version_ids"][0])["revision"], 1)

    def test_extraction_binds_actual_members_and_dispositions(self):
        batch = self.capture("plain")
        wrong = self.accept({"observations": [], "dispositions": {}}, "extract",
                            {"batch_id": batch["batch_id"], "members": []})
        with self.assertRaisesRegex(SemanticStoreError, "extraction_input_mismatch"):
            self.store.commit_extraction(wrong, batch["batch_id"], [])
        status = {batch["member_version_ids"][0]: {"status": "noise", "reason": "explicit"}}
        job = self.extraction_job(batch, [], status)
        with self.assertRaisesRegex(SemanticStoreError, "extraction_artifact_mismatch"):
            self.store.commit_extraction(job, batch["batch_id"], [], {})
        self.store.commit_extraction(job, batch["batch_id"], [], status)
        receipt = self.store.complete_batch(batch["batch_id"])
        self.assertEqual(receipt, self.store.verify_processing_receipt(receipt))
        self.assertEqual(receipt["proof"]["observation_memberships"], [])

    def test_recapture_adopts_core_owner_without_second_observation(self):
        first, obs = self.observation()
        self.action([self.create("one", obs["observation_id"])])
        original = self.store.complete_batch(first["batch_id"])
        second = self.capture("猫守着箱子")
        job = self.extraction_job(second, [])
        adopted = self.store.commit_extraction(job, second["batch_id"], [])
        self.assertEqual(len(adopted["adopted_core_job_ids"]), 1)
        completed = self.store.complete_batch(second["batch_id"])
        self.assertEqual(completed["proof"]["core_sources"], original["proof"]["core_sources"])
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM observations").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM source_core_jobs").fetchone()[0], 1)

    def test_accepted_v1_subject_identity_retains_its_original_scope(self):
        batch = self.capture("plain", payload={"raw_sender_id": "raw-sender", "sender_id": "fallback"})
        vid = batch["member_version_ids"][0]
        member = asdict(CapturedSource(vid, "plain", True, 1))
        member["subject_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, dumps(["subject", "namespace", "chat", "raw-sender"])))
        status = {vid: {"status": "noise", "reason": "explicit"}}
        job = self.accept({"observations": [], "dispositions": status}, "extract",
                          {"batch_id": batch["batch_id"], "members": [member]}, versions={"prompt": "semantic-extraction-v1"})
        self.store.commit_extraction(job, batch["batch_id"], [], status)
        self.assertEqual(self.store.complete_batch(batch["batch_id"])["batch_id"], batch["batch_id"])

    def test_v2_attribution_is_independently_bound_to_captured_payload(self):
        payload = {"raw_sender_id": "wxid_synthetic_account", "sender": "小林"}
        batch = self.capture("我完成了测试。", payload=payload)
        vid = batch["member_version_ids"][0]
        member = asdict(CapturedSource(vid, "我完成了测试。", True, 1,
                                      **captured_source_metadata("namespace", "chat", payload)))
        status = {vid: {"status": "noise", "reason": "synthetic_explicit_disposition"}}
        for field, value in (("speaker_id", "wxid_synthetic_account"), ("speaker_name", "另一个名字"),
                             ("subject_id", member["speaker_id"])):
            with self.subTest(field=field):
                bad = {**member, field: value}
                job = self.accept({"observations": [], "dispositions": status}, "extract",
                                  {"batch_id": batch["batch_id"], "members": [bad]})
                with self.assertRaisesRegex(SemanticStoreError, "extraction_input_mismatch"):
                    self.store.commit_extraction(job, batch["batch_id"], [], status)
        missing = {key: value for key, value in member.items() if key != "speaker_id"}
        job = self.accept({"observations": [], "dispositions": status}, "extract",
                          {"batch_id": batch["batch_id"], "members": [missing]})
        with self.assertRaisesRegex(SemanticStoreError, "extraction_input_mismatch"):
            self.store.commit_extraction(job, batch["batch_id"], [], status)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM extraction_receipts").fetchone()[0], 0)
        job = self.extraction_job(batch, [], status)
        self.store.commit_extraction(job, batch["batch_id"], [], status)
        self.assertEqual(self.store.get_source(vid)["payload"], payload)

    def test_new_extraction_cannot_use_unversioned_fixture_to_skip_attribution(self):
        batch = self.capture("plain")
        vid = batch["member_version_ids"][0]
        status = {vid: {"status": "noise", "reason": "synthetic_explicit_disposition"}}
        job = self.accept({"observations": [], "dispositions": status}, "extract",
                          {"batch_id": batch["batch_id"], "members": [asdict(CapturedSource(vid, "plain", True, 1))]},
                          versions={"fixture": "unversioned"})
        with self.assertRaisesRegex(SemanticStoreError, "extraction_prompt_version_conflict"):
            self.store.commit_extraction(job, batch["batch_id"], [], status)

    def test_claim_subject_and_case_must_belong_to_its_cited_sources(self):
        records = [{"source_id": "speaker-" + str(i), "logical_shard_id": "shard", "physical_generation": "physical",
                    "source_cursor_token": str(i), "source_sent_at": i + 1, "text": "我完成了测试。",
                    "payload": {"raw_sender_id": "wxid_synthetic_" + str(i), "sender": "同名", "case_anchor": "case-" + str(i)}}
                   for i in range(2)]
        batch = self.store.capture_batch("two-speakers", records, source_namespace="namespace", chat_id="chat",
            inventory_digest="d" * 64, inventory_revision=1, input_cursors={}, output_cursors={}, source_eof=True)
        sources = [CapturedSource(row["source_version_id"], row["text"], True, row["source_sent_at"],
                   **captured_source_metadata("namespace", "chat", row["payload"]))
                   for row in self.store.get_batch(batch["batch_id"])["members"]]
        span = prepare_spans(sources, namespace=self.store.generation_id)[0]
        raw = {"observations": [{"title": "自述", "scene_kind": "life",
               "spans": [{"source_id": sources[0].source_id, "span_id": span.span_id}],
               "units": [{"kind": "scene", "text": sources[0].text, "span_indices": [0]}], "topics": [],
               "claims": [{"subject_id": sources[0].speaker_id, "case_id": sources[0].case_id,
                           "condition": None, "channel": None, "version": None, "phase": None,
                           "observed_at": 1, "predicates": {"completed": True}, "span_indices": [0]}]}]}
        valid = [o.as_dict() for o in validate_extraction(raw, sources, namespace=self.store.generation_id)]
        disposition = {sources[1].source_id: {"status": "noise", "reason": "synthetic_explicit_disposition"}}
        for field, value in (("subject_id", sources[1].speaker_id), ("case_id", sources[1].case_id)):
            with self.subTest(field=field):
                forged = copy.deepcopy(valid)
                forged[0]["claims"][0][field] = value
                job = self.extraction_job(batch, forged, disposition)
                with self.assertRaisesRegex(SemanticStoreError, "claim_unprovided_anchor"):
                    self.store.commit_extraction(job, batch["batch_id"], forged, disposition)
        job = self.extraction_job(batch, valid, disposition)
        self.store.commit_extraction(job, batch["batch_id"], valid, disposition)
        self.assertEqual(self.store.list_claims()[0]["subject_id"], sources[0].speaker_id)

    def test_partial_source_observation_does_not_override_unclassified_remainder(self):
        batch = self.capture("first page and unresolved remainder")
        vid = batch["member_version_ids"][0]
        span = asdict(prepare_spans([CapturedSource(vid, "first page and unresolved remainder", True)],
                                   namespace=self.store.generation_id)[0])
        obs = {"observation_id": "partial", "title": "first page", "scene_kind": "life",
               "spans": [span], "units": [{"unit_id": "partial-unit", "kind": "scene", "text": "first page",
                                           "span_ids": [span["span_id"]]}], "claims": [], "topics": []}
        status = {vid: {"status": "unclassified", "reason": "unresolved_page"}}
        job = self.extraction_job(batch, [obs], status)
        receipt = self.store.commit_extraction(job, batch["batch_id"], [obs], status)
        self.assertEqual(receipt["dispositions"], {})
        self.action([self.create("partial-item", "partial")])
        with closing(self.store.connect()) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM source_dispositions WHERE source_version_id=?", (vid,)).fetchone())
        with self.assertRaisesRegex(SemanticStoreError, "semantic_batch_incomplete"):
            self.store.complete_batch(batch["batch_id"])

    def test_program_owned_span_rejects_plausible_substring(self):
        batch, observation = self.observation()
        changed = copy.deepcopy(observation)
        changed["spans"][0]["start"] = 1
        changed["spans"][0]["excerpt"] = changed["spans"][0]["excerpt"][1:]
        # Use a new raw version so the core-owner guard does not mask span validation.
        source = self.capture("猫守着箱子!", source_id="new-source")
        changed["spans"][0]["source_id"] = source["member_version_ids"][0]
        job = self.extraction_job(source, [changed])
        with self.assertRaisesRegex(SemanticStoreError, "observation_invalid_span"):
            self.store.commit_extraction(job, source["batch_id"], [changed])
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM observations").fetchone()[0], 1)

    def test_quote_contiguous_ranges_allow_boundary_but_reject_gap_and_other_source(self):
        for mode in ("adjacent", "gap", "other_source"):
            with self.subTest(mode=mode):
                bodies = ["甲" * 2000 + "乙" * 2000 + "丙" * 2000]
                if mode == "other_source":
                    bodies = ["甲" * 2000, "乙" * 2000]
                batch = self.store.capture_batch("quote-" + mode, [
                    {"source_id": mode + str(i), "logical_shard_id": "shard", "physical_generation": "physical",
                     "source_cursor_token": "[1,1]", "source_sent_at": 1, "text": body, "core": True}
                    for i, body in enumerate(bodies)], source_namespace="namespace", chat_id="chat",
                    inventory_digest="d" * 64, inventory_revision=1, input_cursors={}, output_cursors={}, source_eof=True)
                sources = [CapturedSource(vid, body, True, 1) for vid, body in zip(batch["member_version_ids"], bodies)]
                spans = [asdict(span) for span in prepare_spans(sources, namespace=self.store.generation_id)]
                spans = [spans[0], spans[2]] if mode == "gap" else spans[:2]
                body = "甲丙" if mode == "gap" else "甲乙"
                obs = {"observation_id": mode, "title": "引用", "scene_kind": "quote", "spans": spans,
                       "units": [{"unit_id": mode, "kind": "quote", "text": body,
                                  "span_ids": [span["span_id"] for span in spans]}], "claims": [], "topics": []}
                job = self.extraction_job(batch, [obs])
                if mode == "adjacent":
                    self.store.commit_extraction(job, batch["batch_id"], [obs])
                else:
                    with self.assertRaisesRegex(SemanticStoreError, "content_unit_quote_invalid"):
                        self.store.commit_extraction(job, batch["batch_id"], [obs])

    def test_claim_identity_conflict_rolls_back_observation_and_owner(self):
        self.observation("first", source_id="a", claims=["claim"])
        with self.assertRaisesRegex(SemanticStoreError, "claim_identity_conflict"):
            self.observation("different", source_id="b", claims=["claim"])
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM observations").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM source_core_jobs").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_none_merge_new_provenance_cannot_change_revision_body(self):
        _, a = self.observation("original", source_id="a")
        self.action([self.create("one", a["observation_id"])])
        original = self.store.get_item("one")
        _, b = self.observation("new semantic unit", source_id="b")
        self.now = 250
        self.action([{"action_key": "evidence", "type": "MERGE_INTO_ITEM", "item_id": "one",
                      "observation_ids": [b["observation_id"]], "modes": ["add_evidence"], "semantic_effect": "none"}], items={"one": 1})
        item = self.store.get_item("one")
        self.assertEqual(item["units"], original["units"])
        self.assertEqual(item["semantic_updated_at"], original["semantic_updated_at"])
        self.assertEqual(item["content_revision"], 2)
        self.assertEqual({o["observation_id"]: o["role"] for o in item["observations"]}[b["observation_id"]], "evidence_only")

    def test_claim_correction_propagates_every_dependency_and_is_atomic(self):
        _, old = self.observation("old", source_id="old", claims=["old"])
        _, new = self.observation("correction", source_id="new", claims=["new"])
        actions = [self.create("old-item", old["observation_id"]), self.create("new-item", new["observation_id"])]
        for index in range(27):
            target = "dependent-" + str(index)
            actions.extend([self.create(target, new["observation_id"]),
                            {"action_key": "edge-" + str(index), "type": "RELATE_ITEMS",
                             "from_item_id": "old-item" if index == 0 else "dependent-" + str(index-1), "to_item_id": target,
                             "relation": "refines_scope", "direction": "directed", "reason": "depends", "evidence_ids": [old["observation_id"]]}])
        self.action(actions)
        self.now = 200
        receipt = self.store.record_claim_relation("new", "old", "corrects", "explicit", self.store.read_revision())
        self.assertEqual(len(receipt["affected_item_ids"]), 29)
        claims = {c["claim_id"]: c for c in self.store.list_claims()}
        self.assertEqual(claims["old"]["status"], "retracted")
        self.assertEqual(claims["new"]["retracts"], ["old"])
        for iid in receipt["affected_item_ids"]:
            self.assertEqual(self.store.get_item(iid)["content_revision"], 2)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_stale_read_set"):
            self.store.record_claim_relation("new", "old", "disputes", expected_revision=0)
        with self.assertRaisesRegex(SemanticStoreError, "proposal_claim_input_mismatch"):
            self.action([{**self.create("not-created", new["observation_id"]), "claim_relations": [
                {"from_claim_id": "new", "to_claim_id": "missing", "relation": "corrects", "reason": "bad"}]}])
        self.assertIsNone(self.store.get_item("not-created"))

    def test_same_material_shares_family_without_refreshing_and_related_does_not_retract(self):
        _, a = self.observation("A", source_id="a", claims=["a"])
        _, b = self.observation("B", source_id="b", claims=["b"])
        self.action([self.create("one", a["observation_id"]), self.create("two", b["observation_id"])])
        self.now = 400
        self.store.record_claim_relation("b", "a", "same_material")
        claims = self.store.list_claims()
        self.assertEqual(claims[0]["material_id"], claims[1]["material_id"])
        self.assertEqual(self.store.get_item("one")["semantic_updated_at"], 100)
        self.store.record_claim_relation("b", "a", "related")
        self.assertTrue(all(c["status"] == "active" for c in self.store.list_claims()))
        self.store.record_claim_relation("b", "a", "contradicts")
        self.assertEqual(self.store.list_claims()[0]["status"], "contested")

    def test_complete_checks_real_membership_and_missing_outbox_on_replay(self):
        batch, obs = self.observation()
        with self.store.transaction() as conn:
            conn.execute("UPDATE observations SET resolution_status='resolved'")
        with self.assertRaisesRegex(SemanticStoreError, "semantic_batch_incomplete"):
            self.store.complete_batch(batch["batch_id"])
        self.action([self.create("one", obs["observation_id"])])
        receipt = self.store.complete_batch(batch["batch_id"])
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM projection_outbox")
        with self.assertRaisesRegex(SemanticStoreError, "semantic_outbox_evidence_missing"):
            self.store.complete_batch(batch["batch_id"])
        with self.assertRaises(SemanticStoreError):
            self.store.verify_processing_receipt(receipt)

    def test_later_batch_cannot_skip_earlier_core_pending(self):
        self.capture("unprocessed", source_id="earlier")
        batch = self.capture("noise", source_id="later")
        dispositions = {batch["member_version_ids"][0]: {"status": "noise", "reason": "explicit"}}
        job = self.extraction_job(batch, [], dispositions)
        self.store.commit_extraction(job, batch["batch_id"], [], dispositions)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_prior_batch_incomplete"):
            self.store.complete_batch(batch["batch_id"])

    def test_resources_merge_split_and_compensation_conserve_exact_metadata(self):
        resource = {"resource_id": "resource", "occurrence_id": "occurrence", "purpose": "reference",
                    "status": "pending", "location": None, "metadata": {"original": "exact", "bytes_read": False}}
        _, a = self.observation("A", source_id="a", resources=[resource])
        _, b = self.observation("B", source_id="b", claims=["claim"])
        self.store.create_view("view", "v", "definition")
        self.action([self.create("one", a["observation_id"]), self.create("two", b["observation_id"]),
                     {"action_key": "pin", "type": "ADD_MEMBERSHIP", "item_id": "two", "view_id": "view",
                      "origin": "user", "pinned": True, "placement": "top", "reason": "chosen"}], views=self.read_set()["views"])
        self.action([{"action_key": "merge", "type": "MERGE_ITEMS", "item_ids": ["one", "two"], "target_item_id": "one", "reason": "same"}],
                    items=self.read_set("one", "two")["items"], views=self.read_set()["views"])
        self.assertEqual(self.store.get_item("one")["resources"][0]["metadata"], resource["metadata"])
        self.assertTrue(self.store.get_item("one")["memberships"][0]["pinned"])
        self.assertEqual(self.store.get_item("one")["claim_ids"], ["claim"])
        before_source_ids = [r["owner_id"] for r in self.store.documents() if r["owner_type"] == "source"]
        restore_read = self.read_set("one", "two")
        receipt = self.store.restore_identity_operation("merge", restore_read)
        self.assertEqual(receipt, self.store.restore_identity_operation("merge", restore_read))
        self.assertEqual(self.store.get_item("two")["lifecycle"], "active")
        self.assertEqual(self.store.get_item("two")["aliases"], [])
        self.assertEqual(self.store.get_item("one")["observation_ids"], [a["observation_id"]])
        self.assertEqual([r["owner_id"] for r in self.store.documents() if r["owner_type"] == "source"], before_source_ids)
        self.action([{"action_key": "split", "type": "SPLIT_ITEM", "item_id": "one", "reason": "two views",
                      "outputs": [self.create("part1", a["observation_id"]), self.create("part2", a["observation_id"])]}],
                    items=self.read_set("one")["items"], views=self.read_set()["views"])
        self.assertEqual(len(self.store.get_item("part1")["resources"]), 1)
        self.store.restore_identity_operation("split", self.read_set("one", "part1", "part2"))
        self.assertEqual(self.store.get_item("part1")["aliases"], ["one"])
        self.assertEqual(self.store.get_item("one")["lifecycle"], "active")

    def test_identity_restore_rejects_changed_associations_and_stale_cas(self):
        _, a = self.observation("A", source_id="a")
        _, b = self.observation("B", source_id="b")
        self.action([self.create("one", a["observation_id"]), self.create("two", b["observation_id"])])
        stale = self.read_set("one", "two")
        self.action([{"action_key": "merge", "type": "MERGE_ITEMS", "item_ids": ["one", "two"], "target_item_id": "one", "reason": "same"}], items=stale["items"])
        with self.assertRaisesRegex(SemanticStoreError, "semantic_stale_read_set"):
            self.store.restore_identity_operation("merge", stale)
        _, later = self.observation("later", source_id="later")
        self.action([{"action_key": "later", "type": "MERGE_INTO_ITEM", "item_id": "one", "observation_ids": [later["observation_id"]],
                      "modes": ["continue"], "semantic_effect": "continuation"}], items=self.read_set("one")["items"])
        with self.assertRaisesRegex(SemanticStoreError, "semantic_identity_restore_changed"):
            self.store.restore_identity_operation("merge", self.read_set("one", "two"))

    def test_supersession_requires_exact_latest_done_and_never_acks_index(self):
        batch, a = self.observation()
        self.action([self.create("one", a["observation_id"])])
        completed = self.store.complete_batch(batch["batch_id"])
        _, b = self.observation("later", source_id="later")
        self.action([{"action_key": "later", "type": "MERGE_INTO_ITEM", "item_id": "one", "observation_ids": [b["observation_id"]],
                      "modes": ["continue"], "semantic_effect": "continuation"}], items={"one": 1})
        with self.assertRaisesRegex(SemanticStoreError, "semantic_projection_supersession_unproved"):
            self.store.supersede_projection_outbox("one", 2, "published")
        latest = next(r for r in self.store.pending_outbox("projection") if r["revision"] == 2)
        self.store.ack_outbox("projection", latest["outbox_id"], "one", 2, "published")
        self.assertEqual(len(self.store.supersede_projection_outbox("one", 2, "published")), 1)
        self.assertTrue(self.store.pending_outbox("index"))
        self.assertEqual(self.store.verify_processing_receipt(completed), completed)

    def test_capture_extraction_and_view_edits_invalidate_global_snapshot(self):
        revision = self.store.read_revision()
        batch = self.capture("noise")
        self.assertGreater(self.store.read_revision(), revision)
        revision = self.store.read_revision()
        dispositions = {batch["member_version_ids"][0]: {"status": "noise", "reason": "explicit"}}
        job = self.extraction_job(batch, [], dispositions)
        self.store.commit_extraction(job, batch["batch_id"], [], dispositions)
        self.assertGreater(self.store.read_revision(), revision)
        revision = self.store.read_revision()
        self.store.create_view("view", "title", "first")
        self.store.update_view("view", "second", 1)
        self.assertEqual(self.store.read_revision(), revision + 2)

    def test_accepted_hash_and_proposal_input_tampering_fail_before_item_write(self):
        _, obs = self.observation()
        action = self.create("one", obs["observation_id"])
        read_set = self.read_set()
        artifact = {"actions": [action], "read_set": read_set}
        inputs = {"state": {"read_set": read_set, "observations": [self.store.get_observation(obs["observation_id"])], "items": {}}}
        job = self.accept(artifact, "propose", inputs)
        with self.store.transaction() as conn:
            conn.execute("UPDATE accepted_job_artifacts SET artifact_hash=? WHERE job_id=?", ("wrong", job))
        with self.assertRaisesRegex(SemanticStoreError, "accepted_artifact_corrupt"):
            self.store.apply_proposal(job, [action], read_set=read_set)
        job = self.accept(artifact, "propose", {"state": {**inputs["state"], "read_set": {**read_set, "store_revision": 0}}})
        with self.assertRaisesRegex(SemanticStoreError, "proposal_input_mismatch"):
            self.store.apply_proposal(job, [action], read_set=read_set)
        self.assertIsNone(self.store.get_item("one"))

    def test_multiple_observations_are_grounded_in_one_core_source(self):
        batch = self.capture("第一句。第二句。")
        vid = batch["member_version_ids"][0]
        span = asdict(prepare_spans([CapturedSource(vid, "第一句。第二句。")], namespace=self.store.generation_id)[0])
        observations = [{"observation_id": "obs" + str(index), "title": body, "scene_kind": "quote",
                         "spans": [span], "units": [{"unit_id": "unit" + str(index), "kind": "quote", "text": body,
                                                      "span_ids": [span["span_id"]]}], "topics": [], "claims": []}
                        for index, body in enumerate(("第一句。", "第二句。"))]
        job = self.extraction_job(batch, observations)
        self.store.commit_extraction(job, batch["batch_id"], observations)
        self.action([self.create("one", "obs0"), self.create("two", "obs1")])
        receipt = self.store.complete_batch(batch["batch_id"])
        self.assertEqual(len(receipt["proof"]["observation_memberships"]), 2)

    def test_identity_merge_cannot_discard_conflicting_user_view_choices(self):
        _, obs = self.observation()
        self.store.create_view("view", "title", "definition")
        actions = [self.create("one", obs["observation_id"]), self.create("two", obs["observation_id"])]
        for iid, placement in (("one", "top"), ("two", "bottom")):
            actions.append({"action_key": "pin-" + iid, "type": "ADD_MEMBERSHIP", "item_id": iid,
                            "view_id": "view", "origin": "user", "placement": placement, "pinned": True})
        self.action(actions, views=self.read_set()["views"])
        with self.assertRaisesRegex(SemanticStoreError, "semantic_user_membership_conflict"):
            self.action([{"action_key": "merge", "type": "MERGE_ITEMS", "item_ids": ["one", "two"], "target_item_id": "one", "reason": "same"}],
                        items=self.read_set("one", "two")["items"], views=self.read_set()["views"])
        self.assertEqual(self.store.get_item("two")["lifecycle"], "active")
        self.assertEqual(self.store.get_item("one")["memberships"][0]["placement"], "top")
        self.assertEqual(self.store.get_item("two")["memberships"][0]["placement"], "bottom")

    def test_stale_view_and_boolean_revision_are_not_valid_cas(self):
        _, obs = self.observation()
        self.store.create_view("view", "title", "original")
        previous_view = self.read_set()["views"]
        self.store.update_view("view", "changed", 1)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_stale_read_set"):
            self.action([self.create("one", obs["observation_id"])], views=previous_view)
        self.action([self.create("one", obs["observation_id"])])
        with self.assertRaisesRegex(SemanticStoreError, "semantic_stale_read_set"):
            self.action([{"action_key": "replay", "type": "MERGE_INTO_ITEM", "item_id": "one", "observation_ids": [obs["observation_id"]],
                          "modes": ["add_evidence"], "semantic_effect": "none"}], items={"one": True})

    def test_migration_aggregate_uses_verified_real_outboxes(self):
        batch, obs = self.observation()
        self.action([self.create("one", obs["observation_id"])])
        processing = self.store.complete_batch(batch["batch_id"])
        receipt = {"receipt_id": "migration", "migration_id": "migration", "canonical_receipt_id": "migration",
                   "outbox_receipt_id": "migration", "semantic_generation_id": self.store.generation_id,
                   "processing_receipt_ids": [processing["receipt_id"]], "capture_receipt_ids": processing["capture_receipt_ids"]}
        self.assertEqual(self.store.record_migration_receipt(receipt, [processing]), receipt)
        with closing(self.store.connect()) as conn:
            row = conn.execute("SELECT * FROM canonical_transactions WHERE transaction_id='migration'").fetchone()
        self.assertEqual(json.loads(row["outbox_ids_json"]), sorted(processing["outbox_ids"]))
        self.assertEqual(json.loads(row["actions_json"])[0]["type"], "MIGRATION")
        with self.assertRaisesRegex(SemanticStoreError, "semantic_migration_receipt_invalid"):
            self.store.record_migration_receipt({**receipt, "capture_receipt_ids": []}, [processing])

    def test_unknown_generation_and_symlink_fail_before_initializing_tables(self):
        path = Path(self.temp.name).resolve() / "foreign.sqlite"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE generation_manifest(generation_id TEXT,schema TEXT)")
            conn.execute("INSERT INTO generation_manifest VALUES('foreign','unknown')")
            conn.commit()
        original = path.read_bytes()
        with self.assertRaisesRegex(SemanticStoreError, "semantic_generation_mismatch"):
            SemanticStore(path, "test-gen")
        self.assertEqual(path.read_bytes(), original)
        link = Path(self.temp.name).resolve() / "linked.sqlite"
        link.symlink_to(path)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_store_symlink"):
            SemanticStore(link, "test-gen")

    def test_read_apis_preserve_database_revision_bytes_and_permissions(self):
        _, obs = self.observation()
        self.action([self.create("one", obs["observation_id"])])
        path = Path(self.store.path)
        before = path.read_bytes(), path.stat().st_mode, self.store.read_revision()
        self.store.get_item("one")
        self.store.list_items()
        self.store.documents()
        self.assertEqual((path.read_bytes(), path.stat().st_mode, self.store.read_revision()), before)

    def test_claim_relation_action_and_later_failure_share_one_transaction(self):
        _, old = self.observation("old", source_id="old", claims=["old"])
        _, new = self.observation("new", source_id="new", claims=["new"])
        self.action([self.create("old-item", old["observation_id"])])
        correction = {**self.create("new-item", new["observation_id"]), "claim_relations": [
            {"from_claim_id": "new", "to_claim_id": "old", "relation": "retracts", "reason": "explicit"}]}
        invalid = {"type": "MERGE_INTO_ITEM", "action_key": "invalid", "item_id": "missing",
                   "observation_ids": [new["observation_id"]], "modes": ["continue"], "semantic_effect": "continuation"}
        with self.assertRaisesRegex(SemanticStoreError, "semantic_item_missing"):
            self.action([correction, invalid], items={"old-item": 1})
        self.assertIsNone(self.store.get_item("new-item"))
        self.assertEqual({c["claim_id"]: c["status"] for c in self.store.list_claims()}["old"], "active")
        self.action([correction], items={"old-item": 1})
        self.assertEqual({c["claim_id"]: c["status"] for c in self.store.list_claims()}["old"], "retracted")

    def test_claim_time_cannot_invent_post_recovery_evidence(self):
        batch = self.capture("occurred earlier")
        vid = batch["member_version_ids"][0]
        span = asdict(prepare_spans([CapturedSource(vid, "occurred earlier")], namespace=self.store.generation_id)[0])
        obs = {"observation_id": "forged-time", "title": "earlier", "scene_kind": "technical", "spans": [span],
               "units": [{"unit_id": "unit", "kind": "fact", "text": "earlier", "span_ids": [span["span_id"]]}],
               "topics": [], "claims": [{"claim_id": "claim", "predicates": {"value": True},
                                           "span_ids": [span["span_id"]], "observed_at": 500}]}
        job = self.extraction_job(batch, [obs])
        with self.assertRaisesRegex(SemanticStoreError, "claim_time_not_grounded"):
            self.store.commit_extraction(job, batch["batch_id"], [obs])
        self.assertIsNone(self.store.get_observation("forged-time"))


if __name__ == "__main__":
    unittest.main()
