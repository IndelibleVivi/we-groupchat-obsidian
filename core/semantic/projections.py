"""Versioned, ownership-preserving reading projections of canonical snapshots.

No source discovery, provider construction or second fact database lives here.
The caller supplies snapshots and a connection factory for its generation DB.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
from typing import Callable, Iterable
import uuid

from core.platform import LockMode, create_file_lock
from core.url_safety import redact_urls_in_text


class ReadingError(RuntimeError):
    """A content-free consumer failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def fingerprint(value: str | bytes) -> str:
    """Integrity binding for plans, filesystem CAS and published receipts."""
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def provider_binding(provider) -> str:
    route = getattr(provider, "route", None)
    if route is None or any(not isinstance(getattr(route, name, None), str) or not getattr(route, name)
                            for name in ("provider", "model", "task")):
        raise ReadingError("text_provider_version_required")
    return json_text({"provider": route.provider, "model": route.model, "task": route.task,
                      "fallback_from": getattr(route, "fallback_from", None)})


def accepted_text_artifact(ledger, provider, *, prompt: str, request_id: str,
                           prompt_version: str, validate: Callable[[str], str],
                           validation_failure_codes: frozenset[str] = frozenset()) -> str:
    """Persist a validated consumer result before allowing dependent writes.

    A running/unknown invocation cannot be replaced with a new request ID. A
    validated artifact is reusable only for the identical generation, prompt
    and provider route; SQLite remains the one generation database. Opt-in
    validation diagnostics preserve failed rows even if a caller changes IDs;
    they prove a received validation failure, never an accepted artifact.
    """
    route = provider_binding(provider)
    job_key = fingerprint(json_text([ledger.generation_id, prompt_version, route, prompt]))
    with ledger.transaction() as conn:
        reused = conn.execute("SELECT generation_id,job_key FROM reader_text_artifacts WHERE request_id=?",
                              (request_id,)).fetchone()
        if reused and (reused[0] != ledger.generation_id or reused[1] != job_key):
            raise ReadingError("text_request_identity_conflict")
        cached = conn.execute("SELECT * FROM reader_text_artifacts WHERE generation_id=? AND job_key=?",
                              (ledger.generation_id, job_key)).fetchone()
        if cached and cached["status"] in {"running", "unknown"}:
            raise ReadingError("text_provider_outcome_unknown")
        if cached and cached["status"] == "failed" and (
                cached["request_id"] == request_id or validation_failure_codes):
            raise ReadingError("text_request_already_failed")
        if not cached or cached["status"] != "accepted":
            conn.execute("INSERT INTO reader_text_artifacts VALUES (?,?,?,?,?,'running','') "
                         "ON CONFLICT(generation_id,job_key) DO UPDATE SET request_id=excluded.request_id,status='running'",
                         (ledger.generation_id, job_key, request_id, route, prompt_version))
    if cached and cached["status"] == "accepted":
        return validate(cached["artifact_json"])
    received = False
    validating = False
    try:
        result = provider.generate(prompt, request_id=request_id, generation_id=ledger.generation_id)
        received = True
        if provider_binding(result) != route:
            raise ReadingError("text_provider_route_changed")
        raw = result.text
        validating = True
        artifact = validate(raw)
    except Exception as exc:
        from .scheduling import not_dispatched
        status = ("pending" if not received and not_dispatched(exc, request_id)
                  else "unknown" if not received and getattr(exc, "outcome_unknown", True) else "failed")
        diagnostic = ""
        if validating and isinstance(exc, ReadingError) and exc.code in validation_failure_codes:
            diagnostic = json_text({"schema": "received-validation-failure-v1", "code": exc.code,
                "generation_id": ledger.generation_id, "job_key": job_key, "request_id": request_id,
                "provider_route": route, "prompt_version": prompt_version})
        with ledger.transaction() as conn:
            conn.execute("UPDATE reader_text_artifacts SET status=?,artifact_json=? WHERE generation_id=? AND job_key=?",
                         (status, diagnostic, ledger.generation_id, job_key))
        raise
    with ledger.transaction() as conn:
        conn.execute("UPDATE reader_text_artifacts SET status='accepted',artifact_json=? WHERE generation_id=? AND job_key=?",
                     (artifact, ledger.generation_id, job_key))
    return artifact


def _token(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", value):
        raise ReadingError("invalid_projection_token")
    return value


def anchor(value: str) -> str:
    return "wgo-" + fingerprint(value)[:24]


def wikilink(path: str, alias: str = "", *, block: str = "", embed: bool = False,
             table: bool = False) -> str:
    if any(c in path + block for c in "\r\n|[]"):
        raise ReadingError("invalid_projection_link")
    target = path.removesuffix(".md") + ("#^" + block if block else "")
    label = redact_urls_in_text(alias).replace("[", "（").replace("]", "）").replace("\n", " ")
    label = label.replace("|", "／")
    separator = "\\|" if table else "|"
    return ("!" if embed else "") + "[[" + target + (separator + label if label else "") + "]]"


def _drop_duplicate_title_heading(body: str, title: str, *, verbatim=()) -> str:
    """Remove a leading H1 that only repeats the publisher-owned note title.

    The publisher writes the note title itself, so a model-echoed H1 at the
    very start of the body is redundant. Only an exact leading match is
    dropped; other headings, blockquoted headings, indented code and fenced
    code stay untouched.
    """
    heading = re.sub(r"\s+", " ", title).strip()
    if not body or not heading:
        return body
    first, newline, rest = body.partition("\n")
    if not first.startswith("# "):
        return body
    if first[2:].strip() != heading or any(first in fragment for fragment in verbatim):
        return body
    return rest.lstrip("\n") if newline else ""


@dataclass(frozen=True)
class EvidenceExcerpt:
    id: str
    text: str
    context: str = ""
    source_time: str = ""
    known_at: str = ""
    label: str = "来源"
    speaker_id: str | None = None
    speaker_name: str | None = None


EVIDENCE_MARKER = re.compile(r"\{\{evidence:([^{}\r\n]+)\}\}")
MARKDOWN_CONTRACT = (
    "Each text is a complete Markdown block, not necessarily one prose paragraph. Choose headings, "
    "unordered or ordered lists, comparison tables, blockquotes, inline code or fenced code when they "
    "make this material easier to read; ordinary prose is equally valid. Do not force every page into "
    "a template or compress away conditions to fit a table. Tables distinguish independent people/cases "
    "and explicitly mark missing values unknown; do not combine separate cases into one success. "
    "Use blockquotes only for exact supplied source wording, with attribution outside the quotation. "
    "Each contiguous blockquote must come from one supplied excerpt. Keep separate messages in separate "
    "quotes with their own citations; never join them into a single purported original passage. "
    "A captured speaker is the message writer/forwarder, not automatically a quoted author, mentioned "
    "person or subject of an experience. Preserve captured readable names where they explain an "
    "experience, stance, correction or story; never infer identity from matching names or assign "
    "credibility scores to people. Unknown quote authors remain unknown; label relayed words as relayed. "
    "Optionally place {{evidence:<exact evidence id>}} beside a claim, after a quote or in a table cell; "
    "the publisher resolves the link. Use outer pipes on table rows, and keep code fences balanced. "
    "No executable command links, control markers or fabricated evidence."
)


def _markdown_lines(text):
    fence = None
    for line in text.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            yield line, True
            if match and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
                fence = None
        elif match:
            fence = (match[1][0], len(match[1]))
            yield line, True
        else:
            yield line, False
    if fence:
        raise ReadingError("markdown_unclosed_code_fence")


def _quote_groups(text):
    groups, current = [], []
    for index, (line, code) in enumerate(_markdown_lines(text)):
        quote = re.match(r"^ {0,3}> ?(.*)$", line) if not code else None
        if quote:
            current.append((index, quote[1]))
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _named_attribution_ids(line, excerpts):
    """Cited evidence ids when a following line attributes the quote by name.

    ``—— 阿甲{{evidence:e1}}`` is a conventional attributed quotation: the
    prose names the captured speaker of that exact supplied excerpt, so the
    quotation binds to the cited evidence. Only supplied ``speaker_name``
    labels qualify, so an ordinary following sentence that merely carries a
    citation stays a separate claim instead of becoming quote attribution.
    """
    if not excerpts:
        return frozenset()
    cited = {match[1] for match in EVIDENCE_MARKER.finditer(line)}
    if not cited:
        return frozenset()
    label = (EVIDENCE_MARKER.sub("", line).strip().lstrip("—–-·•* \u3000")
             .rstrip("：:，,。. \u3000"))
    if not label or any(mark in label for mark in "。！？!?；;"):
        return frozenset()
    names = {excerpt.speaker_name for excerpt in excerpts.values() if excerpt.speaker_name}
    if label not in names:
        return frozenset()
    if any(excerpts[id_].speaker_name != label for id_ in cited):
        raise ReadingError("markdown_quote_attribution_mismatch")
    return frozenset(cited)


def _quote_references(text, group, excerpts=None):
    quoted = "\n".join(line for _, line in group)
    references = {match[1] for match in EVIDENCE_MARKER.finditer(quoted)}
    # A citation-only line immediately after a quotation is its attribution.
    # It must support that quotation, not merely another sentence in the block.
    following = text.splitlines()[group[-1][0] + 1:]
    next_line = next((line for line in following if line.strip()), "")
    if not EVIDENCE_MARKER.sub("", next_line).strip(" ·"):
        references.update(match[1] for match in EVIDENCE_MARKER.finditer(next_line))
    else:
        references.update(_named_attribution_ids(next_line, excerpts))
    return references


def _markdown_blocks(text):
    """Locate the supported Markdown leaves; compound bodies remain links.

    This is not an HTML renderer. It keeps anchors out of citation footers and
    admits an embed only when the complete body is one known Markdown block.
    """
    lines = list(_markdown_lines(text))
    tables = {}
    for index, (line, code) in enumerate(lines):
        cells = line.strip().strip("|").split("|")
        if (index and not code and len(cells) >= 2
                and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells)
                and not lines[index - 1][1] and re.search(r"(?<!\\)\|", lines[index - 1][0])):
            end = index + 1
            while end < len(lines) and not lines[end][1] and re.search(r"(?<!\\)\|", lines[end][0]):
                end += 1
            tables[index - 1] = end
    def listing(line):
        match = re.match(r"^ {0,3}([-+*]|\d+[.)])\s+", line)
        return ("ordered" if match[1][0].isdigit() else "unordered") if match else None
    def special(index):
        line, code = lines[index]
        return (code or index in tables or listing(line) or re.match(r"^ {0,3}(?:>|#{1,6}\s)", line))
    blocks, index = [], 0
    while index < len(lines):
        line, code = lines[index]
        if not line.strip():
            index += 1
            continue
        start, index = index, index + 1
        if start in tables:
            kind, index = "table", tables[start]
        elif code:
            kind = "code"
            opener = re.match(r"^ {0,3}(`{3,}|~{3,})", line)[1]
            while index < len(lines):
                closing = re.fullmatch(r" {0,3}" + re.escape(opener[0]) + "{" + str(len(opener)) + r",}\s*", lines[index][0])
                index += 1
                if closing:
                    break
        elif re.match(r"^ {0,3}>", line):
            kind = "quote"
            while index < len(lines) and re.match(r"^ {0,3}>", lines[index][0]):
                index += 1
        elif listing(line):
            kind, list_kind = "list", listing(line)
            while index < len(lines):
                following = lines[index][0]
                if not following.strip():
                    next_index = next((i for i in range(index + 1, len(lines)) if lines[i][0].strip()), len(lines))
                    if (next_index == len(lines) or not (listing(lines[next_index][0]) == list_kind
                            or re.match(r"^(?: {2,}|\t)", lines[next_index][0]))):
                        break
                    index = next_index
                    continue
                if following.startswith(("  ", "\t")) or listing(following) == list_kind:
                    index += 1
                elif special(index):
                    break
                else:  # A contiguous plain line may be a lazy list continuation.
                    index += 1
        elif re.match(r"^ {0,3}#{1,6}\s", line):
            kind = "heading"
        else:
            kind = "paragraph"
            while index < len(lines) and lines[index][0].strip() and not special(index):
                index += 1
        blocks.append((start, index, kind))
    return blocks


