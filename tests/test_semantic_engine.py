import json
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

from core.semantic.engine import CapturedSource, SemanticValidationError, extraction_prompt, prepare_spans, validate_extraction
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


class ExtractionTests(unittest.TestCase):
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
                values.append({"title": text[:60], "scene_kind": "life" if "花" in text else "technical",
                               "spans": [{"source_id": source["source_id"], "span_id": "bad" if self.malformed else span["span_id"]} for span in source["spans"]],
                               "units": [{"kind": "scene", "text": text, "span_indices": list(range(len(source["spans"])))}], "topics": [], "claims": claims})
            result = {"observations": values}
            if self.noise:
                result["dispositions"] = {source["source_id"]: {"status": "noise", "reason": "synthetic_empty_acknowledgment"}
                                          for source in data if source["core"]}
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
                    result.append({"title": source["speaker_name"] + "的讲述", "scene_kind": "life",
                        "spans": [{"source_id": source["source_id"], "span_id": source["spans"][0]["span_id"]}],
                        "units": [{"kind": "quote", "text": source["spans"][0]["excerpt"], "span_indices": [0]}],
                        "topics": [], "claims": [claim]})
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
        with self.assertRaisesRegex(SemanticValidationError, "semantic_invalid_source_span"):
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
