"""Reader integration uses only temporary canonical stores and captured delivery."""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
from unittest.mock import patch

from ai.vector_index import LocalCosineIndex
from ai.semantic_provider import TextResult
from core.config import _sanitize_config
from core.semantic.briefing import BriefChange, BriefingService
from core.semantic.projections import ReadingError, anchor, fingerprint, patch_properties, property_value
from core.semantic.reader_runtime import DEFAULT_PROPERTIES, EVENT_SCHEMA, ReaderRuntime
from core.semantic.runtime import ProviderRoles, SemanticRuntime
from core.semantic.store import SemanticStore
from core.semantic.watches import WatchSpec
from tests.test_semantic_delivery import RecordingNotifier
from tests.test_semantic_briefing import bounded_editor_response
from tests.test_semantic_engine import FakeDecision, FakeText, capture, freeze_legacy_primary
from tests.test_semantic_projections import ReadingHarness, ScriptedTextProvider
from tests.test_semantic_retrieval import FakeEmbedding
from tests.test_semantic_watches import WatchText


class PipelineText(FakeText):
    def __init__(self):
        super().__init__()
        self.reader_calls = []

    def generate(self, prompt, *, request_id, generation_id=""):
        if prompt.startswith("{"):
            value = json.loads(prompt)
            self.reader_calls.append((prompt, request_id))
            if "snapshot" in value:
                from tests.test_semantic_projections import ProjectionTests
                response = ProjectionTests.paged_response(prompt)
            else:
                response = bounded_editor_response(prompt)
            return TextResult(json.dumps(response, ensure_ascii=False), self.route)
        return super().generate(prompt, request_id=request_id, generation_id=generation_id)


class MarkdownPipelineText(FakeText):
    """Scripted synthetic prose, not a live model-quality evaluation."""
    QUOTE = "猫在箱子里睡熟了，面还热着。"
    SOURCES = (
        "周五我给猫留了纸箱。它睡了一晚；没有提到窗边。",
        "我把纸箱放在窗边，但猫没有进去。观察时长没记。",
        "先保存草稿，再同步阅读页：\n1. 打开 `草稿.md`。\n2. 执行本地预览，检查标题。\n\n```text\npreview --local\n```",
        "邻居托我转述，原作者没有留名：\n" + QUOTE,
    )
    PAYLOADS = (
        {"raw_sender_id": "wxid_fixture_one", "sender": "小禾"},
        {"raw_sender_id": "wxid_fixture_two", "sender": "小禾"},
        {"raw_sender_id": "wxid_fixture_relay", "sender": "阿岚"},
        {"raw_sender_id": "wxid_fixture_relay", "sender": "阿岚"},
    )
    LATE = "周一续记：同一个窗边纸箱加了软垫，猫仍未进入；观察五分钟。"

    def __init__(self):
        super().__init__()
        self.prompts = []

    @staticmethod
    def citation(id_):
        return "{{evidence:" + id_ + "}}"

    @classmethod
    def comparison(cls, first, second):
        return ("## 两条独立的箱猫记录\n\n同名是两条消息的显示名，下面保留各自的观察条件。\n\n"
                "| 案例 | 记录时显示名 | 观察到的情况 | 观察时长 | 来源 |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| 消息 A | 小禾 | 猫睡在纸箱里；是否靠窗未知 | 一晚 | " + cls.citation(first) + " |\n"
                "| 消息 B | 小禾 | 纸箱在窗边，猫没有进去 | 未知（没有记录） | " + cls.citation(second) + " |")

    @classmethod
    def body(cls, value):
        snapshot, previous = value["snapshot"], value["previous"]
        units = snapshot["units"]
        if previous:
            blocks = previous["blocks"]
            old = {id_ for block in blocks for id_ in block["unit_ids"]}
            for unit in units:
                if unit["id"] not in old:
                    table = next(b for b in blocks if b["id"] == "case-comparison")
                    table["text"] += "\n| 消息 B 续记 | 小禾 | 同一个窗边纸箱加了软垫，猫仍未进入 | 五分钟 | " + cls.citation(unit["evidence_ids"][0]) + " |"
                    table["unit_ids"].append(unit["id"])
                    table["evidence_ids"].extend(unit["evidence_ids"])
        else:
            first = next(u for u in units if u["text"] == cls.SOURCES[0])
            second = next(u for u in units if u["text"] == cls.SOURCES[1])
            steps = next(u for u in units if u["role"] == "step")
            quote = next(u for u in units if u["role"] == "quote")
            def block(id_, text, assigned):
                return {"id": id_, "role": "narrative", "text": text,
                        "unit_ids": [u["id"] for u in assigned],
                        "evidence_ids": [id_ for u in assigned for id_ in u["evidence_ids"]],
                        "resource_ids": [], "primary_home": snapshot["item_id"], "unit_statuses": {}}
            blocks = [block("case-comparison", cls.comparison(first["evidence_ids"][0], second["evidence_ids"][0]), (first, second)),
                      block("reading-steps", "## 阿岚记录的操作顺序\n\n" + steps["text"], (steps,)),
                      block("relayed-quote", "## 一句转述\n\n阿岚转述邻居的话；原作者没有提供姓名。\n\n> " + cls.QUOTE, (quote,))]
        return {"item_id": snapshot["item_id"], "content_revision": snapshot["content_revision"], "blocks": blocks}

    @classmethod
    def brief(cls, value):
        changes = value["changes"]
        evidence = {a["excerpt"]: a["evidence_id"] for c in changes for a in c["attributions"]}
        text = ("两位显示名都是小禾的讲述者留下了不同观察；阿岚带来的邻居原话，把这一天接回了餐桌。\n\n"
                + cls.comparison(evidence[cls.SOURCES[0]], evidence[cls.SOURCES[1]])
                + "\n\n阿岚转述邻居的话，原作者没有提供姓名：\n\n> " + cls.QUOTE
                + "\n\n## 阿岚记录的操作顺序\n\n" + cls.SOURCES[2]
                + "\n\n" + cls.citation(evidence[cls.SOURCES[2]]))
        return {"status": "success", "title": "纸箱、面条与几步记录", "paragraphs": [{"text": text,
            "change_ids": [c["id"] for c in changes],
            "evidence_refs": [{"item_id": c["item_id"], "evidence_id": id_} for c in changes for id_ in c["evidence_ids"]],
            "scopes": list(dict.fromkeys(c["scope"] for c in changes if c["scope"])),
            "corrections": list(dict.fromkeys(c["correction"] for c in changes if c["correction"]))}]}

    def generate(self, prompt, *, request_id, generation_id=""):
        self.prompts.append(prompt)
        if prompt.startswith("{"):
            value = json.loads(prompt)
            result = self.body(value) if "snapshot" in value else self.brief(value)
        else:
            value = json.loads(prompt.rsplit("\n", 1)[-1])
            result = json.loads(super().generate(prompt, request_id=request_id, generation_id=generation_id).text)
            if prompt.startswith("The following captured"):
                for observation in result["observations"]:
                    observation["scene_kind"] = "life"
                    unit = observation["units"][0]
                    if unit["text"] == self.SOURCES[2]:
                        unit["kind"] = "step"
                    elif unit["text"] == self.SOURCES[3]:
                        unit.update(kind="quote", text=self.QUOTE)
            elif isinstance(value, dict) and "observations" in value and not value["items"]:
                result["actions"] = [{"action_key": request_id + ":comparison", "type": "CREATE_ITEM",
                    "item_id": value["new_item_ids"][0], "kind": "story", "title": "纸箱、面条与记录",
                    "identity_scope": {"scene": "并列阅读各自的箱猫观察；不合并人物或独立事件"},
                    "observation_ids": [o["observation_id"] for o in value["observations"]]}]
        return TextResult(json.dumps(result, ensure_ascii=False), self.route)


