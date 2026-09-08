import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from core.monitor import TopicMonitor, load_state, save_state
from core.knowledge import KnowledgeStore
from core.review_queue import ReviewQueue
from core.link_preview import fetch_link_preview
from core.api_errors import is_retryable_ai_error, normalize_ai_error
from core.wechat_db import _clean_msg_text


class FakeDB:
    def __init__(self, messages):
        self.messages = messages
        self.calls = 0
        self.get_message_calls = []

    def get_messages(self, username, since_ts=0, limit=500, page_forward=False):
        self.calls += 1
        self.get_message_calls.append({
            "username": username,
            "since_ts": since_ts,
            "limit": limit,
            "page_forward": page_forward,
        })
        messages = [m for m in self.messages if m["timestamp"] > since_ts]
        if page_forward and since_ts > 0:
            return messages[:limit]
        return messages[-limit:]

    def format_messages_for_ai(self, messages, show_group_nickname=False):
        return "\n".join(
            f"[{m['time_str']}] {m.get('sender', '')}: {m['text']}"
            for m in messages
        )


def msg(ts, text):
    return {
        "timestamp": ts,
        "time_str": f"2026-05-29 00:{ts:02d}",
        "sender": "成员",
        "text": text,
        "type": 1,
    }


class TopicMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "state.json")
        self.hits_dir = os.path.join(self.tmp.name, "hits")
        self.config = {
            "monitor_topic": "Claude Code 新功能",
            "monitor_chat_username": "chatroom",
            "monitor_chat_display_name": "示例技术群",
            "monitor_interval_minutes": 3,
            "monitor_max_messages_per_run": 200,
            "monitor_cooldown_minutes": 15,
            "show_group_nickname": True,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_ai_502_html_error_is_user_friendly_and_retryable(self):
        error = """<html>
<head><title>502 Bad Gateway</title></head>
<body><center><h1>502 Bad Gateway</h1></center></body>
</html>"""

        self.assertTrue(is_retryable_ai_error(error))
        self.assertEqual(
            normalize_ai_error(error, "DeepSeek"),
            "DeepSeek API 服务临时不可用，请稍后再试",
        )

    def monitor(self, db, evaluator, now=1000, relation_evaluator=None, knowledge_store=None):
        return TopicMonitor(
            db,
            self.config,
            state_file=self.state_file,
            hits_dir=self.hits_dir,
            ai_evaluator=evaluator,
            relation_evaluator=relation_evaluator,
            knowledge_store=knowledge_store,
            now_func=lambda: now,
        )

    def queue_monitor(self, db, evaluator, queue, now=1000, relation_evaluator=None, knowledge_store=None):
        return TopicMonitor(
            db,
            self.config,
            state_file=self.state_file,
            hits_dir=self.hits_dir,
            ai_evaluator=evaluator,
            relation_evaluator=relation_evaluator,
            knowledge_store=knowledge_store,
            review_queue=queue,
            now_func=lambda: now,
        )

    def test_no_messages_does_not_call_ai(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        called = []
        db = FakeDB([])

        result = self.monitor(db, lambda *_: called.append(True)).check_once()

        self.assertEqual(result["status"], "no_messages")
        self.assertEqual(called, [])

    def test_no_match_updates_bookmark(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "普通闲聊")])

        result = self.monitor(db, lambda *_: {"match": False, "score": 20}).check_once()

        self.assertEqual(result["status"], "no_match")
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 11)

    def test_wechat_forwarded_record_shell_url_is_not_saved_as_openable_link(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        shell_url = (
            "https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate"
            "?t=page/favorite_record__w_unsupport"
        )
        db = FakeDB([msg(11, f"示例成员转发了收藏里的示例合并记录 {shell_url}")])
        decision = self._knowledge_decision(
            title="示例教程合并记录",
            digest=f"1. 【00:11】示例成员转发了示例教程合并聊天记录：{shell_url}",
            links=[shell_url],
            resource_status="linked",
        )

        result = self.monitor(db, lambda *_: decision).check_once()

        self.assertEqual(result["decision"]["links"], [])
        self.assertEqual(result["decision"]["resource_status"], "mentioned_private")
        self.assertTrue(result["decision"]["resource_lead"])

    def test_match_writes_hit_and_state(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "Claude Code 发布新功能")])
        decision = {
            "match": True,
            "score": 92,
            "title": "Claude Code 新功能",
            "digest": "1. 【00:11】成员提到 Claude Code 发布新功能，值得看。",
            "summary": "1. 【00:11】成员提到 Claude Code 发布新功能，值得看。",
            "topic_key": "claude-code-new-feature",
        }

        result = self.monitor(db, lambda *_: decision).check_once()
        state = load_state(self.state_file)

        self.assertEqual(result["status"], "notified")
        self.assertTrue(os.path.exists(result["hit_path"]))
        with open(result["hit_path"], encoding="utf-8") as f:
            hit_text = f.read()
        self.assertIn("1. 【00:11】成员提到 Claude Code 发布新功能", hit_text)
        self.assertNotIn("评分", hit_text)
        self.assertNotIn("证据", hit_text)
        self.assertNotIn("新增消息", hit_text)
        self.assertEqual(state["last_checked_ts"], 11)
        self.assertEqual(state["last_topic_key"], "claude-code-new-feature")

    def test_cooldown_suppresses_duplicate_topic(self):
        save_state({
            "last_checked_ts": 10,
            "last_topic_key": "same-topic",
            "last_notified_ts": 950,
        }, self.state_file)
        db = FakeDB([msg(11, "重复讨论")])
        decision = {
            "match": True,
            "score": 90,
            "title": "重复主题",
            "summary": "重复",
            "topic_key": "same-topic",
        }

        result = self.monitor(db, lambda *_: decision, now=1000).check_once()

        self.assertEqual(result["status"], "cooldown")
        self.assertFalse(os.path.isdir(self.hits_dir))
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 11)

    def test_non_dry_monitor_pages_backlog_earliest_first_without_skipping(self):
        self.config["monitor_max_messages_per_run"] = 2
        self.config["monitor_context_overlap_minutes"] = 0
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(i, f"消息{i}") for i in range(11, 16)])
        prompts = []

        monitor = self.monitor(
            db,
            lambda prompt, *_: prompts.append(prompt) or {"match": False, "score": 20},
        )
        first = monitor.check_once()
        second = monitor.check_once()

        self.assertEqual(first["message_count"], 2)
        self.assertIn("消息11", prompts[0])
        self.assertIn("消息12", prompts[0])
        self.assertNotIn("消息13", prompts[0])
        self.assertEqual(first["last_msg_ts"], 12)
        self.assertEqual(second["message_count"], 2)
        self.assertIn("消息13", prompts[1])
        self.assertIn("消息14", prompts[1])
        self.assertNotIn("消息15", prompts[1])
        self.assertEqual(second["last_msg_ts"], 14)
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 14)
        self.assertTrue(db.get_message_calls[0]["page_forward"])

    def test_dense_overlap_retry_includes_checkpoint_second_for_legacy_state(self):
        self.config["monitor_max_messages_per_run"] = 2
        save_state({"last_checked_ts": 1000}, self.state_file)
        db = FakeDB([msg(i, f"消息{i}") for i in range(1, 1005)])
        prompts = []

        result = self.monitor(
            db,
            lambda prompt, *_: prompts.append(prompt) or {"match": False, "score": 20},
        ).check_once()

        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["message_count"], 2)
        self.assertIn("消息1000", prompts[0])
        self.assertIn("消息1001", prompts[0])
        self.assertNotIn("消息1002", prompts[0])
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 1001)
        self.assertEqual(len(db.get_message_calls), 2)
        self.assertEqual(db.get_message_calls[0]["since_ts"], 280)
        self.assertAlmostEqual(db.get_message_calls[1]["since_ts"], 999.999)

    def test_dense_overlap_retry_skips_processed_checkpoint_hashes(self):
        self.config["monitor_max_messages_per_run"] = 2
        messages = [msg(i, f"消息{i}") for i in range(1, 1005)]
        checkpoint_message = messages[999]
        save_state({
            "last_checked_ts": 1000,
            "last_checked_message_hash_ts": 1000,
            "last_checked_message_hashes": [
                TopicMonitor._message_hash(checkpoint_message),
            ],
        }, self.state_file)
        db = FakeDB(messages)
        prompts = []

        result = self.monitor(
            db,
            lambda prompt, *_: prompts.append(prompt) or {"match": False, "score": 20},
        ).check_once()

        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["message_count"], 2)
        self.assertNotIn("消息1000", prompts[0])
        self.assertIn("消息1001", prompts[0])
        self.assertIn("消息1002", prompts[0])
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 1002)
        self.assertEqual(len(db.get_message_calls), 2)
        self.assertEqual(db.get_message_calls[0]["since_ts"], 280)
        self.assertAlmostEqual(db.get_message_calls[1]["since_ts"], 999.999)

    def test_ai_failure_does_not_advance_backlog_checkpoint(self):
        self.config["monitor_max_messages_per_run"] = 2
        self.config["monitor_context_overlap_minutes"] = 0
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(i, f"消息{i}") for i in range(11, 16)])

        def fail_evaluation(*_):
            raise RuntimeError("AI unavailable")

        with self.assertRaises(RuntimeError):
            self.monitor(db, fail_evaluation).check_once()

        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 10)

    def test_same_timestamp_backlog_is_drained_across_runs(self):
        self.config["monitor_max_messages_per_run"] = 2
        self.config["monitor_context_overlap_minutes"] = 0
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, f"同秒消息{i}") for i in range(1, 6)])
        prompts = []

        monitor = self.monitor(
            db,
            lambda prompt, *_: prompts.append(prompt) or {"match": False, "score": 20},
        )
        first = monitor.check_once()
        second = monitor.check_once()
        third = monitor.check_once()

        self.assertEqual(first["message_count"], 2)
        self.assertIn("同秒消息1", prompts[0])
        self.assertIn("同秒消息2", prompts[0])
        self.assertEqual(second["message_count"], 2)
        self.assertIn("同秒消息3", prompts[1])
        self.assertIn("同秒消息4", prompts[1])
        self.assertEqual(third["message_count"], 1)
        self.assertIn("同秒消息5", prompts[2])
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 11)

    def test_message_limit_uses_latest_messages(self):
        self.config["monitor_max_messages_per_run"] = 200
        self.config["monitor_interval_minutes"] = 999
        save_state({"last_checked_ts": 0.1}, self.state_file)
        messages = [msg(i, f"消息{i}") for i in range(1, 251)]
        db = FakeDB(messages)
        seen_prompt = []

        monitor = self.monitor(db, lambda prompt, *_: seen_prompt.append(prompt) or {"match": False, "score": 0})
        result = monitor.check_once(dry_run=True)

        self.assertEqual(result["message_count"], 200)
        self.assertIn("消息51", seen_prompt[0])
        self.assertNotIn("消息50", seen_prompt[0])

    def test_recent_context_is_included_without_counting_as_new(self):
        save_state({"last_checked_ts": 100}, self.state_file)
        db = FakeDB([
            msg(95, "把会变化的 block 放在断点后面"),
            msg(101, "role 需要是 user，不然会破坏缓存"),
        ])
        seen_prompt = []

        result = self.monitor(
            db,
            lambda prompt, *_: seen_prompt.append(prompt) or {"match": False, "score": 20},
        ).check_once()

        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["message_count"], 1)
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 101)
        self.assertIn("<recent_context>", seen_prompt[0])
        self.assertIn("把会变化的 block 放在断点后面", seen_prompt[0])
        self.assertIn("role 需要是 user", seen_prompt[0])

    def test_only_recent_context_does_not_call_ai(self):
        save_state({"last_checked_ts": 100}, self.state_file)
        called = []
        db = FakeDB([msg(95, "把会变化的 block 放在断点后面")])

        result = self.monitor(db, lambda *_: called.append(True)).check_once()

        self.assertEqual(result["status"], "no_messages")
        self.assertEqual(called, [])

    def test_default_monitor_ai_uses_selected_summary_provider(self):
        class FakeProvider:
            def summarize(self, prompt):
                return {"match": False, "score": 20, "reason": "irrelevant"}

        self.config["ai_provider"] = "ollama"
        self.config["ai_model"] = "qwen3:8b"
        self.config["monitor_ai_provider"] = ""
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "普通闲聊")])
        seen_configs = []

        def fake_create_provider(config):
            seen_configs.append(dict(config))
            return FakeProvider()

        with patch("ai.factory.create_provider", fake_create_provider):
            result = TopicMonitor(
                db,
                self.config,
                state_file=self.state_file,
                hits_dir=self.hits_dir,
            ).check_once()

        self.assertEqual(result["status"], "no_match")
        self.assertEqual(seen_configs[0]["ai_provider"], "ollama")
        self.assertFalse(seen_configs[0]["ai_thinking"])

    def test_qwen_and_custom_monitors_use_the_monitor_request_contract(self):
        class FakeProvider:
            def summarize(self, prompt):
                return {"match": False, "score": 20, "reason": "irrelevant"}

        for provider_name in ("custom", "qwen"):
            with self.subTest(provider=provider_name):
                self.config["ai_provider"] = provider_name
                self.config["ai_model"] = "qwen-test"
                self.config["ai_base_url"] = "https://example.invalid/v1"
                self.config["monitor_ai_timeout_seconds"] = 120
                save_state({"last_checked_ts": 10}, self.state_file)
                db = FakeDB([msg(11, "普通闲聊")])
                seen_configs = []

                def fake_create_provider(config):
                    seen_configs.append(dict(config))
                    return FakeProvider()

                with patch("ai.factory.create_provider", fake_create_provider):
                    result = TopicMonitor(
                        db,
                        self.config,
                        state_file=self.state_file,
                        hits_dir=self.hits_dir,
                    ).check_once()

                self.assertEqual(result["status"], "no_match")
                self.assertFalse(seen_configs[0]["ai_thinking"])
                self.assertEqual(seen_configs[0]["ai_timeout_seconds"], 120)

    def test_monitor_ai_provider_can_override_summary_provider(self):
        class FakeProvider:
            def summarize(self, prompt):
                return {"match": False, "score": 20, "reason": "irrelevant"}

        self.config["ai_provider"] = "ollama"
        self.config["monitor_ai_provider"] = "deepseek"
        self.config["monitor_ai_model"] = "deepseek-chat"
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "普通闲聊")])
        seen_configs = []

        def fake_create_provider(config):
            seen_configs.append(dict(config))
            return FakeProvider()

        with patch("ai.factory.create_provider", fake_create_provider):
            TopicMonitor(
                db,
                self.config,
                state_file=self.state_file,
                hits_dir=self.hits_dir,
            ).check_once()

        self.assertEqual(seen_configs[0]["ai_provider"], "deepseek")
        self.assertEqual(seen_configs[0]["ai_model"], "deepseek-chat")

    def test_dry_run_does_not_update_state(self):
        self.config["monitor_interval_minutes"] = 999
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "Claude Code 发布新功能")])

        result = self.monitor(
            db,
            lambda *_: {"match": True, "score": 95, "topic_key": "dry", "summary": "Synthetic dry run"},
        ).check_once(dry_run=True)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 10)
        self.assertFalse(os.path.isdir(self.hits_dir))

    def test_knowledge_duplicate_suppresses_notification_and_hit_file(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        save_state({"last_checked_ts": 10}, self.state_file)
        store = KnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            now_func=lambda: 900,
        )
        first = store.apply_event(
            self._knowledge_decision(),
            [msg(10, "Claude Code 发布新功能")],
            self.config,
            {"relation": "new"},
        )
        db = FakeDB([msg(11, "Claude Code 发布新功能")])

        result = self.monitor(
            db,
            lambda *_: self._knowledge_decision(summary="1. 【00:11】成员重复提到 Claude Code 发布新功能。"),
            relation_evaluator=lambda *_: {
                "relation": "duplicate",
                "target_topic_id": first["topic_id"],
            },
        ).check_once()

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["relation"], "duplicate")
        self.assertTrue(result["knowledge_event_written"])
        self.assertFalse(os.path.isdir(self.hits_dir))
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 11)

    def test_knowledge_result_exposes_every_source_date_across_midnight(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        self.config["daily_digest_timezone"] = "Asia/Singapore"
        save_state({"last_checked_ts": 10}, self.state_file)
        before_midnight = msg(11, "午夜前消息")
        before_midnight["time_str"] = "2026-08-02 23:50"
        after_midnight = msg(12, "午夜后消息")
        after_midnight["time_str"] = "2026-08-03 00:10"

        result = self.monitor(
            FakeDB([before_midnight, after_midnight]),
            lambda *_: self._knowledge_decision(),
        ).check_once()

        self.assertTrue(result["knowledge_event_written"])
        self.assertEqual(result["source_window"], {
            "start": "2026-08-02 23:50",
            "end": "2026-08-03 00:10",
        })
        self.assertEqual(result["affected_dates"], ["2026-08-02", "2026-08-03"])

    def test_default_relation_exact_message_hash_is_duplicate(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        store = KnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            now_func=lambda: 900,
        )
        repeated_message = msg(11, "Claude Code 发布新功能")
        first = store.apply_event(
            self._knowledge_decision(),
            [repeated_message],
            self.config,
            {"relation": "new"},
        )
        save_state({"last_checked_ts": 10}, self.state_file)

        result = self.monitor(
            FakeDB([repeated_message]),
            lambda *_: self._knowledge_decision(),
            knowledge_store=store,
        ).check_once()

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["relation"], "duplicate")
        self.assertEqual(result["knowledge_topic_id"], first["topic_id"])
        self.assertEqual(result["relation_source"], "exact_message_hash")

    def test_date_index_projection_failure_advances_checkpoint_without_replaying_ai(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        store = KnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            now_func=lambda: 900,
        )
        save_state({"last_checked_ts": 10}, self.state_file)
        ai_calls = []
        monitor = self.monitor(
            FakeDB([msg(11, "Claude Code 发布新功能")]),
            lambda *_: ai_calls.append(True) or self._knowledge_decision(),
            knowledge_store=store,
        )

        with patch.object(
            store,
            "write_date_indexes",
            side_effect=OSError(11, "Resource deadlock avoided"),
        ):
            first = monitor.check_once()
            second = monitor.check_once()

        self.assertEqual(first["status"], "notified")
        self.assertEqual(first["knowledge_projection_warnings"][0]["surface"], "date_indexes")
        self.assertEqual(second["status"], "no_messages")
        self.assertEqual(ai_calls, [True])
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 11)
        conn = store.connect()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        finally:
            conn.close()

    def test_default_relation_same_chat_exact_topic_key_is_update(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        store = KnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            now_func=lambda: 900,
        )
        store.apply_event(
            self._knowledge_decision(),
            [msg(10, "Claude Code 发布新功能")],
            self.config,
            {"relation": "new"},
        )
        save_state({"last_checked_ts": 10}, self.state_file)

        result = self.monitor(
            FakeDB([msg(11, "Claude Code 又补了一条新信息")]),
            lambda *_: self._knowledge_decision(
                summary="1. 【00:11】成员补充了 Claude Code 新功能的新信息。",
            ),
            knowledge_store=store,
        ).check_once()

        self.assertEqual(result["relation"], "update")
        self.assertEqual(result["relation_source"], "same_topic_key")
        self.assertNotIn("关系判定失败", result["relation_reason"])

    def test_default_relation_cross_chat_similarity_stays_new(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        store = KnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            now_func=lambda: 900,
        )
        other_chat = dict(self.config)
        other_chat["monitor_chat_username"] = "other-chatroom"
        other_chat["monitor_chat_display_name"] = "另一个群"
        store.apply_event(
            self._knowledge_decision(),
            [msg(10, "Claude Code 发布新功能")],
            other_chat,
            {"relation": "new"},
        )
        save_state({"last_checked_ts": 10}, self.state_file)

        result = self.monitor(
            FakeDB([msg(11, "Claude Code 发布新功能")]),
            lambda *_: self._knowledge_decision(),
            knowledge_store=store,
        ).check_once()

        self.assertEqual(result["relation"], "new")
        self.assertEqual(result["relation_source"], "insufficient_relation_evidence")

    def test_default_relation_disputed_same_topic_is_contradiction(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        store = KnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            now_func=lambda: 900,
        )
        store.apply_event(
            self._knowledge_decision(),
            [msg(10, "Claude Code 发布新功能")],
            self.config,
            {"relation": "new"},
        )
        save_state({"last_checked_ts": 10}, self.state_file)

        result = self.monitor(
            FakeDB([msg(11, "刚才的发布消息被证实是假的")]),
            lambda *_: self._knowledge_decision(
                summary="1. 【00:11】刚才的发布消息被证实是假的。",
                status_hint="disputed",
            ),
            knowledge_store=store,
        ).check_once()

        self.assertEqual(result["relation"], "contradiction")
        self.assertEqual(result["relation_source"], "disputed_same_topic")

    def test_relation_evaluator_exception_falls_back_to_deterministic_result(self):
        def fail_relation_evaluator(*_args):
            raise RuntimeError("provider unavailable")

        monitor = self.monitor(
            FakeDB([]),
            lambda *_: {"match": False, "score": 0},
            relation_evaluator=fail_relation_evaluator,
        )
        candidate = {
            "topic_key": "same-key",
            "source_chat_username": "chatroom",
            "links": [],
            "status_hint": "tracking",
        }
        candidates = [{
            "topic_id": 7,
            "topic_key": "same-key",
            "source_chat_username": "chatroom",
            "links": [],
            "status": "tracking",
        }]

        result = monitor._classify_knowledge_relation(candidate, candidates, "messages")

        self.assertEqual(result["relation"], "update")
        self.assertEqual(result["target_topic_id"], 7)
        self.assertEqual(result["source"], "same_topic_key")

    def test_default_relation_matches_legacy_topic_without_chat_username(self):
        monitor = self.monitor(FakeDB([]), lambda *_: {"match": False, "score": 0})
        candidate = {
            "topic_key": "same-key",
            "source_chat_username": "chatroom",
            "source_chat": "示例技术群",
            "links": [],
            "status_hint": "tracking",
        }
        candidates = [{
            "topic_id": 8,
            "topic_key": "same-key",
            "source_chat_username": "",
            "source_chat": "示例技术群",
            "links": [],
            "status": "tracking",
        }]

        result = monitor._classify_knowledge_relation(candidate, candidates, "messages")

        self.assertEqual(result["relation"], "update")
        self.assertEqual(result["target_topic_id"], 8)

    def test_relation_hash_lookup_error_is_reported_and_falls_back_to_new(self):
        class FailingHashStore:
            def topic_id_for_message_hash(self, *_args, **_kwargs):
                raise RuntimeError("database unavailable")

            def find_candidates(self, _candidate):
                return [{
                    "topic_id": 7,
                    "topic_key": "claude-code-new-feature",
                    "source_chat_username": "chatroom",
                    "source_chat": "示例技术群",
                    "links": [],
                    "status": "tracking",
                    "score": 100,
                }]

        monitor = self.monitor(
            FakeDB([]),
            lambda *_: {"match": False, "score": 0},
            knowledge_store=FailingHashStore(),
        )

        result = monitor._process_with_knowledge(
            self._knowledge_decision(summary="Claude Code 发布新功能。"),
            [msg(11, "Claude Code 发布新功能")],
            "messages",
            dry_run=True,
        )

        self.assertEqual(result["relation"], "new")
        self.assertEqual(result["relation_source"], "insufficient_relation_evidence")
        self.assertEqual(result["relation_lookup_error"], "RuntimeError")

    def test_related_ids_require_strong_same_chat_evidence(self):
        monitor = self.monitor(FakeDB([]), lambda *_: {"match": False, "score": 0})
        candidate = {
            "title": "Example Project memory bridge launch",
            "topic_key": "example-project-launch",
            "entities": ["Example Project", "Claude"],
            "source_chat_username": "chatroom",
            "source_chat": "示例技术群",
            "links": [],
        }
        candidates = [
            {
                "topic_id": 1,
                "title": "Example Project memory bridge review",
                "topic_key": "example-project-review",
                "entities": ["Example Project"],
                "source_chat_username": "chatroom",
                "links": [],
                "score": 90,
            },
            {
                "topic_id": 2,
                "title": "Different unrelated title",
                "topic_key": "different",
                "entities": ["Claude"],
                "source_chat_username": "chatroom",
                "links": [],
                "score": 95,
            },
            {
                "topic_id": 3,
                "title": "Example Project memory bridge review",
                "topic_key": "cross-chat",
                "entities": ["Example Project"],
                "source_chat_username": "other-chatroom",
                "links": [],
                "score": 99,
            },
            {
                "topic_id": 4,
                "title": "Example Project memory bridge review 历史总结",
                "topic_key": "history-summary:chatroom:2026-07-01",
                "entities": ["Example Project"],
                "source_chat_username": "chatroom",
                "links": [],
                "score": 99,
            },
        ]

        self.assertEqual(monitor._strong_related_topic_ids(candidate, candidates), [1])

    def test_review_queue_created_for_file_hit_and_duplicate_reuses_item(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        file_dir = os.path.join(self.tmp.name, "xwechat_files", "wxid_test", "msg", "file", "2026-05")
        db_dir = os.path.join(self.tmp.name, "xwechat_files", "wxid_test", "db_storage")
        os.makedirs(file_dir, exist_ok=True)
        os.makedirs(db_dir, exist_ok=True)
        self.config["db_dir"] = db_dir
        queue = ReviewQueue(os.path.join(self.tmp.name, "review_queue"))
        save_state({"last_checked_ts": 10}, self.state_file)

        result = self.queue_monitor(
            FakeDB([msg(11, "[文件] example-toolkit.zip")]),
            lambda *_: self._knowledge_decision(
                title="Example Toolkit patch",
                summary="1. 【00:11】成员分享了 example-toolkit.zip，可以评估部署。",
                links=[],
            ),
            queue,
        ).check_once()
        reused = self.queue_monitor(
            FakeDB([msg(12, "[文件] example-toolkit.zip")]),
            lambda *_: self._knowledge_decision(
                title="Example Toolkit patch",
                summary="1. 【00:12】成员重复分享 example-toolkit.zip。",
                links=[],
            ),
            queue,
            relation_evaluator=lambda *_: {
                "relation": "duplicate",
                "target_topic_id": result["knowledge_topic_id"],
            },
        ).check_once()

        self.assertEqual(result["status"], "notified")
        self.assertEqual(result["review_queue_item"]["suggested_action"], "import_resource")
        self.assertEqual(result["review_queue_item"]["resources"]["files"][0]["name"], "example-toolkit.zip")
        self.assertEqual(reused["status"], "duplicate")
        self.assertNotIn("review_queue_item", reused)
        self.assertEqual(queue.pending_count(), 1)

    def test_review_queue_created_for_p1_p2_hits_and_skips_p3_digest_only_hits(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        queue = ReviewQueue(os.path.join(self.tmp.name, "review_queue"))
        save_state({"last_checked_ts": 10}, self.state_file)

        link_result = self.queue_monitor(
            FakeDB([msg(11, "这里有 source package https://github.com/example/tool")]),
            lambda *_: self._knowledge_decision(
                title="source package link",
                summary="1. 【00:11】成员分享了 repo 链接。",
                links=["https://github.com/example/tool"],
            ),
            queue,
        ).check_once()
        save_state({"last_checked_ts": 11}, self.state_file)
        design_result = self.queue_monitor(
            FakeDB([msg(12, "AI伴侣交互里的连续性和自主权设计挺关键")]),
            lambda *_: self._knowledge_decision(
                title="AI伴侣交互连续性设计",
                summary="1. 【00:12】成员讨论了连续性和自主权设计。",
                topic_key="human-ai-continuity-design",
                links=[],
            ),
            queue,
        ).check_once()

        self.assertEqual(link_result["review_queue_item"]["priority"], "P2")
        self.assertEqual(link_result["review_queue_item"]["suggested_action"], "evaluate_reference")
        self.assertTrue(link_result["notify_now"])
        self.assertFalse(design_result["notify_now"])
        self.assertEqual(design_result["review_priority"], "P3")
        self.assertEqual(design_result["suggested_action"], "archive_reference")
        self.assertNotIn("review_queue_item", design_result)
        self.assertEqual(queue.pending_count(), 1)

    def test_resource_lead_without_file_or_link_creates_follow_up_queue_item(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        queue = ReviewQueue(os.path.join(self.tmp.name, "review_queue"))
        save_state({"last_checked_ts": 10}, self.state_file)

        result = self.queue_monitor(
            FakeDB([
                msg(11, "示例作者制作了一份互动设计资源"),
                msg(12, "可以私发吗，求一份"),
            ]),
            lambda *_: self._knowledge_decision(
                title="示例互动设计资源线索",
                summary="1. 【00:11】示例成员提到一份互动设计资源；【00:12】其他示例成员请求私发，但还没有文件或链接。",
                topic_key="interaction-skills-private-share",
                category="教程资源",
                key_facts=["示例作者提到资源已完成，可能需要在对话中请求"],
                links=[],
                event_type="resource_lead",
                resource_lead=True,
                resource_status="mentioned_private",
                lead_key="interaction-skills-private-share",
            ),
            queue,
        ).check_once()

        self.assertEqual(result["status"], "notified")
        self.assertEqual(result["review_priority"], "P2")
        self.assertEqual(result["suggested_action"], "follow_up_resource")
        self.assertTrue(result["notify_now"])
        self.assertEqual(result["review_queue_item"]["suggested_action"], "follow_up_resource")
        self.assertTrue(result["review_queue_item"]["resource_lead"])
        self.assertEqual(result["review_queue_item"]["resource_status"], "mentioned_private")
        self.assertEqual(result["review_queue_item"]["lead_key"], "interaction-skills-private-share")
        self.assertEqual(queue.pending_count(), 1)

    def test_pure_design_philosophy_discussion_stays_digest_only_without_queue_item(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        queue = ReviewQueue(os.path.join(self.tmp.name, "review_queue"))
        save_state({"last_checked_ts": 10}, self.state_file)

        result = self.queue_monitor(
            FakeDB([msg(11, "AI伴侣交互里的连续性、自主权和记忆边界设计挺关键")]),
            lambda *_: self._knowledge_decision(
                title="AI伴侣交互连续性设计",
                summary="1. 【00:11】成员讨论了连续性、自主权和记忆边界设计。",
                topic_key="human-ai-continuity-boundary-design",
                category="设计讨论",
                links=[],
                event_type="discussion",
                resource_lead=False,
                resource_status="none",
            ),
            queue,
        ).check_once()

        self.assertEqual(result["status"], "notified")
        self.assertFalse(result["notify_now"])
        self.assertEqual(result["review_priority"], "P3")
        self.assertEqual(result["suggested_action"], "archive_reference")
        self.assertNotIn("review_queue_item", result)
        self.assertEqual(queue.pending_count(), 0)

    def test_high_signal_read_note_not_persisted_to_review_queue(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        queue = ReviewQueue(os.path.join(self.tmp.name, "review_queue"))
        save_state({"last_checked_ts": 10}, self.state_file)

        result = self.queue_monitor(
            FakeDB([msg(11, "示例人机互动玩法：语音模式与状态反馈")]),
            lambda *_: self._knowledge_decision(
                title="示例人机互动玩法：语音模式与状态反馈",
                summary="1. 【00:11】示例成员讨论了语音模式、状态反馈和互动玩法配置。",
                topic_key="ai-intimacy-play-voice",
                category="设计讨论",
                links=[],
                event_type="discussion",
                resource_lead=False,
                resource_status="none",
            ),
            queue,
        ).check_once()

        self.assertEqual(result["status"], "notified")
        self.assertEqual(result["review_priority"], "P2")
        self.assertEqual(result["suggested_action"], "read_note")
        self.assertEqual(result["actionability"], "none")
        self.assertFalse(result["queue_worthy"])
        self.assertTrue(result["notify_now"])
        self.assertNotIn("review_queue_item", result)
        self.assertEqual(queue.pending_count(), 0)

    def test_provider_promo_with_link_is_retained_but_not_immediate_queue(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        queue = ReviewQueue(os.path.join(self.tmp.name, "review_queue"))
        save_state({"last_checked_ts": 10}, self.state_file)

        result = self.queue_monitor(
            FakeDB([msg(11, "Example AI 最近有两个月试用活动 https://example.com/example-ai-promo")]),
            lambda *_: self._knowledge_decision(
                title="Example AI 两个月试用活动",
                summary="1. 【00:11】成员提到 Example AI 最近有两个月试用活动，值得 daily digest 留一下。",
                topic_key="example-ai-trial-promo",
                category="工具更新",
                links=["https://example.com/example-ai-promo"],
                event_type="provider_activity",
                resource_lead=False,
                resource_status="none",
            ),
            queue,
        ).check_once()

        self.assertEqual(result["status"], "notified")
        self.assertFalse(result["notify_now"])
        self.assertEqual(result["review_priority"], "P3")
        self.assertEqual(result["suggested_action"], "read_note")
        self.assertEqual(result["actionability"], "none")
        self.assertFalse(result["queue_worthy"])
        self.assertNotIn("review_queue_item", result)
        self.assertEqual(queue.pending_count(), 0)

    def test_desire_toy_skill_resource_lead_is_not_downgraded_by_topic(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        queue = ReviewQueue(os.path.join(self.tmp.name, "review_queue"))
        save_state({"last_checked_ts": 10}, self.state_file)

        result = self.queue_monitor(
            FakeDB([
                msg(11, "我做了一个示例偏好配置 skill，暂时不方便公开"),
                msg(12, "可以私发一份示例资源吗"),
            ]),
            lambda *_: self._knowledge_decision(
                title="示例偏好配置 skill 资源线索",
                summary="1. 【00:11】示例成员提到偏好配置 skill 暂不公开；【00:12】其他示例成员开始索要。",
                topic_key="example-preference-skill-private-share",
                category="教程资源",
                links=[],
                event_type="resource_lead",
                resource_lead=True,
                resource_status="mentioned_private",
                lead_key="example-preference-skill-private-share",
            ),
            queue,
        ).check_once()

        self.assertEqual(result["review_priority"], "P2")
        self.assertEqual(result["suggested_action"], "follow_up_resource")
        self.assertTrue(result["notify_now"])
        self.assertEqual(queue.pending_count(), 1)

    def test_attached_philosophy_design_doc_remains_review_worthy(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        queue = ReviewQueue(os.path.join(self.tmp.name, "review_queue"))
        save_state({"last_checked_ts": 10}, self.state_file)

        result = self.queue_monitor(
            FakeDB([
                msg(11, "[文件] human-ai-design-philosophy.md"),
                msg(12, "里面可能有 repo/source design"),
            ]),
            lambda *_: self._knowledge_decision(
                title="人机互动设计文档",
                summary="1. 【00:11】成员分享了 human-ai-design-philosophy.md；【00:12】补充说里面可能包含 repo/source design。",
                topic_key="human-ai-design-philosophy-doc",
                category="设计文档",
                links=[],
                event_type="resource",
                resource_status="attached",
            ),
            queue,
        ).check_once()

        self.assertEqual(result["review_priority"], "P2")
        self.assertEqual(result["suggested_action"], "import_resource")
        self.assertTrue(result["notify_now"])
        self.assertEqual(result["review_queue_item"]["resources"]["files"][0]["name"], "human-ai-design-philosophy.md")
        self.assertEqual(queue.pending_count(), 1)

    def test_duplicate_resource_lead_reuses_pending_queue_item_by_lead_key(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        queue = ReviewQueue(os.path.join(self.tmp.name, "review_queue"))
        save_state({"last_checked_ts": 10}, self.state_file)

        first = self.queue_monitor(
            FakeDB([msg(11, "示例作者制作了一份互动设计资源，可以回头私发")]),
            lambda *_: self._knowledge_decision(
                title="示例互动设计资源线索",
                summary="1. 【00:11】示例作者提到资源之后可以私发。",
                topic_key="interaction-skills-private-share",
                links=[],
                event_type="resource_lead",
                resource_lead=True,
                resource_status="mentioned_pending",
                lead_key="interaction-skills-private-share",
            ),
            queue,
        ).check_once()

        save_state({"last_checked_ts": 11}, self.state_file)
        second = self.queue_monitor(
            FakeDB([msg(12, "刚才那份示例资源我也想要一份")]),
            lambda *_: self._knowledge_decision(
                title="其他示例成员继续索要资源",
                summary="1. 【00:12】其他示例成员继续索要同一份资源，artifact 仍未到手。",
                topic_key="interaction-skills-private-share",
                links=[],
                event_type="resource_lead",
                resource_lead=True,
                resource_status="mentioned_pending",
                lead_key="interaction-skills-private-share",
            ),
            queue,
            relation_evaluator=lambda *_: {
                "relation": "update",
                "target_topic_id": first["knowledge_topic_id"],
            },
        ).check_once()

        self.assertEqual(first["review_queue_item"]["id"], second["review_queue_item"]["id"])
        self.assertEqual(queue.pending_count(), 1)

    def test_knowledge_new_update_and_contradiction_notify(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        save_state({"last_checked_ts": 10}, self.state_file)

        new_result = self.monitor(
            FakeDB([msg(11, "Claude Code 发布新功能")]),
            lambda *_: self._knowledge_decision(),
        ).check_once()
        self.assertEqual(new_result["status"], "notified")
        self.assertEqual(new_result["relation"], "new")
        self.assertTrue(os.path.exists(new_result["hit_path"]))
        self.assertTrue(os.path.exists(new_result["knowledge_path"]))

        save_state({"last_checked_ts": 11}, self.state_file)
        update_result = self.monitor(
            FakeDB([msg(12, "Claude Code 新功能补了链接")]),
            lambda *_: self._knowledge_decision(
                summary="1. 【00:12】成员补充了 Claude Code 新功能链接。",
                key_facts=["成员补充了 Claude Code 新功能链接"],
                links=["https://example.com/codex"],
            ),
            relation_evaluator=lambda *_: {"relation": "update"},
        ).check_once()
        self.assertEqual(update_result["status"], "notified")
        self.assertEqual(update_result["relation"], "update")
        self.assertTrue(update_result["title"].startswith("新线索:"))

        save_state({"last_checked_ts": 12}, self.state_file)
        contradiction_result = self.monitor(
            FakeDB([msg(13, "刚才那个 Claude Code 新功能截图是假的")]),
            lambda *_: self._knowledge_decision(
                title="Claude Code 新功能截图被辟谣",
                summary="1. 【00:13】成员指出刚才的新功能截图是假的。",
                key_facts=["新功能截图被指出是假的"],
                status_hint="disputed",
            ),
            relation_evaluator=lambda *_: {"relation": "contradiction"},
        ).check_once()
        self.assertEqual(contradiction_result["status"], "notified")
        self.assertEqual(contradiction_result["relation"], "contradiction")
        self.assertTrue(contradiction_result["title"].startswith("反转/辟谣:"))

    def test_knowledge_dry_run_does_not_write_db_or_markdown(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        self.config["monitor_interval_minutes"] = 999
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "Claude Code 发布新功能")])

        result = self.monitor(
            db,
            lambda *_: self._knowledge_decision(),
            knowledge_store=KnowledgeStore(
                self.config["monitor_knowledge_db"],
                self.config["monitor_obsidian_root"],
                read_only=True,
            ),
        ).check_once(dry_run=True)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["relation"], "new")
        self.assertFalse(os.path.exists(self.config["monitor_knowledge_db"]))
        self.assertFalse(os.path.exists(self.config["monitor_obsidian_root"]))
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 10)

    def test_raw_message_links_are_saved_when_model_omits_links(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "Claude Code 新功能来了 https://example.com/codex?from=group。")])

        result = self.monitor(
            db,
            lambda *_: self._knowledge_decision(
                summary="1. 【00:11】成员提到 Claude Code 新功能，值得看。",
                links=[],
            ),
        ).check_once()

        self.assertEqual(result["status"], "notified")
        with open(result["knowledge_path"], encoding="utf-8") as f:
            markdown = f.read()
        self.assertIn("https://example.com/codex?from=group", markdown)

    def test_context_links_are_not_saved_when_model_omits_links(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        save_state({"last_checked_ts": 100}, self.state_file)
        db = FakeDB([
            msg(95, "旧链接 https://example.com/old-context"),
            msg(101, "Claude Code 新功能来了，这次没有贴链接。"),
        ])

        result = TopicMonitor(
            db,
            self.config,
            state_file=self.state_file,
            hits_dir=self.hits_dir,
            ai_evaluator=lambda *_: self._knowledge_decision(
                summary="1. 【00:101】成员提到 Claude Code 新功能，但没有贴链接。",
                links=[],
            ),
            link_preview_fetcher=lambda url: {"url": url, "status": "empty"},
            now_func=lambda: 1000,
        ).check_once()

        self.assertEqual(result["status"], "notified")
        with open(result["knowledge_path"], encoding="utf-8") as f:
            markdown = f.read()
        self.assertNotIn("https://example.com/old-context", markdown)

    def test_prompt_keeps_ai_interaction_and_multiple_candidate_guidance(self):
        monitor = self.monitor(FakeDB([]), lambda *_: {"match": False, "score": 0})
        messages = [msg(11, "示例模型做互动问卷时加载了测试技能，并给出了示例结果")]
        prompt = monitor._build_prompt(
            messages,
            "2026-05-29 00:11 成员: 示例模型做互动问卷时加载了测试技能，并给出了示例结果",
            self.config["monitor_topic"],
        )

        self.assertIn("多个候选都达到通知门槛", prompt)
        self.assertIn("AI/agent/模型互动实验", prompt)
        self.assertIn("模型行为边界或偏好反馈", prompt)
        self.assertIn("不要按单个敏感词字面过滤或命中", prompt)
        self.assertIn("亲密关系、身体体验、情感陪伴或 AI 伴侣交互", prompt)
        self.assertIn("resource_lead", prompt)
        self.assertIn("mentioned_private", prompt)

    def test_human_ai_target_chat_prompt_uses_fixed_taxonomy(self):
        self.config["monitor_chat_display_name"] = "示例人机互动群"
        monitor = self.monitor(FakeDB([]), lambda *_: {"match": False, "score": 0})
        messages = [msg(11, "示例讨论涉及长期记忆、共读玩法和模型边界")]

        prompt = monitor._build_prompt(
            messages,
            "2026-05-29 00:11 成员: 示例讨论涉及长期记忆、共读玩法和模型边界",
            self.config["monitor_topic"],
        )

        self.assertIn("human_ai_intimacy_v1", prompt)
        self.assertIn("category 必须从以下固定分类中选择", prompt)
        self.assertIn("互动实验与玩法", prompt)
        self.assertIn("记忆与连续性", prompt)
        self.assertIn("模型与平台", prompt)
        self.assertIn("工具与方法", prompt)
        self.assertIn("semantic_tags", prompt)

    def test_assigned_renamed_chat_uses_fixed_prompt_categories(self):
        self.config.update({
            "monitor_chat_username": "room@chatroom",
            "monitor_chat_display_name": "群已经改名",
            "monitor_chat_taxonomy_profiles": {
                "room@chatroom": "human_ai_intimacy_v1",
            },
            "monitor_chat_aliases": {"room@chatroom": "示例人机互动群"},
        })

        text = self.monitor(FakeDB([]), lambda *_: {})._build_taxonomy_context()

        self.assertIn("category 必须从以下固定分类中选择", text)
        self.assertIn("记忆与连续性", text)

    def test_normalized_decision_keeps_semantic_tags(self):
        monitor = self.monitor(FakeDB([]), lambda *_: {"match": False, "score": 0})

        normalized = monitor._normalize_decision({
            "match": True,
            "score": 90,
            "title": "共读玩法",
            "digest": "1. 【00:11】成员讨论共读玩法。",
            "topic_key": "shared-reading-play",
            "category": "互动实验与玩法",
            "semantic_tags": ["共读", "记忆", "共读"],
        })

        self.assertEqual(normalized["semantic_tags"], ["共读", "记忆"])

    def test_link_preview_context_is_disabled_by_default(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "Claude Code 新功能介绍 https://example.com/codex")])
        seen_prompt = []
        preview_calls = []

        monitor = TopicMonitor(
            db,
            self.config,
            state_file=self.state_file,
            hits_dir=self.hits_dir,
            ai_evaluator=lambda prompt, *_: seen_prompt.append(prompt) or {"match": False, "score": 0},
            link_preview_fetcher=lambda url: preview_calls.append(url) or {
                "url": url,
                "status": "ok",
                "title": "不应默认读取",
                "summary": "不应默认进入 prompt。",
            },
            now_func=lambda: 1000,
        )

        result = monitor.check_once()

        self.assertEqual(result["status"], "no_match")
        self.assertEqual(preview_calls, [])
        self.assertNotIn("<link_previews>", seen_prompt[0])

    def test_old_enabled_link_preview_config_remains_zero_network(self):
        self.config["monitor_fetch_links"] = True
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "Claude Code 新功能介绍 https://example.com/codex")])
        seen_prompt = []
        preview_calls = []

        monitor = TopicMonitor(
            db,
            self.config,
            state_file=self.state_file,
            hits_dir=self.hits_dir,
            ai_evaluator=lambda prompt, *_: seen_prompt.append(prompt) or {"match": False, "score": 0},
            link_preview_fetcher=lambda url: preview_calls.append(url) or {
                "url": url,
                "status": "ok",
                "title": "Claude Code 新功能说明",
                "summary": "介绍了一个可以启发实际项目的功能更新。",
            },
            now_func=lambda: 1000,
        )

        result = monitor.check_once()

        self.assertEqual(result["status"], "no_match")
        self.assertEqual(preview_calls, [])
        self.assertNotIn("<link_context>", seen_prompt[0])
        self.assertNotIn("Claude Code 新功能说明", seen_prompt[0])
        self.assertIn("Remote link preview 已禁用", seen_prompt[0])

    def test_signed_url_credentials_never_enter_ai_prompts(self):
        secret_url = (
            "https://example.com/object?X-Amz-Credential=prompt-secret"
            "&X-Amz-Signature=signature-secret&view=1"
            "#access_token=fragment-secret"
        )
        save_state({"last_checked_ts": 10}, self.state_file)
        seen_prompt = []
        db = FakeDB([msg(11, f"资源在这里 {secret_url}")])
        monitor = TopicMonitor(
            db,
            self.config,
            state_file=self.state_file,
            hits_dir=self.hits_dir,
            ai_evaluator=lambda prompt, *_: seen_prompt.append(prompt) or {
                "match": False,
                "score": 0,
            },
            now_func=lambda: 1000,
        )

        result = monitor.check_once()
        relation_prompt = monitor._build_relation_prompt(
            {"links": [secret_url]},
            [{"topic_id": 1, "links": [secret_url]}],
            f"source {secret_url}",
        )

        self.assertEqual(result["status"], "no_match")
        for prompt in (seen_prompt[0], relation_prompt):
            for secret in ("prompt-secret", "signature-secret", "fragment-secret"):
                self.assertNotIn(secret, prompt)
            self.assertIn("REDACTED", prompt)
            self.assertIn("view=1", prompt)

    def test_ai_error_output_redacts_url_credentials(self):
        self.config.update({
            "monitor_ai_retry_attempts": 1,
            "monitor_ai_retry_delay_seconds": 0,
        })
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "一条普通消息")])
        error_url = "https://example.com/fail?token=error-secret&view=1"
        monitor = TopicMonitor(
            db,
            self.config,
            state_file=self.state_file,
            hits_dir=self.hits_dir,
            ai_evaluator=lambda *_: (_ for _ in ()).throw(
                TimeoutError(f"request timed out at {error_url}")
            ),
            now_func=lambda: 1000,
        )
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(RuntimeError) as raised:
            monitor.check_once()

        self.assertNotIn("error-secret", output.getvalue())
        self.assertNotIn("error-secret", str(raised.exception))
        self.assertIn("token=REDACTED", output.getvalue())
        self.assertIn("view=1", str(raised.exception))

    def test_wechat_record_link_preview_is_also_disabled(self):
        preview = fetch_link_preview(
            "https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate"
            "?t=page/favorite_record__w_unsupport&from=singlemessage"
        )

        self.assertEqual(preview["status"], "link_preview_disabled")
        self.assertEqual(preview["network_requests"], 0)

    def _knowledge_decision(self, **overrides):
        data = {
            "match": True,
            "score": 92,
            "title": "Claude Code 新功能",
            "digest": "1. 【00:11】成员提到 Claude Code 发布新功能，值得看。",
            "topic_key": "claude-code-new-feature",
            "category": "工具更新",
            "entities": ["Claude Code"],
            "key_facts": ["Claude Code 发布新功能"],
            "links": [],
            "event_type": "release",
            "status_hint": "tracking",
        }
        if "summary" in overrides:
            summary = overrides["summary"]
            overrides["digest"] = summary
        data.update(overrides)
        return data


