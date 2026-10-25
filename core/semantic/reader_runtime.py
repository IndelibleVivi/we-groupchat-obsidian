"""Source-independent Obsidian controls, visible-open receipts and attention.

The application supplies an already composed SemanticRuntime and owns scheduling,
provider construction, source capture and native notification/open operations.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import PurePosixPath
import re
import stat
import threading
import time

from .briefing import BriefPlan
from .controls import OpenObservation, ReaderControls
from .delivery import AttentionEvent, ContentBinding, EvidenceTarget
from .projections import (ReadingError, fingerprint, json_text, patch_properties,
                          property_value, region_body, wikilink)
from .watches import WatchService


EVENT_SCHEMA = "wgo.reader-visible-open.v1"
EVENT_FIELDS = {"schema", "event_id", "generation_id", "item_id", "content_revision", "path",
                "file_sha256", "observed_at", "origin", "visible"}
DEFAULT_PROPERTIES = ReaderControls.DEFAULT_PROPERTIES
_SCHEMA = """
CREATE TABLE IF NOT EXISTS reader_event_receipts(
 generation_id TEXT NOT NULL,event_key TEXT NOT NULL,input_hash TEXT NOT NULL,
 status TEXT NOT NULL,code TEXT NOT NULL,observed_at REAL NOT NULL,
 PRIMARY KEY(generation_id,event_key));
CREATE TABLE IF NOT EXISTS reader_runtime_status(
 generation_id TEXT PRIMARY KEY,body_hash TEXT NOT NULL,revision INTEGER NOT NULL);
