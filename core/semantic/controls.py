"""Registered Obsidian control inputs and version-specific reading state."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
import json
import re
from typing import Callable
import uuid

from core.url_safety import redact_urls_in_text
from .projections import (
    ProjectionPublisher, ReadingError, ReadingLedger, fingerprint, json_text,
    patch_properties, property_present, property_value, region_body, wikilink,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reader_preferences (
 generation_id TEXT NOT NULL,item_id TEXT NOT NULL,revision INTEGER NOT NULL,
 follow INTEGER NOT NULL,mute INTEGER NOT NULL,wait_condition TEXT NOT NULL,
 PRIMARY KEY(generation_id,item_id));
CREATE TABLE IF NOT EXISTS reader_known (
 generation_id TEXT NOT NULL,item_id TEXT NOT NULL,content_revision INTEGER NOT NULL,
 known_at TEXT NOT NULL,PRIMARY KEY(generation_id,item_id,content_revision));
CREATE TABLE IF NOT EXISTS reader_open_observations (
 observation_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,item_id TEXT NOT NULL,
 content_revision INTEGER NOT NULL,observed_at TEXT NOT NULL,origin TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reader_property_maps (
 generation_id TEXT NOT NULL,object_id TEXT NOT NULL,role TEXT NOT NULL,property_key TEXT NOT NULL,
 PRIMARY KEY(generation_id,object_id,role),UNIQUE(generation_id,object_id,property_key));
CREATE TABLE IF NOT EXISTS reader_actions (
 action_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,kind TEXT NOT NULL,item_id TEXT NOT NULL,
 content_revision INTEGER NOT NULL,preference_revision INTEGER NOT NULL,file_hash TEXT NOT NULL,
 value_json TEXT NOT NULL,proposal_id TEXT NOT NULL,label TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reader_queries (
 generation_id TEXT NOT NULL,query_revision INTEGER NOT NULL,query_hash TEXT NOT NULL,
 query_text TEXT NOT NULL,status TEXT NOT NULL,answer_text TEXT NOT NULL,hits_json TEXT NOT NULL,
 request_id TEXT NOT NULL,PRIMARY KEY(generation_id,query_revision));
"""


@dataclass(frozen=True)
class ReaderPreference:
    item_id: str
    revision: int = 0
    follow: bool = False
    mute: bool = False
    wait: str = ""
    known_revision: int = 0


@dataclass(frozen=True)
class OpenObservation:
    observation_id: str
    item_id: str
    content_revision: int
    observed_at: str
    origin: str
    visible: bool


@dataclass(frozen=True)
class SearchRequest:
    query: str
    revision: int
    request_id: str


@dataclass(frozen=True)
class QueryHit:
    item_id: str
    content_revision: int
    label: str
    block: str = ""


@dataclass(frozen=True)
class SearchAnswer:
    text: str
    hits: tuple[QueryHit, ...]


@dataclass(frozen=True)
class ControlReceipt:
    id: str
    status: str
    revision: int = 0
    code: str = ""


class CoreSearchAdapter:
    """Turn the real HybridRetriever result into grounded Obsidian output.

    This path is usable without another model call: supplied result excerpts
    and explicit coverage are the answer. Unassigned source evidence stays
    visible without inventing an Item identity or a link.
    """

    def __init__(self, store, retriever):
        self.store, self.retriever = store, retriever

    def __call__(self, request: SearchRequest) -> SearchAnswer:
        result = self.retriever.search(request.query)
        linked, paragraphs = {}, []
        for hit in result.hits:
            item_ids = [hit.item_id] if hit.item_id else ([hit.object_id] if hit.kind == "item" else [])
            if not item_ids:
                with closing(self.store.connect()) as conn:
                    if hit.kind == "observation":
                        item_ids = [r[0] for r in conn.execute("SELECT item_id FROM item_observations WHERE observation_id=?", (hit.object_id,))]
                    elif hit.kind == "source":
                        item_ids = [r[0] for r in conn.execute("SELECT DISTINCT io.item_id FROM source_spans s JOIN observation_evidence e USING(span_id) "
                            "JOIN item_observations io USING(observation_id) WHERE s.source_version_id=?", (hit.object_id,))]
                    elif hit.kind == "claim":
                        item_ids = [r[0] for r in conn.execute("SELECT item_id FROM item_claims WHERE claim_id=?", (hit.object_id,))]
                    elif hit.kind == "relation":
                        relation = conn.execute("SELECT from_item_id,to_item_id FROM item_relations WHERE relation_id=?", (hit.object_id,)).fetchone()
                        item_ids = list(relation) if relation else []
            titles = []
            for id_ in item_ids:
                item = self.store.get_item(id_)
                if item and item["lifecycle"] == "active":
                    linked[id_] = QueryHit(id_, item["content_revision"], item["title"])
                    titles.append(item["title"])
            label = "、".join(titles) if titles else "尚未归入条目的证据"
            paragraphs.append("### " + redact_urls_in_text(label).replace("\n", " ") + "\n\n" + redact_urls_in_text(hit.text))
        if not paragraphs:
            paragraphs.append("当前检索没有显示相符证据；有限的结果窗口不能证明它不存在。")
        coverage = result.coverage
        if not coverage.complete:
            paragraphs.append("检索覆盖仍有未完成部分："
                              f"未读来源 {coverage.unread_sources}，未归类来源 {coverage.unclassified_sources}，"
                              f"待嵌入 {coverage.embedding_pending}，待索引 {coverage.index_pending}。"
                              + ("当前来源尚未完整观察。" if not coverage.source_complete else "")
                              + ("部分检索能力暂不可用。" if coverage.unknown else ""))
        return SearchAnswer("\n\n".join(paragraphs), tuple(linked.values()))


