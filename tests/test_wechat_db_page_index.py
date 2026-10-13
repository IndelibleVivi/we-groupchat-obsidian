"""Keyset paging without a ``create_time`` index.

The WeChat source shard tables expose only sender/server/sort_seq/type_seq
indexes, so the legacy keyset query
``WHERE create_time > ? OR (create_time = ? AND rowid > ?)`` degrades to a full
table ``SCAN`` plus a temporary B-tree on *every* page. These tests pin the
snapshot-scoped, key-only page index that removes the repeated scan while
keeping exact row/order/cursor/EOF semantics, a read-only source, bounded
memory, and per-thread isolation.
"""
import hashlib
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from core.wechat_db import WeChatDB


def _table_name(username):
    return f"Msg_{hashlib.md5(username.encode()).hexdigest()}"


def _write_shard(path, table_name):
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"""
            CREATE TABLE [{table_name}] (
                local_id INTEGER, server_id INTEGER, sort_seq INTEGER,
                local_type INTEGER, create_time INTEGER,
                message_content TEXT, WCDB_CT_message_content INTEGER,
                status INTEGER, packed_info_data BLOB
            )
        """)
        # The real source carries non-time indexes; include one so the derived
        # index has to win over an adjacent index rather than an empty schema.
        conn.execute(f"CREATE INDEX [{table_name}_sort] ON [{table_name}](sort_seq)")
        conn.commit()
    finally:
        conn.close()


def _insert_rows(path, table_name, rows):
    """Insert ``rows`` in list order, so physical ``rowid`` equals position + 1."""
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            f"INSERT INTO [{table_name}] VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row.get("local_id", index + 1),
                    row.get("server_id", 10_000 + index),
                    row.get("sort_seq", index),
                    row.get("local_type", 1),
                    row["create_time"],
                    row.get("content", f"sender:\nrow-{index}"),
                    row.get("content_type"),
                    row.get("status", 0),
                    row.get("packed_info_data"),
                )
                for index, row in enumerate(rows)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _expected_order(path, table_name):
    conn = sqlite3.connect(path)
    try:
        return [
            (int(create_time), int(rowid))
            for create_time, rowid in conn.execute(
                f"SELECT create_time, rowid FROM [{table_name}] "
                f"ORDER BY create_time ASC, rowid ASC"
            )
        ]
    finally:
        conn.close()


def _disordered_rows(count):
    """Rows whose physical order disagrees with time order, with repeats."""
    base = 1_600_000_000
    rows = []
    for index in range(count):
        if index % 11 == 0:
            create_time = base + 5  # heavy same-second bucket
        elif index % 3 == 0:
            create_time = base  # oldest timestamp, inserted mid-stream
        else:
            create_time = base + (index % 97)
        rows.append({"create_time": create_time})
    # Reverse physical order so rowid order is the opposite of time order.
    rows.reverse()
    return rows