"""


class ReaderRuntime:
    MAX_EVENT_BYTES = 8192
    MAX_EVENTS_PER_POLL = 128
    CHANNEL = "obsidian-reader"

    def __init__(self, semantic_runtime, *, notifier=None, device_id, live=False):
        if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 200 or type(live) is not bool:
            raise ReadingError("invalid_reader_runtime_identity")
        self.runtime = semantic_runtime
        self.store, self.ledger = semantic_runtime.store, semantic_runtime.ledger
        self.publisher, self.controls = semantic_runtime.publisher, semantic_runtime.controls
        self.delivery = semantic_runtime.delivery
        self.notifier, self.device_id, self.live = notifier, device_id, live
        self.properties = self.controls.property_mapping(semantic_runtime.config.get("semantic_reader_properties", {}))
        self.watches = WatchService(self.store, text_provider=semantic_runtime.roles.text,
                                    decision_provider=semantic_runtime.roles.decision)
        self._lock = threading.RLock()
        self.ledger.install(_SCHEMA)

    @staticmethod
    def _code(exc):
        value = getattr(exc, "code", "reader_operation_deferred")
        return value if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,100}", value) else "reader_operation_deferred"

    def _floor(self):
        with closing(self.store.connect()) as conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0])
        value = metadata.get("delivery_floor_change_id")
        return value if type(value) is int and value >= 0 else None

    def _properties(self, items, codes):
        count = 0
        for item in items:
            try:
                self.controls.register_item_properties(item["item_id"], properties=self.properties)
                self.controls.sync_properties(item["item_id"])
                count += 1
            except Exception as exc:
                codes.add(self._code(exc))
        return count

    def _reflect_actions(self, receipts, codes):
        for receipt in receipts:
            if receipt.status != "applied":
                continue
            action = self.ledger.one("SELECT * FROM reader_actions WHERE action_id=?", (receipt.id,))
            if action["kind"] not in {"follow", "mute", "wait"}:
                continue
            try:
                with self.publisher.locked():
                    path = self.publisher.resolve(action["item_id"])
                    raw = self.publisher.read(path) if path else None
                    if raw is None or fingerprint(raw) != action["file_hash"]:
                        raise ReadingError("preference_projection_changed")
                    pref = self.controls.preference(action["item_id"])
                    mappings = self.ledger.all("SELECT role,property_key FROM reader_property_maps WHERE generation_id=? AND object_id=?",
                                               (self.store.generation_id, action["item_id"]))
                    values = {row["property_key"]: getattr(pref, row["role"]) for row in mappings if row["role"] != "query"}
                    text = patch_properties(raw.decode("utf-8"), values, machine_only=False)
                    self.publisher.atomic_write(path, text.encode(), expected=fingerprint(raw))
            except Exception as exc:
                codes.add(self._code(exc))

    def _read_event(self, relative):
        path = self.publisher._safe_path(relative, create_parents=False)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and info.st_mode & 0o077):
                raise ReadingError("reader_event_not_private_regular")
            if info.st_size > self.MAX_EVENT_BYTES:
                raise ReadingError("reader_event_too_large")
            raw = stream.read(self.MAX_EVENT_BYTES + 1)
            if len(raw) > self.MAX_EVENT_BYTES:
                raise ReadingError("reader_event_too_large")
            return raw

    def _archive_open(self, path, signature):
        relative = ".wgo/reader-events/" + path.name
        if fingerprint(self._read_event(relative)) != signature:
            raise ReadingError("reader_event_changed_after_receipt")
        target = self.publisher._safe_path(".wgo/reader-events/processed/" + path.name)
        if not target.exists():
            os.replace(path, target)

    def _validate_open(self, value, filename):
        if (not isinstance(value, dict) or set(value) != EVENT_FIELDS or value["schema"] != EVENT_SCHEMA
                or not re.fullmatch(r"[a-f0-9]{32}", str(value["event_id"]))
                or filename != value["event_id"] + ".json" or value["origin"] != "obsidian_visible_open"
                or value["visible"] is not True or value["generation_id"] != self.store.generation_id
                or type(value["content_revision"]) is not int or value["content_revision"] <= 0
                or not isinstance(value["item_id"], str) or not value["item_id"] or len(value["item_id"]) > 200
                or not re.fullmatch(r"[a-f0-9]{64}", str(value["file_sha256"]))
                or type(value["observed_at"]) not in (int, float) or not math.isfinite(value["observed_at"])
                or not 0 < value["observed_at"] <= time.time() + 30):
            raise ReadingError("invalid_visible_open_event")
        path = value["path"]
        if (not isinstance(path, str) or len(path) > 1024 or "\\" in path or "\x00" in path
                or PurePosixPath(path).is_absolute() or any(part in {"..", ".wgo"} for part in PurePosixPath(path).parts)
                or not path.endswith(".md")):
            raise ReadingError("reader_event_path_invalid")
        raw = self.publisher.read(path)
        if raw is None or fingerprint(raw) != value["file_sha256"]:
            raise ReadingError("reader_event_file_changed")
        text = raw.decode("utf-8")
        if any(property_value(text, key) != value[field] for key, field in (
                ("wgo_id", "item_id"), ("wgo_generation", "generation_id"),
                ("wgo_content_revision", "content_revision"))):
            raise ReadingError("reader_event_identity_mismatch")
        if self.publisher._find_id(value["item_id"]) != path:
            raise ReadingError("reader_event_identity_mismatch")
        with closing(self.store.connect()) as conn:
            known = conn.execute("SELECT 1 FROM item_revisions WHERE item_id=? AND revision=?",
                                 (value["item_id"], value["content_revision"])).fetchone()
            if not known and value["item_id"].startswith("brief:"):
                known = conn.execute("SELECT 1 FROM reader_brief_history WHERE generation_id=? AND edition_id=? AND content_revision=?",
                    (self.store.generation_id, value["item_id"], value["content_revision"])).fetchone()
        if not known:
            raise ReadingError("reader_event_revision_unknown")
        projection = self.publisher.record(value["item_id"])
        if projection is None or projection["status"] != "ready":
            raise ReadingError("reader_event_projection_not_trusted")
        machine = region_body(text, projection["region"])
        current = (projection["content_revision"] == value["content_revision"] and machine is not None
                   and fingerprint(machine) == projection["machine_hash"])
        history = self.ledger.one("SELECT 1 FROM reader_projection_history WHERE generation_id=? AND object_id=? AND file_hash=?",
                                  (self.store.generation_id, value["item_id"], value["file_sha256"]))
        if not current and not history:
            raise ReadingError("reader_event_projection_not_trusted")
        return OpenObservation(value["event_id"], value["item_id"], value["content_revision"],
            datetime.fromtimestamp(value["observed_at"], timezone.utc).isoformat(), "obsidian_visible_open", True)

    def _consume_opens(self, codes):
        directory = self.publisher._safe_path(".wgo/reader-events/placeholder", create_parents=True).parent
        if os.name == "posix" and directory.stat().st_mode & 0o077:
            raise ReadingError("reader_event_directory_not_private")
        consumed = 0
        # Only final JSON files are candidates. Temp files and unrelated source
        # Markdown are never interpreted as commands or events.
        for path in sorted(directory.iterdir(), key=lambda p: p.name):
            if not path.name.endswith(".json"):
                continue
            previous = self.ledger.one("SELECT * FROM reader_event_receipts WHERE generation_id=? AND event_key=?",
                                        (self.store.generation_id, path.name))
            if previous:
                if previous["status"] == "accepted":
                    try:
                        self._archive_open(path, previous["input_hash"])
                    except Exception as exc:
                        codes.add(self._code(exc))
                continue
            if consumed >= self.MAX_EVENTS_PER_POLL:
                codes.add("reader_event_batch_pending")
                break
            relative, signature = ".wgo/reader-events/" + path.name, "unread"
            try:
                raw = self._read_event(relative)
                signature = fingerprint(raw)
                value = json.loads(raw)
                with self.publisher.locked():
                    observation = self._validate_open(value, path.name)
                    # observe_open is itself durable and idempotent. If a crash
                    # precedes the inbox receipt, replay cannot count it twice.
                    self.controls.observe_open(observation)
                status, code = "accepted", ""
            except Exception as exc:
                status, code = "rejected", self._code(exc)
                codes.add(code)
            with self.ledger.transaction() as conn:
                conn.execute("INSERT OR IGNORE INTO reader_event_receipts VALUES(?,?,?,?,?,?)",
                             (self.store.generation_id, path.name, signature, status, code, time.time()))
            consumed += 1
            if status == "accepted":
                # Receipt first; only validated-against-file-identity accepted
                # events are moved. Invalid original files remain untouched.
                try:
                    self._archive_open(path, signature)
                except Exception as exc:
                    codes.add(self._code(exc))
        return consumed

    def _new_changes(self):
        floor = self._floor()
        if floor is None:
            return []
        with closing(self.store.connect()) as conn:
            return [dict(row) for row in conn.execute("SELECT c.*,t.actions_json FROM change_events c "
                "JOIN canonical_transactions t USING(transaction_id) WHERE c.change_id>? AND c.semantic_effect<>'none' "
                "ORDER BY c.change_id", (floor,))]

    def _claim_dependencies(self, item_id, claim_ids):
        evidence, bindings = set(), {}
        for claim_id in sorted(set(claim_ids)):
            with closing(self.store.connect()) as conn:
                claim = conn.execute("SELECT span_ids_json FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
                owners = [row[0] for row in conn.execute("SELECT item_id FROM item_claims WHERE claim_id=? ORDER BY item_id", (claim_id,))]
            if claim is None:
                raise ReadingError("attention_claim_missing")
            owner = item_id if item_id in owners else next((id_ for id_ in owners if self.store.get_item(id_)["lifecycle"] == "active"), None)
            if owner is None:
                raise ReadingError("attention_claim_owner_missing")
            item = self.store.get_item(owner)
            pref = self.controls.preference(owner)
            bindings[owner] = ContentBinding(owner, item["content_revision"], pref.revision)
            evidence.update((owner, span) for span in json.loads(claim[0]))
        if not evidence:
            raise ReadingError("attention_evidence_missing")
        return tuple(bindings.values()), tuple(EvidenceTarget(*pair) for pair in sorted(evidence))

    def _send(self, event, *, source_revision=None):
        with self.publisher.locked():
            # Observe a property edit made during watch/editor work before
            # crossing the notifier boundary. Binding checks then fail closed.
            for binding in event.content_bindings:
                if self.ledger.one("SELECT 1 FROM reader_property_maps WHERE generation_id=? AND object_id=?",
                                    (self.store.generation_id, binding.item_id)):
                    self.controls.sync_properties(binding.item_id)
            if source_revision is not None and self.controls.preference(event.target_id).known_revision >= source_revision:
                return None
            if event.kind == "correction":
                # An ordinary later revision may give an as-yet-unsent correction
                # a current target, but never another send for a dispatched one.
                # Reuse the exact durable event/receipt across target revisions,
                # including events registered before source-level lookup existed.
                previous = self.ledger.one("SELECT a.event_id FROM reader_attention_events a "
                    "JOIN reader_deliveries d USING(event_id) WHERE a.generation_id=? "
                    "AND json_extract(a.event_json,'$.source_event_id')=? "
                    "AND json_extract(a.event_json,'$.kind')='correction' "
                    "AND json_extract(a.event_json,'$.target_id')=? AND d.device_id=? AND d.channel=? "
                    "AND d.status IN ('accepted','unknown','sending','rejected') ORDER BY a.rowid LIMIT 1",
                    (self.store.generation_id, event.source_event_id, event.target_id, self.device_id, self.CHANNEL))
                if previous:
                    return self.delivery.deliver(previous["event_id"], device_id=self.device_id, channel=self.CHANNEL,
                                                 notifier=self.notifier, live=self.live)
            identity = fingerprint(json_text([self.store.generation_id, event.source_event_id,
                                               event.kind, event.target_id, event.target_revision]))
            previous = self.ledger.one("SELECT 1 FROM reader_attention_events WHERE event_id=? AND generation_id=?",
                                        (identity, self.store.generation_id))
            if previous is None:
                self.delivery.register(event)
            # Retain the first exact bindings; replay may not silently rebind
            # an already accepted/unknown event to a new preference revision.
            return self.delivery.deliver(identity, device_id=self.device_id, channel=self.CHANNEL,
                                         notifier=self.notifier, live=self.live)

    def deliver_changes(self):
        """Bridge exact canonical/watch changes; never performs a source read."""
        with self._lock:
            changes = self._new_changes()
            by_item = {}
            for change in changes:
                by_item.setdefault(change["item_id"], []).append(change)
            receipts = []
            for item in self.store.list_items():
                item_id = item["item_id"]
                item_changes = by_item.get(item_id, ())
                if not item_changes:
                    continue
                change = item_changes[-1]
                pref = self.controls.preference(item_id)
                if pref.mute or pref.known_revision >= item["content_revision"] or not (pref.follow or pref.wait):
                    continue
                status = self.watches.reader_watch_status(item_id)
                if (change["revision"] == item["content_revision"] and status
                        and status.get("preference_revision") == pref.revision and status.get("status") != "stopped"):
                    watch = self.watches.get_watch(status["watch_id"]) if status.get("watch_id") else None
                    for watch_change in status.get("changes", ())[-1:]:
                        if watch_change["kind"] not in {"met", "reopened"} or not pref.wait:
                            continue
                        evaluation = json.loads(watch_change["evaluation_json"])
                        current_eval = status.get("evaluation") or {}
                        event_id = fingerprint(json_text([self.store.generation_id, "watch:" + watch_change["change_id"],
                            "wait_" + watch_change["kind"], item_id, item["content_revision"]]))
                        registered = self.ledger.one("SELECT 1 FROM reader_attention_events WHERE event_id=?", (event_id,))
                        # Historical met/reopened states are not new attention.
                        if (evaluation.get("state") != current_eval.get("state")
                                or watch is None or (watch_change["canonical_revision"] != watch["evaluated_revision"] and not registered)
                                or watch_change["created_at"] < change["created_at"]):
                            continue
                        ids = {id_ for witness in evaluation.get("witnesses", ()) for id_ in witness["claim_ids"]}
                        ids.update(set(evaluation.get("revoked_claim_ids", ())) & set(item["claim_ids"]))
                        if watch_change["kind"] == "reopened":
                            with closing(self.store.connect()) as conn:
                                ids.update(row[0] for old in tuple(ids) for row in conn.execute(
                                    "SELECT from_claim_id FROM claim_relations WHERE to_claim_id=?", (old,)))
                        bindings, evidence = self._claim_dependencies(item_id, ids)
                        title = item["title"] + ("：等待条件已满足" if watch_change["kind"] == "met" else "：原来的等待结论需要重看")
                        body = pref.wait + ("。已有对应证据，可以打开继续读。" if watch_change["kind"] == "met"
                                            else "。支持原结论的证据发生变化，现为 " + current_eval.get("state", "unknown") + "。")
                        receipts.append(self._send(AttentionEvent("watch:" + watch_change["change_id"], item_id,
                            item["content_revision"], title, body, "wait_" + watch_change["kind"], bindings, evidence)))
                for correction_change in item_changes:
                    if correction_change["revision"] <= pref.known_revision:
                        continue
                    corrections = []
                    for action in json.loads(correction_change["actions_json"]):
                        relations = [action] if action.get("type") == "CLAIM_RELATION" else action.get("claim_relations", ())
                        corrections.extend(r for r in relations if r.get("relation") in {
                            "corrects_or_retracts", "corrects", "retracts", "disputes", "contradicts"}
                            and r.get("to_claim_id") in item["claim_ids"])
                    if not corrections:
                        continue
                    ids = {r[key] for r in corrections for key in ("from_claim_id", "to_claim_id")}
                    bindings, evidence = self._claim_dependencies(item_id, ids)
                    reasons = "；".join(dict.fromkeys(r.get("reason") or "相关证据改变了原来的结论" for r in corrections))
                    receipt = self._send(AttentionEvent("correction:" + str(correction_change["change_id"]), item_id,
                        item["content_revision"], item["title"] + "：关注的内容有更正", reasons, "correction", bindings, evidence),
                        source_revision=correction_change["revision"])
                    if receipt is not None:
                        receipts.append(receipt)
            return receipts

    def deliver_brief(self, receipt):
        """Use the successful immutable plan's exact contributors and references."""
        with self._lock:
            floor = self._floor()
            if floor is None or receipt.status != "success":
                return None
            edition = self.ledger.one("SELECT * FROM reader_brief_editions WHERE generation_id=? AND edition_id=?",
                                      (self.store.generation_id, receipt.edition_id))
            history = self.ledger.one("SELECT * FROM reader_brief_history WHERE generation_id=? AND edition_id=? AND content_revision=?",
                                      (self.store.generation_id, receipt.edition_id, receipt.revision))
            if (edition is None or history is None or edition["status"] != "success"
                    or edition["content_revision"] != receipt.revision or edition["request_id"] != receipt.request_id):
                raise ReadingError("attention_brief_receipt_stale")
            plan = BriefPlan.from_json(history["plan_json"])
            inputs = {value["id"]: value for value in json.loads(history["inputs_json"])}
            selected = {id_ for paragraph in plan.paragraphs for id_ in paragraph.change_ids}
            new, bindings, evidence = set(), {}, set()
            for id_ in selected:
                value = inputs.get(id_)
                if value is None or not str(id_).isdigit():
                    raise ReadingError("attention_brief_change_invalid")
                with closing(self.store.connect()) as conn:
                    event = conn.execute("SELECT * FROM change_events WHERE change_id=?", (int(id_),)).fetchone()
                if event is None or event["item_id"] != value["item_id"] or event["revision"] != value["content_revision"]:
                    raise ReadingError("attention_brief_change_invalid")
                event_ids = value.get("contributing_change_ids") or [id_]
                if id_ not in event_ids or any(not str(event_id).isdigit() for event_id in event_ids):
                    raise ReadingError("attention_brief_change_invalid")
                with closing(self.store.connect()) as conn:
                    contributors = [conn.execute("SELECT * FROM change_events WHERE change_id=?", (int(event_id),)).fetchone()
                                    for event_id in event_ids]
                if any(row is None or row["item_id"] != value["item_id"]
                       or not value.get("before_revision", 0) < row["revision"] <= value["content_revision"]
                       for row in contributors):
                    raise ReadingError("attention_brief_change_invalid")
                if any(row["change_id"] > floor and row["semantic_effect"] != "none" for row in contributors):
                    new.add(id_)
                pref = self.controls.preference(value["item_id"])
                bindings[value["item_id"]] = ContentBinding(value["item_id"], value["content_revision"], pref.revision)
            if not new:
                return None
            for paragraph in plan.paragraphs:
                evidence.update((ref.item_id, ref.evidence_id) for ref in paragraph.evidence_refs)
            body = "\n".join(p.text for p in plan.paragraphs if set(p.change_ids) & new)
            return self._send(AttentionEvent("brief:" + receipt.edition_id + ":" + str(receipt.revision),
                receipt.edition_id, receipt.revision, plan.title or "今日简报", body, "brief",
                tuple(bindings.values()), tuple(EvidenceTarget(*pair) for pair in sorted(evidence))))

    def _status_page(self, statuses, codes, derived=None, views=None):
        lines = []
        labels = {"met": "已满足", "unmet": "尚未满足", "unknown": "尚不确定", "contested": "存在相反证据", "stopped": "已停止"}
        for result in statuses:
            item = self.store.get_item(result["item_id"])
            path = self.publisher.resolve(result["item_id"])
            label = wikilink("WGO/" + path, item["title"]) if path else item["title"]
            evaluation = result.get("evaluation") or {}
            lines.append("### " + label + "\n\n" + labels.get(result["status"], "尚不确定") + "。" + result.get("reason", ""))
            if evaluation.get("review_due"):
                lines.append("已到复查时间；这不表示条件已满足。")
            if result.get("counterevidence"):
                lines.append("保留反证或撤回记录，请结合条目中的来源阅读。")
        if not lines:
            lines.append("尚无持续关注或等待条件。阅读、检索和明确的「知道了」操作不依赖可见打开插件。")
        failures = self.ledger.all("SELECT code,COUNT(*) AS n FROM reader_event_receipts WHERE generation_id=? "
                                   "AND status='rejected' GROUP BY code ORDER BY code LIMIT 20", (self.store.generation_id,))
        if failures:
            lines.append("阅读事件未确认（原文件保留）：" + "；".join(row["code"] + " × " + str(row["n"]) for row in failures))
        if codes:
            lines.append("待处理状态：" + "、".join(sorted(codes)[:20]))
        if views:
            names = {"unchanged": "无需更新", "pending": "继续整理中", "complete": "本轮完成",
                     "deferred": "等待更多依据", "blocked": "受阻"}
            lines.append("目录整理：" + names.get(views.get("status"), "尚未确认")
                         + "；本轮处理 " + str(views.get("pages_processed", 0)) + " 页。")
        if derived:
            labels = {"ready": "就绪", "pending": "等待", "blocked": "受阻"}
            index, projection = derived["index"], derived["projection"]
            index_detail = "索引维护：" + labels.get(index["state"], "尚未确认")
            if index.get("pending") is not None:
                index_detail += "，待处理 " + str(index["pending"])
            if index.get("codes"):
                index_detail += "；" + "；".join(
                    code + " × " + str(count) for code, count in sorted(index["codes"].items()))
            lines.append(index_detail)
            projection_detail = (
                "正文维护：" + labels.get(projection["state"], "尚未确认")
                + "，已就绪 " + str(projection["ready"] if projection["ready"] is not None else "未核对")
                + "/" + str(projection["total"] if projection["total"] is not None else "未核对")
                + "，待发布 " + str(projection["pending_outbox"] if projection["pending_outbox"] is not None else "未核对")
                + "，阅读冲突 " + str(projection["conflicts"] if projection["conflicts"] is not None else "未核对")
                + "，失败 " + str(projection["failed"])
                + "，本轮未处理 " + str(projection["unprocessed"] if projection["unprocessed"] is not None else "未核对"))
            if projection.get("codes"):
                projection_detail += "；" + "；".join(
                    code + " × " + str(count) for code, count in sorted(projection["codes"].items()))
            lines.append(projection_detail)
            changes = projection.get("changes")
            if changes is not None:
                lines.append("变化页维护：本轮选择 " + str(changes["selected"])
                    + "，仍待处理 " + str(changes["remaining"])
                    + "，失败 " + str(changes["failed"])
                    + "，冲突 " + str(changes["conflicts"]) + "。")
        body = "\n\n".join(lines)
        signature = fingerprint(body)
        old = self.ledger.one("SELECT * FROM reader_runtime_status WHERE generation_id=?", (self.store.generation_id,))
        revision = (old["revision"] if old else 0) + int(old is None or old["body_hash"] != signature)
        result = self.publisher.publish_document(object_id="control:reader-status", path="阅读状态.md", title="阅读状态",
                                                 body=body, content_revision=revision)
        if result.status in {"published", "unchanged"}:
            with self.ledger.transaction() as conn:
                conn.execute("INSERT OR REPLACE INTO reader_runtime_status VALUES(?,?,?)", (self.store.generation_id, signature, revision))

    def poll(self, *, maintain_derived=False, maintain_views=False):
        with self._lock:
            codes, statuses, actions, attention = set(), [], [], []
            items = self.store.list_items()
            properties = self._properties(items, codes)
            try:
                if self.publisher.record(self.controls.MANAGEMENT_ID) is None:
                    self.controls.refresh_management()
                actions = self.controls.process_management(acted_at=datetime.now(timezone.utc).isoformat())
                self._reflect_actions(actions, codes)
            except Exception as exc:
                codes.add(self._code(exc))
            view_actions, views = [], None
            try:
                view_actions = self.runtime.view_controls.process_management()
                for receipt in view_actions:
                    if receipt.code:
                        codes.add(self._code(ReadingError(receipt.code)))
            except Exception as exc:
                codes.add(self._code(exc))
            query, opened = "deferred", 0
            try:
                self.controls.initialize_query(property_key=self.properties["query"])
                query = self.controls.run_query().status
            except Exception as exc:
                codes.add(self._code(exc))
            try:
                opened = self._consume_opens(codes)
            except Exception as exc:
                codes.add(self._code(exc))
            for item in items:
                pref = self.controls.preference(item["item_id"])
                previous = self.watches.reader_watch_status(item["item_id"])
                if not (pref.follow or pref.wait or previous):
                    continue
                try:
                    statuses.append(self.watches.sync_reader_watch(item["item_id"], pref.revision, pref.wait, pref.follow))
                except Exception as exc:
                    codes.add(self._code(exc))
                    statuses.append({"item_id": item["item_id"], "status": "unknown", "reason": "等待条件暂未完成校验。"})
            try:
                attention = self.deliver_changes()
            except Exception as exc:
                codes.add(self._code(exc))
            if maintain_views:
                try:
                    views = self.runtime.maintain_views(max_pages=1)
                    if views.get("code"):
                        codes.add(self._code(ReadingError(views["code"])))
                except Exception as exc:
                    codes.add(self._code(exc))
                    views = {"status": "blocked", "pages_processed": 0}
            derived = None
            if maintain_derived:
                try:
                    derived = self.runtime.maintain_derived(max_rounds=1)
                except Exception as exc:
                    code = self._code(exc)
                    codes.add(code)
                    empty = {"state": "blocked", "complete": False, "pending": None,
                             "failed": 1, "codes": {code: 1}, "ready": None, "total": None,
                             "pending_outbox": None, "conflicts": None, "unprocessed": None}
                    derived = {"state": "blocked", "complete": False,
                               "index": dict(empty), "projection": dict(empty)}
            for item in items:
                try:
                    if self.publisher.ready(item["item_id"], item["content_revision"]):
                        self.controls.register_item_actions(item["item_id"], item["content_revision"])
                except Exception as exc:
                    codes.add(self._code(exc))
            try:
                self.controls.refresh_management()
            except Exception as exc:
                codes.add(self._code(exc))
            try:
                self._status_page(statuses, codes, derived, views)
            except Exception as exc:
                codes.add(self._code(exc))
            return {"properties": properties, "actions": len(actions), "view_actions": len(view_actions),
                    "view_action_states": {state: sum(r.status == state for r in view_actions)
                                           for state in sorted({r.status for r in view_actions})},
                    "views": views, "query": query, "open_events": opened,
                    "watch_states": {key: sum(s["status"] == key for s in statuses) for key in sorted({s["status"] for s in statuses})},
                    "attention": {key: sum(r.status == key for r in attention) for key in sorted({r.status for r in attention})},
                    "derived": derived,
                    "codes": sorted(codes)[:20]}