def render_markdown_fixture(base):
    """Run actual capture/engine/body/brief APIs against a synthetic provider."""
    base = Path(base).resolve()
    base.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    store = SemanticStore(base / "synthetic.sqlite", "markdown-fixture", now_func=now.timestamp)
    provider = MarkdownPipelineText()
    runtime = SemanticRuntime(store, ProviderRoles(provider, FakeDecision(), FakeEmbedding(), LocalCosineIndex()),
                              base / "WGO", config={"daily_digest_timezone": "Asia/Singapore"})
    def captured(batch, texts, payloads):
        return store.capture_batch(batch, [{"source_id": batch + "-" + str(index), "text": text,
            "payload": payloads[index], "logical_shard_id": "fixture-shard", "physical_generation": "fixture",
            "source_cursor_token": str(index), "source_sent_at": now.timestamp() - 3600 + index}
            for index, text in enumerate(texts)], source_namespace="synthetic-fixture", chat_id="fixture-room",
            inventory_digest="fixture-only", inventory_revision=1, input_cursors={}, output_cursors={}, source_eof=True)
    captured("initial", provider.SOURCES, provider.PAYLOADS)
    # Preserve this reader fixture's explicit substring quote under its frozen
    # historical wire. Fresh indexed quote selection copies complete spans.
    freeze_legacy_primary(runtime.engine, "initial")
    runtime.process("initial")
    first_publication = runtime.publish_all()
    item_id = store.list_items()[0]["item_id"]
    before = runtime.publisher.load_plan(item_id)
    brief = runtime.brief(now=now)
    captured("continuation", (provider.LATE,), (provider.PAYLOADS[1],))
    runtime.process("continuation")
    second_publication = runtime.publish_all()
    (base / "SYNTHETIC-README.md").write_text(
        "# 合成阅读输出\n\n此目录由临时 canonical store 与 scripted synthetic TextProvider fixture 生成。"
        "未读取真实聊天、未调用模型服务，不能作为 live model 质量证据。\n\n"
        "`WGO/条目/` 是增量续写后的笔记；`WGO/简报/` 保留当日第一版简报；"
        "`WGO/来源/` 是准确片段及捕获显示名。\n", encoding="utf-8")
    return runtime, provider, item_id, before, brief, (first_publication, second_publication)


