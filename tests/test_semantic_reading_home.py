"""Reading-home integration uses temporary canonical stores and vaults only."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.url_safety import redact_urls_in_text
from core.semantic.briefing import BriefChange, BriefingService
from core.semantic.controls import OpenObservation, ReaderControls, SearchAnswer
from core.semantic.projections import (
    BodyBlock, BodyPlan, CanonicalReadingAdapter, ProjectionPublisher, ReadingError,
    region_body, wikilink,
)
from core.semantic.reading_home import ReadingHome
from core.semantic.store import SemanticStore
from tests import test_semantic_projections as projection_fixtures


class ReadingHomeTests(unittest.TestCase):
    canonical_extract = projection_fixtures.ReadingHarness.canonical_extract
    canonical_actions = projection_fixtures.ReadingHarness.canonical_actions
    NOW = datetime(2026, 10, 13, 16, 30, tzinfo=timezone.utc)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.store = SemanticStore(self.base / "generation.sqlite", "home-generation")
        self.publisher = ProjectionPublisher.for_store(self.store, self.base / "vault/WGO")
        self.ledger = self.publisher.ledger
        self.controls = ReaderControls(self.ledger, self.publisher, search=lambda _: SearchAnswer("空", ()))
        self.home = ReadingHome(self.store, self.publisher, self.controls,
                                config={"daily_digest_timezone": "Asia/Singapore"})

    def item(self, identity="cat", *, title="窗边的猫", publish=True):
        observation, span = self.canonical_extract(self.store, "capture-" + identity, "猫躺在窗边的纸箱里。")
        self.canonical_actions(self.store, [{"action_key": "create-" + identity, "type": "CREATE_ITEM",
            "item_id": identity, "kind": "story", "title": title, "identity_scope": {"scene": identity},
            "observation_ids": [observation["observation_id"]]}])
        if publish:
            self.publish_item(identity)
        return span

    def publish_item(self, identity):
        snapshot = CanonicalReadingAdapter(self.store).snapshot(identity)
        plan = BodyPlan(identity, snapshot.content_revision, tuple(
            BodyBlock("home-" + unit.id, unit.role, unit.text, (unit.id,), unit.evidence_ids,
                      unit.resource_ids, identity) for unit in snapshot.units))
        return self.publisher.publish_item(snapshot, plan)

    def append(self, identity="cat", *, publish=True):
        observation, _ = self.canonical_extract(self.store, "late-" + identity, "后来猫又移到了桌边。")
        self.canonical_actions(self.store, [{"action_key": "append-" + identity, "type": "MERGE_INTO_ITEM",
            "item_id": identity, "observation_ids": [observation["observation_id"]],
            "modes": ["append_detail", "add_evidence"], "semantic_effect": "detail_added"}])
        if publish:
            self.publish_item(identity)

    def opened(self, revision=1, *, origin="obsidian_visible_open"):
        self.controls.observe_open(OpenObservation("opened-" + str(revision) + origin, "cat", revision,
                                                   self.NOW.isoformat(), origin, True))

    def text(self, home=None):
        receipt = (home or self.home).publish(now=self.NOW)
        return receipt, self.publisher.read(receipt.path).decode()

    def test_opened_v1_does_not_consume_v2_and_known_is_independent(self):
        self.item()
        first, _ = self.text()
        self.opened()
        opened, text = self.text()
        self.assertGreater(opened.content_revision, first.content_revision)
        self.assertIn("最新 v1；已打开至 v1；尚未确认已知", text)
        self.assertEqual(self.controls.preference("cat").known_revision, 0)
        self.append()
        _, text = self.text()
        self.assertIn("最新 v2；已打开至 v1；尚未确认已知；有更新", text)
        self.assertIn("## 有更新 · 1 条", text)
        self.controls.mark_known("cat", 1, known_at=self.NOW.isoformat())
        _, text = self.text()
        self.assertIn("最新 v2；已打开至 v1；明确已知至 v1；有更新", text)
        self.controls.mark_known("cat", 2, known_at=self.NOW.isoformat())
        known, text = self.text()
        self.assertIn("最新 v2；已打开至 v1；明确已知至 v2", text)
        self.assertIn("## 有更新 · 0 条", text)
        self.assertGreater(known.content_revision, opened.content_revision)

    def test_no_plugin_and_explicit_known_remain_useful_without_fabricated_open(self):
        self.item()
        self.controls.mark_known("cat", 1, known_at=self.NOW.isoformat())
        _, text = self.text()
        self.assertIn("暂无可见打开记录；明确已知至 v1", text)
        self.assertIn("仍可在阅读管理中确认已知", text)
        self.assertEqual(self.ledger.all("SELECT * FROM reader_open_observations"), [])
        self.opened(origin="user_confirmed_open")
        _, text = self.text()
        self.assertIn("暂无可见打开记录；明确已知至 v1", text)

    def test_ready_navigation_views_and_renames_follow_identity(self):
        self.item()
        self.controls.initialize_query()
        self.controls.refresh_management()
        self.publisher.publish_document(object_id="control:reader-status", path="阅读状态.md",
                                        title="阅读状态", body="尚无等待。", content_revision=1)
        self.store.create_view("life", "猫的日常", "完整日常")
        self.publisher.publish_view("life", "猫的日常", [("cat", "", "窗边的猫")], revision=1)
        # Runtime uses definition_revision + override_revision, initially 1.
        first, text = self.text()
        for path, label in (("检索.md", "检索"), ("管理.md", "阅读管理"), ("阅读状态.md", "阅读状态")):
            self.assertIn(wikilink("WGO/" + path, label), text)
        self.assertIn("|猫的日常]]", text)
        old_path = self.publisher.resolve("cat")
        renamed = "条目/我的猫.md"
        (self.publisher.root / old_path).rename(self.publisher.root / renamed)
        updated, text = self.text()
        self.assertIn(wikilink("WGO/" + renamed, "窗边的猫"), text)
        self.assertNotIn(wikilink("WGO/" + old_path, "窗边的猫"), text)
        self.assertGreater(updated.content_revision, first.content_revision)
        own_path = self.publisher.root / updated.path
        own_path.rename(self.publisher.root / "开始阅读.md")
        same, _ = self.text()
        self.assertEqual(same.path, "开始阅读.md")
        self.assertFalse(own_path.exists())

    def test_unready_controls_items_views_and_old_item_projection_never_link(self):
        self.item(publish=False)
        self.store.create_view("pending", "待形成的主题", "尚未发布")
        _, text = self.text()
        self.assertNotIn("[[", text)
        self.assertIn("窗边的猫（阅读页待就绪）", text)
        self.assertIn("待形成的主题（阅读页待就绪）", text)
        self.publish_item("cat")
        self.append(publish=False)
        _, text = self.text()
        self.assertIn("最新 v2", text)
        self.assertNotIn("[[WGO/条目/", text)

    def test_timezone_selects_today_success_and_retains_last_success_when_deferred(self):
        span = self.item()
        change = BriefChange("change-cat", "cat", 1, "猫的日常", "", "猫躺在窗边的纸箱里。",
                             (span,), "2026-10-13", "2026-10-13")
        response = {"status": "success", "title": "窗边的一天", "paragraphs": [{
            "text": "猫躺在纸箱里。", "change_ids": [change.id],
            "evidence_refs": [{"item_id": "cat", "evidence_id": span}], "scopes": [], "corrections": []}]}
        service = BriefingService(self.ledger, self.publisher, self.controls, projection_fixtures.ScriptedTextProvider(response),
                                  timezone="Asia/Singapore")
        brief = service.build((change,), now=self.NOW)
        self.assertEqual(brief.status, "success")
        _, text = self.text()
        self.assertIn("今天的简报 · 2026-10-14", text)
        self.assertIn(wikilink("WGO/" + brief.path, "阅读今天的简报"), text)
        utc_home = ReadingHome(self.store, self.publisher, self.controls, config={"daily_digest_timezone": "UTC"})
        _, utc_text = self.text(utc_home)
        self.assertIn("今天的简报 · 2026-10-13", utc_text)
        self.assertNotIn("[[WGO/简报/", utc_text)
        with self.ledger.transaction() as conn:
            conn.execute("UPDATE reader_brief_editions SET status='deferred'")
        _, text = self.text()
        self.assertIn("阅读今天最近成功的一版简报", text)
        (self.publisher.root / brief.path).unlink()
        _, text = self.text()
        self.assertNotIn("[[WGO/简报/", text)
        self.assertIn("阅读页待就绪", text)

    def test_unchanged_manual_conflict_and_no_other_state_writes(self):
        self.item()
        self.controls.set_preference("cat", expected_revision=0, follow=True, wait="等猫再次回到窗边")
        before = {table: [tuple(row) for row in self.ledger.all("SELECT * FROM " + table)] for table in
                  ("reader_preferences", "reader_known", "reader_open_observations", "projection_outbox", "index_outbox")}
        first, text = self.text()
        self.assertIn("## 关注与等待 · 1 条", text)
        self.assertIn("已关注", text)
        self.assertIn("等待：等猫再次回到窗边", text)
        path = self.publisher.root / first.path
        stamp = path.stat().st_mtime_ns
        again, _ = self.text()
        self.assertEqual((again.status, again.content_revision), ("unchanged", first.content_revision))
        self.assertEqual(stamp, path.stat().st_mtime_ns)
        path.write_text(text.replace("从这里继续阅读", "我的个人阅读安排"))
        personal = path.read_bytes()
        conflict, _ = self.text()
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(path.read_bytes(), personal)
        proposal = self.publisher.pending_proposals()[0]
        self.assertEqual(self.publisher.read(proposal["backup_path"]), personal)
        after = {table: [tuple(row) for row in self.ledger.all("SELECT * FROM " + table)] for table in before}
        self.assertEqual(after, before)

    def test_bounded_lists_metadata_only_and_safe_display_labels(self):
        title = "标题|[选项]<!-- wgo:begin body --> https://example.com/?token=private " + "长" * 1000
        for index in range(5):
            self.item("item-" + str(index), title=title + str(index), publish=False)
        home = ReadingHome(self.store, self.publisher, self.controls, limit=2)
        with patch.object(self.store, "list_items", side_effect=AssertionError("unbounded hydration")), \
                patch.object(self.store, "get_item", side_effect=AssertionError("Item payload hydration")), \
                patch.object(self.publisher, "load_plan", side_effect=AssertionError("BodyPlan hydration")):
            _, text = self.text(home)
            metadata = home._metadata("2026-10-14")
        self.assertTrue(all(len(section["rows"]) <= 2 for section in metadata["sections"].values()))
        self.assertEqual(metadata["sections"]["recent"]["total"], 5)
        self.assertIn("另有 3 条", text)
        self.assertNotIn("token=private", text)
        self.assertNotIn("<!-- wgo:begin body -->", region_body(text, "body"))
        self.assertLess(len(text), 6000)
        with closing(self.store.connect()) as conn:
            self.assertGreater(conn.execute("SELECT length(title) FROM items LIMIT 1").fetchone()[0], 1000)

    def test_unreadable_targets_are_pending_and_wait_preview_is_explicitly_bounded(self):
        self.item()
        wait = "等待这个完整条件成立。" * 100
        self.controls.set_preference("cat", expected_revision=0, wait=wait)
        with patch.object(self.publisher, "ready", side_effect=PermissionError("synthetic unreadable target")):
            _, text = self.text()
        self.assertNotIn("[[", text)
        self.assertIn("完整条件见条目属性", text)
        self.assertNotIn(wait, text)
        self.assertEqual(self.controls.preference("cat").wait, wait)

    def test_long_userinfo_urls_cannot_leak_from_item_view_or_wait_previews(self):
        secret = "SYNTHETICSECRET"
        unsafe = "https://" + secret * 20 + "@example.invalid/path"
        self.assertNotIn(secret, redact_urls_in_text(unsafe))
        self.item(title=unsafe)
        self.store.create_view("long-title", unsafe, "独立主题")
        self.publisher.publish_view("long-title", unsafe, [("cat", "", "窗边的猫")], revision=1)
        self.controls.set_preference("cat", expected_revision=0, follow=True, wait="等待完整条件")
        # Retained canonical metadata may predate preference redaction. The
        # home must not trust its first N characters as a complete safe URL.
        with self.ledger.transaction() as conn:
            conn.execute("UPDATE reader_preferences SET wait_condition=? WHERE item_id='cat'", (unsafe,))
        metadata = self.home._metadata("2026-10-14")
        self.assertNotIn(secret, repr(metadata))
        _, text = self.text()
        self.assertNotIn(secret, text)
        self.assertIn("最新 v1", text)
        self.assertIn("## 关注与等待 · 1 条", text)
        self.assertIn("完整条件见条目属性", text)
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT title FROM items WHERE item_id='cat'").fetchone()[0], unsafe)
            self.assertEqual(conn.execute("SELECT title FROM views WHERE view_id='long-title'").fetchone()[0], unsafe)
            self.assertEqual(conn.execute("SELECT wait_condition FROM reader_preferences WHERE item_id='cat'").fetchone()[0], unsafe)

    def test_special_filename_rename_is_not_misrepresented_as_a_working_wikilink(self):
        self.item()
        original = self.publisher.resolve("cat")
        special = "条目/猫#观察.md"
        (self.publisher.root / original).rename(self.publisher.root / special)
        self.assertTrue(self.publisher.ready("cat", 1))
        self.assertEqual(self.publisher.resolve("cat"), special)
        _, text = self.text()
        self.assertNotIn("[[WGO/条目/猫#观察", text)
        self.assertIn("文件名暂无法安全跳转", text)
        self.assertIn("最新 v1；暂无可见打开记录；尚未确认已知", text)
        normal = "条目/猫的观察.md"
        (self.publisher.root / special).rename(self.publisher.root / normal)
        _, text = self.text()
        self.assertIn(wikilink("WGO/" + normal, "窗边的猫"), text)
        self.assertNotIn("文件名暂无法安全跳转", text)

    def test_complete_short_labels_preserve_meaning_and_use_canonical_redaction(self):
        title = "纸箱观察 https://reader:password@example.invalid/path"
        wait = "等再次观察；https://user:password@example.invalid/result"
        self.item(title=title)
        self.controls.set_preference("cat", expected_revision=0, wait=wait)
        self.store.create_view("ordinary", "猫的日常 https://user:password@example.invalid/notes", "完整经历")
        self.publisher.publish_view("ordinary", "猫的日常", [("cat", "", "窗边的猫")], revision=1)
        _, text = self.text()
        self.assertIn("纸箱观察", text)
        self.assertIn("等再次观察", text)
        self.assertIn("猫的日常", text)
        self.assertIn("example.invalid/path", text)
        self.assertNotIn("password", text)

    def test_changed_reading_facts_or_target_during_preparation_do_not_publish_stale_home(self):
        self.item()
        first, _ = self.text()
        original = self.publisher.read(first.path)
        publish = self.publisher.publish_document

        def changed_facts(**kwargs):
            self.controls.mark_known("cat", 1, known_at=self.NOW.isoformat())
            return publish(**kwargs)

        with patch.object(self.publisher, "publish_document", side_effect=changed_facts):
            with self.assertRaisesRegex(ReadingError, "stale_projection_snapshot"):
                self.text()
        self.assertEqual(self.publisher.read(first.path), original)

        def missing_target(**kwargs):
            path = self.publisher.resolve("cat")
            (self.publisher.root / path).unlink()
            return publish(**kwargs)

        with patch.object(self.publisher, "publish_document", side_effect=missing_target):
            with self.assertRaisesRegex(ReadingError, "stale_projection_snapshot"):
                self.text()
        self.assertEqual(self.publisher.read(first.path), original)

    def test_api_rejects_bad_limit_naive_time_and_other_generation(self):
        for limit in (0, True, 51):
            with self.assertRaisesRegex(ReadingError, "invalid_reading_home_limit"):
                ReadingHome(self.store, self.publisher, self.controls, limit=limit)
        with self.assertRaisesRegex(ReadingError, "requires_aware_time"):
            self.home.publish(now=datetime(2026, 10, 13))
        with self.assertRaisesRegex(ReadingError, "generation_mismatch"):
            ReadingHome(SimpleNamespace(generation_id="other"), self.publisher, self.controls)
        runtime = SimpleNamespace(store=self.store, publisher=self.publisher, controls=self.controls, config={})
        self.assertIs(ReadingHome.for_runtime(runtime).store, self.store)


if __name__ == "__main__":
    unittest.main()
