"""Contract tests for the canonical read-source seam."""
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest

from core import source_adapter
from core.source_adapter import (
    SOURCE_ERROR_CODES,
    SourceUnavailableError,
    bind_source_inventory,
    decode_cursor_token,
    encode_cursor_token,
    normalize_source_error,
    read_source_page,
    source_capabilities,
    source_cycle_stage_stats,
    source_stage_stats,
    format_source_stage_summary,
    source_snapshot,
)
from core.wechat_db import WeChatDB, WeChatSourceDegraded


class _LegacySource:
    """A reader that only exposes the timestamp/identity page contract."""

    def __init__(self, rows=None, *, shards=("shard-1",), fail_shards=()):
        self.rows = list(rows or [])
        self.shards = list(shards)
        self.fail_shards = set(fail_shards)
        self.requested_limits = []

    def get_message_shards(self, _username):
        if self.shards and set(self.shards) <= self.fail_shards:
            raise SourceUnavailableError("source_shard_unavailable")
        return list(self.shards)

    def get_messages_for_shard(
        self,
        _username,
        source_shard_id,
        *,
        since_ts=0,
        limit=500,
        page_forward=False,
        since_inclusive=False,
    ):
        if source_shard_id in self.fail_shards:
            raise SourceUnavailableError("source_shard_unavailable")
        self.requested_limits.append(limit)
        rows = [
            dict(row)
            for row in self.rows
            if (
                int(row["timestamp"]) >= since_ts
                if since_inclusive
                else int(row["timestamp"]) > since_ts
            )
        ]
        rows.sort(key=lambda row: int(row["timestamp"]))
        return rows[:limit]


def _message(identity, timestamp):
    return {
        "timestamp": timestamp,
        "source_message_id": identity,
        "text": "body",
    }


class CapabilityTests(unittest.TestCase):
    def test_capabilities_are_bounded_and_structural(self):
        legacy = _LegacySource()
        self.assertEqual(source_capabilities(None), frozenset())
        self.assertEqual(source_capabilities(legacy), frozenset({"shard_pages"}))

        full = source_adapter.WeChatSource
        self.assertTrue(hasattr(full, "get_cursor_page_for_shard"))

    def test_supports_monitor_source_cursors_needs_inventory_and_keyset(self):
        source = _LegacySource()
        self.assertFalse(source_adapter.supports_monitor_source_cursors(source))

        source.get_cursor_page_for_shard = lambda *a, **k: {}
        self.assertFalse(source_adapter.supports_monitor_source_cursors(source))

        source.get_source_inventory = lambda **k: {}
        self.assertTrue(source_adapter.supports_monitor_source_cursors(source))

        del source.get_source_inventory
        self.assertFalse(source_adapter.supports_monitor_source_cursors(source))
        self.assertFalse(source_adapter.supports_monitor_source_cursors(None))

    def test_monitor_module_reexports_the_canonical_capability_check(self):
        from core import monitor_source

        source = _LegacySource()
        self.assertIs(
            monitor_source.supports_monitor_source_cursors,
            source_adapter.supports_monitor_source_cursors,
        )
        self.assertFalse(monitor_source.supports_monitor_source_cursors(source))


class CursorTokenTests(unittest.TestCase):
    def test_round_trip(self):
        token = encode_cursor_token(100, 7)
        self.assertEqual(token, "[100,7]")
        self.assertEqual(decode_cursor_token(token), (100, 7))

    def test_empty_token_uses_the_timestamp_floor(self):
        self.assertEqual(decode_cursor_token(""), (0, 0))
        self.assertEqual(decode_cursor_token("", since_ts=42), (42, 0))
        self.assertEqual(decode_cursor_token("", since_ts=-5), (0, 0))

    def test_malformed_token_is_a_source_failure_not_an_empty_page(self):
        # Extra trailing elements stay tolerated: the pre-existing decoder read
        # the first two entries, and tightening that is not this change's scope.
        for token in ("{", "[1]", "1", "[\"a\",\"b\"]", "null", "{}"):
            with self.subTest(token=token):
                with self.assertRaises(SourceUnavailableError) as raised:
                    decode_cursor_token(token)
                self.assertEqual(raised.exception.code, "source_cursor_invalid")


