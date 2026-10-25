"""Live navigation uses fake providers and private disposable SQLite only."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai.semantic_provider import ProviderError
from core.semantic.contracts import SemanticStoreError, digest, dumps
from core.semantic.engine import SemanticEngine
from core.semantic.jobs import JobStore
from core.semantic.store import SemanticStore
from core.semantic.verification import RebuildVerifier
from tests.test_semantic_engine import FakeText, FakeDecision, capture


class LiveViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SemanticStore(Path(self.temp.name).resolve() / "knowledge.sqlite", "synthetic-generation")
        self.text, self.decision = FakeText(), FakeDecision()
        self.engine = SemanticEngine(self.store, self.text, self.decision)

    def seed(self, count=1):
        capture(self.store, "seed", ["Independent scene " + str(index) for index in range(count)])
        self.engine.process_batch("seed")

    def counts(self):
        return len(self.text.calls), len(self.decision.calls)

    def live_errors(self):
        report = RebuildVerifier(self.store.path, {"generation_id": self.store.generation_id}).verify()
        return {issue.code for issue in report.issues if issue.code.startswith("live_view_") or issue.code == "accepted_action_pending"}

    def done(self):
        for _ in range(20):
            result = self.engine.maintain_views()
            if result["status"] != "pending":
                return result
        self.fail("live maintenance did not settle")

    def test_one_page_per_turn_restart_idle_and_metadata_only(self):
        self.seed(20)
        # Existing canonical writes can read full Items. Orchestration must not
        # use list_items or get_item to construct a prompt/catalog.
        with patch.object(self.store, "list_items", side_effect=AssertionError("full catalog")), \
             patch.object(self.store, "get_item", side_effect=AssertionError("full body")):
            result = self.engine.maintain_views()
            self.assertEqual((result["status"], result["pages_processed"]), ("pending", 1))
            self.engine = SemanticEngine(self.store, self.text, self.decision)
            result = self.engine.maintain_views()
            self.assertEqual((result["status"], result["pages_processed"]), ("complete", 1))
            counts = self.counts()
            for _ in range(3):
                self.assertEqual(self.engine.maintain_views()["status"], "unchanged")
            self.assertEqual(self.counts(), counts)
        self.assertEqual(self.live_errors(), set())
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM view_memberships").fetchone()[0], 20)

    def test_membership_revision_and_unrelated_global_revision_do_not_self_trigger(self):
        self.seed()
        self.assertEqual(self.done()["status"], "complete")
        counts = self.counts()
        with self.store.transaction() as db:
            db.execute("UPDATE generation_manifest SET revision=revision+1")
        self.assertEqual(self.engine.maintain_views()["status"], "unchanged")
        self.assertEqual(self.counts(), counts)

    def test_new_item_arriving_during_frozen_run_enters_next_run(self):
        self.seed(20)
        self.assertEqual(self.engine.maintain_views()["status"], "pending")
        capture(self.store, "later", ["Another independent scene"])
        # Force the existing fake resolver to CREATE rather than merge.
        self.engine.decision_provider.possible = 0
        self.engine.process_batch("later")
        self.assertEqual(self.engine.maintain_views()["status"], "pending")
        self.assertEqual(self.done()["status"], "complete")
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM view_memberships").fetchone()[0], 21)
        self.assertEqual(self.live_errors(), set())

    def test_defer_replays_until_actual_dependency_change(self):
        self.seed()
        self.decision.defer = True
        self.assertEqual(self.done()["status"], "deferred")
        counts = self.counts()
        self.assertEqual(self.engine.maintain_views()["status"], "deferred")
        with self.store.transaction() as db:
            db.execute("UPDATE generation_manifest SET revision=revision+1")
        self.assertEqual(self.engine.maintain_views()["status"], "deferred")
        self.assertEqual(self.counts(), counts)
        self.store.create_view("user", "User View", "A new user definition")
        self.decision.defer = False
        self.assertEqual(self.done()["status"], "complete")
        self.assertGreater(self.counts()[0], counts[0])
        self.assertEqual(self.live_errors(), set())

    def test_unknown_blocks_changed_dependencies_and_restart(self):
        self.seed()
        class Unknown(FakeText):
            def generate(provider, prompt, **kwargs):
                provider.calls.append(kwargs["request_id"])
                raise ProviderError("synthetic_sent_unknown", retryable=True, outcome_unknown=True)
        unknown = Unknown()
        self.engine.text_provider = unknown
        with self.assertRaises(ProviderError):
            self.engine.maintain_views()
        self.store.create_view("user", "User View", "Changed dependency")
        self.engine = SemanticEngine(self.store, unknown, self.decision)
        self.assertEqual(self.engine.maintain_views()["status"], "blocked")
        self.assertEqual(len(unknown.calls), 1)
        self.assertIn("live_view_provider_pending", self.live_errors())

    def test_stale_before_commit_keeps_only_model_artifacts(self):
        self.seed()
        original = self.store.commit_live_view_plan
        changed = []
        def race(*args):
            if not changed:
                changed.append(True)
                self.store.create_view("user", "User View", "Changed dependency")
            return original(*args)
        with patch.object(self.store, "commit_live_view_plan", side_effect=race):
            self.assertEqual(self.engine.maintain_views()["status"], "pending")
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE json_extract(versions_json,'$.contract')='semantic-live-view-members-v2'").fetchone()[0], 0)
        self.assertNotIn("accepted_action_pending", self.live_errors())
        self.assertEqual(self.done()["status"], "complete")
        self.assertEqual(self.live_errors(), set())

    def test_transaction_failure_rolls_back_view_program_artifact_and_action(self):
        self.seed()
        with patch.object(self.store, "_apply_proposal", side_effect=RuntimeError("synthetic_commit_failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic_commit_failure"):
                self.engine.maintain_views()
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM views").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE json_extract(versions_json,'$.contract')='semantic-live-view-members-v2'").fetchone()[0], 0)
        counts = self.counts()
        self.assertEqual(self.done()["status"], "complete")
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.live_errors(), set())

    def test_commit_crash_replays_same_receipt_once_before_dependency_check(self):
        self.seed()
        original = self.store.commit_live_view_plan
        receipts = []
        def crash(*args):
            result = original(*args)
            receipts.append(result["receipt"])
            raise RuntimeError("synthetic_return_crash")
        with patch.object(self.store, "commit_live_view_plan", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "synthetic_return_crash"):
                self.engine.maintain_views()
        counts = self.counts()
        self.engine = SemanticEngine(self.store, self.text, self.decision)
        result = self.done()
        self.assertEqual(result["pages"][0]["receipt"], receipts[0])
        self.assertEqual(self.counts(), counts)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM canonical_transactions t JOIN jobs j ON j.job_id=t.job_id WHERE json_extract(j.versions_json,'$.contract')='semantic-live-view-members-v2'").fetchone()[0], 1)
        self.assertEqual(self.live_errors(), set())

    def test_user_controls_exact_cas_preserves_origin_and_other_state(self):
        self.seed()
        self.done()
        with closing(self.store.connect()) as db:
            before = dict(db.execute("SELECT * FROM view_memberships").fetchone())
            item = dict(db.execute("SELECT item_id,content_revision FROM items").fetchone())
            original_revisions = db.execute("SELECT count(*) FROM item_revisions").fetchone()[0]
        view = self.store.get_view(before["view_id"])
        result = self.store.update_view_controls(view["view_id"], view["definition_revision"], view["override_revision"],
            definition="Exact user definition", memberships={item["item_id"]: {"pinned": True, "excluded": True}},
            expected_item_revisions={item["item_id"]: item["content_revision"]})
        self.assertEqual(result["definition_revision"], view["definition_revision"] + 1)
        self.assertEqual(result["override_revision"], view["override_revision"] + 1)
        member = result["memberships"][0]
        self.assertEqual({key: member[key] for key in ("origin", "placement", "reason")}, {key: before[key] for key in ("origin", "placement", "reason")})
        self.assertTrue(member["pinned"] and member["excluded"])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM item_revisions").fetchone()[0], original_revisions)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_stale_read_set"):
            self.store.update_view_controls(view["view_id"], view["definition_revision"], view["override_revision"], definition="Rejected")
        self.assertEqual(self.done()["status"], "complete")
        after = self.store.get_view(view["view_id"])
        self.assertEqual(after["definition"], "Exact user definition")
        with closing(self.store.connect()) as db:
            final = dict(db.execute("SELECT * FROM view_memberships").fetchone())
        self.assertEqual(final, member)

    def test_control_invalid_batch_is_atomic_and_same_connection_rollback(self):
        self.seed(); self.done()
        with closing(self.store.connect()) as db:
            member = dict(db.execute("SELECT * FROM view_memberships").fetchone())
            revision = db.execute("SELECT content_revision FROM items").fetchone()[0]
        view = self.store.get_view(member["view_id"])
        with self.assertRaisesRegex(SemanticStoreError, "semantic_view_control_invalid"):
            self.store.update_view_controls(view["view_id"], 1, 0, definition="Not committed", memberships={member["item_id"]: {"pinned": 1, "excluded": False}}, expected_item_revisions={member["item_id"]: revision})
        self.assertEqual(self.store.get_view(view["view_id"]), view)
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store.transaction() as db:
                self.store._update_view_controls(db, view["view_id"], 1, 0, definition="Not committed")
                raise RuntimeError("rollback")
        self.assertEqual(self.store.get_view(view["view_id"]), view)

    def test_unassigned_has_no_canonical_noop_and_verifier_checks_coverage(self):
        self.seed()
        original = self.text.generate
        from ai.semantic_provider import TextResult
        def unassigned(prompt, **kwargs):
            if prompt.startswith("Induce useful navigation"):
                state = json.loads(prompt.rsplit("\n", 1)[-1])
                self.text.calls.append(kwargs["request_id"])
                return TextResult(json.dumps({"groups": [], "unassigned": [{"item_id": iid, "reason": "No grounded grouping"} for iid in state["items"]]}), self.text.route)
            return original(prompt, **kwargs)
        with patch.object(self.text, "generate", side_effect=unassigned):
            self.assertEqual(self.done()["status"], "complete")
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM canonical_transactions WHERE actions_json='[]'").fetchone()[0], 0)
        self.assertEqual(self.live_errors(), set())
        with self.store.transaction() as db:
            row = db.execute("SELECT a.* FROM accepted_job_artifacts a JOIN jobs j USING(job_id) WHERE kind='live_view_induction'").fetchone()
            value = json.loads(row["artifact_json"]); value["unassigned"] = []
            db.execute("UPDATE accepted_job_artifacts SET artifact_json=?,artifact_hash=? WHERE job_id=?", (dumps(value), digest(value), row["job_id"]))
        self.assertIn("live_view_outcome_invalid", self.live_errors())

    def test_verifier_rejects_rehashed_program_source_binding(self):
        self.seed(); self.done()
        self.assertEqual(self.live_errors(), set())
        with self.store.transaction() as db:
            row = db.execute("SELECT a.* FROM accepted_job_artifacts a JOIN jobs j USING(job_id) WHERE j.kind='propose' AND json_extract(j.versions_json,'$.contract')='semantic-live-view-members-v2'").fetchone()
            value = json.loads(row["artifact_json"]); value["source_choice_job_id"] = "fabricated"
            db.execute("UPDATE accepted_job_artifacts SET artifact_json=?,artifact_hash=? WHERE job_id=?", (dumps(value), digest(value), row["job_id"]))
        self.assertIn("live_view_program_projection_invalid", self.live_errors())

    def test_provider_redaction_does_not_rewrite_exact_user_definition(self):
        self.seed()
        secret = "https://example.test/?token=private-secret"
        self.store.create_view("user", "User View", secret)
        prompts = []
        original = self.text.generate
        def observe(prompt, **kwargs):
            prompts.append(prompt)
            return original(prompt, **kwargs)
        with patch.object(self.text, "generate", side_effect=observe):
            self.done()
        self.assertTrue(prompts)
        self.assertNotIn("private-secret", "".join(prompts))
        self.assertEqual(self.store.get_view("user")["definition"], secret)

    def test_overlarge_view_catalog_fails_closed_before_provider(self):
        self.seed()
        self.store.create_view("large", "Large", "x" * 60001)
        counts = self.counts()
        with self.assertRaisesRegex(SemanticStoreError, "semantic_view_context_too_large"):
            self.engine.maintain_views()
        self.assertEqual(self.counts(), counts)

    def test_unknown_choice_and_unexpired_running_block_before_new_context(self):
        self.seed()
        class UnknownChoice(FakeDecision):
            def decide(provider, request, **kwargs):
                provider.calls.append(kwargs["request_id"])
                raise ProviderError("synthetic_choice_unknown", retryable=True, outcome_unknown=True)
        unknown = UnknownChoice()
        self.engine.decision_provider = unknown
        with self.assertRaises(ProviderError):
            self.engine.maintain_views()
        self.store.create_view("new", "New", "Dependency changed")
        counts = len(self.text.calls), len(unknown.calls)
        self.assertEqual(self.engine.maintain_views()["status"], "blocked")
        self.assertEqual((len(self.text.calls), len(unknown.calls)), counts)

    def test_running_lease_blocks_and_expiry_reclaims_exact_job(self):
        self.seed()
        from dataclasses import asdict
        from core.semantic.live_views import CONTRACT
        ids = self.store.live_view_catalog()["item_ids"]
        state = self.store.live_view_page(ids)
        jobs = JobStore(self.store)
        job = jobs.ensure("live_view_induction", {"state": state}, {"contract": CONTRACT,
            "text_route": asdict(self.text.route), "decision_route": asdict(self.decision.route)})
        jobs.claim(job["job_id"], "old-worker", lease_seconds=10)
        counts = self.counts()
        self.assertEqual(self.engine.maintain_views()["status"], "blocked")
        self.assertEqual(self.counts(), counts)
        now = self.store.now_func()
        self.store.now_func = lambda: now + 11
        self.assertEqual(self.done()["status"], "complete")
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT attempt FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0], 2)
        self.assertEqual(self.live_errors(), set())

    def test_global_revision_race_refreshes_cas_without_provider_replan(self):
        self.seed()
        original = self.store.commit_live_view_plan
        changed = []
        def race(*args):
            if not changed:
                changed.append(True)
                with self.store.transaction() as db:
                    db.execute("UPDATE generation_manifest SET revision=revision+1")
            return original(*args)
        before = self.counts()
        with patch.object(self.store, "commit_live_view_plan", side_effect=race):
            self.assertEqual(self.engine.maintain_views()["status"], "complete")
        self.assertEqual(self.counts(), (before[0] + 1, before[1] + 1))
        self.assertEqual(self.live_errors(), set())

    def test_retired_target_page_has_explicit_historical_coverage_without_provider(self):
        self.seed(20)
        first = self.engine.maintain_views()
        self.assertEqual(first["status"], "pending")
        with closing(self.store.connect()) as db:
            run = json.loads(db.execute("SELECT a.artifact_json FROM jobs j JOIN accepted_job_artifacts a USING(job_id) WHERE j.kind='live_view_run'").fetchone()[0])
        retired = run["item_ids"][16:]
        target = run["item_ids"][0]
        all_ids = [target, *retired]
        read = {"store_revision": self.store.read_revision(),
                "items": {iid: self.store.get_item(iid)["content_revision"] for iid in all_ids},
                "views": {}}
        with closing(self.store.connect()) as db:
            read["views"] = {row["view_id"]: {key: row[key] for key in ("definition_revision", "override_revision")}
                             for row in db.execute("SELECT * FROM views")}
        action = {"type": "MERGE_ITEMS", "action_key": "synthetic-retire", "item_ids": all_ids,
                  "target_item_id": target, "reason": "Explicit synthetic merge"}
        state = {"items": {iid: self.store.get_item(iid) for iid in all_ids}, "observations": [], "read_set": read}
        from tests.test_semantic_verification import accept_fixture
        proposal = accept_fixture(self.store, {"actions": [action], "read_set": read}, "propose", inputs={"state": state})
        self.store.apply_proposal(proposal, [action], read_set=read)
        counts = self.counts()
        result = self.engine.maintain_views()
        self.assertEqual(result["pages_processed"], 1)
        self.assertEqual(result["pages"][0]["retired_item_ids"], sorted(retired))
        self.assertIsNone(result["pages"][0]["plan_job_id"])
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.done()["status"], "complete")
        self.assertEqual(self.live_errors(), set())

    def test_control_metadata_only_return_and_missing_member_rollback(self):
        self.seed(); self.done()
        with closing(self.store.connect()) as db:
            member = dict(db.execute("SELECT * FROM view_memberships").fetchone())
        view = self.store.get_view(member["view_id"])
        result = self.store.update_view_controls(view["view_id"], 1, 0, include_memberships=False)
        self.assertEqual(result, {key: view[key] for key in ("view_id", "definition_revision", "override_revision", "origin")})
        with self.assertRaisesRegex(SemanticStoreError, "semantic_stale_read_set"):
            self.store.update_view_controls(view["view_id"], 1, 0, definition="Uncommitted",
                memberships={"unknown": {"pinned": True, "excluded": False}}, expected_item_revisions={"unknown": 1})
        self.assertEqual(self.store.get_view(view["view_id"]), view)

    def test_idle_fast_path_never_hydrates_catalog(self):
        self.seed(); self.done()
        counts = self.counts()
        with patch.object(self.store, "live_view_catalog", side_effect=AssertionError("unneeded metadata scan")):
            self.assertEqual(self.engine.maintain_views()["status"], "unchanged")
        self.assertEqual(self.counts(), counts)

    def test_verifier_rejects_complete_checkpoint_after_unmaintained_dependency_change(self):
        self.seed(); self.done()
        self.store.create_view("later-view", "Later", "Unmaintained user definition")
        self.assertIn("live_view_dependencies_pending", self.live_errors())
        self.done()
        self.assertEqual(self.live_errors(), set())

    def test_invalid_page_budget_never_dispatches(self):
        counts = self.counts()
        for value in (0, 2, True, 1.0):
            with self.assertRaisesRegex(ValueError, "semantic_view_page_budget_invalid"):
                self.engine.maintain_views(max_pages=value)
        self.assertEqual(self.counts(), counts)

    def test_source_choice_frozen_route_tamper_is_rejected_independently(self):
        self.seed()
        page = self.done()["pages"][0]
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (page["choice_job_id"],)).fetchone()
            versions = json.loads(row["versions_json"])
            versions["route"]["model"] = "fabricated-route-model"
            key = digest({"generation_id": self.store.generation_id, "kind": row["kind"],
                          "inputs": json.loads(row["inputs_json"]), "versions": versions})
            db.execute("UPDATE jobs SET versions_json=?,job_key=? WHERE job_id=?", (dumps(versions), key, row["job_id"]))
        self.assertIn("live_view_program_projection_invalid", self.live_errors())
        fresh = SemanticStore(self.store.path, self.store.generation_id)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_live_view_binding_invalid"):
            fresh.commit_live_view_plan(page["plan_job_id"], page["choice_job_id"])

    def test_receipt_fingerprint_tamper_cannot_replay_or_hide_in_outcome(self):
        self.seed()
        page = self.done()["pages"][0]
        fabricated = {**page["receipt"], "input_fingerprint": "fabricated"}
        with self.store.transaction() as db:
            db.execute("UPDATE decision_log SET receipt_json=? WHERE job_id=?", (dumps(fabricated), page["receipt"]["job_id"]))
            row = db.execute("SELECT a.* FROM accepted_job_artifacts a JOIN jobs j USING(job_id) WHERE j.kind='live_view_outcome'").fetchone()
            outcome = json.loads(row["artifact_json"])
            outcome["receipt"] = fabricated
            db.execute("UPDATE accepted_job_artifacts SET artifact_json=?,artifact_hash=? WHERE job_id=?",
                       (dumps(outcome), digest(outcome), row["job_id"]))
        fresh = SemanticStore(self.store.path, self.store.generation_id)
        with self.assertRaisesRegex(SemanticStoreError, "semantic_canonical_receipt_invalid"):
            fresh.commit_live_view_plan(page["plan_job_id"], page["choice_job_id"])
        self.assertIn("live_view_program_projection_invalid", self.live_errors())
        with closing(self.store.connect()) as db:
            self.assertEqual(json.loads(db.execute("SELECT receipt_json FROM action_receipts WHERE job_id=?",
                (page["receipt"]["job_id"],)).fetchone()[0]), page["receipt"])

    def test_metadata_only_controls_do_not_hydrate_large_text_or_memberships(self):
        self.seed(); self.done()
        with self.store.transaction() as db:
            view_id = db.execute("SELECT view_id FROM views").fetchone()[0]
            db.execute("UPDATE views SET title=?,definition=? WHERE view_id=?", ("t" * 2000000, "d" * 2000000, view_id))
        statements = []
        with self.store.transaction() as db:
            db.set_trace_callback(statements.append)
            result = self.store._update_view_controls(db, view_id, 1, 0, include_memberships=False)
        self.assertEqual(set(result), {"view_id", "definition_revision", "override_revision", "origin"})
        self.assertLess(len(dumps(result)), 300)
        selects = [query.lower() for query in statements if query.lower().startswith("select")]
        self.assertTrue(selects)
        self.assertFalse(any("select *" in query or "title" in query or "select definition," in query for query in selects))
        self.assertFalse(any("from view_memberships" in query for query in selects))
