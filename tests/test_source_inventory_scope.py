"""Inventory scope contract for one bounded source traversal.

A traversal that re-observed the source inventory through the public getter on
every page made O(pages) independent observations, so any single transiently
incomplete or unreadable observation failed the whole pass. These tests pin the
contract: one frozen scoped inventory reused by every page of a traversal, plus
exactly one independent fresh final verification that still fails closed when
the source changed or is incomplete. The reduced verification is an explicit
traversal-owner decision, not an automatic property of being nested.
"""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from core.monitor_source import MonitorSourceError, read_monitor_source_batch
from core.semantic.source import SemanticCapture
from core.semantic.store import SemanticStore
from tests.test_wechat_db_page_index import _ShardFixture, _disordered_rows


class SourceInventoryScopeTests(unittest.TestCase):
    def _source(self, rows, *, username="scope@chatroom", page_size=1):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fixture = _ShardFixture(tmp.name, username=username)
        fixture.insert(rows)
        db = fixture.mkdb()
        store = SemanticStore(
            Path(tmp.name).resolve() / "knowledge.sqlite", "generation"
        )
        return fixture, db, SemanticCapture(store, db, page_size=page_size)

    def _counting_inventory(self, db, *, rewrite_fresh):
        """Wrap ``get_source_inventory`` and mutate only the fresh observation."""
        real = db.get_source_inventory
        calls = {"frozen": 0, "fresh": 0}

        def wrapped(*, update=True, sensitive=False, reuse_scoped=False):
            if reuse_scoped:
                calls["frozen"] += 1
                return real(update=update, sensitive=sensitive, reuse_scoped=True)
            calls["fresh"] += 1
            return rewrite_fresh(
                real(update=update, sensitive=sensitive), calls["fresh"]
            )

        db.get_source_inventory = wrapped
        return calls

    def test_multi_page_snapshot_reuses_one_frozen_inventory(self):
        fixture, db, capture = self._source(_disordered_rows(900), page_size=60)

        result = capture.snapshot([fixture.username], snapshot_id="C0")

        pages = len(result["batches"])
        self.assertGreater(pages, 5)
        self.assertEqual(result["capture_seq"], 900)
        stats = db.source_stage_stats()
        # One frozen read plus one independent fresh final observation, no
        # matter how many pages the traversal read.
        self.assertEqual(stats["inventory_scans"], 2)
        self.assertGreaterEqual(stats["inventory_reuses"], pages)
        # Traversal exit clears the frozen inventory and specs cache.
        state = db._source_snapshot_state()
        self.assertIsNone(state["inventory_snapshot"])
        self.assertIsNone(state["inventory_specs"])

    def test_scan_count_does_not_grow_with_the_page_count(self):
        small_fixture, small_db, small = self._source(
            _disordered_rows(120), page_size=60
        )
        large_fixture, large_db, large = self._source(
            _disordered_rows(900), page_size=60
        )

        small_result = small.snapshot([small_fixture.username], snapshot_id="C0")
        large_result = large.snapshot([large_fixture.username], snapshot_id="C0")
        self.assertLess(len(small_result["batches"]), len(large_result["batches"]))
        self.assertEqual(small_db.source_stage_stats()["inventory_scans"], 2)
        self.assertEqual(large_db.source_stage_stats()["inventory_scans"], 2)

        # The legacy shape -- a fresh observation per page -- still scales with
        # the page count, which is exactly the observation this fix removes.
        shard = large_db.get_message_shards(large_fixture.username)[0]
        with large_db.source_snapshot():
            baseline = large_db.source_stage_stats()["inventory_scans"]
            pages = 0
            token = ""
            while True:
                large_db.get_source_inventory(update=True)
                page = large_db.get_cursor_page_for_shard(
                    large_fixture.username,
                    shard,
                    cursor_token=token,
                    since_ts=0,
                    limit=60,
                )
                pages += 1
                token = page["next_cursor"]
                if page["exhausted"]:
                    break
        grew = large_db.source_stage_stats()["inventory_scans"] - baseline
        self.assertEqual(pages, len(large_result["batches"]))
        self.assertEqual(grew, pages)

    def test_late_inventory_change_fails_closed_without_snapshot_receipt(self):
        fixture, db, capture = self._source(_disordered_rows(300), page_size=100)

        def changed(snapshot, _call):
            snapshot = dict(snapshot)
            snapshot["inventory_digest"] = "f" * len(snapshot["inventory_digest"])
            return snapshot

        calls = self._counting_inventory(db, rewrite_fresh=changed)
        with self.assertRaises(MonitorSourceError) as raised:
            capture.snapshot([fixture.username], snapshot_id="C0")

        self.assertEqual(raised.exception.code, "source_generation_changed")
        # Exactly one independent fresh final observation, and pages never
        # observed the inventory fresh.
        self.assertEqual(calls["fresh"], 1)
        self.assertGreater(calls["frozen"], 0)
        # The failed pass returns no snapshot receipt and leaves no canonical
        # or processing receipt behind.
        with closing(capture.store.connect()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM processing_receipts").fetchone()[0],
                0,
            )
        # The error path still releases the frozen traversal state.
        self.assertIsNone(db._source_snapshot_state()["inventory_snapshot"])

    def test_late_incomplete_inventory_fails_closed(self):
        fixture, db, capture = self._source(_disordered_rows(120), page_size=60)

        def incomplete(snapshot, _call):
            snapshot = dict(snapshot)
            snapshot["complete"] = False
            return snapshot

        self._counting_inventory(db, rewrite_fresh=incomplete)
        with self.assertRaises(MonitorSourceError) as raised:
            capture.snapshot([fixture.username], snapshot_id="C0")

        self.assertEqual(raised.exception.code, "source_inventory_incomplete")

    def test_standalone_batch_owns_one_fresh_final_verification(self):
        fixture, db, _capture = self._source(_disordered_rows(120), page_size=60)

        def changed(snapshot, _call):
            snapshot = dict(snapshot)
            snapshot["inventory_digest"] = "a" * len(snapshot["inventory_digest"])
            return snapshot

        calls = self._counting_inventory(db, rewrite_fresh=changed)
        with self.assertRaises(MonitorSourceError) as raised:
            read_monitor_source_batch(
                db,
                fixture.username,
                {"last_checked_ts": 0},
                raw_limit=200,
                page_size=60,
            )

        self.assertEqual(raised.exception.code, "source_generation_changed")
        self.assertEqual(calls["fresh"], 1)

    def test_nested_batch_keeps_its_own_fresh_verification_by_default(self):
        fixture, db, _capture = self._source(_disordered_rows(120), page_size=60)

        def changed(snapshot, _call):
            snapshot = dict(snapshot)
            snapshot["inventory_digest"] = "b" * len(snapshot["inventory_digest"])
            return snapshot

        calls = self._counting_inventory(db, rewrite_fresh=changed)
        # Nesting alone never disables the verification: an enclosing scope
        # proves nothing about a fresh final observation, so a batch that does
        # not explicitly opt out still owns one.
        with db.source_snapshot():
            with self.assertRaises(MonitorSourceError) as raised:
                read_monitor_source_batch(
                    db,
                    fixture.username,
                    {"last_checked_ts": 0},
                    raw_limit=200,
                    page_size=60,
                )
        self.assertEqual(raised.exception.code, "source_generation_changed")
        self.assertEqual(calls["fresh"], 1)

    def test_standalone_page_verifies_fresh_and_leaks_no_frozen_state(self):
        fixture, db, capture = self._source(_disordered_rows(120), page_size=60)

        def changed(snapshot, _call):
            snapshot = dict(snapshot)
            snapshot["inventory_digest"] = "c" * len(snapshot["inventory_digest"])
            return snapshot

        self._counting_inventory(db, rewrite_fresh=changed)
        with self.assertRaises(MonitorSourceError) as raised:
            capture.page(fixture.username, {}, snapshot_id="live")

        self.assertEqual(raised.exception.code, "source_generation_changed")
        state = db._source_snapshot_state()
        self.assertIsNone(state["inventory_snapshot"])
        self.assertIsNone(state["inventory_specs"])

    def test_legacy_source_without_scoped_reuse_keeps_the_fresh_read(self):
        from tests.test_monitor_source import CursorDB, raw_message

        db = CursorDB({
            "logical-a": (
                "generation-a",
                [raw_message("generation-a", 1, 11, "可见消息")],
            ),
        })
        batch = read_monitor_source_batch(
            db, "chat@chatroom", {"last_checked_ts": 10}, raw_limit=10
        )

        self.assertEqual(batch.raw_count, 1)
        self.assertTrue(batch.source_eof)
        self.assertGreaterEqual(db.inventory_calls, 1)


if __name__ == "__main__":
    unittest.main()
