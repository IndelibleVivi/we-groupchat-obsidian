from dataclasses import replace
from datetime import datetime, timezone
import json
from unittest.mock import patch

from core.semantic.briefing import BriefChange, BriefingService
from core.semantic.controls import ReaderControls, SearchAnswer
from core.semantic.projections import ReadingError
from tests.test_semantic_projections import ReadingHarness, ScriptedTextProvider


def bounded_editor_response(prompt):
    """Synthetic editor exercises each accepted stage without a model call."""
    data = json.loads(prompt)
    if data["phase"] in {"brief_compact_page_v1", "brief_compact_merge_v1"}:
        evidence = data.get("evidence_ids", []) or [id_ for child in data.get("children", []) for id_ in child["evidence_ids"]]
        return {"binding": data["binding"], "coverage": data["coverage"], "title": "猫的日常",
                "summary": "这次日常续写记录猫和家人的相处；保留前后变化与给定范围，更正仍需读到原来的条件。",
                "evidence_ids": list(dict.fromkeys(evidence))[:16]}
    if data["phase"] == "brief_select_v1":
        return {"selected_change_ids": [c["id"] for c in data["changes"][:data["maximum_selected"]]], "reason": "日常片段构成连续故事。"}
    return {"status": "success", "title": "日常接在一起", "paragraphs": [{
        "text": "猫和家人的日常又接上了新的片段。", "change_ids": [c["id"] for c in data["changes"]],
        "evidence_refs": [{"item_id": c["item_id"], "evidence_id": c["evidence_ids"][0]} for c in data["changes"]],
        "scopes": list(dict.fromkeys(c["scope"] for c in data["changes"] if c["scope"])),
        "corrections": list(dict.fromkeys(c["correction"] for c in data["changes"] if c["correction"]))}]}


