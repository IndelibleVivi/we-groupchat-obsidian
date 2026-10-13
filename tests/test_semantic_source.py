import json
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from core.monitor_source import MonitorSourceError
from core.semantic.source import SemanticCapture
from core.semantic.store import SemanticStore
from tests.test_monitor_source import CursorDB, raw_message


class Source(CursorDB):
    def get_source_inventory(self, update=True):
        return {**super().get_source_inventory(update=update), "source_namespace": "source-fixture"}


class SemanticCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SemanticStore(Path(self.tmp.name).resolve() / "knowledge.sqlite", "test-generation")
        self.source = Source({"one": ("g1", [raw_message("g1", 1, 12, "A"),
                                                raw_message("g1", 2, 12, "same-second")]),
                              "two": ("g2", [raw_message("g2", 1, 11, "earlier")])})
        self.capture = SemanticCapture(self.store, self.source, page_size=1)

    def test_snapshot_keeps_raw_order_and_exact_pass_receipts(self):
        result = self.capture.snapshot(["chat"], snapshot_id="C0")
        self.assertEqual(result["capture_seq"], 3)
        self.assertTrue(result["source_inventory"]["complete"])
        self.assertTrue(result["batches"][-1]["source_eof"])
        self.assertTrue(all(x["purpose"] == "snapshot:C0" for x in result["batches"]))
        with closing(self.store.connect()) as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT text FROM source_records ORDER BY capture_seq")],
                             ["earlier", "A", "same-second"])
        self.assertEqual(json.loads(result["h1"][0]["cursors"]["one"]["cursor_token"]), [12, 2])

    def test_full_second_pass_captures_late_old_rows_and_a_b_a(self):
        self.capture.snapshot(["chat"], snapshot_id="C0")
        self.source.shards["one"]["messages"][0]["text"] = "B"
        self.source.shards["one"]["messages"].append(raw_message("g1", 3, 5, "late"))
        self.source.shards["one"]["messages"].sort(key=lambda row: (row["timestamp"], row["source_envelope"]["rowid"]))
        c1 = self.capture.snapshot(["chat"], snapshot_id="C1")
        next(row for row in self.source.shards["one"]["messages"] if row["source_message_id"] == "msg-g1-1")["text"] = "A"
        self.capture.snapshot(["chat"], snapshot_id="C2")
        with closing(self.store.connect()) as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT text FROM source_records WHERE source_id='msg-g1-1' ORDER BY capture_seq")], ["A", "B", "A"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_records WHERE text='late'").fetchone()[0], 1)
        self.assertEqual(c1["capture_seq"], 5)

    def test_incomplete_inventory_never_creates_canonical_capture(self):
        self.source.complete = False
        with self.assertRaises(MonitorSourceError):
            self.capture.snapshot(["chat"], snapshot_id="bad")
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM capture_batches").fetchone()[0], 0)

    def test_native_image_metadata_is_bound_without_attachment_io_or_raw_mutation(self):
        from core.wechat_db import WeChatDB
        from unittest.mock import patch
        row = self.source.shards['one']['messages'][0]
        metadata = WeChatDB._resource_metadata(object(), '<msg><img md5="' + 'a' * 32 + '" /></msg>', 3, None)
        for index, value in enumerate(metadata):
            value.update(resource_index=index, source_message_id=row['source_message_id'])
        row['resources'] = metadata
        original = json.loads(json.dumps(row))
        with patch('core.attachment_archive.AttachmentArchive', side_effect=AssertionError('no attachment IO')):
            self.capture.snapshot(['chat'], snapshot_id='image-C0')
        with closing(self.store.connect()) as conn:
            source = conn.execute('SELECT source_version_id,payload_json FROM source_records WHERE source_id=?', (row['source_message_id'],)).fetchone()
        self.assertEqual(json.loads(source['payload_json']), original)
        resource = self.store.get_source(source['source_version_id'])['resources'][0]
        self.assertEqual(resource['metadata'], metadata[0])
        self.assertEqual(resource['status'], 'ready_metadata')
        self.assertIsNone(resource['location'])
        self.capture.snapshot(['chat'], snapshot_id='image-C1')
        self.assertEqual(self.store.get_source(source['source_version_id'])['resources'], [resource])

    def test_resource_occurrences_do_not_merge_by_declared_hash(self):
        from core.semantic.source import message_resources
        metadata = {'kind': 'file', 'resource_index': 0, 'declared_hash': 'a' * 32}
        first = message_resources({'source_message_id': 'first', 'resources': [metadata]}, 'namespace')
        second = message_resources({'source_message_id': 'second', 'resources': [metadata]}, 'namespace')
        self.assertNotEqual(first[0]['resource_id'], second[0]['resource_id'])
        self.assertNotEqual(first[0]['occurrence_id'], second[0]['occurrence_id'])
        self.assertNotEqual(first[0]['resource_id'], message_resources({'source_message_id': 'first', 'resources': [metadata]}, 'other')[0]['resource_id'])


if __name__ == "__main__":
    unittest.main()
