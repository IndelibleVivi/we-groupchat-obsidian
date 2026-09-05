import errno
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime
from unittest.mock import patch

import core.knowledge as knowledge
from core.knowledge import (
    OBSIDIAN_CATEGORY_INDEX_FILENAME,
    OBSIDIAN_DATE_INDEX_MARKER,
    HUMAN_AI_INTIMACY_PROFILE,
    KnowledgeStore,
    TAXONOMY_PROFILES,
    _render_relation_markdown_line,
    build_message_hash,
    ensure_obsidian_vault,
    normalize_candidate,
    safe_path_part,
)
from core.taxonomy_assignment import FREE_FORM_PROFILE, TaxonomyResolution


def msg(ts, sender, text):
    return {
        "timestamp": ts,
        "time_str": f"2026-05-29 03:{ts:02d}",
        "sender": sender,
        "text": text,
    }


def msg_at(time_str, sender="示例成员甲", text="测试消息"):
    return {
        "timestamp": 1,
        "time_str": time_str,
        "sender": sender,
        "text": text,
    }


def candidate(**overrides):
    data = {
        "title": "Example Model 2.0 发布传闻",
        "summary": "1. 【03:16】群里提到 Example Model 2.0 可能今天发布。",
        "topic_key": "example-model-2.0-release-rumor",
        "category": "AI模型",
        "entities": ["Claude", "Opus"],
        "key_facts": ["群里认为 Example Model 2.0 可能今天发布"],
        "links": ["https://example.com/example-model"],
        "event_type": "rumor",
        "status_hint": "rumor",
    }
    data.update(overrides)
    return data


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "knowledge.db")
        self.obsidian_root = os.path.join(self.tmp.name, "obsidian")
        self.config = {"monitor_chat_display_name": "示例技术群"}
        self.messages = [
            msg(16, "示例成员甲", "示例模型的新版本将在下周发布"),
            msg(17, "示例成员乙", "我会整理一份公开测试笔记"),
        ]
        self.store = KnowledgeStore(
            self.db_path,
            self.obsidian_root,
            now_func=lambda: 1000,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, table):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        finally:
            conn.close()

    def test_atomic_markdown_writes_use_unique_temps_and_serialize_same_path(self):
        path = os.path.join(self.tmp.name, "shared.md")
        original_replace = os.replace
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        call_lock = threading.Lock()
        calls = []

        def controlled_replace(source, target):
            with call_lock:
                calls.append(source)
                ordinal = len(calls)
            if ordinal == 1:
                first_entered.set()
                release_first.wait(timeout=2)
            else:
                second_entered.set()
            original_replace(source, target)

        errors = []

        def write(value):
            try:
                KnowledgeStore._atomic_write_text(path, value)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch.object(knowledge.os, "replace", side_effect=controlled_replace):
            first = threading.Thread(target=write, args=("first\n",))
            second = threading.Thread(target=write, args=("second\n",))
            first.start()
            self.assertTrue(first_entered.wait(timeout=2))
            second.start()
            self.assertFalse(second_entered.wait(timeout=0.1))
            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertTrue(second_entered.is_set())
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "second\n")
        self.assertEqual(
            [name for name in os.listdir(self.tmp.name) if name.endswith(".tmp")],
            [],
        )

    @staticmethod
    def digest(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    @classmethod
    def tree_digests(cls, root):
        return {
            os.path.relpath(os.path.join(dirpath, filename), root): cls.digest(
                os.path.join(dirpath, filename)
            )
            for dirpath, dirnames, filenames in os.walk(root)
            for filename in sorted(filenames)
        }

    def prepare_taxonomy_projection_fixture(self):
        assigned = self.store.apply_event(
            candidate(
                title="AI伴侣互动测试",
                topic_key="projection-assigned-change",
                category="AI伴侣交互",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )
        self.move_topic_to_legacy_category(assigned, "AI伴侣交互")
        self.assigned_topic_id = assigned["topic_id"]

        unchanged = self.store.apply_event(
            candidate(
                title="模型平台观察",
                topic_key="projection-assigned-unchanged",
                category="模型与平台",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )
        self.unchanged_assigned_topic_id = unchanged["topic_id"]

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE topics
                SET taxonomy_profile = '', taxonomy_version = 0
                WHERE topic_id = ?
                """,
                (self.unchanged_assigned_topic_id,),
            )
            conn.execute(
                """
                UPDATE events
                SET taxonomy_profile = '', taxonomy_version = 0
                WHERE topic_id = ?
                """,
                (self.unchanged_assigned_topic_id,),
            )
            conn.commit()
        finally:
            conn.close()

        relation_source = self.store.apply_event(
            candidate(
                title="Unrelated relation source",
                topic_key="projection-relation-source",
                category="Unrelated",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "其他群"},
            {"relation": "new"},
        )
        self.relation_source_topic_id = relation_source["topic_id"]
        self.unrelated_topic_id = relation_source["topic_id"]

        metadata_relation_source = self.store.apply_event(
            candidate(
                title="Metadata-only relation source",
                topic_key="projection-metadata-relation-source",
                category="Unrelated",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "其他群"},
            {"relation": "new"},
        )
        self.metadata_relation_source_topic_id = metadata_relation_source["topic_id"]

        history = self.store.apply_event(
            candidate(
                title="示例人机互动群 · 2026-06-10 历史总结",
                topic_key="history-summary:projection:2026-06-10",
                category="技术方法",
                links=[],
                event_type="history_summary",
            ),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )
        self.history_topic_id = history["topic_id"]

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO relations(
                    source_topic_id, target_topic_id, relation, reason, created_at
                ) VALUES (?, ?, 'related', 'projection dependency', 999)
                """,
                (self.relation_source_topic_id, self.assigned_topic_id),
            )
            conn.execute(
                """
                INSERT INTO relations(
                    source_topic_id, target_topic_id, relation, reason, created_at
                ) VALUES (?, ?, 'related', 'metadata-only dependency', 999)
                """,
                (
                    self.metadata_relation_source_topic_id,
                    self.unchanged_assigned_topic_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO relations(
                    source_topic_id, target_topic_id, relation, reason, created_at
                ) VALUES (?, ?, 'related', 'forbidden history source', 999)
                """,
                (self.history_topic_id, self.assigned_topic_id),
            )
            conn.commit()
        finally:
            conn.close()

        unmanaged_path = os.path.join(
            self.obsidian_root,
            "关注推送",
            "示例人机互动群",
            "00-按日期.user.md",
        )
        with open(unmanaged_path, "w", encoding="utf-8") as f:
            f.write("# user-owned date index\n")

    def move_topic_to_legacy_category(self, result, category):
        old_rel_path = result["obsidian_path"]
        parts = old_rel_path.split(os.sep)
        new_rel_path = os.path.join(*parts[:-2], category, parts[-1])
        old_path = self.store.full_obsidian_path(old_rel_path)
        new_path = self.store.full_obsidian_path(new_rel_path)
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        if os.path.exists(old_path):
            os.replace(old_path, new_path)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE topics SET category = ?, obsidian_path = ? WHERE topic_id = ?",
                (category, new_rel_path, result["topic_id"]),
            )
            conn.execute(
                "UPDATE events SET category = ? WHERE topic_id = ?",
                (category, result["topic_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        result["obsidian_path"] = new_rel_path
        result["knowledge_path"] = new_path
        return new_rel_path

    def test_vault_chat_alias_candidates_are_sorted_unique_metadata_only(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE topics (source_chat_username TEXT, vault_chat_name TEXT)"
            )
            conn.executemany(
                "INSERT INTO topics VALUES (?, ?)",
                [
                    ("room@chatroom", "Zulu Vault"),
                    ("room@chatroom", "Alpha Vault"),
                    ("room@chatroom", "Zulu Vault"),
                    ("room@chatroom", "  "),
                    ("other@chatroom", "Other Vault"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        store = KnowledgeStore(self.db_path, self.obsidian_root, read_only=True)
        with patch("builtins.open") as markdown_open:
            self.assertEqual(
                store.vault_chat_alias_candidates("room@chatroom"),
                ["Alpha Vault", "Zulu Vault"],
            )
        markdown_open.assert_not_called()

    def test_vault_chat_alias_candidates_surfaces_schema_errors_safely(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE unrelated (value TEXT)")
        conn.commit()
        conn.close()
        store = KnowledgeStore(self.db_path, self.obsidian_root, read_only=True)

        with self.assertRaisesRegex(
            knowledge.KnowledgeMetadataQueryError,
            "knowledge metadata query failed",
        ) as raised:
            store.vault_chat_alias_candidates("room@chatroom")

        self.assertNotIn(self.db_path, str(raised.exception))

    def test_new_topic_creates_sqlite_topic_and_markdown(self):
        result = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})

        topics = self.rows("topics")
        events = self.rows("events")

        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["title"], "Example Model 2.0 发布传闻")
        self.assertEqual(json.loads(topics[0]["entities_json"]), ["Claude", "Opus"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["relation"], "new")
        self.assertTrue(os.path.exists(result["knowledge_path"]))
        with open(result["knowledge_path"], encoding="utf-8") as f:
            md = f.read()
        basename = os.path.basename(result["knowledge_path"])
        self.assertEqual(basename, "[链接] Example Model 2.0 发布传闻.md")
        self.assertFalse(basename.startswith("2026-05-29 03-16 "))
        self.assertIn('title: "Example Model 2.0 发布传闻"', md)
        self.assertIn('created: "2026-05-29 03:16"', md)
        self.assertIn('updated: "2026-05-29 03:17"', md)
        self.assertIn("has_links: true", md)
        self.assertIn("has_files: false", md)
        self.assertIn("category: \"AI模型\"", md)
        self.assertIn("# [链接] Example Model 2.0 发布传闻", md)
        self.assertIn("## 摘要", md)
        self.assertIn("Example Model 2.0 可能今天发布", md)
        self.assertNotIn("## 当前摘要", md)
        self.assertNotIn("## 时间线", md)
        self.assertNotIn("## 来源记录", md)
        self.assertNotIn("```text", md)

    def test_exact_signed_url_stays_canonical_but_markdown_is_redacted(self):
        exact = (
            "https://example.com/object?X-Goog-Credential=knowledge-secret"
            "&X-Goog-Signature=signature-secret&view=1"
            "#access_token=fragment-secret"
        )
        result = self.store.apply_event(
            candidate(
                title=f"资源 {exact}",
                summary=f"请阅读 {exact}",
                key_facts=[f"原始链接是 {exact}"],
                links=[exact],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )

        topics = self.rows("topics")
        self.assertEqual(json.loads(topics[0]["links_json"]), [exact])
        with open(result["knowledge_path"], encoding="utf-8") as handle:
            markdown = handle.read()
        for secret in ("knowledge-secret", "signature-secret", "fragment-secret"):
            self.assertNotIn(secret, markdown)
        self.assertIn("X-Goog-Credential=REDACTED", markdown)
        self.assertIn("view=1", markdown)
        self.assertIn("access_token=REDACTED", markdown)

    def test_atomic_topic_renders_stable_source_contract(self):
        result = self.store.apply_event(
            candidate(links=[]), self.messages, self.config, {"relation": "new"}
        )
        expected_id = f"wg_topic_{result['topic_id']}"
        expected_generated_at = datetime.fromtimestamp(1000).astimezone().isoformat(
            timespec="seconds"
        )

        with open(result["knowledge_path"], encoding="utf-8") as handle:
            first_markdown = handle.read()

        self.assertIn("source_app: we-groupchat-obsidian", first_markdown)
        self.assertIn("source_kind: knowledge_topic", first_markdown)
        self.assertIn("source_schema_version: 1", first_markdown)
        self.assertIn(f"source_id: {expected_id}", first_markdown)
        self.assertIn(f"generated_at: {expected_generated_at}", first_markdown)
        self.assertIsNotNone(datetime.fromisoformat(expected_generated_at).utcoffset())

        conn = self.store.connect()
        try:
            conn.execute(
                """
                UPDATE topics
                SET title = 'Renamed topic', category = 'Renamed category',
                    obsidian_path = '关注推送/Renamed/topic.md'
                WHERE topic_id = ?
                """,
                (result["topic_id"],),
            )
            conn.commit()
            rerendered = self.store.render_topic_markdown(conn, result["topic_id"])
        finally:
            conn.close()

        self.assertIn(f"source_id: {expected_id}", rerendered)
        self.assertIn(f"generated_at: {expected_generated_at}", rerendered)

    def test_history_summary_signals_render_as_projections(self):
        cases = (
            {
                "title": "Key signal",
                "topic_key": "history-summary:room:2026-07-16",
                "event_type": "summary",
            },
            {
                "title": "2026-07-16 历史总结",
                "topic_key": "title-signal",
                "event_type": "summary",
            },
            {
                "title": "Event signal",
                "topic_key": "event-signal",
                "event_type": "history_summary",
            },
        )

        for index, values in enumerate(cases):
            with self.subTest(signal=index):
                result = self.store.apply_event(
                    candidate(links=[], **values),
                    self.messages,
                    self.config,
                    {"relation": "new"},
                )
                with open(result["knowledge_path"], encoding="utf-8") as handle:
                    markdown = handle.read()
                frontmatter = markdown.split("---", 2)[1]
                self.assertIn("source_app: we-groupchat-obsidian", frontmatter)
                self.assertIn("source_kind: projection", frontmatter)
                self.assertIn("source_schema_version: 1", frontmatter)
                self.assertIn("projection_kind: history_summary", frontmatter)
                self.assertNotIn("source_id:", frontmatter)

    def test_topic_id_for_message_hash_returns_exact_event_topic(self):
        result = self.store.apply_event(
            candidate(),
            self.messages,
            self.config,
            {"relation": "new"},
        )

        self.assertEqual(
            self.store.topic_id_for_message_hash(
                build_message_hash(self.messages),
                source_chat="示例技术群",
            ),
            result["topic_id"],
        )
        self.assertEqual(
            self.store.topic_id_for_message_hash(
                build_message_hash(self.messages),
                source_chat_username="new-stable-id@chatroom",
                source_chat="示例技术群",
            ),
            result["topic_id"],
        )
        self.assertIsNone(
            self.store.topic_id_for_message_hash(
                build_message_hash(self.messages),
                source_chat="另一个群",
            )
        )
        self.assertIsNone(
            self.store.topic_id_for_message_hash(
                "missing-hash",
                source_chat="示例技术群",
            )
        )
        self.assertIsNone(self.store.topic_id_for_message_hash(""))

    def test_chat_alias_controls_path_but_not_source_frontmatter(self):
        config = {
            "monitor_chat_username": "room@chatroom",
            "monitor_chat_display_name": "示例技术群改名后",
            "monitor_chat_aliases": {"room@chatroom": "示例稳定群名"},
        }

        result = self.store.apply_event(candidate(links=[]), self.messages, config, {"relation": "new"})

        self.assertIn(
            os.path.join("关注推送", "示例稳定群名", "AI模型"),
            result["obsidian_path"],
        )
        self.assertNotIn("示例技术群改名后", result["obsidian_path"])
        topic = self.rows("topics")[0]
        event = self.rows("events")[0]
        self.assertEqual(topic["source_chat"], "示例技术群改名后")
        self.assertEqual(topic["source_chat_username"], "room@chatroom")
        self.assertEqual(topic["vault_chat_name"], "示例稳定群名")
        self.assertEqual(event["source_chat_username"], "room@chatroom")

        with open(result["knowledge_path"], encoding="utf-8") as f:
            md = f.read()
        self.assertIn('source_chat: "示例技术群改名后"', md)
        self.assertIn('source_chat_username: "room@chatroom"', md)
        self.assertIn('vault_chat_name: "示例稳定群名"', md)

    def test_continuity_boost_uses_chat_username_when_display_name_changes(self):
        config = {
            "monitor_chat_username": "room@chatroom",
            "monitor_chat_display_name": "旧群名",
        }
        self.store.apply_event(
            candidate(
                title="ExampleProject 断点恢复设计",
                topic_key="",
                summary="1. 【03:16】群里讨论 ExampleProject 断点恢复设计。",
                entities=[],
                key_facts=["ExampleProject 断点恢复设计需要稳定记录"],
                links=[],
            ),
            self.messages,
            config,
            {"relation": "new"},
        )
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM topics").fetchone()
            score = self.store._continuity_boost(
                {
                    "source_chat": "新群名",
                    "source_chat_username": "room@chatroom",
                    "title": "ExampleProject 断点恢复设计后续",
                    "summary": "继续讨论 ExampleProject 断点恢复设计。",
                    "key_facts": ["继续讨论断点恢复设计"],
                    "links": [],
                    "window_start": "2026-05-29 03:25",
                },
                row,
            )
        finally:
            conn.close()

        self.assertGreater(score, 0)

    def test_date_indexes_are_link_only_and_newest_first(self):
        self.store.apply_event(
            candidate(title="旧功能讨论", category="工具更新", links=[]),
            [{"timestamp": 1, "time_str": "2026-05-29 03:16", "sender": "示例成员甲", "text": "旧功能"}],
            self.config,
            {"relation": "new"},
        )
        self.store.apply_event(
            candidate(title="新功能讨论", topic_key="new-feature", category="AI实验", links=[]),
            [{"timestamp": 2, "time_str": "2026-05-30 09:10", "sender": "示例成员乙", "text": "新功能"}],
            self.config,
            {"relation": "new"},
        )

        global_index = os.path.join(self.obsidian_root, "关注推送", "00-按日期.md")
        chat_index = os.path.join(self.obsidian_root, "关注推送", "示例技术群", "00-按日期.md")
        self.assertTrue(os.path.exists(global_index))
        self.assertTrue(os.path.exists(chat_index))

        with open(global_index, encoding="utf-8") as f:
            md = f.read()
        self.assertTrue(md.startswith("---\nsource_app: we-groupchat-obsidian\n"))
        self.assertIn(f"---\n{OBSIDIAN_DATE_INDEX_MARKER}\n", md)
        self.assertTrue(self.store._is_managed_date_index(global_index))
        self.assertIn("## 全部", md)
        self.assertLess(md.index("### 2026-05-30"), md.index("### 2026-05-29"))
        self.assertIn("- 09:10 · 示例技术群 / AI实验", md)
        self.assertIn("- 03:16 · 示例技术群 / 工具更新", md)
        self.assertNotIn("- 26:05", md)
        self.assertIn("[[关注推送/示例技术群/AI实验/新功能讨论|新功能讨论]]", md)
        self.assertIn("[[关注推送/示例技术群/工具更新/旧功能讨论|旧功能讨论]]", md)
        self.assertIn("示例技术群 / AI实验", md)
        self.assertNotIn("## 摘要", md)
        self.assertNotIn("## 月份归档", md)
        self.assertNotIn("关注推送/按日期/2026-05", md)

    def test_date_index_renders_projection_contract(self):
        self.store.apply_event(
            candidate(links=[]), self.messages, self.config, {"relation": "new"}
        )
        path = os.path.join(self.obsidian_root, "关注推送", "00-按日期.md")

        with open(path, encoding="utf-8") as handle:
            markdown = handle.read()

        expected_prefix = "\n".join((
            "---",
            "source_app: we-groupchat-obsidian",
            "source_kind: projection",
            "source_schema_version: 1",
            "projection_kind: date_index",
            "---",
            OBSIDIAN_DATE_INDEX_MARKER,
        ))
        self.assertTrue(markdown.startswith(expected_prefix))
        self.assertNotIn("source_id:", markdown.split("---", 2)[1])
        body = markdown.split("---\n", 2)[2]
        self.assertTrue(body.startswith(f"{OBSIDIAN_DATE_INDEX_MARKER}\n# 按日期\n"))
        self.assertTrue(self.store._is_managed_date_index(path))

        malformed = os.path.join(self.tmp.name, "malformed-date-index.md")
        with open(malformed, "w", encoding="utf-8") as handle:
            handle.write("---\nsource_kind: projection\n---\n" + OBSIDIAN_DATE_INDEX_MARKER + "\n")
        self.assertFalse(self.store._is_managed_date_index(malformed))

    def test_date_index_overview_keeps_full_history_without_archive_folder(self):
        self.store.apply_event(
            candidate(title="旧归档主题", topic_key="archive-old", category="工具更新", links=[]),
            [msg_at("2026-05-01 08:00", text="旧归档")],
            self.config,
            {"relation": "new"},
        )
        self.store.apply_event(
            candidate(title="六月早期主题", topic_key="june-early", category="AI实验", links=[]),
            [msg_at("2026-06-05 09:30", text="六月早期")],
            self.config,
            {"relation": "new"},
        )
        self.store.apply_event(
            candidate(title="另一个群的新主题", topic_key="other-chat-new", category="技术方法", links=[]),
            [msg_at("2026-06-18 10:20", text="另一个群")],
            {"monitor_chat_display_name": "Example Interaction Lab"},
            {"relation": "new"},
        )

        global_index = os.path.join(self.obsidian_root, "关注推送", "00-按日期.md")
        chat_index = os.path.join(self.obsidian_root, "关注推送", "示例技术群", "00-按日期.md")

        self.assertTrue(os.path.exists(global_index))
        self.assertTrue(os.path.exists(chat_index))
        self.assertFalse(os.path.exists(os.path.join(self.obsidian_root, "关注推送", "按日期")))
        self.assertFalse(os.path.exists(os.path.join(self.obsidian_root, "关注推送", "示例技术群", "按日期")))

        with open(global_index, encoding="utf-8") as f:
            overview = f.read()
        self.assertIn("另一个群的新主题", overview)
        self.assertIn("六月早期主题", overview)
        self.assertIn("旧归档主题", overview)
        self.assertNotIn("关注推送/按日期/2026-06", overview)
        self.assertNotIn("关注推送/按日期/2026-05", overview)

        with open(chat_index, encoding="utf-8") as f:
            chat_overview = f.read()
        self.assertIn("六月早期主题", chat_overview)
        self.assertIn("旧归档主题", chat_overview)
        self.assertNotIn("另一个群的新主题", chat_overview)

    def test_v1_managed_date_index_is_upgraded_in_place(self):
        monitor_root = os.path.join(self.obsidian_root, "关注推送")
        os.makedirs(monitor_root, exist_ok=True)
        managed_v1 = os.path.join(monitor_root, "00-按日期.md")
        with open(managed_v1, "w", encoding="utf-8") as f:
            f.write("<!-- wechat-summary:managed-date-index v1 -->\n# old generated index\n")

        self.store.apply_event(candidate(links=[]), self.messages, self.config, {"relation": "new"})

        with open(managed_v1, encoding="utf-8") as f:
            md = f.read()
        self.assertTrue(md.startswith("---\nsource_app: we-groupchat-obsidian\n"))
        self.assertIn(f"---\n{OBSIDIAN_DATE_INDEX_MARKER}\n", md)
        self.assertTrue(self.store._is_managed_date_index(managed_v1))
        self.assertIn("## 全部", md)
        self.assertFalse(os.path.exists(os.path.join(monitor_root, "00-按日期.generated.md")))

    def test_unmanaged_date_index_is_not_overwritten(self):
        monitor_root = os.path.join(self.obsidian_root, "关注推送")
        os.makedirs(monitor_root, exist_ok=True)
        unmanaged = os.path.join(monitor_root, "00-按日期.md")
        with open(unmanaged, "w", encoding="utf-8") as f:
            f.write("# custom date view\n")

        self.store.apply_event(candidate(links=[]), self.messages, self.config, {"relation": "new"})

        with open(unmanaged, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# custom date view\n")
        generated = os.path.join(monitor_root, "00-按日期.generated.md")
        self.assertTrue(os.path.exists(generated))
        with open(generated, encoding="utf-8") as f:
            generated_md = f.read()
        self.assertTrue(generated_md.startswith("---\nsource_app: we-groupchat-obsidian\n"))
        self.assertIn(f"---\n{OBSIDIAN_DATE_INDEX_MARKER}\n", generated_md)
        self.assertTrue(self.store._is_managed_date_index(generated))

    def test_managed_generated_date_index_is_removed_when_canonical_is_managed(self):
        monitor_root = os.path.join(self.obsidian_root, "关注推送")
        os.makedirs(monitor_root, exist_ok=True)
        canonical = os.path.join(monitor_root, "00-按日期.md")
        generated = os.path.join(monitor_root, "00-按日期.generated.md")
        with open(canonical, "w", encoding="utf-8") as f:
            f.write(f"{OBSIDIAN_DATE_INDEX_MARKER}\n# old canonical\n")
        with open(generated, "w", encoding="utf-8") as f:
            f.write(f"{OBSIDIAN_DATE_INDEX_MARKER}\n# old generated\n")

        self.store.apply_event(candidate(links=[]), self.messages, self.config, {"relation": "new"})

        self.assertTrue(os.path.exists(canonical))
        self.assertFalse(os.path.exists(generated))

    def test_unreadable_canonical_date_index_does_not_create_generated_fallback(self):
        self.store.apply_event(candidate(links=[]), self.messages, self.config, {"relation": "new"})
        monitor_root = os.path.join(self.obsidian_root, "关注推送")
        canonical = os.path.join(monitor_root, "00-按日期.md")
        generated = os.path.join(monitor_root, "00-按日期.generated.md")
        real_open = open

        def transient_read_failure(path, *args, **kwargs):
            if os.fspath(path) == canonical and "r" in (args[0] if args else kwargs.get("mode", "r")):
                raise OSError("transient canonical read failure")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=transient_read_failure):
            with self.assertRaisesRegex(OSError, "transient canonical read failure"):
                self.store.write_date_indexes()

        self.assertFalse(os.path.exists(generated))

    def test_apply_event_reports_date_index_projection_failure_after_core_commit(self):
        failure = OSError(errno.EDEADLK, "Resource deadlock avoided")

        with patch.object(self.store, "write_date_indexes", side_effect=failure):
            result = self.store.apply_event(
                candidate(links=[]),
                self.messages,
                self.config,
                {"relation": "new"},
            )

        self.assertEqual(
            result["projection_warnings"],
            [{
                "surface": "date_indexes",
                "error_type": "OSError",
                "errno": errno.EDEADLK,
            }],
        )
        self.assertTrue(os.path.exists(result["knowledge_path"]))
        self.assertEqual(len(self.rows("topics")), 1)
        self.assertEqual(len(self.rows("events")), 1)

    def test_event_and_daily_digest_invalidation_commit_together(self):
        result = self.store.apply_event(
            candidate(links=[]),
            self.messages,
            self.config,
            {"relation": "new"},
        )

        changes = self.rows("daily_digest_changes")
        self.assertEqual(result["event_id"], 1)
        self.assertEqual(len(self.rows("events")), 1)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["change_kind"], "canonical_event")
        self.assertEqual(changes[0]["window_start"], "2026-05-29 03:16")
        self.assertEqual(changes[0]["window_end"], "2026-05-29 03:17")

    def test_daily_digest_invalidation_rolls_back_with_event(self):
        with patch.object(
            self.store,
            "_register_attachment_mentions",
            side_effect=sqlite3.OperationalError("fixture transaction failure"),
        ):
            with self.assertRaisesRegex(
                sqlite3.OperationalError,
                "fixture transaction failure",
            ):
                self.store.apply_event(
                    candidate(links=[]),
                    self.messages,
                    self.config,
                    {"relation": "new"},
                )

        self.assertEqual(self.rows("events"), [])
        self.assertEqual(self.rows("daily_digest_changes"), [])

    def test_apply_event_reports_topic_markdown_failure_without_writing_indexes(self):
        failure = OSError(errno.EIO, "I/O error")

        with (
            patch.object(self.store, "_write_topic_markdown", side_effect=failure),
            patch.object(self.store, "write_date_indexes") as write_date_indexes,
        ):
            result = self.store.apply_event(
                candidate(links=[]),
                self.messages,
                self.config,
                {"relation": "new"},
            )

        self.assertEqual(
            result["projection_warnings"],
            [{
                "surface": "topic_markdown",
                "error_type": "OSError",
                "errno": errno.EIO,
            }],
        )
        self.assertEqual(result["knowledge_path"], "")
        write_date_indexes.assert_not_called()
        self.assertEqual(len(self.rows("topics")), 1)
        self.assertEqual(len(self.rows("events")), 1)

    def test_reused_source_batch_repairs_missing_topic_projection(self):
        failure = OSError(errno.EIO, "I/O error")

        with patch.object(self.store, "_write_topic_markdown", side_effect=failure):
            first = self.store.apply_event(
                candidate(links=[]),
                self.messages,
                self.config,
                {"relation": "new"},
                source_batch_id="source-batch-retry",
            )

        self.assertEqual(first["knowledge_path"], "")
        self.assertEqual(first["projection_warnings"][0]["surface"], "topic_markdown")

        reused = self.store.apply_event(
            candidate(links=[]),
            self.messages,
            self.config,
            {"relation": "new"},
            source_batch_id="source-batch-retry",
        )

        self.assertTrue(reused["reused"])
        self.assertTrue(os.path.isfile(reused["knowledge_path"]))
        self.assertEqual(reused["projection_warnings"], [])
        self.assertEqual(len(self.rows("topics")), 1)
        self.assertEqual(len(self.rows("events")), 1)

    def test_managed_legacy_date_archive_dir_is_removed(self):
        monitor_root = os.path.join(self.obsidian_root, "关注推送")
        archive_dir = os.path.join(monitor_root, "按日期")
        os.makedirs(archive_dir, exist_ok=True)
        managed_archive = os.path.join(archive_dir, "2026-05.md")
        managed_generated_archive = os.path.join(archive_dir, "2026-05.generated.md")
        with open(managed_archive, "w", encoding="utf-8") as f:
            f.write(f"{OBSIDIAN_DATE_INDEX_MARKER}\n# old archive\n")
        with open(managed_generated_archive, "w", encoding="utf-8") as f:
            f.write(f"{OBSIDIAN_DATE_INDEX_MARKER}\n# old generated archive\n")

        result = self.store.apply_event(candidate(links=[]), self.messages, self.config, {"relation": "new"})

        self.assertTrue(os.path.exists(result["knowledge_path"]))
        self.assertFalse(os.path.exists(archive_dir))

    def test_unmanaged_legacy_date_archive_dir_is_preserved(self):
        monitor_root = os.path.join(self.obsidian_root, "关注推送")
        archive_dir = os.path.join(monitor_root, "按日期")
        os.makedirs(archive_dir, exist_ok=True)
        unmanaged_archive = os.path.join(archive_dir, "2026-05.md")
        with open(unmanaged_archive, "w", encoding="utf-8") as f:
            f.write("# custom archive\n")

        self.store.apply_event(candidate(links=[]), self.messages, self.config, {"relation": "new"})

        self.assertTrue(os.path.exists(archive_dir))
        with open(unmanaged_archive, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# custom archive\n")

    def test_date_index_plan_reports_unmanaged_conflicts_without_writing(self):
        monitor_root = os.path.join(self.obsidian_root, "关注推送")
        os.makedirs(monitor_root, exist_ok=True)
        unmanaged = os.path.join(monitor_root, "00-按日期.md")
        with open(unmanaged, "w", encoding="utf-8") as f:
            f.write("# custom date view\n")

        self.store.apply_event(candidate(links=[]), self.messages, self.config, {"relation": "new"})
        os.remove(os.path.join(monitor_root, "00-按日期.generated.md"))

        plan = self.store.plan_date_indexes()

        self.assertEqual(plan["conflict_count"], 1)
        self.assertEqual(plan["targets"][0]["status"], "fallback")
        self.assertTrue(plan["targets"][0]["path"].endswith("00-按日期.generated.md"))
        self.assertFalse(os.path.exists(os.path.join(monitor_root, "00-按日期.generated.md")))

    def test_wechat_forwarded_record_shell_url_is_not_a_resource_link(self):
        shell_url = (
            "https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate"
            "?t=page/favorite_record__w_unsupport"
        )

        normalized = normalize_candidate(
            candidate(
                title="示例教程合并记录",
                summary=f"示例成员转发了收藏里的示例合并记录：{shell_url}",
                links=[shell_url],
                resource_status="linked",
            )
        )

        self.assertEqual(normalized["links"], [])
        self.assertEqual(normalized["resource_status"], "mentioned_private")
        self.assertTrue(normalized["resource_lead"])

    def test_normalize_candidate_preserves_semantic_tags(self):
        normalized = normalize_candidate(
            candidate(
                semantic_tags=[
                    "共读",
                    "memory play",
                    "共读",
                    "",
                ],
            )
        )

        self.assertEqual(normalized["semantic_tags"], ["共读", "memory play"])

    def test_human_ai_target_chat_uses_fixed_taxonomy_and_semantic_tags(self):
        result = self.store.apply_event(
            candidate(
                title="AI伴侣互动测试",
                topic_key="human-ai-play-test",
                category="AI伴侣交互",
                links=[],
                semantic_tags=["共读", "记忆", "共读"],
            ),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )

        topics = self.rows("topics")
        events = self.rows("events")

        self.assertEqual(topics[0]["category"], "互动实验与玩法")
        self.assertEqual(events[0]["category"], "互动实验与玩法")
        self.assertEqual(json.loads(topics[0]["semantic_tags_json"]), ["共读", "记忆"])
        self.assertEqual(json.loads(events[0]["semantic_tags_json"]), ["共读", "记忆"])
        self.assertEqual(topics[0]["taxonomy_profile"], HUMAN_AI_INTIMACY_PROFILE)
        self.assertEqual(topics[0]["taxonomy_version"], 2)
        self.assertIn(
            os.path.join("关注推送", "示例人机互动群", "互动实验与玩法"),
            result["obsidian_path"],
        )

        with open(result["knowledge_path"], encoding="utf-8") as f:
            md = f.read()
        self.assertIn('category: "互动实验与玩法"', md)
        self.assertIn(f'taxonomy_profile: "{HUMAN_AI_INTIMACY_PROFILE}"', md)
        self.assertIn("taxonomy_version: 2", md)
        self.assertIn("semantic_tags:", md)
        self.assertIn('  - "共读"', md)
        self.assertIn('  - "记忆"', md)

    def test_explicitly_assigned_renamed_chat_writes_stable_path(self):
        config = {
            "monitor_chat_username": "room@chatroom",
            "monitor_chat_display_name": "群已经改名",
            "monitor_chat_aliases": {"room@chatroom": "示例人机互动群"},
            "monitor_chat_taxonomy_profiles": {
                "room@chatroom": "human_ai_intimacy_v1",
            },
        }

        result = self.store.apply_event(
            candidate(category="AI伴侣交互"),
            self.messages,
            config,
            {"relation": "new"},
        )

        topic = self.store.get_topic(result["topic_id"])
        self.assertEqual(topic["taxonomy_profile"], "human_ai_intimacy_v1")
        self.assertEqual(topic["taxonomy_version"], 2)
        self.assertIn("示例人机互动群/互动实验与玩法", topic["obsidian_path"])

    def test_human_ai_target_chat_preserves_raw_canonical_category_before_generic_aliases(self):
        normalized = normalize_candidate(
            candidate(
                title="私发资源线索",
                topic_key="private-resource-lead",
                category="资源线索",
                links=[],
            )
        )

        result = self.store.apply_event(
            normalized,
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )

        topic = self.rows("topics")[0]

        self.assertEqual(topic["category"], "资源线索")
        self.assertIn(
            os.path.join("关注推送", "示例人机互动群", "资源线索"),
            result["obsidian_path"],
        )

    def test_reorganize_paths_preserves_fixed_taxonomy_categories(self):
        created = self.store.apply_event(
            candidate(
                title="固定分类互动玩法",
                topic_key="fixed-taxonomy-play",
                category="AI伴侣交互",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )

        before = self.store.get_topic(created["topic_id"])
        dry_run_changes = self.store.find_category_changes()
        result = self.store.run_maintenance()
        after = self.store.get_topic(created["topic_id"])

        self.assertEqual(dry_run_changes, [])
        self.assertEqual(result["category_change_count"], 0)
        self.assertEqual(after["category"], "互动实验与玩法")
        self.assertEqual(after["obsidian_path"], before["obsidian_path"])
        self.assertIn(
            os.path.join("关注推送", "示例人机互动群", "互动实验与玩法"),
            after["obsidian_path"],
        )

    def test_unknown_human_ai_target_category_goes_to_reviewable_folder(self):
        result = self.store.apply_event(
            candidate(
                title="边缘话题观察",
                topic_key="unclear-human-ai-topic",
                category="非常细的新标签",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "Example Interaction Lab"},
            {"relation": "new"},
        )

        topic = self.rows("topics")[0]

        self.assertEqual(topic["category"], "待归类")
        self.assertEqual(topic["taxonomy_profile"], HUMAN_AI_INTIMACY_PROFILE)
        self.assertIn(
            os.path.join("关注推送", "Example Interaction Lab", "待归类"),
            result["obsidian_path"],
        )

    def test_human_ai_taxonomy_does_not_apply_to_other_chats(self):
        result = self.store.apply_event(
            candidate(
                title="其他群里的 AI伴侣互动测试",
                topic_key="other-ai-companion-play",
                category="AI伴侣交互",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "其他群"},
            {"relation": "new"},
        )

        topic = self.rows("topics")[0]

        self.assertEqual(topic["category"], "AI伴侣交互")
        self.assertEqual(topic["taxonomy_profile"], "")
        self.assertIn(
            os.path.join("关注推送", "其他群", "AI伴侣交互"),
            result["obsidian_path"],
        )

    def test_ensure_obsidian_vault_seeds_default_files_without_overwrite(self):
        seeded = ensure_obsidian_vault(self.obsidian_root, include_app_config=True)

        self.assertIn(".obsidian/app.json", seeded["created"])
        self.assertTrue(os.path.exists(os.path.join(self.obsidian_root, ".obsidian", "app.json")))
        self.assertTrue(os.path.exists(os.path.join(self.obsidian_root, "首页.md")))
        self.assertTrue(os.path.exists(
            os.path.join(self.obsidian_root, "关注推送", "AI模型", OBSIDIAN_CATEGORY_INDEX_FILENAME)
        ))

        home = os.path.join(self.obsidian_root, "首页.md")
        with open(home, "w", encoding="utf-8") as f:
            f.write("# custom\n")

        seeded_again = ensure_obsidian_vault(self.obsidian_root, include_app_config=True)

        self.assertEqual(seeded_again["created"], [])
        with open(home, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# custom\n")

    def test_ensure_obsidian_vault_removes_generated_legacy_category_index_only(self):
        monitor_root = os.path.join(self.obsidian_root, "关注推送")
        os.makedirs(monitor_root, exist_ok=True)
        generated_legacy = os.path.join(monitor_root, "工具更新.md")
        custom_legacy = os.path.join(monitor_root, "AI模型.md")
        with open(generated_legacy, "w", encoding="utf-8") as f:
            f.write('# 工具更新\n\n```query\npath:"关注推送/工具更新"\n```\n')
        with open(custom_legacy, "w", encoding="utf-8") as f:
            f.write("# AI模型\n\n我自己写的内容\n")

        ensure_obsidian_vault(self.obsidian_root, include_app_config=True)

        self.assertFalse(os.path.exists(generated_legacy))
        self.assertTrue(os.path.exists(custom_legacy))
        self.assertTrue(os.path.exists(
            os.path.join(self.obsidian_root, "关注推送", "工具更新", OBSIDIAN_CATEGORY_INDEX_FILENAME)
        ))

    def test_ensure_obsidian_vault_migrates_generated_home_links(self):
        os.makedirs(self.obsidian_root, exist_ok=True)
        home = os.path.join(self.obsidian_root, "首页.md")
        with open(home, "w", encoding="utf-8") as f:
            f.write("# 微信关注推送知识库\n\n- [[关注推送/AI模型]]\n- [[关注推送/工具更新]]\n")

        ensure_obsidian_vault(self.obsidian_root, include_app_config=True)

        with open(home, encoding="utf-8") as f:
            md = f.read()
        self.assertIn("[[关注推送/AI模型/目录|AI模型]]", md)
        self.assertIn("[[关注推送/工具更新/目录|工具更新]]", md)
        self.assertNotIn("[[关注推送/AI模型]]", md)

    def test_custom_obsidian_vault_only_creates_monitor_subdir_by_default(self):
        custom_root = os.path.join(self.tmp.name, "custom-vault")

        ensure_obsidian_vault(custom_root)

        self.assertTrue(os.path.isdir(os.path.join(custom_root, "关注推送")))
        self.assertFalse(os.path.exists(os.path.join(custom_root, ".obsidian", "app.json")))
        self.assertFalse(os.path.exists(os.path.join(custom_root, "首页.md")))

    def test_relation_line_helper_matches_existing_markdown_contract(self):
        self.assertEqual(
            _render_relation_markdown_line(
                "updates",
                "关注推送/群聊/模型与平台/目标.md",
                "目标标题",
            ),
            "- updates:: [[关注推送/群聊/模型与平台/目标|目标标题]]",
        )

    def test_duplicate_event_only_records_event(self):
        first = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        topic_id = first["topic_id"]

        self.store.apply_event(
            candidate(summary="1. 【03:18】大家又重复讨论 2.0 传闻。"),
            self.messages,
            self.config,
            {"relation": "duplicate", "target_topic_id": topic_id},
        )

        topics = self.rows("topics")
        events = self.rows("events")

        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["event_count"], 1)
        self.assertEqual([e["relation"] for e in events], ["new", "duplicate"])

    def test_update_creates_separate_note_and_links_to_original_topic(self):
        first = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        topic_id = first["topic_id"]

        updated = self.store.apply_event(
            candidate(
                summary="1. 【03:21】示例成员贴出性能曝光链接，传闻有了新来源。",
                key_facts=["示例成员贴出 Example Model 2.0 性能曝光链接"],
                links=["https://example.com/example-model", "https://example.com/benchmark"],
            ),
            self.messages,
            self.config,
            {"relation": "update", "target_topic_id": topic_id, "reason": "新增链接"},
        )

        topics = self.rows("topics")
        relations = self.rows("relations")
        self.assertEqual(len(topics), 2)
        self.assertEqual(topics[0]["event_count"], 1)
        self.assertEqual(topics[1]["event_count"], 1)
        self.assertNotEqual(first["knowledge_path"], updated["knowledge_path"])
        self.assertIn("性能曝光链接", "\n".join(json.loads(topics[1]["key_facts_json"])))
        self.assertEqual(relations[0]["relation"], "updates")
        self.assertEqual(relations[0]["source_topic_id"], updated["topic_id"])
        self.assertEqual(relations[0]["target_topic_id"], topic_id)
        with open(updated["knowledge_path"], encoding="utf-8") as f:
            md = f.read()
        self.assertIn("新线索", md)
        self.assertIn("updates:: [[关注推送/示例技术群/AI模型/[链接] Example Model 2.0 发布传闻|Example Model 2.0 发布传闻]]", md)
        self.assertIn("https://example.com/benchmark", md)

    def test_contradiction_creates_separate_disputed_note(self):
        first = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        topic_id = first["topic_id"]

        contradicted = self.store.apply_event(
            candidate(
                title="Example Model 2.0 发布图被辟谣",
                summary="1. 【03:30】示例成员丙指出流传图片是网友假想，不是官方图。",
                key_facts=["流传图片被指出是网友假想"],
                status_hint="disputed",
            ),
            self.messages,
            self.config,
            {"relation": "contradiction", "target_topic_id": topic_id, "reason": "图片被辟谣"},
        )

        topics = self.rows("topics")
        relations = self.rows("relations")
        self.assertEqual(len(topics), 2)
        self.assertEqual(topics[0]["status"], "rumor")
        self.assertEqual(topics[1]["status"], "disputed")
        self.assertEqual(relations[0]["relation"], "contradicts")
        self.assertEqual(relations[0]["source_topic_id"], contradicted["topic_id"])
        self.assertEqual(relations[0]["target_topic_id"], topic_id)
        self.assertIn("流传图片被指出是网友假想", json.loads(topics[1]["key_facts_json"]))

    def test_new_topic_links_related_existing_topics(self):
        first = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        topic_a = first["topic_id"]

        second = self.store.apply_event(
            candidate(
                title="Example Model 2.0 上下文长度讨论",
                topic_key="example-model-2.0-context-length",
                summary="1. 【03:40】群里讨论 Example Model 2.0 的上下文长度。",
            ),
            self.messages,
            self.config,
            {"relation": "new", "related_topic_ids": [topic_a]},
        )

        related = [r for r in self.rows("relations") if r["relation"] == "related"]
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["source_topic_id"], second["topic_id"])
        self.assertEqual(related[0]["target_topic_id"], topic_a)

        with open(second["knowledge_path"], encoding="utf-8") as f:
            md = f.read()
        self.assertIn("## 相关主题", md)
        self.assertIn(
            "[[关注推送/示例技术群/AI模型/[链接] Example Model 2.0 发布传闻|Example Model 2.0 发布传闻]]",
            md,
        )
        self.assertIn("event_count:", md)

    def test_link_related_skips_missing_and_self_ids(self):
        first = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        topic_a = first["topic_id"]

        second = self.store.apply_event(
            candidate(title="无关主题", topic_key="unrelated"),
            self.messages,
            self.config,
            {"relation": "new", "related_topic_ids": [999999, topic_a]},
        )

        related = [
            r for r in self.rows("relations")
            if r["relation"] == "related" and r["source_topic_id"] == second["topic_id"]
        ]
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["target_topic_id"], topic_a)

    def test_knowledge_audit_summarizes_relation_and_duplicate_surfaces(self):
        first = self.store.apply_event(
            candidate(title="Example Model 2.0 发布传闻", topic_key="example-model-a"),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        self.store.apply_event(
            candidate(
                title="Example Model 2.0 今天发布?",
                topic_key="example-model-b",
                summary="1. 【03:25】又有人说 Example Model 2.0 今天发。",
                key_facts=["有人称今天发布"],
            ),
            self.messages,
            self.config,
            {"relation": "new", "related_topic_ids": [first["topic_id"]]},
        )

        audit = self.store.knowledge_audit(example_limit=2)

        self.assertEqual(audit["total_topics"], 2)
        self.assertEqual(audit["relation_counts"]["related"], 1)
        self.assertEqual(audit["relation_edge_count"], 1)
        self.assertEqual(audit["relation_examples"][0]["relation"], "related")
        self.assertEqual(audit["relation_examples"][0]["target_title"], "Example Model 2.0 发布传闻")
        self.assertEqual(audit["duplicate_group_count"], 1)
        self.assertEqual(audit["taxonomy"]["profile"], HUMAN_AI_INTIMACY_PROFILE)
        self.assertEqual(audit["taxonomy"]["scoped_topic_count"], 0)
        self.assertEqual(audit["taxonomy"]["assignment_source_counts"], {})

        unknown = self.store.knowledge_audit(taxonomy_profile="missing_profile", example_limit=2)
        self.assertEqual(unknown["taxonomy"]["taxonomy_version"], 0)
        self.assertEqual(unknown["taxonomy"]["assignment_source_counts"], {})
        self.assertEqual(audit["category_change_count"], 0)

    def test_find_candidates_prefers_recent_same_chat_continuation(self):
        self.store.apply_event(
            candidate(
                title="Example Model 2.0 大版本体验讨论",
                topic_key="example-model-2.0-broad",
                summary="1. 【03:05】群里泛聊 Example Model 2.0 的模型体验。",
                entities=["Example Model", "2.0", "示例成员甲"],
                key_facts=["Example Model 2.0 的模型体验被多次讨论"],
                links=[],
            ),
            [msg(5, "示例成员甲", "示例模型测试结果不稳定")],
            self.config,
            {"relation": "new"},
        )
        recent = self.store.apply_event(
            candidate(
                title="示例模型缓存与工具调用讨论",
                topic_key="claude-cache-tool-impact",
                summary="1. 【03:19】示例成员讨论工具调用对模型缓存的影响，并建议放在断点后。",
                entities=["Claude", "示例成员乙", "示例成员丙"],
                key_facts=[
                    "示例工具调用可能降低模型缓存命中率",
                    "将工具说明放在断点后可避免缓存破坏",
                ],
                links=[],
            ),
            [
                msg(19, "示例成员乙", "示例工具说明较长"),
                msg(20, "示例成员丙", "可以放在示例断点后吗"),
            ],
            self.config,
            {"relation": "new"},
        )

        candidates = self.store.find_candidates(
            candidate(
                title="示例成员讨论模型断点与缓存优化",
                topic_key="explicit-breakpoint-cache",
                summary="1. 【03:22】示例成员丙补充变化 block 放在断点后，role 需要是 user。",
                entities=["Example Model", "2.0", "示例成员乙", "示例成员丙"],
                key_facts=[
                    "显式断点可以放在变化block前，保持前缀稳定",
                    "变化block放在断点后且role需为user",
                ],
                links=[],
                source_chat="示例技术群",
                window_start="2026-05-29 03:22",
            ),
            limit=3,
        )

        self.assertEqual(candidates[0]["topic_id"], recent["topic_id"])

    def test_find_candidates_excludes_history_summaries_for_live_monitor_hits(self):
        self.store.apply_event(
            candidate(
                title="Example Interaction Lab · 2026-06-10 历史总结",
                topic_key="history-summary:chatroom:2026-06-10",
                summary="1. 群里讨论了 Example Assistant 使用体验、记忆管理和 Obsidian。",
                category="技术方法",
                entities=["Example Interaction Lab", "历史总结", "Example Assistant", "Obsidian"],
                key_facts=["示例日期共 42 条消息"],
                links=[],
                event_type="history_summary",
                status_hint="resolved",
            ),
            [msg(10, "example.user", "今天讨论了 Example Assistant 和 Obsidian")],
            {"monitor_chat_display_name": "Example Interaction Lab"},
            {"relation": "new"},
        )

        candidates = self.store.find_candidates(
            candidate(
                title="Example Assistant使用经验与行为观察",
                topic_key="example-assistant-usage-behavior-20260611",
                summary="1. 【10:06】ExampleAuthor 讨论 Example Assistant 的公开测试配置。",
                category="技术方法",
                entities=["Example Assistant", "ExampleAuthor"],
                key_facts=["示例作者比较两种公开测试配置的行为差异"],
                links=[],
                event_type="discussion",
                status_hint="tracking",
            )
        )

        self.assertEqual(candidates, [])

    def test_run_maintenance_merges_duplicate_topics(self):
        self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        self.store.apply_event(
            candidate(
                title="Example Model 2.0 今天发布?",
                summary="1. 【03:25】又有人说 2.0 今天发。",
                key_facts=["有人称今天发布"],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        self.assertEqual(len(self.rows("topics")), 2)

        result = self.store.run_maintenance()

        self.assertEqual(result["group_count"], 1)
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(result["reexport_count"], 1)

        topics = self.rows("topics")
        events = self.rows("events")
        self.assertEqual(len(topics), 1)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["topic_id"] == topics[0]["topic_id"] for e in events))
        facts = json.loads(topics[0]["key_facts_json"])
        self.assertIn("有人称今天发布", facts)

    def test_run_maintenance_dry_run_reports_without_changing(self):
        self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        self.store.apply_event(
            candidate(title="Example Model 2.0 今天发布?", summary="重复"),
            self.messages,
            self.config,
            {"relation": "new"},
        )

        result = self.store.run_maintenance(dry_run=True)

        self.assertEqual(result["group_count"], 1)
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(len(self.rows("topics")), 2)

    def test_duplicate_groups_exclude_history_summaries(self):
        history = candidate(
            title="同一个群 · 2026-06-10 历史总结",
            topic_key="history-summary:chatroom:2026-06-10",
            event_type="history_summary",
            status_hint="resolved",
            links=[],
        )
        self.store.apply_event(history, self.messages, self.config, {"relation": "new"})
        self.store.apply_event(
            {**history, "topic_key": "history-summary:chatroom:2026-06-11"},
            self.messages,
            self.config,
            {"relation": "new"},
        )

        self.assertEqual(self.store.find_duplicate_groups(), [])

    def test_duplicate_groups_do_not_cross_chats(self):
        same = candidate(title="完全相同的主题", topic_key="same-topic", links=[])
        self.store.apply_event(
            same,
            self.messages,
            {"monitor_chat_display_name": "群 A", "monitor_chat_username": "a@chatroom"},
            {"relation": "new"},
        )
        self.store.apply_event(
            same,
            self.messages,
            {"monitor_chat_display_name": "群 B", "monitor_chat_username": "b@chatroom"},
            {"relation": "new"},
        )

        self.assertEqual(self.store.find_duplicate_groups(), [])

    def test_duplicate_groups_do_not_chain_transitively(self):
        for suffix in ("A", "B", "C"):
            self.store.apply_event(
                candidate(
                    title="相同 blocking 标题",
                    topic_key=f"chain-{suffix.lower()}",
                    links=[],
                ),
                self.messages,
                self.config,
                {"relation": "new"},
            )
        topic_ids = [row["topic_id"] for row in self.rows("topics")]
        direct_pairs = {
            frozenset((topic_ids[0], topic_ids[1])),
            frozenset((topic_ids[1], topic_ids[2])),
        }

        def similarity(a, b):
            return 90 if frozenset((a["topic_id"], b["topic_id"])) in direct_pairs else 0

        with patch.object(self.store, "_topic_similarity", side_effect=similarity):
            groups = self.store.find_duplicate_groups()

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            {topic["topic_id"] for topic in groups[0]},
            {topic_ids[0], topic_ids[1]},
        )

    def test_duplicate_groups_do_not_compare_pairs_with_only_one_shared_title_token(self):
        self.store.apply_event(
            candidate(title="Example Project launch", topic_key="example-project", links=[]),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        self.store.apply_event(
            candidate(title="Beacon launch", topic_key="beacon", links=[]),
            self.messages,
            self.config,
            {"relation": "new"},
        )

        with patch.object(
            self.store,
            "_topic_similarity",
            side_effect=AssertionError("ineligible pair reached expensive similarity scorer"),
        ):
            groups = self.store.find_duplicate_groups()

        self.assertEqual(groups, [])

    def test_merge_group_preserves_semantic_tags_and_rewires_relations(self):
        primary = self.store.apply_event(
            candidate(
                title="重复主题",
                topic_key="duplicate-primary",
                semantic_tags=["primary-tag"],
                links=[],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        secondary = self.store.apply_event(
            candidate(
                title="重复主题",
                topic_key="duplicate-secondary",
                semantic_tags=["secondary-tag"],
                links=[],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        external = self.store.apply_event(
            candidate(title="外部主题", topic_key="external", links=[]),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        conn = self.store.connect()
        try:
            conn.execute(
                "INSERT INTO relations(source_topic_id, target_topic_id, relation, reason, created_at) VALUES (?, ?, 'related', 'later existing', 10)",
                (external["topic_id"], primary["topic_id"]),
            )
            conn.execute(
                "INSERT INTO relations(source_topic_id, target_topic_id, relation, reason, created_at) VALUES (?, ?, 'related', 'incoming', 1)",
                (external["topic_id"], secondary["topic_id"]),
            )
            conn.execute(
                "INSERT INTO relations(source_topic_id, target_topic_id, relation, reason, created_at) VALUES (?, ?, 'updates', 'outgoing', 2)",
                (secondary["topic_id"], external["topic_id"]),
            )
            topics = [self.store._topic_dict(row) for row in conn.execute(
                "SELECT * FROM topics WHERE topic_id IN (?, ?) ORDER BY topic_id",
                (primary["topic_id"], secondary["topic_id"]),
            )]
            self.store._merge_group(conn, topics)
            conn.commit()
        finally:
            conn.close()

        rows = self.rows("relations")
        endpoints = {
            (row["source_topic_id"], row["target_topic_id"], row["relation"])
            for row in rows
        }
        self.assertIn((external["topic_id"], primary["topic_id"], "related"), endpoints)
        self.assertIn((primary["topic_id"], external["topic_id"], "updates"), endpoints)
        merged_topic = self.rows("topics")[0]
        self.assertEqual(
            set(json.loads(merged_topic["semantic_tags_json"])),
            {"primary-tag", "secondary-tag"},
        )
        rewired = next(
            row
            for row in rows
            if row["source_topic_id"] == external["topic_id"]
            and row["target_topic_id"] == primary["topic_id"]
            and row["relation"] == "related"
        )
        self.assertEqual(rewired["created_at"], 1)
        self.assertEqual(rewired["reason"], "incoming")

    def test_topic_markdown_write_is_atomic(self):
        result = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        conn = self.store.connect()
        try:
            with patch.object(self.store, "_atomic_write_text") as atomic_write:
                self.store._write_topic_markdown(conn, result["topic_id"])
        finally:
            conn.close()

        atomic_write.assert_called_once()
        self.assertEqual(atomic_write.call_args.args[0], result["knowledge_path"])
        self.assertIn("Example Model 2.0 发布传闻", atomic_write.call_args.args[1])

    def test_reorganize_keeps_old_paths_when_reexport_fails(self):
        result = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        self.move_topic_to_legacy_category(result, "AI产品技巧")
        old_path = result["knowledge_path"]

        with patch.object(self.store, "reexport_all", side_effect=RuntimeError("reexport failed")):
            with self.assertRaisesRegex(RuntimeError, "reexport failed"):
                self.store.reorganize_paths()

        self.assertTrue(os.path.exists(old_path))

    def test_run_maintenance_merges_category_folders(self):
        technique = self.store.apply_event(
            candidate(
                title="Example Model 2.0 思考链提取技巧",
                topic_key="example-model-thinking-tips",
                category="AI产品技巧",
                summary="1. 【03:16】群里分享 Example Model 2.0 思考链提取技巧。",
                links=[],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        tool = self.store.apply_event(
            candidate(
                title="自建 app 新功能讨论",
                topic_key="self-app-feature",
                category="自建app新功能",
                summary="1. 【03:20】群里讨论自建 app 的新功能。",
                links=[],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )

        def move_to_legacy_folder(result, legacy_category):
            current_path = result["knowledge_path"]
            legacy_rel = os.path.join(
                "关注推送", legacy_category, os.path.basename(current_path),
            )
            legacy_path = os.path.join(self.obsidian_root, legacy_rel)
            os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
            os.rename(current_path, legacy_path)
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "UPDATE topics SET category = ?, obsidian_path = ? WHERE topic_id = ?",
                    (legacy_category, legacy_rel, result["topic_id"]),
                )
                conn.commit()
            finally:
                conn.close()
            return legacy_path

        old_technique_path = move_to_legacy_folder(technique, "AI产品技巧")
        old_tool_path = move_to_legacy_folder(tool, "自建app新功能")
        plan = self.store.run_maintenance(dry_run=True)
        self.assertEqual(plan["category_change_count"], 2)
        self.assertTrue(os.path.exists(old_technique_path))
        self.assertTrue(os.path.exists(old_tool_path))

        result = self.store.run_maintenance()

        self.assertEqual(result["category_change_count"], 2)
        self.assertFalse(os.path.exists(old_technique_path))
        self.assertFalse(os.path.exists(old_tool_path))
        topics = {t["topic_key"]: t for t in self.store.list_topics()}
        self.assertEqual(topics["example-model-thinking-tips"]["category"], "技术方法")
        self.assertEqual(topics["self-app-feature"]["category"], "自建app")
        self.assertIn(os.path.join("关注推送", "示例技术群", "技术方法"), topics["example-model-thinking-tips"]["obsidian_path"])
        self.assertIn(os.path.join("关注推送", "示例技术群", "自建app"), topics["self-app-feature"]["obsidian_path"])
        self.assertIn("Example Model 2.0 思考链提取技巧", topics["example-model-thinking-tips"]["obsidian_path"])
        self.assertIn("自建 app 新功能讨论", topics["self-app-feature"]["obsidian_path"])
        self.assertEqual(result["removed_empty_dirs"], 2)

    def test_taxonomy_migration_dry_run_maps_target_chats_without_writing(self):
        target = self.store.apply_event(
            candidate(
                title="AI伴侣互动测试",
                topic_key="ai-companion-play",
                category="AI伴侣交互",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )
        self.move_topic_to_legacy_category(target, "AI伴侣交互")
        other = self.store.apply_event(
            candidate(
                title="其他群里的 AI伴侣互动测试",
                topic_key="other-ai-companion-play",
                category="AI伴侣交互",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "其他群"},
            {"relation": "new"},
        )

        plan = self.store.plan_taxonomy_migration()

        self.assertEqual(plan["profile"], "human_ai_intimacy_v1")
        self.assertEqual(plan["scoped_topic_count"], 1)
        self.assertEqual(plan["category_change_count"], 1)
        self.assertEqual(plan["path_change_count"], 1)
        self.assertEqual(
            plan["category_mappings"],
            [
                {
                    "from": "AI伴侣交互",
                    "to": "互动实验与玩法",
                    "count": 1,
                    "example_paths": [target["obsidian_path"]],
                }
            ],
        )
        self.assertIn("互动实验与玩法", plan["changes"][0]["to_path"])
        self.assertTrue(os.path.exists(target["knowledge_path"]))
        self.assertFalse(os.path.exists(os.path.join(
            self.obsidian_root,
            plan["changes"][0]["to_path"],
        )))
        topics = {t["topic_key"]: t for t in self.store.list_topics()}
        self.assertEqual(topics["ai-companion-play"]["category"], "AI伴侣交互")
        self.assertEqual(topics["ai-companion-play"]["obsidian_path"], target["obsidian_path"])
        self.assertEqual(topics["other-ai-companion-play"]["obsidian_path"], other["obsidian_path"])

    def test_taxonomy_projection_includes_exact_dependent_surfaces(self):
        self.prepare_taxonomy_projection_fixture()

        projection = self.store.taxonomy_projection("human_ai_intimacy_v1")

        self.assertEqual(
            [row["topic_id"] for row in projection["topic_changes"]],
            [self.assigned_topic_id, self.unchanged_assigned_topic_id],
        )
        self.assertEqual(
            projection["render_topic_ids"],
            [
                self.assigned_topic_id,
                self.unchanged_assigned_topic_id,
                self.relation_source_topic_id,
            ],
        )
        self.assertIn(
            "关注推送/示例人机互动群/00-按日期.md",
            projection["managed_date_index_paths"],
        )
        self.assertNotIn(self.history_topic_id, projection["render_topic_ids"])
        self.assertNotIn(
            self.metadata_relation_source_topic_id,
            projection["render_topic_ids"],
        )
        self.assertNotIn(
            "关注推送/示例人机互动群/00-按日期.user.md",
            projection["managed_date_index_paths"],
        )

    def test_projection_applies_only_exact_rows_to_supplied_connection(self):
        self.prepare_taxonomy_projection_fixture()
        source_hash = self.digest(self.db_path)
        vault_hashes = self.tree_digests(self.obsidian_root)

        projection = self.store.taxonomy_projection("human_ai_intimacy_v1")

        self.assertEqual(self.digest(self.db_path), source_hash)
        self.assertEqual(self.tree_digests(self.obsidian_root), vault_hashes)
        source = sqlite3.connect(self.db_path)
        shadow = sqlite3.connect(":memory:")
        source.backup(shadow)
        source.close()
        try:
            self.store.apply_taxonomy_projection(shadow, projection)

            changed = shadow.execute(
                "SELECT category, taxonomy_profile, taxonomy_version FROM topics WHERE topic_id = ?",
                (self.assigned_topic_id,),
            ).fetchone()
            unrelated = shadow.execute(
                "SELECT category FROM topics WHERE topic_id = ?",
                (self.unrelated_topic_id,),
            ).fetchone()
            self.assertEqual(
                tuple(changed),
                ("互动实验与玩法", "human_ai_intimacy_v1", 2),
            )
            self.assertEqual(unrelated[0], "Unrelated")
            self.assertEqual(self.digest(self.db_path), source_hash)
            self.assertEqual(self.tree_digests(self.obsidian_root), vault_hashes)
        finally:
            shadow.close()

    def test_apply_taxonomy_projection_rejects_mutated_before_value(self):
        self.prepare_taxonomy_projection_fixture()
        projection = self.store.taxonomy_projection("human_ai_intimacy_v1")
        source = sqlite3.connect(self.db_path)
        shadow = sqlite3.connect(":memory:")
        source.backup(shadow)
        source.close()
        try:
            changed_topic_id = projection["topic_changes"][0]["topic_id"]
            shadow.execute(
                "UPDATE topics SET category = 'drifted' WHERE topic_id = ?",
                (changed_topic_id,),
            )

            with self.assertRaisesRegex(ValueError, "^taxonomy_projection_drift$"):
                self.store.apply_taxonomy_projection(shadow, projection)
        finally:
            shadow.close()

    def test_render_topic_markdown_orders_tied_relations_by_target_topic_id(self):
        source = self.store.apply_event(
            candidate(title="Relation source", topic_key="relation-order-source", links=[]),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        first_target = self.store.apply_event(
            candidate(title="First target", topic_key="relation-order-first", links=[]),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        second_target = self.store.apply_event(
            candidate(title="Second target", topic_key="relation-order-second", links=[]),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        conn = sqlite3.connect(self.db_path)
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            conn.execute(
                """
                INSERT INTO relations(
                    source_topic_id, target_topic_id, relation, reason, created_at
                ) VALUES (?, ?, 'related', 'same sort key', 999)
                """,
                (source["topic_id"], second_target["topic_id"]),
            )
            conn.execute(
                """
                INSERT INTO relations(
                    source_topic_id, target_topic_id, relation, reason, created_at
                ) VALUES (?, ?, 'related', 'same sort key', 999)
                """,
                (source["topic_id"], first_target["topic_id"]),
            )
            markdown = self.store.render_topic_markdown(conn, source["topic_id"])
        finally:
            conn.close()

        self.assertLess(markdown.index("First target"), markdown.index("Second target"))
        relation_query = next(
            statement for statement in statements if "FROM relations r" in statement
        )
        order_by = relation_query.split("ORDER BY", 1)[1]
        self.assertIn("r.target_topic_id", order_by)

    def test_migration_does_not_scope_unassigned_chat_by_requested_profile(self):
        created = self.store.apply_event(
            candidate(
                title="旧名匹配但 username 未分配",
                topic_key="unassigned-username-legacy-name",
                category="AI伴侣交互",
                links=[],
            ),
            self.messages,
            {
                "monitor_chat_username": "unassigned@chatroom",
                "monitor_chat_display_name": "示例人机互动群",
            },
            {"relation": "new"},
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE topics SET taxonomy_profile = '', taxonomy_version = 0 WHERE topic_id = ?",
                (created["topic_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        store = KnowledgeStore.from_config({
            "monitor_knowledge_db": self.db_path,
            "monitor_obsidian_root": self.obsidian_root,
            "monitor_obsidian_subdir": "关注推送",
            "monitor_chat_taxonomy_profiles": {},
        }, read_only=True)
        plan = store.plan_taxonomy_migration("human_ai_intimacy_v1")

        self.assertEqual(plan["scoped_topic_count"], 0)
        self.assertEqual(plan["assignment_source_counts"], {})

    def test_migration_accepts_stored_valid_taxonomy_provenance(self):
        self.store.apply_event(
            candidate(category="AI伴侣交互", links=[]),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )
        store = KnowledgeStore.from_config({
            "monitor_knowledge_db": self.db_path,
            "monitor_obsidian_root": self.obsidian_root,
            "monitor_obsidian_subdir": "关注推送",
            "monitor_chat_taxonomy_profiles": {},
        }, read_only=True)

        plan = store.plan_taxonomy_migration("human_ai_intimacy_v1")

        self.assertEqual(plan["scoped_topic_count"], 1)
        self.assertEqual(plan["assignment_source_counts"], {"stored": 1})

    def test_migration_scopes_renamed_chat_by_explicit_username_assignment(self):
        created = self.store.apply_event(
            candidate(category="AI伴侣交互", links=[]),
            self.messages,
            {
                "monitor_chat_username": "room@chatroom",
                "monitor_chat_display_name": "群已经改名",
            },
            {"relation": "new"},
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE topics SET taxonomy_profile = '', taxonomy_version = 0 WHERE topic_id = ?",
                (created["topic_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        store = KnowledgeStore.from_config({
            "monitor_knowledge_db": self.db_path,
            "monitor_obsidian_root": self.obsidian_root,
            "monitor_obsidian_subdir": "关注推送",
            "monitor_chat_taxonomy_profiles": {
                "room@chatroom": "human_ai_intimacy_v1",
            },
        }, read_only=True)

        plan = store.plan_taxonomy_migration("human_ai_intimacy_v1")

        self.assertEqual(plan["scoped_topic_count"], 1)
        self.assertEqual(plan["assignment_source_counts"], {"explicit": 1})

    def test_migration_free_form_assignment_suppresses_legacy_name_fallback(self):
        created = self.store.apply_event(
            candidate(category="AI伴侣交互", links=[]),
            self.messages,
            {
                "monitor_chat_username": "room@chatroom",
                "monitor_chat_display_name": "示例人机互动群",
            },
            {"relation": "new"},
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE topics SET taxonomy_profile = '', taxonomy_version = 0 WHERE topic_id = ?",
                (created["topic_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        store = KnowledgeStore.from_config({
            "monitor_knowledge_db": self.db_path,
            "monitor_obsidian_root": self.obsidian_root,
            "monitor_obsidian_subdir": "关注推送",
            "monitor_chat_taxonomy_profiles": {"room@chatroom": "free_form"},
        }, read_only=True)

        plan = store.plan_taxonomy_migration("human_ai_intimacy_v1")

        self.assertEqual(plan["scoped_topic_count"], 0)
        self.assertEqual(plan["assignment_source_counts"], {})

    def test_migration_scopes_username_less_row_by_exact_assigned_stable_alias(self):
        created = self.store.apply_event(
            candidate(category="AI伴侣交互", links=[]),
            self.messages,
            {"monitor_chat_display_name": "Unrelated Display"},
            {"relation": "new"},
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE topics
                SET source_chat_username = '', taxonomy_profile = '',
                    taxonomy_version = 0, vault_chat_name = ?
                WHERE topic_id = ?
                """,
                ("Stable Vault", created["topic_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        store = KnowledgeStore.from_config({
            "monitor_knowledge_db": self.db_path,
            "monitor_obsidian_root": self.obsidian_root,
            "monitor_obsidian_subdir": "关注推送",
            "monitor_chat_taxonomy_profiles": {
                "room@chatroom": HUMAN_AI_INTIMACY_PROFILE,
            },
            "monitor_chat_aliases": {"room@chatroom": "Stable Vault"},
        }, read_only=True)

        plan = store.plan_taxonomy_migration(HUMAN_AI_INTIMACY_PROFILE)

        self.assertEqual(plan["scoped_topic_count"], 1)
        self.assertEqual(plan["assignment_source_counts"], {"stable_alias": 1})

    def test_migration_rejects_aliases_without_registered_preset_assignment(self):
        created = self.store.apply_event(
            candidate(category="AI伴侣交互", links=[]),
            self.messages,
            {"monitor_chat_display_name": "Unrelated Display"},
            {"relation": "new"},
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE topics
                SET source_chat_username = '', taxonomy_profile = '',
                    taxonomy_version = 0, vault_chat_name = ?
                WHERE topic_id = ?
                """,
                ("Stable Vault", created["topic_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        for assignment in (FREE_FORM_PROFILE, "missing_profile", None):
            with self.subTest(assignment=assignment):
                profiles = ({"room@chatroom": assignment} if assignment else {})
                store = KnowledgeStore.from_config({
                    "monitor_knowledge_db": self.db_path,
                    "monitor_obsidian_root": self.obsidian_root,
                    "monitor_obsidian_subdir": "关注推送",
                    "monitor_chat_taxonomy_profiles": profiles,
                    "monitor_chat_aliases": {"room@chatroom": "Stable Vault"},
                }, read_only=True)
                plan = store.plan_taxonomy_migration(HUMAN_AI_INTIMACY_PROFILE)
                self.assertEqual(plan["scoped_topic_count"], 0)

    def test_migration_refuses_alias_shared_by_multiple_registered_profiles(self):
        created = self.store.apply_event(
            candidate(category="AI伴侣交互", links=[]),
            self.messages,
            {"monitor_chat_display_name": "Unrelated Display"},
            {"relation": "new"},
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE topics
                SET source_chat_username = '', taxonomy_profile = '',
                    taxonomy_version = 0, vault_chat_name = ?
                WHERE topic_id = ?
                """,
                ("Shared Vault", created["topic_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        other_profile = dict(TAXONOMY_PROFILES[HUMAN_AI_INTIMACY_PROFILE])
        with patch.dict(TAXONOMY_PROFILES, {"other_registered_profile": other_profile}):
            store = KnowledgeStore.from_config({
                "monitor_knowledge_db": self.db_path,
                "monitor_obsidian_root": self.obsidian_root,
                "monitor_obsidian_subdir": "关注推送",
                "monitor_chat_taxonomy_profiles": {
                    "one@chatroom": HUMAN_AI_INTIMACY_PROFILE,
                    "two@chatroom": "other_registered_profile",
                },
                "monitor_chat_aliases": {
                    "one@chatroom": "Shared Vault",
                    "two@chatroom": "Shared Vault",
                },
            }, read_only=True)

            resolution = store._taxonomy_resolution_for_topic(
                store.get_topic(created["topic_id"])
            )
            plan = store.plan_taxonomy_migration(HUMAN_AI_INTIMACY_PROFILE)

        self.assertEqual(resolution, TaxonomyResolution("", "ambiguous_alias"))
        self.assertEqual(plan["scoped_topic_count"], 0)

    def test_reorganize_paths_applies_taxonomy_profile_to_legacy_target_chat_rows(self):
        target = self.store.apply_event(
            candidate(
                title="Claude 模型边界讨论",
                topic_key="legacy-target-chat-model-boundary",
                category="AI模型",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )
        self.move_topic_to_legacy_category(target, "AI模型")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE topics
                SET taxonomy_profile = '', taxonomy_version = 0
                WHERE topic_id = ?
                """,
                (target["topic_id"],),
            )
            conn.execute(
                """
                UPDATE events
                SET taxonomy_profile = '', taxonomy_version = 0
                WHERE topic_id = ?
                """,
                (target["topic_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        changes = self.store.find_category_changes()
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["from"], "AI模型")
        self.assertEqual(changes[0]["to"], "模型与平台")

        result = self.store.reorganize_paths()
        topic = self.rows("topics")[0]
        event = self.rows("events")[0]

        self.assertEqual(result["path_change_count"], 1)
        self.assertEqual(topic["category"], "模型与平台")
        self.assertEqual(topic["taxonomy_profile"], HUMAN_AI_INTIMACY_PROFILE)
        self.assertEqual(topic["taxonomy_version"], 2)
        self.assertEqual(event["category"], "模型与平台")
        self.assertEqual(event["taxonomy_profile"], HUMAN_AI_INTIMACY_PROFILE)
        self.assertEqual(event["taxonomy_version"], 2)
        self.assertIn(
            os.path.join("关注推送", "示例人机互动群", "模型与平台"),
            topic["obsidian_path"],
        )

    def test_find_category_changes_reports_metadata_only_taxonomy_backfill(self):
        target = self.store.apply_event(
            candidate(
                title="Claude 模型边界讨论",
                topic_key="metadata-only-taxonomy-backfill",
                category="AI模型",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE topics
                SET taxonomy_profile = '', taxonomy_version = 0
                WHERE topic_id = ?
                """,
                (target["topic_id"],),
            )
            conn.execute(
                """
                UPDATE events
                SET taxonomy_profile = '', taxonomy_version = 0
                WHERE topic_id = ?
                """,
                (target["topic_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        changes = self.store.find_category_changes()

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["from"], "模型与平台")
        self.assertEqual(changes[0]["to"], "模型与平台")
        self.assertEqual(changes[0]["reason"], "taxonomy_profile")
        self.assertEqual(changes[0]["from_path"], target["obsidian_path"])
        self.assertEqual(changes[0]["to_path"], target["obsidian_path"])

    def test_taxonomy_migration_dry_run_reserves_planned_paths_for_collisions(self):
        first = self.store.apply_event(
            candidate(
                title="同名主题",
                topic_key="same-title-model",
                category="AI模型",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )
        second = self.store.apply_event(
            candidate(
                title="同名主题",
                topic_key="same-title-technique",
                category="技术方法",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )
        self.move_topic_to_legacy_category(first, "AI模型")
        self.move_topic_to_legacy_category(second, "技术方法")

        plan = self.store.plan_taxonomy_migration()
        to_paths = [
            change["to_path"]
            for change in plan["changes"]
            if change["title"] == "同名主题"
        ]

        self.assertEqual(len(to_paths), 2)
        self.assertEqual(len(set(to_paths)), 2)
        self.assertIn(
            os.path.join("关注推送", "示例人机互动群", "模型与平台", "同名主题.md"),
            to_paths,
        )
        self.assertIn(
            os.path.join("关注推送", "示例人机互动群", "工具与方法", "同名主题.md"),
            to_paths,
        )

    def test_taxonomy_migration_dry_run_keeps_unknown_target_category_reviewable(self):
        result = self.store.apply_event(
            candidate(
                title="边缘话题观察",
                topic_key="unclear-human-ai-topic",
                category="非常细的新标签",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "Example Interaction Lab"},
            {"relation": "new"},
        )
        self.move_topic_to_legacy_category(result, "非常细的新标签")

        plan = self.store.plan_taxonomy_migration()

        self.assertEqual(plan["unresolved_count"], 1)
        self.assertEqual(plan["category_mappings"][0]["from"], "非常细的新标签")
        self.assertEqual(plan["category_mappings"][0]["to"], "待归类")
        self.assertEqual(plan["category_mappings"][0]["example_paths"], [result["obsidian_path"]])

    def test_taxonomy_migration_dry_run_excludes_history_summaries(self):
        self.store.apply_event(
            candidate(
                title="示例人机互动群 · 2026-06-10 历史总结",
                topic_key="history-summary:room:2026-06-10",
                category="技术方法",
                links=[],
                event_type="history_summary",
            ),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )

        plan = self.store.plan_taxonomy_migration()

        self.assertEqual(plan["scoped_topic_count"], 0)
        self.assertEqual(plan["legacy_history_summary_count"], 0)
        self.assertEqual(plan["migratable_topic_count"], 0)
        self.assertEqual(plan["category_change_count"], 0)
        self.assertEqual(plan["path_change_count"], 0)

    def test_taxonomy_migration_dry_run_maps_observed_drift_aliases(self):
        play = self.store.apply_event(
            candidate(
                title="互动玩法观察",
                topic_key="observed-play-label",
                category="互动玩法",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "示例人机互动群"},
            {"relation": "new"},
        )
        self.move_topic_to_legacy_category(play, "互动玩法")
        policy = self.store.apply_event(
            candidate(
                title="AI政策风险观察",
                topic_key="observed-policy-label",
                category="AI政策",
                links=[],
            ),
            self.messages,
            {"monitor_chat_display_name": "Example Interaction Lab"},
            {"relation": "new"},
        )
        self.move_topic_to_legacy_category(policy, "AI政策")

        plan = self.store.plan_taxonomy_migration()
        mappings = {(row["from"], row["to"]): row["count"] for row in plan["category_mappings"]}

        self.assertEqual(mappings[("互动玩法", "互动实验与玩法")], 1)
        self.assertEqual(mappings[("AI政策", "风险与边界")], 1)

    def test_human_ai_taxonomy_v2_splits_platform_and_method_aliases(self):
        cases = [
            ("AI模型", "模型与平台"),
            ("服务动态", "模型与平台"),
            ("AI事件", "风险与边界"),
            ("工具更新", "工具与方法"),
            ("技术方法", "工具与方法"),
            ("自建app", "工具与方法"),
            ("开源项目", "资源线索"),
            ("群聊问答与经验", "记忆与连续性"),
            ("AI互动文化", "AI关系与理论"),
        ]
        for index, (old_category, expected) in enumerate(cases):
            created = self.store.apply_event(
                candidate(
                    title=f"taxonomy v2 alias {index}",
                    topic_key=f"taxonomy-v2-alias-{index}",
                    category=old_category,
                    links=[],
                ),
                self.messages,
                {"monitor_chat_display_name": "示例人机互动群"},
                {"relation": "new"},
            )
            self.move_topic_to_legacy_category(created, old_category)

        plan = self.store.plan_taxonomy_migration()
        mappings = {(row["from"], row["to"]): row["count"] for row in plan["category_mappings"]}

        for old_category, expected in cases:
            self.assertEqual(mappings[(old_category, expected)], 1)

    def test_human_ai_taxonomy_v2_splits_legacy_tool_model_bucket_by_title(self):
        cases = [
            ("Claude 路由与模型上下文观察", "模型与平台"),
            ("Qwen3.6 27B体验、记忆系统讨论、充值提示", "模型与平台"),
            ("Anthropic 周限额提高 50% 活动即将到期", "模型与平台"),
            ("心跳脚本配置教程", "工具与方法"),
            ("Codex+Tmux回车问题修复方案", "工具与方法"),
            ("账号封号与KYC风险提醒", "风险与边界"),
            ("示例编码工具需要电话验证？公开测试反馈", "风险与边界"),
            ("开源项目资源分享", "资源线索"),
            ("Codex 现状 ExampleToolkit V2 测试邀请", "资源线索"),
        ]
        for index, (title, _) in enumerate(cases):
            created = self.store.apply_event(
                candidate(
                    title=title,
                    topic_key=f"legacy-tool-model-split-{index}",
                    category="AI模型",
                    links=[],
                ),
                self.messages,
                {"monitor_chat_display_name": "示例人机互动群"},
                {"relation": "new"},
            )
            self.move_topic_to_legacy_category(created, "工具与模型")

        plan = self.store.plan_taxonomy_migration()
        changes = {row["title"]: row["to"] for row in plan["changes"]}

        for title, expected in cases:
            self.assertEqual(changes[title], expected)

    def test_file_resources_get_title_prefix_and_month_folder_hint(self):
        config = {
            "monitor_chat_display_name": "示例技术群",
            "db_dir": os.path.join(
                self.tmp.name,
                "xwechat_files",
                "wxid_test",
                "db_storage",
            ),
        }
        file_dir = os.path.join(self.tmp.name, "xwechat_files", "wxid_test", "msg", "file", "2026-05")
        os.makedirs(file_dir, exist_ok=True)
        messages = [
            msg(16, "示例成员甲", "[文件] test workflow.zip"),
            msg(17, "示例成员乙", "这个示例 workflow 可以用于测试"),
        ]

        result = self.store.apply_event(
            candidate(
                title="示例成员分享 workflow 文件",
                links=[],
                key_facts=["示例成员分享了 workflow 文件"],
            ),
            messages,
            config,
            {"relation": "new"},
        )

        basename = os.path.basename(result["knowledge_path"])
        self.assertEqual(basename, "[文件] 示例成员分享 workflow 文件.md")
        with open(result["knowledge_path"], encoding="utf-8") as f:
            md = f.read()
        self.assertIn("# [文件] 示例成员分享 workflow 文件", md)
        self.assertIn("has_files: true", md)
        self.assertIn("resource_types:", md)
        self.assertIn('  - "file"', md)
        self.assertIn("### 文件", md)
        self.assertIn("test workflow.zip", md)
        self.assertIn("file://" + file_dir.replace(" ", "%20"), md)

    def test_attachment_mention_is_registered_with_event_transaction(self):
        messages = [
            {
                **msg(16, "蛋", "[文件] source reliability.pdf"),
                "source_message_id": "wgmsg_fixture_001",
                "resources": [
                    {
                        "kind": "file",
                        "resource_index": 2,
                        "original_name": "source reliability.pdf",
                        "declared_size": 4096,
                        "declared_hash": "a" * 32,
                        "attach_id": "fixture-attach-id",
                        "extension": "pdf",
                    }
                ],
            }
        ]

        result = self.store.apply_event(
            candidate(title="Source reliability 文件", links=[]),
            messages,
            self.config,
            {"relation": "new"},
        )

        mentions = self.rows("attachment_mentions")
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0]["event_id"], result["event_id"])
        self.assertEqual(mentions[0]["topic_id"], result["topic_id"])
        self.assertEqual(mentions[0]["source_message_id"], "wgmsg_fixture_001")
        self.assertEqual(mentions[0]["resource_index"], 2)
        self.assertEqual(mentions[0]["kind"], "file")
        self.assertEqual(mentions[0]["declared_size"], 4096)
        self.assertEqual(mentions[0]["status"], "pending")

        with open(result["knowledge_path"], encoding="utf-8") as f:
            md = f.read()
        self.assertIn("归档状态：pending", md)

    def test_attachment_registration_failure_rolls_back_event_and_topic(self):
        messages = [
            {
                **msg(16, "蛋", "[文件] must-rollback.txt"),
                "source_message_id": "wgmsg_fixture_rollback",
                "resources": [
                    {
                        "kind": "file",
                        "resource_index": 0,
                        "original_name": "must-rollback.txt",
                    }
                ],
            }
        ]

        with patch.object(
            KnowledgeStore,
            "_register_attachment_mentions",
            side_effect=sqlite3.OperationalError("fixture catalog failure"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "fixture catalog failure"):
                self.store.apply_event(
                    candidate(title="应整体回滚", links=[]),
                    messages,
                    self.config,
                    {"relation": "new"},
                )

        self.assertEqual(self.rows("topics"), [])
        self.assertEqual(self.rows("events"), [])
        self.assertEqual(self.rows("attachment_mentions"), [])

    def test_image_attachment_projection_keeps_sender_and_time(self):
        messages = [{
            **msg(18, "蛋", "[图片]"),
            "source_message_id": "wgmsg_fixture_image",
            "resources": [{
                "kind": "image",
                "resource_index": 0,
                "original_name": "",
                "declared_hash": "b" * 32,
            }],
        }]

        result = self.store.apply_event(
            candidate(title="图片附件 source reliability", links=[]),
            messages,
            self.config,
            {"relation": "new"},
        )

        with open(result["knowledge_path"], encoding="utf-8") as file:
            markdown = file.read()
        self.assertIn("图片附件（2026-05-29 03:18 · 蛋）", markdown)
        self.assertIn("归档状态：pending", markdown)

    def test_clean_filename_collision_uses_compact_month_day(self):
        first = self.store.apply_event(
            candidate(title="同名主题", topic_key="same-title-a", links=[]),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        second = self.store.apply_event(
            candidate(title="同名主题", topic_key="same-title-b", links=[]),
            [msg(18, "示例成员甲", "第二条同名主题")],
            self.config,
            {"relation": "new"},
        )

        self.assertEqual(os.path.basename(first["knowledge_path"]), "同名主题.md")
        self.assertEqual(os.path.basename(second["knowledge_path"]), "05-29 同名主题.md")
        self.assertNotIn("2026-05-29 03-18", second["knowledge_path"])

    def test_maintenance_does_not_merge_broadly_related_ai_topics(self):
        self.store.apply_event(
            candidate(
                title="Example Model 2.0 思考链提取技巧讨论",
                topic_key="example-model-thinking-chain",
                summary="1. 【03:16】群里讨论 Example Model 2.0 的思考链提取方法。",
                entities=["Claude", "2.0"],
                key_facts=["群里讨论 Example Model 2.0 思考链提取方法"],
                links=[],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        self.store.apply_event(
            candidate(
                title="Example Model 1.9 vs 2.0 实际体验讨论",
                topic_key="claude-46-vs-48-experience",
                summary="1. 【03:20】示例讨论比较 Example Model 1.9 和 2.0 的测试行为。",
                entities=["Claude", "2.0"],
                key_facts=["示例讨论比较 Example Model 1.9 和 2.0 的测试行为"],
                links=[],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )

        result = self.store.run_maintenance(dry_run=True)

        self.assertEqual(result["group_count"], 0)
        self.assertEqual(result["removed_count"], 0)
        self.assertEqual(result["reexport_count"], 2)

    def test_safe_filename_handles_chinese_emoji_slash_and_long_title(self):
        unsafe = "Example/Model: 2.0 🚀 " + "很长" * 60
        result = self.store.apply_event(
            candidate(title=unsafe, category="AI/模型🚀"),
            self.messages,
            self.config,
            {"relation": "new"},
        )

        basename = os.path.basename(result["knowledge_path"])
        self.assertNotIn("/", basename)
        self.assertNotIn(":", basename)
        self.assertNotIn("🚀", basename)
        self.assertLessEqual(len(safe_path_part(unsafe, max_len=90)), 90)
        self.assertTrue(os.path.exists(result["knowledge_path"]))


if __name__ == "__main__":
    unittest.main()
