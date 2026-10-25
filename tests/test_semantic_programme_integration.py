"""Programme acceptance through actual reader turns and disposable canonical data."""
from contextlib import closing
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from ai.semantic_provider import ProviderError, TextResult
from ai.vector_index import LocalCosineIndex
from core.semantic.projections import fingerprint, patch_properties, wikilink
from core.semantic.reader_runtime import EVENT_SCHEMA, ReaderRuntime
from core.semantic.reading_changes import ReadingChanges
from core.semantic.runtime import ProviderRoles, SemanticRuntime
from core.semantic.store import SemanticStore
from tests.test_semantic_engine import FakeDecision, capture
from tests.test_semantic_reader_runtime import PipelineText
from tests.test_semantic_retrieval import FakeEmbedding
from tests import test_semantic_projections as projection_fixtures


class ProgrammeText(PipelineText):
    """Scripted editorial output and explicit correction, never a live model."""

    def __init__(self):
        super().__init__()
        self.claims = True
        self.navigation_inputs = []
        self.unknown_navigation = False
        self.correction = None
        self.independent = False

    def generate(self, prompt, **kwargs):
        if prompt.startswith("{"):
            supplied = json.loads(prompt)
            if "snapshot" in supplied:
                # The base paging fixture keeps old prose verbatim. A genuine
                # correction also updates the retained block's typed status.
                self.reader_calls.append((prompt, kwargs["request_id"]))
                result = projection_fixtures.ProjectionTests.paged_response(prompt)
                units = {unit["id"]: unit for unit in supplied["snapshot"]["units"]}
                for block in result["blocks"]:
                    block["unit_statuses"] = {identity: units[identity]["status"]
                        for identity in block["unit_ids"] if units[identity]["status"] != "current"}
                    if block["unit_statuses"]:
                        block["role"] = "counterevidence"
                return TextResult(json.dumps(result, ensure_ascii=False), self.route)
        if prompt.startswith("Induce useful navigation"):
            state = json.loads(prompt.rsplit("\n", 1)[-1])
            self.navigation_inputs.append(state)
            if self.unknown_navigation:
                self.calls.append(kwargs["request_id"])
                raise ProviderError("synthetic_navigation_unknown", outcome_unknown=True)
            response = json.loads(super().generate(prompt, **kwargs).text)
            for group in response["groups"]:
                group["title"] = "日常场景"
            return TextResult(json.dumps(response, ensure_ascii=False), self.route)
        if (self.correction or self.independent) and not prompt.startswith("{"):
            supplied = json.loads(prompt.rsplit("\n", 1)[-1])
            if isinstance(supplied, dict) and supplied.get("new_item_ids") and supplied.get("observations"):
                if self.independent:
                    self.calls.append(kwargs["request_id"])
                    actions = [{"action_key": kwargs["request_id"] + ":" + str(index), "type": "CREATE_ITEM",
                        "item_id": supplied["new_item_ids"][index], "kind": "story", "title": value["title"],
                        "identity_scope": {"scene": value["title"]}, "observation_ids": [value["observation_id"]]}
                        for index, value in enumerate(supplied["observations"])]
                    return TextResult(json.dumps({"actions": actions, "novelty": {
                        "considered_item_ids": list(supplied["items"]), "reason": "Independent supplied scenes"}},
                        ensure_ascii=False), self.route)
                item_id, old_claim = self.correction
                observation = supplied["observations"][0]
                if item_id in supplied["items"] and "撤回" in observation["title"]:
                    self.calls.append(kwargs["request_id"])
                    action = {"action_key": kwargs["request_id"] + ":correct", "type": "MERGE_INTO_ITEM",
                        "item_id": item_id, "observation_ids": [observation["observation_id"]],
                        "modes": ["correct"], "semantic_effect": "state_changed", "claim_relations": [{
                            "from_claim_id": observation["claims"][0]["claim_id"], "to_claim_id": old_claim,
                            "relation": "corrects_or_retracts", "reason": "明确撤回之前的报告，保留新的观察。"}]}
                    return TextResult(json.dumps({"actions": [action], "novelty": {
                        "considered_item_ids": list(supplied["items"]), "reason": "Exact supplied claim correction"}},
                        ensure_ascii=False), self.route)
        return super().generate(prompt, **kwargs)


class ProgrammeIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = SemanticStore(self.root / "canonical.sqlite", "programme-generation")
        self.text, self.decision, self.embedding = ProgrammeText(), FakeDecision(), FakeEmbedding()
        self.restart()

    def restart(self):
        self.runtime = SemanticRuntime(self.store,
            ProviderRoles(self.text, self.decision, self.embedding, LocalCosineIndex()), self.root / "WGO",
            config={"daily_digest_timezone": "UTC"})
        self.reader = ReaderRuntime(self.runtime, device_id="synthetic-device", live=False)
        self.publisher, self.controls = self.runtime.publisher, self.runtime.controls

    def ingest(self, batch, texts, *, independent=False):
        before = {item["item_id"] for item in self.store.list_items()}
        old = self.decision.possible
        self.decision.possible = 0 if independent else old
        self.text.independent = independent
        try:
            capture(self.store, batch, texts)
            self.runtime.process(batch)
        finally:
            self.decision.possible = old
            self.text.independent = False
        return [item for item in self.store.list_items() if item["item_id"] not in before]

    def tick(self):
        return self.reader.poll(maintain_derived=True, maintain_views=True)

    def text_for(self, object_id):
        path = self.publisher.resolve(object_id)
        self.assertIsNotNone(path, object_id)
        return self.publisher.read(path).decode()

    def action(self, kind, *, item_id=None):
        rows = self.runtime.ledger.all("SELECT * FROM reader_actions WHERE kind=? AND status='registered' ORDER BY rowid DESC", (kind,))
        return next(row for row in rows if item_id is None or row["item_id"] == item_id
                    or (json.loads(row["value_json"]) or {}).get("item_id") == item_id)

    def check(self, management_id, actions):
        path = self.publisher.root / self.publisher.resolve(management_id)
        text = path.read_text()
        for action in actions:
            text, count = re.subn(r"(?m)^- \[ \](.*<!-- wgo-action:" + action["action_id"] + r" -->)$", r"- [x]\1", text)
            self.assertEqual(count, 1)
        path.write_text(text)

    def counts(self):
        return len(self.text.calls), len(self.text.reader_calls), len(self.decision.calls), len(self.embedding.calls)

    def assert_idle_restart(self):
        self.tick()
        counts = self.counts()
        self.restart()
        for _ in range(2):
            self.assertEqual(self.tick()["views"]["status"], "unchanged")
        self.assertEqual(self.counts(), counts)

    def test_new_catalog_to_published_view_user_batch_and_later_model_respects_overrides(self):
        items = self.ingest("initial-scenes", ["窗边的猫睡在纸箱里。", "散步回来带了一束花。"])
        self.assertEqual(len(items), 2)
        first = self.tick()
        self.assertEqual(first["views"]["status"], "complete")
        with closing(self.store.connect()) as conn:
            view_id = conn.execute("SELECT view_id FROM views").fetchone()[0]
        view_object = "view:" + view_id
        self.assertTrue(self.publisher.ready(view_object))
        self.assertIn("日常场景", self.text_for("control:home"))
        item_id = items[0]["item_id"]
        path = self.publisher.root / self.publisher.resolve(view_object)
        definition = "只按家里的日常场景整理，保留明确排除的记录。"
        path.write_text(patch_properties(path.read_text(), {"主题说明": definition}, machine_only=False))
        self.tick()  # Registers explicit adoption, rather than treating an edit as permission.
        actions = [self.action("view_definition"), self.action("view_pin", item_id=item_id),
                   self.action("view_exclude", item_id=item_id)]
        self.check(self.runtime.view_controls.MANAGEMENT_ID, actions)
        transactions = []
        original = self.store._update_view_controls
        def observe(conn, *args, **kwargs):
            self.assertTrue(conn.in_transaction)
            conn.set_trace_callback(transactions.append)
            return original(conn, *args, **kwargs)
        with patch.object(self.store, "_update_view_controls", side_effect=observe):
            applied = self.tick()
        self.assertEqual(applied["view_actions"], 3)
        self.assertTrue(any("UPDATE reader_actions" in query for query in transactions))
        self.assertIn("COMMIT", transactions)
        view = self.store.get_view(view_id)
        self.assertEqual(view["definition"], definition)
        with closing(self.store.connect()) as conn:
            member = dict(conn.execute("SELECT * FROM view_memberships WHERE view_id=? AND item_id=?", (view_id, item_id)).fetchone())
            statuses = [conn.execute("SELECT status FROM reader_actions WHERE action_id=?", (action["action_id"],)).fetchone()[0]
                        for action in actions]
        self.assertEqual(statuses, ["applied"] * 3)
        self.assertEqual((member["pinned"], member["excluded"]), (1, 1))
        before = len(self.text.navigation_inputs)
        added = self.ingest("new-independent-scene", ["后来添了一张小桌子。"], independent=True)
        self.assertEqual(len(added), 1)
        self.assertEqual(self.tick()["views"]["status"], "complete")
        self.assertGreater(len(self.text.navigation_inputs), before)
        supplied = self.text.navigation_inputs[-1]
        self.assertEqual(supplied["views"][view_id]["definition"], definition)
        bound_override = next(value for value in supplied["overrides"]
                              if value["view_id"] == view_id and value["item_id"] == item_id)
        self.assertEqual((bound_override["pinned"], bound_override["excluded"]), (1, 1))
        self.assertEqual(self.store.get_view(view_id)["definition"], definition)
        with closing(self.store.connect()) as conn:
            self.assertEqual(tuple(conn.execute("SELECT pinned,excluded FROM view_memberships WHERE view_id=? AND item_id=?", (view_id, item_id)).fetchone()), (1, 1))
        self.assertNotIn(wikilink("WGO/" + self.publisher.resolve(item_id), items[0]["title"]), self.text_for(view_object))
        self.assert_idle_restart()

    def test_known_and_visible_open_stay_distinct_through_correction_changes_and_home(self):
        items = self.ingest("reports", ["猫已经回到窗边。", "花放在饭桌上。"])
        self.tick()
        known, opened = items[0], items[1]
        # Live membership publication is itself a canonical navigation revision.
        # Bind controls to the version actually displayed after that first turn.
        baseline = self.publisher.current_revision(known["item_id"])
        opened_revision = self.publisher.current_revision(opened["item_id"])
        self.check(self.controls.MANAGEMENT_ID, [self.action("known", item_id=known["item_id"])])
        path = self.publisher.resolve(opened["item_id"])
        event_id = str(uuid.uuid4())
        event = {"schema": EVENT_SCHEMA, "event_id": event_id, "generation_id": self.store.generation_id,
            "item_id": opened["item_id"], "content_revision": opened_revision, "path": path,
            "file_sha256": fingerprint(self.publisher.read(path)), "observed_at": time.time(),
            "origin": "obsidian_visible_open", "visible": True}
        inbox = self.publisher._safe_path(".wgo/reader-events/" + event_id + ".json")
        inbox.write_text(json.dumps(event)); inbox.chmod(0o600)
        accepted = self.tick()
        self.assertEqual((accepted["actions"], accepted["open_events"]), (1, 1))
        self.assertEqual(self.controls.preference(known["item_id"]).known_revision, baseline)
        self.assertEqual(self.controls.preference(opened["item_id"]).known_revision, 0)
        old_claim = self.store.get_item(known["item_id"])["claims"][0]["claim_id"]
        self.text.correction = (known["item_id"], old_claim)
        self.ingest("retraction", ["撤回此前报告 failure：新的观察与旧结论相反。"])
        current = self.store.get_item(known["item_id"])["content_revision"]
        self.assertEqual(current, baseline + 1)
        self.tick()
        visible = self.text_for(ReadingChanges.object_id(known["item_id"]))
        self.assertIn("从明确已知的 v" + str(baseline) + " 读到当前 v" + str(current), visible)
        self.assertIn("已撤回", visible)
        self.assertIn("## 状态或条件更正", visible)
        self.assertIn("尚未确认已知", self.text_for(ReadingChanges.object_id(opened["item_id"])))
        home = self.text_for("control:home")
        self.assertIn("明确已知至 v" + str(baseline), home)
        self.assertIn(wikilink("WGO/" + self.publisher.resolve(ReadingChanges.object_id(known["item_id"])), "查看具体变化"), home)
        self.assertEqual(self.controls.preference(known["item_id"]).known_revision, baseline)
        self.assert_idle_restart()

    def test_unknown_view_work_and_personal_conflict_do_not_stop_other_derived_pages(self):
        first = self.ingest("first", ["窗边第一段记录。"])[0]
        self.tick()
        self.text.unknown_navigation = True
        second = self.ingest("second", ["另一段独立日常。"], independent=True)[0]
        result = self.tick()
        self.assertEqual(result["views"]["status"], "blocked")
        self.assertTrue(self.publisher.ready(second["item_id"], 1))
        self.assertTrue(self.publisher.ready(ReadingChanges.object_id(second["item_id"])))
        navigation_calls = len(self.text.navigation_inputs)
        personal_path = self.publisher.root / self.publisher.resolve(first["item_id"])
        personal_path.write_text(personal_path.read_text().replace("<!-- wgo:begin body -->", "<!-- wgo:begin body -->\n个人安排"))
        personal = personal_path.read_bytes()
        third = self.ingest("third", ["又新增了一个独立场景。"], independent=True)[0]
        self.restart()
        result = self.tick()
        self.assertEqual(result["views"]["status"], "blocked")
        self.assertEqual(len(self.text.navigation_inputs), navigation_calls)
        self.assertTrue(self.publisher.ready(third["item_id"], 1))
        self.assertGreaterEqual(result["derived"]["projection"]["conflicts"], 1)
        self.assertEqual(personal_path.read_bytes(), personal)
        self.assertIn("目录整理：受阻", self.text_for("control:reader-status"))


if __name__ == "__main__":
    unittest.main()
