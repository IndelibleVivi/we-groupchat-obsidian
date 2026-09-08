"""Synthetic acceptance and restart regressions using the real monitor/store."""
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from core.knowledge import KnowledgeStore
from core.monitor import MonitorDecisionError, TopicMonitor, load_state, save_state
from core.monitor_result import classify_monitor_result
from core.monitor_state import MonitorStateStore
from core.wechat_db import WeChatDB
from tests.test_monitor_source import ConflictingKnowledgeStore, CursorDB, raw_message


POSITIVE = {
    "match": True, "score": 90, "title": "Synthetic finding",
    "summary": "A synthetic resource worth retaining.", "topic_key": "synthetic-resource",
    "category": "工具更新",
}
NEGATIVE = {"match": False, "score": 20}


class DecisionValidationTests(unittest.TestCase):
    def test_invalid_responses_never_become_negative_decisions(self):
        invalid = [
            "", "temporary upstream error", '{"match":true', "{}", "[]", "null",
            {}, [], None, {"match": False}, {"score": 20},
            {"match": "false", "score": 80}, {"match": 0, "score": 20},
            {"match": False, "score": "20"}, {"match": False, "score": True},
            {"match": False, "score": float("nan")},
            {"match": False, "score": -1}, {"match": False, "score": 101},
            {"match": True, "score": 20}, {"match": True, "score": 90},
            {**POSITIVE, "summary": " "}, {**POSITIVE, "digest": []},
            {**POSITIVE, "links": "https://example.test"},
            {**POSITIVE, "links": [{"url": "https://example.test"}]},
            {**POSITIVE, "resource_lead": "false"},
            {**POSITIVE, "items": [{"summary": None}]},
            '{"match":false,"score":20,"score":90}',
            '{"match":false,"score":20,"extra":NaN}',
            'prose {"match":false,"score":20}',
        ]
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(MonitorDecisionError) as raised:
                    TopicMonitor._validate_decision(value)
                self.assertEqual(str(raised.exception), "ai_invalid_response")

    def test_explicit_negative_and_supported_body_forms_remain_valid(self):
        for value in (
            NEGATIVE, POSITIVE, {**POSITIVE, "digest": "Synthetic digest"},
            {"match": True, "score": 70, "items": [{"time": "12:00", "text": "Finding"}]},
            json.dumps(NEGATIVE), '```json\n{"match":false,"score":20}\n```',
        ):
            with self.subTest(value=value):
                self.assertIsInstance(TopicMonitor._validate_decision(value), dict)


class MonitorAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_path = str(self.root / "state.json")
        self.now = 1000
        self.config = {
            "monitor_chat_username": "fixture@chatroom",
            "monitor_chat_display_name": "Synthetic group",
            "monitor_topic": "Synthetic resources",
            "monitor_max_messages_per_run": 10,
            "monitor_context_overlap_minutes": 0,
            "monitor_ai_retry_attempts": 0,
            "monitor_ai_retry_delay_seconds": 0,
            "monitor_ai_failure_backoff_minutes": 1,
            "monitor_knowledge_enabled": True,
            "monitor_knowledge_db": str(self.root / "knowledge.db"),
            "monitor_obsidian_root": str(self.root / "vault"),
            "review_queue_dir": str(self.root / "review"),
        }
        save_state({"last_checked_ts": 10}, self.state_path)
        self.db = CursorDB({"logical-a": ("generation-a", [
            raw_message("generation-a", 1, 11, "SYNTHETIC_PRIVATE_SOURCE_BODY"),
        ])})
        self.store = KnowledgeStore(self.config["monitor_knowledge_db"], self.config["monitor_obsidian_root"])

    def monitor(self, evaluator, *, store=None, db=None, state_store=None):
        return TopicMonitor(
            db or self.db, self.config, state_file=self.state_path,
            hits_dir=str(self.root / "hits"), knowledge_store=store or self.store,
            ai_evaluator=evaluator, state_store=state_store,
            now_func=lambda: self.now,
        )

    def event_count(self):
        conn = self.store.connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()

    def append(self):
        self.db.shards["logical-a"]["messages"].append(
            raw_message("generation-a", 2, 12, "LATER_SYNTHETIC_MESSAGE")
        )

    def test_invalid_response_preserves_batch_backoff_and_no_canonical_event(self):
        calls = []
        monitor = self.monitor(lambda *_: calls.append(True) or '{"match":true')
        first = monitor.check_once()
        state = load_state(self.state_path)
        self.assertEqual(first["status"], "ai_invalid_response")
        self.assertEqual(classify_monitor_result(first).action, "blocked")
        self.assertEqual(state["last_checked_ts"], 10)
        self.assertNotIn("source_cursors", state)
        self.assertEqual(state["ai_last_error_code"], "ai_invalid_response")
        self.assertEqual(state["ai_next_retry_after"], 1060)
        self.assertEqual(state["pending_source_batch"]["raw_count"], 1)
        self.assertEqual(json.loads(Path(self.state_path).read_text())["schema"], "we-groupchat-obsidian.monitor-state.v2")
        self.assertNotIn("SYNTHETIC_PRIVATE_SOURCE_BODY", Path(self.state_path).read_text())
        self.assertEqual(self.event_count(), 0)
        self.assertEqual(monitor.check_once()["status"], "ai_backoff")
        self.assertEqual(calls, [True])
        self.append()
        self.now = 1061
        second = self.monitor(lambda *_: NEGATIVE).check_once()
        self.assertEqual(second["message_count"], 1)
        self.assertEqual(second["status"], "no_match")
        self.assertEqual(load_state(self.state_path)["last_checked_ts"], 11)
        self.assertNotIn("pending_source_batch", load_state(self.state_path))
        self.assertEqual(json.loads(Path(self.state_path).read_text())["schema"], "we-groupchat-obsidian.monitor-state.v1")
        self.assertNotIn("ai_failure_count", load_state(self.state_path))
        third = self.monitor(lambda *_: NEGATIVE).check_once()
        self.assertEqual(third["message_count"], 1)
        self.assertEqual(load_state(self.state_path)["last_checked_ts"], 12)

    def test_invalid_response_short_retries_are_bounded_and_content_free(self):
        self.config["monitor_ai_retry_attempts"] = 1
        calls = []
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.monitor(lambda *_: calls.append(True) or "PRIVATE_RESPONSE_SENTINEL").check_once()
        self.assertEqual(calls, [True, True])
        self.assertEqual(result["status"], "ai_invalid_response")
        self.assertNotIn("PRIVATE_RESPONSE_SENTINEL", output.getvalue())
        self.assertNotIn("PRIVATE_RESPONSE_SENTINEL", Path(self.state_path).read_text())

    def test_invalid_then_valid_short_retry_advances_once(self):
        self.config["monitor_ai_retry_attempts"] = 1
        evaluator = mock.Mock(side_effect=["truncated", NEGATIVE])
        result = self.monitor(evaluator).check_once()
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(evaluator.call_count, 2)
        self.assertNotIn("pending_source_batch", load_state(self.state_path))

    def test_dry_run_invalid_response_does_not_mutate_state(self):
        original = Path(self.state_path).read_bytes()
        self.db.context_messages = self.db.shards["logical-a"]["messages"]
        self.config["monitor_interval_minutes"] = 999
        result = self.monitor(lambda *_: "invalid").check_once(dry_run=True)
        self.assertEqual(result["status"], "ai_invalid_response")
        self.assertEqual(Path(self.state_path).read_bytes(), original)

    def test_legacy_reader_invalid_response_does_not_consume_messages(self):
        from tests.test_monitor import FakeDB, msg
        result = self.monitor(lambda *_: [], db=FakeDB([msg(11, "synthetic")])).check_once()
        self.assertEqual(result["status"], "ai_invalid_response")
        self.assertEqual(load_state(self.state_path)["last_checked_ts"], 10)
        self.assertEqual(self.event_count(), 0)

    def test_restart_after_event_commit_with_new_messages_and_changed_limit(self):
        state_store = MonitorStateStore(self.state_path)
        store = ConflictingKnowledgeStore(
            self.config["monitor_knowledge_db"], self.config["monitor_obsidian_root"],
            state_store=state_store,
        )
        calls = []
        first = self.monitor(lambda *_: calls.append(True) or POSITIVE, store=store).check_once()
        self.assertEqual(first["status"], "monitor_state_conflict")
        pending_id = load_state(self.state_path)["pending_source_batch"]["source_batch_id"]
        self.assertEqual(self.event_count(), 1)
        self.append()
        self.config["monitor_max_messages_per_run"] = 1000
        self.config["monitor_context_overlap_minutes"] = 1
        # Canonical recovery must precede optional context reads.
        with mock.patch.object(self.db, "get_messages", side_effect=AssertionError("no context on recovery")):
            second = self.monitor(lambda *_: self.fail("committed batch replayed provider")).check_once()
        self.assertEqual(second["status"], "duplicate")
        self.assertTrue(second["knowledge_event_reused"])
        self.assertFalse(second["source_eof"])
        self.assertEqual(second["message_count"], 1)
        self.assertEqual(load_state(self.state_path)["last_checked_ts"], 11)
        self.assertEqual(self.event_count(), 1)
        self.assertEqual(calls, [True])
        conn = self.store.connect()
        try:
            self.assertEqual(conn.execute("SELECT source_batch_id FROM events").fetchone()[0], pending_id)
        finally:
            conn.close()
        self.config["monitor_context_overlap_minutes"] = 0
        third = self.monitor(lambda *_: calls.append(True) or POSITIVE).check_once()
        self.assertEqual(third["message_count"], 1)
        self.assertEqual(load_state(self.state_path)["last_checked_ts"], 12)
        self.assertEqual(self.event_count(), 2)
        self.assertEqual(calls, [True, True])

    def test_write_failure_before_event_keeps_members_for_next_attempt(self):
        with mock.patch.object(self.store, "apply_event", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.monitor(lambda *_: POSITIVE).check_once()
        self.append()
        result = self.monitor(lambda *_: POSITIVE).check_once()
        self.assertEqual(result["message_count"], 1)
        self.assertEqual(self.event_count(), 1)
        self.assertEqual(load_state(self.state_path)["last_checked_ts"], 11)

    def test_intent_write_failure_stops_before_provider(self):
        state_store = MonitorStateStore(self.state_path)
        original = Path(self.state_path).read_bytes()
        with mock.patch.object(state_store, "_write_locked_unmapped", side_effect=OSError("full disk")):
            result = self.monitor(lambda *_: self.fail("provider before intent"), state_store=state_store).check_once()
        self.assertEqual(result["status"], "monitor_state_write_failed")
        self.assertEqual(Path(self.state_path).read_bytes(), original)
        self.assertEqual(self.event_count(), 0)

    def test_execution_lock_rejects_overlapping_worker_without_provider(self):
        second_results = []
        def evaluate(*_):
            second_results.append(self.monitor(lambda *_: self.fail("concurrent provider")).check_once())
            return NEGATIVE
        self.assertEqual(self.monitor(evaluate).check_once()["status"], "no_match")
        self.assertEqual(second_results[0]["status"], "monitor_worker_busy")
        self.assertEqual(classify_monitor_result(second_results[0]).action, "blocked")
        # Ownership is released on completion, not left as a sticky lease.
        self.assertEqual(self.monitor(lambda *_: NEGATIVE).check_once()["status"], "no_messages")

    def stage_failed_batch(self):
        self.monitor(lambda *_: "invalid").check_once()
        self.now = 1061

    def test_in_place_source_change_blocks_reinterpretation(self):
        self.stage_failed_batch()
        original = Path(self.state_path).read_bytes()
        self.db.shards["logical-a"]["messages"][0]["text"] = "Changed payload"
        result = self.monitor(lambda *_: self.fail("changed source evaluated")).check_once()
        self.assertEqual(result["status"], "monitor_pending_batch_changed")
        self.assertEqual(Path(self.state_path).read_bytes(), original)

    def test_late_insert_before_frozen_boundary_blocks_substitution(self):
        self.stage_failed_batch()
        self.db.shards["logical-a"]["messages"].insert(0, raw_message("generation-a", 2, 10, "late row"))
        result = self.monitor(lambda *_: self.fail("substituted batch evaluated")).check_once()
        self.assertEqual(result["status"], "monitor_pending_batch_changed")
        self.assertEqual(load_state(self.state_path)["last_checked_ts"], 10)

    def test_changed_generation_blocks_before_source_page(self):
        self.stage_failed_batch()
        self.db.replace_generation("logical-a", "generation-b", [])
        calls_before = len(self.db.page_calls)
        result = self.monitor(lambda *_: self.fail("changed generation evaluated")).check_once()
        self.assertEqual(result["status"], "source_generation_changed")
        self.assertEqual(len(self.db.page_calls), calls_before)

    def test_policy_change_is_explicit_but_model_change_is_allowed(self):
        self.stage_failed_batch()
        self.config["monitor_topic"] = "Different interest"
        original = Path(self.state_path).read_bytes()
        result = self.monitor(lambda *_: self.fail("changed policy evaluated")).check_once()
        self.assertEqual(result["status"], "monitor_pending_policy_changed")
        self.assertEqual(Path(self.state_path).read_bytes(), original)
        self.config["monitor_topic"] = "Synthetic resources"
        self.config["monitor_ai_model"] = "replacement-fixture-model"
        self.assertEqual(self.monitor(lambda *_: NEGATIVE).check_once()["status"], "no_match")

    def test_corrupt_pending_descriptor_never_resets_checkpoint(self):
        self.stage_failed_batch()
        state = load_state(self.state_path)
        state["pending_source_batch"]["raw_count"] = 100
        save_state(state, self.state_path)
        original = Path(self.state_path).read_bytes()
        result = self.monitor(lambda *_: self.fail("corrupt intent evaluated")).check_once()
        self.assertEqual(result["status"], "monitor_pending_batch_corrupt")
        self.assertEqual(Path(self.state_path).read_bytes(), original)

    def test_changed_checkpoint_cannot_reuse_old_intent(self):
        self.stage_failed_batch()
        state = load_state(self.state_path)
        state["last_checked_ts"] = 11
        save_state(state, self.state_path)
        result = self.monitor(lambda *_: self.fail("moved checkpoint evaluated")).check_once()
        self.assertEqual(result["status"], "monitor_pending_checkpoint_changed")

    def test_filtered_rows_remain_in_frozen_raw_membership(self):
        self.db.shards["logical-a"]["messages"].insert(0, raw_message("generation-a", 2, 10, ""))
        self.stage_failed_batch()
        self.assertEqual(load_state(self.state_path)["pending_source_batch"]["raw_count"], 2)
        self.append()
        result = self.monitor(lambda *_: NEGATIVE).check_once()
        self.assertEqual(result["raw_message_count"], 2)
        self.assertEqual(result["message_count"], 1)

    def test_health_inspects_pending_metadata_without_source_or_body(self):
        from core.monitor import state_file_for_chat
        from scripts.health_check import monitor_state_health
        self.stage_failed_batch()
        directory = self.root / "health-states"
        directory.mkdir()
        path = Path(state_file_for_chat(self.config["monitor_chat_username"], state_dir=directory))
        path.write_bytes(Path(self.state_path).read_bytes())
        config = {"monitor_chats": [{"username": self.config["monitor_chat_username"], "name": "Synthetic group"}]}
        with mock.patch.object(WeChatDB, "get_source_inventory", side_effect=AssertionError("health read source")):
            report = monitor_state_health(config, state_dir=directory, runtime_log=self.root / "missing.log")
        self.assertEqual(report["pending_batches"], 1)
        self.assertEqual(report["invalid_response_chats"], 1)
        text = json.dumps(report)
        for sentinel in ("fixture@chatroom", "SYNTHETIC_PRIVATE_SOURCE_BODY", str(self.root), "generation-a"):
            self.assertNotIn(sentinel, text)

    def test_real_sqlite_source_event_projection_restart_chain(self):
        db_root = self.root / "wechat-fixture" / "db_storage"
        source_path = db_root / "message" / "message_0.db"
        source_path.parent.mkdir(parents=True)
        table = "Msg_" + hashlib.md5(self.config["monitor_chat_username"].encode()).hexdigest()
        with sqlite3.connect(source_path) as conn:
            conn.execute(f"CREATE TABLE {table}(local_type INTEGER, create_time INTEGER, message_content TEXT, status INTEGER)")
            conn.execute(f"INSERT INTO {table} VALUES(1,11,'member:\nSynthetic SQLite source',0)")
        db = WeChatDB(str(db_root), {})
        db._contacts = {}
        state_store = MonitorStateStore(self.state_path)
        store = ConflictingKnowledgeStore(
            self.config["monitor_knowledge_db"], self.config["monitor_obsidian_root"],
            state_store=state_store,
        )
        first = self.monitor(lambda *_: POSITIVE, db=db, store=store).check_once()
        self.assertEqual(first["status"], "monitor_state_conflict")
        with sqlite3.connect(source_path) as conn:
            conn.execute(f"INSERT INTO {table} VALUES(1,12,'member:\nLater SQLite source',0)")
        second = self.monitor(lambda *_: self.fail("real source replayed provider"), db=db).check_once()
        self.assertTrue(second["knowledge_event_reused"])
        self.assertEqual(self.event_count(), 1)
        self.assertTrue(Path(second["knowledge_path"]).is_file())
        self.assertEqual(load_state(self.state_path)["last_checked_ts"], 11)
        third = self.monitor(lambda *_: NEGATIVE, db=db).check_once()
        self.assertEqual(third["message_count"], 1)
        self.assertEqual(load_state(self.state_path)["last_checked_ts"], 12)


if __name__ == "__main__":
    unittest.main()
