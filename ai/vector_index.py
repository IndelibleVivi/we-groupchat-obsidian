"""Rebuildable cosine indexes; canonical objects and receipts remain in SQLite."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import math
import re
import struct
import threading
from typing import Protocol, Sequence
import uuid

from .semantic_provider import EmbeddingProfile, ProviderError, finite_number, json_bytes, validate_vector


def _opaque_id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ProviderError("vector_id_not_opaque") from None
    return value


def _generation(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ProviderError("vector_invalid_generation")
    return value


def _float32(vector):
    try:
        result = tuple(struct.unpack("!f", struct.pack("!f", value))[0] for value in vector)
    except (OverflowError, struct.error):
        raise ProviderError("vector_float32_overflow") from None
    if not all(math.isfinite(value) for value in result) or not any(result):
        raise ProviderError("embedding_invalid_vector")
    return result


@dataclass(frozen=True)
class VectorRecord:
    id: str
    values: tuple[float, ...]


@dataclass(frozen=True)
class VectorMatch:
    id: str
    score: float


@dataclass(frozen=True)
class IndexReceipt:
    backend: str
    profile_id: str
    generation_id: str
    mutation_id: str
    records: tuple[VectorRecord, ...]
    indexed: bool = False
    index_identity: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ProviderError("vector_invalid_receipt")
        try:
            if not isinstance(value["records"], (list, tuple)):
                raise ValueError
            records = tuple(VectorRecord(item["id"], tuple(item["values"])) for item in value["records"])
            if any(set(item) != {"id", "values"} for item in value["records"]):
                raise ValueError
            receipt = cls(**{**value, "records": records})
        except (TypeError, KeyError, ValueError):
            raise ProviderError("vector_invalid_receipt") from None
        _validate_receipt(receipt)
        return receipt


def _validate_receipt(receipt):
    if (not isinstance(receipt, IndexReceipt) or receipt.backend not in {"local", "cloudflare-vectorize"}
            or type(receipt.indexed) is not bool
            or not isinstance(receipt.profile_id, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt.profile_id)
            or not isinstance(receipt.index_identity, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt.index_identity)):
        raise ProviderError("vector_invalid_receipt")
    _generation(receipt.generation_id)
    _opaque_id(receipt.mutation_id)
    if (not isinstance(receipt.records, (tuple, list)) or not receipt.records
            or any(not isinstance(row, VectorRecord) for row in receipt.records)
            or len({row.id for row in receipt.records}) != len(receipt.records)):
        raise ProviderError("vector_invalid_receipt")
    for row in receipt.records:
        if not isinstance(row, VectorRecord):
            raise ProviderError("vector_invalid_receipt")
        _opaque_id(row.id)
        if not isinstance(row.values, (tuple, list)) or not row.values:
            raise ProviderError("vector_invalid_receipt")
        if not all(math.isfinite(finite_number(value)) for value in row.values) or not any(row.values):
            raise ProviderError("vector_invalid_receipt")


def _records(records, profile, generation_id, *, float32=False):
    _generation(generation_id)
    if not isinstance(records, (tuple, list)) or not records:
        raise ProviderError("vector_invalid_records")
    output = []
    ids = set()
    for row in records:
        if not isinstance(row, VectorRecord):
            raise ProviderError("vector_invalid_records")
        identifier = _opaque_id(row.id)
        if identifier in ids:
            raise ProviderError("vector_duplicate_id")
        ids.add(identifier)
        vector = validate_vector(row.values, profile)
        output.append(VectorRecord(identifier, _float32(vector) if float32 else vector))
    return tuple(output)


def _top_k(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise ProviderError("vector_invalid_top_k")


class VectorIndex(Protocol):
    def upsert(self, records: Sequence[VectorRecord], *, profile: EmbeddingProfile,
               generation_id: str, request_id: str) -> IndexReceipt: ...

    def confirm(self, receipt: IndexReceipt, *, profile: EmbeddingProfile,
                request_id: str) -> IndexReceipt: ...

    def query(self, vector: Sequence[float], *, profile: EmbeddingProfile,
              generation_id: str, top_k: int = 10, request_id: str) -> tuple[VectorMatch, ...]: ...


class LocalCosineIndex:
    """Standard-library exact cosine scan, hydrated from canonical SQLite."""

    def __init__(self):
        self._vectors = {}
        self._lock = threading.RLock()
        self.index_identity = hashlib.sha256(b"wgo-local-cosine-v1").hexdigest()

    def upsert(self, records, *, profile, generation_id, request_id):
        records = _records(records, profile, generation_id)
        key = (profile.profile_id, generation_id)
        with self._lock:
            existing = self._vectors.get(key, {})
            if any(row.id in existing and existing[row.id] != row.values for row in records):
                raise ProviderError("vector_immutable_id_conflict")
            self._vectors.setdefault(key, {}).update({row.id: row.values for row in records})
        return IndexReceipt("local", profile.profile_id, generation_id, str(uuid.uuid4()),
                            records, True, self.index_identity)

    def confirm(self, receipt, *, profile, request_id):
        _validate_receipt(receipt)
        if (receipt.backend != "local" or receipt.index_identity != self.index_identity
                or receipt.profile_id != profile.profile_id):
            raise ProviderError("vector_receipt_binding_mismatch")
        records = _records(receipt.records, profile, receipt.generation_id)
        with self._lock:
            current = self._vectors.get((profile.profile_id, receipt.generation_id), {})
            complete = all(current.get(row.id) == row.values for row in records)
        return replace(receipt, indexed=complete)

    def query(self, vector, *, profile, generation_id, top_k=10, request_id):
        _top_k(top_k)
        _generation(generation_id)
        vector = validate_vector(vector, profile)
        norm = math.hypot(*vector)
        with self._lock:
            rows = tuple(self._vectors.get((profile.profile_id, generation_id), {}).items())
        scores = []
        for identifier, values in rows:
            other_norm = math.hypot(*values)
            score = math.fsum((left / norm) * (right / other_norm) for left, right in zip(vector, values))
            scores.append(VectorMatch(identifier, max(-1., min(1., score))))
        return tuple(sorted(scores, key=lambda row: (-row.score, row.id))[:top_k])


class CloudflareVectorizeIndex:
    """Opaque Vectorize v2 projection, independently confirmed after upsert."""

    def __init__(self, client, index_name, *, pending_receipt_check=None):
        if not isinstance(index_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", index_name):
            raise ProviderError("vector_invalid_index_name")
        self.client = client
        self._path = "/vectorize/v2/indexes/" + index_name
        self.index_identity = hashlib.sha256((client.vectorize_account_key + ":" + index_name).encode()).hexdigest()
        self._confirmed = {}
        self._lock = threading.RLock()
        self._pending_mutation = None
        self._write_started = False
        self.pending_receipt_check = pending_receipt_check

    @staticmethod
    def _bindings(profile_id, generation_id):
        generation = hashlib.sha256(("wgo-generation:" + _generation(generation_id)).encode()).hexdigest()
        namespace = hashlib.sha256((profile_id + ":" + generation).encode()).hexdigest()
        return namespace, {"profile_id": profile_id, "generation_id": generation}

    @staticmethod
    def _remote_id(profile_id, generation_id, identifier):
        return hashlib.sha256((profile_id + ":" + generation_id + ":" + _opaque_id(identifier)).encode()).hexdigest()

    @staticmethod
    def _request_id(request_id, operation):
        if not isinstance(request_id, str) or not request_id:
            raise ProviderError("vector_invalid_request_id")
        return "vec:" + hashlib.sha256((request_id + ":" + operation).encode()).hexdigest()

    def _request(self, suffix, *, request_id, generation_id, units=1, **kwargs):
        return self.client.request(self._path + suffix, request_id=request_id,
                                   route="vectorize:v2", upper_bound_units=units,
                                   generation_id=generation_id, service="vectorize", **kwargs)

    def _verify_index(self, profile, generation_id, request_id):
        result = self._request("", method="GET", generation_id=generation_id,
                               request_id=self._request_id(request_id, "config"))
        config = result.get("config") if isinstance(result, dict) else None
        if not isinstance(config, dict) or type(config.get("dimensions")) is not int:
            raise ProviderError("vector_invalid_index_config")
        if config["dimensions"] != profile.dimensions or config.get("metric") != profile.metric:
            raise ProviderError("vector_index_profile_mismatch")

    def upsert(self, records, *, profile, generation_id, request_id):
        """Caller holds its canonical index lock through saving the receipt.

        The callback checks durable receipts in that lock. The in-process guard
        is only an additional check and cannot replace cross-process ownership.
        """
        with self._lock:
            if self.pending_receipt_check is None:
                raise ProviderError("vector_pending_guard_required")
            try:
                pending = self.pending_receipt_check(self.index_identity)
            except Exception:
                raise ProviderError("vector_pending_guard_unavailable") from None
            if type(pending) is not bool:
                raise ProviderError("vector_pending_guard_unavailable")
            if pending or self._pending_mutation is not None:
                raise ProviderError("vector_mutation_pending")
            self._pending_mutation = "in_flight"
            self._write_started = False
            try:
                receipt = self._upsert(records, profile=profile, generation_id=generation_id, request_id=request_id)
            except BaseException as exc:
                # A crash/unknown result after dispatch must be reconciled by
                # the canonical job owner, never silently resubmitted here.
                unknown = self._write_started and not (isinstance(exc, ProviderError) and not exc.outcome_unknown)
                self._pending_mutation = "unknown" if unknown else None
                raise
            self._pending_mutation = receipt.mutation_id
            return receipt

    def _upsert(self, records, *, profile, generation_id, request_id):
        records = _records(records, profile, generation_id, float32=True)
        # One bounded receipt must fit get_by_ids verification without an
        # unbounded follow-up. Callers partition larger canonical queues.
        if len(records) > 1000 or profile.dimensions > 1536:
            raise ProviderError("vector_batch_limit")
        namespace, metadata = self._bindings(profile.profile_id, generation_id)
        rows = [{"id": self._remote_id(profile.profile_id, generation_id, row.id),
                 "values": row.values, "namespace": namespace, "metadata": metadata} for row in records]
        with self._lock:
            for row, payload in zip(records, rows):
                old = self._confirmed.get(payload["id"])
                if old is not None and old.values != row.values:
                    raise ProviderError("vector_immutable_id_conflict")
        self._verify_index(profile, generation_id, request_id)
        # Match Cloudflare's maintained Wrangler client: multipart `vectors`,
        # with NDJSON as the file content (not a guessed JSON array endpoint).
        boundary = "wgo" + hashlib.sha256(json_bytes(rows)).hexdigest()
        ndjson = b"\n".join(json_bytes(row) for row in rows) + b"\n"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"vectors\"; filename=\"vectors.ndjson\"\r\n"
                "Content-Type: application/x-ndjson\r\n\r\n").encode() + ndjson + f"\r\n--{boundary}--\r\n".encode()
        self._write_started = True
        result = self._request("/upsert?unparsable-behavior=error", body=body,
                               content_type="multipart/form-data; boundary=" + boundary,
                               request_id=self._request_id(request_id, "upsert"), generation_id=generation_id,
                               units=2 * profile.dimensions * len(records))
        mutation_id = result.get("mutationId") if isinstance(result, dict) else None
        try:
            _opaque_id(mutation_id)
        except ProviderError:
            raise ProviderError("vector_invalid_mutation", outcome_unknown=True) from None
        return IndexReceipt("cloudflare-vectorize", profile.profile_id, generation_id,
                            mutation_id, records, False, self.index_identity)

    def confirm(self, receipt, *, profile, request_id):
        _validate_receipt(receipt)
        if (receipt.backend != "cloudflare-vectorize" or receipt.index_identity != self.index_identity
                or receipt.profile_id != profile.profile_id):
            raise ProviderError("vector_receipt_binding_mismatch")
        records = _records(receipt.records, profile, receipt.generation_id, float32=True)
        self._verify_index(profile, receipt.generation_id, request_id)
        info = self._request("/info", method="GET", generation_id=receipt.generation_id,
                             request_id=self._request_id(request_id, "info"))
        if not isinstance(info, dict) or type(info.get("dimensions")) is not int or info["dimensions"] != profile.dimensions:
            raise ProviderError("vector_invalid_index_info")
        # UUIDs carry no ordering contract. Another marker (including a newer
        # one) is not proof that this receipt reached the query index.
        if info.get("processedUpToMutation") != receipt.mutation_id:
            return replace(receipt, indexed=False)
        expected = {self._remote_id(receipt.profile_id, receipt.generation_id, row.id): row for row in records}
        result = self._request("/get_by_ids", payload={"ids": list(expected)},
                               generation_id=receipt.generation_id, units=max(1, len(records) * profile.dimensions),
                               request_id=self._request_id(request_id, "readback"))
        if not isinstance(result, list):
            raise ProviderError("vector_invalid_readback")
        namespace, metadata = self._bindings(receipt.profile_id, receipt.generation_id)
        observed = set()
        for row in result:
            if not isinstance(row, dict) or row.get("id") not in expected or row["id"] in observed:
                raise ProviderError("vector_readback_ids_mismatch")
            if row.get("namespace") != namespace or row.get("metadata") != metadata:
                raise ProviderError("vector_readback_binding_mismatch")
            values = _float32(validate_vector(row.get("values"), profile))
            if values != expected[row["id"]].values:
                raise ProviderError("vector_readback_values_mismatch")
            observed.add(row["id"])
        if observed != set(expected):
            return replace(receipt, indexed=False)
        confirmed = replace(receipt, records=records, indexed=True)
        self.restore_receipt(confirmed)
        with self._lock:
            if self._pending_mutation == receipt.mutation_id:
                self._pending_mutation = None
        return confirmed

    def restore_receipt(self, receipt):
        """Rehydrate a previously confirmed receipt from trusted canonical SQLite."""
        _validate_receipt(receipt)
        if receipt.backend != "cloudflare-vectorize" or receipt.index_identity != self.index_identity:
            raise ProviderError("vector_receipt_binding_mismatch")
        if not receipt.indexed:
            with self._lock:
                if self._pending_mutation not in (None, receipt.mutation_id):
                    raise ProviderError("vector_mutation_pending")
                self._pending_mutation = receipt.mutation_id
            return
        pending = {self._remote_id(receipt.profile_id, receipt.generation_id, row.id): row for row in receipt.records}
        with self._lock:
            if any(key in self._confirmed and self._confirmed[key] != row for key, row in pending.items()):
                raise ProviderError("vector_immutable_id_conflict")
            self._confirmed.update(pending)

    def query(self, vector, *, profile, generation_id, top_k=10, request_id):
        _top_k(top_k)
        _generation(generation_id)
        vector = _float32(validate_vector(vector, profile))
        self._verify_index(profile, generation_id, request_id)
        namespace, metadata = self._bindings(profile.profile_id, generation_id)
        result = self._request("/query", payload={"vector": vector, "namespace": namespace,
                               "topK": top_k, "returnValues": False, "returnMetadata": "all"},
                               generation_id=generation_id, units=profile.dimensions,
                               request_id=self._request_id(request_id, "query"))
        if not isinstance(result, dict) or not isinstance(result.get("matches"), list):
            raise ProviderError("vector_invalid_query_response")
        rows = result["matches"]
        if type(result.get("count")) is not int or result["count"] != len(rows) or len(rows) > top_k:
            raise ProviderError("vector_query_count_mismatch")
        output, seen = [], set()
        with self._lock:
            confirmed = dict(self._confirmed)
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in seen:
                raise ProviderError("vector_query_ids_mismatch")
            if row.get("namespace") != namespace or row.get("metadata") != metadata:
                raise ProviderError("vector_query_binding_mismatch")
            if row["id"] not in confirmed:
                raise ProviderError("vector_query_unconfirmed")
            record = confirmed[row["id"]]
            if self._remote_id(profile.profile_id, generation_id, record.id) != row["id"]:
                raise ProviderError("vector_query_binding_mismatch")
            score = finite_number(row.get("score"), low=-1.000001, high=1.000001, code="vector_invalid_score")
            seen.add(row["id"])
            output.append(VectorMatch(record.id, max(-1., min(1., score))))
        return tuple(sorted(output, key=lambda row: (-row.score, row.id)))
