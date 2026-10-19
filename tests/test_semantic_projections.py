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
    bind_reading_policy, CONTENT_READING_POLICY, SOURCE_READING_POLICY,
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

    def test_json_mode_body_and_repair_both_declare_json_instructions(self):
        snapshot = self.snapshot()
        good = {"blocks": [{"id": "home", "role": "narrative",
                           "text": snapshot.units[0].text, "unit_indices": [0]}]}
        def guarded(value):
            def response(prompt):
                self.assertRegex(json.loads(prompt)["task"], r"(?i)\bjson\b")
                return value
            return response
        provider = ScriptedTextProvider(guarded({"blocks": []}), guarded(good))
        plan = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger).plan(
            snapshot, request_id="json-mode-body")
        self.assertEqual(plan.blocks[0].unit_ids, ("story",))
        self.assertEqual(len(provider.calls), 2)

    def canonical_story(self):
        from core.semantic.store import SemanticStore
        store = SemanticStore(self.base / "actual.sqlite", "canonical-gen", now_func=lambda: 1791849600.0)
        observation, span_id = self.canonical_extract(store, "capture", "周五，猫躺在箱子里。")
        self.canonical_actions(store, [{"action_key": "create-item", "type": "CREATE_ITEM", "item_id": "stable-cat",
            "kind": "life", "title": "箱猫", "identity_scope": {"scene": "同一只猫"},
            "observation_ids": [observation["observation_id"]]}])
        return store, observation, span_id

    def canonical_extract(self, store, batch_id, source_text, *, claims=None, payload=None,
                          source_namespace="synthetic", chat_id="synthetic-chat", unit_kind="scene", unit_text=None):
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
            "units": [{"kind": unit_kind, "text": source_text if unit_text is None else unit_text, "span_indices": [0]}], "topics": [], "claims": claims or []}]},
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
    def responsibility_snapshot(self, *, fresh):
        from core.semantic.store import SemanticStore
        store = SemanticStore(self.base / ("fresh.sqlite" if fresh else "legacy.sqlite"),
                              "fresh" if fresh else "legacy", now_func=lambda: 1791849600.0)
        if fresh:
            with store.transaction() as conn:
                bind_reading_policy(conn, {"reading_policy": CONTENT_READING_POLICY})
        material = (
            ("step", "先停服务，再备份数据库。", "阿禾给出的操作顺序：停掉服务，然后对数据库做一次备份。"),
            ("step", "备份完成后，把配置格式切为 Y，再启动服务。", "阿禾补充：完成备份是前提，随后切换配置为 Y，最后启动服务。"),
            ("counterevidence", "大文件仍会失败，只在小文件验证过。", "阿岚报告这个办法有范围限制：目前小文件成功，大文件仍失败。"),
            ("fact", "所有文件都可用的旧结论撤回。", "阿禾更正了此前所有文件都能处理的说法，明确撤回。"),
            ("fact", "日志文件已上传，先看失败行。", "阿岚分享日志文件，并建议先查看其中失败的行。"),
        )
        observations, spans = [], []
        for index, (kind, source, derived) in enumerate(material):
            observation, span = self.canonical_extract(store, "capture-" + str(index), source,
                unit_kind=kind, unit_text=derived)
            observations.append(observation)
            spans.append(span)
            with store.transaction() as conn:
                conn.execute("INSERT INTO resource_refs VALUES(?,?,?,?,?,?,?,?)", (
                    "resource-" + str(index), observation["observation_id"], None, "occ-" + str(index),
                    "file", "pending", None, json.dumps({"label": "操作记录" + str(index)})))
        self.canonical_actions(store, [{"action_key": "create", "type": "CREATE_ITEM", "item_id": "procedure",
            "kind": "technical", "title": "Y 格式恢复步骤", "identity_scope": {"subject": "Y 恢复"},
            "observation_ids": [o["observation_id"] for o in observations]}])
        item = store.get_item("procedure")
        item["claims"] = [{"claim_id": "contrary", "status": "contested", "span_ids": [spans[2]]},
                          {"claim_id": "old", "status": "retracted", "span_ids": [spans[3]]}]
        with patch.object(store, "get_item", return_value=item):
            snapshot = CanonicalReadingAdapter(store).snapshot("procedure")
        response = {"blocks": [{"id": "procedure-home", "role": "counterevidence", "unit_indices": list(range(5)),
            "text": "先停服务并备份数据库；备份结束后改用 Y 格式配置，再启动服务。"
                    "目前只验证过小文件，大文件仍失败，所有文件都可用的旧结论已撤回。"
                    "日志文件已经上传，应先查看失败行。"}]}
        return store, snapshot, response, material

    def test_fresh_content_responsibilities_allow_faithful_reorganization_with_exact_sources(self):
        store, snapshot, response, material = self.responsibility_snapshot(fresh=True)
        self.assertEqual([unit.verbatim for unit in snapshot.units], [()] * 5)
        self.assertEqual({unit.text for unit in snapshot.units}, {row[2] for row in material})
        self.assertEqual({e.text for e in snapshot.evidence}, {row[1] for row in material})
        self.assertEqual({u.text: u.status for u in snapshot.units}, {row[2]: status for row, status in zip(
            material, ("current", "current", "contested", "retracted", "current"))})
        provider = ScriptedTextProvider(response)
        plan = BodyPlanWriter(provider, store.generation_id).plan(snapshot, request_id="faithful-fresh")
        block = plan.blocks[0]
        self.assertEqual(block.unit_ids, tuple(u.id for u in snapshot.units))
        self.assertEqual(set(block.evidence_ids), {e.id for e in snapshot.evidence})
        self.assertEqual(set(block.resource_ids), {r.id for r in snapshot.resources})
        self.assertEqual(block.unit_statuses, {u.id: u.status for u in snapshot.units if u.status != "current"})
        publisher = ProjectionPublisher.for_store(store, self.base / "fresh-WGO", file_lock=MacOSFileLock())
        publisher.publish_item(snapshot, plan)
        for evidence in snapshot.evidence:
            page = publisher.ledger.one("SELECT page_number FROM reader_source_locations WHERE evidence_id=?", (evidence.id,))[0]
            self.assertIn(evidence.text, publisher.read("来源/procedure-" + str(page).zfill(4) + ".md").decode())

    def test_legacy_derived_wording_remains_literal_and_fresh_explicit_and_quotes_stay_strict(self):
        store, old, response, _material = self.responsibility_snapshot(fresh=False)
        self.assertEqual(sum(bool(unit.verbatim) for unit in old.units), 4)
        with self.assertRaisesRegex(ReadingError, "body_plan_lost_evidence"):
            BodyPlanWriter(ScriptedTextProvider(response), store.generation_id).plan(old, request_id="legacy-paraphrase")
        _store, fresh, response, _material = self.responsibility_snapshot(fresh=True)
        explicit = replace(fresh, units=(replace(fresh.units[0], verbatim=(fresh.evidence[0].text,)),) + fresh.units[1:])
        with self.assertRaisesRegex(ReadingError, "body_plan_lost_evidence"):
            BodyPlanWriter(ScriptedTextProvider(response), _store.generation_id).plan(explicit, request_id="explicit-deleted")
        invented = {"blocks": [{**response["blocks"][0], "text": "> 先删数据库再停服务。"}]}
        with self.assertRaisesRegex(ReadingError, "markdown_quote_not_grounded"):
            BodyPlanWriter(ScriptedTextProvider(invented), _store.generation_id).plan(fresh, request_id="invented-quote")
    def test_fresh_oversized_derived_step_is_editable_without_losing_bounded_input_or_home(self):
        text = "\n".join("派生说明：执行步骤" + str(i) + "，在 Y 模式下验证结果" + str(i) + "。" for i in range(400))
        snapshot = replace(self.snapshot(), reading_policy="content-responsibility-v1",
            units=(ReadingUnit("steps", text, ("step-e",), ("log",), role="step"),),
            evidence=(EvidenceExcerpt("step-e", "完整的步骤来源。" * 500, "相关上下文。" * 200),),
            resources=(ReadingResource("log", "步骤日志"),))
        def edit(prompt):
            data = json.loads(prompt)
            response = self.paged_response(prompt)
            for block in response["blocks"]:
                block["text"] = block["text"].replace("派生说明：", "操作：")
            return response
        provider = ScriptedTextProvider(*([edit] * 300))
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=9000)
        plan = writer.plan(snapshot, request_id="fresh-oversized")
        self.assertEqual(plan.blocks[0].unit_ids, ("steps",))
        self.assertEqual(plan.blocks[0].evidence_ids, ("step-e",))
        self.assertEqual(plan.blocks[0].resource_ids, ("log",))
        self.assertNotEqual(plan.blocks[0].text, text)
        self.assertIn("操作：", plan.blocks[0].text)
        prompts = [json.loads(call[0]) for call in provider.calls]
        supplied = [u["text"] for p in prompts for u in p["snapshot"]["units"]]
        self.assertEqual("".join(supplied), text)
        self.assertTrue(all(not u["verbatim"] for p in prompts for u in p["snapshot"]["units"]))
        self.assertTrue(all(len(call[0].encode()) <= 9000 for call in provider.calls))
        self.assertEqual("".join(e["text"] for p in prompts for e in p["snapshot"]["evidence"]), snapshot.evidence[0].text)
        self.assertEqual("".join(e["context"] for p in prompts for e in p["snapshot"]["evidence"]), snapshot.evidence[0].context)
        count = len(provider.calls)
        self.assertEqual(writer.plan(snapshot, request_id="fresh-oversized"), plan)
        self.assertEqual(len(provider.calls), count)
        row = self.ledger.one("SELECT prompt_version FROM reader_text_artifacts WHERE request_id='fresh-oversized'")
        self.assertEqual(row[0], "body-paged-v12")

    def test_v12_prompt_bytes_remain_frozen_in_all_three_composition_shapes(self):
        snapshot = replace(self.snapshot(), reading_policy=CONTENT_READING_POLICY)
        previous = self.plan(snapshot)
        cases = ((None, {}, {}, 7555, "5c1ef9ba5a0ee5009e5a5c74fc7301dd08b02a935b64ed7110009293ce8d86f6"),
                 (previous, {b.id: b for b in previous.blocks}, {}, 5671,
                  "d404cbbcc2c9aa7e745c18799b7de58aa192061dd93e3530ec157ce344b9fee6"),
                 (None, {}, {"phase": "body_fragment_v1"}, 5340,
                  "71802c0fad9fb92ac3e5557f274091b0574028a3daabc9c8a3ea4d25fa286919"))
        for prior, unchanged, context, size, expected in cases:
            prompt = BodyPlanWriter._prompt(snapshot, prior, unchanged, **context)
            self.assertEqual(len(prompt.encode()), size)
            self.assertEqual(fingerprint(prompt), expected)

    def test_source_composition_hides_machine_prose_and_budgets_only_the_actual_payload(self):
        derived = "AGENT-DERIVED-REPORT-WRAPPER" * 10000
        snapshot = replace(self.snapshot(), reading_policy=SOURCE_READING_POLICY, title="AGENT-DERIVED-TITLE",
            units=(ReadingUnit("step", derived, ("e1",), ("log",), role="step"),),
            evidence=(EvidenceExcerpt("e1", "先停服务，备份后再切换 Y 格式。", "仅在小文件验证过。",
                                      "2026-10-09T12:00:00+08:00", speaker_id="speaker", speaker_name="阿禾"),),
            resources=(ReadingResource("log", "操作日志"),))
        def compose(prompt):
            self.assertNotIn("AGENT-DERIVED", prompt)
            data = json.loads(prompt)
            self.assertNotIn("title", data["snapshot"])
            self.assertNotIn("text", data["snapshot"]["units"][0])
            self.assertEqual(data["snapshot"]["evidence"][0], asdict(snapshot.evidence[0]))
            self.assertEqual(data["snapshot"]["units"][0]["evidence_ids"], ["e1"])
            self.assertEqual(data["snapshot"]["units"][0]["resource_ids"], ["log"])
            return {"blocks": [{"id": "procedure", "role": "narrative", "unit_indices": [0],
                "text": "先停服务，完成备份后再切换到 Y 格式；目前只验证过小文件。"}]}
        provider = ScriptedTextProvider(compose)
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=8000)
        plan = writer.plan(snapshot, request_id="source-only")
        self.assertEqual(len(provider.calls), 1)
        self.assertLessEqual(len(provider.calls[0][0].encode()), 8000)
        self.assertEqual(plan.unit_fingerprints["step"], fingerprint(json.dumps(asdict(snapshot.units[0]),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
        self.assertEqual(plan.blocks[0].unit_ids, ("step",))
        self.assertEqual(plan.blocks[0].resource_ids, ("log",))
        self.assertEqual(self.ledger.one("SELECT prompt_version FROM reader_text_artifacts WHERE request_id='source-only'")[0],
                         "body-plan-v13")
        self.assertEqual(writer.plan(snapshot, request_id="source-only"), plan)
        self.assertEqual(len(provider.calls), 1)

    @staticmethod
    def source_response(prompt):
        snapshot = json.loads(prompt)["snapshot"]
        units = snapshot["units"]
        if not units:
            return {"blocks": []}
        literal = [fragment for unit in units for fragment in unit["verbatim"]]
        text = "".join(literal) if literal else "\n".join(e["text"] or e["context"] for e in snapshot["evidence"]) or "来源关联资源。"
        return {"blocks": [{"id": "source-home", "role": "counterevidence" if any(u["status"] != "current" for u in units) else "narrative",
                            "text": text, "unit_indices": list(range(len(units)))}]}

    def test_source_only_pages_preserve_all_mapping_and_supplemental_sources(self):
        units = tuple(ReadingUnit("u-" + str(i), "HIDDEN-DERIVED" * 1000, ("e-" + str(i),), ("r-" + str(i),)) for i in range(30))
        evidence = tuple(EvidenceExcerpt("e-" + str(i), "源材料的完整事实与限制。" * 25 + str(i)) for i in range(30))
        evidence += (EvidenceExcerpt("supplement", "另一条保留的更正证据。"),)
        snapshot = replace(self.snapshot(), reading_policy=SOURCE_READING_POLICY, title="HIDDEN-TITLE", units=units, evidence=evidence,
            resources=tuple(ReadingResource("r-" + str(i), "日志" + str(i)) for i in range(30)), identity_scope={"subject": "Y 格式"})
        def compose(prompt):
            self.assertNotIn("HIDDEN", prompt)
            self.assertEqual(json.loads(prompt)["snapshot"]["identity_scope"], snapshot.identity_scope)
            return self.source_response(prompt)
        provider = ScriptedTextProvider(*([compose] * 100))
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=9000)
        plan = writer.plan(snapshot, request_id="source-pages")
        self.assertGreater(len(provider.calls), 1)
        self.assertEqual({u for block in plan.blocks for u in block.unit_ids}, {u.id for u in units})
        self.assertEqual({e["id"] for call in provider.calls for e in json.loads(call[0])["snapshot"]["evidence"]}, {e.id for e in evidence})
        self.assertEqual({r for block in plan.blocks for r in block.resource_ids}, {r.id for r in snapshot.resources})
        self.assertTrue(all(len(call[0].encode()) <= 9000 for call in provider.calls))
        self.assertEqual(self.ledger.one("SELECT prompt_version FROM reader_text_artifacts WHERE request_id='source-pages'")[0], "body-paged-v13")

    def test_oversized_source_windows_pack_adjacent_records_without_context_narrative(self):
        excerpts = tuple(EvidenceExcerpt("window-e-" + str(i), "来源" + str(i) + "的具体事实。" * 15,
            "此前的限定。" + "来源" + str(i) + "的具体事实。" * 15 + "后续范围仍待验证。",
            "2026-10-09T12:" + str(i).zfill(2) + ":00+08:00") for i in range(30))
        snapshot = replace(self.snapshot(), reading_policy=SOURCE_READING_POLICY, title="HIDDEN-TITLE",
            units=(ReadingUnit("whole", "HIDDEN-DERIVED" * 1000, tuple(e.id for e in excerpts)),), evidence=excerpts)
        observed = []
        def compose(prompt):
            data = json.loads(prompt)
            self.assertNotIn("HIDDEN", prompt)
            observed.append(data)
            core = [e for e in data["snapshot"]["evidence"] if e["text"]]
            if not core:
                self.assertEqual(data["required_unit_indices"], [])
                return {"blocks": []}
            self.assertTrue(all(e["context"] for e in core))
            return {"blocks": [{"id": "window-home", "role": "narrative",
                "text": "\n".join(e["text"] for e in core), "unit_indices": [0]}]}
        provider = ScriptedTextProvider(*([compose] * 100))
        plan = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=8000).plan(
            snapshot, request_id="bounded-source-windows")
        self.assertGreater(len(provider.calls), 1)
        self.assertLess(len(provider.calls), len(excerpts))
        self.assertTrue(any(len(data["snapshot"]["evidence"]) > 1 for data in observed))
        self.assertEqual(len(plan.blocks), 1)
        self.assertEqual(plan.blocks[0].unit_ids, ("whole",))
        supplied = [e for data in observed for e in data["snapshot"]["evidence"]]
        for original in excerpts:
            self.assertEqual("".join(e["text"] for e in supplied if e["id"] == original.id), original.text)
            self.assertEqual("".join(e["context"] for e in supplied if e["id"] == original.id), original.context)
            self.assertEqual(plan.blocks[0].text.count(original.text), 1)
        self.assertTrue(all(len(call[0].encode()) <= 8000 for call in provider.calls))

    def test_single_oversized_source_keeps_context_tail_supporting_without_new_narrative(self):
        original = EvidenceExcerpt("one-source", "唯一的核心事实。", "完整的支持上下文。" * 2000,
                                   "2026-10-09T12:00:00+08:00")
        snapshot = replace(self.snapshot(), reading_policy=SOURCE_READING_POLICY,
            units=(ReadingUnit("one", "HIDDEN derived", (original.id,)),), evidence=(original,))
        windows = []
        def compose(prompt):
            data = json.loads(prompt)
            windows.append(data)
            if not any(e["text"] for e in data["snapshot"]["evidence"]):
                self.assertEqual(data["required_unit_indices"], [])
                self.assertEqual(data["snapshot"]["units"], [])
                self.assertEqual(data["continuation"]["core_evidence_ids"], [])
                self.assertEqual(data["continuation"]["context_only_evidence_ids"], [original.id])
                return {"blocks": []}
            return {"blocks": [{"id": "one-home", "role": "narrative", "text": original.text, "unit_indices": [0]}]}
        provider = ScriptedTextProvider(*([compose] * 100))
        plan = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=8000).plan(
            snapshot, request_id="context-tail")
        self.assertGreater(sum(not data["required_unit_indices"] for data in windows), 0)
        self.assertEqual(plan.blocks[0].text, original.text)
        self.assertEqual(plan.blocks[0].unit_ids, ("one",))
        supplied = [e for data in windows for e in data["snapshot"]["evidence"]]
        self.assertEqual("".join(e["text"] for e in supplied), original.text)
        self.assertEqual("".join(e["context"] for e in supplied), original.context)
        self.assertTrue(all(len(call[0].encode()) <= 8000 for call in provider.calls))

    def test_source_oversized_evidence_uses_raw_chronology_and_one_existing_home(self):
        snapshot = replace(self.snapshot(), reading_policy=SOURCE_READING_POLICY, title="HIDDEN-TITLE",
            units=(ReadingUnit("later", "HIDDEN-DERIVED" * 1000, ("later-e",), ("log",), status="contested"),
                   ReadingUnit("earlier", "HIDDEN-DERIVED" * 1000, ("earlier-e",))),
            evidence=(EvidenceExcerpt("later-e", "后来的完整来源。" * 1000, "后来的完整上下文。" * 100,
                                      "2026-10-09T13:00:00+08:00", speaker_name="阿禾"),
                      EvidenceExcerpt("earlier-e", "此前的完整来源。" * 1000, "此前的完整上下文。" * 100,
                                      "2026-10-09T12:00:00+08:00", speaker_name="阿岚")),
            resources=(ReadingResource("log", "日志"),))
        previous = BodyPlan(snapshot.item_id, 1, (BodyBlock("stable", "narrative", "旧自动正文。",
            ("later", "earlier"), ("later-e", "earlier-e"), ("log",), snapshot.item_id),))
        def compose(prompt):
            self.assertNotIn("HIDDEN", prompt)
            self.assertTrue(all("text" not in unit for unit in json.loads(prompt)["snapshot"]["units"]))
            return self.source_response(prompt)
        provider = ScriptedTextProvider(*([compose] * 500))
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=8000)
        plan = writer.plan(snapshot, previous=previous, request_id="source-huge")
        self.assertEqual(len(plan.blocks), 1)
        self.assertEqual(plan.blocks[0].id, "stable")
        self.assertEqual(plan.blocks[0].unit_ids, ("later", "earlier"))
        self.assertEqual(plan.blocks[0].unit_statuses, {"later": "contested"})
        supplied = [e for call in provider.calls for e in json.loads(call[0])["snapshot"]["evidence"]]
        self.assertEqual(list(dict.fromkeys(e["id"] for e in supplied)), ["earlier-e", "later-e"])
        for original in snapshot.evidence:
            self.assertEqual("".join(e["text"] for e in supplied if e["id"] == original.id), original.text)
            self.assertEqual("".join(e["context"] for e in supplied if e["id"] == original.id), original.context)
        self.assertTrue(all(len(call[0].encode()) <= 8000 for call in provider.calls))
        count = len(provider.calls)
        self.assertEqual(writer.plan(snapshot, previous=previous, request_id="source-huge"), plan)
        self.assertEqual(len(provider.calls), count)

    def test_source_explicit_oversized_literals_and_no_empty_hidden_prose_calls(self):
        literal = "真正显式的原话必须准确。" * 300
        snapshot = replace(self.snapshot(), reading_policy=SOURCE_READING_POLICY,
            units=(ReadingUnit("explicit", "HIDDEN-DERIVED" * 1000, (), verbatim=(literal,)),), evidence=())
        provider = ScriptedTextProvider(*([self.source_response] * 100))
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=8000)
        plan = writer.plan(snapshot, request_id="source-literal")
        self.assertEqual(plan.blocks[0].text, literal)
        self.assertTrue(all(":verbatim:" in call[1] for call in provider.calls))
        self.assertTrue(all("HIDDEN" not in call[0] and len(call[0].encode()) <= 8000 for call in provider.calls))
        missing = replace(snapshot, units=(replace(snapshot.units[0], verbatim=()),))
        unused = ScriptedTextProvider()
        with self.assertRaisesRegex(ReadingError, "body_source_material_missing"):
            BodyPlanWriter(unused, self.ledger.generation_id).plan(missing, request_id="empty-hidden")
        self.assertEqual(unused.calls, [])
        def omit(prompt):
            result = self.source_response(prompt)
            result["blocks"][0]["text"] = "删掉显式原话。"
            return result
        bad = ScriptedTextProvider(*([omit] * 10))
        with self.assertRaisesRegex(ReadingError, "body_plan_lost_evidence"):
            BodyPlanWriter(bad, self.ledger.generation_id, self.ledger, max_input_bytes=8000).plan(
                replace(snapshot, item_id="bad-literal"), request_id="bad-source-literal")
        self.assertEqual(len(bad.calls), 2)

    def test_source_policy_preserves_known_repair_and_cross_version_unknown_identity(self):
        snapshot = replace(self.snapshot(), reading_policy=SOURCE_READING_POLICY)
        provider = ScriptedTextProvider({"blocks": []}, self.source_response)
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger)
        plan = writer.plan(snapshot, request_id="source-repair")
        before = [dict(row) for row in self.ledger.all("SELECT * FROM reader_text_artifacts ORDER BY rowid")]
        self.assertEqual([row["status"] for row in before], ["failed", "accepted"])
        self.assertTrue(all(row["prompt_version"] == "body-plan-v13" for row in before))
        self.assertEqual(writer.plan(snapshot, request_id="source-repair"), plan)
        self.assertEqual(len(provider.calls), 2)
        for version in ("body-plan-v11", "body-plan-v12"):
            for status in ("unknown", "running"):
                root = "source-guard-" + version + status
                with self.ledger.transaction() as conn:
                    conn.execute("INSERT INTO reader_text_artifacts VALUES(?,?,?,?,?,?,?)", (
                        self.ledger.generation_id, root, root + ":group:0:fragment:0", "route", version, status, ""))
                unused = ScriptedTextProvider()
                with self.assertRaisesRegex(ReadingError, "text_provider_outcome_unknown"):
                    BodyPlanWriter(unused, self.ledger.generation_id, self.ledger).plan(snapshot, request_id=root)
                self.assertEqual(unused.calls, [])
        self.assertEqual([dict(row) for row in self.ledger.all(
            "SELECT * FROM reader_text_artifacts WHERE request_id=? OR request_id LIKE ? ORDER BY rowid",
            ("source-repair", "source-repair:repair:%"))], before)

    def test_source_increment_keeps_unaffected_bytes_and_canonical_mapping(self):
        snapshot = replace(self.snapshot(), reading_policy=SOURCE_READING_POLICY,
            units=(ReadingUnit("step", "HIDDEN step prose", ("e1",), role="step"),
                   ReadingUnit("other", "HIDDEN unrelated prose", ("e2",))),
            evidence=(EvidenceExcerpt("e1", "先停服务。"), EvidenceExcerpt("e2", "周末散步。")))
        first = BodyPlanWriter(ScriptedTextProvider({"blocks": [
            {"id": "procedure", "role": "narrative", "text": "先停服务。", "unit_indices": [0]},
            {"id": "other-home", "role": "narrative", "text": "周末散步。", "unit_indices": [1]}]}),
            self.ledger.generation_id).plan(snapshot, request_id="source-before")
        published = self.publisher.publish_item(snapshot, first)
        new = replace(snapshot, content_revision=2, units=snapshot.units + (ReadingUnit("backup", "HIDDEN new prose", ("e3",)),),
            evidence=snapshot.evidence + (EvidenceExcerpt("e3", "停服务后备份数据库。"),))
        self.revisions[snapshot.item_id] = 2
        def rewrite(prompt):
            self.assertNotIn("HIDDEN", prompt)
            return {"blocks": [
                {"id": "procedure", "role": "narrative", "text": "停服务后备份数据库。", "unit_indices": [0, 2]},
                {"id": "other-home", "role": "narrative", "text": "周末散步。", "unit_indices": [1]}]}
        provider = ScriptedTextProvider(rewrite)
        plan = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger).plan(new, previous=first, request_id="source-after")
        self.assertEqual(plan.blocks[1], first.blocks[1])
        self.assertEqual(plan.blocks[0].unit_ids, ("step", "backup"))
        target = self.publisher.root / published.path
        manual = target.read_bytes().replace("先停服务。".encode(), "用户手改正文。".encode())
        target.write_bytes(manual)
        self.assertEqual(self.publisher.publish_item(new, plan).status, "conflict")
        self.assertEqual(target.read_bytes(), manual)

    def test_content_policy_binding_cannot_silently_become_source_composition(self):
        from core.semantic.store import SemanticStore
        store = SemanticStore(self.base / "frozen-content.sqlite", "frozen-content")
        with store.transaction() as conn:
            bind_reading_policy(conn, {"reading_policy": CONTENT_READING_POLICY})
            before = conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0]
            with self.assertRaisesRegex(ReadingError, "reading_policy_mismatch"):
                bind_reading_policy(conn, {"reading_policy": SOURCE_READING_POLICY})
            self.assertEqual(conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0], before)
        fresh = SemanticStore(self.base / "new-source.sqlite", "new-source")
        with fresh.transaction() as conn:
            bind_reading_policy(conn, {"reading_policy": SOURCE_READING_POLICY})
        observation, _span = self.canonical_extract(fresh, "raw", "先停服务，再备份。", unit_kind="step", unit_text="HIDDEN canonical derived text")
        self.canonical_actions(fresh, [{"action_key": "create", "type": "CREATE_ITEM", "item_id": "procedure", "title": "HIDDEN title",
            "kind": "technical", "identity_scope": {"subject": "恢复服务"}, "observation_ids": [observation["observation_id"]]}])
        actual = CanonicalReadingAdapter(fresh).snapshot("procedure")
        self.assertEqual(actual.units[0].text, "HIDDEN canonical derived text")
        self.assertEqual(actual.units[0].verbatim, ())
        self.assertEqual(actual.identity_scope, {"subject": "恢复服务"})
        self.assertNotIn("HIDDEN", BodyPlanWriter._prompt(actual, None, {}))
        with fresh.transaction() as conn:
            self.assertEqual(bind_reading_policy(conn, {"reading_policy": SOURCE_READING_POLICY})["reading_policy"], SOURCE_READING_POLICY)

    def test_source_paged_increment_delivers_supplemental_after_unchanged_groups(self):
        units = tuple(ReadingUnit("u-" + str(i), "HIDDEN derived", ("e-" + str(i),)) for i in range(30))
        evidence = tuple(EvidenceExcerpt("e-" + str(i), "完整的来源正文。" * 40 + str(i)) for i in range(30))
        first = replace(self.snapshot(), reading_policy=SOURCE_READING_POLICY, units=units, evidence=evidence)
        previous = validate_body_plan(first, BodyPlan(first.item_id, 1, tuple(BodyBlock(
            "home-" + str(i), "narrative", evidence[i].text, (unit.id,), unit.evidence_ids, primary_home=first.item_id)
            for i, unit in enumerate(units))))
        extra = ReadingUnit("new", "HIDDEN new derived", ("new-e",))
        updated = replace(first, content_revision=2, units=units + (extra,),
            evidence=evidence + (EvidenceExcerpt("new-e", "新增的具体条件。"), EvidenceExcerpt("supplement", "关联的更正原文。")))
        def compose(prompt):
            self.assertNotIn("HIDDEN", prompt)
            data = json.loads(prompt)
            self.assertIn("supplement", {e["id"] for e in data["snapshot"]["evidence"]})
            self.assertEqual(len(data["previous"]["blocks"]), 1)
            return {"blocks": [{"id": data["previous"]["blocks"][0]["id"], "role": "narrative",
                "text": evidence[-1].text + "新增的具体条件。", "unit_indices": [0, 1]}]}
        provider = ScriptedTextProvider(compose)
        plan = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=8000).plan(
            updated, previous=previous, request_id="source-paged-increment")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(plan.blocks[:-1], previous.blocks[:-1])
        self.assertEqual(plan.blocks[-1].id, previous.blocks[-1].id)
        self.assertEqual(plan.blocks[-1].unit_ids, (units[-1].id, "new"))

    def test_source_fragment_unknown_blocks_new_budget_without_another_call(self):
        snapshot = replace(self.snapshot(), reading_policy=SOURCE_READING_POLICY,
            units=(ReadingUnit("raw", "HIDDEN derived", ("raw-e",)),),
            evidence=(EvidenceExcerpt("raw-e", "真实来源内容。" * 2000),))
        provider = ScriptedTextProvider(RuntimeError("synthetic unknown source fragment"))
        with self.assertRaisesRegex(RuntimeError, "synthetic unknown source fragment"):
            BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=8000).plan(snapshot, request_id="raw-unknown")
        row = self.ledger.one("SELECT * FROM reader_text_artifacts WHERE request_id LIKE 'raw-unknown:%'")
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["prompt_version"], "body-plan-v13")
        self.assertIn(":group:0:source:0", row["request_id"])
        unused = ScriptedTextProvider()
        with self.assertRaisesRegex(ReadingError, "text_provider_outcome_unknown"):
            BodyPlanWriter(unused, self.ledger.generation_id, self.ledger).plan(snapshot, request_id="raw-unknown")
        self.assertEqual(unused.calls, [])
        self.assertEqual(len(provider.calls), 1)

    def test_legacy_prompt_bytes_and_paged_identity_omit_the_new_policy_field(self):
        snapshot = self.snapshot()
        previous = self.plan(snapshot)
        cases = ((None, {}, {}, "246c56e467841d13c8155b1f85bd74960590ac7a5072fa7a194aa8c3a0b2d4fc"),
                 (previous, {b.id: b for b in previous.blocks}, {}, "4a8cc9a9b896cb46edf4706527269d106f0f613c3b21d57e175f08761a008f64"),
                 (None, {}, {"phase": "body_fragment_v1"}, "18dd55db1123248a9ff30348a3f1f92841799bfb81f0e5c572a024609ee81594"))
        for prior, unchanged, context, expected in cases:
            prompt = BodyPlanWriter._prompt(snapshot, prior, unchanged, **context)
            self.assertNotIn("reading_policy", json.loads(prompt)["snapshot"])
            self.assertEqual(fingerprint(prompt), expected)
        from core.semantic.projections import reading_snapshot_payload
        expected_payload = asdict(snapshot)
        expected_payload.pop("reading_policy")
        expected_payload.pop("identity_scope")
        self.assertEqual(reading_snapshot_payload(snapshot), expected_payload)
        legacy_key = fingerprint(json.dumps(expected_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        self.assertEqual(fingerprint(json.dumps(reading_snapshot_payload(snapshot), ensure_ascii=False,
                                              sort_keys=True, separators=(",", ":"))), legacy_key)

    def test_reading_policy_binding_preserves_populated_legacy_and_rejects_rebinding(self):
        from core.semantic.store import SemanticStore
        store, _observation, _span = self.canonical_story()
        with store.transaction() as conn:
            before = conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0]
            self.assertNotIn("reading_policy", bind_reading_policy(conn, {}))
            self.assertEqual(conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0], before)
            with self.assertRaisesRegex(ReadingError, "reading_policy_mismatch"):
                bind_reading_policy(conn, {"reading_policy": CONTENT_READING_POLICY})
            self.assertEqual(conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0], before)
        for status in ("accepted", "failed", "pending", "running", "unknown"):
            with self.subTest(status=status):
                candidate = SemanticStore(self.base / (status + ".sqlite"), status)
                ledger = ReadingLedger(candidate.connect, candidate.generation_id)
                with candidate.transaction() as conn:
                    conn.execute("INSERT INTO reader_text_artifacts VALUES(?,?,?,?,?,?,?)",
                                 (status, "job", "request", "route", "body-plan-v11", status, ""))
                    before = conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0]
                    with self.assertRaisesRegex(ReadingError, "reading_policy_mismatch"):
                        bind_reading_policy(conn, {"reading_policy": CONTENT_READING_POLICY})
                    self.assertNotIn("reading_policy", bind_reading_policy(conn, {}))
                    self.assertEqual(conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0], before)
        fresh = SemanticStore(self.base / "empty.sqlite", "empty")
        with fresh.transaction() as conn:
            self.assertEqual(bind_reading_policy(conn, {"reading_policy": CONTENT_READING_POLICY})["reading_policy"],
                             CONTENT_READING_POLICY)
            before = conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0]
            self.assertEqual(bind_reading_policy(conn, {"reading_policy": CONTENT_READING_POLICY})["reading_policy"],
                             CONTENT_READING_POLICY)
            with self.assertRaisesRegex(ReadingError, "reading_policy_mismatch"):
                bind_reading_policy(conn, {})
            self.assertEqual(conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0], before)

    def test_fresh_writer_binding_version_and_unknown_guards_cannot_cross_policy(self):
        store, snapshot, response, _material = self.responsibility_snapshot(fresh=True)
        ledger = ReadingLedger(store.connect, store.generation_id)
        provider = ScriptedTextProvider(response)
        writer = BodyPlanWriter(provider, store.generation_id, ledger)
        plan = writer.plan(snapshot, request_id="fresh-version")
        self.assertEqual(ledger.one("SELECT prompt_version FROM reader_text_artifacts WHERE request_id='fresh-version'")[0],
                         "body-plan-v12")
        with self.assertRaisesRegex(ReadingError, "reading_policy_mismatch"):
            writer.plan(replace(snapshot, reading_policy=None), previous=plan, request_id="legacy-spoof")
        self.assertEqual(len(provider.calls), 1)
        for status in ("unknown", "running"):
            with self.subTest(status=status):
                root = "cross-policy-" + status
                with self.ledger.transaction() as conn:
                    conn.execute("INSERT INTO reader_text_artifacts VALUES(?,?,?,?,?,?,?)", (
                        self.ledger.generation_id, root, root + ":group:0:fragment:0", "route", "body-plan-v11", status, ""))
                fresh = replace(self.snapshot(), reading_policy=CONTENT_READING_POLICY)
                unused = ScriptedTextProvider(self.paged_response)
                with self.assertRaisesRegex(ReadingError, "text_provider_outcome_unknown"):
                    BodyPlanWriter(unused, self.ledger.generation_id, self.ledger, max_input_bytes=9000).plan(fresh, request_id=root)
                self.assertEqual(unused.calls, [])

    def test_fresh_oversized_explicit_literals_require_exact_accepted_continuations(self):
        required = "显式逐字义务，标点和术语不能换。" * 80
        snapshot = replace(self.snapshot(), reading_policy=CONTENT_READING_POLICY,
            units=(ReadingUnit("large", "派生的操作说明。" * 500, ("e1",), role="step", verbatim=(required,)),),
            evidence=(EvidenceExcerpt("e1", required),))
        def response(prompt):
            data = json.loads(prompt)
            if any(":verbatim:" in u["id"] for u in data["snapshot"]["units"]):
                return self.paged_response(prompt)
            result = self.paged_response(prompt)
            for block in result["blocks"]:
                block["text"] = block["text"].replace("派生的", "完整的")
            return result
        provider = ScriptedTextProvider(*([response] * 200))
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=9000)
        plan = writer.plan(snapshot, request_id="large-explicit")
        self.assertIn(required, plan.blocks[0].text)
        literal_calls = [call for call in provider.calls if ":verbatim:" in call[1]]
        self.assertGreater(len(literal_calls), 1)
        self.assertTrue(all(len(call[0].encode()) <= 9000 for call in provider.calls))
        count = len(provider.calls)
        self.assertEqual(writer.plan(snapshot, request_id="large-explicit"), plan)
        self.assertEqual(len(provider.calls), count)
        def omit(prompt):
            result = response(prompt)
            if any(":verbatim:" in u["id"] for u in json.loads(prompt)["snapshot"]["units"]):
                result["blocks"][0]["text"] = "遗漏了显式原文。"
            return result
        # An independent generation/request with an omitted literal must fail,
        # including the sole repair; the program cannot fill it in itself.
        bad_snapshot = replace(snapshot, item_id="bad-explicit")
        bad = ScriptedTextProvider(*([omit] * 200))
        with self.assertRaisesRegex(ReadingError, "body_plan_lost_evidence"):
            BodyPlanWriter(bad, self.ledger.generation_id, self.ledger, max_input_bytes=9000).plan(
                bad_snapshot, request_id="bad-explicit")
        self.assertEqual(len([call for call in bad.calls if ":verbatim:" in call[1]]), 2)

    def test_fresh_increment_keeps_unaffected_bytes_stable_homes_and_manual_conflicts(self):
        for budget in (80000, 9000):
            with self.subTest(budget=budget):
                filler = tuple(ReadingUnit("context-" + str(i), "独立日常记录。" * 40, ("context-e-" + str(i),))
                               for i in range(25)) if budget == 9000 else ()
                units = filler + (ReadingUnit("step", "阿禾的操作说明：先停掉服务。", ("step-e",), role="step"),)
                first = replace(self.snapshot(item_id="fresh-increment-" + str(budget)), reading_policy=CONTENT_READING_POLICY,
                    units=units, evidence=tuple(EvidenceExcerpt(u.evidence_ids[0], u.text) for u in units))
                previous = self.plan(first)
                original = self.publisher.publish_item(first, previous)
                extra = ReadingUnit("backup", "停服务后备份数据库。", ("backup-e",), role="step")
                updated = replace(first, content_revision=2, units=units + (extra,),
                                  evidence=first.evidence + (EvidenceExcerpt("backup-e", extra.text),))
                self.revisions[first.item_id] = 2
                def compose(prompt):
                    data = json.loads(prompt)
                    positions = {u["id"]: i for i, u in enumerate(data["snapshot"]["units"])}
                    return {"blocks": [{"id": b["id"], "role": b["role"],
                        "text": "先停服务，再备份数据库。" if "step" in b["unit_ids"] else b["text"],
                        "unit_indices": [positions[u] for u in b["unit_ids"]] +
                                        ([positions["backup"]] if "step" in b["unit_ids"] else [])}
                        for b in data["previous"]["blocks"]]}
                provider = ScriptedTextProvider(compose)
                plan = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=budget).plan(
                    updated, previous=previous, request_id="fresh-increment-" + str(budget))
                self.assertEqual(plan.blocks[:-1], previous.blocks[:-1])
                self.assertEqual(plan.blocks[-1].id, previous.blocks[-1].id)
                self.assertEqual(plan.blocks[-1].unit_ids, ("step", "backup"))
                target = self.publisher.root / original.path
                manual = target.read_bytes().replace(units[-1].text.encode(), "我的手改正文，保留精确字节。".encode())
                target.write_bytes(manual)
                self.assertEqual(self.publisher.publish_item(updated, plan).status, "conflict")
                self.assertEqual(target.read_bytes(), manual)
                self.assertEqual(len(provider.calls), 1)

    def test_editorial_examples_preserve_required_words_in_one_content_home(self):
        prompt = json.loads(BodyPlanWriter._prompt(self.snapshot(), None, ()))
        self.assertEqual({e["kind"] for e in prompt["writing_examples"]}, {"technical", "life"})
        for example in prompt["writing_examples"]:
            with self.subTest(kind=example["kind"]):
                self.assertTrue(example["illustrative_only"])
                evidence = tuple(EvidenceExcerpt(
                    f"example-e{i}", text, "", "", "")
                    for i, text in enumerate(example["source_excerpts"]))
                units = tuple(ReadingUnit(
                    f"example-u{i}", unit["text"],
                    tuple(f"example-e{j}" for j in unit["evidence_indices"]),
                    role=unit["role"], verbatim=tuple(unit["verbatim"]))
                    for i, unit in enumerate(example["units"]))
                snapshot = replace(self.snapshot(), units=units, evidence=evidence)
                plan = BodyPlanWriter._response(snapshot, json.dumps(example["response"], ensure_ascii=False))
                validate_body_plan(snapshot, plan)
                self.assertEqual(len(plan.blocks), 1)
                self.assertEqual(plan.blocks[0].unit_ids, tuple(unit.id for unit in units))
                for unit in units:
                    for fragment in unit.verbatim:
                        self.assertEqual(plan.blocks[0].text.count(fragment), 1)
                        broken = replace(plan, blocks=(replace(
                            plan.blocks[0], text=plan.blocks[0].text.replace(fragment, "")),))
                        with self.assertRaisesRegex(ReadingError, "body_plan_lost_evidence"):
                            validate_body_plan(snapshot, broken)

    def test_body_verbatim_repair_names_the_actual_obligation(self):
        snapshot = self.snapshot()
        def repair(prompt):
            intro = json.loads(prompt)["task"].split("只返回 schema", 1)[0]
            self.assertIn("body_plan_lost_evidence", intro)
            self.assertIn("逐字复制 unit.verbatim", intro)
            return self.paged_response(prompt)
        provider = ScriptedTextProvider({"blocks": [{"id": "story", "role": "narrative",
            "text": "猫占了纸箱。", "unit_indices": [0]}]}, repair)
        plan = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger).plan(
            snapshot, request_id="verbatim-repair-explanation")
        self.assertEqual(plan.blocks[0].text, snapshot.units[0].text)
        self.assertEqual(len(provider.calls), 2)

    def test_body_source_order_uses_offset_times_without_reordering_unit_homes(self):
        units = (ReadingUnit("late-unit", "后来收到文件。", ("late",)),
                 ReadingUnit("early-unit", "先提出问题。", ("early",)),
                 ReadingUnit("unknown-unit", "时间未知的材料。", ("unknown",)))
        snapshot = replace(self.snapshot(), units=units, evidence=(
            EvidenceExcerpt("late", "后来收到文件。", source_time="2026-10-09T10:10:00+08:00"),
            EvidenceExcerpt("unknown", "时间未知的材料。", source_time="2026-10-09"),
            EvidenceExcerpt("early", "先提出问题。", source_time="2026-10-09T02:00:00+00:00")))
        prompt = json.loads(BodyPlanWriter._prompt(snapshot, None, {}))
        self.assertEqual(prompt["source_sequence"], ["early", "late"])
        self.assertEqual(prompt["undated_evidence"], ["unknown"])
        self.assertEqual([u["id"] for u in prompt["snapshot"]["units"]],
                         [u.id for u in units])
        self.assertEqual(prompt["required_unit_indices"], [0, 1, 2])
        self.assertEqual(snapshot.units, units)

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
        self.assertEqual(root["prompt_version"], "body-paged-v11")
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

    def test_named_quote_attribution_binds_citation_and_keeps_one_source_link(self):
        quote, later = "猫在纸箱里睡着了。", "今天下雨了。"
        snapshot = replace(self.snapshot(item_id="named-quote"),
            units=(ReadingUnit("story", quote, ("e1",), role="quote", verbatim=(quote,)),),
            evidence=(EvidenceExcerpt("e1", quote, "此前刚搬家。", speaker_name="阿甲"),
                      EvidenceExcerpt("e2", later, speaker_name="阿甲")))
        def plan(text):
            return BodyPlan(snapshot.item_id, snapshot.content_revision, (BodyBlock(
                "quoted", "narrative", text, ("story",), ("e1", "e2"), primary_home=snapshot.item_id),))
        valid = "> " + quote + "\n\n—— 阿甲{{evidence:e1}}"
        accepted = validate_body_plan(snapshot, plan(valid))
        self.assertEqual(accepted.blocks[0].text, valid)
        receipt = self.publisher.publish_item(snapshot, accepted)
        body = region_body(self.publisher.read(receipt.path).decode(), "body")
        self.assertIn("—— 阿甲", body)
        self.assertEqual(body.count("#^" + anchor("e1")), 1)
        # The same named speaker citing a different excerpt must reject, not pass
        # because the quotation happens to match some other supplied evidence.
        with self.assertRaisesRegex(ReadingError, "markdown_quote_not_grounded"):
            validate_body_plan(snapshot, plan("> " + quote + "\n\n—— 阿甲{{evidence:e2}}"))
        misnamed = replace(snapshot, evidence=(snapshot.evidence[0], replace(snapshot.evidence[1], text=quote, speaker_name="阿乙")))
        with self.assertRaisesRegex(ReadingError, "markdown_quote_attribution_mismatch"):
            validate_body_plan(misnamed, plan("> " + quote + "\n\n—— 阿甲{{evidence:e2}}"))
        # An ordinary following claim keeps its own citation and is not silently
        # adopted as the quotation's attribution.
        claim = "> " + quote + "\n\n阿甲后来提到另一件事{{evidence:e2}}"
        self.assertEqual(validate_body_plan(snapshot, plan(claim)).blocks[0].text, claim)
        # Unknown authors stay supported: an unattributable following line is not
        # mistaken for a captured display name.
        unknown = replace(snapshot, evidence=(EvidenceExcerpt("e1", quote),))
        vague = "> " + quote + "\n\n—— {{evidence:e1}}"
        validate_body_plan(unknown, BodyPlan(unknown.item_id, unknown.content_revision, (BodyBlock(
            "quoted", "narrative", vague, ("story",), ("e1",), primary_home=unknown.item_id),)))

    def test_duplicate_publisher_title_heading_drops_only_an_exact_title_line(self):
        def body_of(item_id, text, *, title="箱猫"):
            snapshot = self.snapshot(item_id=item_id, text="周五，她把箱子让给了猫。", title=title)
            plan = self.plan(snapshot)
            plan = replace(plan, blocks=(replace(plan.blocks[0], text=text),))
            receipt = self.publisher.publish_item(snapshot, plan)
            return self.publisher.read(receipt.path).decode()

        echoed = body_of("dup-heading", "# 箱猫\n\n周五，她把箱子让给了猫。")
        self.assertEqual(echoed.count("# 箱猫"), 1)
        self.assertNotIn("# 箱猫", region_body(echoed, "body"))
        self.assertIn("周五，她把箱子让给了猫。", region_body(echoed, "body"))

        other = body_of("other-heading", "# 另一个标题\n\n周五，她把箱子让给了猫。")
        self.assertIn("# 另一个标题", region_body(other, "body"))

        fenced = body_of("fenced-title", "```text\n# 箱猫\n周五，她把箱子让给了猫。\n```")
        self.assertIn("```text\n# 箱猫\n周五，她把箱子让给了猫。\n```", region_body(fenced, "body"))
        source_heading = self.snapshot(item_id="source-heading", text="# 箱猫\n\n这是原文标题。")
        receipt = self.publish(source_heading)
        self.assertIn(source_heading.units[0].text, region_body(self.publisher.read(receipt.path).decode(), "body"))

    def test_composition_response_binds_snapshot_metadata_and_replays_canonical_plan(self):
        snapshot = replace(self.snapshot(revision=7), resources=(ReadingResource("photo", "照片", None, "pending"),))
        snapshot = replace(snapshot, units=(replace(snapshot.units[0], resource_ids=("photo",)),))
        response = {"blocks": [{"id": "story-home", "role": "narrative", "text": snapshot.units[0].text,
                                 "unit_indices": [0]}]}
        provider = ScriptedTextProvider(response)
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger)
        plan = writer.plan(snapshot, request_id="composition")
        self.assertEqual((plan.item_id, plan.content_revision), (snapshot.item_id, 7))
        block = plan.blocks[0]
        self.assertEqual((block.unit_ids, block.evidence_ids, block.resource_ids, block.primary_home),
                         (("story",), ("e1",), ("photo",), snapshot.item_id))
        self.assertEqual(writer.plan(snapshot, request_id="composition"), plan)
        self.assertEqual(len(provider.calls), 1)
        row = self.ledger.one("SELECT artifact_json FROM reader_text_artifacts WHERE request_id='composition'")
        self.assertEqual(BodyPlan.from_json(row[0]), plan)
        for indices in ([True], [-1], [1], [0, 0], []):
            wrong = {"blocks": [{**response["blocks"][0], "unit_indices": indices}]}
            with self.subTest(indices=indices), self.assertRaises(ReadingError):
                BodyPlanWriter(ScriptedTextProvider(wrong), self.ledger.generation_id).plan(snapshot, request_id="bad")
        # A full caller-supplied envelope is never silently corrected.
        wrong_revision = json.loads(plan.to_json())
        wrong_revision["content_revision"] = 8
        with self.assertRaisesRegex(ReadingError, "stale_body_plan"):
            BodyPlanWriter(ScriptedTextProvider(wrong_revision), self.ledger.generation_id).plan(snapshot, request_id="stale")

    def test_overlapping_units_share_prose_but_keep_both_content_homes(self):
        snapshot = self.snapshot()
        snapshot = replace(snapshot, units=(snapshot.units[0], replace(snapshot.units[0], id="overlap")))
        def response(indices):
            return {"blocks": [{"id": "same-scene", "role": "narrative",
                                "text": snapshot.units[0].text, "unit_indices": indices}]}
        for indices in ([0], [1], [0, 0, 1]):
            with self.subTest(indices=indices), self.assertRaisesRegex(ReadingError, "body_plan_unit_coverage"):
                BodyPlanWriter(ScriptedTextProvider(response(indices)), self.ledger.generation_id).plan(
                    snapshot, request_id="incomplete-scene")
        plan = BodyPlanWriter(ScriptedTextProvider(response([0, 1])), self.ledger.generation_id).plan(
            snapshot, request_id="complete-scene")
        self.assertEqual(plan.blocks[0].unit_ids, (snapshot.units[0].id, "overlap"))
        self.assertEqual(plan.blocks[0].text, snapshot.units[0].text)

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
        def compose(prompt):
            partial = json.loads(prompt)["snapshot"]
            return {"blocks": [{"id": "fragment-" + str(index), "role": unit["role"],
                                "text": unit["text"], "unit_indices": [index]}
                               for index, unit in enumerate(partial["units"])]}
        provider = ScriptedTextProvider(*([compose] * 500))
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

    def test_new_detail_rewrites_automatic_prose_in_place_small_and_paged(self):
        for budget in (80000, 9000):
            with self.subTest(budget=budget):
                filler = tuple(ReadingUnit("context-" + str(i), "独立的日常记录。" * 30, ("context-e-" + str(i),))
                               for i in range(30)) if budget == 9000 else ()
                units = filler + (ReadingUnit("plan", "P卡提供eSIM。", ("plan-e",)),)
                first = replace(self.snapshot(item_id="plan-" + str(budget), title="P卡 eSIM 激活"), units=units,
                    evidence=tuple(EvidenceExcerpt(u.evidence_ids[0], u.text) for u in units))
                previous = self.plan(first)
                original = self.publisher.publish_item(first, previous)
                extra = ReadingUnit("activation", "支持在线激活。", ("activation-e",))
                updated = replace(first, content_revision=2, units=units + (extra,),
                    evidence=first.evidence + (EvidenceExcerpt("activation-e", extra.text),))
                self.revisions[first.item_id] = 2
                def compose(prompt):
                    data = json.loads(prompt)
                    indices = {u["id"]: i for i, u in enumerate(data["snapshot"]["units"])}
                    return {"blocks": [{"id": b["id"], "role": b["role"],
                        "text": "P卡提供支持在线激活的eSIM。" if "plan" in b["unit_ids"] else b["text"],
                        "unit_indices": [indices[u] for u in b["unit_ids"]] +
                                        ([indices["activation"]] if "plan" in b["unit_ids"] else [])}
                        for b in data["previous"]["blocks"]]}
                provider = ScriptedTextProvider(compose)
                writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=budget)
                plan = writer.plan(updated, previous=previous, request_id="rewrite-" + str(budget))
                self.assertEqual(plan.blocks[-1].text, "P卡提供支持在线激活的eSIM。")
                self.assertEqual(plan.blocks[-1].id, previous.blocks[-1].id)
                self.assertEqual(plan.blocks[:-1], previous.blocks[:-1])
                self.assertEqual(set(plan.blocks[-1].evidence_ids), {"plan-e", "activation-e"})
                result = self.publisher.publish_item(updated, plan)
                self.assertEqual(result.path, original.path)
                before = self.publisher.read(result.path)
                self.assertIn("P卡提供支持在线激活的eSIM。", before.decode())
                self.assertNotIn("P卡提供eSIM。", before.decode())
                repeated = writer.plan(updated, previous=plan, request_id="unchanged-" + str(budget))
                self.assertEqual(self.publisher.publish_item(updated, repeated).status, "unchanged")
                self.assertEqual(self.publisher.read(result.path), before)
                self.assertEqual(len(provider.calls), 1)

    def test_body_upgrade_cannot_repartition_an_unknown_page_or_fragment(self):
        # Old versions wrote children before the completed paged aggregate.
        # A longer prompt or smaller budget can now choose different child IDs.
        from core.semantic.projections import accepted_text_artifact
        snapshot = self.snapshot()
        for version, suffix in ((version, suffix) for version in ("body-plan-v4", "body-plan-v9")
                                for suffix in (":page:0", ":group:0:fragment:0", ":repair:old", "")):
            with self.subTest(version=version, suffix=suffix):
                root = "body_%_" + version + str(len(suffix))
                old = ScriptedTextProvider(RuntimeError("old request outcome unknown"))
                with self.assertRaisesRegex(RuntimeError, "old request outcome unknown"):
                    accepted_text_artifact(self.ledger, old, prompt="old prompt " + suffix,
                        request_id=root + suffix, prompt_version=version, validate=lambda raw: raw)
                provider = ScriptedTextProvider(self.paged_response)
                writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=9000)
                with self.assertRaisesRegex(ReadingError, "text_provider_outcome_unknown"):
                    writer.plan(snapshot, request_id=root)
                self.assertEqual(provider.calls, [])
                self.assertEqual(self.ledger.one(
                    "SELECT status FROM reader_text_artifacts WHERE request_id=?", (root + suffix,))[0], "unknown")
        # The identity check is literal and scoped; unrelated requests can work.
        fresh = ScriptedTextProvider(self.paged_response)
        BodyPlanWriter(fresh, self.ledger.generation_id, self.ledger).plan(snapshot, request_id="independent-body")
        self.assertEqual(len(fresh.calls), 1)

    def test_increment_rewrite_keeps_exact_quotes_and_manual_conflict_bytes(self):
        first = self.snapshot()
        previous = self.plan(first)
        published = self.publisher.publish_item(first, previous)
        extra = ReadingUnit("new", "周六一起吃面。", ("e2",))
        updated = replace(first, content_revision=2, units=first.units + (extra,),
                          evidence=first.evidence + (EvidenceExcerpt("e2", extra.text),))
        self.revisions[first.item_id] = 2
        changed = replace(previous.blocks[0], text="她把箱子让给了狗，周六一起吃面。",
                          unit_ids=("story", "new"), evidence_ids=("e1", "e2"))
        with self.assertRaisesRegex(ReadingError, "body_plan_lost_evidence"):
            BodyPlanWriter(ScriptedTextProvider(BodyPlan(first.item_id, 2, (changed,)).to_json()),
                           self.ledger.generation_id).plan(updated, previous=previous, request_id="tampered-quote")
        valid = replace(changed, text=first.units[0].text + "周六一起吃面。")
        plan = BodyPlanWriter(ScriptedTextProvider(BodyPlan(first.item_id, 2, (valid,)).to_json()),
                              self.ledger.generation_id).plan(updated, previous=previous, request_id="exact-quote")
        target = self.publisher.root / published.path
        manual = target.read_bytes().replace(first.units[0].text.encode(), "用户手写正文，保留原样。".encode())
        target.write_bytes(manual)
        conflict = self.publisher.publish_item(updated, plan)
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(target.read_bytes(), manual)

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

    def test_pending_file_keeps_captured_filename_in_reading_surface(self):
        store, observation, _span = self.canonical_story()
        with store.transaction() as conn:
            conn.execute("INSERT INTO resource_refs VALUES(?,?,?,?,?,?,?,?)", (
                "song", observation["observation_id"], "stable-cat", "file-occurrence", "file",
                "ready_metadata", None, json.dumps({"original_name": "写给猫的歌.mp3"})))
        snapshot = CanonicalReadingAdapter(store).snapshot("stable-cat")
        self.assertEqual(snapshot.resources[0].label, "写给猫的歌.mp3")
        publisher = ProjectionPublisher.for_store(store, self.base / "filename-WGO", file_lock=MacOSFileLock())
        receipt = publisher.publish_item(snapshot, self.plan(snapshot))
        self.assertIn("写给猫的歌.mp3（附件待到达）", publisher.read(receipt.path).decode())

    def test_canonical_quote_can_be_composed_without_copying_chat_scaffolding(self):
        from core.semantic.store import SemanticStore
        store = SemanticStore(self.base / "composition.sqlite", "composition-generation")
        original = "[回复] 可以的，本来就是给云笺和叶页做的"
        observation, span = self.canonical_extract(store, "answer", original,
            payload={"sender": "木桥"}, unit_kind="quote")
        self.canonical_actions(store, [{"action_key": "answer-item", "type": "CREATE_ITEM", "item_id": "answer",
            "kind": "technical", "title": "云笺和叶页的支持", "identity_scope": {"feature": "客户端支持"},
            "observation_ids": [observation["observation_id"]]}])
        snapshot = CanonicalReadingAdapter(store).snapshot("answer")
        text = "木桥确认支持云笺和叶页，称它“本来就是给云笺和叶页做的”。"
        provider = ScriptedTextProvider({"blocks": [{"id": "support", "role": "narrative", "text": text,
                                                    "unit_indices": [0]}]})
        publisher = ProjectionPublisher.for_store(store, self.base / "composition-WGO", file_lock=MacOSFileLock())
        receipt = publisher.publish_canonical_item(store, "answer", BodyPlanWriter(provider, store.generation_id),
                                                   request_id="compose-answer")
        body = region_body(publisher.read(receipt.path).decode(), "body")
        self.assertIn(text, body)
        self.assertNotIn("[回复]", body)
        self.assertIn(original, publisher.read("来源/answer-0001.md").decode())
        self.assertIn(publisher.evidence_link("answer", span, alias="木桥的消息"), body)
        plan = publisher.load_plan("answer")
        self.assertEqual(plan.blocks[0].unit_ids, (snapshot.units[0].id,))
        self.assertEqual(plan.blocks[0].evidence_ids, (span,))
        with self.assertRaisesRegex(ReadingError, "markdown_quote_not_grounded"):
            validate_body_plan(snapshot, replace(plan, blocks=(replace(plan.blocks[0], text="> 已实测全部客户端"),)))

    def test_many_evidence_links_are_collapsed_without_losing_exact_targets(self):
        snapshot = self.snapshot()
        snapshot = replace(snapshot, evidence=tuple(EvidenceExcerpt("e" + str(i), "猫在箱子里。", speaker_name="小禾")
            for i in range(1, 5)), units=(replace(snapshot.units[0], evidence_ids=("e1", "e2", "e3", "e4")),))
        receipt = self.publish(snapshot)
        body = region_body(self.publisher.read(receipt.path).decode(), "body")
        self.assertIn("> [!quote]- 本段来源（4 条）", body)
        for excerpt in snapshot.evidence:
            link = self.publisher.evidence_link(snapshot.item_id, excerpt.id, alias="小禾的消息")
            self.assertIn("> - " + link, body)
            self.assertIn("^" + anchor(excerpt.id), self.publisher.read("来源/box-cat-0001.md").decode())

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

    def test_received_body_validation_repairs_once_and_replays_exact_artifact(self):
        for code, bad in (("body_plan_unit_coverage", {"blocks": []}),
                          ("body_plan_lost_evidence", {"blocks": [{"id": "home", "role": "narrative",
                              "text": "省略了必须逐字保留的原话。", "unit_indices": [0]}]})):
            with self.subTest(code=code):
                snapshot = self.snapshot(item_id=code)
                root = "repair-" + code
                before = []
                def repair(prompt):
                    rows = self.ledger.all("SELECT * FROM reader_text_artifacts WHERE request_id=? OR request_id LIKE ?",
                                           (root, root + ":repair:%"))
                    original = next(row for row in rows if row["request_id"] == root)
                    child = next(row for row in rows if row["request_id"] != root)
                    self.assertEqual((original["status"], child["status"]), ("failed", "running"))
                    diagnostic = json.loads(original["artifact_json"])
                    self.assertEqual(diagnostic["code"], code)
                    expected_id = root + ":repair:" + fingerprint(json.dumps(
                        [original["job_key"], root], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                    self.assertEqual(child["request_id"], expected_id)
                    self.assertEqual(child["provider_route"], original["provider_route"])
                    self.assertIn(code, json.loads(prompt)["task"])
                    self.assertEqual(json.loads(prompt)["snapshot"], json.loads(provider.calls[0][0])["snapshot"])
                    self.assertLessEqual(len(prompt.encode()), len(provider.calls[0][0].encode()))
                    before.append(dict(original))
                    return self.paged_response(prompt)
                provider = ScriptedTextProvider(bad, repair)
                plan = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger).plan(snapshot, request_id=root)
                self.assertEqual(plan.blocks[0].text, snapshot.units[0].text)
                self.assertEqual(len(provider.calls), 2)
                self.assertEqual(dict(self.ledger.one("SELECT * FROM reader_text_artifacts WHERE request_id=?", (root,))), before[0])
                # New process and even a caller alias adopt the same accepted child.
                replay = ScriptedTextProvider()
                resumed = BodyPlanWriter(replay, self.ledger.generation_id, self.ledger).plan(snapshot, request_id=root)
                self.assertEqual(resumed, plan)
                self.assertEqual(BodyPlanWriter(replay, self.ledger.generation_id, self.ledger).plan(
                    snapshot, request_id=root + "-alias"), plan)
                self.assertEqual(replay.calls, [])
                self.assertEqual(len(self.ledger.all("SELECT * FROM reader_text_artifacts WHERE request_id=? OR request_id LIKE ?",
                                                   (root, root + ":repair:%"))), 2)

    def test_bounded_pages_and_lossless_fragments_repair_the_exact_child(self):
        for phase in ("body_page_v1", "body_fragment_v1"):
            with self.subTest(phase=phase):
                snapshot = self.snapshot(item_id=phase)
                if phase == "body_page_v1":
                    units = tuple(ReadingUnit("unit-" + str(i), "这是日常片段。" * 40 + str(i), ("e-" + str(i),))
                                  for i in range(20))
                    snapshot = replace(snapshot, units=units,
                        evidence=tuple(EvidenceExcerpt("e-" + str(i), u.text) for i, u in enumerate(units)))
                else:
                    text = "连续的中文与步骤不能被截断。" * 1500
                    snapshot = replace(snapshot, units=(ReadingUnit("large", text, ("large-e",), verbatim=(text,)),),
                                       evidence=(EvidenceExcerpt("large-e", text),))
                def first(prompt):
                    self.assertEqual(json.loads(prompt)["phase"], phase)
                    if phase == "body_page_v1":
                        return {"blocks": []}
                    response = self.paged_response(prompt)
                    response["blocks"][0]["text"] += "额外文字。"
                    return response
                provider = ScriptedTextProvider(first, *([self.paged_response] * 100))
                writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger, max_input_bytes=9000)
                plan = writer.plan(snapshot, request_id=phase)
                self.assertGreater(len(provider.calls), 2)
                self.assertTrue(all(len(call[0].encode()) <= 9000 for call in provider.calls))
                self.assertEqual({u for block in plan.blocks for u in block.unit_ids}, {u.id for u in snapshot.units})
                if phase == "body_fragment_v1":
                    self.assertEqual(plan.blocks[0].text, snapshot.units[0].text)
                failed = self.ledger.one("SELECT * FROM reader_text_artifacts WHERE status='failed' AND request_id LIKE ?",
                                         (phase + ":%",))
                expected_suffix = ":page:0" if phase == "body_page_v1" else ":group:0:fragment:0"
                self.assertEqual(failed["request_id"], phase + expected_suffix)
                child_id = provider.calls[1][1]
                self.assertTrue(child_id.startswith(failed["request_id"] + ":repair:"))
                count = len(provider.calls)
                self.assertEqual(writer.plan(snapshot, request_id=phase), plan)
                self.assertEqual(len(provider.calls), count)

    def test_body_repair_fits_an_original_prompt_at_the_provider_input_limit(self):
        snapshot = self.snapshot()
        size = len(BodyPlanWriter._prompt(snapshot, None, {}).encode())
        self.assertLess(size, 9000)
        snapshot = replace(snapshot, title=snapshot.title + "x" * (9000 - size))
        provider = ScriptedTextProvider({"blocks": []}, self.paged_response)
        provider.max_input_bytes = 9000
        plan = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger).plan(snapshot, request_id="full-budget")
        self.assertEqual(plan.blocks[0].text, snapshot.units[0].text)
        self.assertEqual(len(provider.calls[0][0].encode()), 9000)
        self.assertLessEqual(len(provider.calls[1][0].encode()), 9000)

    def test_body_original_interrupted_dispatch_stays_running_without_repair(self):
        snapshot = self.snapshot()
        def stop(_prompt):
            raise KeyboardInterrupt("synthetic original interruption")
        provider = ScriptedTextProvider(stop)
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger)
        with self.assertRaises(KeyboardInterrupt):
            writer.plan(snapshot, request_id="original-running")
        with self.assertRaisesRegex(ReadingError, "text_provider_outcome_unknown"):
            writer.plan(snapshot, request_id="original-running")
        row = self.ledger.one("SELECT * FROM reader_text_artifacts WHERE request_id='original-running'")
        self.assertEqual((row["status"], row["artifact_json"]), ("running", ""))
        self.assertEqual(len(provider.calls), 1)

    def test_repaired_body_resumes_after_publication_interruption(self):
        store, _observation, _span = self.canonical_story()
        publisher = ProjectionPublisher.for_store(store, self.base / "actual-WGO", file_lock=MacOSFileLock())
        provider = ScriptedTextProvider({"blocks": []}, self.paged_response)
        writer = BodyPlanWriter(provider, store.generation_id)
        with patch.object(publisher, "publish_item", side_effect=RuntimeError("synthetic publication crash")):
            with self.assertRaisesRegex(RuntimeError, "synthetic publication crash"):
                publisher.publish_canonical_item(store, "stable-cat", writer, request_id="repair-publish")
        self.assertEqual([row[0] for row in publisher.ledger.all(
            "SELECT status FROM reader_text_artifacts ORDER BY rowid")], ["failed", "accepted"])
        replay = ScriptedTextProvider()
        result = publisher.publish_canonical_item(store, "stable-cat", BodyPlanWriter(replay, store.generation_id),
                                                  request_id="repair-publish")
        self.assertEqual(result.status, "published")
        self.assertEqual(replay.calls, [])
        self.assertEqual(store.pending_outbox("projection"), [])

    def test_body_known_failure_resumes_after_interruption_before_repair_dispatch(self):
        snapshot = self.snapshot()
        provider = ScriptedTextProvider({"blocks": []}, self.paged_response)
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger)
        with patch.object(writer, "_repair_received_validation", side_effect=KeyboardInterrupt("before repair")):
            with self.assertRaises(KeyboardInterrupt):
                writer.plan(snapshot, request_id="before-repair")
        failed = dict(self.ledger.one("SELECT * FROM reader_text_artifacts WHERE request_id='before-repair'"))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(provider.calls), 1)
        resumed = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger).plan(snapshot, request_id="before-repair")
        self.assertEqual(resumed.blocks[0].text, snapshot.units[0].text)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(dict(self.ledger.one("SELECT * FROM reader_text_artifacts WHERE request_id='before-repair'")), failed)

    def test_body_repair_unknown_or_interrupted_dispatch_blocks_resume(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                snapshot = self.snapshot(item_id="unknown-repair-" + str(interrupted))
                root = snapshot.item_id
                def stop(_prompt):
                    if interrupted:
                        raise KeyboardInterrupt("synthetic repair interruption")
                    raise RuntimeError("unknown repair result")
                provider = ScriptedTextProvider({"blocks": []}, stop)
                error = KeyboardInterrupt if interrupted else RuntimeError
                with self.assertRaises(error):
                    BodyPlanWriter(provider, self.ledger.generation_id, self.ledger).plan(snapshot, request_id=root)
                rows = self.ledger.all("SELECT * FROM reader_text_artifacts WHERE request_id LIKE ?", (root + "%",))
                self.assertEqual([row["status"] for row in rows], ["failed", "running" if interrupted else "unknown"])
                for budget in (80000, 9000):
                    replay = ScriptedTextProvider(self.paged_response)
                    with self.assertRaisesRegex(ReadingError, "text_provider_outcome_unknown"):
                        BodyPlanWriter(replay, self.ledger.generation_id, self.ledger, max_input_bytes=budget).plan(
                            snapshot, request_id=root)
                    self.assertEqual(replay.calls, [])
                self.assertEqual(len(provider.calls), 2)

    def test_body_repair_exhaustion_retains_both_failures_and_never_calls_again(self):
        snapshot = self.snapshot()
        provider = ScriptedTextProvider({"blocks": []}, {"blocks": []})
        writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger)
        with self.assertRaisesRegex(ReadingError, "body_plan_unit_coverage"):
            writer.plan(snapshot, request_id="exhausted")
        before = [dict(row) for row in self.ledger.all("SELECT * FROM reader_text_artifacts ORDER BY rowid")]
        self.assertEqual([row["status"] for row in before], ["failed", "failed"])
        for root in ("exhausted", "exhausted-alias"):
            with self.assertRaisesRegex(ReadingError, "text_request_already_failed"):
                writer.plan(snapshot, request_id=root)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual([dict(row) for row in self.ledger.all("SELECT * FROM reader_text_artifacts ORDER BY rowid")], before)

    def test_body_provider_route_and_historical_failures_have_no_repair_proof(self):
        from ai.semantic_provider import ProviderError, ProviderRoute
        for failure in ("route", "provider", "historical", "stale"):
            with self.subTest(failure=failure):
                snapshot = self.snapshot(item_id="no-repair-" + failure)
                root = snapshot.item_id
                if failure == "historical":
                    # Historical validation failures did not persist trustworthy diagnostics.
                    from core.semantic.projections import accepted_text_artifact
                    provider = ScriptedTextProvider({"blocks": []})
                    with self.assertRaisesRegex(ReadingError, "body_plan_unit_coverage"):
                        accepted_text_artifact(self.ledger, provider,
                            prompt=BodyPlanWriter._prompt(snapshot, None, {}), request_id=root,
                            prompt_version="body-plan-v11", validate=lambda raw: validate_body_plan(
                                snapshot, BodyPlanWriter._response(snapshot, raw)).to_json())
                    expected = "text_request_already_failed"
                else:
                    value = ProviderError("synthetic provider error", outcome_unknown=False)
                    if failure == "stale":
                        value = {**json.loads(self.plan(snapshot).to_json()), "content_revision": 2}
                    provider = ScriptedTextProvider(value)
                    if failure == "route":
                        def wrong_route(prompt, *, request_id, generation_id):
                            provider.calls.append((prompt, request_id, generation_id))
                            return SimpleNamespace(text='{"blocks":[]}', route=ProviderRoute("wrong", "wrong", "text"))
                        provider.generate = wrong_route
                    expected = {"route": "text_provider_route_changed", "provider": "provider_failed",
                                "stale": "stale_body_plan"}[failure]
                writer = BodyPlanWriter(provider, self.ledger.generation_id, self.ledger)
                with self.assertRaisesRegex((ReadingError, ProviderError), expected):
                    writer.plan(snapshot, request_id=root)
                for resume in (root, root + "-alias"):
                    with self.assertRaisesRegex(ReadingError, "text_request_already_failed"):
                        writer.plan(snapshot, request_id=resume)
                row = self.ledger.one("SELECT * FROM reader_text_artifacts WHERE request_id=?", (root,))
                self.assertEqual((row["status"], row["artifact_json"]), ("failed", ""))
                self.assertEqual(len(provider.calls), 1)

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