class ReaderControls:
    ACTION_KINDS = frozenset({"known", "follow", "mute", "wait", "adopt", "move_notes", "keep_personal"})
    QUERY_ID = "control:query"
    MANAGEMENT_ID = "control:management"
    DEFAULT_PROPERTIES = {"follow": "阅读关注", "mute": "阅读静音", "wait": "阅读等待", "query": "查询"}

    @staticmethod
    def property_mapping(configured=None):
        """One mapping contract for first publication and subsequent polling."""
        configured = {} if configured is None else configured
        if not isinstance(configured, dict) or set(configured) - set(ReaderControls.DEFAULT_PROPERTIES):
            raise ReadingError("invalid_control_property_mapping")
        mapping = ReaderControls.DEFAULT_PROPERTIES | configured
        if (any(not isinstance(key, str) or key.startswith("wgo_")
                or not re.fullmatch(r"[\w-]+", key) for key in mapping.values()) or len(set(mapping.values())) != 4):
            raise ReadingError("invalid_control_property_mapping")
        return mapping

    def __init__(self, ledger: ReadingLedger, publisher: ProjectionPublisher, *,
                 search: Callable[[SearchRequest], SearchAnswer]):
        self.ledger = ledger
        self.publisher = publisher
        self.search = search
        ledger.install(_SCHEMA)

    def preference(self, item_id: str) -> ReaderPreference:
        row = self.ledger.one("SELECT * FROM reader_preferences WHERE generation_id=? AND item_id=?",
                              (self.ledger.generation_id, item_id))
        known = self.ledger.one("SELECT MAX(content_revision) FROM reader_known WHERE generation_id=? AND item_id=?",
                                (self.ledger.generation_id, item_id))[0] or 0
        return ReaderPreference(item_id, row["revision"] if row else 0,
                                bool(row["follow"]) if row else False,
                                bool(row["mute"]) if row else False,
                                row["wait_condition"] if row else "", known)

    def set_preference(self, item_id: str, *, expected_revision: int,
                       follow: bool | None = None, mute: bool | None = None,
                       wait: str | None = None, expected_content_revision: int | None = None) -> ReaderPreference:
        if ((follow is not None and type(follow) is not bool)
                or (mute is not None and type(mute) is not bool)
                or (wait is not None and (not isinstance(wait, str) or len(wait) > 2000))):
            raise ReadingError("invalid_reader_preference")
        with self.ledger.transaction() as conn:
            if expected_content_revision is not None and self.publisher.current_revision(item_id) != expected_content_revision:
                raise ReadingError("stale_control_action")
            row = conn.execute("SELECT * FROM reader_preferences WHERE generation_id=? AND item_id=?",
                               (self.ledger.generation_id, item_id)).fetchone()
            actual = row["revision"] if row else 0
            if actual != expected_revision:
                raise ReadingError("stale_reader_preference")
            values = (bool(row["follow"]) if row else False, bool(row["mute"]) if row else False,
                      row["wait_condition"] if row else "")
            updated = (values[0] if follow is None else follow, values[1] if mute is None else mute,
                       values[2] if wait is None else redact_urls_in_text(wait))
            revision = actual + int(updated != values)
            conn.execute("INSERT INTO reader_preferences VALUES (?,?,?,?,?,?) ON CONFLICT(generation_id,item_id) "
                         "DO UPDATE SET revision=excluded.revision,follow=excluded.follow,mute=excluded.mute,"
                         "wait_condition=excluded.wait_condition",
                         (self.ledger.generation_id, item_id, revision, *updated))
        return self.preference(item_id)

    def mark_known(self, item_id: str, displayed_revision: int, *, known_at: str):
        current = self.publisher.current_revision(item_id)
        if (type(displayed_revision) is not int or displayed_revision <= 0 or
                current is None or displayed_revision > current):
            raise ReadingError("invalid_known_revision")
        with self.ledger.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO reader_known VALUES (?,?,?,?)",
                         (self.ledger.generation_id, item_id, displayed_revision, known_at))

    def observe_open(self, observation: OpenObservation) -> ControlReceipt:
        if (observation.visible is not True or observation.origin not in {"obsidian_visible_open", "user_confirmed_open"}
                or not observation.observation_id or not observation.observed_at
                or observation.content_revision <= 0):
            raise ReadingError("unverified_open_observation")
        current = self.publisher.current_revision(observation.item_id)
        if current is None or observation.content_revision > current:
            raise ReadingError("invalid_open_revision")
        values = (observation.observation_id, self.ledger.generation_id, observation.item_id,
                  observation.content_revision, observation.observed_at, observation.origin)
        with self.ledger.transaction() as conn:
            existing = conn.execute("SELECT * FROM reader_open_observations WHERE observation_id=?",
                                    (observation.observation_id,)).fetchone()
            if existing and tuple(existing) != values:
                raise ReadingError("open_observation_collision")
            conn.execute("INSERT OR IGNORE INTO reader_open_observations VALUES (?,?,?,?,?,?)", values)
        return ControlReceipt(observation.observation_id, "observed", observation.content_revision)

    def register_property(self, object_id: str, role: str, key: str, *, initial=None):
        """Reserve one input property. Existing unregistered user keys fail closed."""
        if (role not in {"follow", "mute", "wait", "query", "view_definition", "view_member"}
                or (role == "view_definition" and not object_id.startswith("view:"))
                or (role == "view_member" and object_id != "control:views") or key.startswith("wgo_")
                or not re.fullmatch(r"[\w-]+", key)):
            raise ReadingError("invalid_control_property_mapping")
        with self.publisher.locked():
            path = self.publisher.resolve(object_id)
            if path is None:
                raise ReadingError("projection_missing")
            raw = self.publisher.read(path)
            text = raw.decode("utf-8")
            existing = self.ledger.one("SELECT property_key FROM reader_property_maps WHERE generation_id=? AND object_id=? AND role=?",
                                       (self.ledger.generation_id, object_id, role))
            if existing:
                if existing[0] != key:
                    raise ReadingError("control_mapping_already_registered")
                return
            if property_present(text, key):
                raise ReadingError("control_property_collision")
            updated = patch_properties(text, {key: initial}, machine_only=False)
            with self.ledger.transaction() as conn:
                conn.execute("INSERT INTO reader_property_maps VALUES (?,?,?,?)",
                             (self.ledger.generation_id, object_id, role, key))
                self.publisher.atomic_write(path, updated.encode("utf-8"), expected=fingerprint(raw))

    def register_item_properties(self, item_id: str, *, properties=None):
        """Prepare editable inputs before an action freezes the Item's file hash."""
        mapping = self.property_mapping(properties)
        pref = self.preference(item_id)
        for role in ("follow", "mute", "wait"):
            self.register_property(item_id, role, mapping[role], initial=getattr(pref, role))

    def sync_properties(self, item_id: str) -> ReaderPreference:
        mappings = self.ledger.all("SELECT role,property_key FROM reader_property_maps WHERE generation_id=? AND object_id=?",
                                   (self.ledger.generation_id, item_id))
        path = self.publisher.resolve(item_id)
        if path is None:
            raise ReadingError("projection_missing")
        text = self.publisher.read(path).decode("utf-8")
        values = {row["role"]: property_value(text, row["property_key"]) for row in mappings
                  if row["role"] != "query"}
        if any(value is None for value in values.values()):
            raise ReadingError("missing_control_property")
        preference = self.preference(item_id)
        return self.set_preference(item_id, expected_revision=preference.revision, **values)

    def initialize_query(self, *, property_key: str = "查询"):
        receipt = self.publisher.publish_document(
            object_id=self.QUERY_ID, path="检索.md", title="检索", body="在属性中填写问题；结果将保留可回查的条目链接。",
            content_revision=0, region="query") if self.publisher.record(self.QUERY_ID) is None else None
        if receipt is not None and receipt.status == "conflict":
            raise ReadingError("query_page_requires_adoption")
        self.register_property(self.QUERY_ID, "query", property_key, initial="")

    def query_history(self):
        return self.ledger.all("SELECT * FROM reader_queries WHERE generation_id=? ORDER BY query_revision",
                               (self.ledger.generation_id,))

    def run_query(self, *, retry_failed: bool = False) -> ControlReceipt:
        mapping = self.ledger.one("SELECT property_key FROM reader_property_maps WHERE generation_id=? AND object_id=? AND role='query'",
                                  (self.ledger.generation_id, self.QUERY_ID))
        if mapping is None:
            raise ReadingError("query_property_not_registered")
        path = self.publisher.resolve(self.QUERY_ID)
        if path is None:
            raise ReadingError("projection_missing")
        value = property_value(self.publisher.read(path).decode("utf-8"), mapping[0])
        if not isinstance(value, str):
            raise ReadingError("invalid_query_property")
        query = redact_urls_in_text(value.strip())
        if not query:
            return ControlReceipt(self.QUERY_ID, "empty")
        if len(query) > 8000:
            raise ReadingError("query_too_large")
        query_hash = fingerprint(query)
        with self.ledger.transaction() as conn:
            previous = conn.execute("SELECT * FROM reader_queries WHERE generation_id=? ORDER BY query_revision DESC LIMIT 1",
                                    (self.ledger.generation_id,)).fetchone()
            if previous and previous["query_hash"] == query_hash and not (
                    retry_failed and previous["status"] in {"deferred", "review"}):
                return ControlReceipt(previous["request_id"], "unchanged", previous["query_revision"], previous["status"])
            revision = (previous["query_revision"] if previous else 0) + 1
            request_id = "query-" + uuid.uuid4().hex
            conn.execute("INSERT INTO reader_queries VALUES (?,?,?,?,?,?,?,?)",
                         (self.ledger.generation_id, revision, query_hash, query, "running", "", "[]", request_id))
        try:
            answer = self.search(SearchRequest(query, revision, request_id))
            if not isinstance(answer, SearchAnswer) or not isinstance(answer.text, str) or not answer.text.strip():
                raise ReadingError("invalid_search_answer")
            links = []
            for hit in answer.hits:
                target = self.publisher.resolve(hit.item_id)
                if (target is None or not self.publisher.ready(hit.item_id, hit.content_revision)
                        or self.publisher.current_revision(hit.item_id) != hit.content_revision):
                    raise ReadingError("query_target_not_ready")
                if hit.block:
                    plan = self.publisher.load_plan(hit.item_id)
                    if plan is None or hit.block not in {b.id for b in plan.blocks}:
                        raise ReadingError("query_block_missing")
                    from .projections import anchor
                    block = anchor(hit.block)
                else:
                    block = ""
                links.append("- " + wikilink("WGO/" + target, hit.label, block=block))
            def current():
                latest = self.publisher.read(path)
                return (latest is not None and property_value(latest.decode("utf-8"), mapping[0]) == value
                        and all(self.publisher.current_revision(hit.item_id) == hit.content_revision
                                and self.publisher.ready(hit.item_id, hit.content_revision) for hit in answer.hits))
            rendered = redact_urls_in_text(answer.text) + ("\n\n" + "\n".join(links) if links else "")
            guards = {"properties": [{"object_id": self.QUERY_ID, "key": mapping[0], "value": value}],
                      "revisions": {hit.item_id: hit.content_revision for hit in answer.hits}}
            historical = self.publisher.publish_document(
                object_id="query-history:" + str(revision), path=f"检索记录/{revision:06d}.md",
                title="检索记录 " + str(revision), body="问题：" + query + "\n\n" + rendered,
                content_revision=revision, is_current=current, proposal_guards=guards)
            if historical.status == "conflict":
                raise ReadingError("query_history_conflict")
            visible = rendered + "\n\n" + wikilink("WGO/" + historical.path, "查看本次查询快照")
            receipt = self.publisher.publish_document(
                object_id=self.QUERY_ID, path=path, title="检索", body=visible,
                content_revision=revision, region="query", is_current=current, proposal_guards=guards)
            status = "review" if receipt.status == "conflict" else "success"
            with self.ledger.transaction() as conn:
                conn.execute("UPDATE reader_queries SET status=?,answer_text=?,hits_json=? WHERE generation_id=? AND query_revision=?",
                             (status, rendered, json_text([hit.__dict__ for hit in answer.hits]),
                              self.ledger.generation_id, revision))
            return ControlReceipt(request_id, status, revision)
        except Exception as exc:
            code = exc.code if isinstance(exc, ReadingError) else "search_failed"
            with self.ledger.transaction() as conn:
                conn.execute("UPDATE reader_queries SET status='deferred' WHERE generation_id=? AND query_revision=?",
                             (self.ledger.generation_id, revision))
            return ControlReceipt(request_id, "deferred", revision, code)

    def register_action(self, kind: str, item_id: str, *, displayed_revision: int,
                        expected_file_hash: str, label: str, value=None, proposal_id: str = "") -> str:
        if kind not in ReaderControls.ACTION_KINDS:
            raise ReadingError("invalid_control_action")
        preference = self.preference(item_id)
        return self._register_record(kind, item_id, displayed_revision, preference.revision,
                                     expected_file_hash, label, value, proposal_id)

    def _register_record(self, kind, item_id, displayed_revision, preference_revision,
                         expected_file_hash, label, value=None, proposal_id=""):
        if "\n" in label or "<!--" in label:
            raise ReadingError("invalid_control_label")
        payload = json_text(value)
        existing = self.ledger.one("""SELECT action_id FROM reader_actions WHERE generation_id=? AND kind=? AND item_id=?
            AND content_revision=? AND preference_revision=? AND file_hash=? AND value_json=?
            AND proposal_id=? AND status='registered'""", (self.ledger.generation_id, kind, item_id,
                displayed_revision, preference_revision, expected_file_hash, payload, proposal_id))
        if existing:
            return existing[0]
        action_id = uuid.uuid4().hex
        with self.ledger.transaction() as conn:
            conn.execute("INSERT INTO reader_actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (action_id, self.ledger.generation_id, kind, item_id, displayed_revision,
                          preference_revision, expected_file_hash, payload, proposal_id,
                          redact_urls_in_text(label), "registered"))
        return action_id

    @staticmethod
    def _action_label(action):
        return f"{action['label']}（内容 v{action['content_revision']}，偏好 v{action['preference_revision']}）"

    def register_item_actions(self, item_id: str, displayed_revision: int):
        file_hash = self.publisher.file_hash(item_id)
        pref = self.preference(item_id)
        for kind, value, label in (("known", None, "知道了这个版本"),
                                   ("follow", not pref.follow, "取消关注" if pref.follow else "关注"),
                                   ("mute", not pref.mute, "取消静音" if pref.mute else "静音")):
            self.register_action(kind, item_id, displayed_revision=displayed_revision,
                                 expected_file_hash=file_hash, label=label, value=value)

    def _validated_management_checks(self) -> tuple[str, list[str]]:
        path = self.publisher.resolve(self.MANAGEMENT_ID)
        row = self.publisher.record(self.MANAGEMENT_ID)
        if path is None or row is None:
            return "", []
        raw = self.publisher.read(path)
        body = region_body(raw.decode("utf-8"), "actions")
        if body is None:
            raise ReadingError("management_region_missing")
        checks = re.findall(r"^- \[[xX]\] ([^\n]+) <!-- wgo-action:([a-f0-9]{32}) -->$", body, re.M)
        def unchecked(value):
            return re.sub(r"^(- )\[[xX]\]( [^\n]+ <!-- wgo-action:[a-f0-9]{32} -->)$", r"\1[ ]\2", value, flags=re.M)
        if unchecked(body) != unchecked(row["machine_body"]):
            raise ReadingError("unregistered_management_edit")
        ids = []
        for label, id_ in checks:
            action = self.ledger.one("SELECT * FROM reader_actions WHERE action_id=? AND generation_id=?",
                                     (id_, self.ledger.generation_id))
            if action is None or action["kind"] not in self.ACTION_KINDS or self._action_label(action) != label:
                raise ReadingError("unregistered_control_action")
            ids.append(id_)
        return fingerprint(raw), ids

    def refresh_management(self):
        # Publication can run before the control poll. Preserve validated checks
        # until their registered actions receive a durable consumption receipt.
        with self.publisher.locked():
            return self._refresh_management()

    def _refresh_management(self):
        authorized_hash, checked = None, set()
        if self.publisher.record(self.MANAGEMENT_ID):
            authorized_hash, action_ids = self._validated_management_checks()
            checked = set(action_ids)
        for proposal in self.publisher.pending_proposals():
            for kind, label in (("adopt", "采用新稿"), ("move_notes", "把改写移到个人备注，再更新"),
                                ("keep_personal", "保留个人版")):
                self.register_action(kind, proposal["object_id"], displayed_revision=proposal["content_revision"],
                                     expected_file_hash=proposal["file_hash"], label=label,
                                     proposal_id=proposal["proposal_id"])
        kinds = sorted(ReaderControls.ACTION_KINDS)
        actions = self.ledger.all("SELECT * FROM reader_actions WHERE generation_id=? AND status='registered' "
                                  "AND kind IN (" + ",".join("?" for _ in kinds) + ") ORDER BY rowid",
                                  (self.ledger.generation_id, *kinds))
        lines = ["只有本页登记的操作生效；来源中的复选框不会执行。"]
        current_targets = {}
        for action in actions:
            stale = False
            if action["action_id"] not in checked:
                if action["proposal_id"]:
                    stale = not self.publisher.proposal_is_current(action["proposal_id"])
                else:
                    item_id = action["item_id"]
                    if item_id not in current_targets:
                        path = self.publisher.resolve(item_id)
                        current_targets[item_id] = (self.publisher.current_revision(item_id),
                            self.preference(item_id).revision, fingerprint(self.publisher.read(path)) if path else "")
                    stale = current_targets[item_id] != (action["content_revision"], action["preference_revision"], action["file_hash"])
            if stale:
                with self.ledger.transaction() as conn:
                    conn.execute("UPDATE reader_actions SET status='superseded' WHERE action_id=?", (action["action_id"],))
                continue
            path = self.publisher.resolve(action["item_id"])
            target = wikilink("WGO/" + path, action["item_id"]) if path else action["item_id"]
            mark = "x" if action["action_id"] in checked else " "
            lines.extend(["\n" + target, f"- [{mark}] {self._action_label(action)} "
                          f"<!-- wgo-action:{action['action_id']} -->"])
        revision = len(self.ledger.all("SELECT action_id FROM reader_actions WHERE generation_id=?",
                                       (self.ledger.generation_id,)))
        return self.publisher.publish_document(
            object_id=self.MANAGEMENT_ID, path="管理.md", title="管理", body="\n".join(lines),
            content_revision=revision, region="actions", authorized_hash=authorized_hash)

    def process_management(self, *, acted_at: str) -> list[ControlReceipt]:
        _hash, action_ids = self._validated_management_checks()
        receipts = []
        for id_ in action_ids:
            action = self.ledger.one("SELECT * FROM reader_actions WHERE action_id=?", (id_,))
            if action["status"] != "registered":
                receipts.append(ControlReceipt(id_, "already_consumed", action["content_revision"]))
                continue
            try:
                item_id, revision = action["item_id"], action["content_revision"]
                path = self.publisher.resolve(item_id) or (
                    self.ledger.one("SELECT path FROM reader_proposals WHERE proposal_id=?", (action["proposal_id"],))[0]
                    if action["proposal_id"] else None)
                raw = self.publisher.read(path) if path else None
                version_current = (self.publisher.proposal_is_current(action["proposal_id"]) if action["proposal_id"]
                                   else self.publisher.current_revision(item_id) == revision)
                if (raw is None or fingerprint(raw) != action["file_hash"]
                        or not version_current
                        or self.preference(item_id).revision != action["preference_revision"]):
                    raise ReadingError("stale_control_action")
                kind = action["kind"]
                if kind in {"adopt", "move_notes", "keep_personal"}:
                    self.publisher.resolve_proposal(action["proposal_id"], kind,
                                                    expected_revision=revision,
                                                    expected_file_hash=action["file_hash"])
                elif kind == "known":
                    self.mark_known(item_id, revision, known_at=acted_at)
                else:
                    self.set_preference(item_id, expected_revision=action["preference_revision"],
                                        expected_content_revision=revision,
                                        **{kind: json.loads(action["value_json"])})
                status, code = "applied", ""
            except ReadingError as exc:
                status, code = "rejected", exc.code
            with self.ledger.transaction() as conn:
                conn.execute("UPDATE reader_actions SET status=? WHERE action_id=?", (status, id_))
            receipts.append(ControlReceipt(id_, status, action["content_revision"], code))
        self.refresh_management()
        return receipts
