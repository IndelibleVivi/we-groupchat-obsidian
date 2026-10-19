"""Only exact, known non-core references may leave the core validation path."""
import json
import unittest

from core.semantic.engine import (CapturedSource, SemanticValidationError,
                                  bind_indexed_extraction, prepare_spans, scope_page_output,
                                  validate_dispositions, validate_extraction)
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

    def test_indexed_choices_bind_before_original_context_scope_and_keep_wire_error_paths(self):
        values = [{"title": "Context alone", "scene_kind": "life", "evidence_indices": [1],
                   "units": [{"kind": "quote", "evidence_indices": [1]}], "topics": [], "claims": []},
                  {"title": "Current scene", "scene_kind": "life", "evidence_indices": [0],
                   "units": [{"kind": "quote", "evidence_indices": [0]}], "topics": [], "claims": []}]
        converted = bind_indexed_extraction({"observations": values}, self.sources, span_ids=self.spans)
        scoped, excluded = scope_page_output(converted, self.sources, namespace="semantic", span_ids=self.spans)
        self.assertEqual(excluded[0]["spans"], [{"source_id": "context", "span_id": self.spans[1]}])
        extracted = validate_extraction(scoped, self.sources)
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0].units[0].text, self.core.text)
        values[1]["units"][0]["evidence_indices"] = [2]
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_evidence_reference") as caught:
            bind_indexed_extraction({"observations": values}, self.sources, span_ids=self.spans)
        self.assertEqual(caught.exception.diagnostic["path"], ["observations", 1, "units", 0, "evidence_indices", 0])
