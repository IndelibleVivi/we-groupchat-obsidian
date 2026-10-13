"""Local attention receipts. OS acceptance is never read or known state."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Protocol
import uuid

from core.url_safety import redact_urls_in_text
from .controls import ReaderControls
from .projections import ReadingError, ReadingLedger, ProjectionPublisher, fingerprint, json_text


@dataclass(frozen=True)
class ContentBinding:
    item_id: str
    content_revision: int
    preference_revision: int


@dataclass(frozen=True)
class EvidenceTarget:
    item_id: str
    evidence_id: str


@dataclass(frozen=True)
class AttentionEvent:
    source_event_id: str
    target_id: str
    target_revision: int
    title: str
    body: str
    kind: str
    content_bindings: tuple[ContentBinding, ...]
    evidence_targets: tuple[EvidenceTarget, ...]


@dataclass(frozen=True)
class NotificationEnvelope:
    event_id: str
    receipt_id: str
    target_id: str
    title: str
    body: str


@dataclass(frozen=True)
class NotifyResult:
    status: str
    receipt_id: str


class CaptureNotifier(Protocol):
    def send(self, notification: NotificationEnvelope) -> NotifyResult: ...


@dataclass(frozen=True)
class DeliveryReceipt:
    event_id: str
    status: str
    receipt_id: str = ""
    code: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reader_attention_events (
 event_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,event_json TEXT NOT NULL,disposition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reader_deliveries (
 event_id TEXT NOT NULL,device_id TEXT NOT NULL,channel TEXT NOT NULL,status TEXT NOT NULL,
 receipt_id TEXT NOT NULL,code TEXT NOT NULL,PRIMARY KEY(event_id,device_id,channel));
CREATE TABLE IF NOT EXISTS reader_delivery_attempts (
 receipt_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,device_id TEXT NOT NULL,channel TEXT NOT NULL,
 status TEXT NOT NULL,code TEXT NOT NULL);
"""


def _event_from_json(text: str) -> AttentionEvent:
    value = json.loads(text)
    return AttentionEvent(value["source_event_id"], value["target_id"], value["target_revision"],
                          value["title"], value["body"], value["kind"],
                          tuple(ContentBinding(**b) for b in value["content_bindings"]),
                          tuple(EvidenceTarget(**e) for e in value["evidence_targets"]))