class ErrorVocabularyTests(unittest.TestCase):
    def test_emitted_codes_are_preserved(self):
        for code in (
            "source_shard_unavailable",
            "source_shard_unknown",
            "source_message_decode_failed",
            "source_cursor_invalid",
            "source_snapshot_failed",
            "source_cache_only",
            "source_key_missing",
            "source_inventory_incomplete",
            "source_inventory_corrupt",
            "source_generation_changed",
        ):
            with self.subTest(code=code):
                self.assertEqual(
                    normalize_source_error(SourceUnavailableError(code)), code
                )
                self.assertIn(code, SOURCE_ERROR_CODES)

    def test_unknown_and_untyped_failures_are_content_free(self):
        self.assertEqual(
            normalize_source_error(RuntimeError("/Users/me/wechat/message_0.db")),
            "source_shard_unavailable",
        )
        self.assertEqual(
            normalize_source_error(OSError("secret path"), fallback="source_unavailable"),
            "source_unavailable",
        )

        class _Leaky(RuntimeError):
            code = "wechat/message/message_1.db"

        self.assertEqual(
            normalize_source_error(_Leaky()), "source_shard_unavailable"
        )


class InventoryBindingTests(unittest.TestCase):
    def test_missing_source_reports_unavailable_evidence(self):
        binding = bind_source_inventory(None, [{"username": "chat"}])
        self.assertFalse(binding["complete"])
        self.assertEqual(binding["error_code"], "source_unavailable")
        self.assertEqual(binding["degraded_shards"], 1)
        self.assertEqual(binding["shards_by_username"], {})
        self.assertEqual(
            binding["evidence"]["error_codes"], ["source_unavailable"]
        )

    def test_inventory_reader_maps_present_generations_to_every_chat(self):
        class _InventorySource(_LegacySource):
            def get_source_inventory(self, *, update=True, sensitive=False):
                del update, sensitive
                return {
                    "schema": "we-groupchat-obsidian.source-inventory.v1",
                    "source_namespace": "opaque",
                    "inventory_revision": 3,
                    "inventory_digest": "d" * 64,
                    "complete": False,
                    "counts": {"present": 1, "missing_file": 1},
                    "error_codes": ["source_missing_file"],
                    "present_generation_ids": ["generation-a"],
                }

        chats = [{"username": "chat-a"}, {"username": "chat-b"}]
        binding = bind_source_inventory(_InventorySource(), chats)

        self.assertFalse(binding["complete"])
        self.assertEqual(binding["error_code"], "source_missing_file")
        self.assertEqual(binding["inventory_revision"], 3)
        self.assertEqual(binding["inventory_digest"], "d" * 64)
        self.assertEqual(
            binding["shards_by_username"],
            {"chat-a": ["generation-a"], "chat-b": ["generation-a"]},
        )
        self.assertEqual(binding["degraded_shards"], 1)
        self.assertNotIn(str(chats), json.dumps(binding))

    def test_inventory_reader_failure_is_content_free(self):
        class _FailingSource(_LegacySource):
            def get_source_inventory(self, *, update=True, sensitive=False):
                del update, sensitive
                raise SourceUnavailableError("source_inventory_corrupt")

        binding = bind_source_inventory(_FailingSource(), [{"username": "chat"}])
        self.assertEqual(binding["error_code"], "source_inventory_corrupt")
        self.assertEqual(binding["degraded_shards"], 1)
        self.assertEqual(
            binding["evidence"]["error_codes"], ["source_inventory_corrupt"]
        )

    def test_legacy_reader_keeps_per_chat_shards_and_partial_policy(self):
        source = _LegacySource(shards=("shard-1",), fail_shards=("shard-1",))
        binding = bind_source_inventory(
            source, [{"username": "chat", "chat_key": "ck"}]
        )

        self.assertFalse(binding["complete"])
        self.assertEqual(binding["degraded_shards"], 1)
        self.assertEqual(binding["error_code"], "source_shard_unavailable")
        self.assertEqual(binding["shards_by_username"], {"chat": []})
        self.assertEqual(binding["evidence"]["schema"], "legacy-source-adapter.v1")
        self.assertEqual(len(binding["evidence"]["inventory_digest"]), 64)

    def test_degraded_evidence_never_carries_source_identity(self):
        source = _LegacySource(shards=("shard-1",), fail_shards=("shard-1",))
        binding = bind_source_inventory(source, [{"username": "chat", "chat_key": "ck"}])
        stored = source_adapter.source_state_evidence(binding)

        self.assertEqual(stored["source_state"], "source_degraded")
        self.assertEqual(stored["source_error_code"], "source_shard_unavailable")
        self.assertNotIn("chat", stored["source_inventory_evidence"])