def validate_markdown_evidence(text, excerpts, *, check_quotes=True):
    """Validate references/quotations while preserving ordinary Markdown."""
    for line, code in _markdown_lines(text):
        if code:
            continue
        matches = list(EVIDENCE_MARKER.finditer(line))
        if (any(match[1] not in excerpts for match in matches)
                or "{{evidence:" in EVIDENCE_MARKER.sub("", line)):
            raise ReadingError("markdown_invalid_evidence_marker")
        if re.search(r"(?:\]\(|<)\s*(?:javascript:|data:|obsidian://.*(?:command|execute))", line, re.I):
            raise ReadingError("markdown_executable_link")
    if check_quotes:
        for group in _quote_groups(text):
            quote = EVIDENCE_MARKER.sub("", "\n".join(line for _, line in group)).strip()
            refs = _quote_references(text, group, excerpts)
            candidates = [excerpts[id_] for id_ in refs] if refs else excerpts.values()
            supported = [quote in redact_urls_in_text(excerpt.text) for excerpt in candidates]
            if quote and not (all(supported) if refs else any(supported)):
                raise ReadingError("markdown_quote_not_grounded")


def render_markdown_evidence(text, excerpts, resolve, *, block_anchor=""):
    """Resolve optional inline citations without breaking tables or quotes."""
    used, result = set(), []
    groups = {group[-1][0]: group for group in _quote_groups(text)}
    blocks = _markdown_blocks(text)
    tables = {index for start, end, kind in blocks if kind == "table" for index in range(start, end)}
    primary = next((block for block in blocks if block[2] in {"table", "list", "quote", "code"}),
                   next((block for block in blocks if block[2] != "heading"), blocks[0] if blocks else None))
    for index, (line, code) in enumerate(_markdown_lines(text)):
        if not code:
            table = index in tables
            def link(match):
                used.add(match[1])
                return resolve(match[1], table)
            line = EVIDENCE_MARKER.sub(link, line)
        result.append(line)
        if block_anchor and primary and index == primary[1] - 1:
            result.extend(("", "^" + block_anchor, ""))
        if index in groups:
            group = groups[index]
            quoted = "\n".join(value for _, value in group)
            if _quote_references(text, group, excerpts):
                continue
            quoted = quoted.strip()
            matches = [id_ for id_, excerpt in excerpts.items() if quoted and quoted in redact_urls_in_text(excerpt.text)]
            if matches:
                result.extend(("", " · ".join(resolve(id_, False) for id_ in matches), ""))
                used.update(matches)
    return "\n".join(result), used


def evidence_alias(excerpt):
    return excerpt.speaker_name + "的消息" if excerpt.speaker_name else "来源"


def _contains_verbatim(text, fragment):
    if fragment in text:
        return True
    # Adding blockquote prefixes is presentation, not a change to the source
    # wording. Remove only that prefix, never list ordinals or code content.
    unquoted = "\n".join(re.sub(r"^ {0,3}> ?", "", line) if not code else line
                          for line, code in _markdown_lines(text))
    return fragment in EVIDENCE_MARKER.sub("", unquoted)


@dataclass(frozen=True)
class ReadingUnit:
    id: str
    text: str
    evidence_ids: tuple[str, ...]
    resource_ids: tuple[str, ...] = ()
    role: str = "narrative"
    verbatim: tuple[str, ...] = ()
    status: str = "current"
    reason: str = ""


@dataclass(frozen=True)
class ReadingResource:
    id: str
    label: str
    local_path: str | None = None
    status: str = "pending"


@dataclass(frozen=True)
class ReadingRelation:
    id: str
    type: str
    other_item_id: str
    reason: str
    direction: str = "outgoing"
    other_title: str = ""


@dataclass(frozen=True)
class ReadingSnapshot:
    item_id: str
    content_revision: int
    title: str
    semantic_at: str
    units: tuple[ReadingUnit, ...]
    evidence: tuple[EvidenceExcerpt, ...]
    resources: tuple[ReadingResource, ...] = ()
    relations: tuple[ReadingRelation, ...] = ()
    kind: str = "item"


class CanonicalReadingAdapter:
    """Read the actual SemanticStore API; no Item/source tables are created."""

    def __init__(self, store, *, context_chars: int = 160):
        if type(context_chars) is not int or not 0 <= context_chars <= 2000:
            raise ReadingError("invalid_reading_context_bound")
        self.store, self.context_chars = store, context_chars

    def aliases(self, item_id: str) -> tuple[str, ...]:
        with closing(self.store.connect()) as conn:
            return tuple(row[0] for row in conn.execute("SELECT new_item_id FROM item_aliases WHERE old_item_id=?", (item_id,)))

    def evidence_span_ids(self, identities: Iterable[str]) -> tuple[str, ...]:
        """Resolve exact canonical relation evidence, without ranked search."""
        spans = set()
        with closing(self.store.connect()) as conn:
            for identity in sorted(set(identities)):
                spans.update(row[0] for row in conn.execute(
                    "SELECT span_id FROM source_spans WHERE span_id=? OR source_version_id=? "
                    "UNION SELECT span_id FROM observation_evidence WHERE observation_id=?", (identity, identity, identity)))
                claim = conn.execute("SELECT span_ids_json FROM claims WHERE claim_id=?", (identity,)).fetchone()
                if claim:
                    spans.update(json.loads(claim[0]))
        return tuple(sorted(spans))

    def current_revision(self, object_id: str) -> int | None:
        if object_id.startswith("source:"):
            object_id = object_id.removeprefix("source:").rsplit(":", 1)[0]
        with closing(self.store.connect()) as conn:
            item = conn.execute("SELECT content_revision,lifecycle FROM items WHERE item_id=?", (object_id,)).fetchone()
            if item is not None:
                return item["content_revision"]
            if object_id.startswith("brief:") and conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reader_brief_editions'").fetchone():
                row = conn.execute("SELECT content_revision FROM reader_brief_editions WHERE generation_id=? AND edition_id=?",
                                   (self.store.generation_id, object_id)).fetchone()
                return row[0] if row else None
        return None

    @staticmethod
    def _time(value) -> str:
        if type(value) not in {int, float}:
            raise ReadingError("invalid_canonical_timestamp")
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError) as exc:
            raise ReadingError("invalid_canonical_timestamp") from exc

    def snapshot(self, item_id: str) -> ReadingSnapshot:
        from .attribution import source_attribution
        store_revision = self.store.read_revision()
        item = self.store.get_item(item_id)
        if item is None or item["lifecycle"] != "active":
            raise ReadingError("canonical_reading_item_unavailable")
        with closing(self.store.connect()) as conn:
            resources = [dict(row) for row in conn.execute(
                "SELECT DISTINCT r.* FROM resource_refs r WHERE r.item_id=? OR r.observation_id IN "
                "(SELECT observation_id FROM item_observations WHERE item_id=?) ORDER BY r.resource_id,r.occurrence_id",
                (item_id, item_id))]
            claim_relations = [dict(row) for row in conn.execute(
                "SELECT DISTINCT r.* FROM claim_relations r JOIN item_claims i "
                "ON i.claim_id=r.to_claim_id OR i.claim_id=r.from_claim_id WHERE i.item_id=? ORDER BY r.rowid", (item_id,))]
            claim_reasons = {row["to_claim_id"]: row["reason"] for row in claim_relations}
            # A correction can be recorded in another Item (or an observation
            # not yet assigned to an Item). Its exact source remains readable
            # beside this Item's historical statement without copying a body.
            relation_ids = {id_ for row in claim_relations for id_ in json.loads(row["evidence_json"])}
            relation_ids.update(row[key] for row in claim_relations for key in ("from_claim_id", "to_claim_id"))
            relation_ids.update(id_ for row in item["relations"] for id_ in row["evidence"])
            relation_spans = self.evidence_span_ids(relation_ids)
            extra_spans = [dict(row) for id_ in sorted(relation_spans)
                           for row in conn.execute("SELECT * FROM source_spans WHERE span_id=?", (id_,))]
        resource_by_id = {}
        resources_by_observation: dict[str, list[str]] = {}
        for resource in resources:
            metadata = json.loads(resource["metadata_json"])
            label = next((metadata[key] for key in ("label", "original_name")
                          if isinstance(metadata.get(key), str) and metadata[key].strip()), None)
            label = label or {"file": "文件", "image": "图片附件", "link": "链接", "voice": "语音", "video": "视频"}.get(
                resource["purpose"], resource["purpose"] or "关联资源")
            converted = ReadingResource(resource["resource_id"], label,
                                        resource["location"], resource["status"])
            previous = resource_by_id.get(converted.id)
            if previous is None or (previous.status != "available" and converted.status == "available"):
                resource_by_id[converted.id] = converted
            if resource["observation_id"]:
                resources_by_observation.setdefault(resource["observation_id"], []).append(converted.id)
        excerpts, units = {}, []
        sources = {}
        for span in [span for observation in item["observations"] for span in observation["spans"]] + extra_spans:
            source_id = span["source_version_id"]
            if source_id not in sources:
                sources[source_id] = self.store.get_source(source_id)
            source = sources[source_id]
            if source is None:
                raise ReadingError("canonical_reading_source_missing")
            visible = redact_urls_in_text(source["text"])
            start, end = span["start"], span["end"]
            if visible[start:end] != span["excerpt"]:
                raise ReadingError("canonical_reading_span_mismatch")
            context = visible[max(0, start - self.context_chars):min(len(visible), end + self.context_chars)]
            if context == span["excerpt"]:
                context = ""
            grade_labels = {"raw_exact": "原文准确片段", "excerpt_only": "保留摘录（上下文范围不完整）",
                            "derived_summary": "派生摘要", "manual_annotation": "个人批注"}
            grade = source["provenance_grade"]
            if grade not in grade_labels:
                raise ReadingError("unknown_source_provenance")
            attribution = source_attribution(source["source_namespace"], source["chat_id"], source["payload"])
            excerpts[span["span_id"]] = EvidenceExcerpt(span["span_id"], span["excerpt"], context,
                self._time(source["source_sent_at"]) if source["source_sent_at"] else "",
                self._time(source["captured_at"]), grade_labels[grade], **attribution)
        for observation in item["observations"]:
            observation_resources = tuple(dict.fromkeys(resources_by_observation.get(observation["observation_id"], ())))
            if observation.get("role") == "evidence_only":
                continue
            for index, unit in enumerate(observation["units"]):
                affected_claims = [claim for claim in item["claims"] if claim["status"] in {"retracted", "contested"}
                                   and set(unit["span_ids"]).intersection(claim["span_ids"])]
                status = ("retracted" if any(c["status"] == "retracted" for c in affected_claims)
                          else "contested" if affected_claims else "current")
                reason = "；".join(dict.fromkeys(claim_reasons.get(c["claim_id"], "") for c in affected_claims)).strip("；")
                # A quote unit identifies source wording, not an obligation to
                # copy an entire chat message into the reading body. Its exact
                # excerpt is already retained in the source projection. Keep
                # literal procedural/correction requirements separate from the
                # writer's choice of a meaningful quotation or paraphrase.
                retained = (unit["text"],) if unit["kind"] in {"step", "counterevidence"} or affected_claims else ()
                units.append(ReadingUnit(unit["unit_id"], unit["text"], tuple(unit["span_ids"]),
                                         observation_resources if index == 0 else (),
                                         "counterevidence" if affected_claims else unit["kind"], retained, status, reason))
        relations = []
        for relation in item["relations"]:
            outgoing = relation["from_item_id"] == item_id
            other_id = relation["to_item_id"] if outgoing else relation["from_item_id"]
            other = self.store.get_item(other_id)
            direction = "symmetric" if relation["direction"] == "symmetric" else ("outgoing" if outgoing else "incoming")
            relations.append(ReadingRelation(relation["relation_id"], relation["relation"], other_id,
                                             relation["reason"], direction, other["title"] if other else ""))
        if self.store.read_revision() != store_revision or self.current_revision(item_id) != item["content_revision"]:
            raise ReadingError("stale_projection_snapshot")
        return ReadingSnapshot(item_id, item["content_revision"], item["title"], self._time(item["semantic_updated_at"]),
                               tuple(units), tuple(excerpts.values()), tuple(resource_by_id.values()),
                               tuple(relations), item["kind"])


