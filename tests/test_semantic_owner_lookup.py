"""Extraction ownership reads stay proportional to the current source batch."""
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core.semantic.engine import SemanticEngine
from core.semantic.store import SemanticStore
from ai.semantic_provider import TextResult
from tests.test_semantic_engine import FakeDecision, FakeText, capture


class ExtractionOwnerLookupTests(unittest.TestCase):
    def test_context_fragment_is_retained_without_inventing_an_item(self):
        class ContextText(FakeText):
            def generate(self, prompt, **kwargs):
                sources = json.loads(prompt.rsplit('\n', 1)[-1])
                return TextResult(json.dumps({'observations': [], 'dispositions': {
                    source['source_id']: {'status': 'context_only',
                        'reason': 'Unclear reference retained without an independent assertion'}
                    for source in sources if source['core']}}), self.route)
        with tempfile.TemporaryDirectory() as folder:
            store = SemanticStore(Path(folder).resolve() / 'knowledge.sqlite', 'generation')
            receipt = capture(store, 'fragment', ['An unresolved conversational reference'])
            result = SemanticEngine(store, ContextText(), FakeDecision()).process_batch('fragment')
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(store.get_batch('fragment')['members'][0]['text'],
                             'An unresolved conversational reference')
            self.assertEqual(store.list_items(), [])
            self.assertTrue(receipt['member_version_ids'])

    def test_unrelated_history_is_not_materialized_for_a_new_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SemanticStore(Path(folder).resolve() / 'knowledge.sqlite', 'generation')
            engine = SemanticEngine(store, FakeText(), FakeDecision())
            capture(store, 'history', ['Earlier independent scene ' + str(i) for i in range(40)])
            engine.extract_batch('history')
            capture(store, 'current', ['An unrelated new scene'])
            original_connect = store.connect
            read_rows = []

            def tracked_connect():
                connection = original_connect()
                def factory(cursor, row):
                    if tuple(column[0] for column in cursor.description) == ('source_version_id', 'job_id'):
                        read_rows.append(row[0])
                    return sqlite3.Row(cursor, row)
                connection.row_factory = factory
                return connection

            with patch.object(store, 'connect', tracked_connect):
                result = engine.extract_batch('current')
            self.assertEqual(len(result['observations']), 1)
            self.assertLessEqual(len(read_rows), 1)

