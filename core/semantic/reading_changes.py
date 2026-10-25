"""Explain concrete Item changes since a reader's explicit known version."""
from __future__ import annotations

from contextlib import closing
from collections import OrderedDict
from dataclasses import dataclass
import html
import json
import re
import threading

from core.url_safety import redact_urls_in_text
from .projections import (
    ProjectionReceipt, ReadingError, fingerprint, wikilink,
)


@dataclass(frozen=True)
class ReadingChangesReceipt:
    """Content-free outcome of one bounded maintenance pass."""

    total: int
    selected: int
    published: int = 0
    unchanged: int = 0
    conflicts: int = 0
    failed: int = 0
    remaining: int = 0
    complete: bool = True
    code: str = ""


class ReadingChanges:
    """Project canonical changes without creating another fact authority.

    The baseline is the reader's explicitly known revision. A visible open is
    never treated as knowledge. One bounded pass selects at most twenty Items
    and ``publish_item`` hydrates only the current and known snapshots.
    """

    OBJECT_PREFIX = "changes:"
    INDEX_ID = "control:changes"
    INDEX_PATH = "变化.md"
    MAX_LIMIT = 20
    MAX_UNITS = 24
    MAX_CLAIMS = 16
    MAX_RELATIONS = 10
    LABEL_LIMIT = 180
    TITLE_LIMIT = 160
    _REVISION_BITS = 31
    # A scheduling hint only: published page bindings remain durable authority.
    # App turns reconstruct their runtime, so keep a bounded process-local hint
    # across instances. Eviction/restart safely starts a fresh metadata scan.
    _cursors = OrderedDict()
    _cursor_lock = threading.Lock()

    _UNIT_FIELDS = ("kind", "text")
    _CLAIM_FIELDS = (
        "status", "condition", "channel", "version", "phase", "predicates",
        "polarity", "modality", "valid_from", "valid_to", "retracts",
    )
    _RELATION_FIELDS = ("from_item_id", "to_item_id", "relation", "direction", "reason")

    def __init__(self, store, publisher, controls, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= self.MAX_LIMIT:
            raise ReadingError("invalid_reading_changes_limit")
        if (store.generation_id != publisher.ledger.generation_id
                or controls.ledger.generation_id != store.generation_id
                or controls.publisher is not publisher):
            raise ReadingError("reading_changes_generation_mismatch")
        self.store, self.publisher, self.controls = store, publisher, controls
        self._cursor_key = (store.path, store.generation_id, str(publisher.root))
        self.limit = limit

    @classmethod
    def for_runtime(cls, runtime, *, limit=20):
        return cls(runtime.store, runtime.publisher, runtime.controls, limit=limit)

    @classmethod
    def object_id(cls, item_id):
        return cls.OBJECT_PREFIX + item_id

    @classmethod
    def path(cls, item_id):
        return "变化/" + item_id + ".md"

    @classmethod
    def _display_title(cls, row, *, limit=100):
        if row["title_truncated"]:
            return "标题较长的条目（打开查看完整标题）"
        return cls._safe_text(row["title"] or "未命名条目", limit=limit)

    @classmethod
    def _safe_text(cls, value, *, limit=None):
        # Select only after complete URL redaction: a clipped URL could remove
        # the delimiter that makes its credentials recognizable.
        text = redact_urls_in_text(str(value or ""))
        text = " ".join(text.split())
        text = html.escape(text, quote=False)
        text = text.replace("[", "（").replace("]", "）").replace("|", "／")
        text = re.sub(r"([\\`*_])", r"\\\1", text)
        limit = cls.LABEL_LIMIT if limit is None else limit
        if len(text) > limit:
            return text[:limit] + "…（完整内容请打开当前条目）"
        return text or "未提供"

    @staticmethod
    def _plain(value):
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))

    @classmethod
    def _unit(cls, value):
        result = {key: cls._plain(value.get(key)) for key in cls._UNIT_FIELDS}
        result["evidence"] = tuple(value.get("span_ids") or ())
        return result

    @classmethod
    def _claim(cls, value):
        result = {key: cls._plain(value.get(key)) for key in cls._CLAIM_FIELDS}
        # Evidence is used only for a verified link. It is excluded from
        # substantive comparison because moving provenance is not a new fact.
        result["evidence"] = tuple(value.get("span_ids") or ())
        return result

    @classmethod
    def _relation(cls, value):
        return {key: cls._plain(value.get(key)) for key in cls._RELATION_FIELDS}

    @staticmethod
    def _semantic_claim(claim):
        return {key: value for key, value in claim.items() if key != "evidence"}

    @staticmethod
    def _semantic_unit(unit):
        return {key: value for key, value in unit.items() if key != "evidence"}

    @classmethod
    def _claim_status(cls, status):
        return {
            "active": "当前有效",
            "retracted": "已撤回",
            "contested": "存在争议或相反证据",
            "stopped": "已停止",
        }.get(status, "状态为 " + cls._safe_text(status or "unknown", limit=30))

    @classmethod
    def _claim_description(cls, claim, *, before=None):
        before = before or {}
        labels = {
            "predicates": "判断内容",
            "condition": "适用条件",
            "channel": "适用范围/渠道",
            "version": "适用版本",
            "phase": "适用阶段",
            "polarity": "肯否性质",
            "modality": "确定性",
            "valid_from": "有效起点",
            "valid_to": "有效终点",
            "retracts": "撤回关联",
        }
        parts = [cls._claim_status(claim.get("status"))]
        for key, label in labels.items():
            if claim.get(key) != before.get(key):
                if key in {"predicates", "retracts"}:
                    parts.append(label + "有修订")
                elif claim.get(key) is not None:
                    parts.append(label + "：" + cls._safe_text(claim[key], limit=120))
                else:
                    parts.append(label + "已移除")
        return "；".join(parts)

    @classmethod
    def _relation_description(cls, relation, *, other_title=""):
        direction = {"incoming": "来自", "symmetric": "双向"}.get(
            relation.get("direction"), "指向")
        other = cls._safe_text(other_title or "另一当前条目", limit=80)
        relation_type = cls._safe_text(relation.get("relation") or "关联", limit=50)
        reason = cls._safe_text(relation.get("reason") or "未提供原因", limit=140)
        return direction + " " + other + " · " + relation_type + "：" + reason

    @staticmethod
    def _keyed(values, identity):
        return {value.get(identity): value for value in values
                if isinstance(value, dict) and isinstance(value.get(identity), str)}

    @classmethod
    def _unit_changes(cls, baseline, current):
        old = {key: cls._unit(value) for key, value in
               cls._keyed(baseline.get("units", ()), "unit_id").items()}
        new = {key: cls._unit(value) for key, value in
               cls._keyed(current.get("units", ()), "unit_id").items()}
        added = [new[key] for key in new if key not in old]
        removed = [old[key] for key in old if key not in new]
        changed = [{"before": old[key], "after": new[key]} for key in new
                   if key in old and cls._semantic_unit(old[key]) != cls._semantic_unit(new[key])]
        provenance = any(key in old and cls._semantic_unit(old[key]) == cls._semantic_unit(new[key])
                         and old[key]["evidence"] != new[key]["evidence"] for key in new)
        return added, removed, changed, provenance

    @classmethod
    def _claim_changes(cls, baseline, current):
        old = {key: cls._claim(value) for key, value in
               cls._keyed(baseline.get("claims", ()), "claim_id").items()}
        new = {key: cls._claim(value) for key, value in
               cls._keyed(current.get("claims", ()), "claim_id").items()}
        added = [new[key] for key in new if key not in old]
        removed = [old[key] for key in old if key not in new]
        changed = [{"before": old[key], "after": new[key]} for key in new
                   if key in old and cls._semantic_claim(old[key]) != cls._semantic_claim(new[key])]
        provenance = any(key in old and cls._semantic_claim(old[key]) == cls._semantic_claim(new[key])
                         and old[key]["evidence"] != new[key]["evidence"] for key in new)
        return added, removed, changed, provenance

    @classmethod
    def _relation_changes(cls, baseline, current):
        old = {key: cls._relation(value) for key, value in
               cls._keyed(baseline.get("relations", ()), "relation_id").items()}
        new = {key: cls._relation(value) for key, value in
               cls._keyed(current.get("relations", ()), "relation_id").items()}
        added = [new[key] for key in new if key not in old]
        removed = [old[key] for key in old if key not in new]
        changed = [{"before": old[key], "after": new[key]} for key in new
                   if key in old and old[key] != new[key]]
        return added, removed, changed

    def _known_revision(self, item_id, current_revision):
        known = self.controls.preference(item_id).known_revision
        if type(known) is not int or known < 0 or known > current_revision:
            raise ReadingError("reading_changes_known_future")
        return known

    @staticmethod
    def _snapshot_value(row, expected, code):
        if row is None:
            raise ReadingError(code)
        try:
            value = json.loads(row["snapshot_json"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReadingError("reading_changes_snapshot_invalid") from exc
        if not isinstance(value, dict) or value.get("content_revision") != expected:
            raise ReadingError("reading_changes_snapshot_mismatch")
        return value

    def _snapshots(self, item_id, current_revision, known):
        with closing(self.store.connect()) as conn:
            current_row = conn.execute(
                "SELECT revision,snapshot_json FROM item_revisions "
                "WHERE item_id=? AND revision=?", (item_id, current_revision)).fetchone()
            baseline_row = None
            if known:
                baseline_row = conn.execute(
                    "SELECT revision,snapshot_json FROM item_revisions "
                    "WHERE item_id=? AND revision=?", (item_id, known)).fetchone()
        current = self._snapshot_value(current_row, current_revision,
                                       "reading_changes_current_snapshot_missing")
        baseline = (self._snapshot_value(baseline_row, known,
                                         "reading_changes_baseline_missing")
                    if known else None)
        return baseline, current

    def _evidence_link(self, item_id, evidence_id):
        try:
            return self.publisher.evidence_link(item_id, evidence_id, alias="核对来源")
        except (ReadingError, OSError, UnicodeError):
            return ""

    def _unit_line(self, item_id, unit, *, prefix):
        kind = self._safe_text(unit.get("kind") or "内容", limit=40)
        text = self._safe_text(unit.get("text") or "（没有可显示的派生说明）")
        links = [link for evidence_id in unit.get("evidence", ())[:3]
                 if (link := self._evidence_link(item_id, evidence_id))]
        missing = len(unit.get("evidence", ())[:3]) > len(links)
        line = "- " + prefix + " [" + kind + "] " + text
        if links:
            line += " · " + " · ".join(links)
        if missing:
            line += " · 部分来源页暂未就绪"
        if len(unit.get("evidence", ())) > 3:
            line += " · 其余来源请从当前条目核对"
        return line

    def _claim_line(self, item_id, claim, *, prefix, before=None):
        links = [link for evidence_id in claim.get("evidence", ())[:3]
                 if (link := self._evidence_link(item_id, evidence_id))]
        line = "- " + prefix + self._claim_description(claim, before=before)
        if links:
            line += " · " + " · ".join(links)
        if len(claim.get("evidence", ())[:3]) > len(links):
            line += " · 部分来源页暂未就绪"
        if len(claim.get("evidence", ())) > 3:
            line += " · 其余来源请从当前条目核对"
        return line

    def _unit_change_line(self, item_id, change):
        return self._unit_line(item_id, change["after"], prefix="更正") + \
            "\n  - 原内容：" + self._safe_text(
                change["before"].get("text"), limit=self.LABEL_LIMIT)

    @staticmethod
    def _relation_other_ids(item_id, relation):
        if not relation.get("from_item_id") or not relation.get("to_item_id"):
            return ()
        return (relation["to_item_id"] if relation["from_item_id"] == item_id
                else relation["from_item_id"],)

    def _relation_label(self, item_id, relation, titles, *, suffix=""):
        other_ids = self._relation_other_ids(item_id, relation)
        other = titles.get(other_ids[0], "") if other_ids else ""
        return "- 关系变化：" + self._relation_description(relation, other_title=other) + suffix

    def _current_target(self, item_id, revision):
        try:
            if not self.publisher.ready(item_id, revision):
                return ""
            path = self.publisher.resolve(item_id)
            link = self._wikilink(path, "打开当前条目")
            if link:
                return link
        except (ReadingError, OSError, UnicodeError):
            return ""
        return ""

    @staticmethod
    def _wikilink(path, label):
        if not path or "#" in path or "^" in path:
            return ""
        try:
            return wikilink("WGO/" + path, label)
        except ReadingError:
            return ""

    @classmethod
    def _page_revision(cls, known, current):
        limit = 1 << cls._REVISION_BITS
        if any(type(value) is not int or not 0 <= value < limit for value in (known, current)):
            raise ReadingError("reading_changes_revision_out_of_range")
        return (known << cls._REVISION_BITS) | current

    def _render_unknown(self, item_id, current):
        units = current.get("units", ())
        lines = [
            "尚未确认已知，以下为当前保留内容",
            "你还没有明确确认过这个条目的已知版本。可见打开记录不会替代已知确认；"
            "这里也不会编造并不存在的初始已知版本。",
            "当前版本：v" + str(current["content_revision"]),
        ]
        for unit in units[:self.MAX_UNITS]:
            lines.append(self._unit_line(item_id, self._unit(unit), prefix="当前"))
        if len(units) > self.MAX_UNITS:
            lines.append("当前内容较多，这里显示前 " + str(self.MAX_UNITS) +
                         " 个内容单元；完整内容请打开当前条目。")
        target = self._current_target(item_id, current["content_revision"])
        lines.append("当前条目：" + (target or "暂未就绪；完整内容可稍后从条目目录核对。"))
        return lines, min(len(units), self.MAX_UNITS)

    def _render_known(self, item_id, known, baseline, current):
        unit_added, unit_removed, unit_changed, unit_provenance = self._unit_changes(
            baseline, current)
        claim_added, claim_removed, claim_changed, claim_provenance = self._claim_changes(
            baseline, current)
        relation_added, relation_removed, relation_changed = self._relation_changes(
            baseline, current)
        lines = [
            "从明确已知的 v" + str(known) + " 读到当前 v" +
            str(current["content_revision"]),
            "比较依据是 canonical 条目版本；这与来源消息的发送先后不是同一件事。",
        ]
        shown = total = 0
        truncated = False

        def append_values(header, count, render, maximum=None):
            nonlocal shown, total, truncated
            if not count:
                return
            lines.append(header)
            total += count
            available = min(count, self.MAX_UNITS - shown)
            if maximum is not None:
                available = min(available, maximum)
            lines.extend(render(index) for index in range(available))
            shown += available
            if available < count:
                truncated = True

        # Corrections must remain visible when a large addition fills the preview.
        append_values("## 状态或条件更正", len(claim_changed), lambda index:
            self._claim_line(item_id, claim_changed[index]["after"], prefix="更正：",
                             before=claim_changed[index]["before"]), self.MAX_CLAIMS)
        append_values("## 不再归入当前条目的状态", len(claim_removed), lambda index:
            self._claim_line(item_id, claim_removed[index], prefix="移出："),
            self.MAX_CLAIMS)
        append_values("## 新增内容", len(unit_added), lambda index:
            self._unit_line(item_id, unit_added[index], prefix="新增"))
        append_values("## 内容表达或状态变化", len(unit_changed), lambda index:
            self._unit_change_line(item_id, unit_changed[index]))
        append_values("## 不再归入当前条目", len(unit_removed), lambda index:
            self._unit_line(item_id, unit_removed[index], prefix="移出"))
        append_values("## 新增状态", len(claim_added), lambda index:
            self._claim_line(item_id, claim_added[index], prefix="新增："),
            self.MAX_CLAIMS)

        titles = {}
        available_relations = max(0, min(self.MAX_RELATIONS, self.MAX_UNITS - shown))
        relation_specs = (
            [(value, "") for value in relation_added] +
            [(value["after"], "（原关系：" + self._safe_text(
                value["before"].get("relation"), limit=50) + "）")
             for value in relation_changed] +
            [(value, "（已移出）") for value in relation_removed])
        selected_relations = relation_specs[:available_relations]
        total += len(relation_specs)
        truncated = truncated or len(selected_relations) < len(relation_specs)
        rendered_relations = [value for value, _suffix in selected_relations]
        with closing(self.store.connect()) as conn:
            other_ids = {
                self._relation_other_ids(item_id, relation)[0]
                for relation in rendered_relations
                if self._relation_other_ids(item_id, relation)
            }
            for other_id in sorted(other_ids):
                row = conn.execute(
                    "SELECT CASE WHEN length(title)<=? THEN title ELSE '' END AS title,"
                    "length(title)>? AS title_truncated "
                    "FROM items WHERE item_id=? AND lifecycle='active'",
                    (self.TITLE_LIMIT, self.TITLE_LIMIT, other_id)).fetchone()
                if row is not None:
                    titles[other_id] = self._display_title(row, limit=80)
        # Selection happens before source-link/file IO.
        if selected_relations:
            lines.append("## 关系变化")
        for relation, suffix in selected_relations:
            lines.append(self._relation_label(item_id, relation, titles, suffix=suffix))
            shown += 1

        if not total:
            lines.append("## 只有整理或出处变化")
            lines.append("没有发现内容单元、状态条件或条目关系的实质变化；"
                         "这次版本差异属于整理、证据位置或其他呈现信息的变化。")
        elif unit_provenance or claim_provenance:
            lines.append("另有一些仅整理或出处位置的变化，未计作新的内容或状态。")
        if truncated:
            lines.append(
                "本页只展示 " + str(shown) + " 项，共发现 " + str(total) +
                " 项变化；完整当前内容请打开当前条目，不能把本页当作全部变化的完整清单。")
        target = self._current_target(item_id, current["content_revision"])
        lines.append("当前条目：" + (target or "暂未就绪；完整内容可稍后从条目目录核对。"))
        return lines, shown

    def _item_row(self, item_id):
        with closing(self.store.connect()) as conn:
            return conn.execute(
                "SELECT item_id,"
                "CASE WHEN length(title)<=? THEN title ELSE '' END AS title,"
                "length(title)>? AS title_truncated,kind,content_revision,"
                "semantic_updated_at FROM items "
                "WHERE item_id=? AND lifecycle='active'",
                (self.TITLE_LIMIT, self.TITLE_LIMIT, item_id)).fetchone()

    def _render_item(self, item):
        item_id, current_revision = item["item_id"], item["content_revision"]
        known = self._known_revision(item_id, current_revision)
        baseline, current = self._snapshots(item_id, current_revision, known)
        lines, shown = (self._render_unknown(item_id, current) if known == 0
                        else self._render_known(item_id, known, baseline, current))
        return "\n\n".join(lines), known, shown

    def _still_current(self, item_id, current_revision, known, snapshot_rows):
        try:
            item = self._item_row(item_id)
            if item is None or item["content_revision"] != current_revision:
                return False
            preference = self.controls.preference(item_id)
            if preference.known_revision != known:
                return False
            with closing(self.store.connect()) as conn:
                for revision, snapshot_json in snapshot_rows:
                    row = conn.execute(
                        "SELECT snapshot_json FROM item_revisions "
                        "WHERE item_id=? AND revision=?", (item_id, revision)).fetchone()
                    if row is None or fingerprint(row["snapshot_json"]) != fingerprint(snapshot_json):
                        return False
            return self.publisher.ready(item_id, current_revision)
        except (ReadingError, OSError, UnicodeError):
            return False

    def publish_item(self, item_id) -> ProjectionReceipt:
        item = self._item_row(item_id)
        if item is None:
            raise ReadingError("reading_changes_item_missing")
        item = dict(item)
        body, known, _shown = self._render_item(item)
        current_revision = item["content_revision"]
        preference = self.controls.preference(item_id)
        if preference.known_revision != known:
            raise ReadingError("stale_projection_snapshot")
        with closing(self.store.connect()) as conn:
            revisions = (current_revision, known) if known else (current_revision,)
            snapshot_rows = [
                (row["revision"], row["snapshot_json"])
                for row in conn.execute(
                    "SELECT revision,snapshot_json FROM item_revisions "
                    "WHERE item_id=? AND revision IN (" +
                    ",".join("?" for _ in revisions) + ")",
                    (item_id, *revisions)).fetchall()
            ]
        guards = {"revisions": {item_id: current_revision}, "known_revisions": {item_id: known}}
        # The two bounded canonical co-ordinates are the durable page version.
        # A later known confirmation therefore requires a fresh comparison.
        page_revision = self._page_revision(known, current_revision)
        return self.publisher.publish_document(
            object_id=self.object_id(item_id), path=self.path(item_id),
            title="变化：" + self._display_title(item), body=body,
            content_revision=page_revision,
            proposal_guards=guards,
            is_current=lambda: self._still_current(
                item_id, current_revision, known, snapshot_rows))

    def _selection(self, limit, *, cursor=None):
        rows, total, _last = self._selection_page(limit, cursor=cursor,
                                                  count_total=True)
        return rows, total

    def _selection_page(self, limit, *, cursor=None, count_total=False):
        # A page remains settled even while the reader has never confirmed it.
        # Only a missing/stale exact (known,current) page binding is backlog.
        backlog = (
            "NOT EXISTS (SELECT 1 FROM reader_projections p "
            "WHERE p.generation_id=? AND p.object_id='changes:'||i.item_id "
            "AND p.status='ready' AND p.content_revision="
            "(coalesce((SELECT max(k.content_revision) FROM reader_known k "
            "WHERE k.generation_id=? AND k.item_id=i.item_id "
            "AND k.content_revision<=i.content_revision),0)*2147483648"
            "+i.content_revision))")
        pending = (
            "EXISTS (SELECT 1 FROM reader_proposals q WHERE q.generation_id=? "
            "AND q.object_id='changes:'||i.item_id AND q.status='pending')")
        current_projection = (
            "EXISTS (SELECT 1 FROM reader_projections c WHERE c.generation_id=? "
            "AND c.object_id=i.item_id AND c.status='ready' "
            "AND c.content_revision=i.content_revision)")
        cursor_sql, cursor_parameters = "", ()
        if cursor is not None:
            priority, stamp, identity = cursor
            cursor_sql = (" WHERE priority>? OR (priority=? AND (semantic_updated_at<? OR "
                          "(semantic_updated_at=? AND item_id>?)))")
            cursor_parameters = (priority, priority, stamp, stamp, identity)
        with closing(self.store.connect()) as conn:
            rows = conn.execute(
                "WITH candidates AS (SELECT i.item_id,"
                "CASE WHEN length(i.title)<=? THEN i.title ELSE '' END AS title,"
                "length(i.title)>? AS title_truncated,i.content_revision,"
                "coalesce((SELECT max(k.content_revision) FROM reader_known k "
                "WHERE k.generation_id=? AND k.item_id=i.item_id "
                "AND k.content_revision<=i.content_revision),0) AS known_revision,"
                "i.semantic_updated_at,"
                "CASE WHEN (" + pending + ") THEN 1 "
                "WHEN NOT (" + current_projection + ") "
                "OR r.revision IS NULL OR json_valid(r.snapshot_json)=0 THEN 2 "
                "ELSE 0 END AS priority "
                "FROM items i LEFT JOIN item_revisions r ON r.item_id=i.item_id "
                "AND r.revision=i.content_revision "
                "WHERE i.lifecycle='active' AND (" + backlog + ")) "
                "SELECT * FROM candidates" + cursor_sql +
                " ORDER BY priority,semantic_updated_at DESC,item_id LIMIT ?",
                (self.TITLE_LIMIT, self.TITLE_LIMIT, self.store.generation_id,
                 self.publisher.ledger.generation_id,
                 self.publisher.ledger.generation_id,
                 self.store.generation_id, self.store.generation_id,
                 *cursor_parameters, limit)).fetchall()
            total = (conn.execute(
                "SELECT count(*) FROM items i WHERE i.lifecycle='active' "
                "AND (" + backlog + ")",
                (self.publisher.ledger.generation_id, self.store.generation_id)
            ).fetchone()[0] if count_total else None)
        values = [dict(row) for row in rows]
        return values, total, values[-1] if values else None

    def _cursor(self, *, audit=False):
        with self._cursor_lock:
            return self._cursors.get(self._cursor_key + (audit,))

    def _set_cursor(self, row, *, audit=False):
        with self._cursor_lock:
            key = self._cursor_key + (audit,)
            self._cursors.pop(key, None)
            if row is not None:
                self._cursors[key] = (row["priority"], row["semantic_updated_at"], row["item_id"])
            while len(self._cursors) > 8:
                self._cursors.popitem(last=False)

    def _audit_ready(self, limit):
        """Rotate through settled metadata without reloading healthy snapshots.

        A ready ledger row cannot prove that its Markdown still exists. Spare
        daily capacity revalidates files through the publisher and repairs missing
        targets; hand edits still create ordinary conflict proposals.
        """
        if not limit:
            return []
        def select(cursor):
            after, args = "", ()
            if cursor is not None:
                _priority, stamp, identity = cursor
                after = " AND (i.semantic_updated_at<? OR (i.semantic_updated_at=? AND i.item_id>?))"
                args = (stamp, stamp, identity)
            with closing(self.store.connect()) as conn:
                return [dict(row) for row in conn.execute(
                    "SELECT i.item_id,i.semantic_updated_at,0 AS priority,p.content_revision AS page_revision "
                    "FROM items i JOIN reader_projections p ON p.generation_id=? "
                    "AND p.object_id='changes:'||i.item_id AND p.status='ready' "
                    "WHERE i.lifecycle='active' AND p.content_revision="
                    "(coalesce((SELECT max(k.content_revision) FROM reader_known k "
                    "WHERE k.generation_id=? AND k.item_id=i.item_id AND k.content_revision<=i.content_revision),0)"
                    "*2147483648+i.content_revision)" + after +
                    " ORDER BY i.semantic_updated_at DESC,i.item_id LIMIT ?",
                    (self.store.generation_id, self.store.generation_id, *args, limit))]
        cursor = self._cursor(audit=True)
        rows = select(cursor)
        if not rows and cursor is not None:
            rows = select(None)
        self._set_cursor(rows[-1] if rows else None, audit=True)
        repair = []
        for row in rows:
            try:
                ready = self.publisher.ready(self.object_id(row["item_id"]), row["page_revision"])
            except (ReadingError, OSError, UnicodeError):
                ready = False
            if not ready:
                repair.append(row)
        return repair

    def publish(self, *, limit=None) -> ReadingChangesReceipt:
        limit = self.limit if limit is None else limit
        if type(limit) is not int or not 1 <= limit <= self.MAX_LIMIT:
            raise ReadingError("invalid_reading_changes_limit")
        rows, total, _last = self._selection_page(limit, cursor=self._cursor(),
                                                  count_total=True)
        if not rows and self._cursor() is not None:
            self._set_cursor(None)
            rows, total, _last = self._selection_page(limit, count_total=True)
        if rows:
            self._set_cursor(rows[-1])
        repairs = self._audit_ready(limit - len(rows))
        total += len(repairs)
        rows.extend(repairs)
        counts = {"published": 0, "unchanged": 0, "conflicts": 0, "failed": 0}
        codes = []
        for row in rows:
            try:
                receipt = self.publish_item(row["item_id"])
                if receipt.status in {"published", "unchanged", "conflict"}:
                    counts[{"conflict": "conflicts"}.get(receipt.status, receipt.status)] += 1
                    if receipt.status == "conflict":
                        codes.append("changes_projection_conflict")
                else:
                    counts["failed"] += 1
                    codes.append("changes_projection_" + receipt.status)
            except (ReadingError, OSError, UnicodeError) as exc:
                counts["failed"] += 1
                codes.append(getattr(exc, "code", "reading_changes_failed"))
        try:
            index = self.publish_index()
            if index.status == "conflict":
                counts["conflicts"] += 1
                codes.append("changes_index_conflict")
            elif index.status not in {"published", "unchanged"}:
                counts["failed"] += 1
                codes.append("reading_changes_index_unavailable")
        except (ReadingError, OSError, UnicodeError) as exc:
            counts["failed"] += 1
            codes.append(getattr(exc, "code", "reading_changes_index_failed"))
        remaining = self._selection(1)[1]
        if not remaining:
            self._set_cursor(None)
        complete = remaining == 0 and not counts["failed"] and not counts["conflicts"]
        return ReadingChangesReceipt(
            total=total, selected=len(rows), remaining=remaining,
            complete=complete, code=",".join(dict.fromkeys(codes)), **counts)

    def _index_body(self):
        return self._render_index(self._index_rows())

    def _index_rows(self):
        with closing(self.store.connect()) as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT i.item_id,CASE WHEN length(i.title)<=? THEN i.title ELSE '' END AS title,"
                "length(i.title)>? AS title_truncated,i.content_revision,p.content_revision "
                "AS page_revision,coalesce((SELECT max(k.content_revision) "
                "FROM reader_known k WHERE k.generation_id=? AND k.item_id=i.item_id "
                "AND k.content_revision<=i.content_revision),0) AS known_revision "
                "FROM items i JOIN reader_projections p ON p.generation_id=? "
                "AND p.object_id='changes:'||i.item_id AND p.status='ready' "
                "WHERE i.lifecycle='active' "
                "ORDER BY i.semantic_updated_at DESC,i.item_id LIMIT ?",
                (self.TITLE_LIMIT, self.TITLE_LIMIT, self.store.generation_id, self.publisher.ledger.generation_id,
                 self.limit)).fetchall()]
            total = conn.execute(
                "SELECT count(*) FROM items i JOIN reader_projections p "
                "ON p.generation_id=? AND p.object_id='changes:'||i.item_id "
                "AND p.status='ready' WHERE i.lifecycle='active'",
                (self.publisher.ledger.generation_id,)).fetchone()[0]
        return rows, total

    def _render_index(self, selection):
        rows, total = selection
        lines = ["这里按明确已知版本列出具体变化。可见打开记录不会替代已知确认。"]
        lines.append("已形成变化页 " + str(total) + " 个；本页列出 " + str(len(rows)) + " 个。")
        for row in rows:
            title = self._display_title(row)
            state = ("已知至 v" + str(row["known_revision"]) +
                     "，当前 v" + str(row["content_revision"])
                     if row["known_revision"] else
                     "尚未确认已知，当前 v" + str(row["content_revision"]))
            try:
                object_id = self.object_id(row["item_id"])
                path = self.publisher.resolve(object_id)
                expected_revision = self._page_revision(
                    row["known_revision"], row["content_revision"])
                if (row["page_revision"] == expected_revision
                        and self.publisher.ready(object_id)):
                    link = self._wikilink(path, title) or (
                        title + "（文件名暂无法安全跳转，请从知识库目录打开）")
                else:
                    link = title + "（变化页待刷新）"
            except (ReadingError, OSError, UnicodeError):
                link = title + "（变化页待就绪）"
            lines.append("- " + link + " — " + state)
        if not rows:
            lines.append("目前没有待比较的条目。")
        if total > len(rows):
            lines.append("本索引固定展示最近 " + str(self.limit) + " 个已形成的变化页，"
                         "不代表变化条目总数；不要据此把其他条目判为没有变化。")
        return "\n\n".join(lines), rows, total

    def publish_index(self) -> ProjectionReceipt:
        body, rows, total = self._index_body()
        binding = [(row["item_id"], row["known_revision"], row["content_revision"])
                   for row in rows]
        prepared = "\n" + body.strip("\n") + "\n"
        previous = self.publisher.record(self.INDEX_ID)
        pending = self.publisher.ledger.one("SELECT content_revision,proposed_body FROM reader_proposals "
            "WHERE generation_id=? AND object_id=? AND status='pending' ORDER BY content_revision DESC,rowid DESC LIMIT 1",
            (self.store.generation_id, self.INDEX_ID))
        if pending is not None and pending["proposed_body"] == prepared:
            revision = pending["content_revision"]
        elif previous is not None and previous["machine_body"] == prepared and pending is None:
            revision = previous["content_revision"]
        else:
            revision = max(previous["content_revision"] if previous else 0,
                           pending["content_revision"] if pending else 0) + 1

        def still_current():
            latest_rows, latest_total = self._index_rows()
            latest = [(row["item_id"], row["known_revision"], row["content_revision"])
                      for row in latest_rows]
            return latest == binding and latest_total == total

        return self.publisher.publish_document(
            object_id=self.INDEX_ID, path=self.INDEX_PATH, title="阅读变化",
            body=body, content_revision=revision, is_current=still_current,
            proposal_guards={"revisions": {row["item_id"]: row["content_revision"] for row in rows},
                             "known_revisions": {row["item_id"]: row["known_revision"] for row in rows}})
