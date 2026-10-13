"""Synthetic reading acceptance; never touches a real vault or source."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.platform.macos_locks import MacOSFileLock
from core.semantic.projections import (
    BodyBlock, BodyPlan, BodyPlanWriter, CanonicalReadingAdapter, EvidenceExcerpt, ProjectionPublisher,
    ReadingError, ReadingLedger, ReadingRelation, ReadingResource, ReadingSnapshot,
    ReadingUnit, anchor, fingerprint, property_value, region_body, validate_body_plan, wikilink,
)


class ScriptedTextProvider:
    def __init__(self, *responses):
        from ai.semantic_provider import ProviderRoute
        self.route = ProviderRoute("synthetic", "scripted-v1", "text")
        self.responses = list(responses)
        self.calls = []

    def generate(self, prompt, *, request_id, generation_id):
        self.calls.append((prompt, request_id, generation_id))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(prompt)
        return SimpleNamespace(text=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), route=self.route)


class ReadingHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.db = self.base / "canonical-generation.sqlite"
        self.revisions = {}
        self.ledger = ReadingLedger(lambda: sqlite3.connect(self.db, timeout=0.5), "generation-one")
        self.publisher = ProjectionPublisher(self.ledger, self.base / "WGO",
                                             current_revision=self.revisions.get,
                                             file_lock=MacOSFileLock())

    def snapshot(self, revision=1, *, item_id="box-cat", text="周五，她把箱子让给了猫。", title="箱猫"):
        self.revisions[item_id] = revision
        return ReadingSnapshot(item_id, revision, title, "2026-10-09T12:00:00+08:00",
                               (ReadingUnit("story", text, ("e1",), verbatim=(text,)),),
                               (EvidenceExcerpt("e1", text, "此前刚搬家。", "2026-10-09", "2026-10-09"),), kind="story")

    def plan(self, snapshot):
        return validate_body_plan(snapshot, BodyPlan(snapshot.item_id, snapshot.content_revision, tuple(
            BodyBlock("paragraph-" + u.id, u.role, u.text, (u.id,), u.evidence_ids, u.resource_ids,
                      snapshot.item_id, {u.id: u.status} if u.status != "current" else {})
            for u in snapshot.units)))

    def publish(self, snapshot=None):
        snapshot = snapshot or self.snapshot()
        return self.publisher.publish_item(snapshot, self.plan(snapshot))

    def canonical_story(self):
        from core.semantic.store import SemanticStore
        store = SemanticStore(self.base / "actual.sqlite", "canonical-gen", now_func=lambda: 1791849600.0)
        observation, span_id = self.canonical_extract(store, "capture", "周五，猫躺在箱子里。")
        self.canonical_actions(store, [{"action_key": "create-item", "type": "CREATE_ITEM", "item_id": "stable-cat",
            "kind": "life", "title": "箱猫", "identity_scope": {"scene": "同一只猫"},
            "observation_ids": [observation["observation_id"]]}])
        return store, observation, span_id

    def canonical_extract(self, store, batch_id, source_text, *, claims=None, payload=None,
                          source_namespace="synthetic", chat_id="synthetic-chat", unit_kind="scene"):
        from core.semantic.attribution import captured_source_metadata, EXTRACTION_PROMPT_VERSION
        from core.semantic.jobs import JobStore
        from core.semantic.engine import CapturedSource, prepare_spans, validate_extraction, validate_accepted_artifact
        capture = store.capture_batch(batch_id, [{"source_id": batch_id + "-msg", "logical_shard_id": "shard",
            "physical_generation": "physical", "source_cursor_token": "cursor", "source_sent_at": 1791763200.0,
            "text": source_text, "payload": payload or {}}], source_namespace=source_namespace, chat_id=chat_id,
            inventory_digest="synthetic-inventory", inventory_revision=1, input_cursors={}, output_cursors={}, source_eof=True)
        source = CapturedSource(capture["member_version_ids"][0], source_text, occurred_at=1791763200.0,
                                **captured_source_metadata(source_namespace, chat_id, payload or {}))
        span = prepare_spans((source,), namespace=store.generation_id)[0]
        observations = [value.as_dict() for value in validate_extraction({"observations": [{
            "title": "箱猫", "scene_kind": "life", "spans": [{"source_id": source.source_id, "span_id": span.span_id}],
            "units": [{"kind": unit_kind, "text": source_text, "span_indices": [0]}], "topics": [], "claims": claims or []}]},
            (source,), namespace=store.generation_id)]
        artifact = {"observations": observations, "dispositions": {}}
        jobs = JobStore(store)
        job = jobs.ensure("extract", {"batch_id": batch_id, "members": [asdict(source)]},
                          {"schema": "synthetic", "prompt": EXTRACTION_PROMPT_VERSION})
        jobs.claim(job["job_id"], "test-worker")
        jobs.accept(job["job_id"], "test-worker", artifact,
                    validate=lambda value: validate_accepted_artifact(value, artifact))
        store.commit_extraction(job["job_id"], batch_id, observations)
        return observations[0], span.span_id

    def canonical_actions(self, store, actions):
        from core.semantic.jobs import JobStore
        from core.semantic.engine import validate_accepted_artifact
        items = {value["item_id"]: store.get_item(value["item_id"]) for value in store.list_items()}
        read_set = {"store_revision": store.read_revision(), "items": {
            id_: item["content_revision"] for id_, item in items.items()}, "views": {}}
        observations = list({obs["observation_id"]: obs for item in items.values()
                             for obs in item["observations"]}.values())
        for action in actions:
            for id_ in action.get("observation_ids", ()):
                if id_ not in {obs["observation_id"] for obs in observations}:
                    observations.append(store.get_observation(id_))
        state = {"read_set": read_set, "observations": observations, "items": items}
        artifact = {"actions": actions, "read_set": read_set}
        jobs = JobStore(store)
        job = jobs.ensure("propose", {"state": state}, {"schema": "synthetic"})
        jobs.claim(job["job_id"], "test-worker")
        jobs.accept(job["job_id"], "test-worker", artifact,
                    validate=lambda value: validate_accepted_artifact(value, artifact))
        return store.apply_proposal(job["job_id"], actions, read_set=read_set)


class ProjectionTests(ReadingHarness):
    @staticmethod
    def paged_response(prompt):
        value = json.loads(prompt)
        snapshot = value["snapshot"]
        previous = value["previous"] or {"blocks": []}
        old_ids = {id_ for block in previous["blocks"] for id_ in block["unit_ids"]}
        blocks = list(previous["blocks"])
        for unit in snapshot["units"]:
            if unit["id"] in old_ids:
                continue
            blocks.append({"id": "paragraph-" + fingerprint(unit["id"])[:24],
                "role": "counterevidence" if unit["status"] != "current" else unit["role"],
                "text": unit["text"], "unit_ids": [unit["id"]], "evidence_ids": unit["evidence_ids"],
                "resource_ids": unit["resource_ids"], "primary_home": snapshot["item_id"],
                "unit_statuses": {unit["id"]: unit["status"]} if unit["status"] != "current" else {}})
        return {"item_id": snapshot["item_id"], "content_revision": snapshot["content_revision"], "blocks": blocks}

    def test_large_body_pages_cover_every_unit_and_accept_before_file_publish(self):
        units = tuple(ReadingUnit("unit-" + str(i), "这是日常片段。" * 40 + str(i), ("e-" + str(i),)) for i in range(60))
        snapshot = replace(self.snapshot(), units=units,
            evidence=tuple(EvidenceExcerpt("e-" + str(i), u.text) for i, u in enumerate(units)))
        provider = ScriptedTextProvider(*([self.paged_response] * 100))
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=9000)
        plan = writer.plan(snapshot, request_id="large-body")
        self.assertGreater(len(provider.calls), 1)
        self.assertTrue(all(len(prompt.encode()) <= 9000 for prompt, *_ in provider.calls))
        self.assertEqual({id_ for b in plan.blocks for id_ in b.unit_ids}, {u.id for u in units})
        supplied = {u["id"] for prompt, *_ in provider.calls for u in json.loads(prompt)["snapshot"]["units"]}
        self.assertEqual(supplied, {u.id for u in units})
        self.assertIsNone(self.publisher.resolve(snapshot.item_id))
        root = self.ledger.one("SELECT * FROM reader_text_artifacts WHERE request_id='large-body'")
        self.assertEqual(root["status"], "accepted")
        count = len(provider.calls)
        resumed = writer.plan(snapshot, request_id="large-body")
        self.assertEqual(resumed, plan)
        self.assertEqual(len(provider.calls), count)
        self.publisher.publish_item(snapshot, resumed)
        self.assertTrue(self.publisher.ready(snapshot.item_id, snapshot.content_revision))

    def test_oversized_single_unit_is_lossless_local_continuations(self):
        text = "连续的中文与步骤不能被截断。" * 6000
        context = "完整必要上下文。" * 300
        unit = ReadingUnit("huge-unit", text, ("large-evidence",), role="step", verbatim=(text,))
        snapshot = replace(self.snapshot(), units=(unit,), evidence=(EvidenceExcerpt("large-evidence", text, context),))
        provider = ScriptedTextProvider(*([self.paged_response] * 1000))
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=16000)
        plan = writer.plan(snapshot, request_id="huge-unit")
        self.assertEqual(len(plan.blocks), 1)
        self.assertEqual(plan.blocks[0].text, text)
        self.assertEqual(plan.blocks[0].unit_ids, ("huge-unit",))
        prompts = [json.loads(call[0]) for call in provider.calls]
        self.assertTrue(all(len(call[0].encode()) <= 16000 for call in provider.calls))
        self.assertEqual({p["phase"] for p in prompts}, {"body_fragment_v1"})
        evidence = [e for p in prompts for e in p["snapshot"]["evidence"]]
        self.assertEqual("".join(e["text"] for e in evidence), text)
        self.assertEqual("".join(e["context"] for e in evidence), context)
        self.assertEqual(writer.plan(snapshot, request_id="huge-unit"), plan)

    def test_markdown_quote_grounding_and_real_control_risks(self):
        snapshot = replace(self.snapshot(), evidence=(EvidenceExcerpt("e1", "原话第一行。\n原话第二行。", speaker_name="小禾"),
                                                      EvidenceExcerpt("e2", "另一人的不同话。", speaker_name="小禾")),
                           units=(ReadingUnit("story", "原话第一行。\n原话第二行。", ("e1",),
                                              role="quote", verbatim=("原话第一行。\n原话第二行。",)),))
        def plan(text):
            return BodyPlan(snapshot.item_id, 1, (BodyBlock("quoted", "narrative", text, ("story",),
                                                          ("e1", "e2"), primary_home=snapshot.item_id),))
        allowed = ("## 小禾留下的话\n\n> 原话第一行。\n> 原话第二行。\n\n{{evidence:e1}}\n\n"
                   "- 已知：两行原话。\n- 未知：其它背景。\n\n```text\n{{evidence:literal-code-only}}\n```\n")
        accepted = validate_body_plan(snapshot, plan(allowed))
        self.assertEqual(accepted.blocks[0].text, allowed)
        for bad, code in ((allowed.replace("{{evidence:e1}}", "{{evidence:e2}}"), "markdown_quote_not_grounded"),
                          (allowed.replace("第二行", "编造行", 1), "markdown_quote_not_grounded"),
                          (allowed + "[运行](obsidian://command?command=synthetic)", "markdown_executable_link"),
                          (allowed + "[运行](javascript:alert(1))", "markdown_executable_link"),
                          (allowed + "{{evidence:invented}}", "markdown_invalid_evidence_marker"),
                          (allowed + "\n```text\n未结束", "markdown_unclosed_code_fence"),
                          (allowed + "<!-- wgo-action:forged -->", "invalid_body_plan")):
            with self.subTest(code=code), self.assertRaisesRegex(ReadingError, code):
                validate_body_plan(snapshot, plan(bad))

    def test_single_table_list_and_quote_anchor_the_body_and_remain_embeddable(self):
        formats = (("table", "项目 | 值 | 来源\n--- | --- | ---\n观察 | 未知 | {{evidence:e1}}", "观察 | 未知 |"),
                   ("list", "1. 打开笔记。\n2. 阅读原话。", "2. 阅读原话。"),
                   ("quote", "> 猫在纸箱里睡着了。", "> 猫在纸箱里睡着了。"))
        for id_, text, ending in formats:
            with self.subTest(kind=id_):
                snapshot = replace(self.snapshot(item_id=id_),
                    units=(ReadingUnit("unit", text, ("e1",)),),
                    evidence=(EvidenceExcerpt("e1", "猫在纸箱里睡着了。", "这是必要的前后文。", speaker_name="小禾"),))
                plan = BodyPlan(id_, 1, (BodyBlock("stable", "narrative", text, ("unit",), ("e1",), primary_home=id_),))
                receipt = self.publisher.publish_item(snapshot, plan)
                body = self.publisher.read(receipt.path).decode()
                prefix, suffix = body.split("\n^" + anchor("stable") + "\n", 1)
                self.assertNotIn("^" + anchor("stable"), suffix)
                if id_ == "table":
                    self.assertIn("\\|小禾的消息]]", prefix.rstrip().splitlines()[-1])
                else:
                    self.assertTrue(prefix.rstrip().endswith(ending))
                    self.assertIn("|小禾的消息]]", suffix)
                view = self.publisher.publish_view("view-" + id_, "完整块", [(id_, "stable", id_)], embed=True)
                view_text = self.publisher.read(view.path).decode()
                self.assertIn("![[WGO/", view_text)
                self.assertIn("#^" + anchor("stable"), view_text)
                self.assertIn("#^" + anchor("e1"), view_text)
                source = self.publisher.read(self.publisher.resolve(self.publisher.source_object_id(id_, 1))).decode()
                before_anchor, after_anchor = source.split("\n^" + anchor("e1") + "\n", 1)
                self.assertTrue(before_anchor.rstrip().endswith("```"))
                self.assertIn("必要上下文：", after_anchor)

    def test_oversized_fenced_steps_validate_only_after_lossless_join(self):
        text = "## 本地步骤\n\n1. 保留完整示例。\n\n```text\n" + "连续记录不能截断。\n" * 1500 + "```"
        snapshot = replace(self.snapshot(), units=(ReadingUnit("fenced", text, ("e1",), role="step", verbatim=(text,)),),
                           evidence=(EvidenceExcerpt("e1", text, speaker_name="阿岚"),))
        provider = ScriptedTextProvider(*([self.paged_response] * 500))
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=12000)
        plan = writer.plan(snapshot, request_id="fenced-continuations")
        self.assertEqual(plan.blocks[0].text, text)
        self.assertGreater(len(provider.calls), 1)
        self.assertTrue(all(len(prompt.encode()) <= 12000 for prompt, *_ in provider.calls))
        self.assertTrue(all(e["speaker_name"] == "阿岚" for prompt, *_ in provider.calls
                            for e in json.loads(prompt)["snapshot"]["evidence"]))

    def test_large_increment_reuses_unaffected_homes_and_only_writes_new_detail(self):
        units = tuple(ReadingUnit("unit-" + str(i), "已有的日常段落。" * 30, ("e-" + str(i),)) for i in range(40))
        first = replace(self.snapshot(), units=units,
            evidence=tuple(EvidenceExcerpt("e-" + str(i), u.text) for i, u in enumerate(units)))
        old = self.plan(first)
        extra = ReadingUnit("new-detail", "后来它把纸箱挪到了窗边。", ("new-evidence",))
        updated = replace(first, content_revision=2, units=units + (extra,),
                          evidence=first.evidence + (EvidenceExcerpt("new-evidence", extra.text),))
        provider = ScriptedTextProvider(*([self.paged_response] * 100))
        plan = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=9000).plan(
            updated, previous=old, request_id="bounded-increment")
        self.assertEqual(plan.blocks[:len(old.blocks)], old.blocks)
        supplied = {u["id"] for prompt, *_ in provider.calls for u in json.loads(prompt)["snapshot"]["units"]}
        self.assertEqual(supplied, {units[-1].id, extra.id})
        self.assertEqual(len(provider.calls), 1)

    def test_story_friday_saturday_sunday_monday_increment(self):
        friday = self.snapshot()
        first = self.publish(friday)
        initial = self.publisher.load_plan(friday.item_id)
        saturday_text = "周五，她把箱子让给了猫。周六吃面时，猫又把纸箱推到了桌边。"
        saturday = replace(friday, content_revision=2,
                           units=(replace(friday.units[0], text=saturday_text, evidence_ids=("e1", "e2"),
                                          verbatim=(friday.units[0].text, "周六吃面")),),
                           evidence=friday.evidence + (EvidenceExcerpt("e2", "周六吃面时，猫又把纸箱推到了桌边。"),))
        self.revisions[friday.item_id] = 2
        saturday_plan = self.plan(saturday)
        provider = ScriptedTextProvider(json.loads(saturday_plan.to_json()))
        writer = BodyPlanWriter(provider, "generation-one")
        amended = writer.plan(saturday, previous=initial, request_id="saturday")
        second = self.publisher.publish_item(saturday, amended)
        self.assertEqual(first.path, second.path)
        text = self.publisher.read(second.path).decode()
        self.assertIn(saturday_text, text)
        self.assertEqual(text.count("周五，她把箱子让给了猫。"), 1)
        before = {p: p.stat().st_mtime_ns for p in self.publisher.root.rglob("*.md")}
        sunday = writer.plan(saturday, previous=amended, request_id="sunday-repost")
        repeated = self.publisher.publish_item(saturday, sunday)
        self.assertEqual(repeated.status, "unchanged")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})
        monday = replace(saturday, content_revision=3,
                         units=saturday.units + (ReadingUnit("window", "周一它终于睡在窗边。", ("e3",)),),
                         evidence=saturday.evidence + (EvidenceExcerpt("e3", "周一它终于睡在窗边。"),))
        self.revisions[monday.item_id] = 3
        provider.responses.append(json.loads(self.plan(monday).to_json()))
        continued = writer.plan(monday, previous=amended, request_id="monday")
        self.assertEqual(continued.blocks[0], amended.blocks[0])
        self.publisher.publish_item(monday, continued)
        self.assertIn("周一它终于睡在窗边。", self.publisher.read(first.path).decode())

    def test_unknown_yaml_title_notes_and_code_are_byte_preserved(self):
        snapshot = self.snapshot()
        receipt = self.publish(snapshot)
        path = self.publisher.root / receipt.path
        text = path.read_text()
        custom = "unknown: !custom\n  nested: [一, 二]\n# 自己的 YAML 注释\n"
        text = text.replace("---\n", "---\n" + custom, 1).replace("# 箱猫", "# 我改的显示标题")
        notes = "\n个人备注：不要改这里。\n```python\nprint('中文')\n```\n"
        path.write_text(text + notes)
        revised = replace(snapshot, content_revision=2, title="源端新标题")
        self.revisions[snapshot.item_id] = 2
        self.publisher.publish_item(revised, self.plan(revised))
        after = path.read_text()
        self.assertIn(custom, after)
        self.assertIn("# 我改的显示标题", after)
        self.assertTrue(after.endswith(notes))
        self.assertEqual(property_value(after, "wgo_annotation_revision"), 1)
        self.assertEqual(self.publisher.record(snapshot.item_id)["semantic_at"], snapshot.semantic_at)

    def test_manual_machine_edit_creates_complete_backup_and_bound_proposal(self):
        snapshot = self.snapshot()
        receipt = self.publish(snapshot)
        path = self.publisher.root / receipt.path
        edited = path.read_text().replace("周五，她把箱子让给了猫。", "我亲自改写的箱猫")
        path.write_text(edited)
        revised = replace(snapshot, content_revision=2)
        self.revisions[snapshot.item_id] = 2
        result = self.publisher.publish_item(revised, self.plan(revised))
        self.assertEqual(result.status, "conflict")
        self.assertEqual(path.read_text(), edited)
        proposal = self.publisher.pending_proposals()[0]
        self.assertEqual(self.publisher.read(proposal["backup_path"]), edited.encode())
        self.assertIsNotNone(self.publisher.read(proposal["draft_path"]))
        self.assertFalse(self.publisher.ready(snapshot.item_id, 2))
        path.write_text(edited + "\n临时补一句。\n")
        with self.assertRaisesRegex(ReadingError, "stale_proposal"):
            self.publisher.resolve_proposal(result.proposal_id, "adopt", expected_revision=2,
                                            expected_file_hash=result.file_hash)

    def test_move_notes_then_adopt_retains_personal_rewrite(self):
        snapshot = self.snapshot()
        receipt = self.publish(snapshot)
        path = self.publisher.root / receipt.path
        path.write_text(path.read_text().replace("周五，她把箱子让给了猫。", "保留我的整段改写"))
        result = self.publish(snapshot)
        accepted = self.publisher.resolve_proposal(result.proposal_id, "move_notes", expected_revision=1,
                                                   expected_file_hash=result.file_hash)
        self.assertEqual(accepted.status, "published")
        text = path.read_text()
        self.assertIn("## 个人备注（保留改写）", text)
        self.assertIn("保留我的整段改写", text)
        self.assertIn(snapshot.units[0].text, region_body(text, "body"))
        self.assertTrue(self.publisher.ready(snapshot.item_id, 1))

    def test_legacy_file_with_no_baseline_is_preserved(self):
        snapshot = self.snapshot()
        path = self.publisher._safe_path("条目/box-cat.md")
        legacy = "---\nmy_field: [foo, bar]\n---\n# 旧笔记\n整页来源未知，全部保留。\n"
        path.write_text(legacy)
        receipt = self.publish(snapshot)
        self.assertEqual(receipt.status, "conflict")
        self.assertEqual(path.read_text(), legacy)
        self.publisher.resolve_proposal(receipt.proposal_id, "adopt", expected_revision=1,
                                         expected_file_hash=receipt.file_hash)
        self.assertIn(legacy.split("---\n")[-1], path.read_text())

    def test_rename_identity_collision_stale_revision_and_symlink_fail_closed(self):
        snapshot = self.snapshot()
        receipt = self.publish(snapshot)
        old = self.publisher.root / receipt.path
        renamed = self.publisher.root / "我自己的标题.md"
        old.rename(renamed)
        self.assertEqual(self.publisher.resolve(snapshot.item_id), "我自己的标题.md")
        revised = replace(snapshot, title="不同标题")
        self.assertEqual(self.publish(revised).path, "我自己的标题.md")
        duplicate = self.publisher.root / "重复.md"
        duplicate.write_bytes(renamed.read_bytes())
        with self.assertRaisesRegex(ReadingError, "identity_collision"):
            self.publisher.resolve(snapshot.item_id)
        duplicate.unlink()
        self.revisions[snapshot.item_id] = 2
        with self.assertRaisesRegex(ReadingError, "stale_projection_snapshot"):
            self.publish(snapshot)
        link = self.publisher.root / "别名.md"
        link.symlink_to(renamed)
        with self.assertRaisesRegex(ReadingError, "projection_symlink"):
            self.publisher.resolve(snapshot.item_id)

    def test_multiviews_embed_same_block_without_copy_and_table_alias(self):
        snapshot = self.snapshot()
        receipt = self.publish(snapshot)
        entries = [(snapshot.item_id, "paragraph-story", "猫的日常")]
        first = self.publisher.publish_view("life", "日常", entries)
        second = self.publisher.publish_view("home", "家里", entries)
        for view in (first, second):
            text = self.publisher.read(view.path).decode()
            self.assertNotIn(snapshot.units[0].text, text)
            self.assertIn("![[WGO/" + receipt.path.removesuffix(".md") + "#^" + anchor("paragraph-story"), text)
        self.assertIn("\\|", wikilink("WGO/条目/box-cat.md", "箱猫", table=True))

    def test_evidence_is_bounded_redacted_and_missing_attachment_is_explicit(self):
        snapshot = self.snapshot()
        resource = ReadingResource("photo", "猫的照片", str(self.base / "absent.png"), "available")
        snapshot = replace(snapshot, units=(replace(snapshot.units[0], resource_ids=("photo",)),),
                           resources=(resource,), evidence=(replace(snapshot.evidence[0],
                               context="链接 https://example.invalid/x?token=secret&topic=cat"),))
        receipt = self.publish(snapshot)
        self.assertIn("附件待到达", self.publisher.read(receipt.path).decode())
        source = self.publisher.read("来源/box-cat-0001.md").decode()
        self.assertIn("REDACTED", source)
        self.assertNotIn("token=secret", source)
        self.assertIn("^" + anchor("e1"), source)
        many = replace(snapshot, evidence=tuple(EvidenceExcerpt("e" + str(i), "片段") for i in range(100, 170)))
        self.publisher._publish_sources(many)
        self.assertGreaterEqual(len(list((self.publisher.root / "来源").glob("*.md"))), 3)

    def test_untouched_paragraph_rewrite_and_fabricated_ref_rejected(self):
        snapshot = self.snapshot()
        original = self.plan(snapshot)
        monday = replace(snapshot, content_revision=2,
                         units=snapshot.units + (ReadingUnit("new", "在窗边。", ("e2",)),),
                         evidence=snapshot.evidence + (EvidenceExcerpt("e2", "在窗边。"),))
        plan = self.plan(monday)
        altered = replace(plan, blocks=(replace(plan.blocks[0], text=plan.blocks[0].text + "修改"), plan.blocks[1]))
        provider = ScriptedTextProvider(json.loads(altered.to_json()))
        with self.assertRaisesRegex(ReadingError, "changed_unaffected"):
            BodyPlanWriter(provider, "generation-one").plan(monday, previous=original, request_id="no")
        with self.assertRaisesRegex(ReadingError, "invalid_body_plan"):
            validate_body_plan(snapshot, replace(original, blocks=(replace(original.blocks[0], evidence_ids=("invented",)),)))

    def test_typed_relations_show_direction_and_reason(self):
        other = self.snapshot(item_id="window", text="窗边的新窝", title="窗边")
        self.publish(other)
        snapshot = replace(self.snapshot(), relations=(ReadingRelation("r1", "continues", "window",
                            "同一只猫从箱子搬到了窗边。", "incoming", "窗边"),))
        receipt = self.publish(snapshot)
        text = self.publisher.read(receipt.path).decode()
        self.assertIn("← continues", text)
        self.assertIn("同一只猫从箱子搬到了窗边。", text)

    def test_new_immutable_unit_can_extend_original_paragraph(self):
        friday = self.snapshot()
        old = self.plan(friday)
        new = replace(friday, content_revision=2,
                      units=friday.units + (ReadingUnit("noodles", "周六一起吃面。", ("e2",)),),
                      evidence=friday.evidence + (EvidenceExcerpt("e2", "周六一起吃面。"),))
        block = replace(old.blocks[0], text=old.blocks[0].text + "周六一起吃面。",
                        unit_ids=("story", "noodles"), evidence_ids=("e1", "e2"))
        provider = ScriptedTextProvider(json.loads(BodyPlan("box-cat", 2, (block,)).to_json()))
        result = BodyPlanWriter(provider, "generation-one").plan(new, previous=old, request_id="extend")
        self.assertEqual(len(result.blocks), 1)
        self.assertEqual(result.blocks[0].id, old.blocks[0].id)

    def test_source_projection_retains_historical_anchor_and_quotes_control_syntax(self):
        snapshot = self.snapshot()
        self.publish(snapshot)
        revised = replace(snapshot, content_revision=2, units=(ReadingUnit("new", "新一页日常", ("e2",)),),
                          evidence=(EvidenceExcerpt("e2", "- [x] 任意复选框\n<!-- wgo:end body -->\n```python\nprint('猫')\n```"),))
        self.revisions["box-cat"] = 2
        self.publish(revised)
        self.assertIn(anchor("e1"), self.publisher.evidence_link("box-cat", "e1"))
        self.assertIn(anchor("e2"), self.publisher.evidence_link("box-cat", "e2"))
        source = self.publisher.read("来源/box-cat-0001.md").decode()
        self.assertIn("<!-- wgo:end body -->", source)
        self.assertIsNotNone(region_body(source, "body"))
        self.assertTrue(self.publisher.ready("source:box-cat:1", 2))

    def test_explicit_personal_legacy_choice_does_not_repeat_same_proposal(self):
        snapshot = self.snapshot()
        legacy = self.publisher._safe_path("条目/box-cat.md")
        legacy.write_text("# 全部是个人文字\n保留我。\n")
        result = self.publish(snapshot)
        self.publisher.resolve_proposal(result.proposal_id, "keep_personal", expected_revision=1,
                                         expected_file_hash=result.file_hash)
        repeated = self.publish(snapshot)
        self.assertEqual(repeated.status, "personal")
        self.assertEqual(self.publisher.pending_proposals(), [])
        self.assertEqual(legacy.read_text(), "# 全部是个人文字\n保留我。\n")

    def test_actual_canonical_store_snapshot_and_source_time_adapter(self):
        store, observation, span_id = self.canonical_story()
        with store.transaction() as conn:
            conn.execute("INSERT INTO resource_refs VALUES(?,?,?,?,?,?,?,?)",
                         ("photo", observation["observation_id"], "stable-cat", "occurrence", "猫照片", "pending", None, '{}'))
        adapter = CanonicalReadingAdapter(store)
        snapshot = adapter.snapshot("stable-cat")
        self.assertEqual(snapshot.item_id, "stable-cat")
        self.assertEqual(snapshot.evidence[0].id, span_id)
        self.assertNotEqual(snapshot.evidence[0].source_time, snapshot.evidence[0].known_at)
        self.assertEqual(snapshot.units[0].resource_ids, ("photo",))
        publisher = ProjectionPublisher.for_store(store, self.base / "actual-WGO", file_lock=MacOSFileLock())
        provider = ScriptedTextProvider(json.loads(self.plan(snapshot).to_json()))
        receipt = publisher.publish_canonical_item(store, "stable-cat", BodyPlanWriter(provider, store.generation_id), request_id="body")
        self.assertEqual(receipt.status, "published")
        self.assertEqual(store.pending_outbox("projection"), [])
        self.assertTrue(publisher.ready("stable-cat", snapshot.content_revision))
        from core.semantic.controls import CoreSearchAdapter, ReaderControls
        from core.semantic.retrieval import HybridRetriever
        from core.semantic.projections import patch_properties
        controls = ReaderControls(publisher.ledger, publisher, search=CoreSearchAdapter(store, HybridRetriever(store)))
        controls.initialize_query()
        query_path = publisher.root / "检索.md"
        query_path.write_text(patch_properties(query_path.read_text(), {"查询": "箱子里的猫"}, machine_only=False))
        result = controls.run_query()
        self.assertEqual(result.status, "success")
        self.assertIn("stable-cat", query_path.read_text())
        self.assertIn("检索覆盖仍有未完成部分", query_path.read_text())

    def test_adapter_names_are_captured_labels_not_cross_source_identity(self):
        from core.semantic.store import SemanticStore
        store = SemanticStore(self.base / "attributed.sqlite", "attribution-generation")
        observations = []
        for index, (namespace, payload) in enumerate((
                ("capture-a", {"raw_sender_id": "wxid_fixture_same", "sender": "小禾"}),
                ("capture-b", {"raw_sender_id": "wxid_fixture_same", "sender": "小禾"}),
                ("capture-a", {"raw_sender_id": "wxid_fixture_other", "sender": "wxid_fixture_other"}))):
            observation, _ = self.canonical_extract(store, "label-" + str(index), "独立记录 " + str(index),
                                                    payload=payload, source_namespace=namespace)
            observations.append(observation["observation_id"])
        self.canonical_actions(store, [{"action_key": "comparison", "type": "CREATE_ITEM", "item_id": "comparison",
            "kind": "story", "title": "独立记录对照", "identity_scope": {"scene": "对照而非合并人物"},
            "observation_ids": observations}])
        snapshot = CanonicalReadingAdapter(store).snapshot("comparison")
        by_text = {e.text: e for e in snapshot.evidence}
        self.assertEqual(by_text["独立记录 0"].speaker_name, by_text["独立记录 1"].speaker_name)
        self.assertNotEqual(by_text["独立记录 0"].speaker_id, by_text["独立记录 1"].speaker_id)
        self.assertIsNone(by_text["独立记录 2"].speaker_name)
        self.assertNotIn("wxid_", json.dumps(asdict(snapshot), ensure_ascii=False))

    def test_source_attribution_upgrade_is_render_only_and_exact_old_body_bound(self):
        snapshot = replace(self.snapshot(), evidence=(replace(self.snapshot().evidence[0], speaker_name="小禾"),))
        self.publish(snapshot)
        excerpt = snapshot.evidence[0]
        old = self.publisher._source_fragment(excerpt, attribution=False)
        with self.ledger.transaction() as conn:
            conn.execute("UPDATE reader_source_fragments SET fragment=?,fragment_hash=? WHERE evidence_id=?",
                         (old, fingerprint(old), excerpt.id))
        object_id = self.publisher.source_object_id(snapshot.item_id, 1)
        path = self.publisher.resolve(object_id)
        self.publisher.publish_document(object_id=object_id, path=path, title="旧格式来源",
            body=old, content_revision=snapshot.content_revision, semantic_at=snapshot.semantic_at)
        source_file = self.publisher.root / path
        source_file.write_text(source_file.read_text() + "\n个人旁注：保留这句话。\n")
        before = self.publisher.record(object_id)
        self.publish(snapshot)
        after = self.publisher.record(object_id)
        self.assertEqual(after["content_revision"], before["content_revision"])
        self.assertEqual(after["semantic_at"], before["semantic_at"])
        self.assertEqual(after["render_revision"], before["render_revision"] + 1)
        self.assertIn("本条消息发言／转述者：小禾", source_file.read_text())
        self.assertIn("个人旁注：保留这句话。", source_file.read_text())
        with self.ledger.transaction() as conn:
            conn.execute("UPDATE reader_source_fragments SET fragment=?,fragment_hash=? WHERE evidence_id=?",
                         (old + "伪造片段", fingerprint(old + "伪造片段"), excerpt.id))
        with self.assertRaisesRegex(ReadingError, "evidence_projection_identity_changed"):
            self.publish(snapshot)

    def test_accepted_body_plan_resumes_after_crash_without_second_provider_call(self):
        store, _observation, _span = self.canonical_story()
        publisher = ProjectionPublisher.for_store(store, self.base / "actual-WGO", file_lock=MacOSFileLock())
        snapshot = CanonicalReadingAdapter(store).snapshot("stable-cat")
        provider = ScriptedTextProvider(json.loads(self.plan(snapshot).to_json()))
        writer = BodyPlanWriter(provider, store.generation_id)
        with patch.object(publisher, "publish_item", side_effect=RuntimeError("synthetic publication crash")):
            with self.assertRaisesRegex(RuntimeError, "synthetic publication crash"):
                publisher.publish_canonical_item(store, "stable-cat", writer, request_id="body-request")
        artifact = publisher.ledger.one("SELECT * FROM reader_text_artifacts")
        self.assertEqual(artifact["status"], "accepted")
        self.assertEqual(BodyPlan.from_json(artifact["artifact_json"]).content_revision, snapshot.content_revision)
        self.assertIsNone(publisher.resolve("stable-cat"))
        self.assertTrue(store.pending_outbox("projection"))
        resumed = publisher.publish_canonical_item(store, "stable-cat", writer, request_id="body-request")
        self.assertEqual(resumed.status, "published")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(store.pending_outbox("projection"), [])

    def test_body_unknown_outcome_cannot_be_retried_under_new_request(self):
        snapshot = self.snapshot()
        provider = ScriptedTextProvider(RuntimeError("unknown transport result"))
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger)
        with self.assertRaisesRegex(RuntimeError, "unknown transport result"):
            writer.plan(snapshot, request_id="one")
        with self.assertRaisesRegex(ReadingError, "text_provider_outcome_unknown"):
            writer.plan(snapshot, request_id="two")
        self.assertEqual(len(provider.calls), 1)

    def test_accepted_artifact_binds_exact_snapshot_and_provider_version(self):
        from ai.semantic_provider import ProviderRoute
        snapshot = self.snapshot()
        provider = ScriptedTextProvider(json.loads(self.plan(snapshot).to_json()))
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger)
        writer.plan(snapshot, request_id="same-request")
        changed = replace(snapshot, content_revision=2)
        with self.assertRaisesRegex(ReadingError, "text_request_identity_conflict"):
            writer.plan(changed, request_id="same-request")
        provider.route = ProviderRoute("synthetic", "scripted-v2", "text")
        provider.responses.append(json.loads(self.plan(snapshot).to_json()))
        writer.plan(snapshot, request_id="new-provider-version")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(self.ledger.all("SELECT * FROM reader_text_artifacts WHERE status='accepted'")), 2)

    def test_identity_merge_redirect_and_split_hub_preserve_notes_and_resolve_rename(self):
        store, first, _span = self.canonical_story()
        second, _span = self.canonical_extract(store, "later", "周一，猫终于睡在窗边。")
        self.canonical_actions(store, [{"action_key": "create-window", "type": "CREATE_ITEM", "item_id": "window",
            "kind": "story", "title": "窗边", "identity_scope": {"scene": "窗边"},
            "observation_ids": [second["observation_id"]]}])
        publisher = ProjectionPublisher.for_store(store, self.base / "actual-WGO", file_lock=MacOSFileLock())
        adapter = CanonicalReadingAdapter(store)
        for item_id in ("stable-cat", "window"):
            snapshot = adapter.snapshot(item_id)
            publisher.publish_item(snapshot, self.plan(snapshot))
        old = publisher.root / publisher.resolve("window")
        old.write_text(old.read_text() + "\n我的窗边备注。\n")
        self.canonical_actions(store, [{"action_key": "merge-window", "type": "MERGE_ITEMS",
            "item_ids": ["stable-cat", "window"], "target_item_id": "stable-cat", "reason": "同一只猫的延续"}])
        with self.assertRaisesRegex(ReadingError, "identity_target_not_ready"):
            publisher.publish_identity_redirect(store, "window")
        snapshot = adapter.snapshot("stable-cat")
        publisher.publish_item(snapshot, self.plan(snapshot))
        redirect = publisher.publish_identity_redirect(store, "window")
        self.assertTrue(publisher.ready("window", store.get_item("window")["content_revision"]))
        self.assertIn("我的窗边备注", old.read_text())
        self.assertNotIn(second["units"][0]["text"], region_body(old.read_text(), "body"))
        self.assertEqual(publisher.resolve("window"), publisher.resolve("stable-cat"))
        outputs = [{"item_id": item_id, "kind": "story", "title": title, "identity_scope": {"scene": title},
                    "observation_ids": [observation["observation_id"]]}
                   for item_id, title, observation in (("box-phase", "箱子阶段", first), ("window-phase", "窗边阶段", second))]
        self.canonical_actions(store, [{"action_key": "split-phases", "type": "SPLIT_ITEM", "item_id": "stable-cat",
                                       "reason": "两个阶段分别阅读", "outputs": outputs}])
        for item_id in ("box-phase", "window-phase"):
            snapshot = adapter.snapshot(item_id)
            publisher.publish_item(snapshot, self.plan(snapshot))
        hub = publisher.publish_identity_redirect(store, "stable-cat")
        self.assertEqual(publisher.resolve("window"), hub.path)
        self.assertEqual(publisher.read(hub.path).decode().count("[[WGO/条目/"), 2)
        renamed = publisher.root / "阶段选择.md"
        (publisher.root / hub.path).rename(renamed)
        self.assertEqual(publisher.resolve("window"), "阶段选择.md")

    def test_claim_status_and_source_grade_are_structural_inputs(self):
        store, _observation, span_id = self.canonical_story()
        adapter = CanonicalReadingAdapter(store)
        item = store.get_item("stable-cat")
        item["claims"] = [{"claim_id": "old-claim", "status": "retracted", "span_ids": [span_id]}]
        source_getter = store.get_source
        with patch.object(store, "get_item", return_value=item), patch.object(store, "get_source", side_effect=lambda id_: {
                **source_getter(id_), "provenance_grade": "derived_summary"}):
            snapshot = adapter.snapshot("stable-cat")
        self.assertEqual(snapshot.evidence[0].label, "派生摘要")
        self.assertEqual(snapshot.units[0].status, "retracted")
        self.assertEqual(snapshot.units[0].verbatim, (snapshot.units[0].text,))
        valid = self.plan(snapshot)
        with self.assertRaisesRegex(ReadingError, "body_plan_lost_claim_status"):
            validate_body_plan(snapshot, replace(valid, blocks=(replace(valid.blocks[0], role="current", unit_statuses={}),)))
        publisher = ProjectionPublisher.for_store(store, self.base / "actual-WGO", file_lock=MacOSFileLock())
        receipt = publisher.publish_item(snapshot, valid)
        self.assertIn("已撤回", publisher.read(receipt.path).decode())
        item["observations"][0]["role"] = "evidence_only"
        with patch.object(store, "get_item", return_value=item):
            evidence_only = adapter.snapshot("stable-cat")
        self.assertFalse(evidence_only.units)
        self.assertEqual(evidence_only.evidence[0].id, span_id)


if __name__ == "__main__":
    unittest.main()
