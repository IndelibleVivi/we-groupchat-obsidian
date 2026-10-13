"""Durable, revisioned state for one topic-monitor checkpoint."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
import stat
from typing import Callable

from .platform import LockBusy, LockMode
from .source_adapter import decode_cursor_token
from .state_storage import StateFileNotRegular, StateStorage


MONITOR_STATE_SCHEMA = "we-groupchat-obsidian.monitor-state.v1"
MONITOR_PENDING_STATE_SCHEMA = "we-groupchat-obsidian.monitor-state.v2"
MONITOR_V3_STATE_SCHEMA = "we-groupchat-obsidian.monitor-state.v3"
MONITOR_CAPTURE_RECEIPT_SCHEMA = "we-groupchat-obsidian.monitor-capture-receipt.v1"
MONITOR_PROCESSING_RECEIPT_SCHEMA = "we-groupchat-obsidian.monitor-processing-receipt.v1"
MONITOR_MIGRATION_RECEIPT_SCHEMA = "we-groupchat-obsidian.monitor-migration-receipt.v1"
MONITOR_ROLLBACK_RECEIPT_SCHEMA = "we-groupchat-obsidian.monitor-rollback-receipt.v1"

_BINDING_FIELDS = frozenset({
    "semantic_generation_id", "source_namespace", "chat_id",
    "source_inventory_digest", "source_inventory_revision",
})
_V3_ONLY_FIELDS = frozenset({
    "semantic_generation_id", "captured_cursors", "capture_receipt",
    "processing_receipt", "migration_receipt",
})
_V3_REQUIRED_FIELDS = _BINDING_FIELDS | _V3_ONLY_FIELDS | frozenset({
    "source_cursors", "last_checked_ts",
})
_V3_METADATA_FIELDS = frozenset({
    "ai_failure_count", "ai_last_error_code", "ai_last_error_ts",
    "ai_next_retry_after", "last_topic_key", "last_notified_ts",
})
_RECEIPT_COMMON_FIELDS = _BINDING_FIELDS | frozenset({"schema", "receipt_id"})
_PROGRESS_RECEIPT_FIELDS = _RECEIPT_COMMON_FIELDS | frozenset({
    "state_revision", "input_cursors", "output_cursors",
})
_CAPTURE_RECEIPT_FIELDS = _PROGRESS_RECEIPT_FIELDS | frozenset({"batch_id"})
_PROCESSING_RECEIPT_FIELDS = _PROGRESS_RECEIPT_FIELDS | frozenset({
    "processing_id", "capture_receipt_ids", "canonical_receipt_id", "outbox_receipt_id",
})
_MIGRATION_RECEIPT_FIELDS = _RECEIPT_COMMON_FIELDS | frozenset({
    "migration_id", "old_state_revision", "old_state_fingerprint",
    "input_source_cursors", "input_captured_cursors",
    "output_source_cursors", "output_captured_cursors",
    "capture_receipt_ids", "processing_receipt_ids",
    "canonical_receipt_id", "outbox_receipt_id", "pending_resolution",
})

_ROLLBACK_RECEIPT_FIELDS = _RECEIPT_COMMON_FIELDS | frozenset({
    "rollback_id", "run_id", "journal_id", "state_revision", "current_state_fingerprint",
    "backup_bytes_digest", "backup_state_fingerprint", "retained_raw_store_id",
    "retained_raw_receipt_id", "retained_capture_receipt_ids", "retained_captured_cursors",
    "replay_receipt_id", "replay_destination_id", "replay_from_cursors", "replay_through_cursors",
})


class MonitorStateError(RuntimeError):
    """A content-free monitor-state failure."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class MonitorStateSnapshot:
    """One parsed state revision.

    ``existed`` reports whether the canonical file existed before the operation
    that returned the snapshot. ``data`` excludes storage-owned schema and
    revision fields.
    """

    data: dict
    revision: int
    existed: bool
    schema: str | None = None


def _require(condition, code="monitor_state_invalid"):
    if not condition:
        raise MonitorStateError(code)