class WeChatMessageCleanTests(unittest.TestCase):
    def test_appmsg_link_keeps_url(self):
        raw = (
            "<msg><appmsg><type>5</type>"
            "<title><![CDATA[Claude Code 新功能说明]]></title>"
            "<url><![CDATA[https://example.com/codex?x=1&y=2]]></url>"
            "</appmsg></msg>"
        )

        cleaned = _clean_msg_text(raw)

        self.assertEqual(
            cleaned,
            "[链接] Claude Code 新功能说明 https://example.com/codex?x=1&y=2",
        )

    def test_appmsg_link_keeps_escaped_url(self):
        raw = (
            "<msg><appmsg><type>5</type>"
            "<title>Claude Code 新功能说明</title>"
            "<url>https://example.com/codex?x=1&amp;y=2</url>"
            "</appmsg></msg>"
        )

        cleaned = _clean_msg_text(raw)

        self.assertIn("https://example.com/codex?x=1&y=2", cleaned)

    def test_appmsg_link_without_title_keeps_exact_url(self):
        raw = (
            "<msg><appmsg><type>5</type><title></title>"
            "<url><![CDATA[https://example.com/titleless?x=1&y=2]]></url>"
            "</appmsg></msg>"
        )

        cleaned = _clean_msg_text(raw)

        self.assertEqual(
            cleaned,
            "[链接] https://example.com/titleless?x=1&y=2",
        )

    def test_forwarded_record_shell_is_not_presented_as_a_resource_link(self):
        raw = (
            "<msg><appmsg><type>19</type><title>聊天记录</title>"
            "<url>https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate"
            "?t=page/favorite_record__w_unsupport</url></appmsg></msg>"
        )

        cleaned = _clean_msg_text(raw)

        self.assertEqual(cleaned, "[聊天记录] 聊天记录")
        self.assertNotIn("https://", cleaned)

    def test_forwarded_chat_record_extracts_embedded_items(self):
        raw = """<msg><appmsg>
<title>群聊的聊天记录</title>
<des>盏:[文件] bdsmtest_long.zip</des>
<type>19</type>
<url>https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate?t=page/favorite_record__w_unsupport</url>
<recorditem><![CDATA[<recordinfo>
<title>群聊的聊天记录</title>
<datalist count="3">
<dataitem datatype="8"><sourcename>盏</sourcename><sourcetime>2026-6-3 凌晨5:36</sourcetime><datatitle>bdsmtest_long.zip</datatitle><datafmt>zip</datafmt></dataitem>
<dataitem datatype="1"><sourcename>盏</sourcename><sourcetime>2026-6-3 凌晨5:37</sourcetime><datadesc>机做的</datadesc></dataitem>
<dataitem datatype="1"><sourcename>盏</sourcename><sourcetime>2026-6-3 凌晨5:37</sourcetime><datadesc>https://bdsmtest.org/questions</datadesc></dataitem>
</datalist>
</recordinfo>]]></recorditem>
</appmsg></msg>"""

        cleaned = _clean_msg_text(raw)

        self.assertIn("[聊天记录] 群聊的聊天记录", cleaned)
        self.assertIn("[文件] bdsmtest_long.zip", cleaned)
        self.assertIn("机做的", cleaned)
        self.assertIn("https://bdsmtest.org/questions", cleaned)
        self.assertNotIn("favorite_record__w_unsupport", cleaned)

    def test_quoted_forwarded_chat_record_extracts_refermsg_record(self):
        raw = """<msg><appmsg>
<title>这里是测试</title>
<type>57</type>
<refermsg><content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;盏的聊天记录&lt;/title&gt;&lt;type&gt;19&lt;/type&gt;&lt;recorditem&gt;&lt;![CDATA[&lt;recordinfo&gt;&lt;desc&gt;盏: [文件] test.zip
盏: https://example.com/questions&lt;/desc&gt;&lt;datalist count="0"/&gt;&lt;/recordinfo&gt;]]&gt;&lt;/recorditem&gt;&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg>
</appmsg></msg>"""

        cleaned = _clean_msg_text(raw)

        self.assertIn("[回复] 这里是测试", cleaned)
        self.assertIn("[聊天记录] 聊天记录", cleaned)
        self.assertIn("盏: [文件] test.zip", cleaned)
        self.assertIn("https://example.com/questions", cleaned)


if __name__ == "__main__":
    unittest.main()
