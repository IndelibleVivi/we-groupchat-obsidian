import unittest
from dataclasses import replace
import tempfile
import json
from pathlib import Path
from ai.semantic_provider import TextResult
from core.semantic.engine import SemanticValidationError

from core.semantic.watches import Claim, EntityAlias, TemporalBoundary, WatchService, WatchSpec, evaluate_watch, resolve_entity
from core.semantic.store import SemanticStore
from tests.test_semantic_engine import FakeDecision, FakeText, SemanticEngine, capture


class WatchText(FakeText):
    def __init__(self, *, invent=False, unresolved=False):
        super().__init__(claims=True)
        self.invent, self.unresolved = invent, unresolved

    def generate(self, prompt, **kwargs):
        if not prompt.startswith("Map the reader"):
            return super().generate(prompt, **kwargs)
        self.calls.append(kwargs["request_id"])
        state = json.loads(prompt.rsplit("\n", 1)[-1])
        plan = {"status": "unresolved" if self.unresolved else "mapped", "kind": "occurrence",
                "predicates": {} if self.unresolved else {"invented" if self.invent else "success": True},
                "qualifiers": {}, "after": None, "reason": "合成条件映射依据。", "evidence_claim_ids": [state["claims"][0]["claim_id"]]}
        return TextResult(json.dumps(plan, ensure_ascii=False), self.route)


def claim(identity, predicates, *, person="alice", case="case-a", phase="test-1", time=10, **kwargs):
    defaults = {"subject_id": person, "case_id": case, "phase": phase,
                "condition": "overseas", "channel": "app", "version": "v1"}
    defaults.update(kwargs)
    return Claim(identity, "phone-plan", predicates, time, ("source-" + identity,), **defaults)


