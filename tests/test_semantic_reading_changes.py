"""Synthetic acceptance for known-baseline reading change pages."""
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.semantic.controls import OpenObservation, ReaderControls, SearchAnswer
from core.semantic.projections import (
    BodyBlock, BodyPlan, CanonicalReadingAdapter, ProjectionPublisher, ReadingError,
    wikilink,
)
from core.semantic.reading_changes import ReadingChanges, ReadingChangesReceipt
from core.semantic.store import SemanticStore
from tests.test_semantic_projections import ScriptedTextProvider, validate_body_plan


class ReadingChangesTests(unittest.TestCase):
    NOW = "2026-10-24T12:00:00+00:00"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.store = SemanticStore(self.base / "generation.sqlite", "changes-generation")
        self.publisher = ProjectionPublisher.for_store(self.store, self.base / "WGO")
        self.ledger = self.publisher.ledger
        self.controls = ReaderControls(
            self.ledger, self.publisher, search=lambda _: SearchAnswer("空", ()))
        self.changes = ReadingChanges(self.store, self.publisher, self.controls)
    @staticmethod
    def _claims(values):
        return [{
            "subject_id": None,
            "case_id": None,
            "condition": value.get("condition"),
            "channel": None,
            "version": None,
            "phase": None,
            "observed_at": 1791763200.0,
            "predicates": value.get("predicates", {"state": "initial"}),
            "span_indices": [0],
        } for value in values]

    def extract(self, key, text, *, kind="fact", claims=None):
        from core.semantic.attribution import (
            EXTRACTION_PROMPT_VERSION, captured_source_metadata,
        )
        from core.semantic.engine import (
            CapturedSource, prepare_spans, validate_accepted_artifact,
            validate_extraction,
        )
        from core.semantic.jobs import JobStore

        batch = "batch-" + key
        capture = self.store.capture_batch(
            batch, [{
                "source_id": batch + "-message", "logical_shard_id": "shard",
                "physical_generation": "physical", "source_cursor_token": "cursor",
                "source_sent_at": 1791763200.0, "text": text, "payload": {},
            }], source_namespace="synthetic", chat_id="synthetic-chat",
            inventory_digest="synthetic-inventory", inventory_revision=1,
            input_cursors={}, output_cursors={}, source_eof=True)
        source = CapturedSource(
            capture["member_version_ids"][0], text, occurred_at=1791763200.0,
            **captured_source_metadata("synthetic", "synthetic-chat", {}))
        span = prepare_spans((source,), namespace=self.store.generation_id)[0]
        observations = [value.as_dict() for value in validate_extraction({
            "observations": [{
                "title": "合成条目", "scene_kind": "technical",
                "spans": [{"source_id": source.source_id, "span_id": span.span_id}],
                "units": [{"kind": kind, "text": text, "span_indices": [0]}],
                "topics": [], "claims": self._claims(claims or []),
            }]
        }, (source,), namespace=self.store.generation_id)]
        artifact = {"observations": observations, "dispositions": {}}
        jobs = JobStore(self.store)
        job = jobs.ensure(
            "extract", {"batch_id": batch, "members": [asdict(source)]},
            {"schema": "synthetic", "prompt": EXTRACTION_PROMPT_VERSION})
        jobs.claim(job["job_id"], "synthetic-worker")
        jobs.accept(
            job["job_id"], "synthetic-worker", artifact,
            validate=lambda value: validate_accepted_artifact(value, artifact))
        self.store.commit_extraction(job["job_id"], batch, observations)
        return observations[0], span.span_id

    def actions(self, actions):
        from core.semantic.engine import validate_accepted_artifact
        from core.semantic.jobs import JobStore

        items = {value["item_id"]: self.store.get_item(value["item_id"])
                 for value in self.store.list_items()}
        read_set = {
            "store_revision": self.store.read_revision(),
            "items": {key: value["content_revision"] for key, value in items.items()},
            "views": {},
        }
        observations = list({
            value["observation_id"]: value for item in items.values()
            for value in item["observations"]}.values())
        for action in actions:
            for observation_id in action.get("observation_ids", ()):
                if observation_id not in {value["observation_id"] for value in observations}:
                    observations.append(self.store.get_observation(observation_id))
        state = {"read_set": read_set, "observations": observations, "items": items}
        artifact = {"actions": actions, "read_set": read_set}
        jobs = JobStore(self.store)
        job = jobs.ensure("propose", {"state": state}, {"schema": "synthetic"})
        jobs.claim(job["job_id"], "synthetic-worker")
        jobs.accept(
            job["job_id"], "synthetic-worker", artifact,
            validate=lambda value: validate_accepted_artifact(value, artifact))
        return self.store.apply_proposal(job["job_id"], actions, read_set=read_set)

    def create(self, item_id, observation_id, *, title, key=None):
        return self.actions([{
            "action_key": key or "create-" + item_id, "type": "CREATE_ITEM",
            "item_id": item_id, "kind": "technical", "title": title,
            "identity_scope": {"subject": item_id},
            "observation_ids": [observation_id],
        }])

    def publish_item(self, item_id, *, block_text=None):
        snapshot = CanonicalReadingAdapter(self.store).snapshot(item_id)
        plan = validate_body_plan(snapshot, BodyPlan(
            item_id, snapshot.content_revision,
            tuple(BodyBlock(
                "change-home-" + unit.id, unit.role,
                block_text if block_text is not None else unit.text,
                (unit.id,), unit.evidence_ids, unit.resource_ids, item_id,
                {unit.id: unit.status} if unit.status != "current" else {})
                for unit in snapshot.units)))
        return self.publisher.publish_item(snapshot, plan)

    def item(self, item_id="cat", *, title="合成条目", text="第一条内容。", claims=None):
        observation, span = self.extract(item_id, text, claims=claims)
        self.create(item_id, observation["observation_id"], title=title)
        self.publish_item(item_id)
        return observation, span

    def append(self, item_id="cat", *, text="第二条内容。", effect="detail_added",
               claims=None, publish=True):
        observation, span = self.extract(
            item_id + "-" + str(self.store.read_revision()) + "-append",
            text, claims=claims)
        self.actions([{
            "action_key": "append-" + item_id + "-" + str(self.store.read_revision()),
            "type": "MERGE_INTO_ITEM", "item_id": item_id,
            "observation_ids": [observation["observation_id"]],
            "modes": ["append_detail", "add_evidence"],
            "semantic_effect": effect,
        }])
        if publish:
            self.publish_item(item_id)
        return observation, span

    def change_text(self, item_id):
        for receipt in self.publisher.pending_proposals():
            if receipt["object_id"] == ReadingChanges.object_id(item_id):
                return self.publisher.read(receipt["path"]).decode("utf-8")
        path = self.publisher.resolve(ReadingChanges.object_id(item_id))
        return self.publisher.read(path).decode("utf-8")

    def text(self, item_id="cat"):
        receipt = self.changes.publish_item(item_id)
        return receipt, self.publisher.read(receipt.path).decode("utf-8")

    def test_known_v1_v2_new_unit_opened_is_not_known(self):
        _observation, _span = self.item()
        self.controls.mark_known("cat", 1, known_at=self.NOW)
        self.controls.observe_open(OpenObservation(
            "open-v1", "cat", 1, self.NOW, "obsidian_visible_open", True))
        self.append()
        receipt, visible = self.text()
        self.assertEqual((receipt.content_revision, receipt.object_id),
                         (receipt.content_revision, "changes:cat"))
        self.assertEqual(receipt.path, "变化/cat.md")
        self.assertIn("从明确已知的 v1 读到当前 v2", visible)
        self.assertIn("## 新增内容", visible)
        self.assertIn("第二条内容", visible)
        self.assertIn("打开当前条目", visible)
        self.assertIn("核对来源", visible)
        self.assertEqual(self.controls.preference("cat").known_revision, 1)
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM reader_open_observations").fetchone()[0], 1)

    def test_never_known_does_not_fabricate_v0_history(self):
        self.item(text="尚未确认的内容。")
        receipt, visible = self.text()
        self.assertIn("尚未确认已知，以下为当前保留内容", visible)
        self.assertIn("当前版本：v1", visible)
        self.assertIn("尚未确认的内容", visible)
        self.assertNotIn("v0", visible)
        self.assertNotIn("从明确已知", visible)
        self.assertEqual(self.controls.preference("cat").known_revision, 0)

    def test_same_text_claim_retraction_condition_and_contested_are_reported(self):
        self.item(
            claims=[{"condition": "小文件"}],
            text="同一文字保留。")
        self.controls.mark_known("cat", 1, known_at=self.NOW)
        correction, _ = self.extract(
            "correction", "更正内容。",
            claims=[{"condition": "仅大文件", "predicates": {"state": "corrected"}}])
        old_claim = self.store.get_item("cat")["claims"][0]["claim_id"]
        new_claim = correction["claims"][0]["claim_id"]
        self.actions([{
            "action_key": "correct-claim", "type": "MERGE_INTO_ITEM",
            "item_id": "cat", "observation_ids": [correction["observation_id"]],
            "modes": ["correct"], "semantic_effect": "state_changed",
            "claim_relations": [{
                "from_claim_id": new_claim, "to_claim_id": old_claim,
                "relation": "corrects", "reason": "后来的更正明确撤回旧说法。",
            }],
        }])
        self.publish_item("cat")
        _, visible = self.text()
        self.assertIn("同一文字保留", self.publisher.read(
            self.publisher.resolve("cat")).decode("utf-8"))
        self.assertIn("## 状态或条件更正", visible)
        self.assertIn("已撤回", visible)
        self.assertIn("适用条件：仅大文件", visible)
        correction_receipt = self.changes.publish_item("cat")
        self.assertEqual(correction_receipt.status, "unchanged")

        text_revision = self.publisher.current_revision("cat")
        display = CanonicalReadingAdapter(self.store).current_revision("cat")
        self.assertEqual(text_revision, display)

    def test_pure_provenance_and_navigation_revision_is_not_substantive(self):
        observation, _span = self.item()
        self.controls.mark_known("cat", 1, known_at=self.NOW)
        self.append(effect="none", text="仅作为额外出处的同一内容。", publish=False)
        self.publish_item("cat")
        _, visible = self.text()
        self.assertIn("## 只有整理或出处变化", visible)
        self.assertNotIn("## 新增内容", visible)
        with closing(self.store.connect()) as conn:
            self.assertGreater(
                conn.execute("SELECT content_revision FROM items WHERE item_id='cat'").fetchone()[0], 1)

    def test_relation_change_uses_visible_titles_without_internal_ids(self):
        first, first_span = self.item()
        other_observation, other_span = self.extract(
            "other", "另一条相关记录。", claims=[{"predicates": {"other": True}}])
        self.create("other", other_observation["observation_id"], title="另一条记录")
        self.publish_item("other")
        self.controls.mark_known("cat", 1, known_at=self.NOW)
        self.actions([{
            "action_key": "relate-visible", "type": "RELATE_ITEMS",
            "from_item_id": "cat", "to_item_id": "other",
            "relation": "refines_scope", "direction": "directed",
            "reason": "补充了适用范围。", "evidence_ids": [other_span],
        }])
        self.publish_item("cat")
        _, visible = self.text()
        self.assertIn("## 关系变化", visible)
        self.assertIn("另一条记录", visible)
        self.assertIn("refines\\_scope", visible)
        self.assertIn("补充了适用范围", visible)
        self.assertNotIn("other_observation", visible)
        self.assertNotIn("relation_id", visible)

    def test_manual_conflict_preserves_bytes_and_known_change_guards_draft(self):
        self.item()
        self.controls.mark_known("cat", 1, known_at=self.NOW)
        first, _ = self.text()
        path = self.publisher.root / first.path
        path.write_text(path.read_text().replace("从明确已知", "我的个人变化说明"))
        personal = path.read_bytes()
        self.append()
        conflict, _ = self.text()
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(path.read_bytes(), personal)
        proposal = next(value for value in self.publisher.pending_proposals()
                        if value["object_id"] == "changes:cat")
        self.assertEqual(self.publisher.read(proposal["backup_path"]), personal)
        self.controls.mark_known("cat", 2, known_at=self.NOW)
        self.assertFalse(self.publisher.proposal_is_current(proposal["proposal_id"]))
        self.assertEqual(self.publisher.read(proposal["backup_path"]), personal)

    def test_rename_and_unsafe_filename_are_never_fake_links(self):
        self.item()
        self.controls.mark_known("cat", 1, known_at=self.NOW)
        self.append()
        first, _ = self.text()
        self.changes.publish_index()
        original = self.publisher.root / first.path
        renamed = "变化/我的说明.md"
        (self.publisher.root / "变化").mkdir(exist_ok=True)
        original.rename(self.publisher.root / renamed)
        same, _ = self.text()
        self.assertEqual(same.path, renamed)
        special = "变化/说明#部分.md"
        (self.publisher.root / renamed).rename(self.publisher.root / special)
        # The identity remains discoverable even when a user picks a delimiter
        # that cannot be represented safely as a heading wikilink.
        self.assertTrue(self.publisher.ready("changes:cat"))
        index = self.changes._index_body()[0]
        self.assertIn("文件名暂无法安全跳转", index)
        self.assertNotIn("[[WGO/变化/说明#部分", index)

    def test_credentials_are_redacted_before_long_label_selection(self):
        secret = "https://reader:password@example.invalid/path?token=private"
        self.item(text="凭据 " + secret + " " + "长" * 1000,
                  title="凭据 " + secret + " " + "说明" * 300)
        self.controls.mark_known("cat", 1, known_at=self.NOW)
        self.append(text="新增内容 " + "长" * 1000)
        _, visible = self.text()
        self.assertNotIn("password", visible)
        self.assertNotIn("token=private", visible)
        self.assertIn("完整内容请打开当前条目", visible)
        self.assertIn("REDACTED@example.invalid", visible)
        self.assertIn("token=REDACTED", visible)
        self.assertLess(len(visible), 12000)

    def test_large_item_reports_totals_and_display_bound(self):
        self.item()
        for index in range(70):
            item, _ = self.extract(
                "large-" + str(index), "大条目内容 " + str(index),
                claims=[{"predicates": {"large": index}}])
            self.create("large-" + str(index), item["observation_id"],
                        title="大 " + str(index))
            self.publish_item("large-" + str(index))
        self.publish_item("cat")
        seen, reports = set(), []
        publish = self.changes.publish_item
        def observe(identity):
            seen.add(identity)
            return publish(identity)
        for _ in range(5):
            with patch.object(self.changes, "publish_item", side_effect=observe):
                result = self.changes.publish(limit=20)
            reports.append(result)
        self.assertIsInstance(result, ReadingChangesReceipt)
        self.assertEqual(len(seen), 71)
        self.assertEqual([r.selected for r in reports], [20, 20, 20, 11, 0])
        self.assertEqual([r.remaining for r in reports], [51, 31, 11, 0, 0])
        self.assertTrue(reports[-1].complete)
        self.assertEqual(
            {row["item_id"] for row in self.changes._selection(20)[0]}, set())
        index = self.changes._index_body()[0]
        self.assertEqual(len(self.changes._index_body()[1]), 20)
        self.assertIn("尚未确认已知，当前 v1", index)
        with self.assertRaisesRegex(ReadingError, "invalid_reading_changes_limit"):
            self.changes.publish(limit=21)

    def test_restart_unchanged_is_idempotent_without_provider_or_canonical_writes(self):
        self.item()
        self.controls.mark_known("cat", 1, known_at=self.NOW)
        self.append()
        with patch.object(self.store, "transaction",
                          side_effect=AssertionError("new canonical mutation")), \
                patch.object(ScriptedTextProvider, "generate",
                             side_effect=AssertionError("provider call")):
            first = self.changes.publish(limit=1)
            self.assertEqual(first.published, 1)
            second = self.changes.publish(limit=1)
            self.assertEqual(second.selected, 0)
            self.assertTrue(second.complete)
            restarted = ReadingChanges(
                SemanticStore(self.store.path, self.store.generation_id),
                self.publisher, self.controls)
            third = restarted.publish(limit=1)
            self.assertEqual(third.selected, 0)
        path = self.publisher.root / "变化/cat.md"
        stamp = path.stat().st_mtime_ns
        self.changes.publish_item("cat")
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_large_single_item_counts_all_changes_but_bounds_evidence_io(self):
        self.item()
        self.controls.mark_known("cat", 1, known_at=self.NOW)
        for index in range(30):
            self.append(text="独立细节 " + str(index), claims=[{"predicates": {"detail": index}}], publish=False)
        self.publish_item("cat")
        with closing(self.store.connect()) as conn:
            original = [tuple(row) for row in conn.execute("SELECT revision,snapshot_json FROM item_revisions")]
        with patch.object(self.publisher, "evidence_link", wraps=self.publisher.evidence_link) as links:
            _, visible = self.text()
        self.assertIn("本页只展示 24 项，共发现 60 项变化", visible)
        self.assertLessEqual(links.call_count, self.changes.MAX_UNITS * 3)
        with closing(self.store.connect()) as conn:
            self.assertEqual([tuple(row) for row in conn.execute("SELECT revision,snapshot_json FROM item_revisions")], original)

    def test_rotation_advances_past_a_full_page_of_failures_and_rechecks_missing_files(self):
        for index in range(24):
            self.item("entry-" + str(index), text="记录 " + str(index))
        identities = [row["item_id"] for row in self.changes._selection(20)[0]]
        publish = self.changes.publish_item
        def blocked(identity):
            if identity in identities:
                raise ReadingError("synthetic_page_unavailable")
            return publish(identity)
        # App turns reconstruct runtime/consumer; the bounded process hint still rotates.
        with patch.object(ReadingChanges, "publish_item", side_effect=blocked):
            first = self.changes.publish()
            second = ReadingChanges(self.store, self.publisher, self.controls).publish()
        self.assertEqual(first.failed, 20)
        self.assertEqual(second.published, 4)
        self.assertEqual(second.remaining, 20)
        self.assertFalse(second.complete)
        self.changes.publish()  # Repair the formerly blocked page through the ordinary owner.
        target = "entry-23"
        path = self.publisher.root / self.publisher.resolve(ReadingChanges.object_id(target))
        path.unlink()  # Disposable synthetic file: test actual missing-file recovery.
        for _ in range(3):
            self.changes.publish()
            if path.exists():
                break
        self.assertTrue(path.exists())
        self.assertTrue(self.publisher.ready(ReadingChanges.object_id(target)))
        with closing(self.store.connect()) as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='reader_changes_cursors'").fetchone())

    def test_current_conflict_can_be_adopted_while_muted_and_index_known_guard_is_exact(self):
        self.item()
        self.controls.mark_known("cat", 1, known_at=self.NOW)
        self.text()
        path = self.publisher.root / self.publisher.resolve("changes:cat")
        path.write_text(path.read_text().replace("从明确已知", "个人比较说明"))
        self.append()
        conflict, _ = self.text()
        self.controls.set_preference("cat", expected_revision=0, mute=True)
        proposal = next(row for row in self.publisher.pending_proposals() if row["proposal_id"] == conflict.proposal_id)
        self.assertTrue(self.publisher.proposal_is_current(proposal["proposal_id"]))
        self.publisher.resolve_proposal(proposal["proposal_id"], "adopt",
            expected_revision=proposal["content_revision"], expected_file_hash=proposal["file_hash"])
        self.assertTrue(self.publisher.ready("changes:cat"))
        self.changes.publish_index()
        index = self.publisher.root / self.publisher.resolve("control:changes")
        index.write_text(index.read_text().replace("这里按明确已知版本", "个人索引"))
        draft = self.changes.publish_index()
        self.assertEqual(draft.status, "conflict")
        self.assertTrue(self.publisher.proposal_is_current(draft.proposal_id))
        self.controls.mark_known("cat", 2, known_at=self.NOW)
        self.assertFalse(self.publisher.proposal_is_current(draft.proposal_id))

    def test_bad_or_stale_snapshot_fails_closed_without_overwriting_page(self):
        self.item()
        self.controls.mark_known("cat", 1, known_at=self.NOW)
        self.append()
        first, _ = self.text()
        original = self.publisher.read(first.path)
        with closing(self.store.connect()) as conn:
            conn.execute("UPDATE item_revisions SET snapshot_json='{}' "
                         "WHERE item_id='cat' AND revision=2")
            conn.commit()
        with self.assertRaisesRegex(ReadingError, "snapshot_mismatch"):
            self.changes.publish_item("cat")
        self.assertEqual(self.publisher.read(first.path), original)

    def test_api_rejects_bad_limits_generation_and_naive_runtime_shape(self):
        for limit in (0, True, 21):
            with self.assertRaisesRegex(ReadingError, "invalid_reading_changes_limit"):
                ReadingChanges(self.store, self.publisher, self.controls, limit=limit)
        with self.assertRaisesRegex(ReadingError, "generation_mismatch"):
            ReadingChanges(
                SimpleNamespace(generation_id="other"), self.publisher, self.controls)
        runtime = SimpleNamespace(
            store=self.store, publisher=self.publisher, controls=self.controls)
        self.assertIs(ReadingChanges.for_runtime(runtime).store, self.store)


if __name__ == "__main__":
    unittest.main()
