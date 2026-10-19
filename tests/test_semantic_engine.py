import json
import hashlib
import unittest
import tempfile
from dataclasses import asdict
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from ai.semantic_provider import DecisionAnswer, DecisionResult, ProviderError, ProviderRoute, TextResult
from core.semantic.engine import SemanticEngine
from core.semantic.store import SemanticStore
from core.url_safety import redact_urls_in_text

from core.semantic.engine import (CapturedSource, SemanticValidationError, bind_indexed_extraction,
                                  extraction_prompt, prepare_spans, validate_dispositions, validate_extraction)
from core.semantic.attribution import EXTRACTION_PROMPT_VERSION, captured_source_metadata, source_attribution


class AttributionTests(unittest.TestCase):
    def test_account_identity_survives_dates_chats_and_observed_name_changes(self):
        first = source_attribution("source-a", "chat-a", {"raw_sender_id": "wxid_synthetic_a", "sender": "小林", "timestamp": 1})
        later = source_attribution("source-a", "chat-b", {"raw_sender_id": "wxid_synthetic_a", "sender": "林同学", "timestamp": 100})
        self.assertEqual(first["speaker_id"], later["speaker_id"])
        self.assertEqual((first["speaker_name"], later["speaker_name"]), ("小林", "林同学"))
        self.assertNotEqual(first["speaker_id"], source_attribution("source-b", "chat-a", {"raw_sender_id": "wxid_synthetic_a"})["speaker_id"])

    def test_same_name_does_not_merge_ids_and_missing_id_stays_unknown(self):
        one = source_attribution("source", "chat", {"raw_sender_id": "one", "sender": "小林"})
        two = source_attribution("source", "chat", {"raw_sender_id": "two", "sender": "小林"})
        self.assertNotEqual(one["speaker_id"], two["speaker_id"])
        for chat in ("chat", "another"):
            self.assertEqual(source_attribution("source", chat, {"sender": "小林"}),
                             {"speaker_id": None, "speaker_name": "小林"})

    def test_local_id_requires_captured_shard_and_is_not_cross_chat_identity(self):
        payload = {"sender_id": 7, "sender": "小林", "source_envelope": {"db_shard_id": "shard-a"}}
        first = source_attribution("source", "chat-a", payload)["speaker_id"]
        self.assertIsNotNone(first)
        self.assertNotEqual(first, source_attribution("source", "chat-b", payload)["speaker_id"])
        self.assertNotEqual(first, source_attribution("source", "chat-a", {**payload, "source_envelope": {"db_shard_id": "shard-b"}})["speaker_id"])
        self.assertIsNone(source_attribution("source", "chat-a", {"sender_id": 7, "sender": "小林"})["speaker_id"])

    def test_raw_label_fallbacks_are_private_and_observed_name_uses_url_redactor(self):
        for payload in ({"raw_sender_id": "synthetic-account", "sender": "synthetic-account"},
                        {"sender": "wxid_synthetic_unknown"}, {"sender": "chat-a"},
                        {"raw_sender_id": "wxid_synthetic_a", "sender": "称呼 wxid_synthetic_a"}):
            self.assertIsNone(source_attribution("source", "chat-a", payload)["speaker_name"])
        label = "小林 https://example.test/name?token=synthetic-secret"
        self.assertEqual(source_attribution("source", "chat", {"sender": label})["speaker_name"], redact_urls_in_text(label))

    def test_explicit_subject_and_speaker_have_separate_scopes(self):
        payload = {"subject_anchor": "document-person", "raw_sender_id": "wxid_synthetic_a", "sender": "讲述者"}
        metadata = captured_source_metadata("source", "chat", payload)
        self.assertNotEqual(metadata["subject_id"], metadata["speaker_id"])
        self.assertIsNone(captured_source_metadata("source", "chat", {"raw_sender_id": "wxid_synthetic_a"})["subject_id"])


def observation(source, *, namespace="semantic", kind="life", text=None, unit_kind="scene", claims=None):
    span = prepare_spans([source], namespace=namespace)[0]
    return {"title": text or source.text, "scene_kind": kind,
            "spans": [{"source_id": source.source_id, "span_id": span.span_id}],
            "units": [{"kind": unit_kind, "text": text or source.text, "span_indices": [0]}],
            "topics": [], "claims": claims or []}


def indexed_observation(refs, first_text, last_text, last_time):
    return {"title": "合成技术场景", "scene_kind": "technical", "spans": refs,
        "units": [{"kind": "fact", "text": first_text, "span_indices": [0]},
                  {"kind": "fact", "text": last_text, "span_indices": [len(refs) - 1]}],
        "topics": [], "claims": [{"subject_id": None, "case_id": None, "condition": None,
            "channel": None, "version": None, "phase": None, "observed_at": last_time,
            "predicates": {"reported": True}, "span_indices": [len(refs) - 1]}]}


def model_observation(sources, *, title, scene_kind="life", units, claims=(), topics=()):
    """Synthetic model output for the wire actually supplied by this prompt.

    Fixtures describe unit/claim selections relative to their explicit sources;
    indexed prompts emit those same choices as flat page evidence indices.
    """
    indexed = "source_index" in sources[0]
    spans = [span for source in sources for span in source["spans"]]
    value = {"title": title, "scene_kind": scene_kind, "units": units, "topics": list(topics), "claims": list(claims)}
    if not indexed:
        return {**value, "spans": [{"source_id": source["source_id"], "span_id": span["span_id"]}
                                  for source in sources for span in source["spans"]]}
    value["evidence_indices"] = [span["evidence_index"] for span in spans]
    for collection in ("units", "claims"):
        value[collection] = [{**{key: item for key, item in entry.items() if key != "span_indices"},
            "evidence_indices": [spans[index]["evidence_index"] if type(index) is int and 0 <= index < len(spans) else index
                                 for index in entry["span_indices"]]} for entry in value[collection]]
    return value


def model_dispositions(entries):
    if entries and "source_index" in entries[0][0]:
        return [{"source_index": source["source_index"], **value} for source, value in entries]
    return {source["source_id"]: value for source, value in entries}


def freeze_legacy_primary(engine, batch_id, policy="retained-knowledge-v5"):
    """Freeze one synthetic historical page before the new-wire selector runs."""
    batch = engine.store.get_batch(batch_id)
    sources = tuple(CapturedSource(member["source_version_id"], member["text"], member["core"], member["source_sent_at"],
        **captured_source_metadata(batch["source_namespace"], batch["chat_id"], member["payload"])) for member in batch["members"])
    parent = engine.jobs.ensure("extract", {"batch_id": batch_id, "members": [asdict(source) for source in sources]},
        {"prompt": EXTRACTION_PROMPT_VERSION, "generation": engine.store.generation_id, "route": asdict(engine.text_provider.route)})
    versions = {"prompt": "semantic-extraction-pages-v2-attribution", "context": "previous-page-source-order-v1"}
    if policy is not None:
        versions["unit_policy"] = policy
    return engine.jobs.ensure("extract_page", {"parent_job_id": parent["job_id"], "page": 0,
        "span_ids": [span.span_id for span in prepare_spans(sources, namespace=engine.store.generation_id)],
        "source_ids": sorted(source.source_id for source in sources),
        "core_source_ids": [source.source_id for source in sources if source.core]}, versions)


class ExtractionTests(unittest.TestCase):
    def test_frozen_unit_policies_keep_their_original_prompt_bytes(self):
        sources = [CapturedSource("core", "终端错误值 A 应改成 B。", True, 1),
                   CapturedSource("context", "补充背景。", False, 2)]
        # Recorded before the v5 change, for the exact same public synthetic
        # sources. These identities protect each historical prompt, not style.
        expected = {
            None: "f291c0e255fb877973fe6d194492bdacd2de659e3d8e2d37704d456b66a4830e",
            "retained-knowledge-v1": "7c39267505836ac17d62082b2de03e568b7513b8f519ac454f6fee0bd57d8fc3",
            "retained-knowledge-v2": "45a0b69f2b031bdcb1596f619042e9e56802f820c8f4c324c787d4cf12f58c7b",
            "retained-knowledge-v3": "92213b21e56b74c96b8c164c6cf6972ff9ab05ba428c6fcc362dccd495079d92",
            "retained-knowledge-v4": "5d81a555112ba469ad7f627e0c54ff4832dddd34f92cf35bbc241031e4ed9470",
            "retained-knowledge-v5": "84b8896030f783e53b300c19e47f8e196cf6cf9273a22960d0d7cfde1bfbf0cc",
        }
        for policy, identity in expected.items():
            with self.subTest(policy=policy):
                prompt = extraction_prompt(sources, namespace="frozen-policy", unit_policy=policy)
                self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(), identity)
        fresh = extraction_prompt(sources, namespace="frozen-policy", unit_policy="retained-knowledge-v5")
        self.assertIn("先判断证据是否有值得留下的具体内容或完整场景", fresh)
        self.assertIn("不是按长度、特定词或消息类型", fresh)

    def test_multiple_independent_scenes_preserve_pure_life(self):
        sources = [CapturedSource("s1", "新版本可用，本地需要重启。"),
                   CapturedSource("s2", "今天沿河走了很久，回来买了一束花。")]
        raw = {"observations": [observation(sources[0], kind="technical"), observation(sources[1])]}
        result = validate_extraction(raw, sources)
        self.assertEqual([item.scene_kind for item in result], ["technical", "life"])
        self.assertEqual(result[1].units[0].text, sources[1].text)
        self.assertEqual(result[1].spans[0].excerpt, sources[1].text)

    def test_model_cannot_invent_span_or_offsets(self):
        source = CapturedSource("s1", "原始短句")
        raw = observation(source)
        raw["spans"][0]["span_id"] = "invented"
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_source_span"):
            validate_extraction({"observations": [raw]}, [source])
        raw["spans"] = [{"source_id": "s1", "start": 0, "end": 2}]
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_shape"):
            validate_extraction({"observations": [raw]}, [source])

    def test_context_only_rejected_and_replay_stable(self):
        source = CapturedSource("s1", "同一生活片段")
        raw = {"observations": [observation(source), observation(source)]}
        first = validate_extraction(raw, [source])
        self.assertEqual(len(first), 1)
        self.assertEqual(first, validate_extraction(raw, [source]))
        context = CapturedSource("s1", source.text, core=False)
        with self.assertRaisesRegex(SemanticValidationError, "semantic_context_without_core"):
            validate_extraction(raw, [context])

    def test_quote_and_nonfinite_claim_fail_closed(self):
        source = CapturedSource("s1", "今天真美")
        raw = observation(source, unit_kind="quote", text="模型编造的引语")
        with self.assertRaisesRegex(SemanticValidationError, "semantic_quote_not_grounded"):
            validate_extraction({"observations": [raw]}, [source])
        raw = observation(source)
        raw["claims"] = [{"subject_id": None, "case_id": None, "condition": None,
                           "channel": None, "version": None, "phase": None,
                           "observed_at": float("nan"), "predicates": {"success": True}, "span_indices": [0]}]
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_claim_time"):
            validate_extraction({"observations": [raw]}, [source])

    def test_prompt_redacts_urls_before_span_creation(self):
        source = CapturedSource("s1", "资料 https://example.test/a?token=secret-value")
        prompt = extraction_prompt([source])
        self.assertNotIn("secret-value", prompt)
        span = prepare_spans([source])[0]
        self.assertNotIn("secret-value", span.excerpt)
        self.assertEqual(span.end, len(span.excerpt))

    def test_zero_observations_is_valid_without_discarding_input(self):
        self.assertEqual(validate_extraction('{"observations":[]}', [CapturedSource("s", "嗯")]), ())
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_json"):
            validate_extraction('{"observations":[],"observations":[]}', [CapturedSource("s", "嗯")])

    def test_quote_can_cross_adjacent_program_spans_but_not_skip_a_gap(self):
        source = CapturedSource("s", "前" * 1998 + "原句跨越边界")
        raw = observation(source, text="原句跨越边界", unit_kind="quote")
        raw["spans"] = [{"source_id": source.source_id, "span_id": span.span_id} for span in prepare_spans([source])]
        raw["units"][0]["span_indices"] = [0, 1]
        self.assertEqual(validate_extraction({"observations": [raw]}, [source])[0].units[0].text, "原句跨越边界")

    def test_claim_cannot_borrow_uncited_identity_or_invent_event_timestamp(self):
        sources = [CapturedSource("s1", "旧测试", occurred_at=10, subject_id="person-a"),
                   CapturedSource("s2", "恢复公告", occurred_at=20, subject_id="person-b")]
        base = {"subject_id": "person-b", "case_id": None, "condition": None, "channel": None,
                "version": None, "phase": None, "observed_at": 10, "predicates": {"success": True}, "span_indices": [0]}
        raw = observation(sources[0], claims=[base])
        with self.assertRaisesRegex(SemanticValidationError, "semantic_unprovided_entity_or_case"):
            validate_extraction({"observations": [raw]}, sources)
        base["subject_id"], base["observed_at"] = "person-a", 21
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_claim_time"):
            validate_extraction({"observations": [raw]}, sources)

    def test_first_person_can_select_cited_speaker_but_reported_subject_stays_unknown(self):
        own = CapturedSource("self", "我今天完成了测试。", occurred_at=10, speaker_id="speaker-a", speaker_name="小林")
        report = CapturedSource("report", "我转述朋友的话：他昨天完成了测试。", occurred_at=11, speaker_id="speaker-b", speaker_name="阿青")
        claim = {"subject_id": "speaker-a", "case_id": None, "condition": None, "channel": None,
                 "version": None, "phase": None, "observed_at": 10, "predicates": {"completed": True}, "span_indices": [0]}
        values = [observation(own, claims=[claim]), observation(report, claims=[{**claim, "subject_id": None, "observed_at": 11}])]
        result = validate_extraction({"observations": values}, [own, report])
        self.assertEqual([o.claims[0]["subject_id"] for o in result], ["speaker-a", None])
        values[0]["claims"][0]["subject_id"] = "speaker-b"
        with self.assertRaisesRegex(SemanticValidationError, "semantic_unprovided_entity_or_case"):
            validate_extraction({"observations": values}, [own, report])

    def index_fixture(self):
        sources = [CapturedSource("synthetic-" + str(index), "技术说明 " + str(index), occurred_at=10 + index)
                   for index in range(21)]
        refs = [{"source_id": span.source_id, "span_id": span.span_id} for span in prepare_spans(sources)]
        return sources, indexed_observation(refs, sources[0].text, sources[-1].text, sources[-1].occurred_at)

    def test_span_indices_report_exact_unit_and_claim_paths_and_range(self):
        for collection, entry in (("units", 1), ("claims", 0)):
            with self.subTest(collection=collection):
                sources, value = self.index_fixture()
                value[collection][entry]["span_indices"] = [0, 21]
                with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_span_reference") as caught:
                    validate_extraction({"observations": [value]}, sources)
                self.assertEqual(str(caught.exception), "semantic_invalid_span_reference")
                self.assertEqual(getattr(caught.exception, "diagnostic", None), {
                    "path": ["observations", 0, collection, entry, "span_indices", 1],
                    "reason": "out_of_range", "value": 21, "value_type": "int",
                    "index_base": 0, "span_count": 21, "valid_range": [0, 20],
                    "span_index_map": [{"index": index, **ref} for index, ref in enumerate(value["spans"])],
                    "span_index_map_complete": True})

    def test_index_boundaries_accept_but_empty_noninteger_and_duplicate_refs_stay_strict(self):
        sources, value = self.index_fixture()
        accepted = validate_extraction({"observations": [value]}, sources)[0]
        self.assertEqual(accepted.units[0].span_ids, (value["spans"][0]["span_id"],))
        self.assertEqual(accepted.units[1].span_ids, (value["spans"][20]["span_id"],))
        self.assertEqual(accepted.claims[0]["span_ids"], (value["spans"][20]["span_id"],))
        for collection, entry in (("units", 1), ("claims", 0)):
            for invalid in ([], None, 21, "21", [True], [20.0], [float("nan")], [float("inf")],
                            [2 ** 4096], [-1], [21], [{}], [0, 0]):
                with self.subTest(collection=collection, invalid=invalid):
                    sources, value = self.index_fixture()
                    value[collection][entry]["span_indices"] = invalid
                    code = "semantic_duplicate_span_reference" if invalid == [0, 0] else "semantic_invalid_span_reference"
                    with self.assertRaisesRegex(SemanticValidationError, code) as caught:
                        validate_extraction({"observations": [value]}, sources)
                    if code == "semantic_invalid_span_reference":
                        json.dumps(caught.exception.diagnostic, allow_nan=False)
        sources, value = self.index_fixture()
        value["spans"] = []
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_observation"):
            validate_extraction({"observations": [value]}, sources)

    def test_span_reference_diagnostic_is_bounded_and_does_not_enter_error_text(self):
        sources, value = self.index_fixture()
        invalid = "synthetic-index-" * 1000
        value["units"][1]["span_indices"] = [invalid]
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_span_reference") as caught:
            validate_extraction({"observations": [value]}, sources)
        diagnostic = getattr(caught.exception, "diagnostic", None)
        self.assertIsInstance(diagnostic, dict)
        self.assertEqual(diagnostic["value"], invalid[:80])
        self.assertTrue(diagnostic["value_truncated"])
        self.assertLess(len(json.dumps(diagnostic)), 9000)
        self.assertTrue(diagnostic["span_index_map_complete"])
        self.assertNotIn("技术说明", json.dumps(diagnostic, ensure_ascii=False))
        self.assertNotIn("synthetic-index", str(caught.exception))

    def test_diagnostic_index_map_is_bounded_and_keeps_exact_validated_ids(self):
        for count, id_length in ((70, 8), (40, 300)):
            with self.subTest(count=count, id_length=id_length):
                sources = [CapturedSource("synthetic-" + str(index) + "x" * id_length, "技术说明 " + str(index),
                                          occurred_at=index) for index in range(count)]
                refs = [{"source_id": span.source_id, "span_id": span.span_id} for span in prepare_spans(sources)]
                value = indexed_observation(refs, sources[0].text, sources[-1].text, sources[-1].occurred_at)
                value["units"][1]["span_indices"] = [count]
                with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_span_reference") as caught:
                    validate_extraction({"observations": [value]}, sources)
                diagnostic = caught.exception.diagnostic
                mapping = diagnostic["span_index_map"]
                self.assertLessEqual(len(mapping), 64)
                self.assertLessEqual(len(json.dumps(mapping, ensure_ascii=False, separators=(",", ":")).encode()), 8192)
                self.assertLess(len(mapping), count)
                self.assertFalse(diagnostic["span_index_map_complete"])
                self.assertEqual(mapping, [{"index": index, **ref} for index, ref in enumerate(refs[:len(mapping)])])
        sources, value = self.index_fixture()
        value["spans"][20]["span_id"] = "unprovided-synthetic-span"
        value["units"][1]["span_indices"] = [21]
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_source_span") as caught:
            validate_extraction({"observations": [value]}, sources)
        self.assertIsNone(caught.exception.diagnostic)