@dataclass(frozen=True)
class BodyBlock:
    id: str
    role: str
    text: str
    unit_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    resource_ids: tuple[str, ...] = ()
    primary_home: str = ""
    unit_statuses: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BodyPlan:
    item_id: str
    content_revision: int
    blocks: tuple[BodyBlock, ...]
    unit_fingerprints: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json_text(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "BodyPlan":
        try:
            value = json.loads(text)
            blocks = tuple(BodyBlock(
                id=b["id"], role=b["role"], text=b["text"],
                unit_ids=tuple(b["unit_ids"]), evidence_ids=tuple(b["evidence_ids"]),
                resource_ids=tuple(b.get("resource_ids", ())),
                primary_home=b["primary_home"],
                unit_statuses=b.get("unit_statuses", {}),
            ) for b in value["blocks"])
            return cls(value["item_id"], value["content_revision"], blocks,
                       value.get("unit_fingerprints", {}))
        except (ValueError, KeyError, TypeError) as exc:
            raise ReadingError("invalid_body_plan") from exc


def validate_body_plan(snapshot: ReadingSnapshot, plan: BodyPlan, *, check_markdown=True) -> BodyPlan:
    if (plan.item_id != snapshot.item_id or type(plan.content_revision) is not int
            or plan.content_revision != snapshot.content_revision):
        raise ReadingError("stale_body_plan")
    units = {u.id: u for u in snapshot.units}
    evidence = {e.id for e in snapshot.evidence}
    resources = {r.id for r in snapshot.resources}
    if len(units) != len(snapshot.units) or len(evidence) != len(snapshot.evidence):
        raise ReadingError("duplicate_snapshot_identity")
    assigned: list[str] = []
    ids: set[str] = set()
    for block in plan.blocks:
        _token(block.id)
        if (block.id in ids or not isinstance(block.text, str) or not block.text.strip()
                or not isinstance(block.role, str) or not block.role
                or block.primary_home != snapshot.item_id or not block.unit_ids
                or "<!-- wgo:" in block.text or "<!-- wgo-action:" in block.text
                or not set(block.unit_ids) <= units.keys()
                or not set(block.evidence_ids) <= evidence
                or not set(block.resource_ids) <= resources):
            raise ReadingError("invalid_body_plan")
        ids.add(block.id)
        assigned.extend(block.unit_ids)
        if check_markdown:
            validate_markdown_evidence(redact_urls_in_text(block.text),
                                       {e.id: e for e in snapshot.evidence if e.id in block.evidence_ids})
        for unit_id in block.unit_ids:
            unit = units[unit_id]
            if unit.status not in {"current", "retracted", "contested"}:
                raise ReadingError("invalid_unit_status")
            if (not set(unit.evidence_ids) <= set(block.evidence_ids)
                    or not set(unit.resource_ids) <= set(block.resource_ids)
                    or any(not _contains_verbatim(redact_urls_in_text(block.text), redact_urls_in_text(fragment))
                           for fragment in unit.verbatim)):
                raise ReadingError("body_plan_lost_evidence")
            if unit.status != "current" and (block.unit_statuses.get(unit_id) != unit.status
                    or block.role not in {"counterevidence", "historical", "correction"}):
                raise ReadingError("body_plan_lost_claim_status")
        if (not set(block.unit_statuses) <= set(block.unit_ids) or any(
                status != units[unit_id].status for unit_id, status in block.unit_statuses.items())):
            raise ReadingError("body_plan_invalid_claim_status")
    if sorted(assigned) != sorted(units):
        raise ReadingError("body_plan_unit_coverage")
    return BodyPlan(plan.item_id, plan.content_revision, plan.blocks,
                    {u.id: fingerprint(json_text(asdict(u))) for u in snapshot.units})


# Only received composition/validation errors are eligible. Stale caller
# envelopes, invalid canonical inputs and provider/transport errors are not.
_BODY_VALIDATION_FAILURES = frozenset({
    "invalid_body_plan", "invalid_projection_token", "body_plan_unit_coverage",
    "body_plan_lost_evidence", "body_plan_lost_claim_status", "body_plan_invalid_claim_status",
    "body_plan_changed_stable_home", "body_plan_changed_unaffected_block", "body_fragment_changed_verbatim",
    "markdown_unclosed_code_fence", "markdown_quote_attribution_mismatch", "markdown_invalid_evidence_marker",
    "markdown_executable_link", "markdown_quote_not_grounded",
})


class BodyPlanWriter:
    def __init__(self, provider, generation_id: str, ledger=None, *, max_input_bytes=80000):
        if type(max_input_bytes) is not int or max_input_bytes < 8000:
            raise ReadingError("invalid_body_context_budget")
        self.provider = provider
        self.generation_id = generation_id
        self.ledger = ledger
        self.max_input_bytes = min(max_input_bytes, getattr(provider, "max_input_bytes", max_input_bytes))

    def plan(self, snapshot: ReadingSnapshot, *, previous: BodyPlan | None = None,
             request_id: str, ledger=None) -> BodyPlan:
        ledger = ledger or self.ledger
        if ledger is not None and ledger.generation_id != self.generation_id:
            raise ReadingError("reading_generation_mismatch")
        current_fingerprints = {u.id: fingerprint(json_text(asdict(u))) for u in snapshot.units}
        unchanged: dict[str, BodyBlock] = {}
        if previous is not None:
            if previous.item_id != snapshot.item_id:
                raise ReadingError("body_plan_item_mismatch")
            unchanged = {b.id: b for b in previous.blocks if all(
                current_fingerprints.get(u) == previous.unit_fingerprints.get(u)
                and u in current_fingerprints for u in b.unit_ids)}
            if (current_fingerprints == previous.unit_fingerprints
                    and set(current_fingerprints) == {u for b in previous.blocks for u in b.unit_ids}):
                return validate_body_plan(snapshot, BodyPlan(
                    snapshot.item_id, snapshot.content_revision, previous.blocks))
        if ledger is not None:
            # A version/budget change may turn a page into fragments (or vice
            # versa). Such repartitioning must not bypass an unresolved child
            # invocation of this stable caller-owned request.
            prefixes = (request_id + ":page:", request_id + ":group:", request_id + ":repair:")
            unresolved = ledger.one(
                "SELECT 1 FROM reader_text_artifacts WHERE generation_id=? "
                "AND status IN ('running','unknown') AND prompt_version LIKE 'body-%' "
                "AND (request_id=? OR substr(request_id,1,?)=? OR substr(request_id,1,?)=? "
                "OR substr(request_id,1,?)=?) LIMIT 1",
                (self.generation_id, request_id, len(prefixes[0]), prefixes[0],
                 len(prefixes[1]), prefixes[1], len(prefixes[2]), prefixes[2]))
            if unresolved:
                raise ReadingError("text_provider_outcome_unknown")
        prompt_text = self._prompt(snapshot, previous, unchanged)
        if len(prompt_text.encode("utf-8")) > self.max_input_bytes:
            return self._paged(snapshot, previous, current_fingerprints, unchanged, request_id, ledger)
        return self._invoke(snapshot, previous, current_fingerprints, unchanged, prompt_text, request_id, ledger)

    @staticmethod
    def _prompt(snapshot, previous, unchanged, **context):
        # Canonical unit order expresses retained responsibilities, and can
        # combine observations committed in a different order from the source.
        # Give the writer an explicit evidence chronology without reordering
        # units, changing their indices or manufacturing dates for old excerpts.
        dated, undated = [], []
        for evidence in snapshot.evidence:
            try:
                instant = datetime.fromisoformat(evidence.source_time)
                if instant.utcoffset() is None:
                    raise ValueError
            except (TypeError, ValueError):
                undated.append(evidence.id)
            else:
                dated.append((instant, evidence.id))
        dated.sort(key=lambda entry: entry[0])
        prompt = {
            "task": "把材料编辑成值得再读的 Obsidian 笔记：让读者读事情本身，技术与生活内容同样认真对待。"
                    "只返回 schema 要求的 JSON 对象 {blocks:[...]}。required_unit_indices 每个序号在 unit_indices "
                    "中恰好一次，归入稳定 block；重叠单元同属一个 block，正文合写一次。程序绑定身份、revision、"
                    "证据、资源与状态；不输出内部 ID、处理说明、frontmatter 或 H1。\n"
                    "按事情的内在关系组织正文：技术页直接解释问题、办法、结果和条件；生活页从具体经历或场景"
                    "进入，连起有意味的反应和后文。把同一层意思整合在一处，保留不同人的经历与判断。短事一段"
                    "即可，只为独立主题分节；不用逐条发言、人物清单或摘要模板。一事只讲一次。单元是内容责任，"
                    "不是必抄的话：感谢、接话、发图动作仅在理解需要时提及，实质细节、条件、步骤和反证不可丢。\n"
                    "只用所供材料，不补背景、因果、动机、感受、结果或结尾启示。疑问、猜测、转述保持其性质，"
                    "只在影响理解时说明不确定性，不写取证审计旁白。保留称呼、归属、代码与术语；speaker_name "
                    "即使为‘您’也是来源称呼，不是在叫读者。性别不明时用名字。发言者、经历主体和引文作者分清。"
                    "保留资源引用；URL slug 不证明网页标题、作者或内容，文件名也不证明未读图文内容。\n"
                    "精确消息在来源页，quote-role 可忠实转述或选用准确措辞，不照搬聊天包装。短引文句内呈现，"
                    "长引文用带归属的 blockquote，不先概括再重复全文。unit.verbatim 每段必须逐字出现在其 home，"
                    "含虚词、标点和术语，适用于显式原话、步骤、反证与更正；自然接入正文，不另外重复解释。\n"
                    "source_sequence 按已知 source_time 排列证据，同刻不代表因果；undated_evidence 先后未知。"
                    "units 序号和入库次序不是事件先后；可按主题组织，但‘接着/此前/最后’须符合来源时间。"
                    "日期时间仅在理解需要时写；semantic_at 不是事件时间，具体时分须带 source_time 的 UTC offset，"
                    "不换成未声明的本机时区。\n"
                    "增量保留已有 block ID 与 retained unit/evidence/resource 的原 home；新细节回答或限定原点时，"
                    "改写接收它的自动段落，独立部分可新建 block。未收到新增或变化单元的 block 保持原字节。"
                    "手改冲突由程序处理。retracted/contested 用 correction/historical/counterevidence role "
                    "明确 changed status，保留原话，不继续当当前结论。\n"
                    + MARKDOWN_CONTRACT,
            "schema": {"blocks": [{"id": "stable-block-id", "role": "narrative",
                                   "text": "complete Markdown block", "unit_indices": [0]}] if snapshot.units else []},
            "writing_units": [{"index": index, "unit_id": unit.id} for index, unit in enumerate(snapshot.units)],
            "source_sequence": [id_ for _instant, id_ in dated],
            "undated_evidence": undated,
            "required_unit_indices": list(range(len(snapshot.units))),
            "snapshot": asdict(snapshot),
            "previous": asdict(previous) if previous else None,
            "unchanged_blocks": list(unchanged),
            **context,
        }
        return redact_urls_in_text(json_text(prompt))

    @staticmethod
    def _response(snapshot, raw, *, check_markdown=True):
        """Bind a composition choice to the supplied canonical snapshot.

        Full BodyPlans remain readable for durable replay and existing injected
        callers. The smaller writer response selects unit positions only; it
        cannot choose canonical identity, revision, evidence or resource metadata.
        """
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"blocks"}:
                return BodyPlan.from_json(raw)
            if not isinstance(value["blocks"], list):
                raise ValueError
            blocks = []
            for block in value["blocks"]:
                if not isinstance(block, dict) or set(block) != {"id", "role", "text", "unit_indices"}:
                    raise ValueError
                indices = block["unit_indices"]
                if not isinstance(indices, list) or any(type(i) is not int or not 0 <= i < len(snapshot.units) for i in indices):
                    raise ValueError
                units = [snapshot.units[i] for i in indices]
                markers = ({m[1] for line, code in _markdown_lines(block["text"]) if not code
                            for m in EVIDENCE_MARKER.finditer(line)} if check_markdown else set())
                blocks.append(BodyBlock(block["id"], block["role"], block["text"],
                    tuple(unit.id for unit in units),
                    tuple(dict.fromkeys([e for unit in units for e in unit.evidence_ids] + sorted(markers))),
                    tuple(dict.fromkeys(r for unit in units for r in unit.resource_ids)), snapshot.item_id,
                    {unit.id: unit.status for unit in units if unit.status != "current"}))
            return BodyPlan(snapshot.item_id, snapshot.content_revision, tuple(blocks))
        except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            raise ReadingError("invalid_body_plan") from exc

    def _invoke(self, snapshot, previous, current_fingerprints, unchanged, prompt_text, request_id, ledger,
                *, exact_fragment=None, canonical_new_ids=False):
        if len(prompt_text.encode("utf-8")) > self.max_input_bytes:
            raise ReadingError("body_context_budget_exceeded")
        def validate(raw):
            plan = self._response(snapshot, raw, check_markdown=exact_fragment is None)
            if canonical_new_ids:
                retained = {b.id for b in previous.blocks} if previous else set()
                plan = replace(plan, blocks=tuple(b if b.id in retained else replace(b,
                    id="paragraph-" + fingerprint(json_text([snapshot.item_id, sorted(b.unit_ids)]))[:24]) for b in plan.blocks))
            result = self._validate_increment(snapshot, previous, current_fingerprints, unchanged, plan,
                                               check_markdown=exact_fragment is None)
            if exact_fragment is not None and (len(result.blocks) != 1 or result.blocks[0].text != exact_fragment):
                raise ReadingError("body_fragment_changed_verbatim")
            return result.to_json()
        if ledger is not None:
            try:
                artifact = accepted_text_artifact(ledger, self.provider, prompt=prompt_text, request_id=request_id,
                    prompt_version="body-plan-v10", validate=validate,
                    validation_failure_codes=_BODY_VALIDATION_FAILURES)
            except ReadingError as exc:
                if exc.code not in _BODY_VALIDATION_FAILURES | {"text_request_already_failed"}:
                    raise
                artifact = self._repair_received_validation(ledger, prompt_text, validate)
                if artifact is None:
                    raise
            return BodyPlan.from_json(artifact)
        route = provider_binding(self.provider)
        result = self.provider.generate(prompt_text, request_id=request_id, generation_id=self.generation_id)
        if provider_binding(result) != route:
            raise ReadingError("text_provider_route_changed")
        return BodyPlan.from_json(validate(result.text))

    def _repair_received_validation(self, ledger, prompt_text, validate):
        """One durable child of the exact failed invocation, never a fresh retry.

        The failed row is immutable and only our received-validation diagnostic
        proves eligibility. Historical empty failures cannot acquire that proof
        on resume. The normal artifact ledger owns dispatch and replay of the
        child, so an interrupted/failed repair cannot create another attempt.
        """
        route = provider_binding(self.provider)
        job_key = fingerprint(json_text([ledger.generation_id, "body-plan-v10", route, prompt_text]))
        failed = ledger.one("SELECT * FROM reader_text_artifacts WHERE generation_id=? AND job_key=?",
                            (ledger.generation_id, job_key))
        if not failed or failed["status"] != "failed":
            return None
        try:
            diagnostic = json.loads(failed["artifact_json"])
        except (TypeError, ValueError):
            return None
        if (not isinstance(diagnostic, dict) or not isinstance(diagnostic.get("code"), str)
                or diagnostic["code"] not in _BODY_VALIDATION_FAILURES):
            return None
        expected = {"schema": "received-validation-failure-v1", "code": diagnostic["code"],
            "generation_id": ledger.generation_id, "job_key": job_key, "request_id": failed["request_id"],
            "provider_route": route, "prompt_version": "body-plan-v10"}
        if diagnostic != expected:
            return None
        prompt = json.loads(prompt_text)
        # Replace only the generic introduction, retaining every concrete
        # writing/validation instruction and all supplied context. This shorter
        # prefix keeps even a full bounded page within its original input limit.
        intro, task = prompt["task"].split("只返回 schema", 1)
        repair_intro = "修复 " + diagnostic["code"] + "；完整重写。\n"
        if diagnostic["code"] == "body_plan_lost_evidence":
            repair_intro = "修复 body_plan_lost_evidence；原 home 中逐字复制 unit.verbatim。\n"
        if len(repair_intro.encode("utf-8")) > len(intro.encode("utf-8")):
            raise ReadingError("body_context_budget_exceeded")
        prompt["task"] = repair_intro + "只返回 schema" + task
        repair_prompt = json_text(prompt)
        if len(repair_prompt.encode("utf-8")) > self.max_input_bytes:
            raise ReadingError("body_context_budget_exceeded")
        child = failed["request_id"] + ":repair:" + fingerprint(json_text([job_key, failed["request_id"]]))
        return accepted_text_artifact(ledger, self.provider, prompt=repair_prompt, request_id=child,
            prompt_version="body-plan-v10", validate=validate,
            validation_failure_codes=_BODY_VALIDATION_FAILURES)

    @staticmethod
    def _partial(snapshot, units):
        evidence = {id_ for unit in units for id_ in unit.evidence_ids}
        resources = {id_ for unit in units for id_ in unit.resource_ids}
        return replace(snapshot, units=tuple(units), evidence=tuple(e for e in snapshot.evidence if e.id in evidence),
                       resources=tuple(r for r in snapshot.resources if r.id in resources), relations=())

    def _paged(self, snapshot, previous, fingerprints, unchanged, request_id, ledger):
        binding = fingerprint(json_text(asdict(snapshot)))
        route = provider_binding(self.provider)
        aggregate_key = fingerprint(json_text([binding, asdict(previous) if previous else None,
                                               route, self.max_input_bytes, "body-paged-v10"]))
        if ledger:
            reused = ledger.one("SELECT * FROM reader_text_artifacts WHERE request_id=?", (request_id,))
            if reused and (reused["generation_id"] != self.generation_id or reused["job_key"] != aggregate_key):
                raise ReadingError("text_request_identity_conflict")
            cached = ledger.one("SELECT * FROM reader_text_artifacts WHERE generation_id=? AND job_key=?",
                                (self.generation_id, aggregate_key))
            if cached and cached["status"] == "accepted":
                return self._validate_increment(snapshot, previous, fingerprints, unchanged,
                                                BodyPlan.from_json(cached["artifact_json"]))
        homes = {unit: block for block in previous.blocks for unit in block.unit_ids} if previous else {}
        previous_by_id = {b.id: b for b in previous.blocks} if previous else {}
        groups, by_group = [], {}
        last_home = None
        for unit in snapshot.units:
            block = homes.get(unit.id)
            # Consecutive new details can extend the preceding stable paragraph.
            prior_home = previous_by_id.get(last_home)
            compatible = (unit.status == "current" and prior_home and (unit.role == prior_home.role
                          or {unit.role, prior_home.role} <= {"scene", "narrative"}))
            key = block.id if block else last_home if compatible else "new-" + unit.id
            if block:
                last_home = block.id
            if key not in by_group:
                by_group[key] = []
                groups.append((key, by_group[key]))
            by_group[key].append(unit)
        accepted, pending, page_number = [], [], 0

        def page_parts(batch):
            units = tuple(unit for _key, group in batch for unit in group)
            partial = self._partial(snapshot, units)
            blocks = tuple(previous_by_id[key] for key, _group in batch if key in previous_by_id)
            prior = BodyPlan(snapshot.item_id, snapshot.content_revision, blocks,
                             {u.id: previous.unit_fingerprints[u.id] for u in units if previous and u.id in previous.unit_fingerprints}) if blocks else None
            fixed = {key: unchanged[key] for key, _group in batch if key in unchanged}
            prompt = self._prompt(partial, prior, fixed, snapshot_binding=binding, phase="body_page_v1")
            return partial, prior, fixed, prompt

        def flush():
            nonlocal pending, page_number
            if not pending:
                return
            partial, prior, fixed, prompt = page_parts(pending)
            part = self._invoke(partial, prior, {u.id: fingerprints[u.id] for u in partial.units}, fixed,
                                prompt, request_id + ":page:" + str(page_number), ledger, canonical_new_ids=True)
            accepted.extend(part.blocks)
            page_number += 1
            pending = []

        for key, group in groups:
            old = previous_by_id.get(key)
            if key in unchanged and set(u.id for u in group) == set(old.unit_ids):
                flush()
                accepted.append(old)
                continue
            if len(page_parts(pending + [(key, group)])[3].encode("utf-8")) <= self.max_input_bytes:
                pending.append((key, group))
                continue
            flush()
            if len(page_parts([(key, group)])[3].encode("utf-8")) <= self.max_input_bytes:
                pending.append((key, group))
            else:
                accepted.append(self._fragment_group(snapshot, group, old, key in unchanged, binding,
                    request_id + ":group:" + str(page_number), ledger))
                page_number += 1
        flush()
        positions = {unit.id: index for index, unit in enumerate(snapshot.units)}
        accepted.sort(key=lambda block: min(positions[id_] for id_ in block.unit_ids))
        result = self._validate_increment(snapshot, previous, fingerprints, unchanged,
                                         BodyPlan(snapshot.item_id, snapshot.content_revision, tuple(accepted)))
        if ledger:
            with ledger.transaction() as conn:
                conn.execute("INSERT OR IGNORE INTO reader_text_artifacts VALUES(?,?,?,?,?,'accepted',?)",
                    (self.generation_id, aggregate_key, request_id, route, "body-paged-v10", result.to_json()))
        return result

    @staticmethod
    def _pieces(value, limit):
        # UTF-8 boundaries remain intact, including one very long unbroken line.
        parts, start, size = [], 0, 0
        for index, char in enumerate(value):
            width = len(char.encode("utf-8"))
            if size + width > limit:
                parts.append(value[start:index])
                start, size = index, 0
            size += width
        if start < len(value):
            parts.append(value[start:])
        return parts

    def _fragment_group(self, snapshot, units, previous, preserve_previous, binding, request_id, ledger):
        """An oversized paragraph is an explicit lossless continuation task.

        The program joins exact local text segments under one stable home. Each
        evidence/context segment is supplied on a bounded page, including pages
        with no narrative output. No bytes or evidence associations are elided.
        """
        limit = max(256, (self.max_input_bytes - 5000) // 5)
        excerpts = {e.id: e for e in snapshot.evidence}
        text_parts, counter = [], 0
        if preserve_previous:
            text_parts.append(previous.text)
        for unit in units:
            if preserve_previous and unit.id in previous.unit_ids:
                continue
            local_text = self._pieces(redact_urls_in_text(unit.text), limit)
            fragments = []
            for id_ in unit.evidence_ids:
                excerpt = excerpts[id_]
                fragments.extend(replace(excerpt, text=part, context="") for part in self._pieces(redact_urls_in_text(excerpt.text), limit))
                fragments.extend(replace(excerpt, text="", context=part) for part in self._pieces(redact_urls_in_text(excerpt.context), limit))
            # Resources are rendered by the program from the original snapshot;
            # providers receive every associated descriptor as bounded context.
            resources = [r for r in snapshot.resources if r.id in unit.resource_ids]
            output = []
            for index in range(max(len(local_text), len(fragments), len(resources))):
                piece = local_text[index] if index < len(local_text) else None
                evidence = (fragments[index],) if index < len(fragments) else ()
                resource = (resources[index],) if index < len(resources) else ()
                local_id = unit.id + ":fragment:" + str(index)
                local_units = (replace(unit, id=local_id, text=piece, evidence_ids=tuple(e.id for e in evidence),
                    resource_ids=tuple(r.id for r in resource), verbatim=(piece,)),) if piece is not None else ()
                partial = replace(snapshot, units=local_units, evidence=evidence, resources=resource, relations=())
                prompt = self._prompt(partial, None, {}, snapshot_binding=binding, phase="body_fragment_v1",
                    continuation={"original_unit_id": unit.id, "index": index,
                        "unit_segments": len(local_text), "evidence_segments": len(fragments),
                        "instruction": "Return exactly this supplied text segment as one block, with no prefix, suffix or paraphrase. "
                        "This is one local continuation of an oversized paragraph, not a separate summary. "
                        "If there is no unit, inspect the supplied evidence/resource context and return blocks: []. "
                        "The program retains all original evidence and joins every segment at its original stable home."})
                part = self._invoke(partial, None, {}, {}, prompt, request_id + ":fragment:" + str(counter),
                                    ledger, exact_fragment=piece)
                counter += 1
                if piece is not None:
                    output.append(part.blocks[0].text)
            assembled = "".join(output)
            if assembled != redact_urls_in_text(unit.text):
                raise ReadingError("body_fragment_coverage")
            # Additional supplied verbatim fragments may be independent of the
            # unit text. Retain them once, without inventing a summary section.
            for fragment in unit.verbatim:
                visible = redact_urls_in_text(fragment)
                if visible not in assembled:
                    assembled += "\n\n" + visible
            text_parts.append(assembled)
        statuses = {u.id: u.status for u in units if u.status != "current"}
        return BodyBlock(previous.id if previous else "paragraph-" + fingerprint(units[0].id)[:24],
            previous.role if preserve_previous else "counterevidence" if statuses else units[0].role,
            "\n\n".join(text_parts), tuple(u.id for u in units), tuple(dict.fromkeys(e for u in units for e in u.evidence_ids)),
            tuple(dict.fromkeys(r for u in units for r in u.resource_ids)), snapshot.item_id, statuses)

    @staticmethod
    def _validate_increment(snapshot, previous, current_fingerprints, unchanged, plan, *, check_markdown=True):
        generated = {b.id: b for b in plan.blocks}
        if previous:
            for block in previous.blocks:
                retained_units = set(block.unit_ids).intersection(current_fingerprints)
                if retained_units and (block.id not in generated or not retained_units <= set(generated[block.id].unit_ids)):
                    raise ReadingError("body_plan_changed_stable_home")
        new_units = set(current_fingerprints) - set(previous.unit_fingerprints if previous else ())
        for id_, block in unchanged.items():
            updated = generated.get(id_)
            if updated == block:
                continue
            added = set(updated.unit_ids) - set(block.unit_ids) if updated else set()
            if (updated is None or not added or not added <= new_units
                    or not set(block.unit_ids) <= set(updated.unit_ids)
                    or updated.role != block.role
                    or not set(block.evidence_ids) <= set(updated.evidence_ids)
                    or not set(block.resource_ids) <= set(updated.resource_ids)):
                raise ReadingError("body_plan_changed_unaffected_block")
        return validate_body_plan(snapshot, plan, check_markdown=check_markdown)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reader_projections (
 generation_id TEXT NOT NULL, object_id TEXT NOT NULL, path TEXT NOT NULL,
 content_revision INTEGER NOT NULL, render_revision INTEGER NOT NULL,
 annotation_revision INTEGER NOT NULL, semantic_at TEXT NOT NULL,
 region TEXT NOT NULL, machine_body TEXT NOT NULL, machine_hash TEXT NOT NULL,
 file_hash TEXT NOT NULL, annotation_hash TEXT NOT NULL, status TEXT NOT NULL,
 PRIMARY KEY(generation_id, object_id));
CREATE TABLE IF NOT EXISTS reader_projection_history (
 generation_id TEXT NOT NULL, object_id TEXT NOT NULL, render_revision INTEGER NOT NULL,
 machine_body TEXT NOT NULL, machine_hash TEXT NOT NULL, file_hash TEXT NOT NULL,
 PRIMARY KEY(generation_id, object_id, render_revision));
CREATE TABLE IF NOT EXISTS reader_body_plans (
 generation_id TEXT NOT NULL, item_id TEXT NOT NULL, content_revision INTEGER NOT NULL,
 plan_json TEXT NOT NULL, PRIMARY KEY(generation_id, item_id));
CREATE TABLE IF NOT EXISTS reader_text_artifacts (
 generation_id TEXT NOT NULL,job_key TEXT NOT NULL,request_id TEXT NOT NULL,
 provider_route TEXT NOT NULL,prompt_version TEXT NOT NULL,status TEXT NOT NULL,artifact_json TEXT NOT NULL,
 PRIMARY KEY(generation_id,job_key));
CREATE TABLE IF NOT EXISTS reader_source_locations (
 generation_id TEXT NOT NULL, item_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
 page_number INTEGER NOT NULL, PRIMARY KEY(generation_id,item_id,evidence_id));
CREATE TABLE IF NOT EXISTS reader_source_fragments (
 generation_id TEXT NOT NULL,item_id TEXT NOT NULL,evidence_id TEXT NOT NULL,
 fragment TEXT NOT NULL,fragment_hash TEXT NOT NULL,
 PRIMARY KEY(generation_id,item_id,evidence_id));
CREATE TABLE IF NOT EXISTS reader_proposals (
 proposal_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL, object_id TEXT NOT NULL,
 content_revision INTEGER NOT NULL, file_hash TEXT NOT NULL, path TEXT NOT NULL,
 region TEXT NOT NULL, proposed_body TEXT NOT NULL, semantic_at TEXT NOT NULL,
 title TEXT NOT NULL, status TEXT NOT NULL, backup_path TEXT NOT NULL, draft_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reader_proposal_guards (
 proposal_id TEXT PRIMARY KEY,guards_json TEXT NOT NULL);
"""


class ReadingLedger:
    """Consumer state on the canonical generation connection factory."""

    def __init__(self, connect: Callable[[], sqlite3.Connection], generation_id: str):
        self.connect_factory = connect
        self.generation_id = _token(generation_id)
        with self.connection() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_manifest'").fetchone():
                generations = [row[0] for row in conn.execute("SELECT generation_id FROM generation_manifest")]
                if generations != [generation_id]:
                    raise ReadingError("reading_generation_mismatch")
        self.install(_SCHEMA)

    @contextmanager
    def connection(self):
        conn = self.connect_factory()
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def install(self, schema: str):
        with self.connection() as conn:
            conn.executescript(schema)
            conn.commit()

    def one(self, sql: str, parameters=()):
        with self.connection() as conn:
            return conn.execute(sql, parameters).fetchone()

    def all(self, sql: str, parameters=()):
        with self.connection() as conn:
            return conn.execute(sql, parameters).fetchall()


def _frontmatter(text: str) -> tuple[str, str, str]:
    """Split without parsing or normalizing unknown YAML, including tags."""
    match = re.match(r"\A---(\r?\n)(.*?)^---[ \t]*(?:\r?\n|$)", text, re.S | re.M)
    if not match:
        if text.startswith("---\n") or text.startswith("---\r\n"):
            raise ReadingError("invalid_frontmatter_boundary")
        return "", text, "\n"
    return match.group(2), text[match.end():], match.group(1)


def _yaml_entries(frontmatter: str) -> list[tuple[str | None, str]]:
    entries: list[tuple[str | None, str]] = []
    for line in frontmatter.splitlines(keepends=True):
        match = re.match(r"^('(?:[^']|'')+'|\"(?:[^\"\\]|\\.)+\"|[\w-]+):(?:[ \t]|\r?$)", line)
        if match:
            key = match.group(1)
            if key.startswith('"'):
                try:
                    key = json.loads(key)
                except ValueError as exc:
                    raise ReadingError("invalid_frontmatter_key") from exc
            elif key.startswith("'"):
                key = key[1:-1].replace("''", "'")
            entries.append((key, line))
        elif entries and (line[:1].isspace() or not line.strip()):
            key, old = entries[-1]
            entries[-1] = (key, old + line)
        else:
            entries.append((None, line))
    return entries


def property_present(text: str, key: str) -> bool:
    fm, _body, _nl = _frontmatter(text)
    return any(k == key for k, _value in _yaml_entries(fm))


def property_value(text: str, key: str):
    fm, _body, _nl = _frontmatter(text)
    found = [value for k, value in _yaml_entries(fm) if k == key]
    if len(found) > 1:
        raise ReadingError("duplicate_frontmatter_property")
    if not found:
        return None
    value = found[0].split(":", 1)[1].strip()
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        if "\n" in value or "\r" in value or value.startswith(("|", ">", "[", "{", "!", "&", "*")):
            raise ReadingError("unsupported_control_property")
        if value.startswith("'") and value.endswith("'"):
            return value[1:-1].replace("''", "'")
        if value.lower() in {"true", "false"}:
            return value.lower() == "true"
        return value


def patch_properties(text: str, values: dict, *, machine_only: bool = True) -> str:
    if any(not re.fullmatch(r"[\w-]+", key) or (machine_only and not key.startswith("wgo_"))
           for key in values):
        raise ReadingError("invalid_owned_property")
    fm, body, nl = _frontmatter(text)
    seen: set[str] = set()
    result = []
    for key, raw in _yaml_entries(fm):
        if key in values:
            if key in seen:
                raise ReadingError("duplicate_frontmatter_property")
            result.append(key + ": " + json_text(values[key]) + nl)
            seen.add(key)
        else:
            result.append(raw)
    result.extend(key + ": " + json_text(value) + nl for key, value in values.items() if key not in seen)
    return "---" + nl + "".join(result) + "---" + nl + body


def _machine_markers(text: str):
    """Recognize only standalone, unfenced machine markers."""
    offset, fence_char, fence_length = 0, "", 0
    markers = []
    for line in text.splitlines(keepends=True):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*?)(?:\r?\n)?$", line)
        if fence:
            chars, rest = fence.groups()
            if not fence_char:
                fence_char, fence_length = chars[0], len(chars)
            elif chars[0] == fence_char and len(chars) >= fence_length and not rest.strip():
                fence_char, fence_length = "", 0
        elif not fence_char:
            match = re.fullmatch(r"(<!-- wgo:(begin|end) ([A-Za-z0-9_-]+) -->)(?:\r?\n)?", line)
            if match:
                markers.append((match.group(2), match.group(3), offset, offset + len(match.group(1))))
        offset += len(line)
    return markers


def _region_bounds(text: str, region: str):
    _token(region)
    begins = [m for m in _machine_markers(text) if m[:2] == ("begin", region)]
    ends = [m for m in _machine_markers(text) if m[:2] == ("end", region)]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or begins[0][3] > ends[0][2]:
        raise ReadingError("invalid_machine_region")
    return begins[0][3], ends[0][2]


def region_body(text: str, region: str) -> str | None:
    bounds = _region_bounds(text, region)
    return text[bounds[0]:bounds[1]] if bounds else None


def patch_region(text: str, region: str, body: str) -> str:
    bounds = _region_bounds(text, region)
    begin, end = f"<!-- wgo:begin {region} -->", f"<!-- wgo:end {region} -->"
    if bounds is None:
        return text + ("\n" if text and not text.endswith("\n") else "") + "\n" + begin + body + end + "\n"
    return text[:bounds[0]] + body + text[bounds[1]:]


def annotation_fingerprint(text: str, region: str) -> str:
    fm, body, _nl = _frontmatter(text)
    annotation_fm = "".join(raw for key, raw in _yaml_entries(fm)
                            if key is None or not key.startswith("wgo_"))
    if region_body(body, region) is not None:
        body = patch_region(body, region, "")
    return fingerprint(annotation_fm + body)


@dataclass(frozen=True)
class ProjectionReceipt:
    object_id: str
    content_revision: int
    status: str
    path: str
    file_hash: str = ""
    proposal_id: str = ""
    render_revision: int = 0


_ROOT_LOCKS: dict[str, threading.RLock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


class ProjectionPublisher:
    MAX_SOURCE_PAGE_CHARS = 24000
    MAX_SOURCE_PAGE_EXCERPTS = 32

    def __init__(self, ledger: ReadingLedger, root: str | Path, *,
                 current_revision: Callable[[str], int | None],
                 resource_roots: Iterable[str | Path] = (), file_lock=None,
                 aliases: Callable[[str], tuple[str, ...]] | None = None):
        self.ledger = ledger
        candidate = Path(root).absolute()
        if any(path.is_symlink() for path in (candidate, *candidate.parents)):
            raise ReadingError("projection_symlink")
        self.root = candidate.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.current_revision = current_revision
        self.aliases = aliases or (lambda _id: ())
        self.resource_roots = tuple(Path(p).resolve() for p in resource_roots) or (self.root,)
        self.file_lock = file_lock or create_file_lock()
        with _ROOT_LOCKS_GUARD:
            self._thread_lock = _ROOT_LOCKS.setdefault(str(self.root), threading.RLock())
        self._lock_local = threading.local()
        self._safe_path(".wgo", directory=True)

    @classmethod
    def for_store(cls, store, root, **kwargs):
        adapter = CanonicalReadingAdapter(store)
        return cls(ReadingLedger(store.connect, store.generation_id), root,
                   current_revision=adapter.current_revision, aliases=adapter.aliases, **kwargs)

    def _safe_path(self, relative: str, *, directory: bool = False, create_parents: bool = True) -> Path:
        pure = Path(relative)
        if pure.is_absolute() or not pure.parts or any(p in {"..", "."} for p in pure.parts):
            raise ReadingError("projection_path_escape")
        current = self.root
        if any(path.is_symlink() for path in (current, *current.parents)):
            raise ReadingError("projection_symlink")
        for index, part in enumerate(pure.parts):
            current = current / part
            if current.is_symlink():
                raise ReadingError("projection_symlink")
            is_dir = index < len(pure.parts) - 1 or directory
            if current.exists() and ((is_dir and not current.is_dir()) or
                                     (not is_dir and not current.is_file())):
                raise ReadingError("projection_non_regular")
            if is_dir and not current.exists() and create_parents:
                current.mkdir(mode=0o700)
        return current

    @contextmanager
    def locked(self):
        with self._thread_lock:
            if getattr(self._lock_local, "held", False):
                yield
                return
            path = self._safe_path(".wgo/projection.lock")
            with self.file_lock.acquire(path, mode=LockMode.EXCLUSIVE, blocking=True):
                self._lock_local.held = True
                try:
                    yield
                finally:
                    self._lock_local.held = False

    def read(self, path: str) -> bytes | None:
        safe = self._safe_path(path, create_parents=False)
        if not safe.exists():
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(safe, flags)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ReadingError("projection_non_regular")
                return stream.read()
        except OSError as exc:
            raise ReadingError("projection_read_failed") from exc

    def atomic_write(self, path: str, payload: bytes, *, expected: str | None) -> bool:
        target = self._safe_path(path)
        before = self.read(path)
        if (fingerprint(before) if before is not None else None) != expected:
            raise ReadingError("projection_file_changed")
        if before == payload:
            return False
        fd, temporary = tempfile.mkstemp(prefix=".wgo-", dir=target.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._safe_path(path)
            latest = self.read(path)
            if (fingerprint(latest) if latest is not None else None) != expected:
                raise ReadingError("projection_file_changed")
            os.replace(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True

    def _find_id(self, object_id: str) -> str | None:
        matches: list[str] = []
        for directory, names, files in os.walk(self.root, followlinks=False):
            names[:] = [n for n in names if n != ".wgo"]
            for name in names:
                if (Path(directory) / name).is_symlink():
                    raise ReadingError("projection_symlink")
            for filename in files:
                if not filename.endswith(".md"):
                    continue
                path = (Path(directory) / filename).relative_to(self.root).as_posix()
                raw = self.read(path)
                try:
                    value = property_value(raw.decode("utf-8"), "wgo_id")
                except (ReadingError, UnicodeError):
                    # An unrelated user note need not be parseable. A recorded
                    # target still undergoes strict parsing during publication.
                    continue
                if value == object_id:
                    matches.append(path)
        if len(matches) > 1:
            raise ReadingError("projection_identity_collision")
        return matches[0] if matches else None

    def resolve(self, object_id: str) -> str | None:
        with self.locked():
            # Resolution is read-only, including when a caller already holds
            # the generation write transaction. Publication records the alias.
            visited = set()
            while True:
                if object_id in visited:
                    raise ReadingError("projection_alias_cycle")
                visited.add(object_id)
                successors = self.aliases(object_id)
                if not successors:
                    return self._find_id(object_id)
                if len(successors) != 1:
                    hub = self._find_id(object_id)
                    if hub is None:
                        raise ReadingError("projection_alias_ambiguous")
                    return hub
                object_id = successors[0]

    def record(self, object_id: str):
        return self.ledger.one("SELECT * FROM reader_projections WHERE generation_id=? AND object_id=?",
                               (self.ledger.generation_id, object_id))

    def ready(self, object_id: str, revision: int | None = None) -> bool:
        try:
            with self.locked():
                row = self.record(object_id)
                if row is None or row["status"] != "ready" or (
                        revision is not None and row["content_revision"] != revision):
                    return False
                latest = self.current_revision(object_id)
                if latest is not None and latest != row["content_revision"]:
                    return False
                if self.ledger.one("SELECT proposal_id FROM reader_proposals WHERE generation_id=? "
                                   "AND object_id=? AND content_revision=? AND status='pending' LIMIT 1",
                                   (self.ledger.generation_id, object_id, row["content_revision"])):
                    return False
                path = self._find_id(object_id)
                if not path:
                    return False
                text = self.read(path).decode("utf-8")
                body = region_body(text, row["region"])
                return (body is not None and fingerprint(body) == row["machine_hash"]
                        and property_value(text, "wgo_content_revision") == row["content_revision"]
                        and property_value(text, "wgo_generation") == self.ledger.generation_id)
        except (ReadingError, UnicodeError):
            return False

    def file_hash(self, object_id: str) -> str:
        path = self.resolve(object_id)
        if path is None:
            raise ReadingError("projection_missing")
        return fingerprint(self.read(path))

    def load_plan(self, item_id: str) -> BodyPlan | None:
        row = self.ledger.one("SELECT plan_json FROM reader_body_plans WHERE generation_id=? AND item_id=?",
                              (self.ledger.generation_id, item_id))
        return BodyPlan.from_json(row[0]) if row else None

    def publish_document(self, *, object_id: str, path: str, title: str, body: str,
                         content_revision: int, semantic_at: str = "", region: str = "body",
                         is_current: Callable[[], bool] | None = None,
                         authorized_hash: str | None = None,
                         proposal_guards: dict | None = None,
                         _proposal_resolution: tuple[str, str] | None = None,
                         _personal_append: str = "") -> ProjectionReceipt:
        """Publish a prepared document. authorized_hash is only for validated controls."""
        _token(region)
        if _machine_markers(body):
            raise ReadingError("nested_machine_region")
        prepared = "\n" + redact_urls_in_text(body).strip("\n") + "\n"
        with self.locked(), self.ledger.transaction() as conn:
            if is_current is not None and not is_current():
                raise ReadingError("stale_projection_snapshot")
            row = conn.execute("SELECT * FROM reader_projections WHERE generation_id=? AND object_id=?",
                               (self.ledger.generation_id, object_id)).fetchone()
            found = self._find_id(object_id)
            target = found or (row["path"] if row else path)
            raw = self.read(target)
            if raw is None:
                text = "# " + redact_urls_in_text(title).replace("\n", " ") + "\n"
                current_body = None
            else:
                try:
                    text = raw.decode("utf-8")
                    current_body = region_body(text, region)
                    identity = property_value(text, "wgo_id")
                except (ReadingError, UnicodeError) as exc:
                    raise ReadingError("projection_unreadable_ownership") from exc
                if identity not in {None, object_id}:
                    raise ReadingError("projection_path_collision")
            expected_hash = fingerprint(raw) if raw is not None else None
            trusted = row is not None and current_body is not None and (
                fingerprint(current_body) == row["machine_hash"])
            authorized = authorized_hash is not None and expected_hash == authorized_hash
            if _personal_append and not authorized:
                raise ReadingError("personal_append_requires_bound_action")
            if raw is not None and ((not trusted and not authorized) or
                                    (row and row["status"] == "personal" and not authorized)):
                retained = conn.execute("SELECT proposal_id FROM reader_proposals WHERE generation_id=? "
                    "AND object_id=? AND content_revision=? AND file_hash=? AND proposed_body=? "
                    "AND status='keep_personal'", (self.ledger.generation_id, object_id,
                     content_revision, expected_hash, prepared)).fetchone()
                if retained:
                    return ProjectionReceipt(object_id, content_revision, "personal", target, expected_hash)
                return self._proposal(conn, object_id, target, raw, prepared, region, title,
                                      content_revision, semantic_at, proposal_guards or {})
            render_revision = (row["render_revision"] if row else 0) + int(
                row is None or row["machine_body"] != prepared or
                row["content_revision"] != content_revision or row["status"] != "ready")
            if _personal_append:
                text += _personal_append
            annotation_hash = annotation_fingerprint(text, region)
            annotation_revision = (row["annotation_revision"] if row else 0) + int(
                row is not None and annotation_hash != row["annotation_hash"])
            updated = patch_properties(text, {
                "wgo_id": object_id, "wgo_generation": self.ledger.generation_id,
                "wgo_content_revision": content_revision, "wgo_render_revision": render_revision,
                "wgo_annotation_revision": annotation_revision, "wgo_semantic_at": semantic_at,
            })
            updated = patch_region(updated, region, prepared)
            if row:
                conn.execute("INSERT OR IGNORE INTO reader_projection_history VALUES (?,?,?,?,?,?)",
                             (self.ledger.generation_id, object_id, row["render_revision"],
                              row["machine_body"], row["machine_hash"], row["file_hash"]))
            changed = self.atomic_write(target, updated.encode("utf-8"), expected=expected_hash)
            file_hash = fingerprint(updated)
            conn.execute("""INSERT INTO reader_projections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(generation_id,object_id) DO UPDATE SET
                path=excluded.path, content_revision=excluded.content_revision,
                render_revision=excluded.render_revision,annotation_revision=excluded.annotation_revision,
                semantic_at=excluded.semantic_at,region=excluded.region,machine_body=excluded.machine_body,
                machine_hash=excluded.machine_hash,file_hash=excluded.file_hash,
                annotation_hash=excluded.annotation_hash,status=excluded.status""",
                         (self.ledger.generation_id, object_id, target, content_revision, render_revision,
                          annotation_revision, semantic_at, region, prepared, fingerprint(prepared),
                          file_hash, annotation_fingerprint(updated, region), "ready"))
            if _proposal_resolution is not None:
                proposal_id, choice = _proposal_resolution
                updated_proposal = conn.execute("UPDATE reader_proposals SET status=? WHERE proposal_id=? "
                    "AND status='pending' AND file_hash=? AND content_revision=?",
                    (choice, proposal_id, authorized_hash, content_revision))
                if updated_proposal.rowcount != 1:
                    raise ReadingError("stale_proposal")
                conn.execute("UPDATE reader_proposals SET status='superseded' WHERE generation_id=? AND object_id=? "
                             "AND proposal_id<>? AND status='pending' AND content_revision<=?",
                             (self.ledger.generation_id, object_id, proposal_id, content_revision))
            return ProjectionReceipt(object_id, content_revision, "published" if changed else "unchanged",
                                     target, file_hash, render_revision=render_revision)

    def _proposal(self, conn, object_id, path, raw, body, region, title, revision, semantic_at, guards):
        file_hash = fingerprint(raw)
        existing = conn.execute("""SELECT * FROM reader_proposals WHERE generation_id=? AND
            object_id=? AND content_revision=? AND file_hash=? AND proposed_body=? AND status='pending'""",
                                (self.ledger.generation_id, object_id, revision, file_hash, body)).fetchone()
        serialized_guards = json_text(guards)
        if existing:
            old_guards = conn.execute("SELECT guards_json FROM reader_proposal_guards WHERE proposal_id=?",
                                      (existing["proposal_id"],)).fetchone()
            if (old_guards[0] if old_guards else "{}") != serialized_guards:
                conn.execute("UPDATE reader_proposals SET status='superseded' WHERE proposal_id=?", (existing["proposal_id"],))
                existing = None
        if existing:
            proposal_id = existing["proposal_id"]
        else:
            proposal_id = uuid.uuid4().hex
            backup = f".wgo/backups/{proposal_id}.md"
            draft = f".wgo/pending/{proposal_id}.md"
            self.atomic_write(backup, raw, expected=None)
            text = raw.decode("utf-8")
            proposed = patch_region(patch_properties(text, {
                "wgo_id": object_id, "wgo_generation": self.ledger.generation_id,
                "wgo_content_revision": revision,
            }), region, body)
            self.atomic_write(draft, proposed.encode("utf-8"), expected=None)
            conn.execute("INSERT INTO reader_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (proposal_id, self.ledger.generation_id, object_id, revision, file_hash,
                          path, region, body, semantic_at, title, "pending", backup, draft))
            conn.execute("INSERT INTO reader_proposal_guards VALUES (?,?)", (proposal_id, serialized_guards))
        status_path = ".wgo/status/" + anchor(object_id) + ".md"
        status = "# 待处理\n\n此笔记有个人改写，原文已保全。请在 WGO/管理 中选择处理方式。\n"
        previous = self.read(status_path)
        self.atomic_write(status_path, status.encode("utf-8"),
                          expected=fingerprint(previous) if previous is not None else None)
        return ProjectionReceipt(object_id, revision, "conflict", path, file_hash, proposal_id)

    def pending_proposals(self):
        return self.ledger.all("SELECT * FROM reader_proposals WHERE generation_id=? AND status='pending'",
                               (self.ledger.generation_id,))

    def proposal_is_current(self, proposal_id: str) -> bool:
        row = self.ledger.one("SELECT * FROM reader_proposals WHERE proposal_id=? AND generation_id=?",
                              (proposal_id, self.ledger.generation_id))
        if row is None or row["status"] != "pending":
            return False
        guards_row = self.ledger.one("SELECT guards_json FROM reader_proposal_guards WHERE proposal_id=?", (proposal_id,))
        guards = json.loads(guards_row[0]) if guards_row else {}
        if any(self.current_revision(item_id) != revision for item_id, revision in guards.get("revisions", {}).items()):
            return False
        for item_id, binding in guards.get("items", {}).items():
            if self.current_revision(item_id) != binding[2]:
                return False
            preference = self.ledger.one("SELECT revision,mute FROM reader_preferences WHERE generation_id=? AND item_id=?",
                                          (self.ledger.generation_id, item_id))
            known = self.ledger.one("SELECT MAX(content_revision) FROM reader_known WHERE generation_id=? AND item_id=?",
                                   (self.ledger.generation_id, item_id))[0] or 0
            if (preference and preference[1]) or (preference[0] if preference else 0) != binding[0] or known != binding[1]:
                return False
        for prop in guards.get("properties", ()):
            target = self._find_id(prop["object_id"])
            if target is None or property_value(self.read(target).decode("utf-8"), prop["key"]) != prop["value"]:
                return False
        object_id = row["object_id"]
        if object_id.startswith("source:"):
            current = self.current_revision(object_id.removeprefix("source:").rsplit(":", 1)[0])
        else:
            current = self.current_revision(object_id)
        if object_id.startswith(("brief:", "control:", "query-history:", "view:")):
            latest = self.ledger.one("SELECT MAX(content_revision) FROM reader_proposals WHERE generation_id=? "
                                     "AND object_id=? AND status='pending'", (self.ledger.generation_id, object_id))[0]
            return latest == row["content_revision"] and (current is None or current <= latest)
        return current == row["content_revision"]

    def resolve_proposal(self, proposal_id: str, choice: str, *, expected_revision: int,
                         expected_file_hash: str) -> ProjectionReceipt:
        if choice not in {"adopt", "move_notes", "keep_personal"}:
            raise ReadingError("invalid_proposal_choice")
        with self.locked():
            row = self.ledger.one("SELECT * FROM reader_proposals WHERE proposal_id=? AND generation_id=?",
                                  (proposal_id, self.ledger.generation_id))
            if row is None or row["status"] != "pending" or row["content_revision"] != expected_revision:
                raise ReadingError("stale_proposal")
            path = self._find_id(row["object_id"]) or row["path"]
            raw = self.read(path)
            if (raw is None or row["file_hash"] != expected_file_hash
                    or fingerprint(raw) != expected_file_hash
                    or not self.proposal_is_current(proposal_id)):
                raise ReadingError("stale_proposal")
            if choice == "keep_personal":
                with self.ledger.transaction() as conn:
                    conn.execute("UPDATE reader_proposals SET status=? WHERE proposal_id=?",
                                 (choice, proposal_id))
                    conn.execute("UPDATE reader_projections SET status='personal' WHERE generation_id=? AND object_id=?",
                                 (self.ledger.generation_id, row["object_id"]))
                    if conn.execute("SELECT 1 FROM reader_projections WHERE generation_id=? AND object_id=?",
                                    (self.ledger.generation_id, row["object_id"])).fetchone() is None:
                        text = raw.decode("utf-8")
                        body = region_body(text, row["region"]) or ""
                        conn.execute("INSERT INTO reader_projections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (self.ledger.generation_id, row["object_id"], path, expected_revision, 0, 0,
                             row["semantic_at"], row["region"], body, fingerprint(body), expected_file_hash,
                             annotation_fingerprint(text, row["region"]), "personal"))
                return ProjectionReceipt(row["object_id"], expected_revision, "personal", path, expected_file_hash)
            personal_append = ""
            if choice == "move_notes":
                text = raw.decode("utf-8")
                old = region_body(text, row["region"])
                if old is not None:
                    personal_append = "\n## 个人备注（保留改写）\n" + old
            receipt = self.publish_document(
                object_id=row["object_id"], path=path, title=row["title"], body=row["proposed_body"],
                content_revision=expected_revision, semantic_at=row["semantic_at"], region=row["region"],
                authorized_hash=expected_file_hash,
                _personal_append=personal_append,
                _proposal_resolution=(proposal_id, choice),
                is_current=lambda: self.proposal_is_current(proposal_id))
            return receipt

    def source_object_id(self, item_id: str, page: int) -> str:
        return f"source:{item_id}:{page}"

    def evidence_link(self, item_id: str, evidence_id: str, *, table: bool = False, alias: str = "来源") -> str:
        row = self.ledger.one("SELECT page_number FROM reader_source_locations WHERE generation_id=? AND item_id=? AND evidence_id=?",
                              (self.ledger.generation_id, item_id, evidence_id))
        if row is None:
            raise ReadingError("evidence_projection_missing")
        object_id = self.source_object_id(item_id, row[0])
        path = self.resolve(object_id)
        if path is None or not self.ready(object_id):
            raise ReadingError("evidence_projection_missing")
        if "\n^" + anchor(evidence_id) + "\n" not in self.read(path).decode("utf-8"):
            raise ReadingError("evidence_anchor_missing")
        return wikilink("WGO/" + path, alias, block=anchor(evidence_id), table=table)

    @staticmethod
    def _source_fragment(excerpt: EvidenceExcerpt, *, attribution: bool = True) -> str:
        def quoted(text):
            # Fencing preserves displayed source bytes while neutralizing
            # checkboxes, YAML, block anchors and our own machine markers.
            text = redact_urls_in_text(text)
            longest = max((len(m.group()) for m in re.finditer(r"`+", text)), default=0)
            fence = "`" * max(3, longest + 1)
            return fence + "text\n" + text + "\n" + fence
        label = redact_urls_in_text(excerpt.label).replace("\n", " ")
        speaker = ""
        if attribution:
            name = redact_urls_in_text(excerpt.speaker_name or "显示名未提供").replace("\n", " ").replace("\r", " ")
            name = re.sub(r"([\\`*_{}\[\]()#+.!|<>~-])", r"\\\1", name)
            speaker = "本条消息发言／转述者：" + name + "\n\n"
        content = speaker + quoted(excerpt.text)
        if attribution:
            content += "\n\n^" + anchor(excerpt.id)
        content += "\n\n必要上下文：\n" + quoted(excerpt.context) if excerpt.context else ""
        if not attribution:  # Exact pre-attribution renderer for one bounded render upgrade.
            content += "\n\n^" + anchor(excerpt.id)
        return f"### {label}\n\n来源时间：{excerpt.source_time or '未提供'}；获知时间：{excerpt.known_at or '未提供'}\n\n" + content

    def _publish_sources(self, snapshot: ReadingSnapshot):
        _token(snapshot.item_id)
        pages: dict[int, list[str]] = {}
        with self.ledger.transaction() as conn:
            if self.current_revision(snapshot.item_id) != snapshot.content_revision:
                raise ReadingError("stale_projection_snapshot")
            existing = {r["evidence_id"]: r["page_number"] for r in conn.execute(
                "SELECT * FROM reader_source_locations WHERE generation_id=? AND item_id=?",
                (self.ledger.generation_id, snapshot.item_id))}
            fragments = {r["evidence_id"]: (r["fragment"], r["fragment_hash"]) for r in conn.execute(
                "SELECT * FROM reader_source_fragments WHERE generation_id=? AND item_id=? ORDER BY rowid",
                (self.ledger.generation_id, snapshot.item_id))}
            if existing.keys() != fragments.keys():
                raise ReadingError("source_projection_cache_incomplete")
            page = max(existing.values(), default=1)
            for evidence_id, (fragment, _hash) in fragments.items():
                pages.setdefault(existing[evidence_id], []).append(fragment)
            for evidence in snapshot.evidence:
                fragment = self._source_fragment(evidence)
                if len(fragment) > self.MAX_SOURCE_PAGE_CHARS:
                    raise ReadingError("source_excerpt_too_large")
                if evidence.id in existing:
                    if fingerprint(fragment) != fragments[evidence.id][1]:
                        legacy = self._source_fragment(evidence, attribution=False)
                        old_fragment, old_hash = fragments[evidence.id]
                        if old_fragment != legacy or old_hash != fingerprint(legacy):
                            raise ReadingError("evidence_projection_identity_changed")
                        # Only this exact old renderer is upgraded. An altered
                        # excerpt/context still fails the immutable evidence check.
                        assigned = existing[evidence.id]
                        pages[assigned][pages[assigned].index(old_fragment)] = fragment
                        conn.execute("UPDATE reader_source_fragments SET fragment=?,fragment_hash=? "
                                     "WHERE generation_id=? AND item_id=? AND evidence_id=?",
                                     (fragment, fingerprint(fragment), self.ledger.generation_id,
                                      snapshot.item_id, evidence.id))
                    continue
                else:
                    while (len(pages.get(page, ())) >= self.MAX_SOURCE_PAGE_EXCERPTS or
                           sum(len(text) + 2 for text in pages.get(page, ())) + len(fragment) > self.MAX_SOURCE_PAGE_CHARS):
                        page += 1
                    assigned = page
                    conn.execute("INSERT INTO reader_source_locations VALUES (?,?,?,?)",
                                 (self.ledger.generation_id, snapshot.item_id, evidence.id, page))
                    conn.execute("INSERT INTO reader_source_fragments VALUES (?,?,?,?,?)",
                                 (self.ledger.generation_id, snapshot.item_id, evidence.id, fragment, fingerprint(fragment)))
                pages.setdefault(assigned, []).append(fragment)
        for page, fragments in sorted(pages.items()):
            rendered = "\n\n".join(fragments)
            if len(rendered) > self.MAX_SOURCE_PAGE_CHARS:
                raise ReadingError("source_page_too_large")
            receipt = self.publish_document(
                object_id=self.source_object_id(snapshot.item_id, page),
                path=f"来源/{snapshot.item_id}-{page:04d}.md", title=snapshot.title + " · 来源",
                body=rendered, content_revision=snapshot.content_revision, semantic_at=snapshot.semantic_at,
                is_current=lambda: self.current_revision(snapshot.item_id) == snapshot.content_revision)
            if receipt.status == "conflict":
                raise ReadingError("evidence_projection_conflict")

    def _resource_text(self, resource: ReadingResource) -> str:
        label = redact_urls_in_text(resource.label)
        if resource.status != "available" or not resource.local_path:
            return label + "（附件待到达）"
        candidate = Path(resource.local_path).absolute()
        accepted_root = next((r for r in self.resource_roots if candidate.is_relative_to(r)), None)
        if accepted_root is None:
            return label + "（附件待到达；本地位置未获准）"
        current = accepted_root
        for part in candidate.relative_to(accepted_root).parts:
            current /= part
            if current.is_symlink():
                return label + "（附件待到达）"
        if not candidate.is_file():
            return label + "（附件待到达）"
        return "[" + label.replace("]", "）") + "](<" + candidate.as_uri() + ">)"

    def publish_item(self, snapshot: ReadingSnapshot, plan: BodyPlan) -> ProjectionReceipt:
        plan = validate_body_plan(snapshot, plan)
        if self.current_revision(snapshot.item_id) != snapshot.content_revision:
            raise ReadingError("stale_projection_snapshot")
        # The accepted render plan survives a projection conflict. Adopting a
        # pending draft must leave its exact stable blocks available to Views.
        with self.ledger.transaction() as conn:
            if self.current_revision(snapshot.item_id) != snapshot.content_revision:
                raise ReadingError("stale_projection_snapshot")
            conn.execute("INSERT INTO reader_body_plans VALUES (?,?,?,?) ON CONFLICT(generation_id,item_id) "
                         "DO UPDATE SET content_revision=excluded.content_revision,plan_json=excluded.plan_json",
                         (self.ledger.generation_id, snapshot.item_id, snapshot.content_revision, plan.to_json()))
        self._publish_sources(snapshot)
        resources = {r.id: r for r in snapshot.resources}
        units = {u.id: u for u in snapshot.units}
        evidence = {e.id: e for e in snapshot.evidence}
        paragraphs = []
        for block in plan.blocks:
            text, placed = render_markdown_evidence(redact_urls_in_text(block.text),
                {id_: evidence[id_] for id_ in block.evidence_ids},
                lambda id_, table: self.evidence_link(snapshot.item_id, id_, table=table,
                                                       alias=evidence_alias(evidence[id_])), block_anchor=anchor(block.id))
            for unit_id in block.unit_ids:
                unit = units[unit_id]
                if unit.status != "current":
                    label = "已撤回的历史陈述" if unit.status == "retracted" else "存在争议的陈述"
                    text += "\n\n" + label + ("：" + redact_urls_in_text(unit.reason) if unit.reason else "，请结合本段更正和来源阅读。")
            refs = [self.evidence_link(snapshot.item_id, e, alias=evidence_alias(evidence[e]))
                    for e in block.evidence_ids if e not in placed]
            if len(refs) > 1:
                text += f"\n\n> [!quote]- 本段来源（{len(refs)} 条）\n" + "\n".join("> - " + ref for ref in refs)
            elif refs:
                text += "\n\n" + refs[0]
            resource_refs = [self._resource_text(resources[r]) for r in block.resource_ids]
            paragraphs.append(text + ("\n\n" + " · ".join(resource_refs) if resource_refs else ""))
        if snapshot.relations:
            lines = ["## 关联"]
            for relation in snapshot.relations:
                path = self.resolve(relation.other_item_id)
                other = (wikilink("WGO/" + path, relation.other_title or relation.other_item_id)
                         if path else (relation.other_title or relation.other_item_id) + "（投影待到达）")
                direction = {"incoming": "←", "symmetric": "↔"}.get(relation.direction, "→")
                lines.append(f"- {direction} {redact_urls_in_text(relation.type)} · {other}："
                             + redact_urls_in_text(relation.reason))
            paragraphs.append("\n".join(lines))
        placed_evidence = {evidence_id for block in plan.blocks for evidence_id in block.evidence_ids}
        remaining_evidence = [e for e in snapshot.evidence if e.id not in placed_evidence]
        if remaining_evidence:
            paragraphs.append("## 补充来源\n\n" + " · ".join(
                self.evidence_link(snapshot.item_id, e.id, alias=evidence_alias(e)) for e in remaining_evidence))
        placed_resources = {resource_id for block in plan.blocks for resource_id in block.resource_ids}
        remaining_resources = [r for r in snapshot.resources if r.id not in placed_resources]
        if remaining_resources:
            paragraphs.append("## 关联资源\n\n" + "\n".join("- " + self._resource_text(r) for r in remaining_resources))
        body = _drop_duplicate_title_heading("\n\n".join(paragraphs), snapshot.title,
            verbatim=tuple(fragment for unit in snapshot.units for fragment in unit.verbatim))
        receipt = self.publish_document(
            object_id=snapshot.item_id, path=f"条目/{_token(snapshot.item_id)}.md", title=snapshot.title,
            body=body, content_revision=snapshot.content_revision,
            semantic_at=snapshot.semantic_at,
            is_current=lambda: self.current_revision(snapshot.item_id) == snapshot.content_revision)
        return receipt

    def publish_canonical_item(self, store, item_id: str, writer: BodyPlanWriter, *, request_id: str) -> ProjectionReceipt:
        prepared = self.prepare_canonical_item(store, item_id, writer, request_id=request_id)
        return self.publish_prepared_item(store, prepared)

    def prepare_canonical_item(self, store, item_id: str, writer: BodyPlanWriter, *, request_id: str):
        """Retain accepted Body work outside the projection-root file lock."""
        if store.generation_id != self.ledger.generation_id:
            raise ReadingError("reading_generation_mismatch")
        item = store.get_item(item_id)
        if item and item["lifecycle"] in {"merged", "split", "restored"}:
            return (item_id, item["content_revision"], None, None)
        snapshot = CanonicalReadingAdapter(store).snapshot(item_id)
        plan = writer.plan(snapshot, previous=self.load_plan(item_id), request_id=request_id, ledger=self.ledger)
        return (item_id, snapshot.content_revision, snapshot, plan)

    def publish_prepared_item(self, store, prepared) -> ProjectionReceipt:
        """Serial publication rechecks the canonical revision after generation."""
        item_id, revision, snapshot, plan = prepared
        if store.generation_id != self.ledger.generation_id:
            raise ReadingError("reading_generation_mismatch")
        item = store.get_item(item_id)
        if item is None or item["content_revision"] != revision:
            raise ReadingError("stale_projection_revision")
        if snapshot is None:
            return self.publish_identity_redirect(store, item_id)
        receipt = self.publish_item(snapshot, plan)
        self._ack_published(store, item_id, snapshot.content_revision, receipt)
        return receipt

    def _ack_published(self, store, item_id, revision, receipt):
        if receipt.status not in {"published", "unchanged"} or not self.ready(item_id, revision):
            return
        with closing(store.connect()) as conn:
            outbox = conn.execute("SELECT * FROM projection_outbox WHERE owner_id=? AND revision=?",
                                  (item_id, revision)).fetchone()
        if outbox is None:
            return
        proof = outbox["receipt_id"] if outbox["status"] == "done" else "reading:" + fingerprint(
            json_text([self.ledger.generation_id, item_id, revision, receipt.file_hash]))
        if outbox["status"] != "done":
            store.ack_outbox("projection", outbox["outbox_id"], item_id, revision, proof)
        # The store separately proves that the latest revision is published.
        # Older pending projection rows are superseded, never falsely ACKed.
        store.supersede_projection_outbox(item_id, revision, proof)

    def publish_identity_redirect(self, store, item_id: str) -> ProjectionReceipt:
        item = store.get_item(item_id)
        if item is None or item["lifecycle"] not in {"merged", "split", "restored"} or not item["aliases"]:
            raise ReadingError("identity_redirect_unavailable")
        if item["lifecycle"] == "merged" and len(item["aliases"]) != 1:
            raise ReadingError("identity_redirect_invalid")
        links, target_revisions = [], {}
        for target_id in item["aliases"]:
            target = store.get_item(target_id)
            if target is None or not self.ready(target_id, target["content_revision"]):
                raise ReadingError("identity_target_not_ready")
            path = self.resolve(target_id)
            if path is None:
                raise ReadingError("identity_target_not_ready")
            target_revisions[target_id] = target["content_revision"]
            links.append("- " + wikilink("WGO/" + path, target["title"]))
        explanations = {"merged": "此条目已合并，后续内容在以下条目中继续。",
                        "split": "此条目已拆分。请根据要继续阅读的内容选择：",
                        "restored": "此条目的身份调整已恢复，继续阅读以下条目："}
        body = explanations[item["lifecycle"]] + "\n\n" + "\n".join(links)
        revision = item["content_revision"]
        def current():
            latest = store.get_item(item_id)
            return (latest is not None and latest["content_revision"] == revision and latest["aliases"] == item["aliases"]
                    and all(self.current_revision(id_) == rev and self.ready(id_, rev) for id_, rev in target_revisions.items()))
        receipt = self.publish_document(object_id=item_id, path=f"条目/{_token(item_id)}.md", title=item["title"], body=body,
                                        content_revision=revision,
                                        semantic_at=CanonicalReadingAdapter._time(item["semantic_updated_at"]), is_current=current)
        self._ack_published(store, item_id, revision, receipt)
        return receipt

    def publish_view(self, view_id: str, title: str, entries: Iterable[tuple[str, str, str]], *,
                     revision: int = 1, embed: bool = True) -> ProjectionReceipt:
        links, bindings = [], {}
        for item_id, block_id, label in entries:
            path = self.resolve(item_id)
            if path is None or not self.ready(item_id):
                raise ReadingError("view_target_not_ready")
            plan = self.load_plan(item_id)
            if block_id and (plan is None or block_id not in {b.id for b in plan.blocks}):
                raise ReadingError("view_block_missing")
            if block_id and "\n^" + anchor(block_id) + "\n" not in self.read(path).decode("utf-8"):
                raise ReadingError("view_block_missing")
            bindings[item_id] = (self.current_revision(item_id), path)
            complete_embed = embed
            if block_id:
                block = next(b for b in plan.blocks if b.id == block_id)
                complete_embed = embed and len(_markdown_blocks(block.text)) == 1 and not block.unit_statuses
            rendered = wikilink("WGO/" + path, label, block=anchor(block_id) if block_id else "", embed=complete_embed)
            if complete_embed and block_id:
                rendered += "\n\n" + " · ".join(self.evidence_link(item_id, id_) for id_ in block.evidence_ids)
            links.append(rendered)
        return self.publish_document(object_id="view:" + view_id, path="视图/" + _token(view_id) + ".md",
            title=title, body="\n\n".join(links), content_revision=revision,
            proposal_guards={"revisions": {id_: rev for id_, (rev, _path) in bindings.items()}},
            is_current=lambda: all(self.current_revision(id_) == rev and self.ready(id_) and self.resolve(id_) == path
                                   for id_, (rev, path) in bindings.items()))