class DeliveryService:
    def __init__(self, ledger: ReadingLedger, publisher: ProjectionPublisher, controls: ReaderControls):
        self.ledger = ledger
        self.publisher = publisher
        self.controls = controls
        ledger.install(_SCHEMA)

    def _disposition(self, event: AttentionEvent) -> str:
        prefs = [self.controls.preference(b.item_id) for b in event.content_bindings]
        if any(p.mute for p in prefs):
            return "suppressed"
        if any(p.known_revision >= b.content_revision for p, b in zip(prefs, event.content_bindings)):
            return "suppressed"
        if event.kind == "brief":
            return "candidate"
        if event.kind in {"wait_met", "wait_reopened"} and any(p.wait for p in prefs):
            return "candidate"
        if event.kind == "correction" and any(p.follow or p.wait for p in prefs):
            return "candidate"
        return "brief"

    def register(self, event: AttentionEvent) -> DeliveryReceipt:
        if (not event.source_event_id or not event.target_id or type(event.target_revision) is not int
                or event.target_revision <= 0 or not event.title.strip() or not event.body.strip()
                or event.kind not in {"change", "brief", "wait_met", "wait_reopened", "correction"}
                or not event.content_bindings or not event.evidence_targets
                or any(type(b.content_revision) is not int or b.content_revision <= 0 for b in event.content_bindings)
                or len({b.item_id for b in event.content_bindings}) != len(event.content_bindings)):
            raise ReadingError("invalid_attention_event")
        if not {e.item_id for e in event.evidence_targets} <= {b.item_id for b in event.content_bindings}:
            raise ReadingError("invalid_attention_evidence")
        payload = asdict(event)
        payload["title"] = redact_urls_in_text(event.title)
        payload["body"] = redact_urls_in_text(event.body)
        serialized = json_text(payload)
        # Render revision, filenames and observed modification times are
        # deliberately absent from attention identity.
        event_id = fingerprint(json_text([self.ledger.generation_id, event.source_event_id,
                                          event.kind, event.target_id, event.target_revision]))
        disposition = self._disposition(event)
        with self.ledger.transaction() as conn:
            previous = conn.execute("SELECT * FROM reader_attention_events WHERE event_id=?", (event_id,)).fetchone()
            if previous and previous["event_json"] != serialized:
                raise ReadingError("attention_event_identity_conflict")
            conn.execute("INSERT OR IGNORE INTO reader_attention_events VALUES (?,?,?,?)",
                         (event_id, self.ledger.generation_id, serialized, disposition))
        return DeliveryReceipt(event_id, previous["disposition"] if previous else disposition)

    def _event(self, event_id: str) -> AttentionEvent:
        row = self.ledger.one("SELECT * FROM reader_attention_events WHERE event_id=? AND generation_id=?",
                              (event_id, self.ledger.generation_id))
        if row is None:
            raise ReadingError("attention_event_missing")
        return _event_from_json(row["event_json"])

    def _validate_current(self, event: AttentionEvent):
        for binding in event.content_bindings:
            preference = self.controls.preference(binding.item_id)
            if preference.mute:
                raise ReadingError("attention_muted")
            if preference.known_revision >= binding.content_revision:
                raise ReadingError("attention_already_known")
            if (self.publisher.current_revision(binding.item_id) != binding.content_revision
                    or preference.revision != binding.preference_revision):
                raise ReadingError("attention_binding_changed")
            if not self.publisher.ready(binding.item_id, binding.content_revision):
                raise ReadingError("attention_item_not_ready")
        if self._disposition(event) != "candidate":
            raise ReadingError("attention_requires_brief")
        if not self.publisher.ready(event.target_id, event.target_revision):
            raise ReadingError("attention_target_not_ready")
        for evidence in event.evidence_targets:
            self.publisher.evidence_link(evidence.item_id, evidence.evidence_id)

    def deliver(self, event_id: str, *, device_id: str, channel: str,
                notifier: CaptureNotifier | None = None, live: bool = False) -> DeliveryReceipt:
        """Rebuild defaults to a recorded silent disposition and zero notifier calls."""
        if not device_id or not channel:
            raise ReadingError("invalid_delivery_channel")
        event = self._event(event_id)
        with self.publisher.locked():
            with self.ledger.transaction() as conn:
                previous = conn.execute("SELECT * FROM reader_deliveries WHERE event_id=? AND device_id=? AND channel=?",
                                        (event_id, device_id, channel)).fetchone()
                if previous and previous["status"] in {"accepted", "unknown", "sending", "rejected"}:
                    status = "unknown" if previous["status"] == "sending" else previous["status"]
                    if status == "unknown":
                        conn.execute("UPDATE reader_deliveries SET status='unknown',code='outcome_requires_reconciliation' "
                                     "WHERE event_id=? AND device_id=? AND channel=?", (event_id, device_id, channel))
                    return DeliveryReceipt(event_id, status, previous["receipt_id"], previous["code"])
                if not live:
                    conn.execute("INSERT INTO reader_deliveries VALUES (?,?,?,?,?,?) ON CONFLICT(event_id,device_id,channel) "
                                 "DO UPDATE SET status=excluded.status,code=excluded.code",
                                 (event_id, device_id, channel, "silent", "", "rebuild_silent"))
                    return DeliveryReceipt(event_id, "silent", code="rebuild_silent")
                if notifier is None:
                    raise ReadingError("capture_notifier_required")
                try:
                    self._validate_current(event)
                except ReadingError as exc:
                    return DeliveryReceipt(event_id, "deferred", code=exc.code)
                receipt_id = uuid.uuid4().hex
                conn.execute("INSERT INTO reader_deliveries VALUES (?,?,?,?,?,?) ON CONFLICT(event_id,device_id,channel) "
                             "DO UPDATE SET status=excluded.status,receipt_id=excluded.receipt_id,code=excluded.code",
                             (event_id, device_id, channel, "sending", receipt_id, ""))
                conn.execute("INSERT INTO reader_delivery_attempts VALUES (?,?,?,?,?,?)",
                             (receipt_id, event_id, device_id, channel, "sending", ""))
            # Intent is durable before the local notifier boundary. A crash
            # after this point leaves sending/unknown and cannot resend.
            with self.ledger.transaction() as conn:
                try:
                    self._validate_current(event)
                except ReadingError as exc:
                    conn.execute("UPDATE reader_deliveries SET status='deferred',code=? WHERE receipt_id=?",
                                 (exc.code, receipt_id))
                    conn.execute("UPDATE reader_delivery_attempts SET status='not_sent',code=? WHERE receipt_id=?",
                                 (exc.code, receipt_id))
                    return DeliveryReceipt(event_id, "deferred", receipt_id, exc.code)
                try:
                    result = notifier.send(NotificationEnvelope(event_id, receipt_id, event.target_id, event.title, event.body))
                    if (not isinstance(result, NotifyResult) or result.receipt_id != receipt_id
                            or result.status not in {"accepted", "rejected", "unknown"}):
                        status, code = "unknown", "invalid_notifier_receipt"
                    else:
                        status, code = result.status, ""
                except Exception:
                    status, code = "unknown", "notifier_outcome_unknown"
                conn.execute("UPDATE reader_deliveries SET status=?,code=? WHERE receipt_id=?", (status, code, receipt_id))
                conn.execute("UPDATE reader_delivery_attempts SET status=?,code=? WHERE receipt_id=?", (status, code, receipt_id))
            return DeliveryReceipt(event_id, status, receipt_id, code)

    def reconcile(self, *, event_id: str, device_id: str, channel: str,
                  receipt_id: str, observed_status: str) -> DeliveryReceipt:
        if observed_status not in {"accepted", "rejected"}:
            raise ReadingError("invalid_delivery_reconciliation")
        with self.ledger.transaction() as conn:
            row = conn.execute("SELECT * FROM reader_deliveries WHERE event_id=? AND device_id=? AND channel=?",
                               (event_id, device_id, channel)).fetchone()
            if row is None or row["receipt_id"] != receipt_id or row["status"] not in {"unknown", "sending"}:
                raise ReadingError("delivery_receipt_mismatch")
            conn.execute("UPDATE reader_deliveries SET status=?,code='exact_receipt_reconciled' WHERE receipt_id=?",
                         (observed_status, receipt_id))
            conn.execute("UPDATE reader_delivery_attempts SET status=?,code='exact_receipt_reconciled' WHERE receipt_id=?",
                         (observed_status, receipt_id))
        return DeliveryReceipt(event_id, observed_status, receipt_id, "exact_receipt_reconciled")

    def resolve_click(self, event_id: str) -> str:
        """Return a freshly resolved local path; clicking is not an open observation."""
        event = self._event(event_id)
        path = self.publisher.resolve(event.target_id)
        if path is None:
            raise ReadingError("attention_click_target_missing")
        return str(self.publisher._safe_path(path))
