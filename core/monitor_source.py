"""Bounded raw-source cursor batches for the topic monitor."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import heapq
import json


_ACTIVE_STATES = frozenset({"present", "generation_changed"})


class MonitorSourceError(RuntimeError):
    """A path-free source batch failure."""

    def __init__(self, code: str, *, details: dict | None = None):
        self.code = str(code or "monitor_source_error")
        self.details = dict(details or {})
        super().__init__(self.code)


@dataclass(frozen=True)
class MonitorSourceBatch:
    inventory_digest: str
    inventory_revision: int
    source_cursors: dict[str, dict[str, str]]
    raw_messages: tuple[dict, ...]
    visible_messages: tuple[dict, ...]
    raw_count: int
    source_eof: bool
    last_checked_ts: float
    source_batch_id: str


def supports_monitor_source_cursors(db) -> bool:
    return callable(getattr(db, "get_source_inventory", None)) and callable(
        getattr(db, "get_cursor_page_for_shard", None)
    )


def _checkpoint(value) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _cursor_token(timestamp: float, rowid: int) -> str:
    return json.dumps(
        [max(0, int(timestamp)), max(0, int(rowid))],
        separators=(",", ":"),
    )


def _exception_code(exc: Exception, fallback: str) -> str:
    code = str(getattr(exc, "code", "") or "").strip()
    return code or fallback


def _read_inventory(db) -> tuple[dict, tuple[dict, ...]]:
    try:
        snapshot = db.get_source_inventory(update=True)
    except Exception as exc:
        raise MonitorSourceError(
            _exception_code(exc, "source_inventory_unavailable")
        ) from exc
    if not isinstance(snapshot, dict):
        raise MonitorSourceError("source_inventory_invalid")
    if snapshot.get("complete") is not True:
        raise MonitorSourceError("source_inventory_incomplete")
    digest = str(snapshot.get("inventory_digest") or "").strip()
    revision = snapshot.get("inventory_revision")
    if (
        not digest
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise MonitorSourceError("source_inventory_invalid")

    rows = snapshot.get("shards")
    if not isinstance(rows, list):
        raise MonitorSourceError("source_inventory_invalid")
    active = []
    logical_ids = set()
    generation_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise MonitorSourceError("source_inventory_invalid")
        state = str(row.get("state") or "")
        if state == "explicitly_retired":
            continue
        if state not in _ACTIVE_STATES:
            raise MonitorSourceError("source_inventory_incomplete")
        logical_id = str(row.get("logical_shard_id") or "").strip()
        generation_id = str(row.get("generation_id") or "").strip()
        if (
            not logical_id
            or not generation_id
            or logical_id in logical_ids
            or generation_id in generation_ids
        ):
            raise MonitorSourceError("source_inventory_invalid")
        logical_ids.add(logical_id)
        generation_ids.add(generation_id)
        active.append({
            "logical_shard_id": logical_id,
            "generation_id": generation_id,
        })
    if not active:
        raise MonitorSourceError("source_inventory_incomplete")
    active.sort(key=lambda row: row["logical_shard_id"])
    return snapshot, tuple(active)


def _normalized_state_cursors(value) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MonitorSourceError("monitor_source_cursors_corrupt")
    normalized = {}
    for logical_id, item in value.items():
        if not isinstance(logical_id, str) or not logical_id or not isinstance(item, dict):
            raise MonitorSourceError("monitor_source_cursors_corrupt")
        generation_id = item.get("generation_id")
        cursor_token = item.get("cursor_token")
        if not isinstance(generation_id, str) or not isinstance(cursor_token, str):
            raise MonitorSourceError("monitor_source_cursors_corrupt")
        normalized[logical_id] = {
            "generation_id": generation_id,
            "cursor_token": cursor_token,
        }
        start_cursor = item.get("start_cursor")
        if start_cursor is not None:
            if not isinstance(start_cursor, str):
                raise MonitorSourceError("monitor_source_cursors_corrupt")
            normalized[logical_id]["start_cursor"] = start_cursor
    return normalized


def _generation_admission_plan(
    snapshot: dict,
    shards: tuple[dict, ...],
    previous_cursors: dict[str, dict[str, str]],
    *,
    raw_limit: int,
) -> dict | None:
    """Describe an exact, bounded reconciliation without reading source rows.

    The returned plan is deliberately non-executable: an old cursor is not
    evidence that the replacement contains the same prefix.  It gives an
    operator/recovery path a stable exact-inventory input while this ordinary
    monitor run remains read-only and leaves the checkpoint untouched.
    """
    changes = []
    for shard in shards:
        logical_id = shard["logical_shard_id"]
        generation_id = shard["generation_id"]
        prior = previous_cursors.get(logical_id)
        if prior is None:
            changes.append({
                "logical_shard_id": logical_id,
                "previous_generation_id": "",
                "current_generation_id": generation_id,
                "previous_cursor_token": "",
                "reason": "new_logical_shard",
            })
        elif prior["generation_id"] != generation_id:
            changes.append({
                "logical_shard_id": logical_id,
                "previous_generation_id": prior["generation_id"],
                "current_generation_id": generation_id,
                "previous_cursor_token": prior["cursor_token"],
                "reason": "generation_replaced",
            })
    if not changes:
        return None
    plan = {
        "schema": "we-groupchat-obsidian.generation-admission-plan.v1",
        "inventory_digest": str(snapshot["inventory_digest"]),
        "inventory_revision": int(snapshot["inventory_revision"]),
        "max_reconciliation_rows": max(1, int(raw_limit)),
        "changes": changes,
        "policy": "prove_stable_prefix_or_explicit_bounded_replay",
    }
    canonical = json.dumps(plan, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    plan["plan_id"] = "wgadmit_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return plan


def _message_position(message: dict, generation_id: str) -> tuple[tuple, str]:
    if not isinstance(message, dict):
        raise MonitorSourceError("source_envelope_invalid")
    envelope = message.get("source_envelope")
    if not isinstance(envelope, dict):
        raise MonitorSourceError("source_envelope_invalid")
    observed_generation = str(
        envelope.get("source_generation_id")
        or envelope.get("db_shard_id")
        or ""
    )
    if observed_generation != generation_id:
        raise MonitorSourceError("source_generation_changed")
    source_message_id = str(
        message.get("source_message_id")
        or envelope.get("source_message_id")
        or ""
    ).strip()
    if not source_message_id:
        raise MonitorSourceError("source_envelope_invalid")
    try:
        create_time = int(envelope.get("create_time"))
        rowid = int(envelope.get("rowid"))
    except (TypeError, ValueError) as exc:
        raise MonitorSourceError("source_envelope_invalid") from exc
    if create_time < 0 or rowid < 0:
        raise MonitorSourceError("source_envelope_invalid")
    return (create_time, source_message_id), _cursor_token(create_time, rowid)


def _is_visible(message: dict) -> bool:
    return bool(str(message.get("text") or message.get("content") or "").strip())


def _batch_identity(username: str, messages: list[dict]) -> str:
    source_ids = [str(message.get("source_message_id") or "") for message in messages]
    basis = "\0".join([
        "monitor-source-batch-v1",
        str(username or ""),
        *source_ids,
    ])
    return "wgbatch_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


def initialize_monitor_source_state(db, checkpoint) -> dict:
    """Bind a new monitor state to the current source generations from now."""
    snapshot, shards = _read_inventory(db)
    # WeChat timestamps have one-second precision.  Starting at row 0 may
    # replay at most the current second, but it cannot lose a row inserted
    # later in that same second after first enable.
    start = _cursor_token(_checkpoint(checkpoint), 0)
    cursors = {
        row["logical_shard_id"]: {
            "generation_id": row["generation_id"],
            "cursor_token": start,
            "start_cursor": start,
        }
        for row in shards
    }
    verify_monitor_source_inventory(
        db,
        str(snapshot["inventory_digest"]),
        int(snapshot["inventory_revision"]),
    )
    return {
        "source_cursors": cursors,
        "source_inventory_digest": str(snapshot["inventory_digest"]),
        "source_inventory_revision": int(snapshot["inventory_revision"]),
    }


def verify_monitor_source_inventory(db, digest: str, revision: int) -> None:
    current, _shards = _read_inventory(db)
    # ``inventory_revision`` belongs to the shared inventory ledger and may
    # advance because another source namespace changed.  The digest is scoped
    # to this WeChat source namespace and is therefore the actual snapshot CAS.
    _ = revision
    if str(current.get("inventory_digest") or "") != str(digest or ""):
        raise MonitorSourceError("source_generation_changed")


def read_monitor_source_batch(
    db,
    username: str,
    state: dict,
    *,
    raw_limit: int,
    page_size: int = 128,
) -> MonitorSourceBatch:
    """Read one globally ordered raw-row batch without advancing durable state."""
    limit = max(1, int(raw_limit))
    chunk_size = max(1, min(limit, int(page_size)))
    snapshot, shards = _read_inventory(db)
    inventory_digest = str(snapshot["inventory_digest"])
    inventory_revision = int(snapshot["inventory_revision"])
    previous_checkpoint = _checkpoint(state.get("last_checked_ts"))
    previous_cursors = _normalized_state_cursors(state.get("source_cursors"))
    migration_start = _cursor_token(previous_checkpoint, 0)

    # Existing installations without per-shard cursors retain their one-time
    # compatibility migration.  Once any shard binding exists, however, a
    # replacement or newly discovered shard must be admitted explicitly.  The
    # ordinary monitor may not borrow a global timestamp and silently skip an
    # older unseen row.
    if previous_cursors:
        admission_plan = _generation_admission_plan(
            snapshot,
            shards,
            previous_cursors,
            raw_limit=limit,
        )
        if admission_plan is not None:
            raise MonitorSourceError(
                "source_generation_admission_required",
                details={"generation_admission_plan": admission_plan},
            )

    cursors = {}
    streams = {}
    heap = []

    def fetch(logical_id: str) -> None:
        stream = streams[logical_id]
        request_cursor = stream["fetch_cursor"]
        try:
            page = db.get_cursor_page_for_shard(
                username,
                stream["generation_id"],
                cursor_token=request_cursor,
                since_ts=0,
                limit=chunk_size,
            )
        except Exception as exc:
            try:
                verify_monitor_source_inventory(
                    db, inventory_digest, inventory_revision
                )
            except MonitorSourceError as inventory_exc:
                raise inventory_exc from exc
            raise MonitorSourceError(
                _exception_code(exc, "source_shard_unavailable")
            ) from exc
        if not isinstance(page, dict) or not isinstance(page.get("messages"), list):
            raise MonitorSourceError("source_page_invalid")
        exhausted = page.get("exhausted")
        next_cursor = page.get("next_cursor")
        if not isinstance(exhausted, bool) or not isinstance(next_cursor, str):
            raise MonitorSourceError("source_page_invalid")
        entries = []
        for message in page["messages"]:
            key, row_cursor = _message_position(message, stream["generation_id"])
            entries.append({"key": key, "cursor": row_cursor, "message": message})
        if entries and next_cursor == request_cursor:
            raise MonitorSourceError("source_cursor_stalled")
        if not entries and not exhausted:
            raise MonitorSourceError("source_cursor_stalled")
        stream["buffer"] = entries
        stream["index"] = 0
        stream["page_exhausted"] = exhausted
        stream["fetch_cursor"] = next_cursor

    def push_head(logical_id: str) -> None:
        stream = streams[logical_id]
        entry = stream["buffer"][stream["index"]]
        heapq.heappush(
            heap,
            (*entry["key"], logical_id),
        )

    for shard in shards:
        logical_id = shard["logical_shard_id"]
        generation_id = shard["generation_id"]
        prior = previous_cursors.get(logical_id) or {}
        start_cursor = (
            prior["cursor_token"]
            if prior.get("generation_id") == generation_id
            else migration_start
        )
        cursors[logical_id] = {
            "generation_id": generation_id,
            "cursor_token": start_cursor,
        }
        if prior.get("start_cursor"):
            cursors[logical_id]["start_cursor"] = prior["start_cursor"]
        elif not prior:
            cursors[logical_id]["start_cursor"] = migration_start
        streams[logical_id] = {
            "generation_id": generation_id,
            "fetch_cursor": start_cursor,
            "buffer": [],
            "index": 0,
            "page_exhausted": False,
        }
        fetch(logical_id)
        if streams[logical_id]["buffer"]:
            push_head(logical_id)

    consumed = []
    while heap and len(consumed) < limit:
        _timestamp, _source_message_id, logical_id = heapq.heappop(heap)
        stream = streams[logical_id]
        entry = stream["buffer"][stream["index"]]
        consumed.append(entry["message"])
        cursors[logical_id]["cursor_token"] = entry["cursor"]
        stream["index"] += 1
        if len(consumed) >= limit:
            break
        if stream["index"] < len(stream["buffer"]):
            push_head(logical_id)
        elif not stream["page_exhausted"]:
            fetch(logical_id)
            if stream["buffer"]:
                push_head(logical_id)

    source_eof = all(
        stream["page_exhausted"]
        and stream["index"] >= len(stream["buffer"])
        for stream in streams.values()
    )
    verify_monitor_source_inventory(db, inventory_digest, inventory_revision)

    last_checked_ts = previous_checkpoint
    if consumed:
        last_checked_ts = max(
            previous_checkpoint,
            max(float(message.get("timestamp") or 0) for message in consumed),
        )
    visible = tuple(message for message in consumed if _is_visible(message))
    return MonitorSourceBatch(
        inventory_digest=inventory_digest,
        inventory_revision=inventory_revision,
        source_cursors=cursors,
        raw_messages=tuple(consumed),
        visible_messages=visible,
        raw_count=len(consumed),
        source_eof=source_eof,
        last_checked_ts=last_checked_ts,
        source_batch_id=_batch_identity(username, consumed) if consumed else "",
    )
