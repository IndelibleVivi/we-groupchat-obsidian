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