def capture(store, batch_id, texts, *, payloads=None):
    return store.capture_batch(batch_id, [
        {"source_id": batch_id + "-" + str(index), "logical_shard_id": "synthetic-shard",
         "physical_generation": "synthetic-generation", "source_cursor_token": str(index + 1),
         "source_sent_at": 10 + index, "text": text, "payload": (payloads or [{} for _ in texts])[index]}
        for index, text in enumerate(texts)], source_namespace="synthetic", chat_id="group-a",
        inventory_digest="synthetic-inventory", inventory_revision=1, input_cursors={},
        output_cursors={"synthetic-shard": str(len(texts))}, source_eof=True)


class FakeText:
    route = ProviderRoute("synthetic", "text", "text")

    def __init__(self, *, noise=False, malformed=False, claims=False, consolidation="none"):
        self.calls = []
        self.prompt_sizes = []
        self.noise, self.malformed, self.claims = noise, malformed, claims
        self.consolidation = consolidation

    def generate(self, prompt, *, request_id, generation_id=""):
        self.calls.append(request_id)
        self.prompt_sizes.append(len(prompt.encode()))
        data = json.loads(prompt.rsplit("\n", 1)[-1])
        if prompt.startswith("The following captured"):
            values = []
            for source in data:
                if not source["core"] or self.noise:
                    continue
                text = "".join(span["excerpt"] for span in source["spans"])
                claims = []
                if self.claims:
                    claims = [{"subject_id": source["subject_id"], "case_id": source["case_id"],
                               "condition": "overseas", "channel": "app", "version": "v1", "phase": "test",
                               "observed_at": source["occurred_at"], "predicates": {"success": "failure" not in text},
                               "span_indices": [0]}]
                value = model_observation([source], title=text[:60], scene_kind="life" if "花" in text else "technical",
                    units=[{"kind": "scene", "text": text, "span_indices": list(range(len(source["spans"])))}], claims=claims)
                if self.malformed:
                    if "evidence_indices" in value:
                        value["evidence_indices"] = [-1]
                    else:
                        value["spans"][0]["span_id"] = "bad"
                values.append(value)
            result = {"observations": values}
            if self.noise:
                result["dispositions"] = model_dispositions([(source, {"status": "noise", "reason": "synthetic_empty_acknowledgment"})
                                                            for source in data if source["core"]])
        elif prompt.startswith("Summarize ONLY"):
            result = {"summary": "Bounded synthetic identity context.", "uncertain": False}
        elif prompt.startswith("Induce useful navigation"):
            result = {"groups": [{"view_id": next(iter(data["views"]), None), "title": "Synthetic scenes",
                                   "definition": "Independent synthetic scenes and their continuations.",
                                   "item_ids": list(data["items"]), "reason": "Synthetic navigation evidence"}], "unassigned": []}
        elif prompt.startswith("Review existing Item identities"):
            actions = []
            if self.consolidation == "merge" and len(data["items"]) > 1:
                actions = [{"action_key": request_id + ":merge", "type": "MERGE_ITEMS", "item_ids": list(data["items"]),
                            "target_item_id": data["focus_id"], "reason": "Synthetic exact case identity evidence"}]
            elif self.consolidation == "split" and len(data["items"][data["focus_id"]]["observation_ids"]) > 1:
                actions = [{"action_key": request_id + ":split", "type": "SPLIT_ITEM", "item_id": data["focus_id"],
                            "reason": "Synthetic independent scenes", "outputs": [
                                {"item_id": data["new_item_ids"][index], "kind": "story", "title": "Scene " + str(index),
                                 "identity_scope": {"synthetic_scene": str(index)}, "observation_ids": [identity]}
                                for index, identity in enumerate(data["items"][data["focus_id"]]["observation_ids"])]}]
            result = {"actions": actions, "novelty": {"considered_item_ids": list(data["items"]), "reason": "Synthetic complete identity review"}}
        else:
            actions = []
            for index, value in enumerate(data["observations"]):
                if data["items"]:
                    actions.append({"action_key": request_id + ":" + str(index), "type": "MERGE_INTO_ITEM",
                                    "item_id": next(iter(data["items"])), "observation_ids": [value["observation_id"]],
                                    "modes": ["append_detail", "add_evidence"], "semantic_effect": "detail_added"})
                else:
                    actions.append({"action_key": request_id + ":" + str(index), "type": "CREATE_ITEM",
                                    "item_id": data["new_item_ids"][index], "kind": "story", "title": value["title"],
                                    "identity_scope": {"scene": value["title"]}, "observation_ids": [value["observation_id"]]})
            result = {"actions": actions, "novelty": {"considered_item_ids": list(data["items"]), "reason": "synthetic full catalog examination"}}
        return TextResult(json.dumps(result, ensure_ascii=False), self.route)


class FakeDecision:
    route = ProviderRoute("synthetic", "decision", "decision")

    def __init__(self, *, defer=False, possible=.9):
        self.calls = []
        self.defer, self.possible = defer, possible

    def decide(self, request, *, request_id, generation_id=""):
        self.calls.append(request_id)
        answers = {}
        for question in request.questions:
            if question.type == "choice":
                choice = "defer" if self.defer else "apply" if "apply" in question.criteria else next(iter(question.criteria))
                answers[question.id] = DecisionAnswer("choice", choice=choice, confidence=1,
                                                      probabilities={key: float(key == choice) for key in question.criteria})
            elif question.type == "noul":
                answers[question.id] = DecisionAnswer("noul", noul=self.possible)
            else:
                answers[question.id] = DecisionAnswer("score", score=3, confidence=1,
                                                      probabilities={str(i): float(i == 3) for i in range(4)})
        return DecisionResult(answers, self.route)


class ExtractionSpanFeedbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SemanticStore(Path(temporary.name).resolve() / "knowledge.sqlite", "synthetic-generation")

    def provider(self, collection, *, with_context=False, never_repair=False, correction_error=None):
        class DiagnosticText(FakeText):
            def __init__(provider):
                super().__init__()
                provider.feedback = []
            def generate(provider, prompt, **kwargs):
                provider.calls.append(kwargs["request_id"])
                data = json.loads(prompt.rsplit("\n", 1)[-1])
                core = [source for source in data if source["core"]]
                refs = [{"source_id": source["source_id"], "span_id": source["spans"][0]["span_id"]} for source in core]
                value = indexed_observation(refs, core[0]["spans"][0]["excerpt"],
                    core[-1]["spans"][0]["excerpt"], core[-1]["occurred_at"])
                entry = 1 if collection == "units" else 0
                value[collection][entry]["span_indices"] = [21]
                observations = [value]
                if with_context:
                    context = next(source for source in data if not source["core"])
                    observations.insert(0, {"title": "旧场景", "scene_kind": "life", "spans": [
                        {"source_id": context["source_id"], "span_id": context["spans"][0]["span_id"]}],
                        "units": [{"kind": "quote", "span_indices": [0]}], "topics": [], "claims": []})
                if "Correct the prior JSON" in prompt:
                    feedback = json.loads(prompt.rsplit("\n", 2)[1])
                    provider.feedback.append(feedback)
                    if correction_error is not None:
                        raise correction_error
                    diagnostic = feedback.get("diagnostic", {})
                    path = ["observations", 1 if with_context else 0, collection, entry, "span_indices", 0]
                    selected = [item["index"] for item in diagnostic.get("span_index_map", [])
                        if item.get("source_id") == refs[-1]["source_id"] and item.get("span_id") == refs[-1]["span_id"]]
                    if (not never_repair and diagnostic.get("path") == path and diagnostic.get("value") == 21
                            and diagnostic.get("index_base") == 0 and diagnostic.get("span_count") == 21
                            and diagnostic.get("valid_range") == [0, 20] and selected == [20]):
                        value[collection][entry]["span_indices"] = selected
                return TextResult(json.dumps({"observations": observations}), provider.route)
        return DiagnosticText()

    def test_21_span_unit_and_claim_repair_requires_the_actual_diagnostic(self):
        for collection in ("units", "claims"):
            with self.subTest(collection=collection):
                batch = "span-feedback-" + collection
                capture(self.store, batch, ["合成技术说明 " + str(index) for index in range(21)])
                provider = self.provider(collection)
                engine = SemanticEngine(self.store, provider, FakeDecision())
                freeze_legacy_primary(engine, batch)
                with patch("core.semantic.engine.validate_extraction", wraps=validate_extraction) as validator:
                    result = engine.extract_batch(batch)
                self.assertEqual(validator.call_count, 2)
                self.assertEqual(len(provider.calls), 2)
                self.assertEqual(provider.calls[1], provider.calls[0] + ":attempt-2")
                self.assertEqual(provider.feedback[0]["validation_error"], "semantic_invalid_span_reference")
                self.assertIn("zero-based", provider.feedback[0]["constraint"])
                self.assertEqual(len(result["observations"]), 1)
                self.assertEqual(len(result["observations"][0]["spans"]), 21)
                target = result["observations"][0][collection][1 if collection == "units" else 0]
                self.assertEqual(target["span_ids"], [result["observations"][0]["spans"][20]["span_id"]])
                self.assertNotIn("diagnostic", engine.jobs.accepted(result["job_id"]))
                with patch.object(provider, "generate", side_effect=AssertionError("resent accepted extraction")), \
                     patch("core.semantic.engine.validate_extraction", side_effect=AssertionError("revalidated accepted extraction")):
                    self.assertEqual(engine.extract_batch(batch)["receipt"], result["receipt"])

    def test_diagnostic_path_keeps_the_original_observation_after_context_exclusion(self):
        sources = [CapturedSource("context", "旧上下文", core=False)] + [
            CapturedSource("core-" + str(index), "合成技术说明 " + str(index), occurred_at=10 + index)
            for index in range(21)]
        engine = SemanticEngine(self.store, self.provider("units", with_context=True), FakeDecision())
        child = engine.jobs.ensure("extract_page", {"synthetic": "feedback-path"}, {"unit_policy": "retained-knowledge-v5"})
        span_ids = [span.span_id for span in prepare_spans(sources, namespace=self.store.generation_id)]
        artifact = engine._accepted_extract_page(child, sources, span_ids, self.store.generation_id, self.store.generation_id)
        self.assertEqual(len(engine.text_provider.calls), 2)
        self.assertEqual(engine.text_provider.feedback[0]["diagnostic"]["path"][1], 1)
        self.assertEqual(len(artifact["observations"]), 1)
        self.assertEqual(len(artifact["excluded_context_results"]), 1)

    def test_invalid_span_references_exhaust_without_trimming_or_acceptance(self):
        capture(self.store, "invalid-forever", ["合成技术说明 " + str(index) for index in range(21)])
        provider = self.provider("units", never_repair=True)
        freeze_legacy_primary(SemanticEngine(self.store, provider, FakeDecision()), "invalid-forever")
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_span_reference"):
            SemanticEngine(self.store, provider, FakeDecision()).extract_batch("invalid-forever")
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(len(provider.feedback), 2)
        with closing(self.store.connect()) as db:
            job = db.execute("SELECT job_id,state,attempt FROM jobs WHERE kind='extract_page'").fetchone()
            self.assertEqual((job["state"], job["attempt"]), ("failed", 3))
            self.assertIsNone(db.execute("SELECT 1 FROM accepted_job_artifacts WHERE job_id=?", (job["job_id"],)).fetchone())
            self.assertEqual(db.execute("SELECT count(*) FROM extraction_receipts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM observations").fetchone()[0], 0)
        self.assertEqual(self.store.coverage()["unread_sources"], 21)

    def test_unknown_span_correction_keeps_the_original_child_unknown_on_resume(self):
        capture(self.store, "unknown-feedback", ["合成技术说明 " + str(index) for index in range(21)])
        provider = self.provider("claims", correction_error=ProviderError("synthetic_correction_unknown", outcome_unknown=True))
        engine = SemanticEngine(self.store, provider, FakeDecision())
        freeze_legacy_primary(engine, "unknown-feedback")
        with self.assertRaisesRegex(ProviderError, "synthetic_correction_unknown"):
            engine.extract_batch("unknown-feedback")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(provider.feedback), 1)
        sent = list(provider.calls)
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            SemanticEngine(self.store, provider, FakeDecision()).extract_batch("unknown-feedback")
        self.assertEqual(provider.calls, sent)
        with closing(self.store.connect()) as db:
            jobs = list(db.execute("SELECT state,attempt FROM jobs WHERE kind='extract_page'"))
            self.assertEqual([tuple(row) for row in jobs], [("unknown", 2)])
            self.assertEqual(db.execute("SELECT count(*) FROM extraction_receipts").fetchone()[0], 0)


class IndexedEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SemanticStore(Path(temporary.name).resolve() / "knowledge.sqlite", "synthetic-generation")

    def wire_fixture(self):
        sources = [CapturedSource("first", "甲" * 4100, occurred_at=10),
                   CapturedSource("second", "乙原话", occurred_at=11), CapturedSource("empty", "", occurred_at=12)]
        value = {"title": "合成经历", "scene_kind": "life", "evidence_indices": [3, 0],
            "units": [{"kind": "fact", "text": "甲的经历", "evidence_indices": [0]}], "topics": [],
            "claims": [{"subject_id": None, "case_id": None, "condition": None, "channel": None,
                "version": None, "phase": None, "observed_at": 11, "predicates": {"reported": True}, "evidence_indices": [3]}]}
        return sources, {"observations": [value]}

    def test_new_wire_is_not_accepted_by_the_historical_validator(self):
        sources, raw = self.wire_fixture()
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_shape"):
            validate_extraction(raw, sources)

    def test_flat_selection_binds_only_the_actually_selected_page_spans(self):
        sources, raw = self.wire_fixture()
        all_spans = prepare_spans(sources)
        selected = [all_spans[2].span_id, all_spans[0].span_id, all_spans[3].span_id]
        prompt = extraction_prompt(sources, span_ids=selected, unit_policy="retained-knowledge-v5",
                                   evidence_contract="indexed-evidence-v1")
        data = json.loads(prompt.rsplit("\n", 1)[1])
        self.assertEqual([[s["start"] for s in source["spans"]] for source in data], [[0, 4000], [0], []])
        self.assertEqual([[s["evidence_index"] for s in source["spans"]] for source in data], [[0, 1], [2], []])
        raw["observations"][0]["evidence_indices"] = [2, 1]
        raw["observations"][0]["units"][0]["evidence_indices"] = [1]
        raw["observations"][0]["claims"][0]["evidence_indices"] = [2]
        converted = bind_indexed_extraction(raw, sources, span_ids=selected)
        result = validate_extraction(converted, sources, span_ids=selected)[0]
        self.assertEqual([span.span_id for span in result.spans], [all_spans[3].span_id, all_spans[2].span_id])
        self.assertEqual(result.units[0].span_ids, (all_spans[2].span_id,))
        self.assertEqual(result.claims[0]["span_ids"], (all_spans[3].span_id,))
        raw["observations"][0]["evidence_indices"] = [3]
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_evidence_reference"):
            bind_indexed_extraction(raw, sources, span_ids=selected)

    def test_missing_extra_and_invalid_reference_shapes_are_rejected_on_the_wire(self):
        for target in ("observation", "unit", "claim"):
            for invalid in (None, "0", 0, [], [True], [0.0], [-1], [4], [0, 0], [{}]):
                with self.subTest(target=target, invalid=invalid):
                    sources, raw = self.wire_fixture()
                    value = raw["observations"][0]
                    path = ["observations", 0]
                    if target != "observation":
                        collection = "units" if target == "unit" else "claims"
                        value = value[collection][0]
                        path += [collection, 0]
                    value["evidence_indices"] = invalid
                    with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_evidence_reference") as caught:
                        bind_indexed_extraction(raw, sources)
                    diagnostic = caught.exception.diagnostic
                    self.assertEqual(diagnostic["path"][:len(path) + 1], path + ["evidence_indices"])
                    self.assertEqual(diagnostic["valid_range"], [0, 3])
            for action in ("missing", "extra"):
                sources, raw = self.wire_fixture()
                value = raw["observations"][0]
                if target != "observation":
                    value = value["units" if target == "unit" else "claims"][0]
                if action == "missing":
                    del value["evidence_indices"]
                else:
                    value["span_indices"] = [0]
                with self.subTest(target=target, action=action), self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_shape"):
                    bind_indexed_extraction(raw, sources)

    def test_unit_and_claim_choices_must_be_observation_subsets_without_union(self):
        for collection in ("units", "claims"):
            sources, raw = self.wire_fixture()
            raw["observations"][0][collection][0]["evidence_indices"] = [1]
            before = json.dumps(raw)
            with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_evidence_reference") as caught:
                bind_indexed_extraction(raw, sources)
            self.assertEqual(caught.exception.diagnostic["reason"], "not_selected_by_observation")
            self.assertEqual(caught.exception.diagnostic["allowed_indices"], [3, 0])
            self.assertEqual(json.dumps(raw), before)

    def test_quote_omits_text_and_original_contiguity_and_text_checks_stay_strict(self):
        for refs, valid in (([0, 1], True), ([0, 2], False), ([0, 3], False)):
            sources, raw = self.wire_fixture()
            value = raw["observations"][0]
            value.update(evidence_indices=refs, units=[{"kind": "quote", "evidence_indices": refs}], claims=[])
            converted = bind_indexed_extraction(raw, sources)
            if valid:
                result = validate_extraction(converted, sources)[0]
                self.assertEqual(result.units[0].text, "甲" * 4000)
            else:
                with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_quote_selection"):
                    validate_extraction(converted, sources)
        for kind in ("quote", "fact"):
            for text in ("", " ", None):
                sources, raw = self.wire_fixture()
                raw["observations"][0]["units"][0].update(kind=kind, text=text)
                with self.subTest(kind=kind, text=text), self.assertRaisesRegex(SemanticValidationError,
                        "semantic_invalid_shape" if kind == "quote" else "semantic_invalid_text"):
                    bind_indexed_extraction(raw, sources)

    def test_source_dispositions_bind_empty_sources_and_reject_bad_indices(self):
        sources, raw = self.wire_fixture()
        raw["observations"] = []
        raw["dispositions"] = [{"source_index": 2, "status": "context_only", "reason": "empty source"}]
        converted = bind_indexed_extraction(raw, sources)
        dispositions = validate_dispositions(converted, sources, ())
        self.assertEqual(dispositions["empty"]["status"], "context_only")
        self.assertEqual(dispositions["first"]["status"], "unclassified")
        for invalid in (None, "2", -1, 3, True, 2.0):
            raw["dispositions"][0]["source_index"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_source_index"):
                bind_indexed_extraction(raw, sources)
        for entries in ({}, [{}], [{"source_index": 2, "status": "context_only", "reason": "empty"}] * 2):
            raw["dispositions"] = entries
            with self.assertRaises(SemanticValidationError):
                bind_indexed_extraction(raw, sources)
        sources, raw = self.wire_fixture()
        raw["dispositions"] = [{"source_index": 0, "status": "context_only", "reason": "already cited"}]
        converted = bind_indexed_extraction(raw, sources)
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_disposition"):
            validate_dispositions(converted, sources, validate_extraction(converted, sources))

    def test_fresh_primary_binds_explicit_flat_indices_to_canonical(self):
        texts = ["甲" * 2200, "一句值得记住的原话。", ""]
        captured = capture(self.store, "indexed", texts)
        seen = []
        class IndexedText(FakeText):
            def generate(provider, prompt, **kwargs):
                provider.calls.append(kwargs["request_id"])
                seen.append(prompt)
                data = json.loads(prompt.rsplit("\n", 1)[-1])
                self.assertEqual([source["source_index"] for source in data], [0, 1, 2])
                self.assertEqual([[span["evidence_index"] for span in source["spans"]] for source in data], [[0, 1], [2], []])
                self.assertFalse(any("source_id" in source or any("span_id" in span for span in source["spans"]) for source in data))
                return TextResult(json.dumps({"observations": [{"title": "合成经历", "scene_kind": "life",
                    "evidence_indices": [2, 1], "units": [
                        {"kind": "fact", "text": "甲" * 200, "evidence_indices": [1]},
                        {"kind": "quote", "evidence_indices": [2]}], "topics": [], "claims": [{
                            "subject_id": None, "case_id": None, "condition": None, "channel": None,
                            "version": None, "phase": None, "observed_at": data[0]["occurred_at"],
                            "predicates": {"reported": True}, "evidence_indices": [1]}]}],
                    "dispositions": [{"source_index": 2, "status": "context_only", "reason": "empty captured source"}]}), provider.route)
        provider = IndexedText()
        engine = SemanticEngine(self.store, provider, FakeDecision())
        result = engine.extract_batch("indexed")
        value = result["observations"][0]
        expected = prepare_spans([CapturedSource(identity, text) for identity, text in zip(captured["member_version_ids"], texts)],
                                 namespace=self.store.generation_id)
        self.assertEqual([span["span_id"] for span in value["spans"]], [expected[2].span_id, expected[1].span_id])
        self.assertEqual(value["units"][0]["span_ids"], [expected[1].span_id])
        self.assertEqual(value["units"][1]["text"], texts[1])
        self.assertEqual(value["claims"][0]["span_ids"], [expected[1].span_id])
        with closing(self.store.connect()) as db:
            child = db.execute("SELECT versions_json,artifact_json FROM jobs JOIN accepted_job_artifacts USING(job_id) WHERE kind='extract_page'").fetchone()
            self.assertEqual(json.loads(child["versions_json"])["evidence_contract"], "indexed-evidence-v1")
            self.assertNotIn("evidence_indices", child["artifact_json"])
            self.assertEqual(db.execute("SELECT text FROM content_units WHERE kind='quote'").fetchone()[0], texts[1])
            self.assertEqual(db.execute("SELECT status FROM source_dispositions WHERE source_version_id=?", (captured["member_version_ids"][2],)).fetchone()[0], "context_only")
        with patch.object(provider, "generate", side_effect=AssertionError("accepted indexed page was resent")), \
             patch("core.semantic.engine.bind_indexed_extraction", side_effect=AssertionError("rebound accepted page")):
            self.assertEqual(engine.extract_batch("indexed")["receipt"], result["receipt"])

    def test_wire_correction_uses_flat_indices_and_original_paths(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                batch = "wire-correction-" + str(unknown)
                capture(self.store, batch, ["第一段合成经历。", "无关的第二段。", "第三段合成原话。"])
                prompts = []
                class Correctable(FakeText):
                    def generate(provider, prompt, **kwargs):
                        provider.calls.append(kwargs["request_id"])
                        prompts.append(prompt)
                        data = json.loads(prompt.rsplit("\n", 1)[-1])
                        selected = [1]
                        if "Correct the prior JSON" in prompt:
                            diagnostic = json.loads(prompt.rsplit("\n", 2)[1])["diagnostic"]
                            self.assertEqual(diagnostic["path"], ["observations", 0, "units", 0, "evidence_indices", 0])
                            self.assertEqual(diagnostic["value"], 1)
                            self.assertEqual(diagnostic["allowed_indices"], [2, 0])
                            self.assertNotIn("span_index_map", diagnostic)
                            if unknown:
                                raise ProviderError("synthetic_wire_unknown", outcome_unknown=True)
                            selected = [2]
                        return TextResult(json.dumps({"observations": [{"title": "两段经历", "scene_kind": "life",
                            "evidence_indices": [2, 0], "units": [{"kind": "quote", "evidence_indices": selected}],
                            "topics": [], "claims": []}], "dispositions": [{"source_index": 1, "status": "context_only",
                                                                                  "reason": "unrelated fragment"}]}), provider.route)
                provider = Correctable()
                engine = SemanticEngine(self.store, provider, FakeDecision())
                if unknown:
                    with self.assertRaisesRegex(ProviderError, "synthetic_wire_unknown"):
                        engine.extract_batch(batch)
                    sent = list(provider.calls)
                    with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
                        SemanticEngine(self.store, provider, FakeDecision()).extract_batch(batch)
                    self.assertEqual(provider.calls, sent)
                else:
                    result = engine.extract_batch(batch)
                    self.assertEqual(result["observations"][0]["units"][0]["text"], "第三段合成原话。")
                self.assertEqual(len(provider.calls), 2)
                self.assertEqual(provider.calls[1], provider.calls[0] + ":attempt-2")
                self.assertNotIn('"span_indices"', prompts[1])
                self.assertNotIn('"source_id"', prompts[1])
                self.assertEqual(prompts[0].rsplit("\n", 1)[1], prompts[1].rsplit("\n", 1)[1])

    def test_all_frozen_primary_policies_keep_old_wire_and_unknown_binding(self):
        for policy in (None, *("retained-knowledge-v" + str(index) for index in range(1, 6))):
            for unknown in (False, True):
                with self.subTest(policy=policy, unknown=unknown):
                    batch = "frozen-wire-" + str(policy) + str(unknown)
                    capture(self.store, batch, ["一段完整的合成经历。"])
                    provider = FakeText()
                    engine = SemanticEngine(self.store, provider, FakeDecision())
                    old = freeze_legacy_primary(engine, batch, policy)
                    if unknown:
                        engine.jobs.claim(old["job_id"], engine.worker_id)
                        engine.jobs.fail(old["job_id"], engine.worker_id, "synthetic_unknown", unknown=True)
                        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
                            engine.extract_batch(batch)
                        self.assertEqual(provider.calls, [])
                    else:
                        with patch("core.semantic.engine.extraction_prompt", wraps=extraction_prompt) as prompts:
                            engine.extract_batch(batch)
                        self.assertEqual(prompts.call_args.kwargs["unit_policy"], policy)
                        self.assertIsNone(prompts.call_args.kwargs["evidence_contract"])
                        sent = list(provider.calls)
                        with patch.object(provider, "generate", side_effect=AssertionError("accepted old page was resent")):
                            engine.extract_batch(batch)
                        self.assertEqual(provider.calls, sent)
                    with closing(self.store.connect()) as db:
                        child = db.execute("SELECT job_id,versions_json FROM jobs WHERE kind='extract_page' AND "
                            "json_extract(inputs_json,'$.parent_job_id')=?", (old["inputs"]["parent_job_id"],)).fetchall()
                        self.assertEqual([row["job_id"] for row in child], [old["job_id"]])
                        self.assertNotIn("evidence_contract", json.loads(child[0]["versions_json"]))

    def test_changed_route_cannot_fork_an_unknown_v2_parent_or_child(self):
        for old_wire in (False, True):
            for unknown_parent in (False, True):
                batch = "route-unknown-" + str(old_wire) + str(unknown_parent)
                capture(self.store, batch, ["合成的完整经历。"])
                old_provider = FakeText()
                engine = SemanticEngine(self.store, old_provider, FakeDecision())
                old = freeze_legacy_primary(engine, batch)
                if not old_wire:
                    # New-wire identity is frozen before any dispatch too.
                    with closing(self.store.connect()) as db:
                        db.execute("DELETE FROM jobs WHERE job_id=?", (old["job_id"],))
                        db.commit()
                    old = engine.jobs.ensure(old["kind"], old["inputs"], dict(old["versions"], evidence_contract="indexed-evidence-v1"))
                target = old["inputs"]["parent_job_id"] if unknown_parent else old["job_id"]
                engine.jobs.claim(target, engine.worker_id)
                engine.jobs.fail(target, engine.worker_id, "synthetic_unknown", unknown=True)
                changed = FakeText()
                changed.route = ProviderRoute("changed", "text", "text")
                with self.subTest(old_wire=old_wire, unknown_parent=unknown_parent), \
                     self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
                    SemanticEngine(self.store, changed, FakeDecision()).extract_batch(batch)
                self.assertEqual(changed.calls, [])
                with closing(self.store.connect()) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='extract' AND "
                        "json_extract(inputs_json,'$.batch_id')=?", (batch,)).fetchone()[0], 1)

    def test_changed_route_reuses_accepted_v2_parent_and_child_without_dispatch(self):
        for child_only in (False, True):
            batch = "route-accepted-" + str(child_only)
            capture(self.store, batch, ["完整的合成经历。"])
            engine = SemanticEngine(self.store, FakeText(), FakeDecision())
            if child_only:
                original = engine._accepted
                def stop_parent(job, callback):
                    if job["kind"] == "extract":
                        raise RuntimeError("synthetic accepted child")
                    return original(job, callback)
                with patch.object(engine, "_accepted", side_effect=stop_parent), self.assertRaisesRegex(RuntimeError, "accepted child"):
                    engine.extract_batch(batch)
            else:
                engine.extract_batch(batch)
            with closing(self.store.connect()) as db:
                before = [tuple(row) for row in db.execute("SELECT job_id,inputs_json,versions_json FROM jobs WHERE "
                    "kind IN ('extract','extract_page') AND (json_extract(inputs_json,'$.batch_id')=? OR "
                    "json_extract(inputs_json,'$.parent_job_id') IN (SELECT job_id FROM jobs WHERE kind='extract' AND "
                    "json_extract(inputs_json,'$.batch_id')=?)) ORDER BY job_id", (batch, batch))]
            changed = FakeText()
            changed.route = ProviderRoute("changed", "text", "text")
            with patch.object(changed, "generate", side_effect=AssertionError("resent accepted extraction under changed route")):
                result = SemanticEngine(self.store, changed, FakeDecision()).extract_batch(batch)
            self.assertIn(result["job_id"], [row[0] for row in before])
            with closing(self.store.connect()) as db:
                after = [tuple(row) for row in db.execute("SELECT job_id,inputs_json,versions_json FROM jobs WHERE "
                    "kind IN ('extract','extract_page') AND (json_extract(inputs_json,'$.batch_id')=? OR "
                    "json_extract(inputs_json,'$.parent_job_id')=?) ORDER BY job_id", (batch, result["job_id"]))]
            self.assertEqual(before, after)

    def test_changed_route_cannot_dispatch_a_pending_frozen_page(self):
        capture(self.store, "route-pending", ["完整的合成经历。"])
        engine = SemanticEngine(self.store, FakeText(), FakeDecision())
        old = freeze_legacy_primary(engine, "route-pending")
        changed = FakeText()
        changed.route = ProviderRoute("changed", "text", "text")
        with self.assertRaisesRegex(SemanticValidationError, "semantic_extraction_route_mismatch"):
            SemanticEngine(self.store, changed, FakeDecision()).extract_batch("route-pending")
        self.assertEqual(changed.calls, [])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='extract'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT job_id FROM jobs WHERE kind='extract_page'").fetchone()[0], old["job_id"])

    def test_frozen_parent_lookup_uses_the_existing_batch_index(self):
        capture(self.store, "parent-index", ["合成经历。"])
        engine = SemanticEngine(self.store, FakeText(), FakeDecision())
        freeze_legacy_primary(engine, "parent-index")
        original = self.store.connect
        queries = []
        def traced():
            connection = original()
            connection.set_trace_callback(lambda query: queries.append(query) if query.startswith(
                "SELECT inputs_json,versions_json,state,job_id FROM jobs ") else None)
            return connection
        with patch.object(self.store, "connect", side_effect=traced):
            engine.extract_batch("parent-index")
        self.assertEqual(len(queries), 1)
        with closing(original()) as db:
            plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + queries[0]))
        self.assertIn("USING INDEX semantic_jobs_batch", plan)

    def test_conflicting_v2_parents_fail_closed_with_unknown_child_priority(self):
        for unknown in (False, True):
            batch = "conflicting-parent-" + str(unknown)
            capture(self.store, batch, ["完整的合成经历。"])
            provider = FakeText()
            engine = SemanticEngine(self.store, provider, FakeDecision())
            child = freeze_legacy_primary(engine, batch)
            with closing(self.store.connect()) as db:
                parent = db.execute("SELECT inputs_json,versions_json FROM jobs WHERE job_id=?", (child["inputs"]["parent_job_id"],)).fetchone()
            versions = json.loads(parent["versions_json"])
            versions["route"] = asdict(ProviderRoute("changed", "text", "text"))
            engine.jobs.ensure("extract", json.loads(parent["inputs_json"]), versions)
            if unknown:
                engine.jobs.claim(child["job_id"], engine.worker_id)
                engine.jobs.fail(child["job_id"], engine.worker_id, "synthetic_unknown", unknown=True)
            with self.assertRaisesRegex(SemanticValidationError,
                    "semantic_job_busy_or_unknown" if unknown else "semantic_extraction_version_conflict"):
                engine.extract_batch(batch)
            self.assertEqual(provider.calls, [])

    def test_repeated_new_wire_errors_exhaust_without_artifact_or_canonical_write(self):
        capture(self.store, "wire-exhausted", ["一段合成原话。"])
        class InvalidQuote(FakeText):
            def generate(provider, prompt, **kwargs):
                provider.calls.append(kwargs["request_id"])
                return TextResult(json.dumps({"observations": [{"title": "原话", "scene_kind": "quote",
                    "evidence_indices": [0], "units": [{"kind": "quote", "text": "", "evidence_indices": [0]}],
                    "topics": [], "claims": []}]}), provider.route)
        provider = InvalidQuote()
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_shape"):
            SemanticEngine(self.store, provider, FakeDecision()).extract_batch("wire-exhausted")
        self.assertEqual(len(provider.calls), 3)
        with closing(self.store.connect()) as db:
            self.assertEqual(tuple(db.execute("SELECT state,attempt FROM jobs WHERE kind='extract_page'").fetchone()), ("failed", 3))
            for table in ("accepted_job_artifacts", "observations", "source_spans", "extraction_receipts", "processing_receipts"):
                self.assertEqual(db.execute("SELECT count(*) FROM " + table).fetchone()[0], 0, table)


class ProposalCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.new_fixture()

    def new_fixture(self, reading_policy=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SemanticStore(Path(temporary.name).resolve() / "knowledge.sqlite", "synthetic-generation",
                                   now_func=lambda: 1000)
        if reading_policy is not None:
            self.fixture_reading_policy(reading_policy)
        capture(self.store, "proposal", ["一个完整且独立的合成生活场景。"])
        self.extracted = SemanticEngine(self.store, FakeText(), FakeDecision()).extract_batch("proposal")

    def fixture_reading_policy(self, policy):
        # Synthetic generation binding only. Tests that alter it after freezing
        # probe the no-rebinding boundary; they do not authorize a runtime switch.
        with self.store.transaction() as db:
            metadata = json.loads(db.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0])
            metadata["reading_policy"] = policy
            db.execute("UPDATE generation_manifest SET metadata_json=?", (json.dumps(metadata),))

    def engine(self, *outputs, title=None):
        store = self.store
        class ScriptedText(FakeText):
            def __init__(provider):
                super().__init__()
                provider.prompts, provider.claims = [], []
            def generate(provider, prompt, **kwargs):
                provider.prompts.append(prompt)
                with closing(store.connect()) as db:
                    provider.claims.append(tuple(db.execute(
                        "SELECT job_id,state,worker_id,attempt FROM jobs WHERE job_id=?",
                        (kwargs["request_id"].split(":text")[0],)).fetchone()))
                result = super().generate(prompt, **kwargs)
                output = outputs[min(len(provider.prompts) - 1, len(outputs) - 1)]
                if isinstance(output, Exception):
                    raise output
                if output in {"empty", "non-text"}:
                    return TextResult("" if output == "empty" else None, result.route)
                value = json.loads(result.text)
                if title is not None:
                    value["actions"][0]["title"] = title
                if output in {"missing", "wrong-route-invalid"}:
                    value["actions"][0].pop("action_key")
                    value["actions"][0].pop("item_id")
                route = ProviderRoute("synthetic", "unexpected", "text") if output.startswith("wrong-route") else result.route
                return TextResult(json.dumps(value, ensure_ascii=False), route)
        text, decision = ScriptedText(), FakeDecision()
        return SemanticEngine(self.store, text, decision, worker_id="proposal-worker"), text, decision

    def rows(self, sql, params=()):
        with closing(self.store.connect()) as db:
            return [dict(row) for row in db.execute(sql, params)]

    def freeze(self, engine):
        frozen = []
        accepted = engine._accepted
        def before_dispatch(job, callback):
            if job["kind"] == "propose":
                frozen.append(job)
                raise RuntimeError("synthetic before proposal dispatch")
            return accepted(job, callback)
        with patch.object(engine, "_accepted", side_effect=before_dispatch):
            with self.assertRaisesRegex(RuntimeError, "before proposal dispatch"):
                engine.resolve_batch("proposal", extracted=self.extracted)
        return frozen[0]

    def test_missing_action_keys_are_corrected_in_one_claim_and_committed_once(self):
        engine, text, _ = self.engine("missing", "valid")
        result = engine.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(text.calls), 2)
        job = self.rows("SELECT * FROM jobs WHERE kind='propose'")[0]
        self.assertEqual(text.calls, [job["job_id"] + ":text", job["job_id"] + ":text:correction-1"])
        self.assertEqual(text.claims, [(job["job_id"], "running", "proposal-worker", 1)] * 2)
        self.assertEqual((job["state"], job["attempt"]), ("done", 1))
        self.assertEqual(len(self.rows("SELECT * FROM action_receipts WHERE job_id=?", (job["job_id"],))), 1)
        self.assertEqual(len(self.rows("SELECT * FROM canonical_transactions WHERE job_id=?", (job["job_id"],))), 1)
        self.assertEqual(len(self.store.list_items()), 1)
        self.store.verify_processing_receipt(result["processing_receipt"])
        self.assertEqual(text.prompts[0].rsplit("\n", 1)[1], text.prompts[1].rsplit("\n", 1)[1])
        diagnostic = json.loads(text.prompts[1].rsplit("\n", 2)[1])
        self.assertEqual(diagnostic["validation_error"], "semantic_invalid_shape")
        required = diagnostic["contract"]["actions"]["CREATE_ITEM"]["required"]
        self.assertTrue({"type", "action_key", "item_id", "observation_ids", "title", "kind", "identity_scope"} <= set(required))
        for contract in diagnostic["contract"]["actions"].values():
            self.assertTrue({"type", "action_key"} <= set(contract["required"]))
        self.assertNotIn("action_key", json.loads(diagnostic["rejected_json"])["actions"][0])

    def test_last_allowed_correction_can_complete_the_same_claim(self):
        engine, text, _ = self.engine("missing", "missing", "valid")
        self.assertEqual(engine.resolve_batch("proposal", extracted=self.extracted)["status"], "complete")
        job = self.rows("SELECT * FROM jobs WHERE kind='propose'")[0]
        self.assertEqual(text.claims, [(job["job_id"], "running", "proposal-worker", 1)] * 3)
        self.assertEqual(len(self.rows("SELECT * FROM action_receipts")), 1)

    def test_three_known_invalid_outputs_exhaust_without_artifact_or_commit(self):
        engine, text, decision = self.engine("missing")
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_shape"):
            engine.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual(len(text.calls), 3)
        job = self.rows("SELECT * FROM jobs WHERE kind='propose'")[0]
        self.assertEqual(text.calls, [job["job_id"] + suffix for suffix in
                                    (":text", ":text:correction-1", ":text:correction-2")])
        self.assertEqual((job["state"], job["attempt"]), ("failed", 1))
        self.assertEqual(len(self.rows("SELECT * FROM job_attempts WHERE job_id=?", (job["job_id"],))), 1)
        self.assertFalse(self.rows("SELECT * FROM accepted_job_artifacts WHERE job_id=?", (job["job_id"],)))
        self.assertFalse(self.rows("SELECT * FROM action_receipts"))
        self.assertFalse(self.rows("SELECT * FROM processing_receipts"))
        self.assertEqual(decision.calls, [])

    def test_unknown_correction_blocks_resume_without_another_call(self):
        engine, text, decision = self.engine("missing", ProviderError("synthetic_correction_unknown", outcome_unknown=True))
        with self.assertRaisesRegex(ProviderError, "synthetic_correction_unknown"):
            engine.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual(len(text.calls), 2)
        job = self.rows("SELECT * FROM jobs WHERE kind='propose'")[0]
        self.assertEqual((job["state"], job["attempt"]), ("unknown", 1))
        sent = list(text.calls), list(decision.calls)
        self.store.create_view("later-view", "后来的合成上下文", "changed after unknown")
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            SemanticEngine(self.store, text, decision).resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual((text.calls, decision.calls), sent)
        self.assertEqual(len(self.rows("SELECT * FROM jobs WHERE kind='propose'")), 1)
        self.assertFalse(self.rows("SELECT * FROM accepted_job_artifacts WHERE job_id=?", (job["job_id"],)))
        self.assertFalse(self.rows("SELECT * FROM processing_receipts"))

    def test_accepted_proposal_replays_without_validation_or_provider_calls(self):
        engine, text, decision = self.engine("valid")
        with patch.object(self.store, "apply_proposal", side_effect=RuntimeError("synthetic before commit")):
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                engine.resolve_batch("proposal", extracted=self.extracted)
        job = self.rows("SELECT * FROM jobs WHERE kind='propose'")[0]
        artifact = self.rows("SELECT * FROM accepted_job_artifacts WHERE job_id=?", (job["job_id"],))[0]
        sent = list(text.calls), list(decision.calls)
        with patch("core.semantic.engine.validate_proposal", side_effect=AssertionError("revalidated accepted proposal")):
            result = engine.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual(result["status"], "complete")
        self.assertEqual((text.calls, decision.calls), sent)
        self.assertEqual(self.rows("SELECT * FROM accepted_job_artifacts WHERE job_id=?", (job["job_id"],)), [artifact])

    def test_provider_and_wrong_route_failures_do_not_trigger_correction(self):
        for output, error in ((ProviderError("synthetic_proposal_failed"), "synthetic_proposal_failed"),
                              (ProviderError("synthetic_proposal_unknown", outcome_unknown=True), "synthetic_proposal_unknown"),
                              ("wrong-route-invalid", "text_provider_route_changed"),
                              ("wrong-route-valid", "text_provider_route_changed"),
                              ("empty", "text_invalid_response"), ("non-text", "text_invalid_response")):
            with self.subTest(output=error):
                self.new_fixture()
                engine, text, _ = self.engine(output)
                with self.assertRaisesRegex((ProviderError, SemanticValidationError), error):
                    engine.resolve_batch("proposal", extracted=self.extracted)
                self.assertEqual(len(text.calls), 1)
                self.assertFalse(self.rows("SELECT * FROM accepted_job_artifacts a JOIN jobs j USING(job_id) WHERE j.kind='propose'"))

    def test_provider_or_route_failure_during_correction_stops_the_claim(self):
        for output, error in ((ProviderError("synthetic_correction_failed"), "synthetic_correction_failed"),
                              ("wrong-route-invalid", "text_provider_route_changed")):
            with self.subTest(error=error):
                self.new_fixture()
                engine, text, _ = self.engine("missing", output, "valid")
                with self.assertRaisesRegex(ProviderError, error):
                    engine.resolve_batch("proposal", extracted=self.extracted)
                self.assertEqual(len(text.calls), 2)
                job = self.rows("SELECT * FROM jobs WHERE kind='propose'")[0]
                self.assertEqual((job["state"], job["attempt"]), ("failed", 1))
                self.assertFalse(self.rows("SELECT * FROM action_receipts"))

    def test_non_model_validation_failure_does_not_trigger_correction(self):
        for error in (RuntimeError("synthetic validator bug"), SemanticValidationError("semantic_incomplete_read_set")):
            with self.subTest(error=str(error)):
                self.new_fixture()
                engine, text, _ = self.engine("valid")
                with patch("core.semantic.engine.validate_proposal", side_effect=error):
                    with self.assertRaisesRegex(type(error), str(error)):
                        engine.resolve_batch("proposal", extracted=self.extracted)
                self.assertEqual(len(text.calls), 1)

    def test_frozen_pending_and_failed_jobs_keep_v1_inputs_and_initial_prompt(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                self.new_fixture()
                engine, text, _ = self.engine("missing", "valid")
                old = self.freeze(engine)
                self.assertEqual(old["versions"]["prompt"], "semantic-proposal-v1")
                historical = []
                if failed:
                    engine.jobs.claim(old["job_id"], "historical-worker")
                    engine.jobs.fail(old["job_id"], "historical-worker", "semantic_invalid_shape")
                    historical = self.rows("SELECT * FROM job_attempts WHERE job_id=?", (old["job_id"],))
                result = engine.resolve_batch("proposal", extracted=self.extracted)
                self.assertEqual(result["status"], "complete")
                jobs = self.rows("SELECT * FROM jobs WHERE kind='propose'")
                self.assertEqual([job["job_id"] for job in jobs], [old["job_id"]])
                self.assertEqual(json.loads(jobs[0]["inputs_json"]), old["inputs"])
                self.assertEqual(json.loads(jobs[0]["versions_json"]), old["versions"])
                self.assertEqual(jobs[0]["attempt"], 2 if failed else 1)
                self.assertEqual(text.calls[0], old["job_id"] + ":text")
                # Recorded from public v1 source at 1733de0. Source identities
                # vary with each temporary database; their exact frozen JSON
                # remains the last line, separate from the unchanged instructions.
                instructions, payload = text.prompts[0].rsplit("\n", 1)
                self.assertEqual(hashlib.sha256(instructions.encode()).hexdigest(),
                                 "6461e305fb6e38136871e5c9e25a10637fdbf0f55e6a25b712c6894f27286b98")
                self.assertEqual(payload, json.dumps(old["inputs"]["state"], ensure_ascii=False,
                                                    separators=(",", ":"), sort_keys=True))
                if failed:
                    self.assertEqual(self.rows("SELECT * FROM job_attempts WHERE job_id=? AND attempt=1",
                                               (old["job_id"],)), historical)

    def test_frozen_unknown_job_remains_unknown_without_dispatch(self):
        engine, text, _ = self.engine("valid")
        old = self.freeze(engine)
        engine.jobs.claim(old["job_id"], "historical-worker")
        engine.jobs.fail(old["job_id"], "historical-worker", "synthetic_unknown", unknown=True)
        before = self.rows("SELECT * FROM jobs WHERE kind='propose'")
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            engine.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual(text.calls, [])
        self.assertEqual(self.rows("SELECT * FROM jobs WHERE kind='propose'"), before)

    def test_source_composition_v2_naming_contract_survives_known_output_correction(self):
        self.new_fixture("source-composition-v1")
        engine, text, _ = self.engine("missing", "valid", title="等待时长上限的设置")
        result = engine.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual(result["status"], "complete")
        job = self.rows("SELECT * FROM jobs WHERE kind='propose'")[0]
        self.assertEqual(json.loads(job["versions_json"])["prompt"], "semantic-proposal-v2")
        self.assertEqual(text.calls, [job["job_id"] + ":text", job["job_id"] + ":text:correction-1"])
        self.assertEqual(text.claims, [(job["job_id"], "running", "proposal-worker", 1)] * 2)
        for prompt in text.prompts:
            self.assertIn("short, natural topic name", prompt)
            self.assertIn("source material's language", prompt)
            self.assertIn("wait-duration limit", prompt)
            self.assertIn("retry-count limit", prompt)
            self.assertIn("Do not concatenate every person", prompt)
            self.assertIn("Do not promote guesses", prompt)
            self.assertIn("Do not infer gender or identity", prompt)
            self.assertIn("unread attachments", prompt)
        self.assertEqual(text.prompts[0].rsplit("\n", 1)[1], text.prompts[1].rsplit("\n", 1)[1])
        self.assertEqual(self.store.list_items()[0]["title"], "等待时长上限的设置")

    def test_other_reading_modes_keep_exact_v1_prompt_contract(self):
        for policy in (None, "derived-verbatim-v1", "content-responsibility-v1"):
            with self.subTest(policy=policy):
                self.new_fixture(policy)
                engine, text, _ = self.engine("valid")
                engine.resolve_batch("proposal", extracted=self.extracted)
                job = self.rows("SELECT * FROM jobs WHERE kind='propose'")[0]
                self.assertEqual(json.loads(job["versions_json"])["prompt"], "semantic-proposal-v1")
                instructions, payload = text.prompts[0].rsplit("\n", 1)
                self.assertEqual(hashlib.sha256(instructions.encode()).hexdigest(),
                                 "6461e305fb6e38136871e5c9e25a10637fdbf0f55e6a25b712c6894f27286b98")
                self.assertEqual(json.loads(payload), json.loads(job["inputs_json"])["state"])
                self.assertNotIn("Item naming contract", text.prompts[0])

    def test_editorial_composition_freezes_v2_and_replays_without_calls(self):
        self.new_fixture("editorial-composition-v1")
        engine, text, decision = self.engine("valid", title="合成场景")
        result = engine.resolve_batch("proposal", extracted=self.extracted)
        frozen = self.rows("SELECT * FROM jobs WHERE kind='propose'")
        self.assertEqual(json.loads(frozen[0]["versions_json"])["prompt"], "semantic-proposal-v2")
        self.assertEqual(self.store.list_items()[0]["title"], "合成场景")
        calls = (list(text.calls), list(decision.calls))
        self.assertEqual(engine.resolve_batch("proposal", extracted=self.extracted)["receipt"], result["receipt"])
        self.assertEqual((text.calls, decision.calls), calls)
        self.assertEqual(self.rows("SELECT * FROM jobs WHERE kind='propose'"), frozen)

    def test_frozen_v1_and_v2_pending_jobs_keep_their_prompt_after_metadata_drift(self):
        for initial, changed, version in ((None, "source-composition-v1", "semantic-proposal-v1"),
                                           ("source-composition-v1", "content-responsibility-v1", "semantic-proposal-v2")):
            with self.subTest(version=version):
                self.new_fixture(initial)
                engine, text, _ = self.engine("valid")
                frozen = self.freeze(engine)
                self.assertEqual(frozen["versions"]["prompt"], version)
                self.fixture_reading_policy(changed)
                engine.resolve_batch("proposal", extracted=self.extracted)
                jobs = self.rows("SELECT * FROM jobs WHERE kind='propose'")
                self.assertEqual([job["job_id"] for job in jobs], [frozen["job_id"]])
                self.assertEqual(json.loads(jobs[0]["versions_json"]), frozen["versions"])
                self.assertEqual("Item naming contract" in text.prompts[0], version == "semantic-proposal-v2")

    def test_frozen_v1_and_v2_accepted_jobs_replay_without_new_version_calls(self):
        for initial, changed in ((None, "source-composition-v1"),
                                 ("source-composition-v1", "content-responsibility-v1")):
            with self.subTest(initial=initial):
                self.new_fixture(initial)
                engine, text, decision = self.engine("valid")
                with patch.object(self.store, "apply_proposal", side_effect=RuntimeError("synthetic before commit")):
                    with self.assertRaisesRegex(RuntimeError, "before commit"):
                        engine.resolve_batch("proposal", extracted=self.extracted)
                frozen = self.rows("SELECT * FROM jobs WHERE kind='propose'")
                artifact = self.rows("SELECT * FROM accepted_job_artifacts WHERE job_id=?", (frozen[0]["job_id"],))
                self.fixture_reading_policy(changed)
                sent = list(text.calls), list(decision.calls)
                with patch("core.semantic.engine.validate_proposal", side_effect=AssertionError("revalidated accepted proposal")):
                    engine.resolve_batch("proposal", extracted=self.extracted)
                self.assertEqual((text.calls, decision.calls), sent)
                self.assertEqual([job["job_id"] for job in self.rows("SELECT * FROM jobs WHERE kind='propose'")],
                                 [frozen[0]["job_id"]])
                self.assertEqual(self.rows("SELECT * FROM accepted_job_artifacts WHERE job_id=?", (frozen[0]["job_id"],)), artifact)

    def test_frozen_v1_and_v2_unknown_jobs_block_new_version_dispatch(self):
        for initial, changed in ((None, "source-composition-v1"),
                                 ("source-composition-v1", "content-responsibility-v1")):
            with self.subTest(initial=initial):
                self.new_fixture(initial)
                engine, text, _ = self.engine("valid")
                frozen = self.freeze(engine)
                engine.jobs.claim(frozen["job_id"], "historical-worker")
                engine.jobs.fail(frozen["job_id"], "historical-worker", "synthetic_unknown", unknown=True)
                before = self.rows("SELECT * FROM jobs WHERE kind='propose'")
                self.fixture_reading_policy(changed)
                with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
                    engine.resolve_batch("proposal", extracted=self.extracted)
                self.assertEqual(text.calls, [])
                self.assertEqual(self.rows("SELECT * FROM jobs WHERE kind='propose'"), before)

    def test_v2_strict_shape_rejection_exhausts_without_filling_fields(self):
        self.new_fixture("source-composition-v1")
        engine, text, _ = self.engine("missing")
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_shape"):
            engine.resolve_batch("proposal", extracted=self.extracted)
        job = self.rows("SELECT * FROM jobs WHERE kind='propose'")[0]
        self.assertEqual(json.loads(job["versions_json"])["prompt"], "semantic-proposal-v2")
        self.assertEqual((job["state"], job["attempt"]), ("failed", 1))
        self.assertEqual(len(text.calls), 3)
        self.assertTrue(all("Item naming contract" in prompt for prompt in text.prompts))
        self.assertFalse(self.rows("SELECT * FROM accepted_job_artifacts WHERE job_id=?", (job["job_id"],)))
        self.assertFalse(self.rows("SELECT * FROM action_receipts"))

    def test_v2_unknown_correction_stays_on_original_job_after_metadata_drift(self):
        self.new_fixture("source-composition-v1")
        engine, text, decision = self.engine("missing", ProviderError("synthetic_correction_unknown", outcome_unknown=True))
        with self.assertRaisesRegex(ProviderError, "synthetic_correction_unknown"):
            engine.resolve_batch("proposal", extracted=self.extracted)
        job = self.rows("SELECT * FROM jobs WHERE kind='propose'")[0]
        self.assertEqual(json.loads(job["versions_json"])["prompt"], "semantic-proposal-v2")
        self.assertEqual((job["state"], job["attempt"]), ("unknown", 1))
        self.assertEqual(len(text.calls), 2)
        self.fixture_reading_policy("content-responsibility-v1")
        sent = list(text.calls), list(decision.calls)
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            SemanticEngine(self.store, text, decision).resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual((text.calls, decision.calls), sent)
        self.assertEqual(self.rows("SELECT * FROM jobs WHERE kind='propose'"), [job])

    def test_conflicting_frozen_proposals_fail_closed_with_unknown_priority(self):
        engine, text, _ = self.engine("valid")
        old = self.freeze(engine)
        alternative = engine.jobs.ensure("propose", old["inputs"], dict(old["versions"], prompt="semantic-proposal-v2"))
        with self.assertRaisesRegex(SemanticValidationError, "semantic_proposal_version_conflict"):
            engine.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual(text.calls, [])
        engine.jobs.claim(alternative["job_id"], "historical-worker")
        engine.jobs.fail(alternative["job_id"], "historical-worker", "synthetic_unknown", unknown=True)
        before = self.rows("SELECT * FROM jobs WHERE kind='propose'")
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            engine.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual(text.calls, [])
        self.assertEqual(self.rows("SELECT * FROM jobs WHERE kind='propose'"), before)

    def test_frozen_proposal_lookup_has_a_fixed_sqlite_work_budget(self):
        self.new_fixture("source-composition-v1")
        engine, _, _ = self.engine("valid")
        frozen = self.freeze(engine)
        for index in range(320):
            engine.jobs.ensure("propose", {"batch_id": "unrelated-" + str(index),
                "state": {"unrelated_material": "x" * 2048}}, frozen["versions"])
        connect = self.store.connect
        steps = [0]
        def budget():
            steps[0] += 1
            return steps[0] > 1000
        def bounded_connection():
            connection = connect()
            connection.set_progress_handler(budget, 1)
            return connection
        with patch.object(self.store, "connect", side_effect=bounded_connection):
            reused = engine._proposal_job("proposal", frozen["inputs"]["state"])
            fresh = engine._proposal_job("new-synthetic-batch", frozen["inputs"]["state"])
        self.assertEqual(reused["job_id"], frozen["job_id"])
        self.assertEqual(fresh["versions"]["prompt"], "semantic-proposal-v2")
        self.assertLessEqual(steps[0], 1000)

    def changed_route_engine(self):
        engine, text, decision = self.engine("valid")
        text.route = ProviderRoute("synthetic", "changed-text", "text")
        decision.route = ProviderRoute("synthetic", "changed-decision", "decision")
        return engine, text, decision

    def test_changed_routes_cannot_bypass_unknown_proposal(self):
        engine, _, _ = self.engine("missing", ProviderError("synthetic_proposal_unknown", outcome_unknown=True))
        with self.assertRaisesRegex(ProviderError, "synthetic_proposal_unknown"):
            engine.resolve_batch("proposal", extracted=self.extracted)
        before = self.rows("SELECT * FROM jobs WHERE kind='propose'")
        changed, text, decision = self.changed_route_engine()
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            changed.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual((text.calls, decision.calls), ([], []))
        self.assertEqual(self.rows("SELECT * FROM jobs WHERE kind='propose'"), before)

    def test_changed_routes_reuse_accepted_proposal_and_choice_without_calls(self):
        engine, _, _ = self.engine("valid")
        with patch.object(self.store, "apply_proposal", side_effect=RuntimeError("synthetic before commit")):
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                engine.resolve_batch("proposal", extracted=self.extracted)
        artifacts = self.rows("SELECT a.* FROM accepted_job_artifacts a JOIN jobs j USING(job_id) WHERE j.kind IN ('propose','decision')")
        changed, text, decision = self.changed_route_engine()
        result = changed.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual(result["status"], "complete")
        self.assertEqual((text.calls, decision.calls), ([], []))
        self.assertEqual(self.rows("SELECT a.* FROM accepted_job_artifacts a JOIN jobs j USING(job_id) WHERE j.kind IN ('propose','decision')"), artifacts)

    def test_changed_routes_cannot_bypass_unknown_choice(self):
        engine, _, decision = self.engine("valid")
        with patch.object(decision, "decide", side_effect=ProviderError("synthetic_choice_unknown", outcome_unknown=True)):
            with self.assertRaisesRegex(ProviderError, "synthetic_choice_unknown"):
                engine.resolve_batch("proposal", extracted=self.extracted)
        before = self.rows("SELECT * FROM jobs WHERE kind IN ('propose','decision')")
        changed, text, decision = self.changed_route_engine()
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            changed.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual((text.calls, decision.calls), ([], []))
        self.assertEqual(self.rows("SELECT * FROM jobs WHERE kind IN ('propose','decision')"), before)

    def test_changed_route_does_not_dispatch_an_unaccepted_frozen_proposal(self):
        engine, _, _ = self.engine("valid")
        frozen = self.freeze(engine)
        changed, text, decision = self.changed_route_engine()
        with self.assertRaisesRegex(ProviderError, "text_provider_route_changed"):
            changed.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual((text.calls, decision.calls), ([], []))
        jobs = self.rows("SELECT * FROM jobs WHERE kind='propose'")
        self.assertEqual([job["job_id"] for job in jobs], [frozen["job_id"]])
        self.assertEqual(json.loads(jobs[0]["versions_json"]), frozen["versions"])
        self.assertFalse(self.rows("SELECT * FROM action_receipts"))

    def test_changed_route_does_not_dispatch_an_unaccepted_frozen_choice(self):
        engine, _, _ = self.engine("valid")
        accepted = engine._accepted
        def before_choice(job, callback):
            if job["kind"] == "decision":
                raise RuntimeError("synthetic before choice dispatch")
            return accepted(job, callback)
        with patch.object(engine, "_accepted", side_effect=before_choice):
            with self.assertRaisesRegex(RuntimeError, "before choice dispatch"):
                engine.resolve_batch("proposal", extracted=self.extracted)
        frozen = self.rows("SELECT * FROM jobs WHERE kind='decision'")[0]
        changed, text, decision = self.changed_route_engine()
        with self.assertRaisesRegex(ProviderError, "decision_provider_route_changed"):
            changed.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual((text.calls, decision.calls), ([], []))
        jobs = self.rows("SELECT * FROM jobs WHERE kind='decision'")
        self.assertEqual([job["job_id"] for job in jobs], [frozen["job_id"]])
        self.assertEqual(jobs[0]["versions_json"], frozen["versions_json"])
        self.assertFalse(self.rows("SELECT * FROM action_receipts"))

    def test_returned_choice_must_match_the_proposal_frozen_route(self):
        engine, _, decision = self.engine("valid")
        decide = decision.decide
        def wrong_route(request, **kwargs):
            result = decide(request, **kwargs)
            return DecisionResult(result.answers, ProviderRoute("synthetic", "unexpected", "decision"))
        with patch.object(decision, "decide", side_effect=wrong_route):
            with self.assertRaisesRegex(ProviderError, "decision_provider_route_changed"):
                engine.resolve_batch("proposal", extracted=self.extracted)
        job = self.rows("SELECT * FROM jobs WHERE kind='decision'")[0]
        self.assertEqual((job["state"], job["attempt"]), ("failed", 1))
        self.assertEqual(len(decision.calls), 1)
        self.assertFalse(self.rows("SELECT * FROM accepted_job_artifacts WHERE job_id=?", (job["job_id"],)))
        self.assertFalse(self.rows("SELECT * FROM action_receipts"))

    def test_incomplete_fixture_versions_cannot_invent_a_decision_route(self):
        engine, text, decision = self.engine("valid")
        ensure = engine.jobs.ensure
        def incomplete_fixture(kind, inputs, versions):
            if kind == "propose":
                versions = {key: value for key, value in versions.items() if key != "decision_route"}
            return ensure(kind, inputs, versions)
        with patch.object(engine.jobs, "ensure", side_effect=incomplete_fixture):
            with self.assertRaisesRegex(SemanticValidationError, "semantic_proposal_route_binding_missing"):
                engine.resolve_batch("proposal", extracted=self.extracted)
        before = self.rows("SELECT * FROM jobs WHERE kind='propose'")
        with self.assertRaisesRegex(SemanticValidationError, "semantic_proposal_route_binding_missing"):
            engine.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual((text.calls, decision.calls), ([], []))
        self.assertEqual(self.rows("SELECT * FROM jobs WHERE kind='propose'"), before)
        self.assertFalse(self.rows("SELECT * FROM jobs WHERE kind='decision'"))

    def test_v2_never_trims_or_rewrites_the_model_title(self):
        self.new_fixture("source-composition-v1")
        title = "称呼、动作和完整细节仍由模型选择，程序不会按字数截断这个不够简短的标题" * 3
        engine, _, _ = self.engine("valid", title=title)
        engine.resolve_batch("proposal", extracted=self.extracted)
        self.assertEqual(self.store.list_items()[0]["title"], title)


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SemanticStore(Path(self.temp.name).resolve() / "knowledge.sqlite", "synthetic-generation")

    def test_real_extraction_input_retains_names_without_account_ids_or_subject_guessing(self):
        payloads = [{"raw_sender_id": "wxid_synthetic_self", "sender": "小林"},
                    {"raw_sender_id": "wxid_synthetic_forwarder", "sender": "阿青"}]
        captured = capture(self.store, "attributed", ["我今天完成测试。", "小林说：朋友完成了测试。"], payloads=payloads)
        seen = []

        class AttributedText(FakeText):
            def generate(provider, prompt, **kwargs):
                provider.calls.append(kwargs["request_id"])
                seen.append(prompt)
                data = json.loads(prompt.rsplit("\n", 1)[-1])
                result = []
                for index, source in enumerate(data):
                    claim = {"subject_id": source["speaker_id"] if index == 0 else None,
                             "case_id": None, "condition": None, "channel": None, "version": None,
                             "phase": None, "observed_at": source["occurred_at"],
                             "predicates": {"completed": True}, "span_indices": [0]}
                    result.append(model_observation([source], title=source["speaker_name"] + "的讲述",
                        units=[{"kind": "quote", "span_indices": [0]}], claims=[claim]))
                return TextResult(json.dumps({"observations": result}), provider.route)

        provider = AttributedText()
        result = SemanticEngine(self.store, provider, FakeDecision()).extract_batch("attributed")
        inputs = json.loads(seen[0].rsplit("\n", 1)[-1])
        self.assertEqual([s["speaker_name"] for s in inputs], ["小林", "阿青"])
        self.assertTrue(all(s["speaker_id"] for s in inputs))
        self.assertTrue(all(s["subject_id"] is None for s in inputs))
        self.assertNotIn("wxid_synthetic", seen[0])
        self.assertEqual([o["claims"][0]["subject_id"] for o in result["observations"]], [inputs[0]["speaker_id"], None])
        with closing(self.store.connect()) as db:
            job = db.execute("SELECT inputs_json,versions_json FROM jobs WHERE job_id=?", (result["job_id"],)).fetchone()
        self.assertEqual(json.loads(job["versions_json"])["prompt"], EXTRACTION_PROMPT_VERSION)
        self.assertEqual(json.loads(job["inputs_json"])["members"][0]["speaker_name"], "小林")
        self.assertEqual(self.store.get_source(captured["member_version_ids"][0])["payload"], payloads[0])

    def legacy_extraction_job(self, *, accept=False):
        payload = {"raw_sender_id": "wxid_synthetic_legacy", "sender": "小林"}
        record = capture(self.store, "old", ["我完成了测试。"], payloads=[payload])
        source = CapturedSource(record["member_version_ids"][0], "我完成了测试。", occurred_at=10,
            **captured_source_metadata("synthetic", "group-a", payload, legacy=True))
        member = asdict(source)
        member.pop("speaker_id")
        member.pop("speaker_name")
        engine = SemanticEngine(self.store, FakeText(), FakeDecision())
        job = engine.jobs.ensure("extract", {"batch_id": "old", "members": [member]}, {"prompt": "semantic-extraction-v1"})
        artifact = {"observations": [o.as_dict() for o in validate_extraction({"observations": [observation(source, namespace=self.store.generation_id)]}, [source], namespace=self.store.generation_id)], "dispositions": {}}
        artifact = json.loads(json.dumps(artifact))
        if accept:
            engine.jobs.claim(job["job_id"], "old-worker")
            engine.jobs.accept(job["job_id"], "old-worker", artifact, validate=lambda value: value if value == artifact else None)
        return engine, job, artifact

    def test_accepted_v1_candidate_replays_exact_inputs_without_v2_provider_call(self):
        engine, job, artifact = self.legacy_extraction_job(accept=True)
        result = engine.extract_batch("old")
        self.assertEqual(result["job_id"], job["job_id"])
        self.assertEqual(result["observations"], artifact["observations"])
        self.assertEqual(engine.extract_batch("old")["receipt"], result["receipt"])
        self.assertEqual(engine.text_provider.calls, [])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='extract'").fetchone()[0], 1)
            self.assertEqual(json.loads(db.execute("SELECT inputs_json FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0]), job["inputs"])

    def test_unknown_v1_parent_or_page_cannot_be_bypassed_by_prompt_upgrade(self):
        engine, job, _ = self.legacy_extraction_job()
        for child in (True, False):
            with self.subTest(child=child):
                target = engine.jobs.ensure("extract_page", {"parent_job_id": job["job_id"], "page": 0}, {"prompt": "semantic-extraction-pages-v1"}) if child else job
                engine.jobs.claim(target["job_id"], "old-worker")
                engine.jobs.fail(target["job_id"], "old-worker", "synthetic_sent_unknown", unknown=True)
                with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
                    engine.extract_batch("old")
        self.assertEqual(engine.text_provider.calls, [])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='extract'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE state='unknown'").fetchone()[0], 2)

    def test_unaccepted_v1_candidate_is_preserved_as_explicit_upgrade_pending(self):
        engine, job, _ = self.legacy_extraction_job()
        with self.assertRaisesRegex(SemanticValidationError, "semantic_extraction_upgrade_pending"):
            engine.extract_batch("old")
        self.assertEqual(engine.text_provider.calls, [])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0], "pending")

    def test_provider_artifact_survives_commit_crash_and_replay_has_no_freshness(self):
        capture(self.store, "batch", ["沿河散步，买了一束花。", "独立的版本变更场景。"])
        text, decisions = FakeText(), FakeDecision()
        engine = SemanticEngine(self.store, text, decisions)
        real_commit = self.store.commit_extraction
        def crash(*args, **kwargs):
            raise RuntimeError("synthetic commit crash")
        self.store.commit_extraction = crash
        with self.assertRaisesRegex(RuntimeError, "synthetic commit crash"):
            engine.process_batch("batch")
        self.assertEqual(len(text.calls), 1)
        self.store.commit_extraction = real_commit
        result = engine.process_batch("batch")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(text.calls), 2)
        self.assertEqual(len(self.store.list_items()), 2)
        before = [(item["content_revision"], item["semantic_updated_at"]) for item in self.store.list_items()]
        again = SemanticEngine(self.store, text, decisions).process_batch("batch")
        self.assertEqual(again["processing_receipt"], result["processing_receipt"])
        self.assertEqual(len(text.calls), 2)
        self.assertEqual(before, [(item["content_revision"], item["semantic_updated_at"]) for item in self.store.list_items()])

    def test_live_stale_read_set_replan_has_durable_supersession_and_no_provider_replay(self):
        from ai.vector_index import LocalCosineIndex
        from core.semantic.runtime import ProviderRoles, SemanticRuntime
        from tests.test_semantic_retrieval import FakeEmbedding
        capture(self.store, "batch", ["同一生活场景需要在更新后的视图上下文中解析。"])
        text, decision = FakeText(), FakeDecision()
        runtime = SemanticRuntime(self.store, ProviderRoles(text, decision, FakeEmbedding(), LocalCosineIndex()),
                                  Path(self.temp.name).resolve() / "WGO")
        original = self.store.apply_proposal
        stale = []
        def concurrent_change(job_id, *args, **kwargs):
            if not stale:
                stale.append(job_id)
                self.store.create_view("new-context", "生活", "synthetic concurrent edit")
            return original(job_id, *args, **kwargs)
        with patch.object(self.store, "apply_proposal", side_effect=concurrent_change):
            processing = runtime.process("batch")
        self.store.verify_processing_receipt(processing)
        replacement = self.store.proposal_supersession(stale[0])
        self.assertIsNotNone(replacement)
        before = [(item["content_revision"], item["semantic_updated_at"]) for item in self.store.list_items()]
        calls = len(text.calls), len(decision.calls)
        replay = runtime.engine.process_batch("batch", plan_revision=1)
        self.assertEqual(replay["superseded_proposal_job_id"], stale[0])
        self.assertEqual(replay["proposal_receipt"], replacement)
        self.assertEqual(calls, (len(text.calls), len(decision.calls)))
        self.assertEqual(before, [(item["content_revision"], item["semantic_updated_at"]) for item in self.store.list_items()])
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs WHERE kind IN ('propose','decision') AND state!='done'").fetchone()[0], 0)

    def test_replacement_resolution_and_supersession_commit_atomically(self):
        capture(self.store, "batch", ["保持原始来源身份。"])
        text, decision = FakeText(), FakeDecision()
        engine = SemanticEngine(self.store, text, decision)
        with patch.object(self.store, "apply_proposal", side_effect=RuntimeError("before commit")):
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                engine.process_batch("batch")
        with closing(self.store.connect()) as conn:
            old = conn.execute("SELECT job_id FROM jobs WHERE kind='propose'").fetchone()[0]
        self.store.create_view("new-context", "上下文", "synthetic refresh")
        record = self.store._record_proposal_supersessions
        def interrupted(conn, job_id, now):
            record(conn, job_id, now)
            raise RuntimeError("after proof before commit")
        with patch.object(self.store, "_record_proposal_supersessions", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "after proof before commit"):
                engine.process_batch("batch", plan_revision=2)
        self.assertEqual(self.store.list_items(), [])
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM proposal_supersessions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT state FROM jobs WHERE job_id=?", (old,)).fetchone()[0], "accepted")
            self.assertEqual(conn.execute("SELECT count(*) FROM action_receipts").fetchone()[0], 0)
        calls = len(text.calls), len(decision.calls)
        self.assertEqual(engine.process_batch("batch", plan_revision=2)["status"], "complete")
        self.assertIsNotNone(self.store.proposal_supersession(old))
        self.assertEqual(calls, (len(text.calls), len(decision.calls)))

    def test_partial_replacement_cannot_supersede_a_plan_for_all_observations(self):
        capture(self.store, "batch", ["第一个生活片段。", "第二个独立片段。"])
        engine = SemanticEngine(self.store, FakeText(), FakeDecision())
        with patch.object(self.store, "apply_proposal", side_effect=RuntimeError("before commit")):
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                engine.process_batch("batch")
        with closing(self.store.connect()) as conn:
            old = conn.execute("SELECT job_id FROM jobs WHERE kind='propose'").fetchone()[0]
        self.store.create_view("new-context", "上下文", "synthetic refresh")
        class PartialText(FakeText):
            def generate(self, prompt, **kwargs):
                result = super().generate(prompt, **kwargs)
                value = json.loads(result.text)
                if "actions" in value:
                    value["actions"] = value["actions"][:1]
                return TextResult(json.dumps(value), result.route)
        result = SemanticEngine(self.store, PartialText(), FakeDecision()).process_batch("batch", plan_revision=2)
        self.assertEqual(result["status"], "unclassified")
        self.assertIsNone(self.store.proposal_supersession(old))
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT state FROM jobs WHERE job_id=?", (old,)).fetchone()[0], "accepted")
            self.assertEqual(conn.execute("SELECT count(*) FROM processing_receipts").fetchone()[0], 0)

    def test_committed_replacement_recovery_finishes_its_actual_jobs(self):
        capture(self.store, "batch", ["提交以后进程中断仍保留确定结果。"])
        text, decision = FakeText(), FakeDecision()
        engine = SemanticEngine(self.store, text, decision)
        with patch.object(self.store, "apply_proposal", side_effect=RuntimeError("before commit")):
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                engine.process_batch("batch")
        self.store.create_view("new-context", "上下文", "synthetic refresh")
        with patch.object(engine.jobs, "finish_applied_proposal", side_effect=RuntimeError("after commit before finish")):
            with self.assertRaisesRegex(RuntimeError, "after commit before finish"):
                engine.process_batch("batch", plan_revision=2)
        calls = len(text.calls), len(decision.calls)
        self.assertEqual(engine.process_batch("batch", plan_revision=1)["status"], "complete")
        self.assertEqual(calls, (len(text.calls), len(decision.calls)))
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs WHERE kind IN ('propose','decision') AND state!='done'").fetchone()[0], 0)

    def test_invalid_refs_leave_canonical_source_unread(self):
        capture(self.store, "batch", ["保留原文"])
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_evidence_reference"):
            SemanticEngine(self.store, FakeText(malformed=True), FakeDecision()).process_batch("batch")
        self.assertEqual(self.store.coverage()["unread_sources"], 1)
        self.assertEqual(self.store.list_items(), [])

    def test_explicit_noise_can_finish_but_decision_defer_cannot(self):
        capture(self.store, "noise", ["嗯"])
        self.assertEqual(SemanticEngine(self.store, FakeText(noise=True), FakeDecision()).process_batch("noise")["status"], "complete")
        capture(self.store, "other", ["不可丢掉的生活故事"])
        result = SemanticEngine(self.store, FakeText(), FakeDecision(defer=True)).process_batch("other")
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(self.store.list_items(), [])
        self.assertEqual(self.store.coverage()["unclassified_sources"], 1)

    def test_accepted_defer_replans_after_later_context_without_replaying_unchanged_poll(self):
        capture(self.store, "first", ["等待明确的后续案例"])
        text, decision = FakeText(), FakeDecision(defer=True)
        engine = SemanticEngine(self.store, text, decision)
        first = engine.process_batch("first")
        self.assertEqual(first["status"], "deferred")
        old = engine.jobs.accepted(first["pages"][0]["decision_job_id"])
        counts = len(text.calls), len(decision.calls)
        engine = SemanticEngine(self.store, text, decision)  # Restart has no in-memory epoch.
        self.assertEqual(engine.process_batch("first")["status"], "deferred")
        self.assertEqual((len(text.calls), len(decision.calls)), counts)
        capture(self.store, "later", ["明确的后续案例"])
        decision.defer = False
        later = engine.process_batch("later")
        self.assertEqual(later["status"], "unclassified")  # ACK still cannot pass the gap.
        self.assertEqual(later["pending_reason"], "semantic_prior_batch_incomplete")
        self.assertEqual(len(self.store.list_items()), 1)  # Later knowledge can commit.
        result = engine.process_batch("first")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(engine.jobs.accepted(first["pages"][0]["decision_job_id"]), old)
        counts = len(text.calls), len(decision.calls)
        self.assertEqual(engine.process_batch("later")["status"], "complete")
        self.assertEqual(engine.process_batch("first")["status"], "complete")
        self.assertEqual((len(text.calls), len(decision.calls)), counts)
        self.store.verify_processing_receipt(result["processing_receipt"])

    def test_unknown_decision_stays_unknown_when_context_changes(self):
        class UnknownDecision(FakeDecision):
            def decide(self, request, **kwargs):
                if any(q.type == "choice" for q in request.questions):
                    self.calls.append(kwargs["request_id"])
                    raise ProviderError("synthetic_unknown_choice", outcome_unknown=True)
                return super().decide(request, **kwargs)
        capture(self.store, "unknown", ["必须保留不明请求"])
        text, decision = FakeText(), UnknownDecision()
        engine = SemanticEngine(self.store, text, decision)
        with self.assertRaisesRegex(ProviderError, "synthetic_unknown_choice"):
            engine.process_batch("unknown")
        self.store.create_view("new-context", "上下文", "new canonical dependency")
        counts = len(text.calls), len(decision.calls)
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            SemanticEngine(self.store, text, decision).process_batch("unknown")
        self.assertEqual((len(text.calls), len(decision.calls)), counts)

    def test_same_source_overlap_is_context_and_never_second_observation(self):
        capture(self.store, "batch", ["同一束花"])
        text, decision = FakeText(), FakeDecision()
        engine = SemanticEngine(self.store, text, decision)
        engine.process_batch("batch")
        original = self.store.get_batch("batch")["members"][0]
        self.store.capture_batch("overlap", [{key: original[key] for key in ("source_id", "logical_shard_id", "physical_generation", "source_cursor_token", "source_sent_at", "text", "payload")}],
                                 source_namespace="synthetic", chat_id="group-a", inventory_digest="synthetic-inventory", inventory_revision=1,
                                 input_cursors={}, output_cursors={}, source_eof=True)
        count = len(text.calls)
        result = engine.process_batch("overlap")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(text.calls), count)
        self.assertEqual(len(self.store.documents()), 3)

    def test_large_catalog_uses_persisted_scope_pages_before_novel_item(self):
        capture(self.store, "catalog", ["独立案例 " + str(index) for index in range(60)])
        text, decision = FakeText(), FakeDecision(possible=0)
        engine = SemanticEngine(self.store, text, decision)
        engine.process_batch("catalog")
        capture(self.store, "new", ["一段不同的生活故事"])
        result = engine.process_batch("new")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(self.store.list_items()), 61)
        with closing(self.store.connect()) as db:
            pages = db.execute("SELECT count(*) FROM jobs WHERE kind='catalog_candidate_scan' AND state='done'").fetchone()[0]
        self.assertGreaterEqual(pages, 3)
        self.assertLess(max(text.prompt_sizes), 100000)
        counts = len(text.calls), len(decision.calls)
        engine.process_batch("new")
        self.assertEqual((len(text.calls), len(decision.calls)), counts)

    def test_more_than_twelve_possible_candidates_receive_complete_detail_refinement(self):
        from core.semantic.retrieval import HybridRetriever
        capture(self.store, "catalog", ["independent " + str(i) for i in range(18)],
                payloads=[{"case_anchor": "shared-case"} for _ in range(18)])
        text = FakeText(claims=True)
        SemanticEngine(self.store, text, FakeDecision(possible=0)).process_batch("catalog")
        target = next(item["item_id"] for item in self.store.list_items() if item["title"] == "independent 17")
        class RefiningDecision(FakeDecision):
            def decide(self, request, **kwargs):
                if "existing_items" not in request.state:
                    return super().decide(request, **kwargs)
                self.calls.append(kwargs["request_id"])
                return DecisionResult({q.id: DecisionAnswer("noul", noul=.9 if item["item_id"] == target else 0)
                    for q, item in zip(request.questions, request.state["existing_items"])}, self.route)
        capture(self.store, "new", ["exact continuation of independent 17"], payloads=[{"case_anchor": "shared-case"}])
        engine = SemanticEngine(self.store, text, RefiningDecision(), retriever=HybridRetriever(self.store))
        self.assertEqual(engine.process_batch("new")["status"], "complete")
        self.assertEqual(len(self.store.list_items()), 18)
        self.assertEqual(len(self.store.get_item(target)["observation_ids"]), 2)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='candidate_refinement' AND state='done'").fetchone()[0], 3)
        capture(self.store, "ambiguous", ["unresolved exact identity"])
        uncertain = FakeDecision()
        engine = SemanticEngine(self.store, text, uncertain)
        result = engine.process_batch("ambiguous")
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["pages"][0]["pending_reason"], "semantic_candidate_ambiguity")
        calls = len(text.calls), len(uncertain.calls)
        self.assertEqual(engine.process_batch("ambiguous")["status"], "deferred")
        self.assertEqual((len(text.calls), len(uncertain.calls)), calls)

    def test_catalog_scale_reports_linear_calls_without_omitting_identity_coverage(self):
        capture(self.store, "source", ["synthetic scale input"])
        decision = FakeDecision(possible=0)
        engine = SemanticEngine(self.store, FakeText(), decision)
        observations = engine.extract_batch("source")["observations"]
        for count in (24, 240, 4000):
            with self.subTest(count=count):
                items = {"item-" + str(i): {"item_id": "item-" + str(i), "kind": "story",
                    "title": "case " + str(i), "identity_scope": {"case": str(i)}, "content_revision": 1}
                    for i in range(count)}
                context = {"items": items, "read_set": {"store_revision": count, "items": {}, "views": {}}}
                calls = len(decision.calls)
                _incoming, selected, _reads, catalog = engine._resolve_candidates(context, observations)
                self.assertEqual(len(decision.calls) - calls, (count + 23) // 24)
                self.assertTrue(catalog["complete"])
                self.assertEqual(catalog["item_count"], count)
                self.assertEqual(len(catalog["accepted_scan_job_ids"]), (count + 23) // 24)
                self.assertEqual(selected, {})
                self.assertEqual(catalog["omitted_possible_ids"], [])

    def test_unknown_provider_outcome_is_not_silently_retried(self):
        class UncertainText(FakeText):
            def generate(self, prompt, *, request_id, generation_id=""):
                self.calls.append(request_id)
                raise ProviderError("synthetic_sent_unknown", retryable=True, outcome_unknown=True)
        capture(self.store, "batch", ["原文仍然保留"])
        text = UncertainText()
        engine = SemanticEngine(self.store, text, FakeDecision())
        with self.assertRaisesRegex(ProviderError, "synthetic_sent_unknown"):
            engine.process_batch("batch")
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            engine.process_batch("batch")
        self.assertEqual(len(text.calls), 1)
        self.assertEqual(self.store.coverage()["unread_sources"], 1)

    def test_large_candidate_identity_digest_does_not_replace_canonical_body(self):
        original = "详细条件和反证必须保留。" * 500
        capture(self.store, "long", [original])
        text, decision = FakeText(), FakeDecision()
        engine = SemanticEngine(self.store, text, decision)
        engine.process_batch("long")
        capture(self.store, "later", ["补充同一案例的下一步"])
        self.assertEqual(engine.process_batch("later")["status"], "complete")
        with closing(self.store.connect()) as db:
            self.assertGreater(db.execute("SELECT count(*) FROM jobs WHERE kind='candidate_digest'").fetchone()[0], 0)
        self.assertTrue(any(unit["text"] == original for item in self.store.list_items() for unit in item["units"]))

    def test_global_extract_then_resolve_is_callable(self):
        capture(self.store, "first", ["第一段故事"])
        capture(self.store, "second", ["第二段故事"])
        engine = SemanticEngine(self.store, FakeText(), FakeDecision(possible=0))
        first, second = engine.extract_batch("first"), engine.extract_batch("second")
        self.assertEqual(self.store.list_items(), [])
        self.assertEqual(engine.resolve_batch("first", extracted=first)["status"], "complete")
        self.assertEqual(engine.resolve_batch("second", extracted=second)["status"], "complete")
        self.assertEqual(len(self.store.list_items()), 2)

    def test_views_induce_across_pages_and_replay_without_provider_calls(self):
        capture(self.store, "scenes", ["独立场景 " + str(index) for index in range(20)])
        text, decision = FakeText(), FakeDecision()
        engine = SemanticEngine(self.store, text, decision)
        engine.process_batch("scenes")
        result = engine.reconcile_views("views-c0")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["assigned_item_ids"]), 20)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM views").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM view_memberships").fetchone()[0], 20)
        counts = len(text.calls), len(decision.calls)
        self.assertEqual(engine.reconcile_views("views-c0"), result)
        self.assertEqual((len(text.calls), len(decision.calls)), counts)

    def test_consolidation_no_forced_merge_then_explicit_merge_retains_sources(self):
        capture(self.store, "scenes", ["同名人物的场景一", "同名人物的独立场景二"])
        text, decision = FakeText(), FakeDecision()
        engine = SemanticEngine(self.store, text, decision)
        engine.process_batch("scenes")
        before = {item["item_id"] for item in self.store.list_items()}
        self.assertEqual(engine.consolidate("c0")["status"], "complete")
        self.assertEqual({item["item_id"] for item in self.store.list_items()}, before)
        text.consolidation = "merge"
        result = engine.consolidate("c1")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(self.store.list_items()), 1)
        self.assertEqual(len(self.store.list_items()[0]["observation_ids"]), 2)
        calls = len(text.calls), len(decision.calls)
        self.assertEqual(engine.consolidate("c1"), result)
        self.assertEqual((len(text.calls), len(decision.calls)), calls)

    def test_consolidation_split_conserves_observations(self):
        from core.semantic.watches import WatchService
        capture(self.store, "first", ["人物同一个，场景一"])
        text, decision = FakeText(consolidation="split"), FakeDecision()
        engine = SemanticEngine(self.store, text, decision)
        engine.process_batch("first")
        capture(self.store, "second", ["人物同一个，场景二"])
        engine.process_batch("second")
        original = self.store.list_items()[0]
        episodes = WatchService(self.store)
        old_episode = episodes.episodes(item_id=original["item_id"])[0]
        episodes.close_episode(old_episode["episode_id"], expected_revision=old_episode["revision"])
        old_history = episodes.episodes(item_id=original["item_id"])
        self.assertEqual(len(original["observation_ids"]), 2)
        self.assertEqual(engine.consolidate("split")["status"], "complete")
        self.assertEqual(len(self.store.list_items()), 2)
        self.assertEqual({oid for item in self.store.list_items() for oid in item["observation_ids"]}, set(original["observation_ids"]))
        self.assertEqual(episodes.episodes(item_id=original["item_id"]), old_history)
        self.assertEqual(len(episodes.episodes()), 3)
        self.assertEqual({value["item_id"] for value in episodes.episodes(include_resolved=False)},
                         {item["item_id"] for item in self.store.list_items()})

    def test_empty_legacy_metadata_has_receipts_without_provider_calls(self):
        self.store.capture_batch("metadata", [{"source_id": "metadata-row", "logical_shard_id": "legacy",
            "physical_generation": "legacy", "source_cursor_token": "1", "source_sent_at": 0,
            "text": "", "kind": "legacy_metadata", "payload": {"legacy_row": {"count": 7}}}],
            source_namespace="legacy", chat_id="synthetic", inventory_digest="legacy", inventory_revision=1,
            input_cursors={}, output_cursors={}, source_eof=True)
        text = FakeText()
        result = SemanticEngine(self.store, text, FakeDecision()).process_batch("metadata")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(text.calls, [])
        self.assertTrue(result["processing_receipt"])

    def test_long_single_source_is_paged_without_losing_or_replaying_spans(self):
        original = "跨段细节必须完整保留。" * 2500 + "https://example.test/route?token=synthetic-secret" + "跨段细节必须完整保留。" * 2500
        visible = redact_urls_in_text(original)
        capture(self.store, "long", [original], payloads=[{"subject_anchor": "synthetic-subject", "raw_sender_id": "synthetic-sender"}])
        text, decision = FakeText(claims=True), FakeDecision()
        engine = SemanticEngine(self.store, text, decision)
        result = engine.process_batch("long")
        self.assertEqual(result["status"], "complete")
        self.assertLess(max(text.prompt_sizes), 100000)
        with closing(self.store.connect()) as db:
            spans = list(db.execute("SELECT start,end,excerpt FROM source_spans ORDER BY start"))
            self.assertGreater(db.execute("SELECT count(*) FROM jobs WHERE kind='extract_page'").fetchone()[0], 1)
        self.assertEqual("".join(row[2] for row in spans), visible)
        self.assertEqual([row[0] for row in spans], list(range(0, len(visible), 2000)))
        self.assertEqual(spans[-1][1], len(visible))
        self.assertTrue(all(claim["subject_id"] for claim in self.store.list_claims()))
        calls = len(text.calls), len(decision.calls)
        self.assertEqual(engine.process_batch("long")["processing_receipt"], result["processing_receipt"])
        self.assertEqual((len(text.calls), len(decision.calls)), calls)

    def test_page_context_keeps_earlier_subject_in_order_without_duplicate_core(self):
        messages = ["普通接话 " + str(i) for i in range(30)]
        messages[16] = "我做了红茶冻，又烤了饼干。"
        messages[24] = "两样都做糊了。"
        capture(self.store, "conversation", messages)
        seen = []
        class InspectText(FakeText):
            def generate(self, prompt, **kwargs):
                seen.append(json.loads(prompt.rsplit("\n", 1)[-1]))
                return super().generate(prompt, **kwargs)
        provider = InspectText()
        result = SemanticEngine(self.store, provider, FakeDecision()).extract_batch("conversation")
        self.assertEqual(len(seen), 2)
        second = seen[1]
        context = [s for s in second if not s["core"]]
        self.assertIn(messages[16], [span["excerpt"] for source in context for span in source["spans"]])
        self.assertEqual([s["occurred_at"] for s in second], sorted(s["occurred_at"] for s in second))
        self.assertLessEqual(len(context), 24)
        self.assertEqual(sum(s["core"] for page in seen for s in page), len(messages))
        self.assertEqual(len(result["observations"]), len(messages))
        self.assertLess(max(provider.prompt_sizes), 100000)

    def test_existing_page_keeps_its_frozen_two_message_context(self):
        from core.semantic.attribution import EXTRACTION_PROMPT_VERSION
        capture(self.store, "frozen-context", ["日常消息 " + str(i) for i in range(30)])
        batch = self.store.get_batch("frozen-context")
        sources = tuple(CapturedSource(m["source_version_id"], m["text"], occurred_at=m["source_sent_at"],
            **captured_source_metadata(batch["source_namespace"], batch["chat_id"], m["payload"])) for m in batch["members"])
        seen = []
        class InspectText(FakeText):
            def generate(self, prompt, **kwargs):
                seen.append(json.loads(prompt.rsplit("\n", 1)[-1]))
                return super().generate(prompt, **kwargs)
        provider = InspectText()
        engine = SemanticEngine(self.store, provider, FakeDecision())
        parent = engine.jobs.ensure("extract", {"batch_id": "frozen-context", "members": [asdict(s) for s in sources]},
            {"prompt": EXTRACTION_PROMPT_VERSION, "generation": self.store.generation_id, "route": asdict(provider.route)})
        spans = prepare_spans(sources, namespace=self.store.generation_id)
        old = engine.jobs.ensure("extract_page", {"parent_job_id": parent["job_id"], "page": 1,
            "span_ids": [s.span_id for s in spans[22:]], "source_ids": sorted(s.source_id for s in sources[22:]),
            "core_source_ids": [s.source_id for s in sources[24:]]}, {"prompt": "semantic-extraction-pages-v2-attribution"})
        with patch("core.semantic.engine.extraction_prompt", wraps=extraction_prompt) as prompts:
            engine.extract_batch("frozen-context")
        self.assertEqual([call.kwargs["unit_policy"] for call in prompts.call_args_list],
                         ["retained-knowledge-v5", None])
        self.assertEqual({s["source_id"] for s in seen[1] if not s["core"]}, {s.source_id for s in sources[22:24]})
        self.assertEqual([s["source_id"] for s in seen[1]], [s.source_id for s in sources[24:] + sources[22:24]])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE job_id=?", (old["job_id"],)).fetchone()[0], "done")
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='extract_page'").fetchone()[0], 2)
        calls = len(provider.calls)
        engine.extract_batch("frozen-context")
        self.assertEqual(len(provider.calls), calls)

    def test_new_unit_policy_cannot_fork_an_unknown_existing_page(self):
        capture(self.store, "old-unknown", ["烤箱温度误设为一百八十度，应该是一百六十度。"])
        text = FakeText()
        engine = SemanticEngine(self.store, text, FakeDecision())
        # Freeze the exact historical child before any newly versioned page
        # can be created; an unknown result must stop the whole batch.
        with patch.object(engine, "_accepted_extract_page", side_effect=RuntimeError("synthetic stop")):
            with self.assertRaises(RuntimeError):
                engine.extract_batch("old-unknown")
        with closing(self.store.connect()) as db:
            row = db.execute("SELECT inputs_json FROM jobs WHERE kind='extract_page'").fetchone()
        old = engine.jobs.ensure("extract_page", json.loads(row[0]),
                                {"prompt": "semantic-extraction-pages-v2-attribution"})
        engine.jobs.claim(old["job_id"], engine.worker_id)
        engine.jobs.fail(old["job_id"], engine.worker_id, "synthetic_unknown", unknown=True)
        with self.assertRaisesRegex(SemanticValidationError, "semantic_job_busy_or_unknown"):
            engine.extract_batch("old-unknown")
        self.assertEqual(text.calls, [])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM accepted_job_artifacts").fetchone()[0], 0)

    def test_extraction_page_crash_reuses_already_accepted_pages(self):
        class InterruptedText(FakeText):
            failed = False
            def generate(self, prompt, **kwargs):
                if len(self.calls) == 2 and not self.failed:
                    self.failed = True
                    self.calls.append(kwargs["request_id"])
                    raise RuntimeError("synthetic page interruption")
                return super().generate(prompt, **kwargs)
        capture(self.store, "large", ["很多个独立的生活场景。" * 500 for _ in range(5)])
        text = InterruptedText()
        engine = SemanticEngine(self.store, text, FakeDecision())
        with self.assertRaisesRegex(RuntimeError, "synthetic page interruption"):
            engine.extract_batch("large")
        prior = text.calls[:2]
        extracted = engine.extract_batch("large")
        self.assertTrue(extracted["receipt"])
        self.assertTrue(all(text.calls.count(identity) == 1 for identity in prior))

    def test_one_unclassified_fragment_keeps_whole_source_unread(self):
        class PartialText(FakeText):
            def generate(self, prompt, **kwargs):
                if prompt.startswith("The following captured") and self.calls:
                    self.calls.append(kwargs["request_id"])
                    return TextResult('{"observations":[]}', self.route)
                return super().generate(prompt, **kwargs)
        capture(self.store, "partial", ["有明确内容但后半段尚未解析。" * 1000])
        result = SemanticEngine(self.store, PartialText(), FakeDecision()).process_batch("partial")
        self.assertEqual(result["status"], "unclassified")
        self.assertEqual(self.store.coverage()["unread_sources"], 1)
        self.assertTrue(self.store.list_items())


if __name__ == "__main__":
    unittest.main()
