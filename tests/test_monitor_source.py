import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from core.knowledge import KnowledgeStore
from core.monitor import TopicMonitor, load_state, save_state
from core.monitor_source import read_monitor_source_batch
from core.monitor_state import MonitorStateStore
from scripts.catch_up_monitor import drain_monitors


def raw_message(generation_id, rowid, timestamp, text, source_message_id=""):
    source_message_id = source_message_id or f"msg-{generation_id}-{rowid}"
    return {
        "timestamp": timestamp,
        "time_str": f"2026-08-30 00:{int(timestamp):02d}",
        "sender": "成员",
        "text": text,
        "type": 1,
        "source_message_id": source_message_id,
        "source_envelope": {
            "db_shard_id": generation_id,
            "create_time": timestamp,
            "rowid": rowid,
            "source_message_id": source_message_id,
        },
        "resources": [],
    }


class FakeSourceError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class CursorDB:
    def __init__(self, shards, *, complete=True, context_messages=None):
        self.shards = {
            logical_id: {
                "generation_id": generation_id,
                "messages": list(messages),
            }
            for logical_id, (generation_id, messages) in shards.items()
        }
        self.complete = complete
        self.context_messages = list(context_messages or [])
        self.inventory_revision = 1
        self.inventory_calls = 0
        self.page_calls = []

    def replace_generation(self, logical_id, generation_id, messages):
        self.shards[logical_id] = {
            "generation_id": generation_id,
            "messages": list(messages),
        }
        self.inventory_revision += 1

    def get_source_inventory(self, update=True):
        self.inventory_calls += 1
        rows = [
            {
                "logical_shard_id": logical_id,
                "generation_id": item["generation_id"],
                "state": "present" if self.complete else "missing_file",
            }
            for logical_id, item in sorted(self.shards.items())
        ]
        digest = hashlib.sha256(json.dumps(
            {"complete": self.complete, "rows": rows},
            sort_keys=True,
        ).encode()).hexdigest()
        return {
            "complete": self.complete,
            "inventory_digest": digest,
            "inventory_revision": self.inventory_revision,
            "shards": rows,
        }

    def get_cursor_page_for_shard(
        self,
        username,
        generation_id,
        *,
        cursor_token="",
        since_ts=0,
        limit=500,
    ):
        self.page_calls.append({
            "username": username,
            "generation_id": generation_id,
            "cursor_token": cursor_token,
            "limit": limit,
        })
        item = next(
            (
                value
                for value in self.shards.values()
                if value["generation_id"] == generation_id
            ),
            None,
        )
        if item is None:
            raise FakeSourceError("source_shard_unknown")
        after = json.loads(cursor_token) if cursor_token else [int(since_ts), 0]
        pending = [
            message
            for message in item["messages"]
            if (
                int(message["source_envelope"]["create_time"]),
                int(message["source_envelope"]["rowid"]),
            ) > (int(after[0]), int(after[1]))
        ]
        page = pending[:limit]
        next_cursor = cursor_token
        if page:
            envelope = page[-1]["source_envelope"]
            next_cursor = json.dumps(
                [int(envelope["create_time"]), int(envelope["rowid"])],
                separators=(",", ":"),
            )
        return {
            "messages": page,
            "next_cursor": next_cursor,
            "exhausted": len(pending) <= limit,
        }

    def get_messages(self, username, since_ts=0, limit=500, page_forward=False):
        rows = [
            message
            for message in self.context_messages
            if float(message.get("timestamp") or 0) > since_ts
        ]
        rows.sort(key=lambda message: (
            float(message.get("timestamp") or 0),
            str(message.get("source_message_id") or ""),
        ))
        return rows[:limit] if page_forward else rows[-limit:]

    @staticmethod
    def format_messages_for_ai(messages, show_group_nickname=False):
        return "\n".join(
            f"[{message['time_str']}] {message.get('sender', '')}: {message['text']}"
            for message in messages
        )