class SourcePageTests(unittest.TestCase):
    def test_legacy_fallback_drains_a_larger_same_timestamp_bucket(self):
        # Five rows share one timestamp; the page size is two. The legacy
        # contract has no opaque position, so the request must widen with the
        # identities already consumed. A capped request would return no rows
        # here and claim EOF while rows two to four were still unseen.
        rows = [_message(f"m{index}", 10) for index in range(5)]
        source = _LegacySource(rows)
        seen_ids = set()
        drained = []
        exhausted_flags = []

        for _ in range(6):
            page, next_cursor, exhausted = read_source_page(
                source,
                "chat",
                "shard-1",
                cursor_token="",
                cursor_timestamp=10,
                seen_ids=seen_ids,
                limit=2,
            )
            page_ids = [item["source_message_id"] for item in page]
            self.assertEqual(next_cursor, "")
            if len(page_ids) < 2:
                # EOF is only allowed once every remaining row is on this page.
                self.assertTrue(exhausted)
            else:
                self.assertFalse(exhausted)
            drained.extend(page_ids)
            exhausted_flags.append(exhausted)
            seen_ids.update(page_ids)
            if exhausted:
                break

        self.assertEqual(drained, ["m0", "m1", "m2", "m3", "m4"])
        self.assertEqual(exhausted_flags, [False, False, True])
        self.assertEqual(source.requested_limits, [2, 4, 6])

    def test_legacy_fallback_never_hides_rows_behind_the_cursor_timestamp(self):
        # The reported regression: three same-second rows, two already seen.
        rows = [_message("m0", 10), _message("m1", 10), _message("m2", 10)]
        source = _LegacySource(rows)

        page, next_cursor, exhausted = read_source_page(
            source,
            "chat",
            "shard-1",
            cursor_timestamp=10,
            seen_ids={"m0", "m1"},
            limit=2,
        )

        self.assertEqual([item["source_message_id"] for item in page], ["m2"])
        self.assertTrue(exhausted)
        self.assertEqual(next_cursor, "")

    def test_legacy_fallback_drops_already_seen_same_timestamp_identities(self):
        rows = [_message("m0", 100), _message("m1", 100), _message("m2", 101)]
        source = _LegacySource(rows)

        page, _next_cursor, exhausted = read_source_page(
            source,
            "chat",
            "shard-1",
            cursor_timestamp=100,
            seen_ids={"m0"},
            limit=10,
        )

        self.assertEqual(
            [item["source_message_id"] for item in page], ["m1", "m2"]
        )
        self.assertTrue(exhausted)

    def test_decode_failure_code_survives_the_shared_reader(self):
        class _DecodeFailingSource:
            def get_cursor_page_for_shard(self, *_args, **_kwargs):
                raise SourceUnavailableError("source_message_decode_failed")

        with self.assertRaises(SourceUnavailableError) as raised:
            read_source_page(
                _DecodeFailingSource(), "chat", "generation", limit=10
            )
        self.assertEqual(
            normalize_source_error(raised.exception), "source_message_decode_failed"
        )

    def test_missing_source_is_a_content_free_failure(self):
        with self.assertRaises(SourceUnavailableError) as raised:
            read_source_page(None, "chat", "shard-1")
        self.assertEqual(raised.exception.code, "source_unavailable")

    def test_snapshot_helper_is_a_noop_without_reader_support(self):
        source = _LegacySource()
        with source_snapshot(source):
            inside = True
        self.assertTrue(inside)
        with source_snapshot(None):
            inside = True
        self.assertTrue(inside)

    def test_snapshot_helper_pins_when_supported(self):
        events = []

        class _SnapshotSource(_LegacySource):
            def source_snapshot(self):
                from contextlib import contextmanager

                @contextmanager
                def _pinned():
                    events.append("enter")
                    try:
                        yield self
                    finally:
                        events.append("exit")

                return _pinned()

        with source_snapshot(_SnapshotSource()):
            events.append("body")
        self.assertEqual(events, ["enter", "body", "exit"])

    def test_source_stage_summary_is_optional_content_free_and_delta_based(self):
        class _StatsSource:
            def __init__(self):
                self.stats = {
                    "inventory_scans": 10,
                    "inventory_reuses": 20,
                    "inventory_seconds": 1.0,
                    "cache_hits": 30,
                    "cache_rebuilds": 4,
                    "cache_rebuild_cold": 1,
                    "cache_rebuild_db": 1,
                    "cache_rebuild_wal": 1,
                    "cache_rebuild_key": 1,
                    "cache_rebuild_cache_path": 0,
                    "cache_rebuild_published_missing": 0,
                    "cache_rebuild_published_replaced": 0,
                    "decrypted_pages": 40,
                    "decrypted_bytes": 163840,
                    "decrypt_seconds": 2.0,
                    "snapshot_count": 3,
                    "snapshot_bytes": 8192,
                    "snapshot_seconds": 0.5,
                    "source_workers": 5,
                    "active_source_workers": 0,
                    "peak_source_workers": 1,
                    "contended_source_workers": 2,
                    "source_wait_seconds": 0.25,
                }

            def source_stage_stats(self):
                return dict(self.stats)

        source = _StatsSource()
        before = source_stage_stats(source)
        source.stats.update({
            "inventory_scans": 12,
            "inventory_reuses": 23,
            "cache_hits": 34,
            "decrypted_pages": 45,
            "decrypted_bytes": 184320,
            "contended_source_workers": 3,
            "source_wait_seconds": 0.5,
        })
        after = source_stage_stats(source)
        rendered = format_source_stage_summary(
            "monitor",
            before,
            after,
            projection_files_written=2,
            projection_bytes_written=4096,
        )

        self.assertIn("cycle=monitor", rendered)
        self.assertIn("inventory_scans=2", rendered)
        self.assertIn("inventory_reuses=3", rendered)
        self.assertIn("cache_hits=4", rendered)
        self.assertIn("decrypted_pages=5", rendered)
        self.assertIn("decrypted_bytes=20480", rendered)
        self.assertIn("contended=1", rendered)
        self.assertIn("projection_files=2", rendered)
        self.assertIn("projection_bytes=4096", rendered)
        self.assertNotIn("/Users", rendered)
        self.assertIsNone(source_stage_stats(_LegacySource()))
        self.assertEqual(
            format_source_stage_summary("resource", None, after),
            "",
        )
        self.assertEqual(
            format_source_stage_summary("resource", after, after),
            "",
        )

    def test_cycle_stage_metrics_are_isolated_by_worker_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = WeChatDB(tmp, {})
            main_before = source_cycle_stage_stats(source)
            worker_delta = []

            def record_worker():
                before = source_cycle_stage_stats(source)
                source._add_source_stage_stats(inventory_scans=1)
                after = source_cycle_stage_stats(source)
                worker_delta.append(
                    after["inventory_scans"] - before["inventory_scans"]
                )

            thread = threading.Thread(target=record_worker)
            thread.start()
            thread.join(2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(worker_delta, [1])
            main_after = source_cycle_stage_stats(source)
            self.assertEqual(
                main_after["inventory_scans"] - main_before["inventory_scans"],
                0,
            )
            self.assertEqual(source_stage_stats(source)["inventory_scans"], 1)


class TimestampSubsetDispatchTests(unittest.TestCase):
    """Readers exposing only one timestamp method must still dispatch cleanly."""

    class _CursorOnlySource:
        """The bounded ``shard_pages`` subset: no ``get_messages_for_shard``."""

        def __init__(self, rows=None):
            self.rows = list(rows or [])
            self.calls = []

        def get_cursor_messages_for_shard(
            self,
            _username,
            source_shard_id,
            *,
            since_ts=0,
            limit=500,
            page_forward=False,
            since_inclusive=False,
        ):
            self.calls.append((source_shard_id, since_ts, limit))
            rows = [
                dict(row) for row in self.rows if int(row["timestamp"]) >= since_ts
            ]
            rows.sort(key=lambda row: int(row["timestamp"]))
            return rows[:limit]

    def test_cursor_only_reader_is_not_masked_by_the_missing_plain_reader(self):
        # The regression: `source_capabilities` accepts a reader that only
        # exposes `get_cursor_messages_for_shard`, so dispatch must not eagerly
        # evaluate the absent `get_messages_for_shard` fallback.
        rows = [_message("m0", 10), _message("m1", 10), _message("m2", 11)]
        source = self._CursorOnlySource(rows)
        self.assertFalse(hasattr(source, "get_messages_for_shard"))
        self.assertEqual(source_capabilities(source), frozenset({"shard_pages"}))

        page, next_cursor, exhausted = read_source_page(
            source, "chat", "shard-1", cursor_timestamp=10, seen_ids={"m0"}, limit=10
        )

        self.assertEqual([item["source_message_id"] for item in page], ["m1", "m2"])
        self.assertEqual(next_cursor, "")
        self.assertTrue(exhausted)
        # The legacy request still widens by the identities already consumed.
        self.assertEqual(source.calls, [("shard-1", 10, 11)])

    def test_timestamp_only_reader_keeps_the_legacy_fallback_path(self):
        source = _LegacySource([_message("m0", 10), _message("m1", 10)])
        self.assertFalse(hasattr(source, "get_cursor_messages_for_shard"))
        self.assertEqual(source_capabilities(source), frozenset({"shard_pages"}))

        page, next_cursor, exhausted = read_source_page(
            source, "chat", "shard-1", cursor_timestamp=10, seen_ids={"m0"}, limit=10
        )

        self.assertEqual([item["source_message_id"] for item in page], ["m1"])
        self.assertEqual(next_cursor, "")
        self.assertTrue(exhausted)
        self.assertEqual(source.requested_limits, [11])

    def test_unsupported_reader_shapes_fail_closed_as_bounded_errors(self):
        class _NoReader:
            pass

        class _NonCallableReader:
            get_cursor_messages_for_shard = None
            get_messages_for_shard = "not callable"

        for source in (_NoReader(), _NonCallableReader()):
            with self.subTest(source=type(source).__name__):
                self.assertEqual(source_capabilities(source), frozenset())
                with self.assertRaises(SourceUnavailableError) as raised:
                    read_source_page(source, "chat", "shard-1")
                self.assertEqual(raised.exception.code, "source_shard_unavailable")
                self.assertIn(raised.exception.code, SOURCE_ERROR_CODES)

    def test_cursor_variant_wins_when_both_timestamp_readers_exist(self):
        rows = [_message("m0", 5)]
        source = _LegacySource(rows)
        used = []

        def _cursor_reader(_username, _shard, **_kwargs):
            used.append("cursor")
            return [dict(row) for row in rows]

        def _plain_reader(*_args, **_kwargs):
            used.append("plain")
            return []

        source.get_cursor_messages_for_shard = _cursor_reader
        source.get_messages_for_shard = _plain_reader

        page, _next_cursor, _exhausted = read_source_page(
            source, "chat", "shard-1", cursor_timestamp=5, limit=10
        )

        self.assertEqual(used, ["cursor"])
        self.assertEqual([item["source_message_id"] for item in page], ["m0"])


class CanonicalReaderParityTests(unittest.TestCase):
    """The seam must page exactly like the production macOS reader."""

    USERNAME = "same-second@chatroom"
    ROWS = 1205

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cache_dir = WeChatDB.CACHE_DIR
        WeChatDB.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        root = os.path.join(self.tmp.name, "db_storage")
        message_dir = os.path.join(root, "message")
        os.makedirs(message_dir)
        path = os.path.join(message_dir, "message_0.db")
        table = "Msg_" + hashlib.md5(self.USERNAME.encode()).hexdigest()
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                f"""
                CREATE TABLE [{table}] (
                    local_type INTEGER,
                    create_time INTEGER,
                    message_content TEXT,
                    WCDB_CT_message_content INTEGER,
                    status INTEGER
                )
                """
            )
            conn.executemany(
                f"INSERT INTO [{table}] VALUES (1, 100, ?, NULL, 0)",
                [(f"sender:\nrow-{index}",) for index in range(self.ROWS)],
            )
            conn.commit()
        finally:
            conn.close()
        self.db = WeChatDB(root, {})
        self.db._contacts = {"sender": "member"}
        self.db._nick_to_remark = {}
        self.db._load_contacts = lambda: None
        self.shard = self.db.get_message_shards(self.USERNAME)[0]

    def tearDown(self):
        WeChatDB.CACHE_DIR = self.old_cache_dir
        self.tmp.cleanup()

    def _canonical_pages(self, limit):
        pages = []
        token = ""
        while True:
            result = self.db.get_cursor_page_for_shard(
                self.USERNAME, self.shard, cursor_token=token, since_ts=0, limit=limit
            )
            pages.append((
                [message["source_message_id"] for message in result["messages"]],
                result["next_cursor"],
                result["exhausted"],
            ))
            token = result["next_cursor"]
            if result["exhausted"]:
                return pages

    def _seam_pages(self, limit):
        pages = []
        token = ""
        while True:
            messages, next_cursor, exhausted = read_source_page(
                self.db,
                self.USERNAME,
                self.shard,
                cursor_token=token,
                cursor_timestamp=0,
                limit=limit,
            )
            pages.append((
                [message["source_message_id"] for message in messages],
                next_cursor,
                exhausted,
            ))
            token = next_cursor
            if exhausted:
                return pages

    def test_keyset_pages_match_the_production_reader(self):
        canonical = self._canonical_pages(500)
        seam = self._seam_pages(500)

        self.assertEqual(seam, canonical)
        self.assertEqual([len(page[0]) for page in seam], [500, 500, 205])
        identities = [item for page in seam for item in page[0]]
        self.assertEqual(len(identities), self.ROWS)
        self.assertEqual(len(set(identities)), self.ROWS)

    def test_source_capabilities_report_the_production_reader(self):
        exposed = source_capabilities(self.db)
        self.assertEqual(
            exposed,
            frozenset(
                {"source_inventory", "shard_pages", "keyset_pages", "snapshot"}
            ),
        )
        self.assertTrue(source_adapter.supports_monitor_source_cursors(self.db))

    def test_reader_implements_the_declared_protocol_methods(self):
        for name in (
            "get_source_inventory",
            "get_message_shards",
            "get_messages_for_shard",
            "get_cursor_page_for_shard",
            "source_snapshot",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(self.db, name)))

    def test_legacy_exception_name_is_the_canonical_class(self):
        self.assertIs(WeChatSourceDegraded, SourceUnavailableError)
        with self.assertRaises(SourceUnavailableError) as raised:
            self.db.get_cursor_page_for_shard(
                self.USERNAME, self.shard, cursor_token="{", limit=10
            )
        self.assertEqual(raised.exception.code, "source_cursor_invalid")
        with self.assertRaises(WeChatSourceDegraded):
            self.db.get_cursor_page_for_shard(
                self.USERNAME, "unknown-generation", limit=10
            )


if __name__ == "__main__":
    unittest.main()
