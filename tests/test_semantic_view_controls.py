"""Explicit View editing through real temporary reader files and canonical CAS."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from core.semantic.controls import ReaderControls, SearchAnswer
from core.semantic.contracts import SemanticStoreError
from core.semantic.projections import (BodyBlock, BodyPlan, CanonicalReadingAdapter, ProjectionPublisher,
                                       ReadingError, patch_properties, property_value, region_body)
from core.semantic.store import SemanticStore
from core.semantic.view_controls import ViewControls
from tests import test_semantic_projections as fixtures


class ViewControlTests(unittest.TestCase):
    canonical_extract = fixtures.ReadingHarness.canonical_extract
    canonical_actions = fixtures.ReadingHarness.canonical_actions

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = SemanticStore(self.root / "knowledge.sqlite", "view-controls")
        self.publisher = ProjectionPublisher.for_store(self.store, self.root / "WGO")
        self.controls = ViewControls(self.store, self.publisher)
        self.items = ReaderControls(self.publisher.ledger, self.publisher, search=lambda _: SearchAnswer("", ()))
        for identity, title in (("cat", "窗边的猫"), ("flower", "一束花")):
            obs, _ = self.canonical_extract(self.store, identity, title + "的一次观察。")
            self.canonical_actions(self.store, [{"action_key": "create-" + identity, "type": "CREATE_ITEM",
                "item_id": identity, "kind": "story", "title": title, "identity_scope": {"scene": identity},
                "observation_ids": [obs["observation_id"]]}])
            snapshot = CanonicalReadingAdapter(self.store).snapshot(identity)
            plan = BodyPlan(identity, 1, tuple(BodyBlock("home-" + u.id, u.role, u.text,
                (u.id,), u.evidence_ids, u.resource_ids, identity) for u in snapshot.units))
            self.publisher.publish_item(snapshot, plan)
        self.store.create_view("daily", "生活记录", "保留生活场景", origin="model")
        # Initial catalog fixture; user mutations below always go through the store API.
        with self.store.transaction() as conn:
            for identity in ("cat", "flower"):
                conn.execute("INSERT INTO view_memberships VALUES(?,?,?,?,?,?,?)",
                             ("daily", identity, "primary", "model", "生活记录", 0, 0))
        self.publish_view()

    def publish_view(self):
        view = self.store.get_view("daily")
        with closing(self.store.connect()) as conn:
            entries = [(r[0], "", r[1]) for r in conn.execute("SELECT i.item_id,i.title FROM items i "
                "JOIN view_memberships m USING(item_id) WHERE m.view_id='daily' AND m.excluded=0")]
        return self.publisher.publish_view("daily", view["title"], entries,
            revision=view["definition_revision"] + view["override_revision"])

    def registered(self, kind, item=None):
        rows = self.controls.ledger.all("SELECT * FROM reader_actions WHERE status='registered' AND kind=?", (kind,))
        return next(row for row in reversed(rows) if item is None or json.loads(row["value_json"]).get("item_id") == item)

    def check(self, *actions):
        path = self.publisher.root / self.publisher.resolve(self.controls.MANAGEMENT_ID)
        text = path.read_text()
        for action in actions:
            text, count = re.subn(r"(?m)^- \[ \](.*<!-- wgo-action:" + action["action_id"] + r" -->)$", r"- [x]\1", text)
            self.assertEqual(count, 1)
        path.write_text(text)

    def test_explicit_description_pin_and_exclusion_commit_as_one_batch(self):
        before = {i["item_id"]: i["content_revision"] for i in self.store.list_items()}
        self.controls.refresh_management()
        path = self.publisher.root / self.publisher.resolve("view:daily")
        path.write_text(patch_properties(path.read_text(), {"主题说明": "关于家里生活的观察"}, machine_only=False))
        self.controls.refresh_management()
        self.assertEqual(self.store.get_view("daily")["definition"], "保留生活场景")
        actions = [self.registered("view_definition"), self.registered("view_pin", "cat"), self.registered("view_exclude", "flower")]
        self.check(*actions)
        receipts = self.controls.process_management()
        self.assertTrue(all(r.status == "applied" for r in receipts), receipts)
        view = self.store.get_view("daily")
        self.assertEqual((view["definition"], view["definition_revision"], view["override_revision"]),
                         ("关于家里生活的观察", 2, 1))
        with closing(self.store.connect()) as conn:
            members = {r[0]: tuple(r)[1:] for r in conn.execute("SELECT item_id,pinned,excluded FROM view_memberships")}
        self.assertEqual(members, {"cat": (1, 0), "flower": (0, 1)})
        self.assertEqual({i["item_id"]: i["content_revision"] for i in self.store.list_items()}, before)
        self.assertEqual(self.controls.process_management()[0].status, "already_consumed")
        self.controls.refresh_management()
        result_page = self.publisher.read(self.publisher.resolve(self.controls.MANAGEMENT_ID)).decode()
        self.assertIn("最近操作结果", result_page)
        self.assertIn("已应用：固定", result_page)

    def test_stale_view_rejects_every_checked_action_without_partial_changes(self):
        self.controls.refresh_management()
        actions = [self.registered("view_pin", "cat"), self.registered("view_exclude", "flower")]
        self.check(*actions)
        self.store.update_view("daily", "另一项明确修改", 1)
        receipts = self.controls.process_management()
        self.assertTrue(all(r.status == "rejected" for r in receipts))
        self.controls.refresh_management()
        self.assertIn("未应用，请检查最新目录后重新勾选",
            self.publisher.read(self.publisher.resolve(self.controls.MANAGEMENT_ID)).decode())
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT SUM(pinned)+SUM(excluded) FROM view_memberships").fetchone()[0], 0)

    def test_action_receipts_and_canonical_changes_rollback_together(self):
        self.controls.refresh_management()
        action = self.registered("view_pin", "cat")
        self.check(action)
        original = self.store._update_view_controls
        def fail(conn, *args, **kwargs):
            original(conn, *args, **kwargs)
            raise SemanticStoreError("synthetic_after_view_write")
        with patch.object(self.store, "_update_view_controls", side_effect=fail):
            result = self.controls.process_management()
        self.assertEqual(result[0].status, "rejected")
        self.assertEqual(self.store.get_view("daily")["override_revision"], 0)
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT pinned FROM view_memberships WHERE item_id='cat'").fetchone()[0], 0)

    def test_view_and_item_management_cannot_consume_each_others_actions(self):
        self.controls.refresh_management()
        self.items.register_item_actions("cat", 1)
        self.items.refresh_management()
        item_page = self.publisher.read(self.publisher.resolve(self.items.MANAGEMENT_ID)).decode()
        view_page = self.publisher.read(self.publisher.resolve(self.controls.MANAGEMENT_ID)).decode()
        self.assertNotIn("固定：", item_page)
        self.assertNotIn("知道了这个版本", view_page)
        self.check(self.registered("view_pin", "cat"))
        self.assertEqual(self.items.process_management(acted_at="2026-10-25T12:00:00+08:00"), [])
        self.assertEqual(self.controls.process_management()[0].status, "applied")

    def test_two_consumers_keep_the_first_applied_receipt(self):
        self.controls.refresh_management()
        action = self.registered("view_pin", "cat")
        self.check(action)
        barrier = threading.Barrier(2)
        validate = self.controls._validated_management_checks
        def together():
            value = validate()
            barrier.wait(timeout=10)
            return value
        with patch.object(self.controls, "_validated_management_checks", side_effect=together), \
             ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.controls.process_management) for _ in range(2)]
            states = [future.result()[0].status for future in futures]
        self.assertCountEqual(states, ["applied", "already_consumed"])
        self.assertEqual(self.controls.ledger.one(
            "SELECT status FROM reader_actions WHERE action_id=?", (action["action_id"],))[0], "applied")

    def test_checked_intent_survives_refresh_and_normal_view_rename(self):
        self.controls.refresh_management()
        action = self.registered("view_pin", "cat")
        self.check(action)
        old = self.publisher.root / self.publisher.resolve("view:daily")
        old.rename(old.parent / "家里的记录.md")
        self.controls.refresh_management()
        self.assertEqual(self.controls.process_management()[0].status, "applied")

    def test_field_collision_and_unregistered_checkbox_fail_closed(self):
        path = self.publisher.root / self.publisher.resolve("view:daily")
        text = patch_properties(path.read_text(), {"主题说明": "已有个人字段"}, machine_only=False)
        path.write_text(text)
        self.store.create_view("other", "另一个目录", "另一个说明", origin="model")
        self.publisher.publish_view("other", "另一个目录", [], revision=1)
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO view_memberships VALUES(?,?,?,?,?,?,?)",
                         ("other", "cat", "primary", "model", "另一分类", 0, 0))
        receipt = self.controls.refresh_management()
        self.assertEqual(receipt.status, "published")
        self.assertEqual(path.read_text(), text)
        self.assertEqual(json.loads(self.registered("view_pin")["value_json"])["view_id"], "other")
        management = self.publisher.root / self.publisher.resolve(self.controls.MANAGEMENT_ID)
        text = management.read_text().replace("在目录页编辑", "- [x] 任意修改 <!-- wgo-action:" + "a" * 32 + " -->\n在目录页编辑")
        management.write_text(text)
        with self.assertRaisesRegex(ReadingError, "unregistered_management_edit"):
            self.controls.process_management()

    def test_default_definition_is_redacted_without_registering_false_user_intent(self):
        url = "https://reader:SYNTHETICSECRET@example.invalid/?token=SYNTHETICSECRET"
        self.store.update_view("daily", url, 1)
        self.publish_view()
        self.controls.refresh_management()
        raw = self.publisher.read(self.publisher.resolve("view:daily")).decode()
        self.assertNotIn("SYNTHETICSECRET", raw)
        self.assertEqual(self.store.get_view("daily")["definition"], url)
        self.assertEqual(self.controls.ledger.one(
            "SELECT COUNT(*) FROM reader_actions WHERE kind='view_definition'")[0], 0)

    def test_catalog_bounds_fields_before_hydrating_and_still_manages_members(self):
        with self.store.transaction() as conn:
            conn.execute("UPDATE views SET title=?,definition=? WHERE view_id='daily'",
                         ("长" * 1000000, "长" * 2000000))
        _total, views = self.controls._views("", 1)
        self.assertEqual(views[0]["title"], "标题较长的目录")
        self.assertIsNone(views[0]["definition"])
        self.controls.refresh_management(limit=1)
        self.assertEqual(self.registered("view_pin", "cat")["status"], "registered")

    def test_explicit_selection_reaches_members_outside_default_window(self):
        self.controls.refresh_management(member_limit=1)
        first_actions = [row[0] for row in self.controls.ledger.all(
            "SELECT action_id FROM reader_actions WHERE status='registered'")]
        path = self.publisher.root / self.publisher.resolve(self.controls.MANAGEMENT_ID)
        member = self.publisher.resolve("cat").removesuffix(".md")
        view = self.publisher.resolve("view:daily").removesuffix(".md")
        path.write_text(patch_properties(path.read_text(), {"目录": "[[WGO/" + view + "]]",
            "条目": "[[WGO/" + member + "]]"}, machine_only=False))
        self.controls.refresh_management(member_limit=1)
        page = path.read_text()
        for identity in first_actions:
            self.assertNotIn(identity, page)
        self.assertEqual(self.controls.ledger.one(
            "SELECT COUNT(*) FROM reader_actions WHERE status='registered'")[0], 2)
        self.check(self.registered("view_pin", "cat"))
        self.assertEqual(self.controls.process_management()[0].status, "applied")

    def test_unsafe_renamed_view_has_an_honest_label_without_breaking_management(self):
        self.controls.refresh_management()
        old = self.publisher.root / self.publisher.resolve("view:daily")
        old.rename(old.parent / "目录[草稿].md")
        self.controls.refresh_management()
        raw = self.publisher.read(self.publisher.resolve(self.controls.MANAGEMENT_ID)).decode()
        self.assertIn("文件名暂无法安全跳转", raw)
        self.check(self.registered("view_pin", "cat"))
        self.assertEqual(self.controls.process_management()[0].status, "applied")
