"""Synthetic old-schema/page conservation; no source/provider/runtime access."""
import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core.semantic.legacy import (
    LegacyError, LegacyInventory, build_publication_plan, iter_legacy_inputs,
    plan_legacy, import_legacy, legacy_resources, SOURCE_FIRST_POLICY,
    preserve_legacy_history, build_history_publication_plan, record_legacy_backups,
    publish_legacy_navigation,
)
from core.semantic.store import SemanticStore


class LegacyFixture:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.db = self.root / "old.sqlite"
        self.vault = self.root / "vault"
        self.tree = self.vault / "Old"
        self.tree.mkdir(parents=True)
        self.page_bytes = (
            '---\r\ntitle: 我的生活笔记\r\naliases:\r\n  - 雨天\r\n'
            'my_custom: "leave: exactly"\r\n---\r\n# 周末\r\n'
            '个人前言，没有机器区域。\r\n\r\n散步记忆 ^memory\r\n'
            '[[Old/second#细节|原有别名]]\r\n'
            '[资料](https://example.test/view?token=private&x=1)\r\n'
            '最后手写的一行\r\n'
        ).encode()
        (self.tree / "life.md").write_bytes(self.page_bytes)
        (self.tree / "second.md").write_text("# 细节\n摘要页\n", encoding="utf-8")
        (self.tree / "foreign.md").write_text("# we-groupchat-obsidian\n不是机器所有。\n", encoding="utf-8")
        conn = sqlite3.connect(self.db)
        conn.executescript("""
          CREATE TABLE topics(topic_id INTEGER PRIMARY KEY,title TEXT,summary TEXT,
            obsidian_path TEXT,source_chat_username TEXT,links_json TEXT,files_json TEXT);
          CREATE TABLE events(event_id INTEGER PRIMARY KEY,topic_id INTEGER,
            summary TEXT,messages_excerpt TEXT,source_message_id TEXT,raw_text TEXT,
            source_batch_id TEXT,created_at REAL);
          CREATE TABLE relations(relation_id INTEGER PRIMARY KEY,source_topic_id INTEGER,
            target_topic_id INTEGER,relation TEXT,reason TEXT);
          CREATE TABLE attachment_mentions(mention_id INTEGER PRIMARY KEY,event_id INTEGER,
            topic_id INTEGER,source_message_id TEXT,resource_index INTEGER,original_name TEXT);
          CREATE TABLE attachment_objects(sha256 TEXT PRIMARY KEY,object_relpath TEXT,size INTEGER);
        """)
        conn.executemany("INSERT INTO topics VALUES(?,?,?,?,?,?,?)", [
            (1, "生活", "星期天下雨", "Old/life.md", "old-chat", '["https://example.test/?token=raw"]', '["记忆.pdf"]'),
            (2, "只有摘要", "没有可证明的原文", "Old/second.md", "other-chat", "[]", "[]"),
        ])
        conn.executemany("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)", [
            (1, 1, "摘要", "只留下的节选", "", None, "old-batch", 1),
            (2, 1, "first A", "", "exact-id", "A", "", 2),
            (3, 1, "B", "", "exact-id", "B", "", 3),
            (4, 1, "returned A", "", "exact-id", "A", "", 4),
            (5, 2, "没有原文的旧摘要", "", "", None, "", 5),
        ])
        conn.execute("INSERT INTO relations VALUES(1,1,2,'related','保留这段理由')")
        conn.execute("INSERT INTO attachment_mentions VALUES(1,2,1,'exact-id',0,'记忆.pdf')")
        conn.execute("INSERT INTO attachment_objects VALUES('object-id','cas/path',12)")
        conn.commit()
        conn.close()

    def plan(self, **kwargs):
        return plan_legacy({"monitor_knowledge_db": str(self.db),
                            "monitor_obsidian_root": str(self.vault),
                            "monitor_obsidian_subdir": "Old",
                            "monitor_chats": [{"username": "old-chat", "display_name": "过去"}],
                            "resource_backup_selected_chats": ["old-resource-chat"]}, **kwargs)


