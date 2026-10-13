"""One editorial, historically honest briefing edition per local day."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
import json
from typing import Iterable
import uuid
from zoneinfo import ZoneInfo

from core.url_safety import redact_urls_in_text
from .attribution import source_attribution
from .controls import ReaderControls
from .projections import (ReadingError, ReadingLedger, ProjectionPublisher, accepted_text_artifact,
                          EvidenceExcerpt, MARKDOWN_CONTRACT, evidence_alias, fingerprint, json_text,
                          provider_binding, render_markdown_evidence, validate_markdown_evidence, wikilink)


@dataclass(frozen=True)
class BriefChange:
    id: str
    item_id: str
    content_revision: int
    title: str
    before: str
    after: str
    evidence_ids: tuple[str, ...]
    source_time: str
    known_at: str
    scope: str = ""
    correction: str = ""
    conditions: tuple[str, ...] = ()
    interest: str = ""
    kind: str = "story"
    relation_change: bool = False
    manual: str = "auto"
    before_revision: int = 0
    contributing_change_ids: tuple[str, ...] = ()
    attributions: tuple[dict, ...] = ()


@dataclass(frozen=True)
class BriefEvidenceRef:
    item_id: str
    evidence_id: str


@dataclass(frozen=True)
class BriefParagraph:
    text: str
    change_ids: tuple[str, ...]
    evidence_refs: tuple[BriefEvidenceRef, ...]
    scopes: tuple[str, ...] = ()
    corrections: tuple[str, ...] = ()


@dataclass(frozen=True)
class BriefPlan:
    status: str
    title: str
    paragraphs: tuple[BriefParagraph, ...]
    reason: str = ""

    @classmethod
    def from_json(cls, text: str) -> "BriefPlan":
        try:
            value = json.loads(text)
            return cls(value["status"], value.get("title", "今日简报"), tuple(
                BriefParagraph(p["text"], tuple(p["change_ids"]),
                               tuple(BriefEvidenceRef(e["item_id"], e["evidence_id"]) for e in p["evidence_refs"]),
                               tuple(p.get("scopes", ())), tuple(p.get("corrections", ())))
                for p in value.get("paragraphs", ())), value.get("reason", ""))
        except (ValueError, KeyError, TypeError) as exc:
            raise ReadingError("invalid_brief_plan") from exc


@dataclass(frozen=True)
class BriefReceipt:
    edition_id: str
    local_day: str
    status: str
    revision: int
    request_id: str
    path: str = ""
    code: str = ""
    unchanged: bool = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reader_brief_editions (
 generation_id TEXT NOT NULL,timezone TEXT NOT NULL,local_day TEXT NOT NULL,
 edition_id TEXT NOT NULL,content_revision INTEGER NOT NULL,successful_text TEXT NOT NULL,
 successful_plan TEXT NOT NULL,input_hash TEXT NOT NULL,status TEXT NOT NULL,request_id TEXT NOT NULL,
 PRIMARY KEY(generation_id,timezone,local_day));
CREATE TABLE IF NOT EXISTS reader_brief_history (
 generation_id TEXT NOT NULL,edition_id TEXT NOT NULL,content_revision INTEGER NOT NULL,
 snapshot_text TEXT NOT NULL,plan_json TEXT NOT NULL,inputs_json TEXT NOT NULL,
 PRIMARY KEY(generation_id,edition_id,content_revision));
CREATE TABLE IF NOT EXISTS reader_brief_attempts (
 request_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,edition_id TEXT NOT NULL,
 input_hash TEXT NOT NULL,status TEXT NOT NULL,code TEXT NOT NULL,plan_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reader_brief_inputs (
 generation_id TEXT NOT NULL,input_hash TEXT NOT NULL,inputs_json TEXT NOT NULL,bindings_json TEXT NOT NULL,
 PRIMARY KEY(generation_id,input_hash));
"""


def _brief_excerpts(changes, refs):
    allowed = {(ref.item_id, ref.evidence_id) for ref in refs}
    excerpts = {id_: EvidenceExcerpt(id_, "") for _, id_ in allowed}
    for change in changes:
        for value in change.attributions:
            if (change.item_id, value["evidence_id"]) in allowed:
                excerpts[value["evidence_id"]] = EvidenceExcerpt(value["evidence_id"], value.get("excerpt", ""),
                    speaker_id=value.get("speaker_id"), speaker_name=value.get("speaker_name"))
    return excerpts


