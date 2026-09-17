"""Canonical read-source seam for WeChat message databases.

This module names the contract that ``core/wechat_db.py::WeChatDB`` already
implemented as an implicit duck type, and it owns the shared helpers that the
monitor, resource capture and the optional Drive file sync previously copied
or drifted apart. It is standard-library only so the contract stays
import-safe; it activates no platform source and claims no Windows support.

Boundaries kept deliberately outside this seam:

- ``MonitorStateStore`` owns the monitor checkpoint/progress CAS; the source
  reader only returns tentative cursors for that store to commit.
- ``SourceInventoryStore`` owns the durable expected-shard set; this module
  only translates one inventory snapshot into the consumer-facing shape.
- Tier-B query/presentation helpers (contacts, FTS search, emoji, AI text
  formatting) stay in the macOS reader. Windows source activation is a later
  phase and must not be inferred from this contract.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Protocol


# The bounded, content-free failure vocabulary one source reader may report.
# This is an inventory of codes that already exist in the current contract, not
# new vocabulary. Grouped by emitter:
#
#   - raised by the macOS reader (core/wechat_db.py): source_shard_unavailable,
#     source_shard_unknown, source_snapshot_failed,
#     source_message_decode_failed, source_cache_only, source_key_missing,
#     source_inventory_incomplete
#   - raised by the durable inventory store and surfaced through the reader
#     (core/source_inventory.py via core/wechat_db.py): source_inventory_corrupt,
#     source_inventory_lock_unavailable, source_inventory_write_failed,
#     source_namespace_invalid, source_relative_path_invalid
#   - raised by this seam: source_cursor_invalid, source_unavailable
#   - reported today as bounded inventory/consumer evidence rather than as an
#     exception: source_inventory_uninitialized, source_inventory_scan_failed,
#     source_shards_unavailable, source_missing_file, source_unreadable
#   - monitor/source-contract code callers must not lose: source_generation_changed
#
# Anything outside this set is normalized to the caller's fallback, so an
# unknown failure can never leak source content. Fallbacks themselves
# (for example ``source_inventory_unavailable``) are caller arguments and are
# deliberately not listed here.
SOURCE_ERROR_CODES = frozenset({
    "source_unavailable",
    "source_shard_unavailable",
    "source_shard_unknown",
    "source_snapshot_failed",
    "source_message_decode_failed",
    "source_cursor_invalid",
    "source_cache_only",
    "source_key_missing",
    "source_inventory_incomplete",
    "source_inventory_uninitialized",
    "source_inventory_scan_failed",
    "source_inventory_corrupt",
    "source_inventory_lock_unavailable",
    "source_inventory_write_failed",
    "source_namespace_invalid",
    "source_relative_path_invalid",
    "source_shards_unavailable",
    "source_missing_file",
    "source_unreadable",
    "source_generation_changed",
})

class SourceUnavailableError(RuntimeError):
    """A content-free source read failure that must not advance a cursor.

    ``code`` is a bounded identifier only. Callers must not add source paths,
    chat identities or row bodies to it.
    """

    code = "source_shard_unavailable"

    def __init__(self, code: str = "source_shard_unavailable"):
        self.code = str(code or "source_shard_unavailable")
        super().__init__(self.code)


def normalize_source_error(exc, *, fallback: str = "source_shard_unavailable") -> str:
    """Return the bounded content-free code for one source failure."""
    code = str(getattr(exc, "code", "") or "").strip()
    if code in SOURCE_ERROR_CODES:
        return code
    return str(fallback or "source_shard_unavailable")


def encode_cursor_token(create_time, rowid) -> str:
    """Render the opaque ``(create_time, rowid)`` keyset position."""
    return json.dumps(
        [int(create_time), int(rowid)],
        separators=(",", ":"),
    )


# The two structural method names that describe the legacy timestamp-page
# subset. ``source_capabilities`` reports ``shard_pages`` when a source exposes
# either one, so page dispatch must accept exactly the same subset; keeping the
# names in one place stops the capability check and the reader from drifting.
_TIMESTAMP_PAGE_METHODS = (
    "get_cursor_messages_for_shard",
    "get_messages_for_shard",
)


def _timestamp_page_reader(source):
    """Resolve the timestamp page reader, or fail closed as a bounded error.

    ``source_capabilities`` accepts readers that expose only
    ``get_cursor_messages_for_shard`` as the ``shard_pages`` capability, so this
    dispatch must resolve that same subset. Reading the fallback as an eager
    ``getattr`` default would evaluate ``source.get_messages_for_shard`` even
    when the cursor variant is present and raise ``AttributeError`` for that
    legitimate shape; a present-but-non-callable attribute must also fail closed
    rather than leak ``TypeError``. Only callable members are admissible, and a
    source that exposes none of them is a content-free source failure.
    """
    for name in _TIMESTAMP_PAGE_METHODS:
        candidate = getattr(source, name, None)
        if callable(candidate):
            return candidate
    raise SourceUnavailableError("source_shard_unavailable")


def decode_cursor_token(token, *, since_ts=0) -> tuple[int, int]:
    """Decode one keyset token, or the timestamp floor when it is absent.

    A malformed token is a source failure, not an empty page: falling back to
    the timestamp floor would silently replay rows a consumer already consumed.
    """
    if not token:
        return (max(0, int(since_ts)), 0)
    try:
        decoded = json.loads(str(token))
        return (int(decoded[0]), int(decoded[1]))
    except (TypeError, ValueError, IndexError, KeyError, json.JSONDecodeError) as exc:
        raise SourceUnavailableError("source_cursor_invalid") from exc


def source_capabilities(source) -> frozenset:
    """Report which bounded read capabilities one source object exposes.

    The vocabulary is closed: ``source_inventory``, ``shard_pages``,
    ``keyset_pages``, ``snapshot``. Nothing else is reported.

    Legacy and fixture adapters expose only timestamp pages. Production
    ``WeChatDB`` additionally exposes the durable inventory and the keyset page
    reader. Detection stays structural instead of using a runtime-checkable
    Protocol, because a full ``isinstance`` check would reject the fixture
    adapters that only implement the legacy subset.
    """
    if source is None:
        return frozenset()
    exposed = []
    if callable(getattr(source, "get_source_inventory", None)):
        exposed.append("source_inventory")
    if any(callable(getattr(source, name, None)) for name in _TIMESTAMP_PAGE_METHODS):
        exposed.append("shard_pages")
    if callable(getattr(source, "get_cursor_page_for_shard", None)):
        exposed.append("keyset_pages")
    if callable(getattr(source, "source_snapshot", None)):
        exposed.append("snapshot")
    return frozenset(exposed)


def supports_monitor_source_cursors(source) -> bool:
    """A monitor run needs both the durable inventory and the keyset reader."""
    exposed = source_capabilities(source)
    return "source_inventory" in exposed and "keyset_pages" in exposed


@contextmanager
def source_snapshot(source):
    """Pin one multi-page traversal when the reader supports it."""
    snapshot = getattr(source, "source_snapshot", None)
    if snapshot is None:
        yield
        return
    with snapshot():
        yield


def bind_source_inventory(source, chats) -> dict:
    """Describe one source revision for the resource and Drive consumers.

    The returned shape is one dict, matching what both consumers already read.
    ``source=None`` and inventory-less legacy readers stay distinct: a legacy
    reader still reports per-chat shards, while a missing source reports
    ``source_unavailable``. Only the caller decides whether a partial
    inventory blocks work -- the monitor requires completeness, while
    resources and Drive keep reporting partial observations.
    """
    chats = list(chats or [])
    if source is None:
        evidence = {
            "schema": "we-groupchat-obsidian.source-inventory.v1",
            "source_namespace": "",
            "inventory_revision": 0,
            "inventory_digest": "",
            "complete": False,
            "counts": {},
            "error_codes": ["source_unavailable"],
        }
        return {
            "complete": False,
            "inventory_digest": "",
            "inventory_revision": 0,
            "source_namespace": "",
            "counts": {},
            "error_codes": ["source_unavailable"],
            "error_code": "source_unavailable",
            "degraded_shards": 1,
            "shards_by_username": {},
            "evidence": evidence,
        }

    inventory_reader = getattr(source, "get_source_inventory", None)
    if callable(inventory_reader):
        try:
            inventory = dict(inventory_reader(update=True, sensitive=False) or {})
        except SourceUnavailableError as exc:
            code = normalize_source_error(exc)
            evidence = {
                "schema": "we-groupchat-obsidian.source-inventory.v1",
                "source_namespace": "",
                "inventory_revision": 0,
                "inventory_digest": "",
                "complete": False,
                "counts": {},
                "error_codes": [code],
            }
            return {
                "complete": False,
                "inventory_digest": "",
                "inventory_revision": 0,
                "source_namespace": "",
                "counts": {},
                "error_codes": [code],
                "error_code": code,
                "degraded_shards": 1,
                "shards_by_username": {},
                "evidence": evidence,
            }
        source_shards = [
            str(value)
            for value in inventory.get("present_generation_ids") or []
            if str(value)
        ]
        counts = {
            str(key): int(value or 0)
            for key, value in (inventory.get("counts") or {}).items()
        }
        error_codes = [
            str(value)
            for value in inventory.get("error_codes") or []
            if str(value)
        ]
        complete = bool(inventory.get("complete"))
        error_code = error_codes[0] if error_codes else (
            "" if complete else "source_inventory_incomplete"
        )
        degraded_shards = sum(
            counts.get(state, 0)
            for state in ("missing_file", "key_missing", "cache_only", "unreadable")
        )
        evidence = {
            "schema": str(inventory.get("schema") or ""),
            "source_namespace": str(inventory.get("source_namespace") or ""),
            "inventory_revision": int(inventory.get("inventory_revision") or 0),
            "inventory_digest": str(inventory.get("inventory_digest") or ""),
            "complete": complete,
            "counts": counts,
            "error_codes": error_codes,
        }
        return {
            **evidence,
            "error_code": error_code,
            "degraded_shards": max(1, degraded_shards) if not complete else 0,
            "shards_by_username": {
                chat["username"]: list(source_shards) for chat in chats
            },
            "evidence": evidence,
        }

    shards_by_username = {}
    error_codes = []
    degraded_shards = 0
    for chat in chats:
        username = chat["username"]
        source_failed = False
        try:
            source_shards = list(source.get_message_shards(username))
        except SourceUnavailableError as exc:
            degraded_shards += 1
            error_codes.append(normalize_source_error(exc))
            source_shards = []
            source_failed = True
        if not source_shards and not source_failed:
            degraded_shards += 1
            if not error_codes:
                error_codes.append("source_shards_unavailable")
        shards_by_username[username] = source_shards
    manifest = [
        {
            "chat_key": chat["chat_key"],
            "source_shards": list(shards_by_username.get(chat["username"], [])),
        }
        for chat in chats
    ]
    evidence = {
        "schema": "legacy-source-adapter.v1",
        "source_namespace": "",
        "inventory_revision": 0,
        "inventory_digest": _json_digest(manifest),
        "complete": degraded_shards == 0,
        "counts": {},
        "error_codes": list(dict.fromkeys(error_codes)),
    }
    return {
        **evidence,
        "error_code": error_codes[0] if error_codes else "",
        "degraded_shards": degraded_shards,
        "shards_by_username": shards_by_username,
        "evidence": evidence,
    }


def source_state_evidence(binding, *, degraded_shards=None, error_code="") -> dict:
    """Return the durable meta values describing one inventory binding."""
    binding = binding or {}
    degraded = (
        int(binding.get("degraded_shards") or 0)
        if degraded_shards is None
        else int(degraded_shards or 0)
    )
    return {
        "source_state": "source_degraded" if degraded else "healthy",
        "source_error_code": str(error_code or binding.get("error_code") or ""),
        "source_inventory_evidence": json.dumps(
            dict(binding.get("evidence") or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def read_source_page(
    source,
    username,
    source_shard_id,
    *,
    cursor_token="",
    cursor_timestamp=0,
    seen_ids=(),
    limit=500,
) -> tuple[list, str, bool]:
    """Read one page from one frozen source shard.

    Keyset-capable readers own the exact same-second position: the request stays
    bounded by ``limit`` and ``next_cursor`` must be persisted by the caller.

    Readers that only expose timestamp pages (the legacy compatibility
    contract) cannot express that position, so the request is widened by the
    identities the caller already consumed and the page is de-duplicated
    afterwards. That makes the request grow with the same-second backlog, which
    is exactly why production readers use the keyset path; capping the legacy
    request instead would silently hide rows that share the cursor timestamp
    and let a caller treat a partial read as EOF.
    """
    if source is None:
        raise SourceUnavailableError("source_unavailable")
    page_limit = max(1, int(limit))
    keyset_reader = getattr(source, "get_cursor_page_for_shard", None)
    if callable(keyset_reader):
        result = keyset_reader(
            username,
            source_shard_id,
            cursor_token=str(cursor_token or ""),
            since_ts=max(0, int(cursor_timestamp)),
            limit=page_limit,
        )
        result = result or {}
        return (
            list(result.get("messages") or []),
            str(result.get("next_cursor") or cursor_token or ""),
            bool(result.get("exhausted")),
        )

    seen = set(seen_ids or ())
    request_limit = page_limit + len(seen)
    reader = _timestamp_page_reader(source)
    messages = reader(
        username,
        source_shard_id,
        since_ts=max(0, int(cursor_timestamp)),
        limit=request_limit,
        page_forward=True,
        since_inclusive=True,
    )
    fresh = []
    for message in messages:
        timestamp = int(message.get("timestamp") or 0)
        identity = str(message.get("source_message_id") or "")
        if not identity or timestamp < cursor_timestamp:
            continue
        if timestamp == cursor_timestamp and identity in seen:
            continue
        fresh.append(message)
    fresh.sort(key=lambda item: (
        int(item.get("timestamp") or 0),
        str(item.get("source_message_id") or ""),
    ))
    page = fresh[:page_limit]
    return page, "", len(messages) < request_limit or not page


def _json_digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


class WeChatSource(Protocol):
    """Tier-A read contract for one WeChat message source.

    ``WeChatDB`` is the current macOS implementation. Implementations must
    preserve the existing on-disk identity formulas -- the logical shard id
    owned by ``SourceInventoryStore`` and the generation/message ids owned by
    ``WeChatDB`` -- because changing them would re-identify durable state
    instead of reading it.
    """

    def get_source_inventory(
        self, *, update: bool = True, sensitive: bool = False
    ) -> dict:
        """Return one path-free completeness snapshot of the expected shards."""

    def get_message_shards(self, username) -> list:
        """Return the present generation ids, or fail closed."""

    def get_messages_for_shard(self, username, source_shard_id, **kwargs) -> list:
        """Return one user-visible timestamp page for a known shard."""

    def get_cursor_page_for_shard(
        self,
        username,
        source_shard_id,
        *,
        cursor_token="",
        since_ts=0,
        limit=500,
    ) -> dict:
        """Return one bounded keyset page: messages, next_cursor, exhausted."""

    def source_snapshot(self):
        """Pin one multi-page traversal; may be absent on legacy adapters."""