class ReaderRuntimeTests(ReadingHarness):
    def setUp(self):
        super().setUp()
        self.store = SemanticStore(self.base / "reader.sqlite", "reader-generation")
        first, self.first_span = self.canonical_extract(self.store, "first", "周五，猫还没回到窗边。", claims=self.claims(False))
        self.canonical_actions(self.store, [{"action_key": "create", "type": "CREATE_ITEM", "item_id": "stable-cat",
            "kind": "story", "title": "窗边的猫", "identity_scope": {"scene": "同一只猫的日常"},
            "observation_ids": [first["observation_id"]]}])
        self.text = WatchText()
        self.runtime = SemanticRuntime(self.store, ProviderRoles(self.text, FakeDecision(), FakeEmbedding(), LocalCosineIndex()),
                                       self.base / "vault/WGO", config=_sanitize_config({}))
        self.publisher, self.ledger, self.controls = self.runtime.publisher, self.runtime.ledger, self.runtime.controls
        self.notifier = RecordingNotifier()
        self.reader = ReaderRuntime(self.runtime, notifier=self.notifier, device_id="synthetic-device", live=True)
        self.publish_current()

    @staticmethod
    def claims(success):
        return [{"subject_id": None, "case_id": None, "condition": None, "channel": None, "version": None,
                 "phase": None, "observed_at": 1791763200.0, "predicates": {"success": success}, "span_indices": [0]}]

    def publish_current(self):
        snapshot = self.runtime.adapter.snapshot("stable-cat")
        return self.publisher.publish_item(snapshot, self.plan(snapshot))

    def append(self, text="周一，猫终于回到窗边。", *, success=True, publish=True):
        identity = uuid.uuid4().hex
        observation, span = self.canonical_extract(self.store, identity, text, claims=self.claims(success))
        self.canonical_actions(self.store, [{"action_key": "append-" + identity, "type": "MERGE_INTO_ITEM",
            "item_id": "stable-cat", "observation_ids": [observation["observation_id"]],
            "modes": ["append_detail", "add_evidence"], "semantic_effect": "detail_added"}])
        if publish:
            self.publish_current()
        return observation, span

    def floor(self, value=None):
        with self.store.transaction() as conn:
            if value is None:
                value = conn.execute("SELECT max(change_id) FROM change_events").fetchone()[0] or 0
            metadata = json.loads(conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0])
            metadata["delivery_floor_change_id"] = value
            conn.execute("UPDATE generation_manifest SET metadata_json=?", (json.dumps(metadata),))
        return value

    def properties(self, **values):
        path = self.publisher.root / self.publisher.resolve("stable-cat")
        path.write_text(patch_properties(path.read_text(), {self.reader.properties[key]: value for key, value in values.items()}, machine_only=False))

    def check(self, kind):
        action = self.ledger.one("SELECT * FROM reader_actions WHERE kind=? AND status='registered' ORDER BY rowid DESC LIMIT 1", (kind,))
        path = self.publisher.root / "管理.md"
        path.write_text(re.sub(r"^- \[ \](.*<!-- wgo-action:" + action["action_id"] + r" -->)$", r"- [x]\1", path.read_text(), flags=re.M))
        return action

    def event(self, **changes):
        path = self.publisher.resolve("stable-cat")
        raw = self.publisher.read(path)
        value = {"schema": EVENT_SCHEMA, "event_id": uuid.uuid4().hex, "generation_id": self.store.generation_id,
                 "item_id": "stable-cat", "content_revision": property_value(raw.decode(), "wgo_content_revision"),
                 "path": path, "file_sha256": fingerprint(raw), "observed_at": time.time(),
                 "origin": "obsidian_visible_open", "visible": True}
        value.update(changes)
        return value

    def inbox(self, value, *, filename=None):
        path = self.publisher._safe_path(".wgo/reader-events/" + (filename or value["event_id"] + ".json"))
        path.write_text(json.dumps(value, ensure_ascii=False))
        path.chmod(0o600)
        return path

    def wait(self):
        self.reader.poll()
        self.properties(wait="等猫明确回到窗边")
        self.reader.poll()

    def test_sanitized_default_empty_mapping_and_partial_override(self):
        self.assertEqual(self.runtime.config["semantic_reader_properties"], {})
        self.assertEqual(self.reader.properties, DEFAULT_PROPERTIES)
        self.runtime.config["semantic_reader_properties"] = {"query": "读者问题"}
        customized = ReaderRuntime(self.runtime, device_id="other")
        self.assertEqual(customized.properties, DEFAULT_PROPERTIES | {"query": "读者问题"})
        self.runtime.config["semantic_reader_properties"] = {"query": "阅读关注"}
        with self.assertRaisesRegex(ReadingError, "invalid_control_property_mapping"):
            ReaderRuntime(self.runtime, device_id="other")

    def test_custom_query_first_publication_and_poll_share_the_mapping(self):
        self.runtime.config["semantic_reader_properties"] = {"query": "问题"}
        self.assertTrue(self.runtime.publish_all()["complete"])
        reader = ReaderRuntime(self.runtime, device_id="custom-query")
        query = self.publisher.root / "检索.md"
        self.assertEqual(property_value(query.read_text(), "问题"), "")
        query.write_text(patch_properties(query.read_text(), {"问题": "猫"}, machine_only=False))
        result = reader.poll()
        self.assertEqual(result["query"], "success")
        self.assertNotIn("control_mapping_already_registered", result.get("codes", ()))
        self.runtime.config["semantic_reader_properties"] = {}
        with self.assertRaisesRegex(ReadingError, "control_mapping_already_registered"):
            self.runtime.publish_all()

    def test_runtime_brief_only_late_delta_and_pure_claim_correction(self):
        provider = ScriptedTextProvider(*([bounded_editor_response] * 20))
        self.runtime.briefing.provider = provider
        first = self.runtime.brief(now=datetime.now(timezone.utc))
        self.assertEqual(first.status, "success", first)
        self.append("周六她吃面，猫安静地待在脚边。")
        later = self.runtime.brief(now=datetime.now(timezone.utc))
        self.assertEqual(later.status, "success", later)
        later_input = json.loads(provider.calls[-1][0])["changes"][0]
        self.assertNotIn("周五", later_input["before"] + later_input["after"])
        self.assertIn("周六", later_input["after"])
        self.assertEqual(later_input["before_revision"], 1)
        self.assertEqual(later.edition_id, first.edition_id)
        count = len(provider.calls)
        self.assertTrue(self.runtime.brief(now=datetime.now(timezone.utc)).unchanged)
        self.assertEqual(len(provider.calls), count)

        item = self.store.get_item("stable-cat")
        old_claim = next(c["claim_id"] for c in item["claims"] if c["predicates"]["success"] is True)
        correcting_claims = self.claims(False)
        correcting_claims[0].update(condition="只就周六那次观察", channel="窗边", version="现场更正")
        correction, new_span = self.canonical_extract(self.store, "correction-only", "更正：当时猫仍在纸箱，尚未回到窗边。",
                                                       claims=correcting_claims)
        new_claim = correction["claims"][0]["claim_id"]
        self.store.record_claim_relation(new_claim, old_claim, "corrects_or_retracts", "先前把纸箱旁误认为窗边。")
        # The new source is not a member of this Item; its exact relation span
        # is nevertheless published as evidence beside the retained old quote.
        self.assertNotIn(correction["observation_id"], self.store.get_item("stable-cat")["observation_ids"])
        self.publish_current()
        revised = self.runtime.brief(now=datetime.now(timezone.utc))
        self.assertEqual(revised.status, "success", revised)
        change = json.loads(provider.calls[-1][0])["changes"][0]
        self.assertIn("已撤回", change["correction"])
        self.assertIn("先前把纸箱旁误认为窗边", change["correction"])
        self.assertIn("现场更正", change["scope"])
        self.assertIn("只就周六那次观察", change["conditions"])
        self.assertIn(new_span, change["evidence_ids"])
        self.assertTrue(change["relation_change"])
        self.assertIn("周六", change["before"])
        self.assertIn("历史陈述，已撤回", change["after"])
        self.assertNotIn("周五", change["before"] + change["after"])
        self.assertIn("更正：当时猫仍在纸箱", self.runtime.adapter.snapshot("stable-cat").evidence[-1].text)
        self.assertTrue(self.publisher.evidence_link("stable-cat", new_span))

    def test_large_single_source_engine_body_brief_and_late_delta(self):
        store = SemanticStore(self.base / "large-story.sqlite", "large-story")
        text = PipelineText()
        runtime = SemanticRuntime(store, ProviderRoles(text, FakeDecision(), FakeEmbedding(), LocalCosineIndex()),
                                  self.base / "large-vault/WGO", config={"daily_digest_timezone": "Asia/Singapore"})
        source = "周五她给猫留了纸箱，又带回一束花。\n" * 2500 + "最后一家人坐在窗边。"
        self.assertGreater(len(source.encode()), 100000)
        capture(store, "long-story", [source])
        runtime.process("long-story")
        self.assertTrue(runtime.publish_all()["complete"])
        self.assertEqual(len(store.list_items()), 1)
        item = store.list_items()[0]
        snapshot = runtime.adapter.snapshot(item["item_id"])
        self.assertGreater(sum(len(unit.text.encode()) for unit in snapshot.units), 100000)
        self.assertTrue(runtime.publisher.ready(item["item_id"], item["content_revision"]))
        first = runtime.brief(now=datetime.now(timezone.utc))
        self.assertEqual(first.status, "success", first)
        inputs = json.loads(runtime.briefing.history(first.edition_id)[0]["inputs_json"])[0]
        self.assertEqual(inputs["before_revision"], 0)
        self.assertGreater(len(inputs["after"].encode()), 100000)
        self.assertGreater(len(inputs["contributing_change_ids"]), 1)
        reader_prompts = [json.loads(prompt) for prompt, _ in text.reader_calls]
        self.assertGreater(sum(value.get("phase") == "brief_compact_page_v1" for value in reader_prompts), 1)
        self.assertEqual(sum(value.get("phase") == "brief_write_v1" for value in reader_prompts), 1)
        self.assertTrue(all(len(prompt.encode()) <= 24000 for prompt, _ in text.reader_calls
                            if json.loads(prompt).get("phase", "").startswith("brief_")))
        before_calls = len(text.reader_calls)
        self.assertTrue(runtime.brief(now=datetime.now(timezone.utc)).unchanged)
        self.assertEqual(len(text.reader_calls), before_calls)
        capture(store, "late-story", ["周一猫把纸箱推到桌边，一家人吃面时它也凑过来了。"])
        runtime.process("late-story")
        runtime.publish_all()
        revised = runtime.brief(now=datetime.now(timezone.utc))
        self.assertEqual(revised.status, "success", revised)
        self.assertEqual(first.edition_id, revised.edition_id)
        latest = json.loads(text.reader_calls[-1][0])["changes"][0]
        self.assertIn("周一", latest["after"])
        self.assertNotIn("周五", latest["before"] + latest["after"])
        self.assertLess(len(json.dumps(latest, ensure_ascii=False).encode()), 6000)
        self.assertEqual(len(list((runtime.publisher.root / "简报").glob("*.md"))), 1)

    def test_canonical_names_markdown_and_incremental_stable_home(self):
        runtime, provider, item_id, before, brief, publications = render_markdown_fixture(self.base / "markdown-fixture")
        self.assertTrue(all(value["complete"] for value in publications), publications)
        self.assertEqual(brief.status, "success", brief)
        snapshot = runtime.adapter.snapshot(item_id)
        by_text = {e.text: e for e in snapshot.evidence}
        first, second = by_text[provider.SOURCES[0]], by_text[provider.SOURCES[1]]
        self.assertEqual((first.speaker_name, second.speaker_name), ("小禾", "小禾"))
        self.assertNotEqual(first.speaker_id, second.speaker_id)
        self.assertEqual(second.speaker_id, by_text[provider.LATE].speaker_id)
        self.assertEqual(by_text[provider.SOURCES[2]].speaker_id, by_text[provider.SOURCES[3]].speaker_id)
        after = runtime.publisher.load_plan(item_id)
        old, new = {b.id: b for b in before.blocks}, {b.id: b for b in after.blocks}
        self.assertEqual(old["reading-steps"], new["reading-steps"])
        self.assertEqual(old["relayed-quote"], new["relayed-quote"])
        self.assertIn(old["case-comparison"].text, new["case-comparison"].text)
        self.assertEqual(len(new["case-comparison"].unit_ids), 3)
        self.assertEqual({id_ for b in after.blocks for id_ in b.unit_ids}, {u.id for u in snapshot.units})
        body = runtime.publisher.read(runtime.publisher.resolve(item_id)).decode()
        daily = runtime.publisher.read(brief.path).decode()
        for text in (body, daily):
            rows = [line for line in text.splitlines() if line.startswith("|")]
            self.assertTrue(rows)
            self.assertTrue(all(len(re.findall(r"(?<!\\)\|", line)) == 6 for line in rows))
            self.assertIn("\\|小禾的消息]]", text)
            self.assertIn("未知（没有记录）", text)
            without_anchors = re.sub(r"(?m)^\^wgo-[a-z0-9]+\n", "", text)
            self.assertIn(provider.SOURCES[2], re.sub(r"\n{3,}", "\n\n", without_anchors))
            self.assertIn("原作者没有提供姓名", text)
            after_quote = without_anchors.split("> " + provider.QUOTE, 1)[1].lstrip().splitlines()[0]
            self.assertIn("|阿岚的消息]]", after_quote)
            self.assertNotIn("{{evidence:", text)
            self.assertNotIn("wxid_", text)
        self.assertIn("五分钟", body)
        self.assertNotIn("五分钟", daily)  # Historical edition does not embed the live Item.
        for id_, ending in (("case-comparison", " |"), ("reading-steps", "2. 执行本地预览，检查标题。"),
                            ("relayed-quote", "> " + provider.QUOTE)):
            prefix = body.split("\n^" + anchor(id_), 1)[0].rstrip()
            self.assertTrue(prefix.endswith(ending), (id_, prefix[-200:]))
        view = runtime.publisher.publish_view("formatted", "共同阅读", [(item_id, b.id, b.id) for b in after.blocks], embed=True)
        links = runtime.publisher.read(view.path).decode()
        self.assertNotIn("![[", links)  # Compound bodies get honest stable navigation, not partial embeds.
        self.assertNotIn(provider.QUOTE, links)
        for block in after.blocks:
            self.assertIn("#^" + anchor(block.id), links)
        for source in provider.SOURCES:
            excerpt = by_text[source]
            link = runtime.publisher.evidence_link(item_id, excerpt.id)
            self.assertIn(anchor(excerpt.id), link)
        sources = "\n".join(path.read_text() for path in (runtime.publisher.root / "来源").glob("*.md"))
        self.assertIn("本条消息发言／转述者：小禾", sources)
        self.assertIn("本条消息发言／转述者：阿岚", sources)
        self.assertIn("````text\n" + provider.SOURCES[2] + "\n````", sources)
        self.assertNotIn("wxid_", sources)
        prompts = [json.loads(prompt) for prompt in provider.prompts if prompt.startswith("{")]
        writer_input = next(value for value in prompts if value.get("phase") == "brief_write_v1")
        attributions = writer_input["changes"][0]["attributions"]
        self.assertEqual({a["excerpt"] for a in attributions}, set(provider.SOURCES))
        self.assertEqual(len({a["speaker_id"] for a in attributions if a["speaker_name"] == "小禾"}), 2)
        self.assertNotIn("wxid_", "\n".join(provider.prompts))
        self.assertTrue(all("complete Markdown block" in value["task"] for value in prompts))

    def test_nonsemantic_latest_revision_preserves_day_delta_and_delivery_contributors(self):
        self.floor()
        self.append("周六她吃面，猫推来了纸箱。")
        extra, _ = self.canonical_extract(self.store, "extra-proof", "补充来源重复记下那只纸箱。")
        self.canonical_actions(self.store, [{"action_key": "provenance-only", "type": "MERGE_INTO_ITEM",
            "item_id": "stable-cat", "observation_ids": [extra["observation_id"]],
            "modes": ["add_evidence"], "semantic_effect": "none"}])
        self.publish_current()
        provider = ScriptedTextProvider(bounded_editor_response)
        self.runtime.briefing.provider = provider
        brief = self.runtime.brief(now=datetime.now(timezone.utc))
        self.assertEqual(brief.status, "success", brief)
        change = json.loads(provider.calls[0][0])["changes"][0]
        self.assertIn("周六", change["after"])
        self.assertNotIn("补充来源重复", change["after"])
        self.assertEqual(len(change["contributing_change_ids"]), 3)
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT semantic_effect FROM change_events ORDER BY change_id DESC LIMIT 1").fetchone()[0], "none")
        self.assertEqual(self.reader.deliver_brief(brief).status, "accepted")
        self.assertEqual(len(self.notifier.notifications), 1)

    def test_source_free_poll_real_search_properties_and_management_known(self):
        with patch.object(self.runtime, "process", side_effect=AssertionError("source processing")), \
                patch.object(self.runtime, "publish_all", side_effect=AssertionError("publication")):
            self.assertEqual(self.reader.poll()["query"], "empty")
            self.properties(follow=True)
            query = self.publisher.root / "检索.md"
            query.write_text(patch_properties(query.read_text(), {"查询": "猫"}, machine_only=False))
            result = self.reader.poll()
            self.assertEqual(result["query"], "success")
            self.assertIn("[[WGO/", query.read_text())
            self.assertTrue(self.controls.preference("stable-cat").follow)
            self.assertEqual(self.reader.poll()["query"], "unchanged")
            self.check("known")
            self.controls.refresh_management()  # Publication before poll.
            self.reader.poll()
        self.assertEqual(self.controls.preference("stable-cat").known_revision, 1)
        self.assertEqual(len(self.controls.query_history()), 1)
        self.assertEqual(self.notifier.notifications, [])

    def test_management_preference_is_reflected_without_next_poll_undo(self):
        self.reader.poll()
        self.check("follow")
        self.reader.poll()
        path = self.publisher.root / self.publisher.resolve("stable-cat")
        self.assertIs(property_value(path.read_text(), "阅读关注"), True)
        self.reader.poll()
        self.assertTrue(self.controls.preference("stable-cat").follow)

    def test_visible_open_is_durable_and_separate_from_known_and_click(self):
        self.reader.poll()
        value = self.event()
        path = self.inbox(value)
        self.assertEqual(self.reader.poll()["open_events"], 1)
        self.assertFalse(path.exists())
        processed = path.parent / "processed" / path.name
        self.assertTrue(processed.is_file())
        self.assertEqual(len(self.ledger.all("SELECT * FROM reader_open_observations")), 1)
        self.assertEqual(self.controls.preference("stable-cat").known_revision, 0)
        self.inbox(value)
        self.assertEqual(self.reader.poll()["open_events"], 0)
        self.assertEqual(len(self.ledger.all("SELECT * FROM reader_open_observations")), 1)

    def test_receipt_before_archive_crash_resumes_without_second_open(self):
        self.reader.poll()
        path = self.inbox(self.event())
        with patch.object(self.reader, "_archive_open", side_effect=OSError("synthetic crash after receipt")):
            self.reader.poll()
        self.assertTrue(path.exists())
        resumed = ReaderRuntime(self.runtime, device_id="synthetic-device")
        resumed.poll()
        self.assertFalse(path.exists())
        self.assertEqual(len(self.ledger.all("SELECT * FROM reader_open_observations")), 1)
        self.assertEqual(len(self.ledger.all("SELECT * FROM reader_event_receipts")), 1)

    def test_generation_escape_hash_origin_and_extra_fields_reject_preserve_original(self):
        self.reader.poll()
        variants = [{"generation_id": "other"}, {"path": "../outside.md"}, {"file_sha256": "a" * 64},
                    {"origin": "notification_click"}, {"visible": False}, {"execute": "known"},
                    {"content_revision": 999}, {"item_id": "spoofed-item"}]
        paths = [self.inbox(self.event(**changes)) for changes in variants]
        self.reader.poll()
        self.assertTrue(all(path.exists() for path in paths))
        self.assertEqual(self.ledger.all("SELECT * FROM reader_open_observations"), [])
        self.assertEqual(len(self.ledger.all("SELECT * FROM reader_event_receipts WHERE status='rejected'")), len(paths))
        self.assertIn("原文件保留", (self.publisher.root / "阅读状态.md").read_text())
        self.assertEqual(self.reader.poll()["open_events"], 0)

    def test_visible_event_rejects_unknown_version_symlink_and_oversize(self):
        self.reader.poll()
        note = self.publisher.root / self.publisher.resolve("stable-cat")
        original = note.read_bytes()
        note.write_text(patch_properties(note.read_text(), {"wgo_content_revision": 999}))
        unknown = self.inbox(self.event())
        codes = set()
        self.reader._consume_opens(codes)
        self.assertIn("reader_event_revision_unknown", codes)
        note.write_bytes(original)
        event = self.event()
        escaped = self.publisher.root / "escaped.md"
        escaped.symlink_to(note)
        link = self.inbox(event | {"path": "escaped.md"})
        large = self.inbox(event | {"event_id": uuid.uuid4().hex})
        large.write_bytes(b"x" * 8193)
        self.reader._consume_opens(codes)
        self.assertIn("projection_symlink", codes)
        self.assertIn("reader_event_too_large", codes)
        self.assertTrue(all(p.exists() for p in (unknown, link, large)))
        self.assertEqual(self.ledger.all("SELECT * FROM reader_open_observations"), [])

    def test_displayed_old_canonical_version_records_only_that_version(self):
        self.reader.poll()
        old = self.event()
        self.append(publish=False)
        self.inbox(old)
        self.reader.poll()
        row = self.ledger.one("SELECT * FROM reader_open_observations")
        self.assertEqual(row["content_revision"], 1)
        self.assertEqual(self.controls.preference("stable-cat").known_revision, 0)
        self.publish_current()
        stale = self.inbox(old | {"event_id": uuid.uuid4().hex})
        self.reader.poll()
        self.assertTrue(stale.exists())
        self.assertEqual(len(self.ledger.all("SELECT * FROM reader_open_observations")), 1)

    def test_source_checkbox_is_inert_even_with_real_action_id(self):
        self.reader.poll()
        action = self.ledger.one("SELECT * FROM reader_actions WHERE kind='known'")
        source = next((self.publisher.root / "来源").glob("*.md"))
        source.write_text(source.read_text() + "\n- [x] 知道了 <!-- wgo-action:" + action["action_id"] + " -->\n")
        self.reader.poll()
        self.assertEqual(self.controls.preference("stable-cat").known_revision, 0)

    def test_historical_startup_and_missing_floor_are_silent(self):
        self.append()
        self.wait()
        self.assertEqual(self.notifier.notifications, [])
        self.floor()
        self.reader.poll()
        self.assertEqual(self.reader.deliver_changes(), [])
        self.assertEqual(self.notifier.notifications, [])

    def test_wait_met_requires_ready_evidence_and_accepted_is_neither_read_nor_known(self):
        self.floor()
        self.wait()
        self.append(publish=False)
        result = self.reader.poll()
        self.assertEqual(self.notifier.notifications, [])
        self.assertEqual(result["watch_states"], {"met": 1})
        self.publish_current()
        source = next((self.publisher.root / "来源").glob("*.md"))
        saved = source.read_bytes()
        source.unlink()
        self.assertEqual(self.reader.deliver_changes()[0].status, "deferred")
        self.assertEqual(self.notifier.notifications, [])
        source.write_bytes(saved)
        self.assertEqual(self.reader.deliver_changes()[0].status, "accepted")
        event = self.notifier.notifications[0]
        self.assertIn("窗边", event.body)
        self.assertNotIn(".md", event.body)
        target = Path(self.runtime.delivery.resolve_click(event.event_id))
        renamed = target.parent / "自己的名字.md"
        target.rename(renamed)
        self.assertEqual(self.runtime.delivery.resolve_click(event.event_id), str(renamed))
        self.assertEqual(self.controls.preference("stable-cat").known_revision, 0)
        self.assertEqual(self.ledger.all("SELECT * FROM reader_open_observations"), [])
        self.inbox(self.event())
        self.reader.poll()
        self.assertEqual(len(self.ledger.all("SELECT * FROM reader_open_observations")), 1)
        self.assertEqual(self.controls.preference("stable-cat").known_revision, 0)
        self.assertEqual(len(self.notifier.notifications), 1)

    def test_mute_changed_during_watch_work_blocks_wire(self):
        self.floor()
        self.wait()
        self.append()
        original = self.reader.watches.sync_reader_watch
        def mutate(*args):
            result = original(*args)
            self.properties(mute=True)
            return result
        with patch.object(self.reader.watches, "sync_reader_watch", side_effect=mutate):
            self.reader.poll()
        self.assertTrue(self.controls.preference("stable-cat").mute)
        self.assertEqual(self.notifier.notifications, [])

    def test_unrelated_canonical_revision_does_not_drop_registered_pending_delivery(self):
        self.floor()
        self.wait()
        self.append(publish=False)
        self.reader.poll()
        observation, _span = self.canonical_extract(self.store, "unrelated", "邻居送来一碗面条。")
        self.canonical_actions(self.store, [{"action_key": "create-unrelated", "type": "CREATE_ITEM", "item_id": "noodles",
            "kind": "story", "title": "邻居的面条", "identity_scope": {"scene": "独立日常"},
            "observation_ids": [observation["observation_id"]]}])
        self.publish_current()
        self.reader.poll()
        self.assertEqual(len(self.notifier.notifications), 1)
        self.assertEqual(self.notifier.notifications[0].target_id, "stable-cat")

    def test_unknown_delivery_dedup_and_cancel_wait_retains_history(self):
        self.floor()
        self.wait()
        self.notifier.status = "raise"
        self.append()
        self.reader.poll()
        receipt = self.ledger.one("SELECT * FROM reader_deliveries")
        self.assertEqual(receipt["status"], "unknown")
        self.reader.poll()
        restarted = ReaderRuntime(self.runtime, notifier=self.notifier, device_id="synthetic-device", live=True)
        restarted.poll()
        self.assertEqual(len(self.notifier.notifications), 1)
        status = self.reader.watches.reader_watch_status("stable-cat")
        self.properties(wait="", follow=False)
        self.reader.poll()
        self.assertEqual(self.reader.watches.reader_watch_status("stable-cat")["status"], "stopped")
        self.assertTrue(self.reader.watches.witness_history(status["watch_id"]))
        self.append("周二，猫又坐在窗边。")
        self.reader.poll()
        self.assertEqual(len(self.notifier.notifications), 1)
        self.assertEqual(self.reader.watches.evaluate_affected(tuple(self.store.get_item("stable-cat")["claim_ids"])), ())

    def test_known_displayed_version_does_not_consume_new_content(self):
        self.floor()
        self.wait()
        self.check("known")
        self.reader.poll()
        self.assertEqual(self.controls.preference("stable-cat").known_revision, 1)
        self.append()
        self.reader.poll()
        self.assertEqual(len(self.notifier.notifications), 1)
        self.assertEqual(self.controls.preference("stable-cat").known_revision, 1)

    def test_reopened_and_correction_use_exact_claim_dependencies(self):
        self.floor()
        self.wait()
        self.append()
        self.reader.poll()
        original = next(c for c in self.store.get_item("stable-cat")["claims"] if c["predicates"]["success"])
        observation, _span = self.append("更正：刚才窗边的是抱枕，猫没有回来。", success=False)
        correction = next(c for c in self.store.get_item("stable-cat")["claims"]
                          if c["claim_id"] == observation["claims"][0]["claim_id"])
        self.store.record_claim_relation(correction["claim_id"], original["claim_id"], "corrects_or_retracts",
                                        reason="窗边误认为猫，后来确认是抱枕。", expected_revision=self.store.read_revision())
        self.publish_current()
        self.reader.poll()
        rows = self.ledger.all("SELECT * FROM reader_attention_events")
        events = [json.loads(row["event_json"]) for row in rows]
        reopened = next(event for event in events if event["kind"] == "wait_reopened")
        evidence = {ref["evidence_id"] for ref in reopened["evidence_targets"]}
        self.assertTrue(set(original["span_ids"]) <= evidence)
        self.assertTrue(set(correction["span_ids"]) <= evidence)
        self.assertIn("保留反证", (self.publisher.root / "阅读状态.md").read_text())

    def test_follow_only_ordinary_change_stays_quiet_but_exact_correction_delivers(self):
        self.floor()
        self.reader.poll()
        self.properties(follow=True)
        self.reader.poll()
        self.append()
        self.reader.poll()
        self.assertEqual(self.notifier.notifications, [])
        original = next(c for c in self.store.get_item("stable-cat")["claims"] if c["predicates"]["success"])
        observed, _span = self.append("更正：窗边的是抱枕。", success=False)
        self.store.record_claim_relation(observed["claims"][0]["claim_id"], original["claim_id"], "corrects_or_retracts",
            reason="窗边对象后来确认是抱枕。", expected_revision=self.store.read_revision())
        self.publish_current()
        self.reader.poll()
        self.assertEqual(len(self.notifier.notifications), 1)
        event = self.runtime.delivery._event(self.notifier.notifications[0].event_id)
        self.assertEqual(event.kind, "correction")
        self.assertEqual(self.controls.preference("stable-cat").wait, "")

    def followed_correction(self, *, publish=True):
        self.floor()
        self.reader.poll()
        self.properties(follow=True)
        self.reader.poll()
        self.append()
        self.reader.poll()
        self.assertEqual(self.notifier.notifications, [])
        original = next(c for c in self.store.get_item("stable-cat")["claims"] if c["predicates"]["success"])
        observed, _ = self.append("更正：窗边的是抱枕。", success=False, publish=publish)
        self.store.record_claim_relation(observed["claims"][0]["claim_id"], original["claim_id"], "corrects_or_retracts",
            reason="窗边对象后来确认是抱枕。", expected_revision=self.store.read_revision())
        if publish:
            self.publish_current()
        with closing(self.store.connect()) as conn:
            change_id = conn.execute("SELECT max(change_id) FROM change_events").fetchone()[0]
        return "correction:" + str(change_id), self.store.get_item("stable-cat")["content_revision"]

    def test_correction_before_later_same_item_detail_delivers_once_across_restart(self):
        source_event, _ = self.followed_correction()
        self.append("后续：她又给纸箱铺了一条毯子。")
        self.reader.poll()
        self.assertEqual(len(self.notifier.notifications), 1)
        notification = self.notifier.notifications[0]
        event = self.runtime.delivery._event(notification.event_id)
        self.assertEqual(event.source_event_id, source_event)
        self.assertEqual(event.target_revision, self.store.get_item("stable-cat")["content_revision"])
        self.reader.poll()
        self.append("第二天，她把毯子折好。")
        self.reader.poll()
        restarted = ReaderRuntime(self.runtime, notifier=self.notifier, device_id="synthetic-device", live=True)
        restarted.poll()
        self.assertEqual(len(self.notifier.notifications), 1)

    def test_correction_before_provenance_only_revision_still_delivers(self):
        self.followed_correction()
        extra, _ = self.canonical_extract(self.store, "later-proof", "补充来源重复记下那只纸箱。")
        self.canonical_actions(self.store, [{"action_key": "later-provenance", "type": "MERGE_INTO_ITEM",
            "item_id": "stable-cat", "observation_ids": [extra["observation_id"]],
            "modes": ["add_evidence"], "semantic_effect": "none"}])
        self.publish_current()
        self.reader.poll()
        self.assertEqual(len(self.notifier.notifications), 1)

    def test_unknown_correction_is_not_resent_after_later_same_item_detail(self):
        self.followed_correction()
        self.notifier.status = "raise"
        self.reader.poll()
        previous = self.ledger.one("SELECT * FROM reader_deliveries WHERE status='unknown'")
        self.assertIsNotNone(previous)
        self.append("后续：她给纸箱加了一条毯子。")
        self.reader.poll()
        self.assertEqual(len(self.notifier.notifications), 1)
        retained = self.ledger.one("SELECT * FROM reader_deliveries WHERE event_id=?", (previous["event_id"],))
        self.assertEqual(retained["receipt_id"], previous["receipt_id"])
        self.assertEqual(retained["status"], "unknown")

    def test_known_correction_is_not_new_attention_after_later_detail(self):
        _, revision = self.followed_correction()
        self.controls.mark_known("stable-cat", revision, known_at="2026-10-15T00:00:00+00:00")
        self.append("后续：她给纸箱加了一条毯子。")
        self.reader.poll()
        self.assertEqual(self.notifier.notifications, [])

    def test_marking_correction_known_during_preparation_blocks_notification(self):
        _, revision = self.followed_correction()
        self.append("后续：她给纸箱加了一条毯子。")
        original = self.reader._claim_dependencies
        def mark_known(*args):
            result = original(*args)
            self.controls.mark_known("stable-cat", revision, known_at="2026-10-15T00:00:00+00:00")
            return result
        with patch.object(self.reader, "_claim_dependencies", side_effect=mark_known):
            self.reader.poll()
        self.assertEqual(self.notifier.notifications, [])

    def test_not_sent_correction_can_use_current_revision_after_publication_recovers(self):
        source_event, _ = self.followed_correction(publish=False)
        self.reader.poll()
        self.assertEqual(self.notifier.notifications, [])
        self.append("后续：她给纸箱加了一条毯子。", publish=False)
        self.publish_current()
        self.reader.poll()
        self.assertEqual(len(self.notifier.notifications), 1)
        event = self.runtime.delivery._event(self.notifier.notifications[0].event_id)
        self.assertEqual(event.source_event_id, source_event)
        self.assertEqual(event.target_revision, self.store.get_item("stable-cat")["content_revision"])

    def brief(self):
        snapshot = self.runtime.adapter.snapshot("stable-cat")
        with closing(self.store.connect()) as conn:
            row = conn.execute("SELECT * FROM change_events ORDER BY change_id DESC LIMIT 1").fetchone()
        change = BriefChange(str(row["change_id"]), "stable-cat", snapshot.content_revision, snapshot.title,
            "之前的日常", "\n".join(unit.text for unit in snapshot.units), tuple(e.id for e in snapshot.evidence),
            "2026-10-12", "2026-10-13")
        response = {"status": "success", "title": "窗边的日常", "paragraphs": [{
            "text": "箱子里的猫又回到窗边，这段日常有了后续。", "change_ids": [change.id],
            "evidence_refs": [{"item_id": "stable-cat", "evidence_id": change.evidence_ids[-1]}], "scopes": [], "corrections": []}]}
        service = BriefingService(self.ledger, self.publisher, self.controls, ScriptedTextProvider(response), timezone="Asia/Singapore")
        return service.build((change,), now=datetime(2026, 10, 13, tzinfo=timezone.utc))

    def test_brief_delivery_requires_active_new_selected_changes_and_exact_target(self):
        historical = self.brief()
        self.assertEqual(historical.status, "success")
        self.assertIsNone(self.reader.deliver_brief(historical))
        self.floor()
        self.assertIsNone(self.reader.deliver_brief(historical))
        self.append()
        current = self.brief()
        self.assertEqual(current.status, "success", current)
        self.assertEqual(current.edition_id, historical.edition_id)
        self.assertEqual(self.reader.deliver_brief(current).status, "accepted")
        notification = self.notifier.notifications[0]
        event = self.runtime.delivery._event(notification.event_id)
        self.assertEqual(event.target_id, current.edition_id)
        self.assertEqual(event.content_bindings[0].content_revision, 2)
        self.assertEqual(len(event.evidence_targets), 1)
        self.assertEqual(self.reader.deliver_brief(current).status, "accepted")
        self.assertEqual(len(self.notifier.notifications), 1)
        with self.assertRaisesRegex(ReadingError, "attention_brief_receipt_stale"):
            self.reader.deliver_brief(historical)
        self.assertEqual(self.ledger.all("SELECT * FROM reader_open_observations"), [])
        raw = self.publisher.read(current.path)
        self.inbox(self.event(item_id=current.edition_id, content_revision=current.revision,
                              path=current.path, file_sha256=fingerprint(raw)))
        self.reader.poll()
        self.assertEqual(self.ledger.one("SELECT item_id FROM reader_open_observations")[0], current.edition_id)
        self.assertEqual(self.controls.preference("stable-cat").known_revision, 0)

    def test_default_non_live_bridge_records_silent_without_notifier(self):
        self.floor()
        self.wait()
        self.append()
        self.reader.live = False
        result = self.reader.poll()
        self.assertEqual(result["attention"], {"silent": 1})
        self.assertEqual(self.notifier.notifications, [])

    def test_watch_review_status_does_not_edit_management_actions(self):
        self.reader.poll()
        self.check("known")
        management = (self.publisher.root / "管理.md").read_bytes()
        self.reader.watches.create_watch(WatchSpec("review-watch", "occurrence", {"success": True},
            item_ids=("stable-cat",), review_after=1, created_at=0))
        evaluated = self.reader.watches.evaluate("review-watch")
        self.assertTrue(evaluated.review_due)
        self.reader._status_page([{"item_id": "stable-cat", "status": evaluated.state,
            "evaluation": asdict(evaluated), "reason": "合成观察", "counterevidence": []}], set())
        self.assertIn("已到复查时间", (self.publisher.root / "阅读状态.md").read_text())
        self.assertEqual((self.publisher.root / "管理.md").read_bytes(), management)