class _TracingWeChatDB(WeChatDB):
    """Records SQL run against the source-readonly connections."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sql_log = []
        self._sql_lock = threading.Lock()

    def _connect_source_readonly(self, path):
        conn = super()._connect_source_readonly(path)
        conn.set_trace_callback(self._record_sql)
        return conn

    def _record_sql(self, statement):
        with self._sql_lock:
            self.sql_log.append(statement)


class _ShardFixture:
    """A single-shard synthetic source root with a controllable row set."""

    def __init__(self, tmp_root, username="room@chatroom"):
        self.username = username
        self.table_name = _table_name(username)
        self.root = os.path.join(tmp_root, "source-a")
        message_dir = os.path.join(self.root, "message")
        os.makedirs(message_dir)
        self.source_path = os.path.join(message_dir, "message_0.db")
        _write_shard(self.source_path, self.table_name)
        self.cache_root = os.path.join(tmp_root, "cache-a")

    def insert(self, rows):
        _insert_rows(self.source_path, self.table_name, rows)

    def mkdb(self):
        db = _TracingWeChatDB(self.root, {}, cache_root=self.cache_root)
        db._contacts = {"sender": "成员"}
        db._contacts_full = []
        db._nick_to_remark = {}
        db._load_contacts = lambda: None
        db._fixture_username = self.username
        return db

    def expected(self):
        return _expected_order(self.source_path, self.table_name)


def _index_files(db):
    directory = os.path.join(db.cache_dir, "page-index")
    if not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, name) for name in os.listdir(directory)
    )


def _drain(db, username, shard, *, limit=200):
    """Drain a shard inside one snapshot; return ``(create_time, rowid)`` stream."""
    positions = []
    pages = []
    with db.source_snapshot():
        token = ""
        while True:
            result = db.get_cursor_page_for_shard(
                username, shard, cursor_token=token, since_ts=0, limit=limit
            )
            pages.append(len(result["messages"]))
            for message in result["messages"]:
                envelope = message["source_envelope"]
                positions.append(
                    (int(envelope["create_time"]), int(envelope["rowid"]))
                )
            token = result["next_cursor"]
            if result["exhausted"]:
                break
    return positions, pages


def _shard_id(db, username):
    return db.get_message_shards(username)[0]


class WeChatDBPageIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, rows, username="room@chatroom"):
        fixture = _ShardFixture(self.tmp.name, username)
        fixture.insert(rows)
        return fixture, fixture.mkdb()

    def test_two_chats_in_one_shard_and_snapshot_have_independent_indexes(self):
        fixture, db = self._build([{"create_time": 100 + i} for i in range(50)])
        other = "another@chatroom"
        table = _table_name(other)
        _write_shard(fixture.source_path, table)
        _insert_rows(fixture.source_path, table, [{"create_time": 500 + i} for i in range(35)])
        shard = _shard_id(db, fixture.username)
        with db.source_snapshot():
            first, _ = _drain(db, fixture.username, shard, limit=7)
            second, _ = _drain(db, other, shard, limit=7)
        self.assertEqual(first, fixture.expected())
        self.assertEqual(second, _expected_order(fixture.source_path, table))
        self.assertEqual(db.source_stage_stats()["page_index_builds"], 2)

    def test_unindexed_disordered_paging_is_exact_and_builds_one_index(self):
        rows = _disordered_rows(1_205)
        fixture, db = self._build(rows)
        shard = _shard_id(db, fixture.username)

        positions, pages = _drain(db, fixture.username, shard, limit=200)

        self.assertEqual(positions, fixture.expected())
        self.assertEqual(len(positions), 1_205)
        self.assertEqual(pages, [200, 200, 200, 200, 200, 200, 5])
        stats = db.source_stage_stats()
        self.assertEqual(stats["page_index_builds"], 1)
        self.assertEqual(stats["page_index_hits"], len(pages) - 2)
        # Cleanup: the snapshot-scoped index file is gone after the traversal.
        self.assertEqual(_index_files(db), [])

    def test_legacy_path_and_indexed_path_agree_exactly(self):
        rows = _disordered_rows(640)
        fixture, _ = self._build(rows)
        shard = _shard_id(fixture.mkdb(), fixture.username)

        indexed_db = fixture.mkdb()
        legacy_db = fixture.mkdb()
        legacy_db._ensure_page_index = lambda *_args: ""

        indexed_positions, _ = _drain(
            indexed_db, fixture.username, shard, limit=150
        )
        legacy_positions, _ = _drain(
            legacy_db, fixture.username, shard, limit=150
        )

        self.assertEqual(indexed_positions, legacy_positions)
        self.assertEqual(indexed_positions, fixture.expected())
        self.assertEqual(indexed_db.source_stage_stats()["page_index_builds"], 1)
        self.assertEqual(legacy_db.source_stage_stats()["page_index_builds"], 0)

    def test_later_pages_seek_the_index_instead_of_rescanning(self):
        rows = _disordered_rows(900)
        fixture, db = self._build(rows)
        shard = _shard_id(db, fixture.username)

        positions = []
        with db.source_snapshot():
            token = ""
            while True:
                result = db.get_cursor_page_for_shard(
                    fixture.username, shard, cursor_token=token, since_ts=0,
                    limit=200,
                )
                positions.extend(
                    (
                        int(message["source_envelope"]["create_time"]),
                        int(message["source_envelope"]["rowid"]),
                    )
                    for message in result["messages"]
                )
                token = result["next_cursor"]
                if result["exhausted"]:
                    break

            state = db._source_snapshot_state()
            (pinned, _table), index_path = next(iter(state["page_index_paths"].items()))
            query = [statement for statement in db.sql_log
                     if "message_content" in statement and "page_keys" in statement][-1]
            connection = db._connect_source_readonly(pinned)
            try:
                self.assertTrue(db._attach_page_index(connection, index_path))
                plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query)]
            finally:
                connection.close()
            self.assertFalse(any("SCAN " + fixture.table_name in step for step in plan))
            self.assertTrue(any(step.startswith("SEARCH ") and "page_keys USING PRIMARY KEY" in step for step in plan), plan)
            self.assertTrue(any("USING INTEGER PRIMARY KEY" in step for step in plan))

        self.assertEqual(positions, fixture.expected())
        selects = [
            statement
            for statement in db.sql_log
            if (
                fixture.table_name in statement
                and "SELECT" in statement
                and "message_content" in statement
            )
        ]
        # ``set_trace_callback`` expands parameters, so match on structure.
        legacy = [
            statement
            for statement in selects
            if "WHERE (create_time, rowid) >" in statement and "page_keys" not in statement
        ]
        indexed = [statement for statement in selects if "page_keys" in statement]
        # The full-scan legacy form runs exactly once, before the index exists;
        # every later page seeks the derived key-only index.
        self.assertEqual(len(legacy), 1)
        self.assertEqual(len(indexed), len(selects) - 1)

    def test_indexed_source_still_produces_exact_pages(self):
        username = "indexed@chatroom"
        fixture = _ShardFixture(self.tmp.name, username)
        conn = sqlite3.connect(fixture.source_path)
        try:
            conn.execute(
                f"CREATE INDEX idx_ct ON [{fixture.table_name}](create_time)"
            )
            conn.commit()
        finally:
            conn.close()
        fixture.insert(_disordered_rows(500))
        db = fixture.mkdb()
        shard = _shard_id(db, username)

        positions, _ = _drain(db, username, shard, limit=120)

        self.assertEqual(positions, fixture.expected())

        self.assertEqual(db.source_stage_stats()["page_index_builds"], 0)

    def test_large_body_is_returned_exactly(self):
        big_body = "sender:\n" + ("x" * 200_000)
        rows = [
            {"create_time": 1_600_000_000 + index} for index in range(4)
        ]
        rows[2]["content"] = big_body
        fixture, db = self._build(rows)
        shard = _shard_id(db, fixture.username)

        with db.source_snapshot():
            collected = []
            token = ""
            while True:
                result = db.get_cursor_page_for_shard(
                    fixture.username, shard, cursor_token=token, since_ts=0,
                    limit=1,
                )
                collected.extend(result["messages"])
                token = result["next_cursor"]
                if result["exhausted"]:
                    break

        self.assertEqual(len(collected), 4)
        self.assertEqual(collected[2]["text"], "x" * 200_000)

    def test_snapshot_cleanup_on_exception(self):
        rows = _disordered_rows(300)
        fixture, db = self._build(rows)
        shard = _shard_id(db, fixture.username)

        class Boom(RuntimeError):
            pass

        with self.assertRaises(Boom):
            with db.source_snapshot():
                token = ""
                for _ in range(2):
                    result = db.get_cursor_page_for_shard(
                        fixture.username, shard, cursor_token=token, since_ts=0,
                        limit=100,
                    )
                    token = result["next_cursor"]
                self.assertTrue(_index_files(db))  # index exists mid-snapshot
                raise Boom()

        self.assertEqual(_index_files(db), [])

    def test_interrupted_index_build_cleans_unregistered_file(self):
        fixture, db = self._build(_disordered_rows(300))
        shard = _shard_id(db, fixture.username)
        with self.assertRaises(KeyboardInterrupt), db.source_snapshot():
            first = db.get_cursor_page_for_shard(fixture.username, shard, limit=100)
            with patch.object(db, "_build_page_index", side_effect=KeyboardInterrupt):
                db.get_cursor_page_for_shard(fixture.username, shard, cursor_token=first["next_cursor"], limit=100)
        self.assertEqual(_index_files(db), [])

    def test_page_index_directory_symlink_is_not_followed(self):
        fixture, db = self._build(_disordered_rows(300))
        shard = _shard_id(db, fixture.username)
        outside = os.path.join(self.tmp.name, "not-owned-by-page-index")
        os.mkdir(outside)
        os.symlink(outside, db._page_index_dir())
        with db.source_snapshot():
            first = db.get_cursor_page_for_shard(fixture.username, shard, limit=100)
            with self.assertRaisesRegex(Exception, "source_shard_unavailable"):
                db.get_cursor_page_for_shard(fixture.username, shard, cursor_token=first["next_cursor"], limit=100)
        self.assertEqual(os.listdir(outside), [])

    def test_snapshot_cleanup_on_generation_discard(self):
        rows = _disordered_rows(300)
        fixture, db = self._build(rows)
        with db.source_snapshot():
            inventory = db.get_source_inventory(update=True)
            generation = inventory["present_generation_ids"][0]
            token = ""
            for _ in range(2):
                result = db.get_cursor_page_for_shard(
                    fixture.username, generation, cursor_token=token,
                    since_ts=0, limit=100,
                )
                token = result["next_cursor"]
            self.assertTrue(_index_files(db))
            db._discard_source_snapshot_pin(next(iter(db._source_snapshot_state()["paths"])))
            self.assertEqual(_index_files(db), [])
        # Leaving the snapshot discards every derived index file.
        self.assertEqual(_index_files(db), [])

    def test_readonly_source_bytes_unchanged(self):
        rows = _disordered_rows(400)
        fixture, db = self._build(rows)
        shard = _shard_id(db, fixture.username)
        with open(fixture.source_path, "rb") as handle:
            before = hashlib.sha256(handle.read()).hexdigest()
        before_stat = os.stat(fixture.source_path)

        _drain(db, fixture.username, shard, limit=90)

        with open(fixture.source_path, "rb") as handle:
            after = hashlib.sha256(handle.read()).hexdigest()
        after_stat = os.stat(fixture.source_path)
        self.assertEqual(before, after)
        self.assertEqual(before_stat.st_size, after_stat.st_size)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
        self.assertFalse(os.path.exists(fixture.source_path + "-journal"))
        self.assertFalse(os.path.exists(fixture.source_path + "-wal"))

    def test_threads_isolate_snapshot_indexes(self):
        rows = _disordered_rows(600)
        fixture, db = self._build(rows)
        shard = _shard_id(db, fixture.username)
        expected = fixture.expected()

        barrier = threading.Barrier(2)
        observed = {}
        errors = []

        def worker(name):
            try:
                with db.source_snapshot():
                    token = ""
                    positions = []
                    while True:
                        result = db.get_cursor_page_for_shard(
                            fixture.username, shard, cursor_token=token,
                            since_ts=0, limit=100,
                        )
                        positions.extend(
                            (
                                int(message["source_envelope"]["create_time"]),
                                int(message["source_envelope"]["rowid"]),
                            )
                            for message in result["messages"]
                        )
                        barrier.wait(timeout=10)
                        token = result["next_cursor"]
                        if result["exhausted"]:
                            break
                    observed[name] = positions
            except Exception as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"t{index}",))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(observed["t0"], expected)
        self.assertEqual(observed["t1"], expected)
        # Both threads cleaned up their own private index files.
        self.assertEqual(_index_files(db), [])

    def test_single_page_read_builds_no_index(self):
        rows = _disordered_rows(80)
        fixture, db = self._build(rows)
        shard = _shard_id(db, fixture.username)

        with db.source_snapshot():
            db.get_cursor_page_for_shard(
                fixture.username, shard, cursor_token="", since_ts=0, limit=500,
            )

        self.assertEqual(db.source_stage_stats()["page_index_builds"], 0)
        self.assertEqual(_index_files(db), [])

    def test_same_second_bucket_spanning_pages_is_exact(self):
        # One timestamp shared by many rows, so the same-second B-tree bucket
        # spans several pages. The ``(create_time, rowid)`` keyset must return
        # each row exactly once, in ascending rowid order, across pages.
        base = 1_600_000_000
        rows = [{"create_time": base} for _ in range(50)]
        fixture, db = self._build(rows)
        shard = _shard_id(db, fixture.username)

        positions, pages = _drain(db, fixture.username, shard, limit=7)

        rowids = [rowid for _, rowid in positions]
        self.assertEqual(rowids, list(range(1, 51)))
        self.assertEqual(len(rowids), len(set(rowids)))
        self.assertEqual(sum(pages), 50)

    def test_missing_chat_table_is_eof_and_never_indexes(self):
        fixture, db = self._build([{"create_time": 1}])
        with db.source_snapshot():
            inventory = db.get_source_inventory(update=True)
            generation = inventory["present_generation_ids"][0]
            outcome = db.get_cursor_page_for_shard(
                "absent@chatroom", generation, cursor_token="", since_ts=0,
                limit=50,
            )
        self.assertEqual(outcome["messages"], [])
        self.assertTrue(outcome["exhausted"])
        self.assertEqual(db.source_stage_stats()["page_index_builds"], 0)

    def test_filtered_rows_still_advance_without_false_eof(self):
        base = 1_600_000_000
        rows = []
        for index in range(10):
            row = {"create_time": base + index}
            if index in (4, 5):
                row["content"] = "sender:\n<sysmsg type='fixture'></sysmsg>"
            rows.append(row)
        fixture, db = self._build(rows)
        shard = _shard_id(db, fixture.username)

        with db.source_snapshot():
            collected = []
            token = ""
            while True:
                page = db.get_cursor_page_for_shard(
                    fixture.username, shard, cursor_token=token, since_ts=0,
                    limit=3,
                )
                collected.extend(page["messages"])
                token = page["next_cursor"]
                if page["exhausted"]:
                    break

        self.assertEqual(len(collected), 10)
        self.assertEqual(collected[4]["text"], "")
        self.assertEqual(collected[5]["text"], "")


class WeChatDBPageIndexFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_index_build_failure_falls_back_to_legacy_scan(self):
        fixture = _ShardFixture(self.tmp.name)
        fixture.insert(_disordered_rows(400))
        db = fixture.mkdb()
        shard = _shard_id(db, fixture.username)

        # Simulate an unbuildable index (e.g. unwritable cache). The page reader
        # must fall back to the legacy scan and never fail the traversal.
        db._build_page_index = lambda *args, **kwargs: ""

        positions, _ = _drain(db, fixture.username, shard, limit=100)

        self.assertEqual(positions, fixture.expected())
        self.assertEqual(db.source_stage_stats()["page_index_builds"], 0)
        self.assertEqual(_index_files(db), [])


if __name__ == "__main__":
    unittest.main()
