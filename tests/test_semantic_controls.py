from dataclasses import replace
import re

from core.semantic.controls import OpenObservation, QueryHit, ReaderControls, SearchAnswer
from core.semantic.projections import ReadingError, patch_properties
from tests.test_semantic_projections import ReadingHarness


class ControlsTests(ReadingHarness):
    def setUp(self):
        super().setUp()
        self.searches = []
        self.controls = ReaderControls(self.ledger, self.publisher, search=self.search)

    def search(self, request):
        self.searches.append(request)
        return SearchAnswer("猫先住箱子，后来坐在窗边。", (QueryHit("box-cat", self.revisions["box-cat"], "箱猫"),))

    def set_query(self, value):
        path = self.publisher.root / "检索.md"
        path.write_text(patch_properties(path.read_text(), {"查询": value}, machine_only=False))

    def check(self, action_id):
        path = self.publisher.root / "管理.md"
        text = path.read_text()
        text = re.sub(r"^- \[ \](.*<!-- wgo-action:" + action_id + r" -->)$", r"- [x]\1", text, flags=re.M)
        path.write_text(text)

    def test_query_callback_answer_receipt_history_and_no_loop(self):
        self.publish()
        self.controls.initialize_query()
        self.set_query("猫最近怎样？")
        result = self.controls.run_query()
        self.assertEqual(result.status, "success")
        self.assertIn("[[WGO/条目/box-cat|箱猫]]", (self.publisher.root / "检索.md").read_text())
        self.assertEqual(self.controls.run_query().status, "unchanged")
        self.assertEqual(len(self.searches), 1)
        self.set_query("它住哪里？")
        self.assertEqual(self.controls.run_query().revision, 2)
        history = self.controls.query_history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["query_text"], "猫最近怎样？")
        self.assertIn("后来坐在窗边", history[0]["answer_text"])

    def test_query_changed_during_callback_does_not_overwrite_current_page(self):
        self.publish()
        self.controls.initialize_query()
        self.set_query("猫怎样？")
        def changed(request):
            self.set_query("现在改问窗边。")
            return self.search(request)
        self.controls.search = changed
        result = self.controls.run_query()
        self.assertEqual(result.status, "deferred")
        self.assertEqual(result.code, "stale_projection_snapshot")
        self.assertIn("现在改问窗边。", (self.publisher.root / "检索.md").read_text())

    def test_query_conflict_action_is_stale_after_property_changes(self):
        self.publish()
        self.controls.initialize_query()
        self.set_query("猫怎样？")
        self.controls.run_query()
        path = self.publisher.root / "检索.md"
        path.write_text(path.read_text().replace("猫先住箱子", "我的答案改写"))
        self.set_query("后来猫在哪里？")
        self.assertEqual(self.controls.run_query().status, "review")
        proposal = next(row for row in self.publisher.pending_proposals() if row["object_id"] == "control:query")
        self.set_query("另一个问题")
        self.assertFalse(self.publisher.proposal_is_current(proposal["proposal_id"]))

    def test_property_mapping_does_not_take_existing_user_field(self):
        result = self.publish()
        path = self.publisher.root / result.path
        path.write_text(patch_properties(path.read_text(), {"关注": "个人用途"}, machine_only=False))
        with self.assertRaisesRegex(ReadingError, "control_property_collision"):
            self.controls.register_property("box-cat", "follow", "关注", initial=False)
        path.write_text(patch_properties(path.read_text(), {"已有空字段": None}, machine_only=False))
        with self.assertRaisesRegex(ReadingError, "control_property_collision"):
            self.controls.register_property("box-cat", "follow", "已有空字段", initial=False)
        self.controls.register_property("box-cat", "follow", "阅读关注", initial=False)
        self.controls.register_property("box-cat", "mute", "阅读静音", initial=False)
        self.controls.register_property("box-cat", "wait", "阅读等待", initial="")
        path.write_text(patch_properties(path.read_text(), {"阅读关注": True, "阅读静音": True, "阅读等待": "猫回窗边"}, machine_only=False))
        pref = self.controls.sync_properties("box-cat")
        self.assertTrue(pref.follow)
        self.assertTrue(pref.mute)
        self.assertEqual(pref.wait, "猫回窗边")
        self.assertIn("个人用途", path.read_text())

    def test_only_registered_management_checkbox_executes(self):
        result = self.publish()
        self.controls.register_item_actions("box-cat", 1)
        self.controls.refresh_management()
        action = self.ledger.one("SELECT * FROM reader_actions WHERE kind='known'")
        # A copied actionable-looking checkbox in source text is inert.
        source = self.publisher.root / "来源/box-cat-0001.md"
        source.write_text(source.read_text() + f"\n- [x] 知道了 <!-- wgo-action:{action['action_id']} -->\n")
        self.assertEqual(self.controls.process_management(acted_at="now"), [])
        self.assertEqual(self.controls.preference("box-cat").known_revision, 0)
        self.check(action["action_id"])
        receipts = self.controls.process_management(acted_at="now")
        self.assertEqual(receipts[0].status, "applied")
        self.assertEqual(self.controls.preference("box-cat").known_revision, 1)
        self.assertEqual(self.controls.process_management(acted_at="now"), [])

    def test_injected_management_line_rejected(self):
        self.publish()
        self.controls.register_item_actions("box-cat", 1)
        self.controls.refresh_management()
        path = self.publisher.root / "管理.md"
        path.write_text(path.read_text().replace("<!-- wgo:end actions -->",
                        "- [x] 偷来的命令 <!-- wgo-action:" + "a" * 32 + " -->\n<!-- wgo:end actions -->"))
        with self.assertRaisesRegex(ReadingError, "unregistered_management_edit"):
            self.controls.process_management(acted_at="now")
        self.assertEqual(self.controls.preference("box-cat").known_revision, 0)

    def test_publication_before_poll_preserves_checked_action_until_consumed(self):
        self.publish()
        self.controls.register_item_actions("box-cat", 1)
        self.controls.refresh_management()
        action = self.ledger.one("SELECT * FROM reader_actions WHERE kind='known'")
        self.check(action["action_id"])
        self.publish()  # A source-triggered publication may precede reader.poll.
        self.controls.register_item_actions("box-cat", 1)
        self.controls.refresh_management()
        self.controls.refresh_management()
        self.assertIn(action["action_id"], self.controls._validated_management_checks()[1])
        self.assertEqual(self.controls.preference("box-cat").known_revision, 0)
        self.assertEqual(self.controls.process_management(acted_at="now")[0].status, "applied")
        self.assertEqual(self.controls.process_management(acted_at="later"), [])
        self.assertEqual(self.controls.preference("box-cat").known_revision, 1)

    def test_refresh_preserves_stale_checked_action_for_explicit_rejection(self):
        snapshot = self.snapshot()
        self.publish(snapshot)
        self.controls.register_item_actions("box-cat", 1)
        self.controls.refresh_management()
        action = self.ledger.one("SELECT * FROM reader_actions WHERE kind='known'")
        self.check(action["action_id"])
        self.revisions["box-cat"] = 2
        self.publish(replace(snapshot, content_revision=2))
        self.controls.register_item_actions("box-cat", 2)
        self.controls.refresh_management()
        self.assertIn(action["action_id"], self.controls._validated_management_checks()[1])
        registered = self.ledger.all("SELECT * FROM reader_actions WHERE status='registered'")
        self.assertEqual([row["action_id"] for row in registered if row["content_revision"] == 1], [action["action_id"]])
        result = self.controls.process_management(acted_at="now")
        self.assertEqual((result[0].status, result[0].code), ("rejected", "stale_control_action"))
        self.assertEqual(self.controls.preference("box-cat").known_revision, 0)

    def test_stale_action_rejected_and_known_cannot_consume_newer(self):
        snapshot = self.snapshot()
        self.publish(snapshot)
        self.controls.register_item_actions("box-cat", 1)
        self.controls.refresh_management()
        action = self.ledger.one("SELECT * FROM reader_actions WHERE kind='known'")
        newer = replace(snapshot, content_revision=2)
        self.revisions["box-cat"] = 2
        self.publish(newer)
        self.check(action["action_id"])
        self.assertEqual(self.controls.process_management(acted_at="now")[0].status, "rejected")
        self.controls.mark_known("box-cat", 1, known_at="now")
        self.assertEqual(self.controls.preference("box-cat").known_revision, 1)
        self.assertLess(self.controls.preference("box-cat").known_revision, self.revisions["box-cat"])

    def test_real_conflict_offers_three_actions_and_can_adopt(self):
        result = self.publish()
        path = self.publisher.root / result.path
        path.write_text(path.read_text().replace("周五，她把箱子让给了猫。", "我的改写"))
        self.publish()
        self.controls.refresh_management()
        actions = self.ledger.all("SELECT * FROM reader_actions")
        self.assertEqual({row["kind"] for row in actions}, {"adopt", "move_notes", "keep_personal"})
        action = next(row for row in actions if row["kind"] == "adopt")
        self.check(action["action_id"])
        self.assertEqual(self.controls.process_management(acted_at="now")[0].status, "applied")
        self.assertTrue(self.publisher.ready("box-cat", 1))

    def test_open_requires_visible_observation_and_does_not_mark_known(self):
        self.publish()
        for origin in ("mtime", "os_accepted", "notification_click"):
            with self.assertRaisesRegex(ReadingError, "unverified_open_observation"):
                self.controls.observe_open(OpenObservation("open", "box-cat", 1, "now", origin, True))
        receipt = self.controls.observe_open(OpenObservation("open", "box-cat", 1, "now", "obsidian_visible_open", True))
        self.assertEqual(receipt.status, "observed")
        self.assertEqual(self.controls.preference("box-cat").known_revision, 0)
        with self.assertRaisesRegex(ReadingError, "open_observation_collision"):
            self.controls.observe_open(OpenObservation("open", "box-cat", 1, "later", "obsidian_visible_open", True))