class BriefingTests(ReadingHarness):
    NOW = datetime(2026, 10, 13, 16, 30, tzinfo=timezone.utc)

    def setUp(self):
        super().setUp()
        self.controls = ReaderControls(self.ledger, self.publisher, search=lambda _: SearchAnswer("空", ()))

    def change(self, *, item="box-cat", revision=1, scope="", correction=""):
        return BriefChange("change-" + item + "-" + str(revision), item, revision, "猫的日常",
                           "猫住在纸箱。", "吃面的时候猫把箱子推到桌边。", ("e1",),
                           "2026-10-09", "2026-10-13", scope, correction,
                           interest="关心这段日常", kind="story")

    def response(self, *changes, text="吃面的日子，猫也搬着纸箱凑到桌边。这段小日常接上了上周的箱猫故事。"):
        return {"status": "success", "title": "猫把周末接起来了", "paragraphs": [{
            "text": text, "change_ids": [c.id for c in changes],
            "evidence_refs": [{"item_id": c.item_id, "evidence_id": "e1"} for c in changes],
            "scopes": list(dict.fromkeys(c.scope for c in changes if c.scope)),
            "corrections": list(dict.fromkeys(c.correction for c in changes if c.correction)),
        }]}

    def service(self, provider):
        return BriefingService(self.ledger, self.publisher, self.controls, provider, timezone="Asia/Singapore")

    def test_life_story_can_fill_entire_brief_one_local_day_and_same_edition(self):
        self.publish()
        change = self.change()
        provider = ScriptedTextProvider(self.response(change))
        service = self.service(provider)
        receipt = service.build((change,), now=self.NOW)
        self.assertEqual(receipt.status, "success")
        self.assertEqual(receipt.local_day, "2026-10-14")
        text = self.publisher.read(receipt.path).decode()
        self.assertIn("猫也搬着纸箱", text)
        self.assertNotIn("技术", text)
        self.assertIn("来源时间：2026-10-09；获知时间：2026-10-13", text)
        self.assertNotIn("![[", text)
        mtime = (self.publisher.root / receipt.path).stat().st_mtime_ns
        same = service.build((change,), now=self.NOW)
        self.assertTrue(same.unchanged)
        self.assertEqual(same.edition_id, receipt.edition_id)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual((self.publisher.root / receipt.path).stat().st_mtime_ns, mtime)
        self.assertEqual(len(list((self.publisher.root / "简报").glob("*.md"))), 1)

    def test_cross_item_editor_and_scope_correction_retained(self):
        self.publish()
        self.publish(self.snapshot(item_id="noodles", text="邻居送来面条。", title="面条"))
        first, second = self.change(scope="只描述本周末"), self.change(item="noodles", correction="昨天的说法不准确，面条是邻居送的。")
        provider = ScriptedTextProvider(self.response(first, second, text="两家的周末由一碗面和一只纸箱连起来。"))
        result = self.service(provider).build((first, second), now=self.NOW)
        self.assertEqual(result.status, "success")
        self.assertEqual(len(provider.calls), 1)
        prompt = json.loads(provider.calls[0][0])
        self.assertEqual(len(prompt["changes"]), 2)
        text = self.publisher.read(result.path).decode()
        self.assertIn("只描述本周末", text)
        self.assertIn("面条是邻居送的", text)

    def test_brief_exact_named_quote_and_wrong_citation_rejection(self):
        self.publish()
        change = replace(self.change(), attributions=({"evidence_id": "e1", "excerpt": "猫把箱子推到桌边。",
                                                      "speaker_id": "opaque-synthetic", "speaker_name": "小禾"},))
        forged = replace(change, id="quote-forgery", after="另一次日常")
        valid = "小禾记录了餐桌边的日常：\n\n> 猫把箱子推到桌边。\n\n- 箱猫故事继续了。"
        provider = ScriptedTextProvider(self.response(change, text=valid),
                                        self.response(forged, text=valid.replace("桌边", "湖边")))
        service = self.service(provider)
        first = service.build((change,), now=self.NOW)
        self.assertEqual(first.status, "success", first)
        text = self.publisher.read(first.path)
        self.assertIn("|小禾的消息]]", text.decode())
        self.assertIn("opaque-synthetic", provider.calls[0][0])
        invalid = service.build((forged,), now=self.NOW)
        self.assertEqual((invalid.status, invalid.code), ("deferred", "markdown_quote_not_grounded"))
        self.assertEqual(self.publisher.read(first.path), text)

    def test_large_attributed_excerpt_enters_all_pages_and_final_metadata(self):
        self.publish()
        excerpt = "阿岚转述邻居的纸箱故事，原作者没有留名。\n" * 2800
        change = replace(self.change(), attributions=({"evidence_id": "e1", "excerpt": excerpt,
                                                      "speaker_id": "opaque-relay", "speaker_name": "阿岚"},))
        provider = ScriptedTextProvider(*([bounded_editor_response] * 300))
        service = BriefingService(self.ledger, self.publisher, self.controls, provider,
                                  timezone="Asia/Singapore", max_input_bytes=12000)
        with patch.object(self.publisher, "publish_document", side_effect=RuntimeError("synthetic crash after acceptance")):
            failed = service.build((change,), now=self.NOW)
        self.assertEqual(failed.status, "deferred")
        count = len(provider.calls)
        result = service.build((change,), now=self.NOW)
        self.assertEqual(result.status, "success", result)
        self.assertEqual(len(provider.calls), count)
        prompts = [json.loads(prompt) for prompt, *_ in provider.calls]
        self.assertTrue(all(len(prompt.encode()) <= 12000 for prompt, *_ in provider.calls))
        parts = [part for page in prompts if page["phase"] == "brief_compact_page_v1" for part in page["parts"]
                 if part["field"] == "attributions.excerpt"]
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(part["text"] for part in parts), excerpt)
        self.assertTrue(all(part["evidence_id"] == "e1" and part["index"] == 0 for part in parts))
        final = next(value for value in prompts if value["phase"] == "brief_write_v1")["changes"][0]
        self.assertEqual(final["attributions"], [{"evidence_id": "e1", "speaker_id": "opaque-relay", "speaker_name": "阿岚"}])
        history = json.loads(service.history(result.edition_id)[0]["inputs_json"])[0]
        self.assertEqual(history["attributions"][0]["excerpt"], excerpt)

    def test_same_day_revision_preserves_historical_snapshot(self):
        snapshot = self.snapshot()
        self.publish(snapshot)
        first = self.change()
        second = self.change(revision=2)
        provider = ScriptedTextProvider(self.response(first, text="最早的箱猫版本。"),
                                        self.response(second, text="后来猫坐在了窗边。"))
        service = self.service(provider)
        initial = service.build((first,), now=self.NOW)
        self.revisions["box-cat"] = 2
        self.publish(replace(snapshot, content_revision=2))
        revised = service.build((second,), now=self.NOW)
        self.assertEqual(initial.edition_id, revised.edition_id)
        self.assertEqual(initial.path, revised.path)
        self.assertEqual(revised.revision, 2)
        history = service.history(initial.edition_id)
        self.assertEqual(len(history), 2)
        self.assertIn("最早的箱猫版本", history[0]["snapshot_text"])
        self.assertNotIn("后来猫坐在", history[0]["snapshot_text"])

    def test_silent_has_receipt_and_failure_preserves_last_success(self):
        snapshot = self.snapshot()
        self.publish(snapshot)
        first = self.change()
        provider = ScriptedTextProvider(self.response(first), RuntimeError("private provider response"))
        service = self.service(provider)
        result = service.build((first,), now=self.NOW)
        successful = self.publisher.read(result.path)
        self.revisions["box-cat"] = 2
        self.publish(replace(snapshot, content_revision=2))
        failed = service.build((self.change(revision=2),), now=self.NOW)
        self.assertEqual(failed.status, "deferred")
        self.assertEqual(failed.code, "brief_provider_failed")
        self.assertEqual(self.publisher.read(result.path), successful)
        self.assertEqual(len(service.history(result.edition_id)), 1)
        silent = service.build((), now=self.NOW)
        self.assertEqual(silent.status, "silent")
        self.assertEqual(service.edition(silent.local_day)["status"], "silent")
        self.assertEqual(self.publisher.read(result.path), successful)

    def test_editorial_silent_is_not_provider_failure(self):
        self.publish()
        provider = ScriptedTextProvider({"status": "silent", "reason": "只是原文重发，没有值得打扰的变化。", "paragraphs": []})
        result = self.service(provider).build((self.change(),), now=self.NOW)
        self.assertEqual(result.status, "silent")
        self.assertFalse((self.publisher.root / "简报").exists())
        row = self.ledger.one("SELECT * FROM reader_brief_attempts")
        self.assertEqual(row["status"], "silent")
        self.assertTrue(row["plan_json"])

    def test_accepted_plan_survives_publication_crash_and_resume_uses_no_provider(self):
        self.publish()
        change = self.change()
        provider = ScriptedTextProvider(self.response(change))
        service = self.service(provider)
        with patch.object(self.publisher, "publish_document", side_effect=RuntimeError("synthetic writer crash")):
            interrupted = service.build((change,), now=self.NOW, request_id="brief-crash")
        self.assertEqual(interrupted.status, "deferred")
        self.assertTrue(self.ledger.one("SELECT plan_json FROM reader_brief_attempts WHERE request_id='brief-crash'")[0])
        self.assertFalse((self.publisher.root / "简报").exists())
        resumed = service.build((change,), now=self.NOW, request_id="brief-crash")
        self.assertEqual(resumed.status, "success")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(service.history(resumed.edition_id)), 1)

    def test_unknown_provider_result_does_not_reissue_under_new_request(self):
        self.publish()
        provider = ScriptedTextProvider(RuntimeError("unknown transport result"))
        service = self.service(provider)
        first = service.build((self.change(),), now=self.NOW, request_id="unknown")
        self.assertEqual(first.status, "deferred")
        again = service.build((self.change(),), now=self.NOW, request_id="different", retry=True)
        self.assertEqual(again.code, "brief_provider_outcome_unknown")
        self.assertEqual(len(provider.calls), 1)

    def test_adopted_brief_conflict_finalizes_same_edition_without_new_provider(self):
        snapshot = self.snapshot()
        self.publish(snapshot)
        initial, newer = self.change(), self.change(revision=2)
        provider = ScriptedTextProvider(self.response(initial), self.response(newer, text="窗边续篇。"))
        service = self.service(provider)
        first = service.build((initial,), now=self.NOW)
        path = self.publisher.root / first.path
        path.write_text(path.read_text().replace("猫也搬着纸箱", "我的个人改写"))
        self.revisions["box-cat"] = 2
        self.publish(replace(snapshot, content_revision=2))
        reviewed = service.build((newer,), now=self.NOW)
        self.assertEqual(reviewed.status, "review")
        proposal = next(row for row in self.publisher.pending_proposals() if row["object_id"] == first.edition_id)
        self.publisher.resolve_proposal(proposal["proposal_id"], "move_notes", expected_revision=2,
                                         expected_file_hash=proposal["file_hash"])
        resumed = service.build((newer,), now=self.NOW)
        self.assertEqual(resumed.status, "success")
        self.assertEqual(resumed.revision, 2)
        self.assertEqual(len(provider.calls), 2)
        self.assertIn("我的个人改写", path.read_text())
        self.assertEqual(len(service.history(resumed.edition_id)), 2)

    def test_large_candidate_set_uses_bounded_durable_selection_then_one_writer(self):
        self.publish()
        changes = tuple(replace(self.change(), id=f"change-{i}", after="生活的片段。" * 90 + str(i)) for i in range(70))
        def answer(prompt):
            data = json.loads(prompt)
            if data["phase"] == "brief_select_v1":
                return {"selected_change_ids": [c["id"] for c in data["changes"][:data["maximum_selected"]]],
                        "reason": "保留能够连接起这一天的日常片段。"}
            return {"status": "success", "title": "日常接在一起", "paragraphs": [{
                "text": "一天里的这些日常彼此呼应。", "change_ids": [c["id"] for c in data["changes"]],
                "evidence_refs": [{"item_id": "box-cat", "evidence_id": "e1"}], "scopes": [], "corrections": []}]}
        provider = ScriptedTextProvider(*([answer] * 100))
        service = BriefingService(self.ledger, self.publisher, self.controls, provider,
                                  timezone="Asia/Singapore", max_input_bytes=12000)
        with patch.object(self.publisher, "publish_document", side_effect=RuntimeError("synthetic writer crash")):
            interrupted = service.build(changes, now=self.NOW)
        self.assertEqual(interrupted.status, "deferred")
        self.assertTrue(all(len(prompt.encode()) <= 12000 for prompt, _request, _gen in provider.calls))
        self.assertEqual(sum(json.loads(prompt)["phase"] == "brief_write_v1" for prompt, *_ in provider.calls), 1)
        selections = self.ledger.all("SELECT * FROM reader_text_artifacts WHERE prompt_version='brief-select-v2'")
        self.assertTrue(selections)
        self.assertTrue(all(row["status"] == "accepted" for row in selections))
        call_count = len(provider.calls)
        resumed = service.build(changes, now=self.NOW)
        self.assertEqual(resumed.status, "success")
        self.assertEqual(len(provider.calls), call_count)

    def test_single_large_change_covers_all_input_and_restores_full_constraints(self):
        self.publish()
        change = replace(self.change(), before="周五的纸箱。" * 7000,
                         after="周六的面条。" * 9000 + "最后猫回到窗边。",
                         scope="这段记录只涉及这一家人的日常。" * 800,
                         correction="应以邻居后来的说明为准。" * 800,
                         conditions=("仍需留意猫愿不愿意靠近窗边。" * 800,))
        provider = ScriptedTextProvider(*([bounded_editor_response] * 200))
        service = BriefingService(self.ledger, self.publisher, self.controls, provider,
                                  timezone="Asia/Singapore", max_input_bytes=12000)
        result = service.build((change,), now=self.NOW)
        self.assertEqual(result.status, "success", result)
        prompts = [json.loads(prompt) for prompt, *_ in provider.calls]
        self.assertTrue(all(len(prompt.encode()) <= 12000 for prompt, *_ in provider.calls))
        pages = [p for p in prompts if p["phase"] == "brief_compact_page_v1"]
        parts = [part for page in pages for part in page["parts"]]
        self.assertEqual([part["part"] for part in parts], list(range(len(parts))))
        for field in ("before", "after", "scope", "correction"):
            fragments = [p for p in parts if p["field"] == field]
            self.assertEqual("".join(p["text"] for p in fragments), getattr(change, field))
            self.assertEqual(fragments[0]["start"], 0)
            self.assertEqual(fragments[-1]["end"], len(getattr(change, field)))
        self.assertTrue(any(p["phase"] == "brief_compact_merge_v1" for p in prompts))
        self.assertEqual(sum(p["phase"] == "brief_write_v1" for p in prompts), 1)
        retained = json.loads(self.ledger.one("SELECT inputs_json FROM reader_brief_inputs")[0])[0]
        self.assertEqual(retained["after"], change.after)
        historical = json.loads(service.history(result.edition_id)[0]["plan_json"])
        self.assertEqual(historical["paragraphs"][0]["scopes"], [change.scope])
        self.assertEqual(historical["paragraphs"][0]["corrections"], [change.correction])
        count = len(provider.calls)
        self.assertTrue(service.build((change,), now=self.NOW).unchanged)
        self.assertEqual(len(provider.calls), count)

    def test_compaction_resume_between_accepted_pages_and_after_final_accept(self):
        self.publish()
        change = replace(self.change(), after="窗边的猫的长故事。" * 7000)
        provider = ScriptedTextProvider(*([bounded_editor_response] * 200))
        service = BriefingService(self.ledger, self.publisher, self.controls, provider,
                                  timezone="Asia/Singapore", max_input_bytes=12000)
        from core.semantic import briefing
        original = briefing.accepted_text_artifact
        def crash_after_accept(*args, **kwargs):
            artifact = original(*args, **kwargs)
            if kwargs["prompt_version"] == "brief-compact-page-v2":
                raise KeyboardInterrupt("synthetic process exit after accepted page")
            return artifact
        with patch.object(briefing, "accepted_text_artifact", side_effect=crash_after_accept):
            with self.assertRaises(KeyboardInterrupt):
                service.build((change,), now=self.NOW, request_id="compaction-crash")
        first_request = provider.calls[0][1]
        self.assertEqual(self.ledger.one("SELECT status FROM reader_brief_attempts")[0], "running")
        def crash_after_writer(*args, **kwargs):
            artifact = original(*args, **kwargs)
            if kwargs["prompt_version"] == "brief-write-v3":
                raise KeyboardInterrupt("synthetic process exit after accepted writer")
            return artifact
        with patch.object(briefing, "accepted_text_artifact", side_effect=crash_after_writer):
            with self.assertRaises(KeyboardInterrupt):
                service.build((change,), now=self.NOW, request_id="new-request-is-not-a-new-job")
        self.assertEqual(sum(request == first_request for _, request, _ in provider.calls), 1)
        call_count = len(provider.calls)
        result = service.build((change,), now=self.NOW, request_id="compaction-crash")
        self.assertEqual(result.status, "success", result)
        self.assertEqual(len(provider.calls), call_count)
        self.assertEqual(len(service.history(result.edition_id)), 1)

    def test_compaction_forged_coverage_or_reference_is_rejected(self):
        self.publish()
        change = replace(self.change(), after="猫的漫长日常。" * 7000)
        for index, mutation in enumerate((lambda value: value.update(coverage=[0, 999999]),
                                          lambda value: value["coverage"].__setitem__(0, False),
                                          lambda value: value.update(evidence_ids=["invented"]))):
            with self.subTest(index=index):
                def forged(prompt):
                    value = bounded_editor_response(prompt)
                    mutation(value)
                    return value
                service = self.service(ScriptedTextProvider(forged))
                result = service.build((replace(change, id="forged-" + str(index)),), now=self.NOW)
                self.assertEqual((result.status, result.code), ("deferred", "invalid_brief_compaction"))
                self.assertFalse((self.publisher.root / "简报").exists())

    def test_explicit_correction_is_eligible_when_retained_quote_unchanged(self):
        self.publish()
        change = replace(self.change(correction="旧结论已撤回。"), before="旧的原话。", after="旧的原话。")
        provider = ScriptedTextProvider(self.response(change))
        self.assertEqual(self.service(provider).build((change,), now=self.NOW).status, "success")
        self.assertEqual(len(provider.calls), 1)

    def test_brief_proposal_is_stale_after_mute_even_when_content_unchanged(self):
        self.publish()
        change = self.change()
        provider = ScriptedTextProvider(self.response(change))
        service = self.service(provider)
        path = self.publisher._safe_path("简报/2026-10-14.md")
        personal = "# 个人当日日记\n整个页面没有可信机器 baseline。\n"
        path.write_text(personal)
        reviewed = service.build((change,), now=self.NOW)
        self.assertEqual(reviewed.status, "review")
        proposal = self.publisher.pending_proposals()[0]
        self.controls.set_preference("box-cat", expected_revision=0, mute=True)
        with self.assertRaisesRegex(ReadingError, "stale_proposal"):
            self.publisher.resolve_proposal(proposal["proposal_id"], "adopt", expected_revision=1,
                                             expected_file_hash=proposal["file_hash"])
        self.assertEqual(path.read_text(), personal)

    def test_manual_include_cannot_be_dropped_by_writer(self):
        self.publish()
        provider = ScriptedTextProvider({"status": "silent", "paragraphs": [], "reason": "想省略这一条"})
        result = self.service(provider).build((replace(self.change(), manual="include"),), now=self.NOW)
        self.assertEqual(result.status, "deferred")
        self.assertEqual(result.code, "brief_ignored_manual_include")

    def test_mute_known_and_manual_exclusion_do_not_enter_combined_prompt(self):
        self.publish()
        self.publish(self.snapshot(item_id="muted", text="私人的变化。"))
        self.publish(self.snapshot(item_id="known", text="已知道的变化。"))
        self.publish(self.snapshot(item_id="manual", text="明确排除。"))
        self.controls.set_preference("muted", expected_revision=0, follow=True, mute=True)
        self.controls.mark_known("known", 1, known_at="now")
        chosen = self.change()
        provider = ScriptedTextProvider(self.response(chosen))
        result = self.service(provider).build((chosen, self.change(item="muted"), self.change(item="known"),
                                              replace(self.change(item="manual"), manual="exclude")), now=self.NOW)
        self.assertEqual(result.status, "success")
        self.assertEqual([c["item_id"] for c in json.loads(provider.calls[0][0])["changes"]], ["box-cat"])

    def test_invalid_ref_and_lost_scope_do_not_publish(self):
        self.publish()
        change = self.change(scope="仅限此日常故事")
        invalid = self.response(change)
        invalid["paragraphs"][0]["evidence_refs"][0]["evidence_id"] = "fabricated"
        service = self.service(ScriptedTextProvider(invalid))
        self.assertEqual(service.build((change,), now=self.NOW).code, "invalid_brief_evidence")
        missing_scope = self.response(change)
        missing_scope["paragraphs"][0]["scopes"] = []
        service.provider = ScriptedTextProvider(missing_scope)
        self.assertEqual(service.build((change,), now=self.NOW, retry=True).code, "brief_lost_scope_or_correction")
        self.assertFalse((self.publisher.root / "简报").exists())

    def test_preference_changes_during_provider_reject_plan(self):
        self.publish()
        change = self.change()
        def mute_while_generating(_prompt):
            self.controls.set_preference("box-cat", expected_revision=0, mute=True)
            return self.response(change)
        provider = ScriptedTextProvider(mute_while_generating)
        result = self.service(provider).build((change,), now=self.NOW)
        self.assertEqual(result.code, "stale_brief_input")
        self.assertFalse((self.publisher.root / "简报").exists())

    def test_new_relation_comparison_is_change_but_pure_render_is_not(self):
        self.publish()
        change = replace(self.change(), before="同样文字", after="同样文字")
        provider = ScriptedTextProvider(self.response(change))
        service = self.service(provider)
        self.assertEqual(service.build((change,), now=self.NOW).status, "silent")
        self.assertEqual(provider.calls, [])
        relation = replace(change, relation_change=True)
        result = service.build((relation,), now=self.NOW)
        self.assertEqual(result.status, "success")
        same = service.rebuild(result.local_day)
        self.assertEqual(same.revision, result.revision)
        self.assertEqual(len(provider.calls), 1)
