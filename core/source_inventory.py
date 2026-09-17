"""Durable completeness inventory for WeChat message database shards."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import posixpath
import stat
import threading

from .platform import LockMode
from .state_storage import StateFileNotRegular, StateStorage
from .project_identity import DATA_DIR_NAME


SOURCE_INVENTORY_SCHEMA = "we-groupchat-obsidian.source-inventory.v1"
SOURCE_INVENTORY_FILE = os.path.join(
    os.path.expanduser(
        os.environ.get("WE_GROUPCHAT_OBSIDIAN_DATA_DIR", f"~/{DATA_DIR_NAME}")
    ),
    "source_inventory.json",
)
SOURCE_STATES = frozenset({
    "present",
    "missing_file",
    "key_missing",
    "cache_only",
    "unreadable",
    "generation_changed",
    "explicitly_retired",
})
COMPLETE_STATES = frozenset({"present", "generation_changed"})
STATE_ERROR_CODES = {
    "missing_file": "source_missing_file",
    "key_missing": "source_key_missing",
    "cache_only": "source_cache_only",
    "unreadable": "source_unreadable",
}


class SourceInventoryError(RuntimeError):
    """A content-free source-inventory storage or schema failure."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


def normalize_relative_source_path(value: str) -> str:
    """Return one canonical, non-absolute source-relative path."""
    raw = str(value or "").replace("\\", "/").strip()
    normalized = posixpath.normpath(raw).replace("\\", "/")
    if (
        not normalized
        or normalized in {".", ".."}
        or normalized.startswith("../")
        or normalized.startswith("/")
    ):
        raise SourceInventoryError("source_relative_path_invalid")
    return normalized.casefold()


def logical_shard_id(source_namespace: str, relative_path: str) -> str:
    normalized = normalize_relative_source_path(relative_path)
    return hashlib.sha256(
        f"wechat-logical-shard-v1\0{source_namespace}\0{normalized}".encode("utf-8")
    ).hexdigest()[:32]


def source_namespaces_for_root(path: str | os.PathLike[str]) -> tuple[str, str]:
    """Return the cache and durable source namespaces for one configured root.

    This is intentionally observation-only.  It does not create a cache,
    inventory ledger, lock file, or source directory.
    """
    source_root = os.path.realpath(os.path.expanduser(os.fspath(path or "")))
    try:
        source_stat = os.stat(source_root)
        cache_identity = (
            f"{source_root}\0{source_stat.st_dev}\0{source_stat.st_ino}"
        )
        namespace_identity = (
            f"{source_root}\0{source_stat.st_dev}:{source_stat.st_ino}"
        )
    except OSError:
        cache_identity = f"{source_root}\0missing"
        cache_namespace = hashlib.sha256(
            cache_identity.encode("utf-8")
        ).hexdigest()
        namespace_identity = cache_namespace
    else:
        cache_namespace = hashlib.sha256(
            cache_identity.encode("utf-8")
        ).hexdigest()
    source_namespace = hashlib.sha256(
        f"wechat-source-namespace-v1\0{namespace_identity}".encode("utf-8")
    ).hexdigest()[:32]
    return cache_namespace, source_namespace


@dataclass(frozen=True)
class SourceInventorySnapshot:
    source_namespace: str
    inventory_revision: int
    inventory_digest: str
    complete: bool
    counts: dict
    error_codes: tuple[str, ...]
    present_generation_ids: tuple[str, ...]
    shards: tuple[dict, ...]

    def as_dict(self, *, sensitive: bool = False) -> dict:
        shard_rows = []
        for shard in self.shards:
            row = {
                "logical_shard_id": shard["logical_shard_id"],
                "generation_id": shard.get("generation_id", ""),
                "state": shard["state"],
            }
            if sensitive:
                row["relative_path"] = shard["relative_path"]
            shard_rows.append(row)
        return {
            "schema": SOURCE_INVENTORY_SCHEMA,
            "source_namespace": self.source_namespace,
            "inventory_revision": self.inventory_revision,
            "inventory_digest": self.inventory_digest,
            "complete": self.complete,
            "counts": dict(self.counts),
            "error_codes": list(self.error_codes),
            "present_generation_ids": list(self.present_generation_ids),
            "shards": shard_rows,
        }