class WatchTests(unittest.TestCase):
    def test_named_phone_conditions_do_not_mix_people_or_plans(self):
        watch = WatchSpec("watch", "occurrence", {"esim": True, "overseas_card": True},
                          {"subject_id": "alice", "condition": "overseas"})
        a = claim("a", {"esim": True})
        b = claim("b", {"overseas_card": True}, person="bob")
        c = claim("c", {"overseas_card": True}, case="different-plan")
        anonymous_a = claim("anon-a", {"esim": True}, person=None, case=None)
        anonymous_b = claim("anon-b", {"overseas_card": True}, person=None, case=None)
        result = evaluate_watch(watch, [a, b, c, anonymous_a, anonymous_b])
        self.assertEqual(result.state, "unknown")
        exact = claim("d", {"overseas_card": True})
        self.assertEqual(evaluate_watch(watch, [a, b, c, exact]).state, "met")

    def test_unknown_qualifiers_are_not_join_keys(self):
        watch = WatchSpec("w", "occurrence", {"a": True, "b": True})
        claims = [claim("a", {"a": True}, phase=None), claim("b", {"b": True}, phase=None)]
        self.assertEqual(evaluate_watch(watch, claims).state, "unknown")
        self.assertEqual(evaluate_watch(watch, claims, coverage_complete=True).state, "unknown")
        complete_single = claim("c", {"a": True, "b": True}, phase=None)
        self.assertEqual(evaluate_watch(watch, [complete_single]).state, "met")

    def test_anonymous_success_cannot_establish_named_absence(self):
        watch = WatchSpec("w", "occurrence", {"success": True}, {"subject_id": "alice"})
        self.assertEqual(evaluate_watch(watch, [claim("anonymous", {"success": True}, person=None)], coverage_complete=True).state, "unknown")

    def test_recovery_announcement_requires_later_test(self):
        watch = WatchSpec("w", "occurrence", {"success": True}, {"subject_id": "alice"},
                          after=TemporalBoundary("service_recovered"))
        old = claim("old", {"success": True}, time=10)
        recovery = claim("recovery", {"service_recovered": True}, time=20)
        self.assertNotEqual(evaluate_watch(watch, [old, recovery]).state, "met")
        later = claim("later", {"success": True}, time=21)
        result = evaluate_watch(watch, [old, recovery, later])
        self.assertEqual(result.state, "met")
        self.assertEqual(result.witnesses[0].boundary_claim_id, "recovery")
        self.assertEqual(result.witnesses[0].claim_ids, ("later",))

    def test_recovery_boundary_uses_same_case_across_different_phases(self):
        watch = WatchSpec("w", "occurrence", {"success": True}, {"phase": "tested"}, after=TemporalBoundary("service_recovered"))
        recovery = claim("recover-a", {"service_recovered": True}, time=20, phase="announcement")
        success = claim("test-a", {"success": True}, time=21, phase="tested")
        unrelated = claim("recover-b", {"service_recovered": True}, person="bob", case="other", time=50, phase="announcement")
        result = evaluate_watch(watch, [recovery, success, unrelated])
        self.assertEqual(result.state, "met")
        self.assertEqual(result.witnesses[0].boundary_claim_id, "recover-a")
        self.assertEqual(evaluate_watch(watch, [success, unrelated]).state, "unknown")

    def test_independent_witness_survives_then_all_revoked_reopens(self):
        watch = WatchSpec("w", "occurrence", {"success": True})
        first, second = claim("a", {"success": True}), claim("b", {"success": True}, person="bob", case="case-b")
        correction = claim("correction", {"correction": True}, retracts=("a",))
        result = evaluate_watch(watch, [first, second, correction], previous_state="met")
        self.assertEqual(result.state, "met")
        self.assertEqual(result.witnesses[0].claim_ids, ("b",))
        correction2 = claim("correction2", {"correction": True}, retracts=("b",))
        reopened = evaluate_watch(watch, [first, second, correction, correction2], previous_state="met", coverage_complete=True)
        self.assertEqual(reopened.state, "unknown")
        self.assertEqual(reopened.change, "reopened")

    def test_current_state_uses_latest_case_but_occurrence_retains_evidence(self):
        old = claim("a", {"success": True}, time=10)
        new = claim("b", {"success": False}, time=11)
        current = WatchSpec("w", "current_state", {"success": True})
        self.assertEqual(evaluate_watch(current, [old, new], coverage_complete=True).state, "unmet")
        simultaneous = replace(old, observed_at=11)
        self.assertEqual(evaluate_watch(current, [simultaneous, new], coverage_complete=True).state, "contested")

    def test_review_after_does_not_expire_and_follow_has_no_deadline(self):
        current = claim("a", {"success": True})
        watch = WatchSpec("w", "occurrence", {"success": True}, review_after=1)
        result = evaluate_watch(watch, [current], now=100000)
        self.assertEqual(result.state, "met")
        self.assertTrue(result.review_due)
        follow = WatchSpec("follow", "follow", item_ids=("phone-plan",))
        result = evaluate_watch(follow, [current], now=100000)
        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.change, "progress")

    def test_follow_forwarded_material_keeps_provenance_without_new_progress(self):
        watch = WatchSpec("w", "follow")
        original = claim("a", {"success": True}, material_id="shared-material")
        forwarded = claim("b", {"success": True}, material_id="shared-material", time=20)
        first = evaluate_watch(watch, [original])
        result = evaluate_watch(watch, [original, forwarded], previous_witness_ids=[first.witnesses[0].witness_id])
        self.assertIsNone(result.change)
        self.assertEqual(len(result.witnesses), 1)
        self.assertEqual(result.witnesses[0].claim_ids, ("a", "b"))

    def test_absence_needs_complete_canonical_and_source_coverage(self):
        watch = WatchSpec("w", "occurrence", {"success": True})
        self.assertEqual(evaluate_watch(watch, []).state, "unknown")
        self.assertEqual(evaluate_watch(watch, [], coverage_complete=True).state, "unmet")

    def test_close_names_and_scoped_aliases_do_not_merge(self):
        aliases = [EntityAlias("alice", "小李", "group-a", ("a",)), EntityAlias("bob", "小李", "group-b", ("b",))]
        self.assertEqual(resolve_entity(aliases, "小李", scope="group-a"), "alice")
        self.assertIsNone(resolve_entity(aliases, "小莉", scope="group-a"))
        self.assertIsNone(resolve_entity(aliases, "小李", scope="group-c"))
        self.assertIsNone(resolve_entity(aliases + [EntityAlias("other", "小李", "group-a", ("c",))], "小李", scope="group-a"))

    def test_same_material_is_one_witness_and_revocation_does_not_leave_forward_alive(self):
        watch = WatchSpec("w", "occurrence", {"success": True})
        first = claim("original", {"success": True}, material_id="material-a")
        initial = evaluate_watch(watch, [first])
        forward = claim("forward", {"success": True}, person="bob", case="case-b", material_id="material-a")
        forwarded = evaluate_watch(watch, [first, forward], previous_state="met",
                                   previous_witness_ids=[w.witness_id for w in initial.witnesses])
        self.assertEqual(len(forwarded.witnesses), 1)
        self.assertIsNone(forwarded.change)
        correction = claim("correction", {"correction": True}, retracts=("original",))
        self.assertEqual(evaluate_watch(watch, [first, forward, correction], previous_state="met").change, "reopened")

    def test_other_case_failure_does_not_revoke_a_valid_occurrence_witness(self):
        watch = WatchSpec("w", "occurrence", {"success": True})
        valid = claim("success", {"success": True})
        other = claim("failure", {"success": False}, person="bob", case="case-b")
        result = evaluate_watch(watch, [valid, other], coverage_complete=True)
        self.assertEqual(result.state, "met")
        self.assertEqual({w.state for w in result.witnesses}, {"met", "unmet"})


class WatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SemanticStore(Path(self.temp.name).resolve() / "knowledge.sqlite", "watch-generation")
        self.text, self.decision = FakeText(claims=True), FakeDecision()
        self.engine = SemanticEngine(self.store, self.text, self.decision)
        self.service = WatchService(self.store, clock=lambda: 1000)

    def test_claim_correction_recomputes_and_preserves_witness_history(self):
        capture(self.store, "first", ["success A", "success B"], payloads=[
            {"subject_anchor": "alice", "case_anchor": "plan-a"},
            {"subject_anchor": "bob", "case_anchor": "plan-b"}])
        self.engine.process_batch("first")
        original = self.store.list_claims()
        self.service.create_watch(WatchSpec("watch", "occurrence", {"success": True}))
        self.assertEqual(self.service.evaluate("watch").state, "met")
        capture(self.store, "correct", ["failure correction"], payloads=[{"subject_anchor": "alice", "case_anchor": "plan-a"}])
        self.engine.process_batch("correct")
        correction = next(value for value in self.store.list_claims() if value["claim_id"] not in {row["claim_id"] for row in original})
        self.service.record_claim_relation(correction["claim_id"], original[0]["claim_id"], "corrects_or_retracts",
                                           reason="synthetic explicit correction", evidence_ids=correction["span_ids"],
                                           expected_revision=self.store.read_revision())
        self.assertEqual(self.service.get_watch("watch")["state"], "met")
        self.service.record_claim_relation(correction["claim_id"], original[1]["claim_id"], "corrects_or_retracts",
                                           reason="synthetic second correction", evidence_ids=correction["span_ids"],
                                           expected_revision=self.store.read_revision())
        self.assertNotEqual(self.service.get_watch("watch")["state"], "met")
        self.assertEqual(self.service.changes("watch")[-1]["kind"], "reopened")
        history = self.service.witness_history("watch")
        self.assertTrue(any(not value["active"] and value["state"] == "met" for value in history))

    def test_late_attachment_reopens_persistent_episode(self):
        receipt = capture(self.store, "first", ["稍后补充旅行路线文件"])
        self.engine.process_batch("first")
        item = self.store.list_items()[0]
        opened = self.service.open_episode("episode", item["item_id"], source_ids=receipt["member_version_ids"])
        closed = self.service.close_episode("episode", expected_revision=opened["revision"])
        late = capture(self.store, "late", ["文件稍后到达"])
        result = self.service.attach_episode("episode", late["member_version_ids"][0], relation="resource",
                                             resource_id="synthetic-resource", status="pending", expected_revision=closed["revision"])
        self.assertEqual(result["status"], "unresolved")
        reopened = next(value for value in WatchService(self.store).episodes(item_id=item["item_id"]) if value["episode_id"] == "episode")
        self.assertEqual(reopened["links"][0]["status"], "pending")
        self.assertEqual(reopened["links"][0]["resource_id"], "synthetic-resource")

    def test_engine_late_reply_and_resource_reopen_exact_episode_once(self):
        first = capture(self.store, "first", ["稍后补旅行文件"])
        self.engine.process_batch("first")
        episode = self.service.episodes()[0]
        self.assertEqual(episode["source_ids"], first["member_version_ids"])
        self.service.close_episode(episode["episode_id"], expected_revision=episode["revision"])
        late = capture(self.store, "late", ["这里是旅行路线文件"], payloads=[{
            "reply_to": "first-0", "resources": [{"resource_id": "route-file", "occurrence_id": "late-0",
                "purpose": "attachment", "status": "pending", "location": None, "metadata": {}}]}])
        late_engine = SemanticEngine(self.store, self.text, FakeDecision(possible=0))
        late_engine.process_batch("late")
        reopened = next(value for value in self.service.episodes() if value["episode_id"] == episode["episode_id"])
        self.assertEqual(len(self.service.episodes()), 2)
        self.assertEqual(reopened["status"], "unresolved")
        self.assertEqual({(link["source_id"], link["relation"], link["resource_id"], link["status"]) for link in reopened["links"]},
                         {(late["member_version_ids"][0], "reply", "", "available"),
                          (late["member_version_ids"][0], "resource", "route-file", "pending")})
        self.service.close_episode(reopened["episode_id"], expected_revision=reopened["revision"])
        before = self.service.episodes()
        calls = len(self.text.calls), len(late_engine.decision_provider.calls)
        late_engine.process_batch("late")
        self.assertEqual(self.service.episodes(), before)
        self.assertEqual((len(self.text.calls), len(late_engine.decision_provider.calls)), calls)

    def test_reply_from_other_chat_does_not_reopen_same_named_occurrence(self):
        first = capture(self.store, "first", ["稍后补充资料"])
        self.engine.process_batch("first")
        episode = self.service.episodes()[0]
        self.service.close_episode(episode["episode_id"], expected_revision=episode["revision"])
        self.store.capture_batch("other-chat", [{
            "source_id": "other-chat-0", "logical_shard_id": "synthetic-shard", "physical_generation": "synthetic-generation",
            "source_cursor_token": "1", "source_sent_at": 10, "text": "另一个群的回复",
            "payload": {"reply_to": first["member_version_ids"][0]}}], source_namespace="synthetic", chat_id="group-b",
            inventory_digest="synthetic-inventory", inventory_revision=1, input_cursors={},
            output_cursors={"synthetic-shard": "1"}, source_eof=True)
        SemanticEngine(self.store, self.text, FakeDecision(possible=0)).process_batch("other-chat")
        unchanged = next(value for value in self.service.episodes() if value["episode_id"] == episode["episode_id"])
        self.assertEqual(unchanged["status"], "resolved")
        self.assertEqual(unchanged["links"], [])

    def test_engine_explicit_claim_correction_links_new_message_to_old_episode(self):
        capture(self.store, "first", ["success A"])
        self.engine.process_batch("first")
        episode = self.service.episodes()[0]
        self.service.close_episode(episode["episode_id"], expected_revision=episode["revision"])
        target = self.store.list_claims()[0]["claim_id"]
        class CorrectionText(FakeText):
            def generate(self, prompt, **kwargs):
                result = super().generate(prompt, **kwargs)
                if not prompt.startswith("Treat all supplied"):
                    return result
                state = json.loads(prompt.rsplit("\n", 1)[-1])
                value = json.loads(result.text)
                value["actions"][0]["claim_relations"] = [{
                    "from_claim_id": state["observations"][0]["claims"][0]["claim_id"],
                    "to_claim_id": target, "relation": "corrects_or_retracts", "reason": "Explicit synthetic correction"}]
                return replace(result, text=json.dumps(value))
        correction = capture(self.store, "correction", ["failure corrected A"])
        SemanticEngine(self.store, CorrectionText(claims=True), self.decision).process_batch("correction")
        reopened = self.service.episodes()[0]
        self.assertEqual(reopened["status"], "unresolved")
        self.assertIn((correction["member_version_ids"][0], "correction"),
                      {(link["source_id"], link["relation"]) for link in reopened["links"]})

    def test_engine_source_version_correction_reopens_only_exact_scoped_anchor(self):
        initial = capture(self.store, "first", ["同名案例的最初记录"])
        self.engine.process_batch("first")
        original = self.service.episodes()[0]
        self.service.close_episode(original["episode_id"], expected_revision=original["revision"])
        # The next Item has the same text and timestamp, but a distinct occurrence.
        isolated = SemanticEngine(self.store, self.text, FakeDecision(possible=0))
        capture(self.store, "unrelated", ["同名案例的最初记录"])
        isolated.process_batch("unrelated")
        other = next(value for value in self.service.episodes() if value["episode_id"] != original["episode_id"])
        self.service.close_episode(other["episode_id"], expected_revision=other["revision"])
        corrected = self.store.capture_batch("corrected", [{
            "source_id": "first-0", "logical_shard_id": "synthetic-shard", "physical_generation": "synthetic-generation",
            "source_cursor_token": "2", "source_sent_at": 10, "text": "原消息的修正版本", "payload": {}}],
            source_namespace="synthetic", chat_id="group-a", inventory_digest="synthetic-inventory", inventory_revision=1,
            input_cursors={}, output_cursors={"synthetic-shard": "2"}, source_eof=True)
        isolated.process_batch("corrected")
        episodes = {value["episode_id"]: value for value in self.service.episodes()}
        self.assertEqual(episodes[original["episode_id"]]["status"], "unresolved")
        self.assertEqual(episodes[other["episode_id"]]["status"], "resolved")
        links = episodes[original["episode_id"]]["links"]
        self.assertEqual([(link["source_id"], link["relation"]) for link in links],
                         [(corrected["member_version_ids"][0], "correction")])
        self.assertNotEqual(initial["member_version_ids"], corrected["member_version_ids"])

    def test_episode_sync_recovers_after_canonical_commit_without_provider_replay(self):
        capture(self.store, "first", ["success A"])
        original = self.engine._sync_episodes
        def interrupted(_receipt):
            raise RuntimeError("synthetic episode interruption")
        self.engine._sync_episodes = interrupted
        with self.assertRaisesRegex(RuntimeError, "synthetic episode interruption"):
            self.engine.process_batch("first")
        self.assertEqual(len(self.store.list_items()), 1)
        self.assertEqual(self.service.episodes(), ())
        counts = len(self.text.calls), len(self.decision.calls)
        self.engine._sync_episodes = original
        result = self.engine.process_batch("first")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["episodes"]), 1)
        self.assertEqual((len(self.text.calls), len(self.decision.calls)), counts)

    def test_same_display_name_different_explicit_chat_scope_remains_distinct(self):
        capture(self.store, "first", ["success A", "success B"], payloads=[
            {"subject_anchor": "person-a", "display_name": "小李", "case_anchor": "a"},
            {"subject_anchor": "person-b", "display_name": "小李", "case_anchor": "b"}])
        self.engine.process_batch("first")
        self.assertEqual(len({row["subject_id"] for row in self.store.list_claims()}), 2)

    def test_reader_wait_maps_typed_condition_replays_and_cancel_preserves_history(self):
        capture(self.store, "first", ["success A"], payloads=[{"subject_anchor": "alice", "case_anchor": "plan-a"}])
        self.engine.process_batch("first")
        item = self.store.list_items()[0]
        text, decision = WatchText(), FakeDecision()
        service = WatchService(self.store, text_provider=text, decision_provider=decision, clock=lambda: 1000)
        result = service.sync_reader_watch(item["item_id"], 1, "等有明确成功案例", False)
        self.assertEqual(result["status"], "met")
        self.assertEqual(result["evaluation"]["witnesses"][0]["claim_ids"], (item["claims"][0]["claim_id"],))
        before = len(text.calls), len(decision.calls)
        replay = service.sync_reader_watch(item["item_id"], 1, "等有明确成功案例", False)
        self.assertEqual(replay["changes"], result["changes"])
        self.assertEqual((len(text.calls), len(decision.calls)), before)
        stopped = service.sync_reader_watch(item["item_id"], 2, "", False)
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(service.witness_history(result["watch_id"]))
        self.assertEqual(service.evaluate_affected((item["claims"][0]["claim_id"],)), ())
        with self.assertRaisesRegex(SemanticValidationError, "watch_reader_preference_changed"):
            service.sync_reader_watch(item["item_id"], 1, "等有明确成功案例", False)

    def test_reader_unmapped_condition_stays_unknown_and_cannot_invent_predicate(self):
        capture(self.store, "first", ["success A"])
        self.engine.process_batch("first")
        identity = self.store.list_items()[0]["item_id"]
        unresolved = WatchService(self.store, text_provider=WatchText(unresolved=True), decision_provider=FakeDecision())
        result = unresolved.sync_reader_watch(identity, 1, "等待尚未出现的明确条件", False)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["watch_id"])
        self.assertEqual(result["counterevidence"][0]["kind"], "unmapped_condition")
        invalid = WatchService(self.store, text_provider=WatchText(invent=True), decision_provider=FakeDecision())
        with self.assertRaisesRegex(SemanticValidationError, "watch_reader_unknown_predicate"):
            invalid.sync_reader_watch(identity, 2, "第二个条件", False)

    def test_reader_follow_needs_no_provider_and_survives_no_claims(self):
        capture(self.store, "first", ["纯生活片段"])
        SemanticEngine(self.store, FakeText(), FakeDecision()).process_batch("first")
        identity = self.store.list_items()[0]["item_id"]
        result = self.service.sync_reader_watch(identity, 0, "", True)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.service.get_watch(result["watch_id"])["spec"]["kind"], "follow")
        self.assertIsNone(self.service.get_watch(result["watch_id"])["spec"]["review_after"])

    def test_reader_mapping_accepted_before_commit_survives_crash(self):
        capture(self.store, "first", ["success A"])
        self.engine.process_batch("first")
        identity = self.store.list_items()[0]["item_id"]
        text, decision = WatchText(), FakeDecision()
        service = WatchService(self.store, text_provider=text, decision_provider=decision, clock=lambda: 1000)
        original = service.evaluate
        service.evaluate = lambda _identity: (_ for _ in ()).throw(RuntimeError("synthetic evaluation interruption"))
        with self.assertRaisesRegex(RuntimeError, "synthetic evaluation interruption"):
            service.sync_reader_watch(identity, 1, "等待成功", False)
        counts = len(text.calls), len(decision.calls)
        service.evaluate = original
        self.assertEqual(service.sync_reader_watch(identity, 1, "等待成功", False)["status"], "met")
        self.assertEqual((len(text.calls), len(decision.calls)), counts)

    def test_evidence_only_attachment_is_not_an_independent_success_witness(self):
        capture(self.store, "first", ["failure A"])
        self.engine.process_batch("first")
        episode = self.service.episodes()[0]
        self.service.close_episode(episode["episode_id"], expected_revision=episode["revision"])
        closed = self.service.episodes()
        class EvidenceOnlyText(FakeText):
            def generate(self, prompt, **kwargs):
                result = super().generate(prompt, **kwargs)
                if prompt.startswith("The following captured") or "Summarize ONLY" in prompt:
                    return result
                value = json.loads(result.text)
                for action in value["actions"]:
                    if action["type"] == "MERGE_INTO_ITEM":
                        action["semantic_effect"] = "none"
                return replace(result, text=json.dumps(value))
        capture(self.store, "forward", ["success forwarded A"])
        SemanticEngine(self.store, EvidenceOnlyText(claims=True), self.decision).process_batch("forward")
        self.assertEqual(self.service.episodes(), closed)
        self.service.create_watch(WatchSpec("watch", "occurrence", {"success": True}))
        result = self.service.evaluate("watch")
        self.assertEqual(result.state, "unmet")
        self.assertFalse(any(w.state == "met" for w in result.witnesses))


if __name__ == "__main__":
    unittest.main()
