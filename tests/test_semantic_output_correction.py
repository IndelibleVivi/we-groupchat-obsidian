"""Known rejected JSON may be corrected; unknown outcomes cannot be replayed."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from ai.semantic_provider import ProviderError, TextResult
from core.semantic.engine import SemanticEngine, SemanticValidationError
from core.semantic.store import SemanticStore
from tests.test_semantic_engine import FakeDecision, FakeText, capture


class OutputCorrectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SemanticStore(Path(temporary.name).resolve() / 'knowledge.sqlite', 'generation')
        capture(self.store, 'batch', ['A complete synthetic scene'])

    def test_known_bad_json_gets_a_new_attempt_and_preserves_one_artifact(self):
        class Correctable(FakeText):
            def generate(self, prompt, **kwargs):
                if not self.calls:
                    self.calls.append(kwargs['request_id'])
                    return TextResult('{"observations":', self.route)
                self.correction_prompt = prompt
                return super().generate(prompt, **kwargs)
        text = Correctable()
        engine = SemanticEngine(self.store, text, FakeDecision())
        result = engine.extract_batch('batch')
        self.assertEqual(len(result['observations']), 1)
        self.assertEqual(len(text.calls), 2)
        self.assertNotEqual(text.calls[0], text.calls[1])
        self.assertIn('semantic_invalid_json', text.correction_prompt)
        with closing(self.store.connect()) as connection:
            page = connection.execute("SELECT job_id,attempt,state FROM jobs WHERE kind='extract_page'").fetchone()
            self.assertEqual((page['attempt'], page['state']), (2, 'done'))
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM accepted_job_artifacts WHERE job_id=?',
                                                (page['job_id'],)).fetchone()[0], 1)
        calls = list(text.calls)
        self.assertEqual(engine.extract_batch('batch')['receipt'], result['receipt'])
        self.assertEqual(text.calls, calls)

    def test_invalid_output_exhausts_two_corrections_without_commit(self):
        class Invalid(FakeText):
            def generate(self, prompt, **kwargs):
                self.calls.append(kwargs['request_id'])
                return TextResult('{', self.route)
        text = Invalid()
        with self.assertRaisesRegex(SemanticValidationError, 'semantic_invalid_json'):
            SemanticEngine(self.store, text, FakeDecision()).extract_batch('batch')
        self.assertEqual(len(text.calls), 3)
        self.assertEqual(len(set(text.calls)), 3)
        with closing(self.store.connect()) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM accepted_job_artifacts').fetchone()[0], 0)

    def test_unknown_outcome_is_not_corrected_or_bypassed_on_resume(self):
        class Unknown(FakeText):
            def generate(self, prompt, **kwargs):
                self.calls.append(kwargs['request_id'])
                raise ProviderError('provider_request_failed', outcome_unknown=True)
        text = Unknown()
        engine = SemanticEngine(self.store, text, FakeDecision())
        with self.assertRaises(ProviderError):
            engine.extract_batch('batch')
        with self.assertRaisesRegex(SemanticValidationError, 'semantic_job_busy_or_unknown'):
            engine.extract_batch('batch')
        self.assertEqual(len(text.calls), 1)

    def test_attributed_quote_wrapper_is_corrected_to_the_exact_passage(self):
        payload = {"raw_sender_id": "wxid_synthetic_speaker", "sender": "小林"}
        passage = "版本回滚以后本地缓存需要重新生成。"
        record = capture(self.store, 'quoted', [passage], payloads=[payload])

        class WrappedQuote(FakeText):
            def __init__(self):
                super().__init__()
                self.prompts = []

            def generate(self, prompt, **kwargs):
                if not prompt.startswith('The following captured'):
                    return super().generate(prompt, **kwargs)
                self.calls.append(kwargs['request_id'])
                self.prompt_sizes.append(len(prompt.encode()))
                self.prompts.append(prompt)
                data = json.loads(prompt.rsplit('\n', 1)[-1])
                quoted = passage if 'Correct the prior JSON' in prompt else '小林说：“' + passage + '”'
                values = [{"title": "署名包裹的引语", "scene_kind": "quote",
                           "spans": [{"source_id": source["source_id"], "span_id": span["span_id"]}
                                     for span in source["spans"]],
                           "units": [{"kind": "quote", "text": quoted,
                                      "span_indices": list(range(len(source["spans"])))}],
                           "topics": [], "claims": []}
                          for source in data if source["core"]]
                return TextResult(json.dumps({"observations": values}, ensure_ascii=False), self.route)

        text = WrappedQuote()
        result = SemanticEngine(self.store, text, FakeDecision()).extract_batch('quoted')
        self.assertEqual(len(result['observations']), 1)
        self.assertEqual(result['observations'][0]['units'][0]['text'], passage)
        self.assertEqual(len(text.calls), 2)
        self.assertIn('semantic_quote_not_grounded', text.prompts[1])
        self.assertIn('verbatim', text.prompts[1])
        with closing(self.store.connect()) as connection:
            quotes = [row[0] for row in connection.execute(
                "SELECT text FROM content_units WHERE kind='quote'")]
        self.assertEqual(quotes, [passage])
        self.assertEqual(self.store.get_source(record['member_version_ids'][0])['payload'], payload)