def _snapshot(
    source_namespace: str,
    revision: int,
    shard_records: dict[str, dict],
    extra_error_codes=(),
) -> SourceInventorySnapshot:
    shards = []
    counts = {state: 0 for state in sorted(SOURCE_STATES)}
    errors = {str(code) for code in extra_error_codes if str(code)}
    for shard_id in sorted(shard_records):
        record = dict(shard_records[shard_id])
        state = str(record.get("state") or "unreadable")
        if state not in SOURCE_STATES:
            state = "unreadable"
        counts[state] += 1
        if state in STATE_ERROR_CODES:
            errors.add(STATE_ERROR_CODES[state])
        shards.append({
            "logical_shard_id": shard_id,
            "relative_path": normalize_relative_source_path(record["relative_path"]),
            "generation_id": str(record.get("generation_id") or ""),
            "state": state,
        })

    active = [row for row in shards if row["state"] != "explicitly_retired"]
    if not active:
        errors.add("source_shards_unavailable")
    complete = bool(active) and not errors and all(
        row["state"] in COMPLETE_STATES for row in active
    )
    public_basis = {
        "schema": SOURCE_INVENTORY_SCHEMA,
        "source_namespace": str(source_namespace),
        "complete": complete,
        "error_codes": sorted(errors),
        "shards": [
            {
                "logical_shard_id": row["logical_shard_id"],
                "generation_id": row["generation_id"],
                "state": row["state"],
            }
            for row in shards
        ],
    }
    digest = hashlib.sha256(json.dumps(
        public_basis,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return SourceInventorySnapshot(
        source_namespace=str(source_namespace),
        inventory_revision=int(revision),
        inventory_digest=digest,
        complete=complete,
        counts=counts,
        error_codes=tuple(sorted(errors)),
        present_generation_ids=tuple(sorted(
            row["generation_id"]
            for row in active
            if row["state"] in COMPLETE_STATES and row["generation_id"]
        )),
        shards=tuple(shards),
    )


class SourceInventoryStore:
    """Own the expected-shard ledger for all local source namespaces.

    Passing ``path=None`` creates an in-memory store for isolated adapters and
    tests. The default constructor owns the private runtime ledger.
    """

    _DEFAULT = object()

    def __init__(self, path=_DEFAULT, *, file_lock=None, platform_services=None):
        if path is self._DEFAULT:
            path = SOURCE_INVENTORY_FILE
        self.path = (
            os.path.abspath(os.path.expanduser(os.fspath(path)))
            if path is not None
            else ""
        )
        self._file_lock = file_lock
        self._storage = StateStorage(self.path, platform_services=platform_services)
        self._memory_lock = threading.RLock()
        self._memory_payload = self._empty_payload()

    @staticmethod
    def _empty_payload() -> dict:
        return {
            "schema": SOURCE_INVENTORY_SCHEMA,
            "revision": 0,
            "sources": {},
        }

    @staticmethod
    def _validate_payload(value) -> dict:
        if not isinstance(value, dict):
            raise SourceInventoryError("source_inventory_corrupt")
        if value.get("schema") != SOURCE_INVENTORY_SCHEMA:
            raise SourceInventoryError("source_inventory_corrupt")
        revision = value.get("revision")
        sources = value.get("sources")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or not isinstance(sources, dict)
        ):
            raise SourceInventoryError("source_inventory_corrupt")
        normalized_sources = {}
        for namespace, source in sources.items():
            if not isinstance(namespace, str) or not isinstance(source, dict):
                raise SourceInventoryError("source_inventory_corrupt")
            records = source.get("shards")
            if not isinstance(records, dict):
                raise SourceInventoryError("source_inventory_corrupt")
            normalized_records = {}
            for shard_id, record in records.items():
                if not isinstance(shard_id, str) or not isinstance(record, dict):
                    raise SourceInventoryError("source_inventory_corrupt")
                relative_path = normalize_relative_source_path(record.get("relative_path"))
                if logical_shard_id(namespace, relative_path) != shard_id:
                    raise SourceInventoryError("source_inventory_corrupt")
                state = str(record.get("state") or "")
                if state not in SOURCE_STATES:
                    raise SourceInventoryError("source_inventory_corrupt")
                normalized_records[shard_id] = {
                    "relative_path": relative_path,
                    "generation_id": str(record.get("generation_id") or ""),
                    "state": state,
                    "retired": bool(record.get("retired")),
                }
            normalized_sources[namespace] = {"shards": normalized_records}
        return {
            "schema": SOURCE_INVENTORY_SCHEMA,
            "revision": revision,
            "sources": normalized_sources,
        }

    def _read_file(self) -> dict:
        fd = -1
        try:
            file_stat = os.lstat(self.path)
            if not stat.S_ISREG(file_stat.st_mode):
                raise SourceInventoryError("source_inventory_corrupt")
            fd = os.open(
                self._storage.operational_path(),
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            opened_stat = os.fstat(fd)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise SourceInventoryError("source_inventory_corrupt")
            with os.fdopen(fd, encoding="utf-8") as handle:
                fd = -1
                return self._validate_payload(json.load(handle))
        except SourceInventoryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceInventoryError("source_inventory_corrupt") from exc
        finally:
            if fd >= 0:
                os.close(fd)

    def _lock_service(self):
        if self._file_lock is None:
            self._file_lock = self._storage.services.require("locks")
        return self._file_lock

    def _lock(self):
        try:
            path = self._storage.prepare()
            return self._lock_service().acquire(
                path + ".lock",
                mode=LockMode.EXCLUSIVE,
                blocking=True,
            )
        except StateFileNotRegular as exc:
            raise SourceInventoryError("source_inventory_corrupt") from exc
        except OSError as exc:
            raise SourceInventoryError("source_inventory_lock_unavailable") from exc

    @staticmethod
    def _unlock(lock_handle) -> None:
        lock_handle.close()

    def _write_file(self, payload: dict) -> None:
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            self._storage.write_bytes(encoded)
        except OSError as exc:
            raise SourceInventoryError("source_inventory_write_failed") from exc

    @staticmethod
    def _source_records(payload: dict, source_namespace: str) -> dict[str, dict]:
        source = (payload.get("sources") or {}).get(str(source_namespace)) or {}
        return {
            shard_id: dict(record)
            for shard_id, record in (source.get("shards") or {}).items()
        }

    def inspect(self, source_namespace: str) -> SourceInventorySnapshot:
        """Read existing ledger evidence without creating or migrating it."""
        namespace = str(source_namespace or "")
        if not namespace:
            raise SourceInventoryError("source_namespace_invalid")
        if not self.path:
            with self._memory_lock:
                payload = self._validate_payload(self._memory_payload)
        elif not os.path.lexists(self.path):
            return _snapshot(namespace, 0, {}, ("source_inventory_uninitialized",))
        else:
            payload = self._read_file()
        records = self._source_records(payload, namespace)
        errors = () if records else ("source_inventory_uninitialized",)
        return _snapshot(namespace, payload["revision"], records, errors)

    def reconcile(
        self,
        source_namespace: str,
        observations,
        *,
        error_codes=(),
    ) -> SourceInventorySnapshot:
        """Merge one actual source scan with every previously expected shard."""
        namespace = str(source_namespace or "")
        if not namespace:
            raise SourceInventoryError("source_namespace_invalid")
        normalized_observations = {}
        for observation in observations:
            relative_path = normalize_relative_source_path(observation.get("relative_path"))
            shard_id = logical_shard_id(namespace, relative_path)
            state = str(observation.get("state") or "unreadable")
            if state not in SOURCE_STATES or state == "explicitly_retired":
                state = "unreadable"
            normalized_observations[shard_id] = {
                "relative_path": relative_path,
                "generation_id": str(observation.get("generation_id") or ""),
                "state": state,
                "retired": False,
            }

        def merge(payload):
            previous = self._source_records(payload, namespace)
            merged = {}
            for shard_id, prior in previous.items():
                if prior.get("retired"):
                    merged[shard_id] = {
                        **prior,
                        "state": "explicitly_retired",
                        "retired": True,
                    }
                else:
                    merged[shard_id] = {
                        **prior,
                        "state": "missing_file",
                        "retired": False,
                    }

            for shard_id, observed in normalized_observations.items():
                prior = previous.get(shard_id) or {}
                prior_generation = str(prior.get("generation_id") or "")
                observed_generation = str(observed.get("generation_id") or "")
                state = observed["state"]
                generation_id = prior_generation
                if state == "present":
                    generation_id = observed_generation
                    if prior_generation and observed_generation != prior_generation:
                        state = "generation_changed"
                    elif prior.get("state") == "generation_changed":
                        state = "generation_changed"
                elif not generation_id:
                    generation_id = observed_generation
                merged[shard_id] = {
                    "relative_path": observed["relative_path"],
                    "generation_id": generation_id,
                    "state": state,
                    "retired": False,
                }

            next_source = {"shards": merged}
            current_source = (payload.get("sources") or {}).get(namespace)
            if current_source != next_source:
                payload = {
                    "schema": SOURCE_INVENTORY_SCHEMA,
                    "revision": int(payload["revision"]) + 1,
                    "sources": dict(payload.get("sources") or {}),
                }
                payload["sources"][namespace] = next_source
                return payload, True
            return payload, False

        if not self.path:
            with self._memory_lock:
                payload = self._validate_payload(self._memory_payload)
                payload, changed = merge(payload)
                if changed:
                    self._memory_payload = payload
        else:
            lock_fd = self._lock()
            try:
                payload = (
                    self._read_file()
                    if os.path.lexists(self.path)
                    else self._empty_payload()
                )
                payload, changed = merge(payload)
                if changed:
                    self._write_file(payload)
            finally:
                self._unlock(lock_fd)

        records = self._source_records(payload, namespace)
        return _snapshot(namespace, payload["revision"], records, error_codes)
