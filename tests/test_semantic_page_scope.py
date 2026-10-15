"""Only exact, known non-core references may leave the core validation path."""
import json
import unittest

from core.semantic.engine import (CapturedSource, SemanticValidationError,
                                  prepare_spans, scope_page_output, validate_dispositions, validate_extraction)
from tests.test_semantic_engine import observation


class PageScopeTests(unittest.TestCase):
    def setUp(self):
        self.core = CapturedSource('core', 'A current scene')
        self.context = CapturedSource('context', 'An earlier scene', core=False)
        self.sources = [self.core, self.context]
        self.spans = tuple(span.span_id for span in prepare_spans(self.sources))

    def test_only_context_output_cannot_finish_an_omitted_core_member(self):
        raw = json.dumps({'observations': [observation(self.context)]})
        scoped, excluded = scope_page_output(raw, self.sources, namespace='semantic', span_ids=self.spans)
        self.assertEqual(len(excluded), 1)
        extracted = validate_extraction(scoped, self.sources)
        self.assertEqual(extracted, ())
        self.assertEqual(validate_dispositions(scoped, self.sources, extracted)['core']['status'], 'unclassified')

    def test_unknown_or_mispaired_context_refs_are_still_rejected(self):
        for bad_ref in ({'source_id': 'invented', 'span_id': self.spans[1]},
                        {'source_id': 'context', 'span_id': self.spans[0]},
                        {'source_id': 'context', 'span_id': 'invented'}):
            with self.subTest(bad_ref=bad_ref):
                value = observation(self.context)
                value['spans'] = [bad_ref]
                scoped, excluded = scope_page_output(json.dumps({'observations': [value]}), self.sources,
                                                     namespace='semantic', span_ids=self.spans)
                self.assertEqual(excluded, [])
                with self.assertRaisesRegex(SemanticValidationError, 'semantic_invalid_source_span'):
                    validate_extraction(scoped, self.sources)
