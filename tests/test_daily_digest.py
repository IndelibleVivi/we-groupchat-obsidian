import os
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo

from core.daily_digest import (
    build_daily_digest,
    _day_bounds,
    digest_output_path,
    mark_daily_digest_success,
    notification_summary,
    refresh_existing_daily_digest,
    refresh_existing_daily_digests,
    source_window_dates,
    should_run_daily_digest,
    write_daily_digest,
)
from core.knowledge import KnowledgeStore
from core.review_queue import ReviewQueue


def local_ts(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(
        tzinfo=ZoneInfo("Asia/Shanghai")
    ).timestamp()


def zoned_ts(text, timezone):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(
        tzinfo=ZoneInfo(timezone)
    ).timestamp()


def msg(ts_text, text):
    return {
        "timestamp": local_ts(ts_text),
        "time_str": ts_text,
        "sender": "成员",
        "text": text,
    }


def candidate(**overrides):
    data = {
        "title": "Claude Code repo patch",
        "summary": "1. 【10:00】成员分享了一个 repo patch，可评估部署。",
        "topic_key": "claude-code-repo-patch",
        "category": "工具更新",
        "entities": ["Claude Code"],
        "key_facts": ["成员分享了 repo patch"],
        "links": ["https://github.com/example/repo"],
        "event_type": "resource",
        "status_hint": "tracking",
    }
    data.update(overrides)
    return data


class DailyDigestTests(unittest.TestCase):
    def test_day_bounds_follow_local_midnight_across_dst(self):
        timezone = ZoneInfo("America/New_York")
        cases = [
            (datetime(2026, 3, 8, 12, tzinfo=timezone), datetime(2026, 3, 9, 0, tzinfo=timezone), 23),
            (datetime(2026, 11, 1, 12, tzinfo=timezone), datetime(2026, 11, 2, 0, tzinfo=timezone), 25),
        ]

        for now, expected_end, expected_hours in cases:
            with self.subTest(now=now):
                start_ts, end_ts = _day_bounds(now)
                self.assertEqual(
                    datetime.fromtimestamp(start_ts, timezone),
                    now.replace(hour=0, minute=0, second=0, microsecond=0),
                )
                self.assertEqual(datetime.fromtimestamp(end_ts, timezone), expected_end)
                self.assertEqual((end_ts - start_ts) / 3600, expected_hours)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = {
            "monitor_knowledge_db": os.path.join(self.tmp.name, "knowledge.db"),
            "monitor_obsidian_root": os.path.join(self.tmp.name, "obsidian"),
            "monitor_obsidian_subdir": "微信群聊/关注推送",
            "review_queue_dir": os.path.join(self.tmp.name, "review_queue"),
            "daily_digest_timezone": "Asia/Shanghai",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_daily_digest_counts_notes_and_lists_only_today_action_items(self):
        store = KnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            obsidian_subdir=self.config["monitor_obsidian_subdir"],
            now_func=lambda: local_ts("2026-06-18 10:00"),
        )
        store.apply_event(
            candidate(),
            [msg("2026-06-18 10:00", "repo patch https://github.com/example/repo")],
            {"monitor_chat_display_name": "示例技术群"},
            {"relation": "new"},
        )
        store.now_func = lambda: local_ts("2026-06-18 10:30")
        store.apply_event(
            candidate(
                title="AI伴侣交互连续性设计讨论",
                summary="1. 【10:30】成员讨论了连续性设计，没有资源候选。",
                topic_key="continuity-design",
                links=[],
                key_facts=["成员讨论了连续性设计"],
                category="设计讨论",
            ),
            [msg("2026-06-18 10:30", "连续性设计讨论")],
            {"monitor_chat_display_name": "Example Interaction Lab"},
            {"relation": "new"},
        )
        queue = ReviewQueue(
            self.config["review_queue_dir"],
            now_func=lambda: local_ts("2026-06-18 11:00"),
        )
        queue.create_or_reuse({
            "title": "VPS token risk",
            "summary": "需要检查 deploy token。",
            "message_hash": "risk",
        })
        queue.create_or_reuse({
            "title": "普通设计闲聊",
            "summary": "只是关系哲学讨论。",
            "message_hash": "design",
        })

        digest = build_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )

        self.assertEqual(digest["date"], "2026-06-18")
        self.assertEqual(digest["new_notes_count"], 2)
        self.assertEqual(digest["today_action_count"], 1)
        self.assertEqual(len(digest["today_action_items"]), 1)
        self.assertEqual(digest["pending_review_count"], 0)
        self.assertEqual(digest["engineering_candidates"], digest["today_action_items"])
        self.assertIn("Claude Code repo patch", digest["markdown"])
        self.assertIn("AI伴侣交互连续性设计讨论", digest["markdown"])
        risk_section = digest["markdown"].split("## 风险与边界", 1)[1]
        self.assertIn("VPS token risk", risk_section)
        self.assertNotIn("普通设计闲聊", risk_section)

    def test_historical_digest_uses_source_window_not_late_processing_time(self):
        store = KnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            obsidian_subdir=self.config["monitor_obsidian_subdir"],
            now_func=lambda: local_ts("2026-06-19 00:20"),
        )
        store.apply_event(
            candidate(title="Late catch-up note", topic_key="late-catch-up", links=[]),
            [msg("2026-06-18 23:50", "昨晚漏处理的内容")],
            {"monitor_chat_display_name": "示例技术群"},
            {"relation": "new"},
        )

        digest = build_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-19 00:30"),
            target_date="2026-06-18",
        )

        self.assertEqual(digest["date"], "2026-06-18")
        self.assertEqual(digest["new_notes_count"], 1)
        self.assertIn("Late catch-up note", digest["markdown"])
        self.assertIn("Generated: 2026-06-19 00:30 CST", digest["markdown"])

    def test_historical_digest_uses_queue_source_window(self):
        ReviewQueue(
            self.config["review_queue_dir"],
            now_func=lambda: local_ts("2026-06-19 00:20"),
        ).create_or_reuse({
            "title": "Late source-window resource",
            "summary": "补跑时识别出的资源。",
            "created_at": "2026-06-19 00:20:00+08:00",
            "window_start": "2026-06-18 23:40",
            "window_end": "2026-06-18 23:50",
            "actionability": "evaluate_reference",
            "suggested_action": "evaluate_reference",
            "message_hash": "late-source-window-resource",
        })

        digest = build_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-19 00:30"),
            target_date="2026-06-18",
        )

        self.assertEqual(digest["today_action_count"], 1)
        self.assertIn("Late source-window resource", digest["markdown"])

    def test_daily_digest_renders_projection_contract(self):
        digest = build_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )

        expected_prefix = "\n".join((
            "---",
            "source_app: we-groupchat-obsidian",
            "source_kind: projection",
            "source_schema_version: 1",
            "projection_kind: daily_digest",
            "---",
        ))
        self.assertTrue(digest["markdown"].startswith(expected_prefix))
        frontmatter = digest["markdown"].split("---", 2)[1]
        self.assertNotIn("source_id:", frontmatter)
        body = digest["markdown"].split("---\n", 2)[2]
        self.assertTrue(body.startswith("# WeChat Daily Digest - 2026-06-18\n"))
        self.assertIn("- Generated: 2026-06-18 21:31 CST", body)

    def test_digest_topics_use_clickable_obsidian_note_links(self):
        store = KnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            obsidian_subdir=self.config["monitor_obsidian_subdir"],
            now_func=lambda: local_ts("2026-06-18 10:00"),
        )
        store.apply_event(
            candidate(),
            [msg("2026-06-18 10:00", "repo patch https://github.com/example/repo")],
            {"monitor_chat_display_name": "示例技术群"},
            {"relation": "new"},
        )

        digest = build_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )

        topic = digest["topics"][0]
        target = os.path.splitext(topic["obsidian_path"])[0]
        self.assertIn(
            f"P2 · [[{target}|Claude Code repo patch]] · 示例技术群 / 工具更新",
            digest["markdown"],
        )
        self.assertNotIn("[P2]", digest["markdown"])
        self.assertNotIn(f" -> {topic['obsidian_path']}", digest["markdown"])

    def test_digest_candidates_prefer_current_knowledge_note_path(self):
        store = KnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            obsidian_subdir=self.config["monitor_obsidian_subdir"],
            now_func=lambda: local_ts("2026-06-18 10:00"),
        )
        result = store.apply_event(
            candidate(title="Current knowledge note", links=[]),
            [msg("2026-06-18 10:00", "repo patch")],
            {"monitor_chat_display_name": "示例技术群"},
            {"relation": "new"},
        )
        ReviewQueue(
            self.config["review_queue_dir"],
            now_func=lambda: local_ts("2026-06-18 11:00"),
        ).create_or_reuse({
            "title": "新线索: Current knowledge note",
            "summary": "需要检查 repo patch。",
            "knowledge_topic_id": result["topic_id"],
            "obsidian_path": "微信群聊/关注推送/示例技术群/工具更新/[链接] stale-note.md",
            "priority": "P2",
            "actionability": "evaluate_reference",
            "suggested_action": "evaluate_reference",
            "message_hash": "stale-candidate-path",
        })

        digest = build_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )

        target = os.path.splitext(result["obsidian_path"])[0]
        resource_section = digest["markdown"].split("## 今日资源机会", 1)[1].split("## 风险与边界", 1)[0]
        self.assertIn(f"[[{target}|Current knowledge note]]", resource_section)
        self.assertNotIn("stale-note", resource_section)

    def test_daily_digest_renders_risk_after_first_twelve_action_items(self):
        queue = ReviewQueue(self.config["review_queue_dir"])
        for index in range(12):
            queue.create_or_reuse({
                "title": f"Resource lead {index + 1}",
                "summary": "需要评估参考链接。",
                "created_at": f"2026-06-18 10:{index:02d}:00+08:00",
                "actionability": "evaluate_reference",
                "suggested_action": "evaluate_reference",
                "message_hash": f"resource-{index}",
            })
        queue.create_or_reuse({
            "title": "Late risk item",
            "summary": "需要检查 deploy token。",
            "created_at": "2026-06-18 11:00:00+08:00",
            "actionability": "review_risk",
            "suggested_action": "review_risk",
            "message_hash": "late-risk",
        })

        digest = build_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )

        self.assertEqual(digest["today_action_count"], 13)
        self.assertEqual(digest["today_risk_count"], 1)
        risk_section = digest["markdown"].split("## 风险与边界", 1)[1]
        self.assertIn("Late risk item", risk_section)
        self.assertNotIn("- None", risk_section)

    def test_daily_digest_caps_resource_and_risk_sections_with_overflow_counts(self):
        queue = ReviewQueue(self.config["review_queue_dir"])
        for index in range(14):
            queue.create_or_reuse({
                "title": f"Resource lead {index + 1}",
                "summary": "需要评估参考链接。",
                "created_at": f"2026-06-18 10:{index:02d}:00+08:00",
                "actionability": "evaluate_reference",
                "suggested_action": "evaluate_reference",
                "message_hash": f"resource-cap-{index}",
            })
            queue.create_or_reuse({
                "title": f"Risk item {index + 1}",
                "summary": "需要检查风险。",
                "created_at": f"2026-06-18 11:{index:02d}:00+08:00",
                "actionability": "review_risk",
                "suggested_action": "review_risk",
                "message_hash": f"risk-cap-{index}",
            })

        digest = build_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )

        resource_section = digest["markdown"].split("## 今日资源机会", 1)[1].split("## 风险与边界", 1)[0]
        risk_section = digest["markdown"].split("## 风险与边界", 1)[1]
        self.assertEqual(digest["today_action_count"], 28)
        self.assertEqual(digest["today_risk_count"], 14)
        self.assertIn("Resource lead 12", resource_section)
        self.assertNotIn("Resource lead 13", resource_section)
        self.assertIn("- ... 另有 2 条今日资源机会未展开", resource_section)
        self.assertIn("Risk item 12", risk_section)
        self.assertNotIn("Risk item 13", risk_section)
        self.assertIn("- ... 另有 2 条风险与边界未展开", risk_section)

    def test_daily_digest_interprets_naive_queue_created_at_in_digest_timezone(self):
        config = dict(self.config)
        config["daily_digest_timezone"] = "UTC"
        queue = ReviewQueue(config["review_queue_dir"])
        queue.create_or_reuse({
            "title": "UTC boundary resource",
            "summary": "UTC digest day should include this naive timestamp.",
            "created_at": "2026-06-18 00:30:00",
            "actionability": "evaluate_reference",
            "suggested_action": "evaluate_reference",
            "message_hash": "utc-boundary",
        })
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Singapore"
        if hasattr(time, "tzset"):
            time.tzset()
        try:
            digest = build_daily_digest(
                config,
                now_func=lambda: zoned_ts("2026-06-18 21:31", "UTC"),
            )
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            if hasattr(time, "tzset"):
                time.tzset()

        self.assertEqual(digest["today_action_count"], 1)
        self.assertIn("UTC boundary resource", digest["markdown"])

    def test_daily_digest_avoids_historical_queue_debt_wording(self):
        queue = ReviewQueue(self.config["review_queue_dir"])
        queue.create_or_reuse({
            "title": "VPS token risk",
            "summary": "需要检查 deploy token。",
            "message_hash": "risk",
        })

        digest = build_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )

        self.assertIn("## 今日值得回看", digest["markdown"])
        self.assertIn("## 今日资源机会", digest["markdown"])
        self.assertNotIn("Pending review queue", digest["markdown"])
        self.assertNotIn("P1/P2 Engineering Candidates", digest["markdown"])

    def test_notification_summary_omits_total_pending_queue_count(self):
        digest = {
            "date": "2026-06-18",
            "new_notes_count": 8,
            "today_action_count": 2,
            "today_risk_count": 1,
            "path": "/tmp/2026-06-18 Daily Digest.md",
        }

        subtitle, message = notification_summary(digest)

        self.assertIn("2026-06-18 · 8 notes · 2 actions · 1 risk", subtitle)
        self.assertNotIn("pending", subtitle)
        self.assertNotIn("P1/P2", message)
        self.assertIn("Digest:", message)

    def test_write_daily_digest_defaults_to_obsidian_vault_path(self):
        digest = write_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )

        self.assertTrue(os.path.exists(digest["path"]))
        self.assertEqual(
            digest["path"],
            os.path.join(
                self.config["monitor_obsidian_root"],
                "微信群聊",
                "关注推送",
                "Daily Digest",
                "2026-06-18 Daily Digest.md",
            ),
        )
        self.assertEqual(
            digest["obsidian_path"],
            os.path.join("微信群聊", "关注推送", "Daily Digest", "2026-06-18 Daily Digest.md"),
        )
        with open(digest["path"], encoding="utf-8") as handle:
            self.assertIn("# WeChat Daily Digest - 2026-06-18", handle.read())

    def test_write_daily_digest_preserves_last_good_file_when_publish_fails(self):
        path, _ = digest_output_path(
            self.config,
            "2026-06-18",
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("last-known-good\n")

        with patch("core.daily_digest.os.replace", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                write_daily_digest(
                    self.config,
                    now_func=lambda: local_ts("2026-06-18 21:31"),
                )

        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "last-known-good\n")
        self.assertEqual(
            [name for name in os.listdir(os.path.dirname(path)) if name.endswith(".tmp")],
            [],
        )

    def test_write_daily_digest_fsyncs_payload_and_parent_before_success(self):
        with patch("core.daily_digest.os.fsync", wraps=os.fsync) as fsync:
            digest = write_daily_digest(
                self.config,
                now_func=lambda: local_ts("2026-06-18 21:31"),
            )

        self.assertTrue(os.path.isfile(digest["path"]))
        self.assertEqual(fsync.call_count, 2)

    def test_historical_digest_is_written_under_month_subfolder(self):
        digest = write_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-18 21:31"),
            target_date="2026-05-31",
        )

        self.assertEqual(
            digest["path"],
            os.path.join(
                self.config["monitor_obsidian_root"],
                "微信群聊",
                "关注推送",
                "Daily Digest",
                "2026-05",
                "2026-05-31 Daily Digest.md",
            ),
        )
        self.assertEqual(
            digest["obsidian_path"],
            os.path.join(
                "微信群聊",
                "关注推送",
                "Daily Digest",
                "2026-05",
                "2026-05-31 Daily Digest.md",
            ),
        )

    def test_writing_current_digest_archives_root_files_from_past_months(self):
        digest_root = os.path.join(
            self.config["monitor_obsidian_root"],
            "微信群聊",
            "关注推送",
            "Daily Digest",
        )
        os.makedirs(digest_root, exist_ok=True)
        legacy_path = os.path.join(digest_root, "2026-05-31 Daily Digest.md")
        with open(legacy_path, "w", encoding="utf-8") as handle:
            handle.write("historical digest\n")

        write_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )

        archived_path = os.path.join(
            digest_root,
            "2026-05",
            "2026-05-31 Daily Digest.md",
        )
        self.assertFalse(os.path.exists(legacy_path))
        with open(archived_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "historical digest\n")

    def test_refresh_existing_digest_rebuilds_only_the_source_day(self):
        write_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )
        store = KnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            obsidian_subdir=self.config["monitor_obsidian_subdir"],
            now_func=lambda: local_ts("2026-06-19 00:20"),
        )
        store.apply_event(
            candidate(title="Post-digest note", topic_key="post-digest", links=[]),
            [msg("2026-06-18 23:50", "digest 之后抵达")],
            {"monitor_chat_display_name": "示例技术群"},
            {"relation": "new"},
        )

        refreshed = refresh_existing_daily_digest(
            self.config,
            local_ts("2026-06-18 23:50"),
            now_func=lambda: local_ts("2026-06-19 00:30"),
        )

        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed["date"], "2026-06-18")
        self.assertEqual(refreshed["new_notes_count"], 1)
        self.assertIn("Post-digest note", refreshed["markdown"])

    def test_source_window_dates_expands_every_local_day(self):
        self.assertEqual(
            source_window_dates(
                self.config,
                "2026-06-18 23:50",
                "2026-06-20 00:10",
            ),
            ["2026-06-18", "2026-06-19", "2026-06-20"],
        )

    def test_refresh_existing_digests_only_rewrites_existing_dates(self):
        existing_path, _ = digest_output_path(
            self.config,
            "2026-06-19",
            now_func=lambda: local_ts("2026-06-20 00:30"),
        )
        os.makedirs(os.path.dirname(existing_path), exist_ok=True)
        with open(existing_path, "w", encoding="utf-8") as handle:
            handle.write("stale")

        refreshed = refresh_existing_daily_digests(
            self.config,
            ["2026-06-18", "2026-06-19"],
            now_func=lambda: local_ts("2026-06-20 00:30"),
        )

        missing_path, _ = digest_output_path(
            self.config,
            "2026-06-18",
            now_func=lambda: local_ts("2026-06-20 00:30"),
        )
        self.assertFalse(os.path.exists(missing_path))
        self.assertEqual([item["date"] for item in refreshed], ["2026-06-19"])

    def test_digest_output_path_allows_explicit_custom_dir(self):
        config = dict(self.config)
        config["daily_digest_dir"] = os.path.join(self.tmp.name, "custom_digests")

        path, obsidian_path = digest_output_path(config, "2026-06-18")

        self.assertEqual(path, os.path.join(config["daily_digest_dir"], "2026-06-18-daily-digest.md"))
        self.assertEqual(obsidian_path, "")

    def test_notification_summary_includes_digest_path(self):
        digest = write_daily_digest(
            self.config,
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )

        _subtitle, message = notification_summary(digest)

        self.assertIn(digest["path"], message)

    def test_should_run_daily_digest_waits_for_success_mark(self):
        state_path = os.path.join(self.tmp.name, "daily_state.json")

        before = should_run_daily_digest(
            state_path,
            {"daily_digest_time": "21:30", "daily_digest_timezone": "Asia/Shanghai"},
            now_func=lambda: local_ts("2026-06-18 21:29"),
        )
        first = should_run_daily_digest(
            state_path,
            {"daily_digest_time": "21:30", "daily_digest_timezone": "Asia/Shanghai"},
            now_func=lambda: local_ts("2026-06-18 21:31"),
        )
        second = should_run_daily_digest(
            state_path,
            {"daily_digest_time": "21:30", "daily_digest_timezone": "Asia/Shanghai"},
            now_func=lambda: local_ts("2026-06-18 22:00"),
        )
        mark_daily_digest_success(
            state_path,
            {"daily_digest_timezone": "Asia/Shanghai"},
            now_func=lambda: local_ts("2026-06-18 22:01"),
        )
        after_success = should_run_daily_digest(
            state_path,
            {"daily_digest_time": "21:30", "daily_digest_timezone": "Asia/Shanghai"},
            now_func=lambda: local_ts("2026-06-18 22:02"),
        )

        self.assertFalse(before)
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertFalse(after_success)


if __name__ == "__main__":
    unittest.main()