def validate_brief_plan(plan: BriefPlan, changes: tuple[BriefChange, ...], *, check_quotes=True) -> BriefPlan:
    if plan.status not in {"success", "silent", "review"} or not isinstance(plan.title, str):
        raise ReadingError("invalid_brief_plan")
    if plan.status in {"silent", "review"}:
        if plan.paragraphs or not isinstance(plan.reason, str) or not plan.reason.strip():
            raise ReadingError("invalid_brief_disposition")
        if plan.status == "silent" and any(c.manual == "include" for c in changes):
            raise ReadingError("brief_ignored_manual_include")
        return plan
    if not plan.paragraphs or len(plan.paragraphs) > 32:
        raise ReadingError("invalid_brief_plan")
    by_id = {c.id: c for c in changes}
    if len(by_id) != len(changes):
        raise ReadingError("duplicate_brief_change")
    for paragraph in plan.paragraphs:
        if (not isinstance(paragraph.text, str) or not paragraph.text.strip()
                or len(paragraph.text) > 16000 or not paragraph.change_ids
                or not set(paragraph.change_ids) <= by_id.keys()
                or "<!-- wgo:" in paragraph.text or "<!-- wgo-action:" in paragraph.text):
            raise ReadingError("invalid_brief_paragraph")
        referred = [by_id[id_] for id_ in paragraph.change_ids]
        valid_evidence = {(c.item_id, e) for c in referred for e in c.evidence_ids}
        supplied = {(e.item_id, e.evidence_id) for e in paragraph.evidence_refs}
        if not supplied or not supplied <= valid_evidence:
            raise ReadingError("invalid_brief_evidence")
        if any(c.evidence_ids and not supplied.intersection({(c.item_id, e) for e in c.evidence_ids}) for c in referred):
            raise ReadingError("brief_unreferenced_change")
        expected_scopes = {redact_urls_in_text(c.scope) for c in referred if c.scope}
        expected_corrections = {redact_urls_in_text(c.correction) for c in referred if c.correction}
        if (set(paragraph.scopes) != expected_scopes or set(paragraph.corrections) != expected_corrections):
            raise ReadingError("brief_lost_scope_or_correction")
        validate_markdown_evidence(redact_urls_in_text(paragraph.text),
                                   _brief_excerpts(referred, paragraph.evidence_refs), check_quotes=check_quotes)
    if not {c.id for c in changes if c.manual == "include"} <= {
            id_ for paragraph in plan.paragraphs for id_ in paragraph.change_ids}:
        raise ReadingError("brief_ignored_manual_include")
    return plan


