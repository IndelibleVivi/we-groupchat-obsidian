"""Bounded adjacent-context coverage for members the primary pages left unclassified."""
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ai.semantic_provider import ProviderError, TextResult
from core.semantic.engine import SemanticEngine, SemanticValidationError, extraction_prompt
from core.semantic.store import SemanticStore

from tests.test_semantic_engine import FakeDecision, FakeText, capture


class CoverageText(FakeText):
    """A synthetic provider whose per-pass ability to ground a message is explicit.

    ``primary`` decides what the first bounded page pass can classify; ``coverage``
    decides the later re-read. Only the coverage prompt carries adjacent context.
    """

    def __init__(self, *, primary=None, coverage=None, noise=None, fail_coverage=0):
        super().__init__()
        self.primary = primary or (lambda text: True)
        self.coverage = coverage or (lambda text: True)
        self.noise = noise or (lambda text: False)
        self.fail_coverage = fail_coverage
        self.coverage_prompts = []

    def generate(self, prompt, **kwargs):
        if not prompt.startswith("The following captured"):
            return super().generate(prompt, **kwargs)
        self.calls.append(kwargs["request_id"])
        self.prompt_sizes.append(len(prompt.encode()))
        data = json.loads(prompt.rsplit("\n", 1)[-1])
        bounded = "Bounded adjacent context" in prompt
        if bounded:
            self.coverage_prompts.append(prompt)
            if self.fail_coverage:
                self.fail_coverage -= 1
                raise RuntimeError("synthetic coverage interruption")
        decide = self.coverage if bounded else self.primary
        values, dispositions = [], {}
        for source in data:
            if not source["core"]:
                continue
            text = "".join(span["excerpt"] for span in source["spans"])
            if decide(text):
                values.append({"title": text[:60], "scene_kind": "life",
                               "spans": [{"source_id": source["source_id"], "span_id": span["span_id"]}
                                         for span in source["spans"]],
                               "units": [{"kind": "scene", "text": text,
                                          "span_indices": list(range(len(source["spans"])))}],
                               "topics": [], "claims": []})
                continue
            if not bounded and self.noise(text):
                dispositions[source["source_id"]] = {"status": "noise", "reason": "synthetic_noise"}
        result = {"observations": values}
        if dispositions:
            result["dispositions"] = dispositions
        return TextResult(json.dumps(result, ensure_ascii=False), self.route)


class CoverageEngineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SemanticStore(Path(temporary.name).resolve() / "knowledge.sqlite", "synthetic-generation")

    def dispositions(self):
        with closing(self.store.connect()) as connection:
            return {row[0]: row[1] for row in connection.execute(
                "SELECT source_version_id,status FROM source_dispositions")}

    def historical_coverage(self, batch_id, policy, provider, messages=None):
        """Freeze a never-dispatched historical child through the real job API."""
        capture(self.store, batch_id, messages or ["待理解的片段 " + batch_id, "明确邻居"])
        engine = SemanticEngine(self.store, provider, FakeDecision())
        intercepted = {}
        original = engine._accepted_extract_page
        def freeze(child, sources, span_ids, namespace, generation, *, coverage=False):
            if not coverage:
                return original(child, sources, span_ids, namespace, generation)
            intercepted.update(child)
            raise RuntimeError("synthetic before historical dispatch")
        with patch.object(engine, "_accepted_extract_page", side_effect=freeze):
            with self.assertRaisesRegex(RuntimeError, "synthetic before historical dispatch"):
                engine.extract_batch(batch_id)
        inputs = dict(intercepted["inputs"])
        inputs.pop("pending_span_ids")
        versions = {"prompt": "semantic-extraction-coverage-v2"}
        if policy is not None:
            versions["unit_policy"] = policy
        with closing(self.store.connect()) as db:
            db.execute("DELETE FROM jobs WHERE job_id=?", (intercepted["job_id"],))
            db.commit()
        return engine, engine.jobs.ensure("extract_coverage", inputs, versions)

    def test_bounded_context_supplement_finishes_an_unclassified_member(self):
        capture(self.store, "bounded", ["晚上老地方见。", "好，就在美术馆门口见。"])
        text = CoverageText(primary=lambda value: "美术馆" in value, coverage=lambda value: True)
        result = SemanticEngine(self.store, text, FakeDecision(possible=0)).process_batch("bounded")
        self.assertEqual(result["status"], "complete")
        self.assertIsNotNone(result["processing_receipt"])
        self.assertEqual(set(self.dispositions().values()), {"observed"})
        self.assertEqual(len(self.dispositions()), 2)
        self.assertTrue(any("美术馆" in prompt for prompt in text.coverage_prompts))
        with closing(self.store.connect()) as connection:
            self.assertGreater(connection.execute(
                "SELECT count(*) FROM jobs WHERE kind='extract_coverage'").fetchone()[0], 0)

    def test_fully_grounded_batch_never_runs_a_coverage_pass(self):
        capture(self.store, "plain", ["一个含义清晰的生活片段。"])
        extracted = SemanticEngine(self.store, CoverageText(), FakeDecision()).extract_batch("plain")
        self.assertNotIn("coverage_page_job_ids", extracted)
        with closing(self.store.connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM jobs WHERE kind='extract_coverage'").fetchone()[0], 0)

    def test_filtered_system_envelope_is_conserved_without_fabricating_a_span(self):
        for batch_id, body, message_type, expected in (
                ('filtered', '', 10000, 'unsupported_type'),
                ('readable-system', 'A readable system event', 10000, 'observed'),
                ('empty-ordinary', '', 1, None)):
            with self.subTest(batch_id=batch_id):
                record = capture(self.store, batch_id, [body], payloads=[{'type': message_type}])
                text = CoverageText(primary=bool, coverage=bool)
                result = SemanticEngine(self.store, text, FakeDecision(possible=0)).process_batch(batch_id)
                source_id = record['member_version_ids'][0]
                self.assertEqual(self.dispositions().get(source_id), expected)
                self.assertEqual(self.store.get_source(source_id)['payload'], {'type': message_type})
                if expected is not None:
                    self.assertEqual(result['status'], 'complete')
                    self.assertTrue(result['processing_receipt'])
                else:
                    self.assertEqual(result['status'], 'unclassified')
                if expected == 'unsupported_type':
                    self.assertFalse(text.coverage_prompts)
                    with closing(self.store.connect()) as connection:
                        self.assertEqual(connection.execute(
                            'SELECT count(*) FROM source_spans WHERE source_version_id=?', (source_id,)).fetchone()[0], 0)

    def test_coverage_can_cite_a_neighbour_alongside_the_pending_core(self):
        capture(self.store, "joint", ["晚上老地方见。", "好，就在美术馆门口见。"])

        class JointEvidence(CoverageText):
            def generate(self, prompt, **kwargs):
                if "Bounded adjacent context" not in prompt:
                    return super().generate(prompt, **kwargs)
                self.calls.append(kwargs['request_id'])
                data = json.loads(prompt.rsplit('\n', 1)[-1])
                core = next(source for source in data if source['core'])
                context = next(source for source in data if not source['core'])
                return TextResult(json.dumps({'observations': [{
                    'title': '两人约在美术馆门口见面', 'scene_kind': 'life',
                    'spans': [{'source_id': source['source_id'], 'span_id': source['spans'][0]['span_id']}
                              for source in (core, context)],
                    'units': [{'kind': 'scene', 'text': '双方约好晚上在美术馆门口见面。', 'span_indices': [0, 1]}],
                    'topics': [], 'claims': []}]}), self.route)

        text = JointEvidence(primary=lambda value: "美术馆" in value)
        result = SemanticEngine(self.store, text, FakeDecision(possible=0)).process_batch('joint')
        self.assertEqual(result['status'], 'complete')
        with closing(self.store.connect()) as connection:
            job = connection.execute("SELECT inputs_json,artifact_json FROM jobs "
                "JOIN accepted_job_artifacts USING(job_id) WHERE kind='extract_coverage'").fetchone()
        inputs, artifact = json.loads(job[0]), json.loads(job[1])
        cited = {span['source_id'] for span in artifact['observations'][0]['spans']}
        self.assertTrue(cited & set(inputs['core_source_ids']))
        self.assertTrue(cited & set(inputs['context_source_ids']))
        self.assertTrue(inputs['context_span_ids'])

    def test_joint_unit_can_keep_context_only_typed_claim_evidence(self):
        capture(self.store, "claim-context", ["这一段是对此前报告的修正。", "此前报告的状态。"])
        class ContextClaim(CoverageText):
            def generate(self, prompt, **kwargs):
                if "Bounded adjacent context" not in prompt:
                    return super().generate(prompt, **kwargs)
                self.calls.append(kwargs["request_id"])
                data = json.loads(prompt.rsplit("\n", 1)[-1])
                core = next(source for source in data if source["core"])
                context = next(source for source in data if not source["core"])
                return TextResult(json.dumps({"observations": [{"title": "修正与此前报告", "scene_kind": "technical",
                    "spans": [{"source_id": source["source_id"], "span_id": source["spans"][0]["span_id"]}
                              for source in (core, context)],
                    "units": [{"kind": "counterevidence", "text": "修正针对此前的报告。", "span_indices": [0, 1]}],
                    "topics": [], "claims": [{"subject_id": None, "case_id": None, "condition": None,
                        "channel": None, "version": None, "phase": "prior report",
                        "observed_at": context["occurred_at"], "predicates": {"status": "prior"}, "span_indices": [1]}]}]}), self.route)
        result = SemanticEngine(self.store, ContextClaim(primary=lambda text: text == "此前报告的状态。"),
                                FakeDecision()).extract_batch("claim-context")
        observation = next(value for value in result["observations"] if value["claims"])
        self.assertEqual(len(observation["units"][0]["span_ids"]), 2)
        self.assertEqual(observation["claims"][0]["span_ids"], [observation["spans"][1]["span_id"]])

    def test_noncore_only_units_cannot_borrow_an_observation_core_reference(self):
        messages = ["普通接话 " + str(index) for index in range(26)]
        messages[20:25] = ["终端故障的修复办法。", "文件用途的说明。", "这个呢", "午餐的独立经历。", "嗯"]
        capture(self.store, "borrowed-core", messages)
        class BorrowedCore(CoverageText):
            def generate(self, prompt, **kwargs):
                if "Bounded adjacent context" not in prompt:
                    return super().generate(prompt, **kwargs)
                self.coverage_prompts.append(prompt)
                self.calls.append(kwargs["request_id"])
                data = json.loads(prompt.rsplit("\n", 1)[-1])
                core = next(source for source in data if source["core"])
                if "Correct the prior JSON" in prompt:
                    return TextResult(json.dumps({"observations": [], "dispositions": {
                        core["source_id"]: {"status": "context_only", "reason": "isolated unclear reply"}}}), self.route)
                context = [source for source in data if not source["core"]]
                refs = [{"source_id": source["source_id"], "span_id": source["spans"][0]["span_id"]}
                        for source in (core, *context)]
                return TextResult(json.dumps({"observations": [{"title": "borrowed old content", "scene_kind": "other",
                    "spans": refs, "units": [{"kind": "quote", "span_indices": [index]}
                                             for index in range(1, len(refs))], "topics": [], "claims": []}]}), self.route)
        text = BorrowedCore(primary=lambda value: value in (messages[20], messages[21], messages[23]),
                            noise=lambda value: value != messages[22])
        decision = FakeDecision(defer=True)
        result = SemanticEngine(self.store, text, decision).process_batch("borrowed-core")
        self.assertEqual(len(text.coverage_prompts), 2, "invalid units must reach the known-output correction loop")
        self.assertIn("semantic_coverage_unit_without_pending_core", text.coverage_prompts[1])
        self.assertEqual(result["status"], "deferred")
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM observations").fetchone()[0], 3)
            row = db.execute("SELECT j.attempt,a.artifact_json FROM jobs j JOIN accepted_job_artifacts a USING(job_id) "
                             "WHERE j.kind='extract_coverage'").fetchone()
        self.assertEqual(row["attempt"], 2)
        self.assertEqual(json.loads(row["artifact_json"])["observations"], [])
        self.assertIsNone(result.get("processing_receipt"))

    def test_terminal_uncited_fragments_do_not_reenter_fresh_coverage_core(self):
        capture(self.store, "terminal-fragments", ["x" * 60000])
        class TerminalPrefix(CoverageText):
            def __init__(self):
                super().__init__()
                self.primary_pages, self.coverage_pages = [], []
            def generate(self, prompt, **kwargs):
                if not prompt.startswith("The following captured"):
                    return super().generate(prompt, **kwargs)
                self.calls.append(kwargs["request_id"])
                data = json.loads(prompt.rsplit("\n", 1)[-1])
                if "Bounded adjacent context" in prompt:
                    self.coverage_pages.append(data)
                    status = "context_only"
                else:
                    self.primary_pages.append(data)
                    status = "noise" if len(self.primary_pages) == 1 else "unclassified"
                return TextResult(json.dumps({"observations": [], "dispositions": {
                    source["source_id"]: {"status": status, "reason": "explicit synthetic fragment status"}
                    for source in data if source["core"]}}), self.route)
        text = TerminalPrefix()
        result = SemanticEngine(self.store, text, FakeDecision()).process_batch("terminal-fragments")
        self.assertEqual(result["status"], "complete")
        self.assertGreater(len(text.primary_pages), 1)
        terminal = {span["span_id"] for source in text.primary_pages[0] if source["core"] for span in source["spans"]}
        pending = {span["span_id"] for page in text.primary_pages[1:] for source in page if source["core"]
                   for span in source["spans"]}
        covered = {span["span_id"] for page in text.coverage_pages for source in page if source["core"]
                   for span in source["spans"]}
        self.assertTrue(terminal.isdisjoint(covered), "a terminal page is not pending merely because its source has another pending page")
        self.assertEqual(covered, pending)
        with closing(self.store.connect()) as db:
            jobs = [(json.loads(row[0]), json.loads(row[1])) for row in db.execute(
                "SELECT inputs_json,versions_json FROM jobs WHERE kind='extract_coverage'")]
        for inputs, versions in jobs:
            self.assertEqual(set(inputs["pending_span_ids"]), set(inputs["span_ids"]))
            self.assertTrue(set(inputs["pending_span_ids"]) <= pending)
            self.assertEqual(versions["planning"], "pending-fragments-v1")
            self.assertEqual(versions["unit_policy"], "retained-knowledge-v5")

    def test_terminal_fragments_in_a_frozen_old_page_keep_their_original_scope(self):
        from dataclasses import asdict
        from core.semantic.engine import CapturedSource, prepare_spans
        class Prefix(CoverageText):
            primary_count = 0
            def generate(self, prompt, **kwargs):
                if not prompt.startswith("The following captured") or "Bounded adjacent context" in prompt:
                    return super().generate(prompt, **kwargs)
                self.primary_count += 1
                self.calls.append(kwargs["request_id"])
                data = json.loads(prompt.rsplit("\n", 1)[-1])
                return TextResult(json.dumps({"observations": [], "dispositions": {
                    source["source_id"]: {"status": "noise" if self.primary_count == 1 else "unclassified",
                                          "reason": "explicit fragment status"}
                    for source in data if source["core"]}}), self.route)
        provider = Prefix()
        engine, intercepted = self.historical_coverage("old-fragments", "retained-knowledge-v4", provider,
                                                      ["x" * 60000])
        member = self.store.get_batch("old-fragments")["members"][0]
        spans = prepare_spans([CapturedSource(member["source_version_id"], member["text"])], namespace=self.store.generation_id)
        old_page = next(engine._pages([asdict(span) for span in spans], max_bytes=22000, max_count=24))
        inputs = dict(intercepted["inputs"], span_ids=[span["span_id"] for span in old_page])
        with closing(self.store.connect()) as db:
            db.execute("DELETE FROM jobs WHERE job_id=?", (intercepted["job_id"],))
            db.commit()
        old = engine.jobs.ensure("extract_coverage", inputs, intercepted["versions"])
        result = engine.extract_batch("old-fragments")
        self.assertTrue(result["receipt"])
        with closing(self.store.connect()) as db:
            jobs = [dict(row) for row in db.execute("SELECT * FROM jobs WHERE kind='extract_coverage'")]
        self.assertEqual(next(json.loads(row["inputs_json"]) for row in jobs if row["job_id"] == old["job_id"]), old["inputs"])
        new = [json.loads(row["inputs_json"]) for row in jobs if row["job_id"] != old["job_id"]]
        self.assertTrue(new)
        self.assertTrue(all(set(value["pending_span_ids"]).isdisjoint(inputs["span_ids"]) for value in new))

    def test_every_terminal_long_source_fragment_finishes_without_coverage(self):
        capture(self.store, "all-terminal", ["x" * 60000])
        provider = CoverageText(primary=lambda text: False, noise=lambda text: True)
        result = SemanticEngine(self.store, provider, FakeDecision()).process_batch("all-terminal")
        self.assertEqual(result["status"], "complete")
        self.assertGreater(len(provider.calls), 1)
        self.assertEqual(provider.coverage_prompts, [])
        self.assertEqual(set(self.dispositions().values()), {"noise"})

    def test_later_primary_evidence_can_finish_pending_spans_without_another_provider_pass(self):
        messages = ["嗯 " + str(index) for index in range(25)]
        messages[0], messages[-1] = "前面的不完整问题。", "后面的完整回答。"
        capture(self.store, "later-primary", messages)
        class LaterAnswer(CoverageText):
            def generate(self, prompt, **kwargs):
                if not prompt.startswith("The following captured"):
                    return super().generate(prompt, **kwargs)
                data = json.loads(prompt.rsplit("\n", 1)[-1])
                answer = next((source for source in data if source["core"] and
                               any(span["excerpt"] == messages[-1] for span in source["spans"])), None)
                if answer is None:
                    return super().generate(prompt, **kwargs)
                self.calls.append(kwargs["request_id"])
                question = next(source for source in data if not source["core"] and
                                any(span["excerpt"] == messages[0] for span in source["spans"]))
                return TextResult(json.dumps({"observations": [{"title": "完整问答", "scene_kind": "other",
                    "spans": [{"source_id": source["source_id"], "span_id": source["spans"][0]["span_id"]}
                              for source in (answer, question)],
                    "units": [{"kind": "fact", "text": "后续回答解释了前面的问题。", "span_indices": [0, 1]}],
                    "topics": [], "claims": []}]}), self.route)
        provider = LaterAnswer(primary=lambda text: False, noise=lambda text: text != messages[0])
        result = SemanticEngine(self.store, provider, FakeDecision(possible=0)).extract_batch("later-primary")
        self.assertTrue(result["receipt"])
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.coverage_prompts, [])

    def test_repeated_invalid_coverage_units_never_become_an_accepted_artifact(self):
        capture(self.store, "never-admit", ["模糊接话。", "明确邻居"])
        class InvalidUnits(CoverageText):
            def generate(self, prompt, **kwargs):
                if "Bounded adjacent context" not in prompt:
                    return super().generate(prompt, **kwargs)
                self.calls.append(kwargs["request_id"])
                data = json.loads(prompt.rsplit("\n", 1)[-1])
                core = next(source for source in data if source["core"])
                context = next(source for source in data if not source["core"])
                return TextResult(json.dumps({"observations": [{"title": "old context", "scene_kind": "other",
                    "spans": [{"source_id": source["source_id"], "span_id": source["spans"][0]["span_id"]}
                              for source in (core, context)],
                    "units": [{"kind": "quote", "span_indices": [1]}], "topics": [], "claims": []}]}), self.route)
        provider = InvalidUnits(primary=lambda text: text == "明确邻居")
        with self.assertRaisesRegex(SemanticValidationError, "semantic_coverage_unit_without_pending_core"):
            SemanticEngine(self.store, provider, FakeDecision()).extract_batch("never-admit")
        with closing(self.store.connect()) as db:
            row = db.execute("SELECT job_id,state,attempt FROM jobs WHERE kind='extract_coverage'").fetchone()
            accepted = db.execute("SELECT 1 FROM accepted_job_artifacts WHERE job_id=?", (row["job_id"],)).fetchone()
            self.assertEqual(db.execute("SELECT count(*) FROM extraction_receipts").fetchone()[0], 0)
        self.assertEqual((row["state"], row["attempt"]), ("failed", 3))
        self.assertIsNone(accepted)

    def test_frozen_policy_children_reuse_accepted_artifacts_without_new_admission(self):
        class ContextUnit(CoverageText):
            def generate(self, prompt, **kwargs):
                if "Bounded adjacent context" not in prompt:
                    return super().generate(prompt, **kwargs)
                self.calls.append(kwargs["request_id"])
                data = json.loads(prompt.rsplit("\n", 1)[-1])
                core = next(source for source in data if source["core"])
                context = next(source for source in data if not source["core"])
                return TextResult(json.dumps({"observations": [{"title": "historical context unit", "scene_kind": "other",
                    "spans": [{"source_id": source["source_id"], "span_id": source["spans"][0]["span_id"]}
                              for source in (core, context)],
                    "units": [{"kind": "quote", "span_indices": [1]}], "topics": [], "claims": []}]}), self.route)
        for policy in (None, *("retained-knowledge-v" + str(index) for index in range(1, 5))):
            with self.subTest(policy=policy):
                batch_id = "accepted-frozen-" + str(policy)
                provider = ContextUnit(primary=lambda text: text == "明确邻居")
                engine, old = self.historical_coverage(batch_id, policy, provider)
                original = engine._accepted
                def stop_parent(job, callback):
                    if job["kind"] == "extract":
                        raise RuntimeError("synthetic after historical child acceptance")
                    return original(job, callback)
                with patch.object(engine, "_accepted", side_effect=stop_parent), \
                     patch("core.semantic.engine.extraction_prompt", wraps=extraction_prompt) as prompts:
                    with self.assertRaisesRegex(RuntimeError, "historical child acceptance"):
                        engine.extract_batch(batch_id)
                self.assertEqual([call.kwargs["unit_policy"] for call in prompts.call_args_list], [policy])
                with closing(self.store.connect()) as db:
                    before = tuple(db.execute("SELECT artifact_hash,artifact_json FROM accepted_job_artifacts WHERE job_id=?",
                                              (old["job_id"],)).fetchone())
                sent = list(provider.calls)
                with patch("core.semantic.engine.validate_extraction", side_effect=AssertionError("revalidated accepted output")):
                    engine.extract_batch(batch_id)
                self.assertEqual(provider.calls, sent)
                with closing(self.store.connect()) as db:
                    after = tuple(db.execute("SELECT artifact_hash,artifact_json FROM accepted_job_artifacts WHERE job_id=?",
                                             (old["job_id"],)).fetchone())
                    count = db.execute("SELECT count(*) FROM jobs WHERE kind='extract_coverage' AND "
                        "json_extract(inputs_json,'$.parent_job_id')=?", (old["inputs"]["parent_job_id"],)).fetchone()[0]
                self.assertEqual(after, before)
                self.assertEqual(count, 1)

    def test_unknown_frozen_unit_policies_block_before_fresh_job_or_provider(self):
        for policy in (None, *("retained-knowledge-v" + str(index) for index in range(1, 5))):
            with self.subTest(policy=policy):
                batch_id = "unknown-frozen-" + str(policy)
                provider = CoverageText(primary=lambda text: False)
                engine, old = self.historical_coverage(batch_id, policy, provider)
                engine.jobs.claim(old["job_id"], engine.worker_id)
                engine.jobs.fail(old["job_id"], engine.worker_id, "synthetic_unknown", unknown=True)
                sent = list(provider.calls)
                with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
                    engine.extract_batch(batch_id)
                self.assertEqual(provider.calls, sent)
                with closing(self.store.connect()) as db:
                    rows = list(db.execute("SELECT job_id,state FROM jobs WHERE kind='extract_coverage' AND "
                        "json_extract(inputs_json,'$.parent_job_id')=?", (old["inputs"]["parent_job_id"],)))
                self.assertEqual([tuple(row) for row in rows], [(old["job_id"], "unknown")])

    def test_unknown_parent_blocks_pending_frozen_coverage_before_dispatch(self):
        provider = CoverageText(primary=lambda text: False)
        engine, old = self.historical_coverage("unknown-parent", "retained-knowledge-v4", provider)
        parent_id = old["inputs"]["parent_job_id"]
        engine.jobs.claim(parent_id, engine.worker_id)
        engine.jobs.fail(parent_id, engine.worker_id, "synthetic_parent_unknown", unknown=True)
        sent = list(provider.calls)
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            engine.extract_batch("unknown-parent")
        self.assertEqual(provider.calls, sent)
        with closing(self.store.connect()) as db:
            rows = list(db.execute("SELECT job_id,state FROM jobs WHERE kind='extract_coverage' AND "
                "json_extract(inputs_json,'$.parent_job_id')=?", (parent_id,)))
        self.assertEqual([tuple(row) for row in rows], [(old["job_id"], "pending")])

    def test_coverage_prompt_upgrade_cannot_bypass_an_old_unknown_job(self):
        capture(self.store, 'upgrade', ['An unclear scene', 'An explanatory neighbour'])
        text = CoverageText(primary=lambda value: False)
        engine = SemanticEngine(self.store, text, FakeDecision())
        with patch.object(engine, '_cover_unclassified', side_effect=RuntimeError('synthetic interruption')):
            with self.assertRaises(RuntimeError):
                engine.extract_batch('upgrade')
        with closing(self.store.connect()) as connection:
            parent = connection.execute("SELECT job_id FROM jobs WHERE kind='extract'").fetchone()[0]
        old = engine.jobs.ensure('extract_coverage', {'parent_job_id': parent, 'phase': 'bounded-adjacent-context-v1'},
                                 {'prompt': 'semantic-extraction-coverage-v1'})
        engine.jobs.claim(old['job_id'], engine.worker_id)
        engine.jobs.fail(old['job_id'], engine.worker_id, 'synthetic_unknown', unknown=True)
        sent = list(text.calls)
        with self.assertRaisesRegex(SemanticValidationError, 'semantic_job_busy_or_unknown'):
            engine.extract_batch('upgrade')
        self.assertEqual(text.calls, sent)

    def test_coverage_reuses_the_accepted_page_without_resending_the_original_model(self):
        capture(self.store, "reuse", ["第一句含义不明。", "补充说明：指的是美术馆。", "第三句含义同样不明。"])
        first = CoverageText(primary=lambda value: "美术馆" in value, coverage=lambda value: True,
                             fail_coverage=1)
        with self.assertRaisesRegex(RuntimeError, "synthetic coverage interruption"):
            SemanticEngine(self.store, first, FakeDecision(possible=0)).extract_batch("reuse")
        with closing(self.store.connect()) as connection:
            page = connection.execute("SELECT job_id FROM jobs WHERE kind='extract_page'").fetchone()
            before = connection.execute("SELECT artifact_hash FROM accepted_job_artifacts WHERE job_id=?",
                                        (page["job_id"],)).fetchone()[0]
            parent = connection.execute("SELECT state FROM jobs WHERE kind='extract'").fetchone()[0]
        self.assertEqual(parent, "pending")
        request_id = page["job_id"] + ":text"
        self.assertEqual(first.calls.count(request_id), 1)
        resumed = CoverageText(primary=lambda value: "美术馆" in value, coverage=lambda value: True)
        result = SemanticEngine(self.store, resumed, FakeDecision(possible=0)).process_batch("reuse")
        self.assertEqual(result["status"], "complete")
        self.assertNotIn(request_id, resumed.calls)
        self.assertTrue(resumed.coverage_prompts)
        with closing(self.store.connect()) as connection:
            after = connection.execute("SELECT artifact_hash FROM accepted_job_artifacts WHERE job_id=?",
                                       (page["job_id"],)).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(set(self.dispositions().values()), {"observed"})

    def test_existing_coverage_keeps_its_old_unit_policy_and_identity(self):
        capture(self.store, "old-policy", ["一句尚待理解的话。"])
        text = CoverageText(primary=lambda value: False)
        engine = SemanticEngine(self.store, text, FakeDecision())
        frozen = {}
        def freeze_old(child, sources, span_ids, namespace, generation, *, coverage=False):
            if not coverage:
                return original(child, sources, span_ids, namespace, generation)
            old_versions = {"prompt": child["versions"]["prompt"]}
            frozen["inputs"] = child["inputs"]
            frozen["versions"] = old_versions
            raise RuntimeError("synthetic pre-dispatch stop")
        original = engine._accepted_extract_page
        # Capture the intended inputs without dispatching or accepting a new
        # coverage task; then model an already-frozen historical task.
        with patch.object(engine, "_accepted_extract_page", side_effect=freeze_old):
            with self.assertRaises(RuntimeError):
                engine.extract_batch("old-policy")
        with closing(self.store.connect()) as db:
            # The intercepted pending job has never been dispatched. Remove
            # only this synthetic fixture so one historical identity remains.
            db.execute("DELETE FROM jobs WHERE kind='extract_coverage'")
            db.commit()
        old = engine.jobs.ensure("extract_coverage", frozen["inputs"], frozen["versions"])
        with patch("core.semantic.engine.extraction_prompt", wraps=extraction_prompt) as prompts:
            result = engine.extract_batch("old-policy")
        self.assertEqual(result["observations"][0]["scene_kind"], "life")
        self.assertEqual([call.kwargs["unit_policy"] for call in prompts.call_args_list], [None])
        with closing(self.store.connect()) as db:
            rows = list(db.execute("SELECT job_id,state FROM jobs WHERE kind='extract_coverage'"))
        self.assertEqual([tuple(row) for row in rows], [(old["job_id"], "done")])

    def test_coverage_does_not_overwrite_a_terminal_neighbour_disposition(self):
        record = capture(self.store, "terminal", ["含义不明的一句话。", "嗯", "补充说明：指的是美术馆。"])
        text = CoverageText(primary=lambda value: "美术馆" in value, coverage=lambda value: True,
                            noise=lambda value: value == "嗯")
        result = SemanticEngine(self.store, text, FakeDecision(possible=0)).process_batch("terminal")
        self.assertEqual(result["status"], "complete")
        statuses = self.dispositions()
        self.assertEqual(statuses[record["member_version_ids"][0]], "observed")
        self.assertEqual(statuses[record["member_version_ids"][1]], "noise")
        self.assertEqual(statuses[record["member_version_ids"][2]], "observed")

    def test_unknown_coverage_outcome_is_not_resent(self):
        capture(self.store, "unknown", ["含义不明的一句话。", "补充上下文。"])

        class UnknownCoverage(CoverageText):
            def generate(self, prompt, **kwargs):
                if prompt.startswith("The following captured") and "Bounded adjacent context" in prompt:
                    self.calls.append(kwargs["request_id"])
                    raise ProviderError("synthetic_coverage_unknown", retryable=True, outcome_unknown=True)
                return super().generate(prompt, **kwargs)

        text = UnknownCoverage(primary=lambda value: False)
        engine = SemanticEngine(self.store, text, FakeDecision())
        with self.assertRaisesRegex(ProviderError, "synthetic_coverage_unknown"):
            engine.extract_batch("unknown")
        sent = list(text.calls)
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            engine.extract_batch("unknown")
        self.assertEqual(text.calls, sent)
        with closing(self.store.connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM jobs WHERE kind='extract_coverage' AND state='unknown'").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM processing_receipts").fetchone()[0], 0)

    def test_still_unclassified_control_case_stays_unread_without_ack(self):
        capture(self.store, "control", ["始终无法理解的一句话。"])
        text = CoverageText(primary=lambda value: False, coverage=lambda value: False)
        result = SemanticEngine(self.store, text, FakeDecision()).process_batch("control")
        self.assertEqual(result["status"], "unclassified")
        self.assertIsNone(result.get("processing_receipt"))
        self.assertEqual(self.store.coverage()["unread_sources"], 1)
        with closing(self.store.connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM processing_receipts").fetchone()[0], 0)
            self.assertGreater(connection.execute(
                "SELECT count(*) FROM jobs WHERE kind='extract_coverage'").fetchone()[0], 0)

    def test_coverage_reuses_the_known_output_correction_path(self):
        capture(self.store, "correct", ["含义不明的一句话。", "补充上下文。"])

        class CorrectedCoverage(CoverageText):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.rejected = False

            def generate(self, prompt, **kwargs):
                if (prompt.startswith("The following captured")
                        and "Bounded adjacent context" in prompt and not self.rejected):
                    self.rejected = True
                    self.calls.append(kwargs["request_id"])
                    self.prompt_sizes.append(len(prompt.encode()))
                    return TextResult('{"observations":', self.route)
                return super().generate(prompt, **kwargs)

        text = CorrectedCoverage(primary=lambda value: False, coverage=lambda value: True)
        result = SemanticEngine(self.store, text, FakeDecision(possible=0)).process_batch("correct")
        self.assertEqual(result["status"], "complete")
        with closing(self.store.connect()) as connection:
            row = connection.execute("SELECT attempt,state FROM jobs WHERE kind='extract_coverage'").fetchone()
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM accepted_job_artifacts WHERE job_id="
                "(SELECT job_id FROM jobs WHERE kind='extract_coverage')").fetchone()[0], 1)
        self.assertEqual((row["attempt"], row["state"]), (2, "done"))

    def test_coverage_callbacks_see_only_bounded_adjacent_context(self):
        texts = ["清晰的技术说明 " + str(index) if index % 2 == 0 else "含义不明的生活片段 " + str(index)
                 for index in range(12)]
        capture(self.store, "many", texts)
        text = CoverageText(primary=lambda value: value.startswith("清晰"), coverage=lambda value: True)
        result = SemanticEngine(self.store, text, FakeDecision(possible=0)).process_batch("many")
        self.assertEqual(result["status"], "complete")
        with closing(self.store.connect()) as connection:
            jobs = [json.loads(row[0]) for row in connection.execute(
                "SELECT inputs_json FROM jobs WHERE kind='extract_coverage'")]
            self.assertGreater(connection.execute(
                "SELECT count(*) FROM jobs WHERE kind='extract_coverage'").fetchone()[0], 0)
        for inputs in jobs:
            self.assertLessEqual(len(inputs["context_source_ids"]), 4)
            self.assertTrue(set(inputs["context_source_ids"]).isdisjoint(inputs["core_source_ids"]))
        self.assertLess(max(text.prompt_sizes), 60000)

    def test_long_source_coverage_binds_every_unclassified_fragment(self):
        record = capture(self.store, "long-coverage",
                         ["第一段含义明确。" + "随后的段落含义不明但与主题相关。" * 1500])
        version = record["member_version_ids"][0]
        text = CoverageText(primary=lambda value: value.startswith("第一段含义明确。"),
                            coverage=lambda value: True)
        result = SemanticEngine(self.store, text, FakeDecision(possible=0)).process_batch("long-coverage")
        self.assertEqual(result["status"], "complete")
        with closing(self.store.connect()) as connection:
            spans = [row[0] for row in connection.execute(
                "SELECT span_id FROM source_spans WHERE source_version_id=?", (version,))]
            self.assertGreater(len(spans), 2)
            page = connection.execute("SELECT job_id FROM jobs WHERE kind='extract_page'").fetchone()[0]
            primary_artifact = json.loads(connection.execute(
                "SELECT artifact_json FROM accepted_job_artifacts WHERE job_id=?", (page,)).fetchone()[0])
            coverage = list(connection.execute(
                "SELECT j.inputs_json,a.artifact_json FROM jobs j "
                "JOIN accepted_job_artifacts a USING(job_id) WHERE j.kind='extract_coverage'"))
            missing = connection.execute(
                "SELECT count(*) FROM source_spans s WHERE s.source_version_id=? AND NOT EXISTS "
                "(SELECT 1 FROM observation_evidence e WHERE e.span_id=s.span_id)", (version,)).fetchone()[0]
        self.assertGreaterEqual(len(coverage), 2)
        self.assertEqual(missing, 0)
        self.assertEqual(text.calls.count(page + ":text"), 1)
        self.assertLess(max(text.prompt_sizes), 100000)
        primary_cited = {span["span_id"] for observation in primary_artifact["observations"]
                         for span in observation["spans"]}
        self.assertTrue(primary_cited)
        covered = set()
        for inputs_json, artifact_json in coverage:
            inputs, artifact = json.loads(inputs_json), json.loads(artifact_json)
            covered.update(inputs["span_ids"])
            cited = {span["source_id"] for observation in artifact["observations"] for span in observation["spans"]}
            self.assertTrue(cited <= set(inputs["core_source_ids"]))
            self.assertTrue(set(inputs["context_source_ids"]).isdisjoint(inputs["core_source_ids"]))
        # Every still-unclassified fragment is bound by the coverage pass and
        # none of the already cited primary spans is processed twice.
        self.assertEqual(covered, set(spans) - primary_cited)


if __name__ == "__main__":
    unittest.main()
