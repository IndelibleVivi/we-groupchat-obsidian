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
    plan_legacy, import_legacy, legacy_resources,
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


if __name__ == "__main__":
    unittest.main()