class BriefingService:
    def __init__(self, ledger: ReadingLedger, publisher: ProjectionPublisher,
                 controls: ReaderControls, provider, *, timezone: str, max_input_bytes: int = 24000):
        if type(max_input_bytes) is not int or max_input_bytes < 6000:
            raise ReadingError("invalid_brief_context_budget")
        self.ledger = ledger
        self.publisher = publisher
        self.controls = controls
        self.provider = provider
        self.timezone = timezone
        provider_limit = getattr(provider, "max_input_bytes", max_input_bytes)
        self.max_input_bytes = min(max_input_bytes, provider_limit) if type(provider_limit) is int else max_input_bytes
        if self.max_input_bytes < 6000:
            raise ReadingError("invalid_brief_context_budget")
        self.zone = ZoneInfo(timezone)
        ledger.install(_SCHEMA)

    def _identity(self, now: datetime) -> tuple[str, str]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ReadingError("brief_requires_aware_time")
        day = now.astimezone(self.zone).date().isoformat()
        return day, "brief:" + fingerprint(self.timezone)[:12] + ":" + day

    def edition(self, local_day: str):
        return self.ledger.one("SELECT * FROM reader_brief_editions WHERE generation_id=? AND timezone=? AND local_day=?",
                               (self.ledger.generation_id, self.timezone, local_day))

    def history(self, edition_id: str):
        return self.ledger.all("SELECT * FROM reader_brief_history WHERE generation_id=? AND edition_id=? ORDER BY content_revision",
                               (self.ledger.generation_id, edition_id))

    def _candidates(self, changes: Iterable[BriefChange]):
        eligible, bindings = [], {}
        for change in changes:
            if (change.manual not in {"auto", "include", "exclude", "defer"}
                    or change.content_revision <= 0 or not change.id or not change.item_id):
                raise ReadingError("invalid_brief_change")
            change = replace(change, **{key: redact_urls_in_text(getattr(change, key))
                for key in ("title", "before", "after", "scope", "correction", "interest")},
                conditions=tuple(redact_urls_in_text(value) for value in change.conditions))
            preference = self.controls.preference(change.item_id)
            # Explicit suppression and already-known content cannot leak into
            # a cross-Item combined paragraph through another candidate.
            if (preference.mute or change.manual in {"exclude", "defer"}
                    or preference.known_revision >= change.content_revision):
                continue
            if change.before == change.after and not change.relation_change and not change.correction:
                continue
            change = replace(change, attributions=self._attributions(change))
            eligible.append(change)
            bindings[change.item_id] = (preference.revision, preference.known_revision, change.content_revision)
        if len({c.id for c in eligible}) != len(eligible):
            raise ReadingError("duplicate_brief_change")
        return tuple(eligible), bindings

    def _attributions(self, change):
        # Canonical captured metadata is authoritative. Explicit snapshots are
        # supported for source-free tests/adapters, but never override a real
        # generation's source record or create a second people database.
        if self.ledger.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_records'"):
            result = []
            for id_ in change.evidence_ids:
                row = self.ledger.one("SELECT s.excerpt,r.source_namespace,r.chat_id,r.payload_json "
                    "FROM source_spans s JOIN source_records r ON r.source_version_id=s.source_version_id "
                    "WHERE s.span_id=?", (id_,))
                if row is None:
                    raise ReadingError("brief_evidence_source_missing")
                result.append({"evidence_id": id_, "excerpt": redact_urls_in_text(row["excerpt"]),
                    **source_attribution(row["source_namespace"], row["chat_id"], json.loads(row["payload_json"]))})
            return tuple(result)
        result = []
        for value in change.attributions:
            if (not isinstance(value, dict) or set(value) != {"evidence_id", "excerpt", "speaker_id", "speaker_name"}
                    or value["evidence_id"] not in change.evidence_ids or not isinstance(value["excerpt"], str)
                    or any(value[key] is not None and not isinstance(value[key], str)
                           for key in ("speaker_id", "speaker_name"))):
                raise ReadingError("invalid_brief_attribution")
            result.append({key: redact_urls_in_text(text) if isinstance(text, str) else text
                           for key, text in value.items()})
        if len({value["evidence_id"] for value in result}) != len(result):
            raise ReadingError("invalid_brief_attribution")
        return tuple(result)

    def _still_current(self, bindings) -> bool:
        for item_id, (preference_revision, known_revision, revision) in bindings.items():
            pref = self.controls.preference(item_id)
            if (self.publisher.current_revision(item_id) != revision or pref.revision != preference_revision
                    or pref.known_revision != known_revision or pref.mute):
                return False
        return True

    def _save_disposition(self, day, edition_id, input_hash, request_id, status, code, plan_json=""):
        with self.ledger.transaction() as conn:
            conn.execute("UPDATE reader_brief_attempts SET status=?,code=?,plan_json=CASE WHEN ?='' THEN plan_json ELSE ? END WHERE request_id=?",
                         (status, code, plan_json, plan_json, request_id))
            conn.execute("""INSERT INTO reader_brief_editions VALUES (?,?,?,?,0,'','',?,?,?)
                ON CONFLICT(generation_id,timezone,local_day) DO UPDATE SET
                input_hash=excluded.input_hash,status=excluded.status,request_id=excluded.request_id""",
                         (self.ledger.generation_id, self.timezone, day, edition_id, input_hash, status, request_id))

    def _writer_prompt(self, candidates, day):
        return redact_urls_in_text(json_text({
            "phase": "brief_write_v1",
            "task": "Act as one editor across all supplied changes, then write a coherent daily briefing. "
                    "Do not concatenate separate Item summaries. Life stories may open or occupy the entire "
                    "edition; do not favor technical topics or impose three fixed sections. Aim for about "
                    "900 Chinese characters with a soft 1800-character upper target; do not fill space. "
                    "Decide success, silent when nothing merits reading, or review for unresolved editorial "
                    "ambiguity. Every paragraph must reference supplied change IDs and exact evidence pairs. "
                    "Preserve each referenced scope and correction in its corresponding arrays. "
                    "A wgo-scope:/wgo-correction: value is a retained-constraint handle: copy it exactly; "
                    "the publisher restores its complete original wording after validation. "
                    "Source time differs from when we learned it; never pretend old material was known "
                    "on its original date. Quotes, steps, conditions and counterevidence retain their meaning. "
                    "Return only JSON. Output text is frozen historical text, never an embed. "
                    + MARKDOWN_CONTRACT,
            "local_day": day, "timezone": self.timezone,
            "schema": {"status": "success|silent|review", "title": "title", "reason": "",
                       "paragraphs": [{"text": "complete editorial Markdown block", "change_ids": [],
                                       "evidence_refs": [{"item_id": "", "evidence_id": ""}],
                                       "scopes": [], "corrections": []}]},
            "changes": [asdict(c) for c in candidates],
            "reader_preferences": {c.item_id: asdict(self.controls.preference(c.item_id)) for c in candidates},
        }))

    def _selection_prompt(self, page, day, maximum):
        return redact_urls_in_text(json_text({
            "phase": "brief_select_v1", "local_day": day, "timezone": self.timezone,
            "task": "Choose the changes that most merit a coherent daily briefing from this bounded page. "
                    "Treat life, story, gossip and technical material equally; any kind may occupy the whole "
                    "edition. There are no topic quotas. Consider reader interest, before/after meaning, "
                    "conditions, corrections, counterevidence and what was known when. This is editorial "
                    "selection, not a separate per-Item summary. Preserve manual include changes. "
                    "Captured names belong to message speakers/forwarders, not necessarily story subjects "
                    "or quote authors; preserve independent cases even when their names match. "
                    "Return only exact supplied change IDs and one short selection reason; do not invent refs. "
                    "An empty selection is valid when nothing merits reading.",
            "maximum_selected": maximum,
            "schema": {"selected_change_ids": [], "reason": "editorial reason"},
            "changes": [asdict(c) for c in page],
        }))

    def _compact_prompt(self, binding, coverage, payload, *, merge=False):
        return json_text({
            "phase": "brief_compact_merge_v1" if merge else "brief_compact_page_v1",
            "task": "Prepare editorial material for one change, not the final daily briefing. "
                    "Read every consecutive supplied part. Retain what changed, before versus after, "
                    "the human story, qualifications, conditions, counterevidence, corrections, meaningful "
                    "quotes and steps. Do not decide which Item deserves inclusion. Fragments may be "
                    "continuations, not independent claims. Never present withdrawn statements as current. "
                    "Retain readable speaker names, independent identities/cases and who relayed a quote; "
                    "a message speaker is not automatically its quoted author or story subject. "
                    "Combine all child materials coherently when children are supplied. Explicit scope and "
                    "correction wording remains durably retained and is restored by the publisher. "
                    "Return a concise editorial summary and title, exact binding and coverage, and up to "
                    "16 relevant supplied evidence IDs. No invented references or machine markup. "
                    "If any evidence IDs are supplied, retain at least one. Return only JSON.",
            "binding": binding, "coverage": coverage,
            "maximum_summary_bytes": self.max_input_bytes // 8,
            "schema": {"binding": binding, "coverage": coverage, "title": "short title",
                       "summary": "editorial material", "evidence_ids": []},
            **payload,
        })

    def _compact_change(self, change, request_id, bindings):
        """Read every original field in bounded, durably accepted work pages.

        Program-assigned contiguous part ranges prove coverage; the provider
        performs editorial compression, not source mutation. Original inputs
        remain in reader_brief_inputs and the eventual historical edition.
        """
        original = json.loads(redact_urls_in_text(json_text(asdict(change))))
        binding = {"change_id": change.id, "item_id": change.item_id,
                   "content_revision": change.content_revision, "before_revision": change.before_revision,
                   "relation_change": change.relation_change, "input_hash": fingerprint(json_text(original))}
        fields = [(key, None, value) for key, value in original.items() if isinstance(value, str)]
        fields.extend((key, index, value) for key in ("conditions", "evidence_ids", "contributing_change_ids")
                      for index, value in enumerate(original[key]))
        fields.extend(("attributions." + key, index, value) for index, attribution in enumerate(original["attributions"])
                      for key, value in attribution.items() if isinstance(value, str))
        # These source fields are complete, including long scopes/conditions and
        # reference inventories. Redaction precedes splitting a possible URL.
        parts = []
        for field, index, value in fields:
            size = max(1, self.max_input_bytes // 24)
            slices = [(0, value)] if field == "evidence_ids" or not value else [
                (start, value[start:start + size]) for start in range(0, len(value), size)]
            for start, text in slices:
                part = {"part": len(parts), "field": field, "index": index,
                        "start": start, "end": start + len(text), "total": len(value), "text": text}
                if field.startswith("attributions."):
                    part["evidence_id"] = original["attributions"][index]["evidence_id"]
                parts.append(part)
        pages, page = [], []
        for part in parts:
            proposed = [*page, part]
            coverage = [proposed[0]["part"], proposed[-1]["part"] + 1]
            prompt = self._compact_prompt(binding, coverage, {"parts": proposed,
                "evidence_ids": [p["text"] for p in proposed if p["field"] == "evidence_ids"]})
            if len(prompt.encode()) > self.max_input_bytes:
                if not page:
                    raise ReadingError("brief_change_identity_exceeds_budget")
                pages.append(page)
                page = [part]
            else:
                page = proposed
        if page:
            pages.append(page)

        def accept(payload, coverage, suffix, *, merge=False):
            prompt = self._compact_prompt(binding, coverage, payload, merge=merge)
            if len(prompt.encode()) > self.max_input_bytes:
                raise ReadingError("brief_compaction_context_exceeds_budget")
            allowed = set(payload.get("evidence_ids", ()))
            if merge:
                allowed = {id_ for child in payload["children"] for id_ in child["evidence_ids"]}
            def validate(raw):
                try:
                    value = json.loads(raw)
                    refs = value["evidence_ids"]
                    if (set(value) != {"binding", "coverage", "title", "summary", "evidence_ids"}
                            or json_text(value["binding"]) != json_text(binding)
                            or value["coverage"] != coverage or any(type(index) is not int for index in value["coverage"])
                            or not isinstance(value["summary"], str) or not value["summary"].strip()
                            or len(value["summary"].encode()) > self.max_input_bytes // 8
                            or not isinstance(value["title"], str) or not value["title"].strip()
                            or len(value["title"].encode()) > 240
                            or not isinstance(refs, list) or len(refs) > 16
                            or any(not isinstance(id_, str) for id_ in refs)
                            or len(set(refs)) != len(refs) or not set(refs) <= allowed
                            or (allowed and not refs) or "<!-- wgo" in value["summary"] + value["title"]):
                        raise ReadingError("invalid_brief_compaction")
                    serialized = redact_urls_in_text(json_text(value))
                    if len(serialized.encode()) > self.max_input_bytes // 3:
                        raise ReadingError("brief_compaction_did_not_reduce_context")
                    return serialized
                except (ValueError, KeyError, TypeError) as exc:
                    raise ReadingError("invalid_brief_compaction") from exc
            artifact = accepted_text_artifact(self.ledger, self.provider, prompt=prompt,
                request_id=f"{request_id}:compact:{fingerprint(change.id)[:16]}:{suffix}",
                prompt_version="brief-compact-merge-v2" if merge else "brief-compact-page-v2", validate=validate)
            if not self._still_current(bindings):
                raise ReadingError("stale_brief_input")
            return json.loads(artifact)

        children = [accept({"parts": page, "evidence_ids": [p["text"] for p in page if p["field"] == "evidence_ids"]},
                           [page[0]["part"], page[-1]["part"] + 1], f"page:{index}")
                    for index, page in enumerate(pages)]
        level = 0
        while len(children) > 1:
            groups, group = [], []
            for child in children:
                trial = [*group, child]
                coverage = [trial[0]["coverage"][0], trial[-1]["coverage"][1]]
                if group and len(self._compact_prompt(binding, coverage, {"children": trial}, merge=True).encode()) > self.max_input_bytes:
                    groups.append(group)
                    group = []
                group.append(child)
            if group:
                groups.append(group)
            if len(groups) >= len(children):
                raise ReadingError("brief_compaction_did_not_reduce_context")
            children = [group[0] if len(group) == 1 else accept({"children": group},
                        [group[0]["coverage"][0], group[-1]["coverage"][1]], f"merge:{level}:{index}", merge=True)
                        for index, group in enumerate(groups)]
            level += 1
        compacted = children[0]
        if compacted["coverage"] != [0, len(parts)]:
            raise ReadingError("brief_compaction_coverage_incomplete")
        return replace(change, title=compacted["title"], before="", after=compacted["summary"],
                       evidence_ids=tuple(compacted["evidence_ids"]), conditions=(), interest="", contributing_change_ids=(),
                       attributions=tuple({key: value for key, value in attribution.items() if key != "excerpt"}
                           for attribution in change.attributions if attribution["evidence_id"] in compacted["evidence_ids"]),
                       scope="wgo-scope:" + binding["input_hash"] if change.scope else "",
                       correction="wgo-correction:" + binding["input_hash"] if change.correction else "")

    def _editor_candidates(self, candidates, day, request_id, bindings):
        """Hierarchical editorial selection; no unbounded prompt or type priority."""
        pool = tuple(self._compact_change(c, request_id, bindings)
                     if max(len(self._writer_prompt((c,), day).encode()),
                            len(self._selection_prompt((c,), day, 1).encode())) > self.max_input_bytes else c
                     for c in candidates)
        level = 0
        while pool and len(self._writer_prompt(pool, day).encode()) > self.max_input_bytes:
            pages, page = [], []
            for candidate in pool:
                if len(self._selection_prompt((candidate,), day, 1).encode()) > self.max_input_bytes:
                    raise ReadingError("brief_compaction_did_not_reduce_context")
                if page and len(self._selection_prompt((*page, candidate), day, 8).encode()) > self.max_input_bytes:
                    pages.append(tuple(page))
                    page = []
                page.append(candidate)
            if page:
                pages.append(tuple(page))
            selected = []
            for page_no, page in enumerate(pages):
                maximum = max(1, min(8, len(page) // 2))
                mandatory = {c.id for c in page if c.manual == "include"}
                if len(mandatory) > maximum:
                    raise ReadingError("brief_manual_selection_exceeds_budget")
                prompt = self._selection_prompt(page, day, maximum)
                def validate(raw, page=page, maximum=maximum, mandatory=mandatory):
                    try:
                        value = json.loads(raw)
                        ids = value["selected_change_ids"]
                        if (not isinstance(ids, list) or any(not isinstance(id_, str) for id_ in ids)
                                or len(ids) > maximum or len(set(ids)) != len(ids)
                                or not set(ids) <= {c.id for c in page} or not mandatory <= set(ids)
                                or not isinstance(value["reason"], str) or not value["reason"].strip()
                                or len(value["reason"]) > 2000):
                            raise ReadingError("invalid_brief_selection")
                        return json_text({"selected_change_ids": ids, "reason": redact_urls_in_text(value["reason"])})
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ReadingError("invalid_brief_selection") from exc
                artifact = accepted_text_artifact(self.ledger, self.provider, prompt=prompt,
                    request_id=f"{request_id}:select:{level}:{page_no}", prompt_version="brief-select-v2", validate=validate)
                if not self._still_current(bindings):
                    raise ReadingError("stale_brief_input")
                chosen = set(json.loads(artifact)["selected_change_ids"])
                selected.extend(c for c in page if c.id in chosen)
            if len(selected) >= len(pool):
                raise ReadingError("brief_selection_did_not_reduce_context")
            pool, level = tuple(selected), level + 1
        return pool

    def build(self, changes: Iterable[BriefChange], *, now: datetime,
              request_id: str | None = None, retry: bool = False) -> BriefReceipt:
        day, edition_id = self._identity(now)
        changes = tuple(changes)
        candidates, bindings = self._candidates(changes)
        # Full filtered inputs are bound so a preference change allows a fresh
        # attempt; excluded text itself is never sent to the provider.
        route = provider_binding(self.provider)
        input_hash = fingerprint(json_text({"changes": [asdict(c) for c in candidates], "bindings": bindings,
                                           "generation": self.ledger.generation_id, "day": day,
                                           "timezone": self.timezone, "provider_route": route,
                                           "prompt_version": "brief-plan-v3", "max_input_bytes": self.max_input_bytes}))
        with self.ledger.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO reader_brief_inputs VALUES (?,?,?,?)",
                         (self.ledger.generation_id, input_hash,
                          redact_urls_in_text(json_text([asdict(c) for c in candidates])), json_text(bindings)))
        existing = self.edition(day)
        cached = self.ledger.one("SELECT * FROM reader_brief_attempts WHERE generation_id=? AND edition_id=? AND input_hash=? "
                                 "AND plan_json<>'' ORDER BY rowid DESC LIMIT 1", (self.ledger.generation_id, edition_id, input_hash))
        resumable = cached and BriefPlan.from_json(cached["plan_json"]).status == "success"
        if existing and existing["input_hash"] == input_hash and not retry and not (
                (existing["status"] == "deferred" and candidates) or (existing["status"] == "review" and resumable)):
            path = self.publisher.resolve(edition_id) or ""
            return BriefReceipt(edition_id, day, existing["status"], existing["content_revision"],
                                existing["request_id"], path, unchanged=True)
        if cached:
            request_id = cached["request_id"]
        else:
            request_id = request_id or "brief-" + uuid.uuid4().hex
            reused = self.ledger.one("SELECT * FROM reader_brief_attempts WHERE request_id=?", (request_id,))
            if reused:
                if reused["input_hash"] != input_hash or reused["generation_id"] != self.ledger.generation_id:
                    raise ReadingError("brief_request_identity_conflict")
            unresolved = self.ledger.one("SELECT * FROM reader_brief_attempts WHERE generation_id=? AND edition_id=? AND input_hash=? "
                                         "AND status IN ('running','unknown') LIMIT 1", (self.ledger.generation_id, edition_id, input_hash))
            if unresolved:
                # Every provider invocation has its own durable artifact state.
                # A crash between accepted pages is resumable; an actual unknown
                # invocation is blocked by accepted_text_artifact itself.
                request_id = unresolved["request_id"]
            with self.ledger.transaction() as conn:
                conn.execute("INSERT OR IGNORE INTO reader_brief_attempts VALUES (?,?,?,?,?,?,?)",
                             (request_id, self.ledger.generation_id, edition_id, input_hash, "running", "", ""))
        revision = existing["content_revision"] if existing else 0
        if not candidates:
            status = "deferred" if any(c.manual == "defer" for c in changes) else "silent"
            self._save_disposition(day, edition_id, input_hash, request_id, status, "no_eligible_change")
            return BriefReceipt(edition_id, day, status, revision, request_id, code="no_eligible_change")
        invoked, received = False, False
        try:
            if not self._still_current(bindings):
                raise ReadingError("stale_brief_input")
            if cached:
                plan = validate_brief_plan(BriefPlan.from_json(cached["plan_json"]), candidates)
            else:
                chosen = self._editor_candidates(candidates, day, request_id, bindings)
                if not chosen:
                    plan = BriefPlan("silent", "", (), "编辑筛选后没有值得写入本日简报的变化。")
                else:
                    prompt = self._writer_prompt(chosen, day)
                    invoked = True
                    originals = {c.id: c for c in candidates}
                    original_chosen = tuple(originals[c.id] for c in chosen)
                    def validate_writer(raw):
                        candidate_plan = BriefPlan.from_json(raw)
                        # Cached artifacts already contain restored constraints.
                        # Normalize those arrays only when they exactly match;
                        # references must still be among the writer's inputs.
                        writer_inputs = {c.id: c for c in chosen}
                        normalized = []
                        for paragraph in candidate_plan.paragraphs:
                            if not set(paragraph.change_ids) <= writer_inputs.keys():
                                raise ReadingError("invalid_brief_paragraph")
                            values = {}
                            for key, attribute in (("scopes", "scope"), ("corrections", "correction")):
                                expected = {redact_urls_in_text(getattr(originals[id_], attribute))
                                            for id_ in paragraph.change_ids if getattr(originals[id_], attribute)}
                                if set(getattr(paragraph, key)) == expected:
                                    values[key] = tuple(dict.fromkeys(getattr(writer_inputs[id_], attribute)
                                        for id_ in paragraph.change_ids if getattr(writer_inputs[id_], attribute)))
                            normalized.append(replace(paragraph, **values))
                        candidate_plan = validate_brief_plan(replace(candidate_plan, paragraphs=tuple(normalized)), chosen,
                                                             check_quotes=False)
                        candidate_plan = replace(candidate_plan, paragraphs=tuple(replace(p,
                            scopes=tuple(dict.fromkeys(redact_urls_in_text(originals[id_].scope)
                                                       for id_ in p.change_ids if originals[id_].scope)),
                            corrections=tuple(dict.fromkeys(redact_urls_in_text(originals[id_].correction)
                                                            for id_ in p.change_ids if originals[id_].correction)))
                            for p in candidate_plan.paragraphs))
                        return json_text(asdict(validate_brief_plan(candidate_plan, original_chosen)))
                    artifact = accepted_text_artifact(self.ledger, self.provider, prompt=prompt,
                        request_id=request_id, prompt_version="brief-write-v3", validate=validate_writer)
                    received = True
                    plan = BriefPlan.from_json(artifact)
            serialized = json_text(asdict(plan))
            if not self._still_current(bindings):
                raise ReadingError("stale_brief_input")
            # This validated model artifact is durable before any projection
            # lookup or filesystem publication. Resume reuses this exact plan.
            with self.ledger.transaction() as conn:
                conn.execute("UPDATE reader_brief_attempts SET status='accepted',plan_json=? WHERE request_id=?",
                             (serialized, request_id))
            if plan.status != "success":
                self._save_disposition(day, edition_id, input_hash, request_id, plan.status, "editorial_" + plan.status,
                                       serialized)
                return BriefReceipt(edition_id, day, plan.status, revision, request_id, code="editorial_" + plan.status)
            by_id = {c.id: c for c in candidates}
            paragraphs = []
            for paragraph in plan.paragraphs:
                excerpts = _brief_excerpts((by_id[id_] for id_ in paragraph.change_ids), paragraph.evidence_refs)
                owners = {ref.evidence_id: ref.item_id for ref in paragraph.evidence_refs}
                text, placed = render_markdown_evidence(redact_urls_in_text(paragraph.text), excerpts,
                    lambda id_, table: self.publisher.evidence_link(owners[id_], id_, table=table,
                                                                       alias=evidence_alias(excerpts[id_])))
                for scope in paragraph.scopes:
                    if scope not in text:
                        text += "\n\n范围：" + scope
                for correction in paragraph.corrections:
                    if correction not in text:
                        text += "\n\n更正：" + correction
                refs = []
                for ref in paragraph.evidence_refs:
                    if ref.evidence_id not in placed:
                        refs.append(self.publisher.evidence_link(ref.item_id, ref.evidence_id,
                                                                 alias=evidence_alias(excerpts[ref.evidence_id])))
                for change_id in paragraph.change_ids:
                    change = by_id[change_id]
                    for condition in change.conditions:
                        visible_condition = redact_urls_in_text(condition)
                        if visible_condition not in text:
                            text += "\n\n条件：" + visible_condition
                    if not self.publisher.ready(change.item_id, change.content_revision):
                        raise ReadingError("brief_item_projection_not_ready")
                    target = self.publisher.resolve(change.item_id)
                    refs.append(wikilink("WGO/" + target, change.title))
                    text += ("\n\n来源时间：" + (change.source_time or "未提供")
                             + "；获知时间：" + (change.known_at or "未提供"))
                paragraphs.append(text + "\n\n" + " · ".join(dict.fromkeys(refs)))
            rendered = "\n\n".join(paragraphs)
            selected_ids = {id_ for paragraph in plan.paragraphs for id_ in paragraph.change_ids}
            selected_inputs = [asdict(c) for c in candidates if c.id in selected_ids]
            previous_history = self.ledger.one("SELECT inputs_json FROM reader_brief_history WHERE generation_id=? "
                "AND edition_id=? AND content_revision=?", (self.ledger.generation_id, edition_id, revision))
            prior_selected = ([c for c in json.loads(previous_history[0]) if c["id"] in selected_ids]
                              if previous_history else [])
            # A new exact contributor/version needs its own historical plan
            # even when the writer chooses the same words. Renamed links are
            # render changes and must not create another unread edition.
            revision += int(existing is None or serialized != existing["successful_plan"]
                            or json_text(selected_inputs) != json_text(prior_selected))
            if revision == 0:
                revision = 1
            projection = self.publisher.record(edition_id)
            semantic_at = (projection["semantic_at"] if projection and projection["content_revision"] == revision
                           else now.isoformat())
            receipt = self.publisher.publish_document(
                object_id=edition_id, path="简报/" + day + ".md", title=plan.title,
                body=rendered, content_revision=revision, semantic_at=semantic_at,
                proposal_guards={"items": bindings},
                is_current=lambda: self._still_current(bindings))
            if receipt.status == "conflict":
                self._save_disposition(day, edition_id, input_hash, request_id, "review", "brief_projection_conflict", serialized)
                return BriefReceipt(edition_id, day, "review", existing["content_revision"] if existing else 0,
                                    request_id, receipt.path, "brief_projection_conflict")
            with self.ledger.transaction() as conn:
                conn.execute("""INSERT INTO reader_brief_editions VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(generation_id,timezone,local_day) DO UPDATE SET
                    content_revision=excluded.content_revision,successful_text=excluded.successful_text,
                    successful_plan=excluded.successful_plan,input_hash=excluded.input_hash,
                    status=excluded.status,request_id=excluded.request_id""",
                             (self.ledger.generation_id, self.timezone, day, edition_id, revision,
                              rendered, serialized, input_hash, "success", request_id))
                conn.execute("INSERT OR IGNORE INTO reader_brief_history VALUES (?,?,?,?,?,?)",
                             (self.ledger.generation_id, edition_id, revision, rendered, serialized,
                              redact_urls_in_text(json_text([asdict(c) for c in candidates]))))
                conn.execute("UPDATE reader_brief_attempts SET status='success',plan_json=? WHERE request_id=?",
                             (serialized, request_id))
            return BriefReceipt(edition_id, day, "success", revision, request_id, receipt.path)
        except Exception as exc:
            code = exc.code if isinstance(exc, ReadingError) else "brief_provider_failed"
            if code == "text_provider_outcome_unknown":
                code = "brief_provider_outcome_unknown"
            self._save_disposition(day, edition_id, input_hash, request_id, "deferred", code)
            invocation = self.ledger.one("SELECT status FROM reader_text_artifacts WHERE request_id=?", (request_id,))
            if invoked and not received and invocation and invocation["status"] in {"running", "unknown"}:
                with self.ledger.transaction() as conn:
                    conn.execute("UPDATE reader_brief_attempts SET status='unknown' WHERE request_id=?",
                                 (request_id,))
            return BriefReceipt(edition_id, day, "deferred", existing["content_revision"] if existing else 0,
                                request_id, code=code)

    def rebuild(self, local_day: str) -> BriefReceipt:
        edition = self.edition(local_day)
        if edition is None or not edition["successful_text"]:
            raise ReadingError("brief_successful_edition_missing")
        receipt = self.publisher.publish_document(
            object_id=edition["edition_id"], path="简报/" + local_day + ".md", title="今日简报",
            body=edition["successful_text"], content_revision=edition["content_revision"],
            semantic_at=(self.publisher.record(edition["edition_id"]) or {"semantic_at": ""})["semantic_at"])
        return BriefReceipt(edition["edition_id"], local_day,
                            "review" if receipt.status == "conflict" else "success",
                            edition["content_revision"], edition["request_id"], receipt.path, unchanged=True)