class LegacyInventoryTests(unittest.TestCase):
    def test_initial_inventory_repeats_a_transient_metadata_change_then_keeps_exact_bytes(self):
        from core.semantic import legacy
        with tempfile.TemporaryDirectory() as temporary:
            fixture = LegacyFixture(temporary)
            target = fixture.tree / "life.md"
            inode = target.stat().st_ino
            original = os.fstat
            changed = []
            def touch_once(fd):
                observed = original(fd)
                if observed.st_ino == inode and not changed:
                    os.utime(target, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000))
                    changed.append(True)
                return observed
            with patch.object(legacy.os, "fstat", side_effect=touch_once):
                inventory = fixture.plan()
            page = next(f for f in inventory.files if f.relative_path == "Old/life.md")
            import base64
            self.assertEqual(base64.b64decode(page.bytes_base64), fixture.page_bytes)
            self.assertEqual(target.read_bytes(), fixture.page_bytes)
            self.assertEqual(changed, [True])

    def test_frozen_read_still_rejects_even_one_metadata_change(self):
        from core.semantic import legacy
        with tempfile.TemporaryDirectory() as temporary:
            fixture = LegacyFixture(temporary)
            target = fixture.tree / "life.md"
            original = os.fstat
            first = []
            def change(fd):
                observed = original(fd)
                if not first:
                    os.utime(target, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000))
                    first.append(True)
                return observed
            with patch.object(legacy.os, "fstat", side_effect=change), \
                 self.assertRaisesRegex(LegacyError, "legacy_input_changed"):
                legacy._read_regular(target, 32 * 1024 * 1024)

    def test_initial_inventory_stops_after_two_unstable_attempts(self):
        from core.semantic import legacy
        with tempfile.TemporaryDirectory() as temporary:
            fixture = LegacyFixture(temporary)
            target = fixture.tree / "foreign.md"
            inode = target.stat().st_ino
            original = os.fstat
            calls = []
            def never_stable(fd):
                observed = original(fd)
                if observed.st_ino == inode:
                    if len(calls) % 2 == 0:
                        os.utime(target, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000))
                    calls.append(True)
                return observed
            with patch.object(legacy.os, "fstat", side_effect=never_stable), \
                 self.assertRaisesRegex(LegacyError, "legacy_input_changed"):
                fixture.plan()
            self.assertEqual(len(calls), 4)
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = LegacyFixture(self.temp.name)

    def test_preserves_all_rows_pages_manual_frontmatter_anchors_and_prior_selection(self):
        before = self.fixture.db.read_bytes()
        plan = self.fixture.plan()
        self.assertEqual(before, self.fixture.db.read_bytes())
        self.assertEqual(2, sum(r.kind == "topic" for r in plan.records))
        self.assertEqual(5, sum(r.kind == "event" for r in plan.records))
        self.assertEqual(1, sum(r.kind == "relation" for r in plan.records))
        file = next(f for f in plan.files if f.relative_path == "Old/life.md")
        self.assertEqual(self.fixture.page_bytes, file.original_bytes())
        self.assertIn("^memory", [a["target"] for a in file.anchors])
        self.assertTrue(any("token=private" in r["syntax"] for r in file.references))
        self.assertTrue(any("雨天" in a for a in file.aliases))
        self.assertEqual("old-chat", plan.selection_snapshot["monitor_chats"][0]["username"])
        self.assertNotIn("subscriptions", plan.to_dict())
        self.assertEqual(plan.to_dict(), LegacyInventory.from_dict(json.loads(json.dumps(plan.to_dict()))).to_dict())

    def test_streamed_inventory_identity_matches_original_canonical_bytes(self):
        import hashlib
        from unittest.mock import patch
        plan = self.fixture.plan()
        raw = json.dumps((plan.to_dict(),), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
        expected = 'legacy_' + hashlib.sha256(raw).hexdigest()[:40]
        with patch.object(LegacyInventory, 'to_dict', side_effect=AssertionError('no full manifest copy')):
            self.assertEqual(plan.manifest_id, expected)
        # Nested metadata is mutable despite the frozen dataclass: no stale
        # process-global identity cache may mask a material change.
        plan.records[0].payload['synthetic_change'] = 'different'
        self.assertNotEqual(plan.manifest_id, expected)

    def test_streamed_record_digest_does_not_allocate_encoded_inventory(self):
        from dataclasses import replace
        import tracemalloc
        from core.semantic.legacy import _json_digest
        row = replace(self.fixture.plan().records[0], text='猫🐈' + 'x' * (1024 * 1024))
        tracemalloc.start()
        try:
            digest = _json_digest((row,) * 128)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(len(digest), 64)
        self.assertLess(peak, 32 * 1024 * 1024)

    def test_streamed_digest_is_byte_identical_for_late_nonbmp_scalars(self):
        # A non-BMP code point placed last widens the whole document if it is
        # encoded as one Unicode string. The streaming digest must stay byte
        # identical to the original canonical encoder without allocating it.
        from core.semantic.legacy import _json, _json_digest
        value = {"records": [{"text": "x" * 4096 + "\U0001f408"} for _ in range(8)]}
        self.assertEqual(_json_digest((value,)),
                         hashlib.sha256(_json((value,)).encode("utf-8")).hexdigest())

    def test_raw_excerpt_summary_are_distinct_and_duplicate_id_is_one_source(self):
        rows = {r.old_id: r for r in self.fixture.plan().records if r.kind == "event"}
        self.assertEqual("excerpt_only", rows["1"].raw_level)
        self.assertEqual("", rows["1"].source_message_id)
        self.assertEqual("derived_summary", rows["5"].raw_level)
        self.assertEqual(["raw"] * 3, [rows[i].raw_level for i in ("2", "3", "4")])
        self.assertEqual(1, len({rows[i].source_ref for i in ("2", "3", "4")}))
        self.assertEqual(3, len({rows[i].legacy_ref for i in ("2", "3", "4")}))
        self.assertEqual(["A", "B", "A"], [rows[i].text for i in ("2", "3", "4")])

    def test_unowned_file_is_inventoried_but_never_imported_or_claimed(self):
        plan = self.fixture.plan()
        foreign = next(f for f in plan.files if f.relative_path == "Old/foreign.md")
        self.assertFalse(foreign.owned)
        self.assertEqual("", foreign.bytes_base64)
        self.assertNotIn("Old/foreign.md", [r.old_id for r in plan.records if r.kind == "page"])

    def test_header_ownership_requires_actual_leading_contract(self):
        (self.fixture.tree / "date.md").write_text(
            "---\nsource_app: we-groupchat-obsidian\nsource_kind: projection\n---\n# 日历\n")
        plan = self.fixture.plan()
        self.assertEqual("source_header", next(f for f in plan.files if f.relative_path == "Old/date.md").ownership)

    def test_symlink_and_escaped_recorded_path_fail_closed(self):
        link = self.fixture.tree / "linked.md"
        link.symlink_to(self.fixture.tree / "life.md")
        with self.assertRaisesRegex(LegacyError, "legacy_symlink"):
            self.fixture.plan()
        link.unlink()
        conn = sqlite3.connect(self.fixture.db)
        conn.execute("UPDATE topics SET obsidian_path='../outside.md' WHERE topic_id=1")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(LegacyError, "legacy_path_escape"):
            self.fixture.plan()

    def test_missing_file_keeps_source_facts_and_has_explicit_missing_projection(self):
        (self.fixture.tree / "life.md").unlink()
        plan = self.fixture.plan()
        self.assertEqual(("Old/life.md",), plan.missing_paths)
        self.assertEqual(5, sum(r.kind == "event" for r in plan.records))

    def test_bounded_inputs_are_frozen_and_manifest_tampering_is_rejected(self):
        plan = self.fixture.plan()
        batches = list(iter_legacy_inputs(plan, batch_size=2))
        self.assertTrue(all(0 < len(b) <= 2 for b in batches))
        self.assertEqual(plan.records, sum(batches, ()))
        raw = plan.to_dict()
        raw["records"][0]["text"] = "lost original"
        with self.assertRaisesRegex(LegacyError, "legacy_manifest_rows_changed"):
            LegacyInventory.from_dict(raw)

    def test_explicit_id_mapping_redirect_hub_and_old_anchor_history_home(self):
        plan = self.fixture.plan()
        topics = {r.old_id: r.legacy_ref for r in plan.records if r.kind == "topic"}
        publication = build_publication_plan(plan, {topics["1"]: ["item1", "item2"], topics["2"]: ["item2"]},
                                             {"item1": "Items/one.md", "item2": "Items/two.md"})
        pages = {p["old_path"]: p for p in publication["pages"]}
        self.assertEqual("hub", pages["Old/life.md"]["mode"])
        self.assertEqual("redirect", pages["Old/second.md"]["mode"])
        self.assertTrue(pages["Old/life.md"]["preserve_original"])
        self.assertEqual("Old/life.md", pages["Old/life.md"]["anchor_maps"][0]["history_path"])
        self.assertEqual([], publication["subscriptions"])
        self.assertEqual(self.fixture.page_bytes, (self.fixture.tree / "life.md").read_bytes())
        with self.assertRaisesRegex(LegacyError, "legacy_mapping_unknown_topic"):
            build_publication_plan(plan, {"生活": ["item1"]}, {"item1": "Items/one.md"})

    def test_explicit_configuration_and_existing_database_are_required(self):
        with self.assertRaisesRegex(LegacyError, "legacy_explicit_inputs_required"):
            plan_legacy({})
        missing = self.fixture.root / "missing.sqlite"
        with self.assertRaisesRegex(LegacyError, "legacy_input_missing"):
            self.fixture.plan(db_path=missing)
        self.assertFalse(missing.exists())

    def test_real_store_import_replay_conserves_rows_and_a_b_a_without_subscriptions(self):
        conn = sqlite3.connect(self.fixture.db)
        conn.execute("INSERT INTO events VALUES(6,1,'duplicate current A','','exact-id','A','',6)")
        conn.commit()
        conn.close()
        inventory = self.fixture.plan()
        store = SemanticStore(self.fixture.root / "candidate.sqlite", "synthetic-generation")
        result = import_legacy(store, inventory, batch_size=3)
        replay = import_legacy(store, inventory, batch_size=3)
        self.assertEqual(result, replay)
        self.assertEqual("captured_semantic_pending", result.status)
        self.assertEqual(len(inventory.records), len(result.record_versions))
        raw = [r for r in inventory.records if r.raw_level == "raw"]
        self.assertEqual(result.record_versions[raw[-1].legacy_ref], result.record_versions[raw[-2].legacy_ref])
        with closing(store.connect()) as check:
            versions = [tuple(r) for r in check.execute(
                "SELECT revision,text FROM source_records WHERE provenance_grade='raw_exact' ORDER BY capture_seq")]
            self.assertEqual([(1, "A"), (2, "B"), (3, "A")], versions)
            self.assertEqual(0, check.execute("SELECT count(*) FROM items").fetchone()[0])
            selection = check.execute("SELECT disposition,metadata_json FROM legacy_mappings WHERE kind='selection'").fetchone()
            self.assertEqual("preserved_no_subscription", selection[0])
            self.assertEqual(inventory.selection_snapshot, json.loads(selection[1]))
            self.assertEqual({"captured_pending_backup"}, {r[0] for r in check.execute(
                "SELECT status FROM annotation_preservation")})
        for record in inventory.records:
            expected = sorted(legacy_resources(record), key=lambda r: (r["resource_id"], r["occurrence_id"]))
            self.assertEqual(expected, store.get_source(result.record_versions[record.legacy_ref])["resources"])
        references = [resource for record in inventory.records for resource in legacy_resources(record)]
        self.assertEqual(4, len(references))
        self.assertEqual({"pending"}, {r["status"] for r in references})
        self.assertTrue(all(r["location"] is None for r in references))
        self.assertTrue(any("token=raw" in json.dumps(r["metadata"]) for r in references))
        self.assertEqual(self.fixture.page_bytes, (self.fixture.tree / "life.md").read_bytes())

    def test_page_text_cannot_be_swapped_while_original_bytes_stay_unchanged(self):
        manifest = self.fixture.plan().to_dict()
        page = next(r for r in manifest["records"] if r["table"] == "files")
        page["text"] = "invented content"
        with self.assertRaisesRegex(LegacyError, "legacy_manifest_bytes_changed"):
            LegacyInventory.from_dict(manifest)


def preserve_history_fixture(store, inventory, root, publisher):
    """Real conservation/publication boundary with private synthetic copies."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    preserve_legacy_history(store, inventory, allow_preserved_refresh=True)
    backups = {}
    for index, file in enumerate(inventory.files):
        if file.owned:
            path = root / (str(index) + ".bin")
            path.write_bytes(file.original_bytes())
            backups[file.relative_path] = path
    import base64
    for index, external in enumerate(inventory.external_files):
        path = root / ("external-" + str(index) + ".bin")
        path.write_bytes(base64.b64decode(external["bytes_base64"]))
        backups["external:" + external["path"]] = path
    record_legacy_backups(store, inventory, backups)
    plan = build_history_publication_plan(inventory)
    result = publish_legacy_navigation(store, inventory, plan, publisher)
    if result["status"] != "published":
        raise AssertionError(result)
    return plan, backups


class SourceFirstHistoryTests(unittest.TestCase):
    def setUp(self):
        from core.semantic.projections import ProjectionPublisher, ReadingLedger, CanonicalReadingAdapter
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = LegacyFixture(self.temp.name)
        self.store = SemanticStore(self.fixture.root / "generation.sqlite", "history-generation")
        with self.store.transaction() as conn:
            conn.execute("UPDATE generation_manifest SET metadata_json=?", (json.dumps({"history_policy": SOURCE_FIRST_POLICY}),))
        self.projections = self.fixture.root / "WGO"
        self.publisher = ProjectionPublisher(ReadingLedger(self.store.connect, self.store.generation_id), self.projections,
            current_revision=CanonicalReadingAdapter(self.store).current_revision)

    def test_history_receipts_preserve_exact_rows_resources_and_do_not_create_semantic_work(self):
        inventory = self.fixture.plan()
        with patch.object(self.store, "capture_batch", side_effect=AssertionError("history is not a source batch")):
            result = preserve_legacy_history(self.store, inventory)
            self.assertEqual(result, preserve_legacy_history(self.store, inventory))
        self.assertEqual(SOURCE_FIRST_POLICY, result["history_policy"])
        with closing(self.store.connect()) as conn:
            for table in ("source_records", "capture_batches", "jobs", "items", "source_dispositions"):
                self.assertEqual(0, conn.execute("SELECT count(*) FROM " + table).fetchone()[0])
            records = conn.execute("SELECT * FROM legacy_mappings WHERE target_type='history_record'").fetchall()
            self.assertEqual(len(inventory.records), len(records))
            for record in inventory.records:
                row = next(r for r in records if r["legacy_id"] == record.legacy_ref)
                from dataclasses import asdict
                self.assertEqual(asdict(record), json.loads(row["metadata_json"])["input"])
                self.assertEqual(list(legacy_resources(record)), json.loads(row["metadata_json"])["resources"])
        with self.assertRaisesRegex(LegacyError, "legacy_history_policy_mismatch"):
            import_legacy(self.store, inventory)

    def test_history_publication_has_readable_db_only_and_missing_page_topics(self):
        with closing(sqlite3.connect(self.fixture.db)) as conn:
            conn.executemany("INSERT INTO topics VALUES(?,?,?,?,?,?,?)", [
                (3, "DB only", "只有数据库的旧结论", "", "chat", "[]", "[]"),
                (4, "Missing page", "缺页旧结论", "Old/missing.md", "chat", "[]", "[]")])
            conn.commit()
        inventory = self.fixture.plan()
        plan, _ = preserve_history_fixture(self.store, inventory, self.fixture.root / "backups", self.publisher)
        self.assertNotIn("topic_item_map", plan)
        self.assertNotIn("item_paths", plan)
        topics = [d for d in plan["documents"] if d["kind"] == "topic"]
        self.assertEqual(4, len(topics))
        text = "\n".join((self.projections / d["path"]).read_text() for d in topics)
        self.assertIn("只有数据库的旧结论", text)
        self.assertIn("缺页旧结论", text)
        self.assertIn("derived_summary", text)
        self.assertIn("excerpt_only", text)
        self.assertIn("raw", text)
        self.assertNotIn("token=raw", text)
        self.assertTrue((self.projections / "Legacy/Index.md").is_file())
        self.assertEqual(self.fixture.page_bytes, (self.fixture.tree / "life.md").read_bytes())
        self.assertTrue(all(p["preservation"] == "unknown_preserve_entire_page" for p in plan["pages"]))
        replay = publish_legacy_navigation(self.store, inventory, plan, self.publisher)
        self.assertEqual("published", replay["status"])

    def test_refresh_retains_both_immutable_passes_and_original_whole_page_backups(self):
        first = self.fixture.plan()
        old_plan, old_backups = preserve_history_fixture(self.store, first, self.fixture.root / "backup-1", self.publisher)
        (self.fixture.tree / "life.md").write_bytes(self.fixture.page_bytes + "新增个人笔记\n".encode())
        second = self.fixture.plan()
        new_plan, _ = preserve_history_fixture(self.store, second, self.fixture.root / "backup-2", self.publisher)
        self.assertNotEqual(first.manifest_id, second.manifest_id)
        self.assertNotEqual(old_plan["documents"][0]["path"], new_plan["documents"][0]["path"])
        self.assertEqual(self.fixture.page_bytes, old_backups["Old/life.md"].read_bytes())
        with closing(self.store.connect()) as conn:
            self.assertEqual(2, conn.execute("SELECT count(*) FROM legacy_mappings WHERE kind='history_pass'").fetchone()[0])
            self.assertEqual(len(first.records) + len(second.records), conn.execute(
                "SELECT count(*) FROM legacy_mappings WHERE target_type='history_record'").fetchone()[0])
        root = (self.projections / "Legacy/Index.md").read_text()
        self.assertIn(first.manifest_id, root)
        self.assertIn(second.manifest_id, root)

    def test_backup_binds_inventory_identity_once_and_retains_exact_receipts(self):
        from core.semantic import legacy
        inventory = self.fixture.plan()
        queue_dir = self.fixture.root / "synthetic-queue"
        queue_dir.mkdir()
        (queue_dir / "pending.jsonl").write_text(json.dumps({
            "schema_version": "review_queue.v1", "id": "synthetic-pending", "status": "pending",
            "title": "合成历史待办", "knowledge_topic_id": 1, "resources": {"links": [], "files": []}}) + "\n")
        inventory = legacy.include_review_queue(inventory, queue_dir)
        self.assertEqual(len(inventory.external_files), 1)
        _, backups = preserve_history_fixture(self.store, inventory, self.fixture.root / "identity-backups", self.publisher)
        with closing(self.store.connect()) as conn:
            before = [tuple(row) for row in conn.execute(
                "SELECT * FROM legacy_mappings WHERE kind='history_backup' ORDER BY legacy_id")]
        self.assertEqual(len(before), sum(file.owned for file in inventory.files) + len(inventory.external_files))
        whole_inventory_calls = []
        original = legacy._json_digest

        def observed(value):
            if len(value) == 1 and value[0] is inventory:
                whole_inventory_calls.append(True)
            return original(value)

        with patch.object(legacy, "_json_digest", side_effect=observed):
            record_legacy_backups(self.store, inventory, backups)
        self.assertEqual(len(whole_inventory_calls), 1)
        with closing(self.store.connect()) as conn:
            after = [tuple(row) for row in conn.execute(
                "SELECT * FROM legacy_mappings WHERE kind='history_backup' ORDER BY legacy_id")]
        self.assertEqual(before, after)

    def test_receipt_tampering_and_existing_semantic_history_fail_closed(self):
        inventory = self.fixture.plan()
        preserve_legacy_history(self.store, inventory)
        with self.store.transaction() as conn:
            conn.execute("UPDATE legacy_mappings SET metadata_json='{}' WHERE target_type='history_record'")
        with self.assertRaisesRegex(LegacyError, "legacy_history_receipt_conflict"):
            preserve_legacy_history(self.store, inventory)
        old_store = SemanticStore(self.fixture.root / "old-policy.sqlite", "old-policy")
        import_legacy(old_store, inventory)
        with old_store.transaction() as conn:
            conn.execute("UPDATE generation_manifest SET metadata_json=?", (json.dumps({"history_policy": SOURCE_FIRST_POLICY}),))
        with self.assertRaisesRegex(LegacyError, "legacy_history_semantic_contamination"):
            preserve_legacy_history(old_store, inventory)


if __name__ == "__main__":
    unittest.main()