class ObsidianPluginTests(ReadingHarness):
    def _run_scheduler_case(self, scenario):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable; Obsidian scheduler mock not executed")
        plugin = Path(__file__).resolve().parents[1] / "integrations/obsidian/main.js"
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
class MarkdownView {}
class Plugin {
  constructor(app) { this.app = app; this.disposers = []; }
  registerEvent(ref) { this.disposers.push(() => ref.off()); }
  registerDomEvent(target, name, callback) {
    target.addEventListener(name, callback);
    this.disposers.push(() => target.removeEventListener(name, callback));
  }
  disposeRegistered() { for (const dispose of this.disposers.splice(0)) dispose(); }
}
const oldLoad = Module._load;
Module._load = function(name, ...rest) {
  if (name === 'obsidian') return {Plugin, MarkdownView, parseYaml: raw =>
    Object.fromEntries(raw.split('\n').filter(Boolean).map(line => {
      const cut = line.indexOf(':');
      return [line.slice(0, cut), JSON.parse(line.slice(cut + 1).trim())];
    }))};
  return oldLoad.call(this, name, ...rest);
};
const Observer = require(process.argv[1]);
Module._load = oldLoad;
const base = fs.realpathSync(process.argv[2]);
const scenario = process.argv[3];
function bus() {
  const callbacks = new Map();
  return {
    on(name, callback) {
      if (!callbacks.has(name)) callbacks.set(name, new Set());
      callbacks.get(name).add(callback);
      return {off: () => callbacks.get(name).delete(callback)};
    },
    emit(name, value) { for (const callback of [...(callbacks.get(name) || [])]) callback(value); }
  };
}
function document() {
  const doc = new EventTarget(), win = new EventTarget();
  doc.visibilityState = 'visible'; doc.focused = true;
  doc.hasFocus = () => doc.focused; doc.defaultView = win;
  doc.frames = new Map(); doc.requests = 0;
  win.getComputedStyle = () => ({display: 'block', visibility: 'visible'});
  win.requestAnimationFrame = callback => {
    const id = ++doc.requests; doc.frames.set(id, callback); return id;
  };
  win.cancelAnimationFrame = id => doc.frames.delete(id);
  doc.release = () => {
    for (const [id, callback] of [...doc.frames]) {
      doc.frames.delete(id); callback(0);
    }
  };
  return doc;
}
function pane(doc, name, id) {
  const view = new MarkdownView(), classes = new Set();
  view.file = {path: 'WGO/条目/' + name + '.md'};
  view.text = '---\nwgo_id: "' + id + '"\nwgo_generation: "generation"\n' +
    'wgo_content_revision: 3\n---\n合成的可见内容。\n';
  view.getViewData = () => view.text;
  view.containerEl = {ownerDocument: doc, isConnected: true,
    getBoundingClientRect: () => ({width: 640, height: 480}), classList: {
      toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
      remove: (...names) => names.forEach(name => classes.delete(name))
    }};
  view.leaf = {view, isDeferred: false}; view.classes = classes;
  fs.mkdirSync(path.dirname(path.join(base, view.file.path)), {recursive: true});
  fs.writeFileSync(path.join(base, view.file.path), view.text);
  return view;
}
const a = document(), b = document();
const first = pane(a, '旧合成', 'old'), second = pane(b, '当前合成', 'current');
let active = first, observeCalls = 0, serial = 0;
const flushes = [];
const workspace = Object.assign(bus(), {
  getActiveViewOfType: Type => active instanceof Type ? active : null,
  getLeavesOfType: () => [first.leaf, second.leaf],
  onLayoutReady: callback => { workspace.ready = callback; }
});
const vault = Object.assign(bus(), {adapter: {getBasePath: () => base}});
const observer = new Observer({workspace, vault});
const originalFlush = observer.flush.bind(observer);
observer.flush = () => { const work = originalFlush(); flushes.push(work); return work; };
observer.onload();
observer.writer.now = () => 100;
observer.writer.random = () => (++serial).toString(16).padStart(32, '0');
const originalObserve = observer.writer.observe.bind(observer.writer);
observer.writer.observe = () => { observeCalls++; return originalObserve(); };
const turn = () => new Promise(resolve => setImmediate(resolve));
function events() {
  const inbox = path.join(base, 'WGO/.wgo/reader-events');
  return fs.existsSync(inbox) ? fs.readdirSync(inbox).filter(n => n.endsWith('.json'))
    .map(n => JSON.parse(fs.readFileSync(path.join(inbox, n), 'utf8'))) : [];
}
function stop() { observer.onunload(); observer.disposeRegistered(); }
(async () => {
  workspace.ready();
  assert.equal(a.frames.size, 1);
  const lateFrame = [...a.frames.values()][0];
  if (scenario === 'supersede') {
    a.visibilityState = 'hidden'; a.focused = false; first.containerEl.isConnected = false;
    a.defaultView.closed = true;
    a.dispatchEvent(new Event('visibilitychange'));
    active = second;
    workspace.emit('active-leaf-change', second.leaf);
    for (let i = 0; i < 100; i++) workspace.emit('layout-change');
    b.defaultView.dispatchEvent(new Event('focus'));
    await turn();
    assert.equal(b.requests, 1, 'new focused document must get a frame without the old callback');
    assert.equal(observeCalls, 0, 'cancelling a frame is not visible-open evidence');
    assert.equal(flushes.length, 1, 'bursts must reuse the one running drain');
    b.release(); await flushes[0];
    assert.deepEqual(events().map(event => event.item_id), ['current']);
    assert.equal(observeCalls, 1);
    lateFrame(0); await turn();
    assert.equal(observeCalls, 1); assert.equal(events().length, 1);
  } else if (scenario === 'unload') {
    stop();
    await turn();
    assert.equal(observer.running, false, 'unload must release the wait before any old callback');
    assert.equal(observeCalls, 0); assert.equal(events().length, 0);
    lateFrame(0); await flushes[0];
    assert.equal(observeCalls, 0, 'a late frame must not enter observe after unload');
    assert.equal(events().length, 0);
    assert.equal(observer.running, false);
    const count = flushes.length;
    workspace.ready(); await turn();
    assert.equal(flushes.length, count, 'late layout-ready must not restart an unloaded plugin');
    assert.equal(observer.pending, false); assert.equal(first.classes.size, 0);
  } else if (scenario === 'epoch') {
    second.containerEl.ownerDocument = a;
    a.release();
    active = second;
    workspace.emit('active-leaf-change', second.leaf);
    await turn();
    assert.equal(a.requests, 2, 'same-document supersession needs its own current frame');
    assert.equal(observeCalls, 0);
    a.release(); await flushes[0];
    assert.deepEqual(events().map(event => event.item_id), ['current']);
  } else if (scenario === 'document') {
    a.release(); active = second; // The active document changes before queued continuation.
    await turn();
    assert.equal(b.requests, 1, 'an old-document frame must not authorize the current document');
    assert.equal(observeCalls, 0);
    b.release(); await flushes[0];
    assert.deepEqual(events().map(event => event.item_id), ['current']);
  } else if (scenario === 'hidden' || scenario === 'no-focus') {
    a.visibilityState = 'hidden'; first.containerEl.isConnected = false; active = second;
    if (scenario === 'hidden') b.visibilityState = 'hidden';
    else b.focused = false;
    workspace.emit('active-leaf-change', second.leaf);
    await turn(); lateFrame(0); await turn(); b.release(); await flushes[0];
    assert.equal(events().length, 0, 'hidden and unfocused current panes never finalize');
    lateFrame(0); await turn(); assert.equal(events().length, 0);
  } else if (scenario === 'normal') {
    assert.equal(observeCalls, 0);
    a.release(); await flushes[0];
    assert.deepEqual(events().map(event => event.item_id), ['old']);
    workspace.emit('layout-change'); await turn();
    assert.equal(a.requests, 2);
    a.release(); await flushes[1];
    assert.equal(events().length, 1, 'a layout-only refresh must keep content deduplication');
  } else throw new Error('unknown synthetic scheduler case');
  if (!observer.stopped) stop();
  assert.equal(observer.running, false);
  process.stdout.write('scheduler case passed: ' + scenario + '\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run([node, "-e", script, str(plugin), str(self.base), scenario],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("scheduler case passed: " + scenario, result.stdout)

    def test_scheduler_supersedes_stalled_old_document_and_coalesces_bursts(self):
        self._run_scheduler_case("supersede")

    def test_scheduler_unload_blocks_late_frame_and_layout_ready(self):
        self._run_scheduler_case("unload")

    def test_scheduler_rechecks_epoch_after_same_document_frame_callback(self):
        self._run_scheduler_case("epoch")

    def test_scheduler_rechecks_current_document_after_frame_callback(self):
        self._run_scheduler_case("document")

    def test_scheduler_stale_frames_cannot_finalize_hidden_or_unfocused_panes(self):
        for scenario in ("hidden", "no-focus"):
            with self.subTest(scenario=scenario):
                self._run_scheduler_case(scenario)

    def test_scheduler_normal_frame_path_preserves_deduplication(self):
        self._run_scheduler_case("normal")

    def test_reading_presentation_tracks_managed_panes_without_editing_notes(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable; Obsidian presentation mock not executed")
        plugin = Path(__file__).resolve().parents[1] / "integrations/obsidian/main.js"
        script = r'''
const assert = require('node:assert/strict');
const Module = require('node:module');
class MarkdownView {}
const oldLoad = Module._load;
Module._load = function(name, ...rest) {
  if (name === 'obsidian') return {Plugin: class {}, MarkdownView, parseYaml: raw => Object.fromEntries(raw.split('\n').filter(Boolean).map(line => {
    const cut = line.indexOf(':'); return [line.slice(0, cut), JSON.parse(line.slice(cut + 1).trim())];
  }))};
  return oldLoad.call(this, name, ...rest);
};
const {ReadingPresentation} = require(process.argv[1])._test;
const original = '---\nwgo_id: "cat"\nwgo_generation: "generation"\nwgo_content_revision: 3\n---\n# 猫的日常\n正文和证据保持原样。\n';
function pane(relative, text = original) {
  const classes = new Set();
  const view = new MarkdownView();
  view.file = {path: relative}; view.text = text; view.getViewData = () => view.text;
  view.containerEl = {classList: {
    toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
    remove: (...names) => names.forEach(name => classes.delete(name))
  }};
  return {view, classes};
}
const item = pane('WGO/条目/猫的日常.md');
const source = pane('WGO/来源/cat-0001.md', original.replace('"cat"', '"source:cat:1"'));
const ordinary = pane('日记.md');
const unowned = pane('WGO/手记.md', '# 用户手记\n原样保留。\n');
let leaves = [item, source, ordinary, unowned];
const presentation = new ReadingPresentation({workspace: {getLeavesOfType: type => {
  assert.equal(type, 'markdown'); return leaves;
}}});
presentation.sync();
assert(item.classes.has('wgo-reader-page')); assert(item.classes.has('wgo-reader-machine-only'));
assert(source.classes.has('wgo-reader-page')); assert(source.classes.has('wgo-reader-machine-only'));
assert.equal(ordinary.classes.size, 0); assert.equal(unowned.classes.size, 0);
assert.equal(item.view.text, original);
item.view.text = original.replace('\n---\n#', '\n关注: "下次变化"\n---\n#');
presentation.sync();
assert(item.classes.has('wgo-reader-page')); assert(!item.classes.has('wgo-reader-machine-only'));
assert(item.view.text.includes('关注: "下次变化"'));
item.view.file.path = '日记.md'; presentation.sync(); assert.equal(item.classes.size, 0);
item.view.file.path = 'WGO/../日记.md'; presentation.sync(); assert.equal(item.classes.size, 0);
item.view.file.path = 'WGO/.wgo/private.md'; presentation.sync(); assert.equal(item.classes.size, 0);
item.view.file.path = 'WGO/条目/猫的日常.md'; item.view.text = original; presentation.sync();
assert(item.classes.has('wgo-reader-page'));
item.isDeferred = true; presentation.sync(); assert.equal(item.classes.size, 0);
item.isDeferred = false; presentation.sync(); assert(item.classes.has('wgo-reader-page'));
item.view.text = '---\nwgo_id: invalid yaml\n---\n正文\n'; presentation.sync();
assert.equal(item.classes.size, 0);
item.view.text = original; presentation.sync();
leaves = [ordinary, unowned]; presentation.sync();
assert.equal(item.classes.size, 0); assert.equal(source.classes.size, 0);
leaves = [item, source]; presentation.sync(); presentation.unload();
assert.equal(item.classes.size, 0); assert.equal(source.classes.size, 0);
assert.equal(item.view.text, original);
process.stdout.write('scoped reading presentation assertions passed\n');
'''
        result = subprocess.run([node, "-e", script, str(plugin)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("presentation assertions passed", result.stdout)

    def test_mocked_actual_pane_only_and_private_atomic_event(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable; Obsidian producer mock not executed")
        plugin = Path(__file__).resolve().parents[1] / "integrations/obsidian/main.js"
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
class MarkdownView {}
const oldLoad = Module._load;
Module._load = function(name, ...rest) {
  if (name === 'obsidian') return {Plugin: class {}, MarkdownView, parseYaml: raw => Object.fromEntries(raw.split('\n').filter(Boolean).map(line => {
    const cut = line.indexOf(':'); return [line.slice(0, cut), JSON.parse(line.slice(cut + 1).trim())];
  }))};
  return oldLoad.call(this, name, ...rest);
};
const {VisibleOpenWriter} = require(process.argv[1])._test;
const base = fs.realpathSync(process.argv[2]);
fs.mkdirSync(path.join(base, 'WGO'), {recursive: true});
const relative = 'WGO/cat.md';
let text = '---\nwgo_id: "cat"\nwgo_generation: "generation"\nwgo_content_revision: 3\n---\n猫的日常。\n';
fs.writeFileSync(path.join(base, relative), text);
let focused = true, size = 50;
const doc = {visibilityState: 'visible', hasFocus: () => focused,
  defaultView: {getComputedStyle: () => ({display: 'block', visibility: 'visible'})}};
let active = new MarkdownView();
active.file = {path: relative};
active.getViewData = () => text;
active.containerEl = {isConnected: true, ownerDocument: doc, getBoundingClientRect: () => ({width: size, height: size})};
const app = {workspace: {getActiveViewOfType: () => active}, vault: {adapter: {getBasePath: () => base}}};
let n = 0;
const writer = new VisibleOpenWriter(app, {random: () => (++n).toString(16).padStart(32, '0'), now: () => 100});
(async () => {
  doc.visibilityState = 'hidden'; assert.equal(await writer.observe(), false);
  doc.visibilityState = 'visible'; focused = false; assert.equal(await writer.observe(), false);
  focused = true; size = 0; assert.equal(await writer.observe(), false);
  size = 50; active.leaf = {isDeferred: true}; assert.equal(await writer.observe(), false);
  active.leaf = {}; assert.equal(await writer.observe(), true);
  assert.equal(await writer.observe(), false);
  const inbox = path.join(base, 'WGO/.wgo/reader-events');
  const files = fs.readdirSync(inbox);
  assert.equal(files.length, 1); assert.match(files[0], /^[a-f0-9]{32}\.json$/);
  assert.equal(fs.statSync(inbox).mode & 0o777, 0o700);
  assert.equal(fs.statSync(path.join(inbox, files[0])).mode & 0o777, 0o600);
  const event = JSON.parse(fs.readFileSync(path.join(inbox, files[0]), 'utf8'));
  assert.equal(event.origin, 'obsidian_visible_open'); assert.equal(event.content_revision, 3);
  assert.equal(event.path, 'cat.md'); assert.equal(event.visible, true);
  assert.equal(Object.hasOwn(event, 'text'), false);
  writer.reset(); text += '尚未保存的编辑。'; assert.equal(await writer.observe(), false);
  text = text.replace('尚未保存的编辑。', '');
  active.file.path = 'outside.md'; assert.equal(await writer.observe(), false);
  active.file.path = 'WGO/../outside.md'; await assert.rejects(() => writer.observe(), /reader_path_invalid/);
  active.file.path = relative;
  const current = active; active = {}; assert.equal(await writer.observe(), false); active = current;
  app.vault.adapter.getBasePath = () => { focused = false; return base; };
  assert.equal(await writer.observe(), false);
  focused = true; app.vault.adapter.getBasePath = () => base;
  fs.renameSync(path.join(base, relative), path.join(base, 'cat-real.md'));
  fs.symlinkSync(path.join(base, 'cat-real.md'), path.join(base, relative));
  await assert.rejects(() => writer.observe(), /reader_path_unsafe/);
  assert.equal(fs.readdirSync(inbox).filter(p => p.endsWith('.json')).length, 1);
  process.stdout.write('visible-open producer assertions passed\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run([node, "-e", script, str(plugin), str(self.base)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("assertions passed", result.stdout)