def _integer(value, *, minimum=0):
    _require(type(value) is int and value >= minimum)


def _identity(value):
    _require(isinstance(value, str) and bool(value) and value == value.strip())


def _digest(value):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def _json_copy(value):
    # Preserve scalar types and reject non-JSON containers/key coercions before
    # taking an isolated snapshot of caller-owned, possibly nested data.
    def validate(item):
        if type(item) in (str, int, bool, type(None)):
            return
        if type(item) is float:
            _require(math.isfinite(item))
        elif type(item) is list:
            for child in item:
                validate(child)
        elif type(item) is dict:
            _require(all(type(key) is str for key in item))
            for child in item.values():
                validate(child)
        else:
            raise MonitorStateError("monitor_state_invalid")
    try:
        validate(value)
        return json.loads(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError, RecursionError) as exc:
        raise MonitorStateError("monitor_state_invalid") from exc


def _fingerprint(value):
    return hashlib.sha256(json.dumps(
        _json_copy(value), ensure_ascii=True, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def monitor_state_fingerprint(snapshot: MonitorStateSnapshot) -> str:
    """Bind an explicit migration to the complete parsed old snapshot."""
    return _fingerprint({
        "schema": snapshot.schema, "revision": snapshot.revision,
        "existed": bool(snapshot.existed or snapshot.revision > 0), "data": snapshot.data,
    })


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("nonfinite JSON number")


def _cursor_position(token):
    _require(isinstance(token, str) and bool(token))
    try:
        pair = json.loads(token, parse_constant=_invalid_constant)
        _require(type(pair) is list and len(pair) == 2)
        for value in pair:
            _integer(value)
        # Strict shape/type admission precedes the shared source decoder, whose
        # historical callers intentionally support coercible timestamp floors.
        return decode_cursor_token(token)
    except (ValueError, TypeError, OverflowError) as exc:
        raise MonitorStateError("monitor_state_invalid") from exc


def _validate_vector(vector, *, allow_empty=False):
    _require(type(vector) is dict and (allow_empty or bool(vector)))
    for logical_id, entry in vector.items():
        _identity(logical_id)
        _require(type(entry) is dict)
        _require(set(entry) in (
            {"generation_id", "cursor_token"},
            {"generation_id", "cursor_token", "start_cursor"},
        ))
        _identity(entry["generation_id"])
        position = _cursor_position(entry["cursor_token"])
        if "start_cursor" in entry:
            _require(_cursor_position(entry["start_cursor"]) <= position)


def _ordered_vectors(before, after):
    """Validate a monotone vector without losing its physical/start binding."""
    _validate_vector(before)
    _validate_vector(after)
    _require(set(before) == set(after), "monitor_state_cursor_binding_changed")
    for key, prior in before.items():
        later = after[key]
        _require(
            prior["generation_id"] == later["generation_id"]
            and ("start_cursor" in prior) == ("start_cursor" in later)
            and prior.get("start_cursor") == later.get("start_cursor"),
            "monitor_state_cursor_binding_changed",
        )
        _require(
            _cursor_position(prior["cursor_token"]) <= _cursor_position(later["cursor_token"]),
            "monitor_state_cursor_regression",
        )


def _receipt_ids(value):
    _require(type(value) is list and bool(value))
    for item in value:
        _identity(item)
    _require(len(set(value)) == len(value))


def _validate_binding(value):
    for key in ("semantic_generation_id", "source_namespace", "chat_id"):
        _identity(value[key])
    _digest(value["source_inventory_digest"])
    _integer(value["source_inventory_revision"], minimum=1)


def _same_binding(receipt, state, *, inventory_revision="equal"):
    _require(
        all(receipt[key] == state[key] for key in _BINDING_FIELDS if key != "source_inventory_revision"),
        "monitor_state_receipt_binding_mismatch",
    )
    observed, current = receipt["source_inventory_revision"], state["source_inventory_revision"]
    valid = (observed == current if inventory_revision == "equal" else
             observed <= current if inventory_revision == "at_most" else observed >= current)
    _require(valid, "monitor_state_receipt_binding_mismatch")


def _progress_receipt(receipt, *, processing=False):
    fields = _PROCESSING_RECEIPT_FIELDS if processing else _CAPTURE_RECEIPT_FIELDS
    schema = MONITOR_PROCESSING_RECEIPT_SCHEMA if processing else MONITOR_CAPTURE_RECEIPT_SCHEMA
    _require(type(receipt) is dict and set(receipt) == fields)
    _require(receipt["schema"] == schema)
    _validate_binding(receipt)
    _identity(receipt["receipt_id"])
    _integer(receipt["state_revision"], minimum=1)
    _ordered_vectors(receipt["input_cursors"], receipt["output_cursors"])
    if processing:
        for key in ("processing_id", "canonical_receipt_id", "outbox_receipt_id"):
            _identity(receipt[key])
        _receipt_ids(receipt["capture_receipt_ids"])
    else:
        _identity(receipt["batch_id"])


def _pending_resolution(resolution, migration):
    if resolution is None:
        return
    _require(type(resolution) is dict and set(resolution) == {
        "disposition", "lineage_id", "pending_descriptor", "member_coverage",
    })
    _require(isinstance(resolution["disposition"], str) and resolution["disposition"] in {"adopted", "superseded_by_rebuild"})
    _identity(resolution["lineage_id"])
    pending = resolution["pending_descriptor"]
    _require(type(pending) is dict)
    _require(pending.get("schema") == "we-groupchat-obsidian.monitor-pending-batch.v1")
    _require(pending.get("checksum") == _fingerprint({
        key: value for key, value in pending.items() if key != "checksum"
    }))
    ids = pending.get("source_message_ids")
    _receipt_ids(ids)
    _integer(pending.get("raw_count"), minimum=1)
    _require(pending["raw_count"] == len(ids))
    for key in (
        "checkpoint_fingerprint", "policy_fingerprint", "inventory_digest", "content_fingerprint",
    ):
        _digest(pending.get(key))
    _identity(pending.get("source_batch_id"))
    _require(pending["source_batch_id"].startswith("wgbatch_"))
    _validate_vector(pending.get("source_cursors"))
    coverage = resolution["member_coverage"]
    _require(type(coverage) is list and len(coverage) == len(ids))
    for expected_id, member in zip(ids, coverage):
        _require(type(member) is dict and set(member) == {
            "source_message_id", "capture_receipt_id", "processing_receipt_id",
        })
        _require(member["source_message_id"] == expected_id)
        _require(member["capture_receipt_id"] in migration["capture_receipt_ids"])
        _require(member["processing_receipt_id"] in migration["processing_receipt_ids"])


def _migration_receipt(receipt):
    _require(type(receipt) is dict and set(receipt) == _MIGRATION_RECEIPT_FIELDS)
    _require(receipt["schema"] == MONITOR_MIGRATION_RECEIPT_SCHEMA)
    _validate_binding(receipt)
    for key in ("receipt_id", "migration_id", "canonical_receipt_id", "outbox_receipt_id"):
        _identity(receipt[key])
    _integer(receipt["old_state_revision"])
    _digest(receipt["old_state_fingerprint"])
    for key in ("input_source_cursors", "input_captured_cursors"):
        _validate_vector(receipt[key], allow_empty=True)
    _ordered_vectors(receipt["output_source_cursors"], receipt["output_captured_cursors"])
    _receipt_ids(receipt["capture_receipt_ids"])
    _receipt_ids(receipt["processing_receipt_ids"])
    _pending_resolution(receipt["pending_resolution"], receipt)


def _validate_v3(data, *, revision):
    _require(_V3_REQUIRED_FIELDS <= set(data) <= _V3_REQUIRED_FIELDS | _V3_METADATA_FIELDS)
    _validate_binding(data)
    _ordered_vectors(data["source_cursors"], data["captured_cursors"])
    _integer(data["last_checked_ts"])
    _require(data["last_checked_ts"] == max(
        _cursor_position(entry["cursor_token"])[0] for entry in data["source_cursors"].values()
    ))
    migration = data["migration_receipt"]
    _migration_receipt(migration)
    _same_binding(migration, data, inventory_revision="at_most")
    _require(migration["old_state_revision"] < revision)
    for field, receipt_field, processing, migration_field in (
        ("captured_cursors", "capture_receipt", False, "output_captured_cursors"),
        ("source_cursors", "processing_receipt", True, "output_source_cursors"),
    ):
        _ordered_vectors(migration[migration_field], data[field])
        receipt = data[receipt_field]
        if receipt is None:
            _require(data[field] == migration[migration_field])
        else:
            _progress_receipt(receipt, processing=processing)
            _same_binding(receipt, data, inventory_revision="at_most" if processing else "equal")
            _require(migration["old_state_revision"] < receipt["state_revision"] < revision)
            _require(receipt["output_cursors"] == data[field])
            _ordered_vectors(migration[migration_field], receipt["input_cursors"])
    for key in _V3_METADATA_FIELDS & set(data):
        value = data[key]
        if key == "ai_failure_count":
            _integer(value)
        elif key == "ai_last_error_code":
            _require(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value) is not None)
        elif key == "last_topic_key":
            _identity(value)
        else:
            _require(type(value) in (int, float) and value >= 0 and (type(value) is int or math.isfinite(value)))


class MonitorStateStore:
    """Own locked reads and compare-and-swap writes for one state file."""

    def __init__(self, path: str | os.PathLike[str], *, file_lock=None, platform_services=None):
        self.path = os.path.abspath(os.path.expanduser(os.fspath(path)))
        self._file_lock = file_lock
        self._storage = StateStorage(self.path, platform_services=platform_services)

    def _lock_service(self):
        if self._file_lock is None:
            self._file_lock = self._storage.services.require("locks")
        return self._file_lock

    def _lock(self, *, exclusive: bool):
        try:
            path = self._storage.prepare()
            return self._lock_service().acquire(
                path + ".lock",
                mode=LockMode.EXCLUSIVE if exclusive else LockMode.SHARED,
                blocking=True,
            )
        except StateFileNotRegular as exc:
            raise MonitorStateError("monitor_state_not_regular") from exc
        except OSError as exc:
            raise MonitorStateError("monitor_state_lock_unavailable") from exc

    @staticmethod
    def _unlock(lock_handle) -> None:
        lock_handle.close()

    def _read_locked(self) -> MonitorStateSnapshot:
        try:
            path_stat = os.lstat(self.path)
        except FileNotFoundError:
            return MonitorStateSnapshot(data={}, revision=0, existed=False)
        except OSError as exc:
            raise MonitorStateError("monitor_state_unreadable") from exc

        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise MonitorStateError("monitor_state_not_regular")

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self._storage.operational_path(), flags)
        except OSError as exc:
            raise MonitorStateError("monitor_state_not_regular") from exc

        try:
            opened_stat = os.fstat(fd)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise MonitorStateError("monitor_state_not_regular")
            try:
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = -1
                    payload = json.load(
                        handle, object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
                    )
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise MonitorStateError("monitor_state_corrupt") from exc
            except OSError as exc:
                raise MonitorStateError("monitor_state_unreadable") from exc
        finally:
            if fd >= 0:
                os.close(fd)

        return self._parse_payload(payload)

    @staticmethod
    def _parse_payload(payload) -> MonitorStateSnapshot:
        if not isinstance(payload, dict):
            raise MonitorStateError("monitor_state_corrupt")
        try:
            _json_copy(payload)
            has_schema = "schema" in payload
            has_revision = "revision" in payload
            if not has_schema and not has_revision:
                _require("pending_source_batch" not in payload and not (_V3_ONLY_FIELDS & set(payload)))
                return MonitorStateSnapshot(data=dict(payload), revision=0, existed=True)
            schema = payload.get("schema")
            _require(isinstance(schema, str) and schema in {
                MONITOR_STATE_SCHEMA, MONITOR_PENDING_STATE_SCHEMA, MONITOR_V3_STATE_SCHEMA,
            })
            _integer(payload.get("revision"), minimum=1)
            data = dict(payload)
            revision = data.pop("revision")
            data.pop("schema")
            if schema == MONITOR_V3_STATE_SCHEMA:
                _validate_v3(data, revision=revision)
            else:
                _require(not (_V3_ONLY_FIELDS & set(data)))
                _require(("pending_source_batch" in data) == (schema == MONITOR_PENDING_STATE_SCHEMA))
            return MonitorStateSnapshot(data=data, revision=revision, existed=True, schema=schema)
        except (MonitorStateError, TypeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
            raise MonitorStateError("monitor_state_corrupt") from exc

    @staticmethod
    def _state_data(value: dict) -> dict:
        if not isinstance(value, dict):
            raise TypeError("monitor state must be a dict")
        data = _json_copy(value)
        if "schema" in data:
            _require(isinstance(data["schema"], str) and data["schema"] in {
                MONITOR_STATE_SCHEMA, MONITOR_PENDING_STATE_SCHEMA, MONITOR_V3_STATE_SCHEMA,
            })
        if "revision" in data:
            _integer(data["revision"], minimum=1)
        data.pop("schema", None)
        data.pop("revision", None)
        return data

    @staticmethod
    def _check_revision(current, expected_revision):
        _integer(expected_revision)
        _require(current.revision == expected_revision, "monitor_state_conflict")

    @staticmethod
    def _generic_state(current, value):
        data = MonitorStateStore._state_data(value)
        if current.schema == MONITOR_V3_STATE_SCHEMA:
            _require(value.get("schema", current.schema) == current.schema, "monitor_state_v3_downgrade_forbidden")
            _require(
                {key: val for key, val in current.data.items() if key not in _V3_METADATA_FIELDS}
                == {key: val for key, val in data.items() if key not in _V3_METADATA_FIELDS},
                "monitor_state_v3_receipt_required",
            )
            _validate_v3(data, revision=current.revision + 1)
        else:
            _require(
                not (_V3_ONLY_FIELDS & set(data)) and value.get("schema") != MONITOR_V3_STATE_SCHEMA,
                "monitor_state_v3_install_required",
            )
        return data

    def _write_locked_unmapped(
        self,
        data: dict,
        *,
        revision: int,
        existed_before: bool,
        schema: str | None = None,
    ) -> MonitorStateSnapshot:
        # The caller selects v3 only after its dedicated receipt validation.
        selected_schema = schema or (
            MONITOR_PENDING_STATE_SCHEMA if "pending_source_batch" in data else MONITOR_STATE_SCHEMA
        )
        payload = {
            "schema": selected_schema, "revision": revision, **self._state_data(data),
        }
        encoded = (json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False,
        ) + "\n").encode("utf-8")
        self._storage.write_bytes(encoded)

        return MonitorStateSnapshot(
            data=self._state_data(data),
            revision=int(revision),
            existed=bool(existed_before),
            schema=selected_schema,
        )

    def _write_locked(
        self,
        data: dict,
        *,
        revision: int,
        existed_before: bool,
        schema: str | None = None,
    ) -> MonitorStateSnapshot:
        try:
            return self._write_locked_unmapped(
                data,
                revision=revision,
                existed_before=existed_before,
                schema=schema,
            )
        except OSError as exc:
            raise MonitorStateError("monitor_state_write_failed") from exc

    @contextmanager
    def execution_lock(self):
        """Serialize a whole monitor run without holding the checkpoint lock.

        A second worker may inspect state, but may not replay the same pending
        provider request. Process exit releases ownership; there is no lease
        timeout that could expire while a provider is still responding.
        """
        try:
            path = self._storage.prepare()
            handle = self._lock_service().acquire(
                path + ".run.lock", mode=LockMode.EXCLUSIVE, blocking=False,
            )
        except LockBusy as exc:
            raise MonitorStateError("monitor_worker_busy") from exc
        except OSError as exc:
            raise MonitorStateError("monitor_worker_lock_unavailable") from exc
        try:
            yield
        finally:
            handle.close()

    def read(self) -> MonitorStateSnapshot:
        fd = self._lock(exclusive=False)
        try:
            return self._read_locked()
        finally:
            self._unlock(fd)

    def inspect(self) -> MonitorStateSnapshot:
        """Read atomic state without creating a directory or lock file.

        State publication uses the platform's atomic replace, so an observation sees either
        the complete old file or the complete new file.  Writers still use the
        locked ``read``/``commit`` surfaces; this method exists for health and
        other strictly read-only diagnostics.
        """
        return self._read_locked()

    def initialize_if_absent(self, initial_state: dict) -> MonitorStateSnapshot:
        fd = self._lock(exclusive=True)
        try:
            current = self._read_locked()
            if current.existed:
                return current
            return self._write_locked(
                self._generic_state(current, initial_state),
                revision=1,
                existed_before=False,
            )
        finally:
            self._unlock(fd)

    def commit(self, expected_revision: int, new_state: dict) -> MonitorStateSnapshot:
        fd = self._lock(exclusive=True)
        try:
            current = self._read_locked()
            self._check_revision(current, expected_revision)
            return self._write_locked(
                self._generic_state(current, new_state),
                revision=current.revision + 1,
                existed_before=current.existed,
                schema=MONITOR_V3_STATE_SCHEMA if current.schema == MONITOR_V3_STATE_SCHEMA else None,
            )
        finally:
            self._unlock(fd)

    def update(self, mutator: Callable[[dict], dict | None]) -> MonitorStateSnapshot:
        if not callable(mutator):
            raise TypeError("mutator must be callable")
        fd = self._lock(exclusive=True)
        try:
            current = self._read_locked()
            working = _json_copy(current.data)
            result = mutator(working)
            if result is not None:
                working = result
            return self._write_locked(
                self._generic_state(current, working),
                revision=current.revision + 1,
                existed_before=current.existed,
                schema=MONITOR_V3_STATE_SCHEMA if current.schema == MONITOR_V3_STATE_SCHEMA else None,
            )
        finally:
            self._unlock(fd)


    def install_v3(self, expected_revision: int, new_state: dict, migration_receipt: dict) -> MonitorStateSnapshot:
        """Explicitly install a generation after its durable migration is verified.

        Receipt lookup/durability belongs to the generation DB caller. This
        boundary verifies its exact old-state, pending-coverage and new-state
        bindings; it never infers a successful database write from a boolean.
        """
        fd = self._lock(exclusive=True)
        try:
            current = self._read_locked()
            self._check_revision(current, expected_revision)
            _require(type(new_state) is dict)
            _require(new_state.get("schema", MONITOR_V3_STATE_SCHEMA) == MONITOR_V3_STATE_SCHEMA)
            data = self._state_data(new_state)
            receipt = _json_copy(migration_receipt)
            _migration_receipt(receipt)
            if "migration_receipt" in data:
                _require(data["migration_receipt"] == receipt, "monitor_state_receipt_binding_mismatch")
            data["migration_receipt"] = receipt
            _validate_v3(data, revision=current.revision + 1)
            _require(data["capture_receipt"] is None and data["processing_receipt"] is None)
            _same_binding(receipt, data)
            _require(
                receipt["old_state_revision"] == current.revision
                and receipt["old_state_fingerprint"] == monitor_state_fingerprint(current)
                and receipt["input_source_cursors"] == current.data.get("source_cursors", {})
                and receipt["input_captured_cursors"] == current.data.get(
                    "captured_cursors", current.data.get("source_cursors", {}),
                )
                and receipt["output_source_cursors"] == data["source_cursors"]
                and receipt["output_captured_cursors"] == data["captured_cursors"],
                "monitor_state_migration_binding_mismatch",
            )
            if current.schema == MONITOR_V3_STATE_SCHEMA:
                _require(
                    data["semantic_generation_id"] != current.data["semantic_generation_id"],
                    "monitor_state_generation_unchanged",
                )
                _require(
                    data["source_namespace"] == current.data["source_namespace"]
                    and data["chat_id"] == current.data["chat_id"],
                    "monitor_state_migration_binding_mismatch",
                )
            if "pending_source_batch" in current.data:
                # Reuse the pending owner's checksum/checkpoint validation. The
                # archived receipt later retains that complete descriptor.
                from .monitor_source import MonitorSourceError, pending_monitor_source_batch
                try:
                    pending = pending_monitor_source_batch(current.data)
                except MonitorSourceError as exc:
                    raise MonitorStateError("monitor_state_pending_migration_invalid") from exc
                resolution = receipt["pending_resolution"]
                _require(
                    resolution is not None and resolution["pending_descriptor"] == pending,
                    "monitor_state_pending_migration_invalid",
                )
            else:
                _require(receipt["pending_resolution"] is None, "monitor_state_pending_migration_invalid")
            return self._write_locked(
                data, revision=current.revision + 1, existed_before=current.existed,
                schema=MONITOR_V3_STATE_SCHEMA,
            )
        finally:
            self._unlock(fd)

    def _commit_progress(self, expected_revision, cursors, receipt, *, processing):
        fd = self._lock(exclusive=True)
        try:
            current = self._read_locked()
            self._check_revision(current, expected_revision)
            _require(current.schema == MONITOR_V3_STATE_SCHEMA, "monitor_state_v3_required")
            receipt = _json_copy(receipt)
            cursors = _json_copy(cursors)
            _progress_receipt(receipt, processing=processing)
            _same_binding(receipt, current.data, inventory_revision="at_most" if processing else "at_least")
            field = "source_cursors" if processing else "captured_cursors"
            receipt_field = "processing_receipt" if processing else "capture_receipt"
            _require(
                receipt["state_revision"] == current.revision
                and receipt["input_cursors"] == current.data[field]
                and receipt["output_cursors"] == cursors,
                "monitor_state_receipt_binding_mismatch",
            )
            for previous in (
                current.data["capture_receipt"], current.data["processing_receipt"],
                current.data["migration_receipt"],
            ):
                if previous is not None:
                    _require(receipt["receipt_id"] != previous["receipt_id"], "monitor_state_receipt_reused")
            prior = current.data[receipt_field]
            identity_key = "processing_id" if processing else "batch_id"
            if prior is not None:
                _require(receipt[identity_key] != prior[identity_key], "monitor_state_receipt_reused")
            _ordered_vectors(current.data[field], cursors)
            data = _json_copy(current.data)
            data[field] = cursors
            data[receipt_field] = receipt
            if not processing:
                data["source_inventory_revision"] = receipt["source_inventory_revision"]
            if processing:
                _ordered_vectors(cursors, current.data["captured_cursors"])
                data["last_checked_ts"] = max(
                    _cursor_position(entry["cursor_token"])[0] for entry in cursors.values()
                )
            _validate_v3(data, revision=current.revision + 1)
            return self._write_locked(
                data, revision=current.revision + 1, existed_before=current.existed,
                schema=MONITOR_V3_STATE_SCHEMA,
            )
        finally:
            self._unlock(fd)

    def commit_capture(self, expected_revision: int, captured_cursors: dict, capture_receipt: dict) -> MonitorStateSnapshot:
        """CAS only durable capture progress using an exact receipt binding."""
        return self._commit_progress(expected_revision, captured_cursors, capture_receipt, processing=False)

    def commit_applied(self, expected_revision: int, source_cursors: dict, processing_receipt: dict) -> MonitorStateSnapshot:
        """CAS the durable semantic prefix, independently of projection delivery."""
        return self._commit_progress(expected_revision, source_cursors, processing_receipt, processing=True)


    def restore_checkpoint(self, expected_revision: int, backup_bytes: bytes, rollback_receipt: dict) -> MonitorStateSnapshot:
        """Restore validated checkpoint data through an explicit rollback CAS.

        This is the sole downgrade exception. The external durable journal
        owns rollback/retained-raw evidence, while this writer binds that proof
        to the current generation and exact backup. No source or DB is opened.
        """
        fd = self._lock(exclusive=True)
        try:
            current = self._read_locked()
            self._check_revision(current, expected_revision)
            _require(current.schema == MONITOR_V3_STATE_SCHEMA, "monitor_state_v3_required")
            _require(type(backup_bytes) is bytes, "monitor_state_backup_invalid")
            try:
                payload = json.loads(
                    backup_bytes.decode("utf-8"), object_pairs_hook=_unique_object,
                    parse_constant=_invalid_constant,
                )
                backup = self._parse_payload(payload)
                if "pending_source_batch" in backup.data:
                    from .monitor_source import MonitorSourceError, pending_monitor_source_batch
                    try:
                        pending_monitor_source_batch(backup.data)
                    except MonitorSourceError as exc:
                        raise MonitorStateError("monitor_state_backup_invalid") from exc
            except (MonitorStateError, ValueError, UnicodeError, RecursionError) as exc:
                raise MonitorStateError("monitor_state_backup_invalid") from exc
            receipt = _json_copy(rollback_receipt)
            _require(type(receipt) is dict and set(receipt) == _ROLLBACK_RECEIPT_FIELDS)
            _require(receipt["schema"] == MONITOR_ROLLBACK_RECEIPT_SCHEMA)
            _validate_binding(receipt)
            _same_binding(receipt, current.data)
            for key in (
                "receipt_id", "rollback_id", "run_id", "journal_id", "retained_raw_store_id",
                "retained_raw_receipt_id", "replay_receipt_id", "replay_destination_id",
            ):
                _identity(receipt[key])
            _integer(receipt["state_revision"], minimum=1)
            for key in ("current_state_fingerprint", "backup_bytes_digest", "backup_state_fingerprint"):
                _digest(receipt[key])
            _receipt_ids(receipt["retained_capture_receipt_ids"])
            _validate_vector(receipt["retained_captured_cursors"])
            _validate_vector(receipt["replay_from_cursors"], allow_empty=True)
            _validate_vector(receipt["replay_through_cursors"])
            _require(
                backup.revision < current.revision
                and receipt["state_revision"] == current.revision
                and receipt["current_state_fingerprint"] == monitor_state_fingerprint(current)
                and receipt["backup_bytes_digest"] == hashlib.sha256(backup_bytes).hexdigest()
                and receipt["backup_state_fingerprint"] == monitor_state_fingerprint(backup)
                and receipt["retained_captured_cursors"] == current.data["captured_cursors"]
                and receipt["replay_through_cursors"] == current.data["captured_cursors"]
                and receipt["replay_from_cursors"] == backup.data.get("source_cursors", {}),
                "monitor_state_rollback_binding_mismatch",
            )
            last_capture = current.data["capture_receipt"]
            required_captures = (
                [last_capture["receipt_id"]] if last_capture is not None
                else current.data["migration_receipt"]["capture_receipt_ids"]
            )
            _require(
                set(required_captures) <= set(receipt["retained_capture_receipt_ids"]),
                "monitor_state_rollback_binding_mismatch",
            )
            _require(
                receipt["receipt_id"] not in {
                    current.data["migration_receipt"]["receipt_id"],
                    *([last_capture["receipt_id"]] if last_capture else []),
                    *([current.data["processing_receipt"]["receipt_id"]]
                      if current.data["processing_receipt"] else []),
                },
                "monitor_state_receipt_reused",
            )
            if backup.schema == MONITOR_V3_STATE_SCHEMA:
                _require(
                    backup.data["source_namespace"] == current.data["source_namespace"]
                    and backup.data["chat_id"] == current.data["chat_id"],
                    "monitor_state_rollback_binding_mismatch",
                )
                _validate_v3(backup.data, revision=current.revision + 1)
            return self._write_locked(
                backup.data, revision=current.revision + 1, existed_before=current.existed,
                schema=backup.schema or MONITOR_STATE_SCHEMA,
            )
        finally:
            self._unlock(fd)