class ConflictingKnowledgeStore(KnowledgeStore):
    def __init__(self, *args, state_store, **kwargs):
        super().__init__(*args, **kwargs)
        self.state_store = state_store
        self.conflicted = False

    def apply_event(self, *args, **kwargs):
        result = super().apply_event(*args, **kwargs)
        if not self.conflicted:
            self.conflicted = True
            self.state_store.update(
                lambda state: state.update({"concurrent_writer": True})
            )
        return result


class MonitorSourceCursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "state.json")
        self.hits_dir = os.path.join(self.tmp.name, "hits")
        self.config = {
            "monitor_topic": "值得记录的变化",
            "monitor_chat_username": "chat@chatroom",
            "monitor_chat_display_name": "示例群聊",
            "monitor_max_messages_per_run": 2,
            "monitor_context_overlap_minutes": 0,
            "monitor_context_max_messages": 20,
            "monitor_ai_retry_attempts": 0,
            "monitor_cooldown_minutes": 0,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def monitor(self, db, evaluator, *, knowledge_store=None, now=1000):
        return TopicMonitor(
            db,
            self.config,
            state_file=self.state_file,
            hits_dir=self.hits_dir,
            ai_evaluator=evaluator,
            knowledge_store=knowledge_store,
            now_func=lambda: now,
        )

    def test_filtered_raw_page_advances_before_next_visible_row(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        db = CursorDB({
            "logical-a": (
                "generation-a",
                [
                    raw_message("generation-a", 1, 11, ""),
                    raw_message("generation-a", 2, 11, ""),
                    raw_message("generation-a", 3, 12, "可见消息"),
                ],
            ),
        })
        prompts = []
        monitor = self.monitor(
            db,
            lambda prompt, *_: prompts.append(prompt) or {"match": False, "score": 20},
        )

        first = monitor.check_once()
        second = monitor.check_once()
        state_at_eof = Path(self.state_file).read_bytes()
        third = monitor.check_once()

        self.assertEqual(first["status"], "source_advanced_no_visible")
        self.assertEqual(first["raw_message_count"], 2)
        self.assertEqual(second["status"], "no_match")
        self.assertIn("可见消息", prompts[0])
        self.assertEqual(third["status"], "no_messages")
        self.assertTrue(third["source_eof"])
        self.assertEqual(Path(self.state_file).read_bytes(), state_at_eof)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(
            load_state(self.state_file)["source_cursors"]["logical-a"]["cursor_token"],
            "[12,3]",
        )

    def test_same_second_rows_drain_once_by_source_identity(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        rows = [
            raw_message("generation-a", rowid, 11, f"同秒消息{rowid}")
            for rowid in range(1, 6)
        ]
        db = CursorDB({"logical-a": ("generation-a", rows)})
        prompts = []
        monitor = self.monitor(
            db,
            lambda prompt, *_: prompts.append(prompt) or {"match": False, "score": 20},
        )

        statuses = [monitor.check_once()["status"] for _ in range(4)]

        self.assertEqual(statuses, ["no_match", "no_match", "no_match", "no_messages"])
        combined = "\n".join(prompts)
        for rowid in range(1, 6):
            self.assertEqual(combined.count(f"同秒消息{rowid}"), 1)
        self.assertEqual(
            load_state(self.state_file)["source_cursors"]["logical-a"]["cursor_token"],
            "[11,5]",
        )

    def test_two_shards_merge_in_stable_source_order(self):
        db = CursorDB({
            "logical-a": (
                "generation-a",
                [
                    raw_message("generation-a", 1, 11, "A11", "source-z"),
                    raw_message("generation-a", 2, 12, "A12", "source-a"),
                ],
            ),
            "logical-b": (
                "generation-b",
                [
                    raw_message("generation-b", 1, 10, "B10", "source-b"),
                    raw_message("generation-b", 2, 11, "B11", "source-a"),
                ],
            ),
        })

        batch = read_monitor_source_batch(
            db,
            "chat@chatroom",
            {"last_checked_ts": 9},
            raw_limit=4,
            page_size=1,
        )

        self.assertEqual(
            [message["text"] for message in batch.raw_messages],
            ["B10", "B11", "A11", "A12"],
        )
        self.assertTrue(batch.source_eof)

    def test_partially_consumed_prefetch_commits_only_last_consumed_row(self):
        db = CursorDB({
            "logical-a": (
                "generation-a",
                [
                    raw_message("generation-a", 1, 10, "A10"),
                    raw_message("generation-a", 2, 100, "A100"),
                ],
            ),
            "logical-b": (
                "generation-b",
                [
                    raw_message("generation-b", 1, 11, "B11"),
                    raw_message("generation-b", 2, 12, "B12"),
                ],
            ),
        })

        batch = read_monitor_source_batch(
            db,
            "chat@chatroom",
            {"last_checked_ts": 0},
            raw_limit=2,
            page_size=2,
        )

        self.assertEqual(
            [message["text"] for message in batch.raw_messages],
            ["A10", "B11"],
        )
        self.assertEqual(
            batch.source_cursors["logical-a"]["cursor_token"],
            "[10,1]",
        )
        self.assertEqual(
            batch.source_cursors["logical-b"]["cursor_token"],
            "[11,1]",
        )
        self.assertFalse(batch.source_eof)

    def test_first_enable_binds_every_generation_from_now(self):
        db = CursorDB({
            "logical-a": (
                "generation-a",
                [raw_message("generation-a", 1, 10, "历史消息")],
            ),
        })
        calls = []
        monitor = self.monitor(
            db,
            lambda *_: calls.append(True) or {"match": False, "score": 20},
            now=1000,
        )

        first = monitor.check_once()
        second = monitor.check_once()

        self.assertEqual(first["status"], "initialized")
        self.assertEqual(second["status"], "no_messages")
        self.assertTrue(second["source_eof"])
        self.assertEqual(calls, [])
        self.assertEqual(
            load_state(self.state_file)["source_cursors"]["logical-a"],
            {
                "generation_id": "generation-a",
                "cursor_token": "[1000,0]",
                "start_cursor": "[1000,0]",
            },
        )

    def test_incomplete_inventory_prevents_ai_and_cursor_movement(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        original = Path(self.state_file).read_bytes()
        db = CursorDB({"logical-a": ("generation-a", [])}, complete=False)
        calls = []

        result = self.monitor(db, lambda *_: calls.append(True)).check_once()

        self.assertEqual(result["status"], "source_inventory_incomplete")
        self.assertEqual(calls, [])
        self.assertEqual(Path(self.state_file).read_bytes(), original)

    def test_ai_failure_preserves_every_source_cursor(self):
        db = CursorDB({
            "logical-a": (
                "generation-a",
                [raw_message("generation-a", 1, 11, "可见消息")],
            ),
        })
        inventory = db.get_source_inventory()
        original_cursors = {
            "logical-a": {
                "generation_id": "generation-a",
                "cursor_token": "[10,0]",
            },
        }
        save_state({
            "last_checked_ts": 10,
            "source_cursors": original_cursors,
            "source_inventory_digest": inventory["inventory_digest"],
            "source_inventory_revision": inventory["inventory_revision"],
        }, self.state_file)

        with self.assertRaisesRegex(RuntimeError, "provider timeout"):
            self.monitor(
                db,
                lambda *_: (_ for _ in ()).throw(RuntimeError("provider timeout")),
            ).check_once()

        state = load_state(self.state_file)
        self.assertEqual(state["last_checked_ts"], 10)
        self.assertEqual(state["source_cursors"], original_cursors)
        self.assertEqual(
            state["source_inventory_digest"], inventory["inventory_digest"]
        )
        self.assertEqual(
            state["source_inventory_revision"], inventory["inventory_revision"]
        )
        self.assertEqual(state["ai_failure_count"], 1)
        self.assertEqual(state["ai_last_error_code"], "ai_timeout")
        self.assertEqual(state["ai_next_retry_after"], 1600)

    def test_state_conflict_preserves_tentative_source_cursor(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        store = MonitorStateStore(self.state_file)
        db = CursorDB({
            "logical-a": (
                "generation-a",
                [raw_message("generation-a", 1, 11, "可见消息")],
            ),
        })

        def conflict(*_):
            store.update(lambda state: state.update({"concurrent_writer": True}))
            return {"match": False, "score": 20}

        result = self.monitor(db, conflict).check_once()
        state = load_state(self.state_file)

        self.assertEqual(result["status"], "monitor_state_conflict")
        self.assertNotIn("source_cursors", state)
        self.assertEqual(state["last_checked_ts"], 10)
        self.assertTrue(state["concurrent_writer"])

    def test_knowledge_retry_reuses_event_after_state_conflict(self):
        self.config.update({
            "monitor_knowledge_enabled": True,
            "monitor_knowledge_db": os.path.join(self.tmp.name, "knowledge.db"),
            "monitor_obsidian_root": os.path.join(self.tmp.name, "obsidian"),
        })
        save_state({"last_checked_ts": 10}, self.state_file)
        state_store = MonitorStateStore(self.state_file)
        knowledge = ConflictingKnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            state_store=state_store,
            now_func=lambda: 900,
        )
        db = CursorDB({
            "logical-a": (
                "generation-a",
                [raw_message("generation-a", 1, 11, "值得记录的新内容")],
            ),
        })
        decision = {
            "match": True,
            "score": 95,
            "title": "值得记录的新内容",
            "summary": "成员补充了一条值得记录的新内容。",
            "topic_key": "source-cursor-retry",
            "category": "工具更新",
        }
        provider_calls = []
        monitor = self.monitor(
            db,
            lambda *_: provider_calls.append(True) or decision,
            knowledge_store=knowledge,
        )

        first = monitor.check_once()
        second = monitor.check_once()

        self.assertEqual(first["status"], "monitor_state_conflict")
        self.assertEqual(second["status"], "duplicate")
        self.assertTrue(second["knowledge_event_reused"])
        self.assertEqual(provider_calls, [True])
        conn = knowledge.connect()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        finally:
            conn.close()
        self.assertEqual(
            load_state(self.state_file)["source_cursors"]["logical-a"]["cursor_token"],
            "[11,1]",
        )

    def test_committed_batch_lookup_failure_preserves_cursor_without_provider(self):
        self.config.update({
            "monitor_knowledge_enabled": True,
            "monitor_knowledge_db": os.path.join(self.tmp.name, "knowledge.db"),
            "monitor_obsidian_root": os.path.join(self.tmp.name, "obsidian"),
        })
        save_state({"last_checked_ts": 10}, self.state_file)
        original = Path(self.state_file).read_bytes()
        db = CursorDB({
            "logical-a": (
                "generation-a",
                [raw_message("generation-a", 1, 11, "值得记录的新内容")],
            ),
        })
        store = mock.Mock()
        store.recover_source_batch.side_effect = OSError("fixture lookup failure")
        provider_calls = []

        result = self.monitor(
            db,
            lambda *_: provider_calls.append(True),
            knowledge_store=store,
        ).check_once()

        self.assertEqual(result["status"], "knowledge_recovery_unavailable")
        self.assertEqual(result["error_code"], "OSError")
        self.assertEqual(provider_calls, [])
        self.assertEqual(Path(self.state_file).read_bytes(), original)

    def test_new_generation_never_inherits_old_generation_cursor(self):
        save_state({
            "last_checked_ts": 10,
            "source_cursors": {
                "logical-a": {
                    "generation_id": "generation-old",
                    "cursor_token": "[99,999]",
                },
            },
        }, self.state_file)
        db = CursorDB({
            "logical-a": (
                "generation-new",
                [raw_message("generation-new", 1, 11, "新 generation 消息")],
            ),
        })

        original = Path(self.state_file).read_bytes()
        provider_calls = []
        result = self.monitor(
            db,
            lambda *_: provider_calls.append(True) or {"match": False, "score": 20},
        ).check_once()

        self.assertEqual(result["status"], "source_generation_admission_required")
        self.assertEqual(db.page_calls, [])
        self.assertEqual(provider_calls, [])
        self.assertEqual(Path(self.state_file).read_bytes(), original)
        plan = result["generation_admission_plan"]
        self.assertEqual(plan["inventory_digest"], db.get_source_inventory()["inventory_digest"])
        self.assertEqual(plan["max_reconciliation_rows"], 2)
        self.assertEqual(plan["changes"], [{
            "logical_shard_id": "logical-a",
            "previous_generation_id": "generation-old",
            "current_generation_id": "generation-new",
            "previous_cursor_token": "[99,999]",
            "reason": "generation_replaced",
        }])
        self.assertRegex(plan["plan_id"], r"^wgadmit_[0-9a-f]{64}$")

    def test_new_logical_shard_requires_admission_before_any_page_read(self):
        save_state({
            "last_checked_ts": 10,
            "source_cursors": {
                "logical-a": {
                    "generation_id": "generation-a",
                    "cursor_token": "[10,0]",
                },
            },
        }, self.state_file)
        original = Path(self.state_file).read_bytes()
        db = CursorDB({
            "logical-a": ("generation-a", []),
            "logical-b": (
                "generation-b",
                [raw_message("generation-b", 1, 1, "late-discovered older row")],
            ),
        })

        result = self.monitor(
            db,
            lambda *_: self.fail("provider must not run before admission"),
        ).check_once()

        self.assertEqual(result["status"], "source_generation_admission_required")
        self.assertEqual(db.page_calls, [])
        self.assertEqual(Path(self.state_file).read_bytes(), original)
        self.assertEqual(
            result["generation_admission_plan"]["changes"][0]["reason"],
            "new_logical_shard",
        )

    def test_generation_change_during_ai_aborts_before_cursor_commit(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        original = Path(self.state_file).read_bytes()
        db = CursorDB({
            "logical-a": (
                "generation-a",
                [raw_message("generation-a", 1, 11, "可见消息")],
            ),
        })

        def replace_generation(*_):
            db.replace_generation(
                "logical-a",
                "generation-b",
                [raw_message("generation-b", 1, 11, "替换后的消息")],
            )
            return {"match": False, "score": 20}

        result = self.monitor(db, replace_generation).check_once()

        self.assertEqual(result["status"], "source_generation_changed")
        self.assertEqual(Path(self.state_file).read_bytes(), original)

    def test_read_only_context_never_changes_source_cursor_authority(self):
        self.config["monitor_context_overlap_minutes"] = 1
        save_state({"last_checked_ts": 10}, self.state_file)
        context = raw_message("context-generation", 99, 9, "只读上下文", "context-id")
        db = CursorDB(
            {
                "logical-a": (
                    "generation-a",
                    [raw_message("generation-a", 1, 11, "新增消息", "new-id")],
                ),
            },
            context_messages=[context],
        )
        prompts = []

        result = self.monitor(
            db,
            lambda prompt, *_: prompts.append(prompt) or {"match": False, "score": 20},
        ).check_once()

        self.assertEqual(result["status"], "no_match")
        self.assertIn("只读上下文", prompts[0])
        self.assertIn("新增消息", prompts[0])
        state = load_state(self.state_file)
        self.assertEqual(state["last_checked_ts"], 11)
        self.assertEqual(
            state["source_cursors"]["logical-a"]["cursor_token"],
            "[11,1]",
        )

    def test_catch_up_reaches_eof_only_after_filtered_and_visible_rows(self):
        self.config["monitor_max_messages_per_run"] = 1
        save_state({"last_checked_ts": 10}, self.state_file)
        db = CursorDB({
            "logical-a": (
                "generation-a",
                [
                    raw_message("generation-a", 1, 11, ""),
                    raw_message("generation-a", 2, 12, "可见消息"),
                ],
            ),
        })
        monitor = self.monitor(
            db,
            lambda *_: {"match": False, "score": 20},
        )

        result = drain_monitors(
            [({"username": "chat@chatroom", "name": "示例群聊"}, monitor)],
            max_pages_per_chat=5,
            max_minutes=1,
        )

        self.assertEqual(result["complete"], ["chat@chatroom"])
        self.assertEqual(result["pages"], {"chat@chatroom": 2})
        self.assertEqual(result["statuses"], {
            "source_advanced_no_visible": 1,
            "no_match": 1,
            "no_messages": 1,
        })

if __name__ == "__main__":
    unittest.main()
