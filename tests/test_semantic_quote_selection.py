"""Source-owned quote text; models select existing spans without copying words."""
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ai.semantic_provider import TextResult
from core.semantic.engine import CapturedSource, SemanticEngine, SemanticValidationError, prepare_spans, validate_extraction
from core.semantic.store import SemanticStore
from tests.test_semantic_engine import FakeDecision, FakeText, capture, observation


class QuoteSelectionTests(unittest.TestCase):
    def test_contiguous_program_spans_supply_exact_quote_words(self):
        source = CapturedSource('source', '原话🙂' * 1200)
        spans = prepare_spans([source])
        raw = observation(source, unit_kind='quote')
        raw['spans'] = [{'source_id': span.source_id, 'span_id': span.span_id} for span in spans]
        raw['units'] = [{'kind': 'quote', 'span_indices': [0, 1]}]
        result = validate_extraction({'observations': [raw]}, [source])[0]
        self.assertEqual(result.units[0].text, spans[0].excerpt + spans[1].excerpt)
        self.assertEqual(result.units[0].span_ids, (spans[0].span_id, spans[1].span_id))

    def test_separate_messages_and_noncontiguous_spans_cannot_be_joined_as_one_quote(self):
        first = CapturedSource('first', '原文' * 3000)
        second = CapturedSource('second', '另一人的原文')
        spans = prepare_spans([first, second])
        for selected in ((0, 2), (0, len(spans) - 1)):
            with self.subTest(selected=selected):
                raw = observation(first, unit_kind='quote')
                raw['spans'] = [{'source_id': span.source_id, 'span_id': span.span_id} for span in spans]
                raw['units'] = [{'kind': 'quote', 'span_indices': list(selected)}]
                with self.assertRaisesRegex(SemanticValidationError, 'semantic_invalid_quote_selection'):
                    validate_extraction({'observations': [raw]}, [first, second])

    def test_selected_quote_is_durable_before_canonical_write_and_keeps_speaker_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SemanticStore(Path(temporary).resolve() / 'knowledge.sqlite', 'generation')
            passage = '回滚后本地缓存需要重新生成。'
            payload = {'raw_sender_id': 'wxid_synthetic', 'sender': '小林'}
            captured = capture(store, 'batch', [passage], payloads=[payload])

            class SelectQuote(FakeText):
                def generate(self, prompt, **kwargs):
                    response = super().generate(prompt, **kwargs)
                    raw = json.loads(response.text)
                    for value in raw['observations']:
                        for unit in value['units']:
                            unit['kind'] = 'quote'
                            unit.pop('text')
                    return TextResult(json.dumps(raw), self.route)

            extracted = SemanticEngine(store, SelectQuote(), FakeDecision()).extract_batch('batch')
            self.assertEqual(extracted['observations'][0]['units'][0]['text'], passage)
            with closing(store.connect()) as connection:
                artifact = json.loads(connection.execute("SELECT artifact_json FROM jobs "
                    "JOIN accepted_job_artifacts USING(job_id) WHERE kind='extract_page'").fetchone()[0])
                self.assertEqual(artifact['observations'][0]['units'][0]['text'], passage)
                self.assertEqual(connection.execute('SELECT text FROM content_units').fetchone()[0], passage)
            self.assertEqual(store.get_source(captured['member_version_ids'][0])['payload'], payload)
