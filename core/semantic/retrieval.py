"""Lexical/dense retrieval over one canonical generation and explicit coverage."""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
from itertools import islice
import json
import math
import re
import sqlite3
import unicodedata
import uuid
import threading
from pathlib import Path
from typing import Mapping, Sequence

from ai.semantic_provider import DecisionQuestion, DecisionRequest, ProviderError, parse_decision_answers, validate_vector
from ai.vector_index import IndexReceipt, LocalCosineIndex, VectorRecord
from core.platform import LockMode, create_platform_services
from core.semantic.engine import SemanticValidationError, _json, validate_accepted_artifact
from core.state_storage import StateStorage
from core.url_safety import redact_urls_in_text
from .scheduling import ProviderScheduler, ScheduledEmbedding, maintain_claim, not_dispatched


DOCUMENT_KINDS = frozenset({"source", "observation", "item", "body", "relation", "change", "claim"})


@dataclass(frozen=True)
class SearchDocument:
    document_id: str
    kind: str
    object_id: str
    revision: int
    text: str
    item_id: str | None = None
    source_ids: tuple[str, ...] = ()
    case_id: str | None = None
    reply_to: str | None = None
    related_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if self.kind not in DOCUMENT_KINDS or type(self.revision) is not int or self.revision < 0:
            raise SemanticValidationError("search_invalid_document")
        if any(not isinstance(value, str) or not value for value in (self.document_id, self.object_id)) or not isinstance(self.text, str):
            raise SemanticValidationError("search_invalid_document")


@dataclass
class IndexPreparation:
    """One operation's canonical snapshot; never a persistent freshness cache."""
    owner: object
    revision: int
    documents: tuple
    rows: dict
    current: dict
    pending: list
    lock: object = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class SearchHit:
    kind: str
    object_id: str
    document_id: str
    item_id: str | None
    revision: int
    text: str
    score: float
    channels: tuple[str, ...]
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class SearchCoverage:
    canonical_revision: int
    documents: int
    embedding_pending: int
    index_pending: int
    unread_sources: int
    unclassified_sources: int
    source_complete: bool
    unknown: tuple[str, ...] = ()

    @property
    def complete(self):
        return (self.source_complete and not self.embedding_pending and not self.index_pending
                and not self.unread_sources and not self.unknown)


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    coverage: SearchCoverage
    candidates_considered: int
    structural_ids: tuple[str, ...]

    @property
    def absence(self):
        # A ranking window is not a full condition evaluation even when every
        # source and vector is current.
        return "unknown" if not self.hits else "evidence_found"


def lexical_tokens(text):
    """Portable Chinese detail tokens plus full Latin/number identifiers."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = []
    for chunk in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_@./:+-]+", normalized):
        if re.fullmatch(r"[\u3400-\u9fff]+", chunk):
            tokens.extend(chunk)
            tokens.extend(chunk[i:i + n] for n in (2, 3) for i in range(len(chunk) - n + 1))
            tokens.append(chunk)
        else:
            tokens.append(chunk)
    return tuple(dict.fromkeys(tokens))


def lexical_rank(query, documents):
    tokens = lexical_tokens(query)
    if not tokens:
        return []
    normalized = unicodedata.normalize("NFKC", query).casefold().strip()
    frequencies = defaultdict(int)
    tokenized = {}
    for document in documents:
        body = unicodedata.normalize("NFKC", document.text).casefold()
        parts = set(lexical_tokens(body))
        tokenized[document.document_id] = (body, parts)
        for token in set(tokens) & parts:
            frequencies[token] += 1
    scores = []
    for document in documents:
        body, parts = tokenized[document.document_id]
        matched = set(tokens) & parts
        score = sum((1 + math.log((len(documents) + 1) / (frequencies[token] + 1))) * min(len(token), 4)
                    for token in matched)
        if normalized and normalized in body:
            score += 8 + len(normalized)
        if score:
            scores.append((document.document_id, score))
    return sorted(scores, key=lambda pair: (-pair[1], pair[0]))


def reciprocal_rank_fusion(*rankings, constant=60):
    scores = defaultdict(float)
    for ranking in rankings:
        seen = set()
        for rank, entry in enumerate(ranking, 1):
            document_id = entry[0] if isinstance(entry, (tuple, list)) else entry
            if document_id in seen:
                continue
            seen.add(document_id)
            scores[document_id] += 1 / (constant + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def structural_candidates(documents: Sequence[SearchDocument], *, source_ids=(), case_ids=(), exact_ids=(),
                          correction_ids=(), dependencies: Mapping[str, Sequence[str]] | None = None):
    """Exact/reply/case and directed dependency traversal bypass top-k.

    ``dependencies`` maps a corrected object to objects that rely on it. It is
    directed: it does not turn all connected Items into an identity component.
    """
    wanted = set(exact_ids) | set(correction_ids)
    pending = list(correction_ids)
    dependencies = dependencies or {}
    while pending:
        current = pending.pop()
        for dependent in dependencies.get(current, ()):
            if dependent not in wanted:
                wanted.add(dependent)
                pending.append(dependent)
    source_ids, case_ids = set(source_ids), set(case_ids)
    # Incoming replies must retrieve their older parent as well as later replies
    # to an incoming anchor. This traversal is a candidate path, never identity.
    changed = True
    while changed:
        changed = False
        for document in documents:
            if document.reply_to and source_ids & set(document.source_ids) and document.reply_to not in source_ids:
                source_ids.add(document.reply_to)
                changed = True
    result = set()
    for document in documents:
        identities = {document.document_id, document.object_id, document.item_id} | set(document.related_ids)
        if (identities & wanted or source_ids & set(document.source_ids) or document.reply_to in source_ids
                or document.case_id is not None and document.case_id in case_ids):
            result.add(document.document_id)
    # Exact source/reply/case hits establish directed dependency entry points.
    # Traverse those edges even when the affected Item has no lexical match.
    pending = [identity for document in documents if document.document_id in result
               for identity in (document.object_id, document.item_id, *document.source_ids) if identity]
    traversed = set(pending)
    while pending:
        for dependent in dependencies.get(pending.pop(), ()):
            if dependent not in traversed:
                traversed.add(dependent)
                pending.append(dependent)
    result.update(document.document_id for document in documents
                  if {document.object_id, document.item_id} & traversed)
    return result


def _cosine(left, right):
    denominator = math.hypot(*left) * math.hypot(*right)
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0


_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_profiles(
 profile_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL, generation_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS embedding_records(
 vector_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, generation_id TEXT NOT NULL,
 document_id TEXT NOT NULL, document_revision INTEGER NOT NULL, fingerprint TEXT NOT NULL,
 artifact_json TEXT NOT NULL, job_id TEXT NOT NULL,
 UNIQUE(profile_id,generation_id,document_id,document_revision,fingerprint));
CREATE TABLE IF NOT EXISTS semantic_index_receipts(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, intent_id TEXT UNIQUE NOT NULL,
 profile_id TEXT NOT NULL, generation_id TEXT NOT NULL, index_identity TEXT NOT NULL,
 status TEXT NOT NULL, records_json TEXT NOT NULL, receipt_json TEXT,
 error_code TEXT);
CREATE TABLE IF NOT EXISTS semantic_lexical_documents(
 document_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS semantic_lexical_terms(
 token TEXT NOT NULL,document_id TEXT NOT NULL REFERENCES semantic_lexical_documents(document_id) ON DELETE CASCADE,
 PRIMARY KEY(token,document_id));
"""


def _fingerprint(document):
    # This hash makes immutable vector IDs bind exactly to the indexed revision.
    return hashlib.sha256(_json([document.document_id, document.revision, document.text]).encode()).hexdigest()


class HybridRetriever:
    """Canonical lexical recall, receipt-confirmed index and local canonical tail.

    Embeddings and receipts are derived tables in ``store.connect()``. Opaque
    UUIDs alone leave SQLite. No arbitrary metadata enters a vector service.
    ``index_pending`` is explicit work; ordinary ``search`` never upserts.
    """

    def __init__(self, store, embedding_provider=None, vector_index=None, *, profile=None,
                 decision_provider=None, jobs=None, worker_id=None, shared_lock_root=None, scheduler=None):
        from core.semantic.jobs import JobStore
        self.store, self.embedding_provider, self.index = store, embedding_provider, vector_index
        self.scheduler = scheduler or getattr(embedding_provider, "scheduler", None) or ProviderScheduler()
        if embedding_provider is not None and not isinstance(embedding_provider, ScheduledEmbedding):
            self.embedding_provider = ScheduledEmbedding(embedding_provider, self.scheduler)
        self.profile = profile or getattr(embedding_provider, "profile", None)
        self.decision_provider = decision_provider
        self.jobs = jobs or JobStore(store)
        self.worker_id = worker_id or str(uuid.uuid4())
        self._active_intent = None
        self._hydrated_intents = set()
        database_parent = Path(store.path).parent
        if isinstance(self.index, LocalCosineIndex):
            self.index.prepare_cache(database_parent)
        if shared_lock_root is None:
            shared_lock_root = (database_parent.parent.parent / "index-locks"
                                if database_parent.parent.name == "generations" else database_parent / "index-locks")
        self.shared_lock_root = Path(shared_lock_root)
        if embedding_provider is not None and self.profile is None:
            raise ProviderError("embedding_profile_required")
        provider_profile = getattr(embedding_provider, "profile", self.profile)
        if provider_profile is not None and self.profile.profile_id != provider_profile.profile_id:
            raise ProviderError("embedding_profile_mismatch")
        with self._db() as db:
            db.executescript(_INDEX_SCHEMA)
            if self.profile:
                row = db.execute("SELECT profile_json,generation_id FROM index_profiles WHERE profile_id=?", (self.profile.profile_id,)).fetchone()
                expected = _json(asdict(self.profile))
                if row is not None and (row[0] != expected or row[1] != store.generation_id):
                    raise ProviderError("embedding_profile_mismatch")
                db.execute("INSERT OR IGNORE INTO index_profiles VALUES(?,?,?)", (self.profile.profile_id, expected, store.generation_id))
            db.commit()
        if self.index is not None and hasattr(self.index, "pending_receipt_check"):
            self.index.pending_receipt_check = self._has_pending_receipt
        self._hydrate()

    @contextmanager
    def _db(self):
        connection = self.store.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _index_lock(self):
        # Hold a native process lock across network plus durable receipt, never
        # a database transaction. A killed process releases only ownership;
        # its durable sending intent remains unresolved.
        with self._db() as db:
            database = next(row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main")
        if not database:
            raise SemanticValidationError("search_private_database_required")
        identity = getattr(self.index, "index_identity", "local")
        if any(path.is_symlink() for path in (self.shared_lock_root, *self.shared_lock_root.parents)):
            raise SemanticValidationError("search_index_lock_symlink")
        create_platform_services().require("private_storage").ensure_directory(str(self.shared_lock_root))
        path = str(self.shared_lock_root / ("semantic-index-" + identity + ".lock"))
        with create_platform_services().require("locks").acquire(path, mode=LockMode.EXCLUSIVE, blocking=False):
            yield

    def _has_pending_receipt(self, index_identity):
        if self._shared_pending(index_identity):
            return True
        with self._db() as db:
            row = db.execute("SELECT 1 FROM semantic_index_receipts WHERE index_identity=? AND status IN ('sending','pending','unknown') AND intent_id<>? LIMIT 1",
                             (index_identity, self._active_intent or "")).fetchone()
        return row is not None

    def _guard_storage(self, index_identity):
        if not isinstance(index_identity, str) or re.fullmatch(r"[a-f0-9]{64}", index_identity) is None:
            raise SemanticValidationError("search_invalid_index_identity")
        return StateStorage(str(self.shared_lock_root / ("semantic-index-" + index_identity + ".pending.json")))

    def _write_guard(self, intent):
        # This content-free pointer is shared across generations. The receipt
        # remains authoritative in its owning canonical database, not here.
        value = {"schema": 1, "index_identity": self.index.index_identity,
                 "generation_id": self.store.generation_id, "intent_id": intent,
                 "database": str(Path(self.store.path).resolve())}
        self._guard_storage(self.index.index_identity).write_bytes(_json(value).encode())

    def _shared_pending(self, index_identity):
        storage = self._guard_storage(index_identity)
        try:
            value = json.loads(Path(storage.operational_path()).read_bytes())
        except FileNotFoundError:
            return False
        except (OSError, ValueError, TypeError):
            raise SemanticValidationError("search_pending_guard_unreadable") from None
        if value is None:
            return False
        if (not isinstance(value, dict) or set(value) != {"schema", "index_identity", "generation_id", "intent_id", "database"}
                or value["schema"] != 1 or value["index_identity"] != index_identity
                or any(not isinstance(value[key], str) or not value[key] for key in ("generation_id", "intent_id", "database"))):
            raise SemanticValidationError("search_pending_guard_invalid")
        database = Path(value["database"])
        if not database.is_absolute() or any(path.is_symlink() for path in (database, *database.parents)):
            raise SemanticValidationError("search_pending_guard_invalid")
        if value["intent_id"] == self._active_intent and database == Path(self.store.path).resolve():
            return False
        connection = None
        try:
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            row = connection.execute("SELECT status,generation_id,index_identity FROM semantic_index_receipts WHERE intent_id=?",
                                     (value["intent_id"],)).fetchone()
        except sqlite3.Error:
            raise SemanticValidationError("search_pending_guard_owner_unavailable") from None
        finally:
            if connection is not None:
                connection.close()
        if row is None or row[1:] != (value["generation_id"], index_identity):
            raise SemanticValidationError("search_pending_guard_receipt_missing")
        if row[0] in {"indexed", "cancelled"}:
            storage.write_bytes(b"null")
            return False
        if row[0] not in {"sending", "pending", "unknown"}:
            raise SemanticValidationError("search_pending_guard_invalid")
        return True

    def _receipts(self):
        if self.profile is None or self.index is None:
            return
        # Close each bounded read before callers perform provider/index work.
        # Metadata consumers must not load every receipt's vector arrays.
        fields = ("sequence,intent_id,status,receipt_json IS NOT NULL AS has_receipt,"
                  "(SELECT json_group_array(json_extract(value,'$.id')) FROM json_each(records_json)) AS record_ids_json")
        sequence = 0
        while True:
            with self._db() as db:
                page = [dict(row) for row in db.execute(
                    f"SELECT {fields} FROM semantic_index_receipts WHERE profile_id=? AND generation_id=? AND index_identity=? AND sequence>? ORDER BY sequence LIMIT 100",
                    (self.profile.profile_id, self.store.generation_id, self.index.index_identity, sequence))]
            if not page:
                return
            for row in page:
                sequence = row["sequence"]
                yield row

    def _hydrate(self):
        if self.profile is None or self.index is None:
            return
        for row in self._receipts():
            if not row["has_receipt"]:
                continue
            if row["intent_id"] in self._hydrated_intents:
                continue
            receipt = self._receipt(row["intent_id"])
            if isinstance(self.index, LocalCosineIndex) and receipt.indexed:
                self.index.upsert(receipt.records, profile=self.profile, generation_id=self.store.generation_id,
                                  request_id="hydrate:" + row["intent_id"])
            elif hasattr(self.index, "restore_receipt"):
                self.index.restore_receipt(receipt)
            if receipt.indexed:
                self._hydrated_intents.add(row["intent_id"])

    def _receipt(self, intent_id):
        with self._db() as db:
            row = db.execute("SELECT receipt_json FROM semantic_index_receipts WHERE intent_id=? AND profile_id=? AND generation_id=? AND index_identity=?",
                             (intent_id, self.profile.profile_id, self.store.generation_id, self.index.index_identity)).fetchone()
        if row is None or row[0] is None:
            raise SemanticValidationError("search_pending_guard_receipt_missing")
        return IndexReceipt.from_dict(json.loads(row[0]))

    def documents(self):
        documents = []
        with self._db() as db:
            sources = {row["source_version_id"]: dict(row) for row in db.execute("SELECT source_version_id,source_namespace,chat_id,source_id FROM source_records")}
            source_heads = {(row["source_namespace"], row["chat_id"], row["source_id"]): row["source_version_id"]
                            for row in db.execute("SELECT * FROM source_heads")}
        for row in self.store.documents():
            kind = row["owner_type"].lower()
            if kind not in DOCUMENT_KINDS:
                raise SemanticValidationError("search_unknown_document_kind")
            metadata = row.get("metadata") or {}
            reply_to = metadata.get("reply_to")
            if kind == "source" and reply_to and row["owner_id"] in sources:
                source = sources[row["owner_id"]]
                reply_to = source_heads.get((source["source_namespace"], source["chat_id"], reply_to), reply_to)
            document_id = metadata.get("document_id") or kind + ":" + row["owner_id"]
            documents.append(SearchDocument(
                document_id, kind, row["owner_id"], row["revision"], redact_urls_in_text(row["text"]),
                metadata.get("item_id"), tuple(metadata.get("source_ids", ())), metadata.get("case_id"),
                reply_to, tuple(metadata.get("related_ids", ())),
            ))
        # Body units and historical semantic changes remain separately typed
        # references. They are projections of canonical rows, not another store.
        for item in self.store.list_items():
            source_ids = tuple(dict.fromkeys(identity for observation in item.get("observations", ()) for identity in observation.get("source_ids", ())))
            for unit in item.get("units", ()):
                documents.append(SearchDocument("body:" + item["item_id"] + ":" + unit["unit_id"], "body", unit["unit_id"],
                                                1, redact_urls_in_text(unit["text"]), item["item_id"], source_ids))
        for claim in self.store.list_claims():
            item_ids = claim.get("item_ids", ())
            body = _json({key: claim.get(key) for key in ("subject_id", "case_id", "condition", "channel", "version", "phase", "observed_at", "predicates", "status")})
            documents.append(SearchDocument("claim:" + claim["claim_id"], "claim", claim["claim_id"], 1,
                                            redact_urls_in_text(body), item_ids[0] if item_ids else None,
                                            tuple(claim.get("source_ids", ())), claim.get("case_id"),
                                            related_ids=tuple(item_ids)))
        with self._db() as db:
            for row in db.execute("SELECT c.*,r.snapshot_json FROM change_events c LEFT JOIN item_revisions r ON r.item_id=c.item_id AND r.revision=c.revision"):
                snapshot = json.loads(row["snapshot_json"]) if row["snapshot_json"] else {}
                text = snapshot.get("title", "") + " " + row["semantic_effect"]
                text += "\n" + "\n".join(unit["text"] for unit in snapshot.get("units", ()))
                documents.append(SearchDocument("change:" + str(row["change_id"]), "change", str(row["change_id"]), row["revision"],
                                                redact_urls_in_text(text), row["item_id"], (), related_ids=(row["item_id"],)))
        if len({document.document_id for document in documents}) != len(documents):
            raise SemanticValidationError("search_duplicate_document")
        return tuple(documents)

    def _lexical(self, query, documents, limit):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            known = {row[0]: row[1] for row in db.execute("SELECT document_id,fingerprint FROM semantic_lexical_documents")}
            current = {document.document_id for document in documents}
            for identity in set(known) - current:
                db.execute("DELETE FROM semantic_lexical_documents WHERE document_id=?", (identity,))
            for document in documents:
                digest = _fingerprint(document)
                if known.get(document.document_id) == digest:
                    continue
                body = unicodedata.normalize("NFKC", document.text).casefold()
                db.execute("INSERT INTO semantic_lexical_documents VALUES(?,?,?) ON CONFLICT(document_id) DO UPDATE SET fingerprint=excluded.fingerprint,body=excluded.body",
                           (document.document_id, digest, body))
                db.execute("DELETE FROM semantic_lexical_terms WHERE document_id=?", (document.document_id,))
                db.executemany("INSERT INTO semantic_lexical_terms VALUES(?,?)", [(token, document.document_id) for token in lexical_tokens(body)])
            db.commit()
            scores = defaultdict(float)
            for token in lexical_tokens(query):
                matched = [row[0] for row in db.execute("SELECT document_id FROM semantic_lexical_terms WHERE token=?", (token,))]
                weight = (1 + math.log((len(documents) + 1) / (len(matched) + 1))) * min(len(token), 4)
                for identity in matched:
                    scores[identity] += weight
            normalized = unicodedata.normalize("NFKC", query).casefold().strip()
            for row in db.execute("SELECT document_id FROM semantic_lexical_documents WHERE instr(body,?)>0", (normalized,)):
                scores[row[0]] += 8 + len(normalized)
        return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]

    def _embeddings(self):
        if self.profile is None:
            return {}
        with self._db() as db:
            return {row["vector_id"]: dict(row) for row in db.execute(
                "SELECT vector_id,profile_id,generation_id,document_id,document_revision,fingerprint,job_id FROM embedding_records WHERE profile_id=? AND generation_id=?",
                (self.profile.profile_id, self.store.generation_id))}

    def _vectors(self, identities):
        """Hydrate only a bounded publication/query page, never the whole index."""
        iterator = iter(identities)
        while page := tuple(islice(iterator, 100)):
            with self._db() as db:
                rows = {row["vector_id"]: row["artifact_json"] for row in db.execute(
                    "SELECT vector_id,artifact_json FROM embedding_records WHERE profile_id=? AND generation_id=? AND vector_id IN (" + ",".join("?" for _ in page) + ")",
                    (self.profile.profile_id, self.store.generation_id, *page))}
            for identity in page:
                if identity not in rows:
                    raise SemanticValidationError("search_embedding_missing")
                yield VectorRecord(identity, tuple(json.loads(rows.pop(identity))["vector"]))

    def _provider_artifact(self, kind, texts, purpose):
        if self.embedding_provider is None or self.profile is None:
            raise ProviderError("embedding_not_configured")
        job = self.jobs.ensure(kind, {"texts": texts, "purpose": purpose},
                               {"profile_id": self.profile.profile_id, "generation_id": self.store.generation_id})
        artifact = self.jobs.accepted(job["job_id"])
        if artifact is not None:
            return job, artifact
        claim = self.jobs.claim(job["job_id"], self.worker_id)
        if claim is None:
            raise SemanticValidationError("search_job_busy_or_unknown")
        try:
            with maintain_claim(lambda: self.jobs.renew(job["job_id"], self.worker_id, attempt=claim["attempt"])):
                result = self.embedding_provider.embed(texts, purpose=purpose, request_id=job["job_id"], generation_id=self.store.generation_id)
            if result.profile_id != self.profile.profile_id or len(result.vectors) != len(texts):
                raise ProviderError("embedding_profile_or_count_mismatch")
            vectors = [list(validate_vector(vector, self.profile, normalize=True)) for vector in result.vectors]
            expected = {"vectors": vectors, "profile_id": result.profile_id, "route": asdict(result.route)}
            artifact = self.jobs.accept(job["job_id"], self.worker_id, expected,
                                        validate=lambda value: validate_accepted_artifact(value, expected))
        except Exception as exc:
            if not_dispatched(exc, job["job_id"]):
                self.jobs.defer(job["job_id"], self.worker_id, exc, attempt=claim["attempt"])
            else:
                self.jobs.fail(job["job_id"], self.worker_id, getattr(exc, "code", "embedding_failed"),
                               unknown=getattr(exc, "outcome_unknown", False))
            raise
        return job, artifact

    def prepare_index(self):
        """Read documents/embeddings once for an index_all pass at one revision."""
        if self.profile is None:
            raise ProviderError("embedding_not_configured")
        revision = self.store.read_revision()
        documents = tuple(document for document in self.documents() if document.text.strip())
        rows = self._embeddings()
        current = {document.document_id: _fingerprint(document) for document in documents}
        existing = {(row["document_id"], row["fingerprint"]) for row in rows.values()}
        pending = [document for document in documents if (document.document_id, current[document.document_id]) not in existing]
        prepared = IndexPreparation(self, revision, documents, rows, current, pending)
        self._assert_preparation(prepared)
        return prepared

    def _assert_preparation(self, prepared, db=None):
        revision = (db.execute("SELECT revision FROM generation_manifest").fetchone()[0]
                    if db is not None else self.store.read_revision())
        if prepared.owner is not self or revision != prepared.revision:
            raise SemanticValidationError("search_snapshot_changed")

    def _index_remaining(self, prepared):
        self._assert_preparation(prepared)
        confirmed = self._confirmed_ids()
        indexed = {row["document_id"] for vector_id, row in prepared.rows.items()
                   if vector_id in confirmed and prepared.current.get(row["document_id"]) == row["fingerprint"]}
        # Missing embeddings are pending too. A fully indexed first page does
        # not prove completion while later canonical documents are untouched.
        return len(prepared.current) - len(indexed)

    def _prepare_embedding_group(self, prepared, documents):
        # Equal text shares one job, even when source/Observation/Body IDs differ.
        # A single complete input is one provider dispatch, making RPM exact.
        job, artifact = self._provider_artifact("document_embedding", [documents[0].text], "document")
        inserted = {}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_preparation(prepared, db)
            for document in documents:
                digest = prepared.current[document.document_id]
                vector_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _json([self.store.generation_id, self.profile.profile_id, document.document_id, digest])))
                record = {"vector_id": vector_id, "profile_id": self.profile.profile_id,
                          "generation_id": self.store.generation_id, "document_id": document.document_id,
                          "document_revision": document.revision, "fingerprint": digest,
                          "artifact_json": _json({"vector": artifact["vectors"][0], "route": artifact["route"]}),
                          "job_id": job["job_id"]}
                db.execute("INSERT OR IGNORE INTO embedding_records VALUES(?,?,?,?,?,?,?,?)", tuple(record.values()))
                inserted[vector_id] = {key: value for key, value in record.items() if key != "artifact_json"}
            db.commit()
        with prepared.lock:
            prepared.rows.update(inserted)
        from core.semantic.jobs import accepted_artifact_receipt
        self.jobs.finish(job["job_id"], accepted_artifact_receipt(job["job_id"], artifact))

    def index_pending(self, *, limit=100, preparation=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise SemanticValidationError("search_invalid_batch_limit")
        if self.profile is None:
            raise ProviderError("embedding_not_configured")
        preparation = preparation or self.prepare_index()
        self._assert_preparation(preparation)
        pending = preparation.pending[:limit]
        if pending:
            groups = defaultdict(list)
            for document in pending:
                groups[document.text].append(document)
            self.scheduler.map("embedding", groups.values(), lambda documents: self._prepare_embedding_group(preparation, documents))
            del preparation.pending[:len(pending)]
        if self.index is None:
            return {"embedded": len(pending), "indexed": 0, "pending": len(preparation.documents)}
        with self._index_lock():
            self._assert_preparation(preparation)
            if self._has_pending_receipt(self.index.index_identity):
                return {"embedded": len(pending), "indexed": 0, "pending": self._index_remaining(preparation)}
            rows = preparation.rows
            already_submitted = {identity for row in self._receipts() if row["status"] != "cancelled"
                                 for identity in json.loads(row["record_ids_json"])}
            current = preparation.current
            identities = islice((vector_id for vector_id, row in rows.items()
                                 if vector_id not in already_submitted and current.get(row["document_id"]) == row["fingerprint"]), limit)
            records = tuple(self._vectors(identities))
            if not records:
                return {"embedded": len(pending), "indexed": 0, "pending": self._index_remaining(preparation)}
            intent = str(uuid.uuid4())
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                self._assert_preparation(preparation, db)
                db.execute("INSERT INTO semantic_index_receipts(intent_id,profile_id,generation_id,index_identity,status,records_json) VALUES(?,?,?,?,?,?)",
                           (intent, self.profile.profile_id, self.store.generation_id, self.index.index_identity, "sending", _json([asdict(record) for record in records])))
                db.commit()
            self._write_guard(intent)
            self._active_intent = intent
            try:
                receipt = self.index.upsert(records, profile=self.profile, generation_id=self.store.generation_id, request_id=intent)
                self._validate_receipt(receipt, records)
                with self._db() as db:
                    db.execute("UPDATE semantic_index_receipts SET status=?,receipt_json=? WHERE intent_id=?",
                               ("indexed" if receipt.indexed else "pending", _json(receipt.to_dict()), intent))
                    db.commit()
            except Exception as exc:
                # Unknown transport or process interruption never permits a
                # second upload. Known preflight rejection can be cancelled.
                status = "unknown" if getattr(exc, "outcome_unknown", True) else "cancelled"
                with self._db() as db:
                    db.execute("UPDATE semantic_index_receipts SET status=?,error_code=? WHERE intent_id=?",
                               (status, getattr(exc, "code", "vector_mutation_failed"), intent))
                    db.commit()
                raise
            finally:
                self._active_intent = None
        return {"embedded": len(pending), "indexed": len(records) if receipt.indexed else 0,
                "pending": self._index_remaining(preparation), "intent_id": intent}

    def _validate_receipt(self, receipt, expected):
        parsed = IndexReceipt.from_dict(receipt.to_dict())
        if (parsed.profile_id != self.profile.profile_id or parsed.generation_id != self.store.generation_id
                or parsed.index_identity != self.index.index_identity
                or {record.id for record in parsed.records} != {record.id for record in expected}):
            raise ProviderError("vector_receipt_binding_mismatch", outcome_unknown=True)
        expected_values = {record.id: record.values for record in expected}
        for record in parsed.records:
            values = validate_vector(record.values, self.profile)
            # Cloudflare receipts contain canonical float32 publication values.
            if any(not math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-7) for a, b in zip(values, expected_values[record.id])):
                raise ProviderError("vector_receipt_values_mismatch", outcome_unknown=True)

    def confirm_pending(self):
        if self.index is None or self.profile is None:
            raise ProviderError("vector_not_configured")
        result = []
        with self._index_lock():
            for row in self._receipts():
                if row["status"] != "pending" or not row["has_receipt"]:
                    continue
                receipt = self._receipt(row["intent_id"])
                confirmed = self.index.confirm(receipt, profile=self.profile, request_id=str(uuid.uuid4()))
                self._validate_receipt(confirmed, receipt.records)
                if confirmed.mutation_id != receipt.mutation_id:
                    raise ProviderError("vector_confirmation_mutation_mismatch")
                with self._db() as db:
                    db.execute("UPDATE semantic_index_receipts SET status=?,receipt_json=? WHERE intent_id=?",
                               ("indexed" if confirmed.indexed else "pending", _json(confirmed.to_dict()), row["intent_id"]))
                    db.commit()
                result.append({"intent_id": row["intent_id"], "indexed": confirmed.indexed})
        return tuple(result)

    def _confirmed_ids(self):
        # Only the contiguous confirmed receipt prefix can be trusted. A newer
        # receipt never makes an older unresolved mutation disappear.
        confirmed = set()
        for row in self._receipts():
            if row["status"] == "cancelled":
                continue
            if row["status"] != "indexed" or not row["has_receipt"]:
                break
            confirmed.update(json.loads(row["record_ids_json"]))
        return confirmed

    def _coverage(self, documents, *, unknown=()):
        rows = self._embeddings()
        current = {document.document_id: _fingerprint(document) for document in documents if document.text.strip()}
        valid = {identity: row for identity, row in rows.items() if current.get(row["document_id"]) == row["fingerprint"]}
        confirmed = self._confirmed_ids()
        source = self.store.coverage() if hasattr(self.store, "coverage") else {}
        reasons = tuple(unknown) + (() if source else ("source_coverage_unavailable",))
        return SearchCoverage(self.store.read_revision(), len(documents), len(current) - len(valid),
                              len(valid) - len(set(valid) & confirmed), int(source.get("unread_sources", 0)),
                              int(source.get("unclassified_sources", 0)), source.get("source_complete") is True, reasons)

    def relation_candidates(self, *, source_ids=(), case_ids=(), exact_ids=(), correction_ids=()):
        documents = self.documents()
        dependencies = self._dependencies()
        wanted = structural_candidates(documents, source_ids=source_ids, case_ids=case_ids,
                                       exact_ids=exact_ids, correction_ids=correction_ids, dependencies=dependencies)
        return tuple(document for document in documents if document.document_id in wanted)

    def _dependencies(self):
        dependencies = defaultdict(list)
        if hasattr(self.store, "list_relations"):
            for relation in self.store.list_relations():
                if relation.get("direction") == "directed":
                    # A -> B means B is affected by the originating evidence A.
                    source = relation.get("from_item_id") or relation.get("from_id")
                    target = relation.get("to_item_id") or relation.get("to_id")
                    if source and target:
                        dependencies[source].append(target)
        with self._db() as db:
            for row in db.execute("SELECT from_claim_id,to_claim_id FROM claim_relations"):
                dependencies[row[0]].append(row[1])
            for row in db.execute("SELECT claim_id,item_id FROM item_claims"):
                dependencies[row[0]].append(row[1])
        for claim in self.store.list_claims():
            for source_id in claim.get("source_ids", ()):
                dependencies[source_id].append(claim["claim_id"])
        return dependencies

    def _rerank(self, query, subset, documents):
        excerpts = {}
        for identity in subset:
            document = documents[identity]
            # Reranking is a bounded ordering hint. Full canonical documents and
            # structural candidates remain available independently of excerpts.
            matches = [document.text.casefold().find(token) for token in lexical_tokens(query)]
            position = min((index for index in matches if index >= 0), default=0)
            start = max(0, position - 500)
            excerpts[identity] = {"text": document.text[start:start + 2000], "start": start,
                                  "total_characters": len(document.text), "revision": document.revision}
        request = DecisionRequest({"query": query, "documents": excerpts},
                                  tuple(DecisionQuestion("rank" + str(i), "score", "How directly does this supplied excerpt answer the query?",
                                                         ["irrelevant", "related context", "useful evidence", "direct exact evidence"])
                                        for i in range(len(subset))))
        job = self.jobs.ensure("search_rerank", {"request": request.as_payload(), "document_ids": subset},
                               {"contract": "semantic-rerank-v1", "route": asdict(self.decision_provider.route)})
        artifact = self.jobs.accepted(job["job_id"])
        if artifact is None:
            claim = self.jobs.claim(job["job_id"], self.worker_id)
            if claim is None:
                raise SemanticValidationError("search_job_busy_or_unknown")
            try:
                with maintain_claim(lambda: self.jobs.renew(job["job_id"], self.worker_id, attempt=claim["attempt"])):
                    result = self.decision_provider.decide(request, request_id=job["job_id"], generation_id=self.store.generation_id)
                raw = {key: {field: value for field, value in asdict(answer).items() if value is not None}
                       for key, answer in result.answers.items()}
                for question in request.questions:
                    if question.id in raw:
                        raw[question.id]["legend"] = {str(i): text for i, text in enumerate(question.criteria)}
                answers = parse_decision_answers(raw, request)
                expected = {"scores": {identity: answers["rank" + str(i)].score for i, identity in enumerate(subset)},
                            "answers": {key: asdict(answer) for key, answer in answers.items()}, "route": asdict(result.route)}
                artifact = self.jobs.accept(job["job_id"], self.worker_id, expected,
                                            validate=lambda value: validate_accepted_artifact(value, expected))
            except Exception as exc:
                if not_dispatched(exc, job["job_id"]):
                    self.jobs.defer(job["job_id"], self.worker_id, exc, attempt=claim["attempt"])
                else:
                    self.jobs.fail(job["job_id"], self.worker_id, getattr(exc, "code", "search_rerank_failed"),
                                   unknown=getattr(exc, "outcome_unknown", False))
                raise
        from core.semantic.jobs import accepted_artifact_receipt
        self.jobs.finish(job["job_id"], accepted_artifact_receipt(job["job_id"], artifact))
        return artifact["scores"]

    def search(self, query, *, lexical_limit=40, dense_limit=40, rerank_limit=10, display_limit=5,
               source_ids=(), case_ids=(), exact_ids=(), correction_ids=()):
        if not isinstance(query, str) or not query.strip():
            raise SemanticValidationError("search_empty_query")
        if (type(lexical_limit) is not int or lexical_limit < 1 or type(dense_limit) is not int or not 1 <= dense_limit <= 100
                or type(rerank_limit) is not int or not 8 <= rerank_limit <= 12
                or type(display_limit) is not int or display_limit < 1):
            raise SemanticValidationError("search_invalid_limits")
        query = redact_urls_in_text(query)
        snapshot_revision = self.store.read_revision()
        documents = self.documents()
        by_id = {document.document_id: document for document in documents}
        lexical = self._lexical(query, documents, lexical_limit)
        dense, reasons = [], []
        rows = self._embeddings()
        current = {identity: row for identity, row in rows.items() if row["document_id"] in by_id
                   and row["fingerprint"] == _fingerprint(by_id[row["document_id"]])}
        if self.embedding_provider is not None:
            try:
                job, artifact = self._provider_artifact("query_embedding", [query], "query")
                query_vector = artifact["vectors"][0]
                from core.semantic.jobs import accepted_artifact_receipt
                self.jobs.finish(job["job_id"], accepted_artifact_receipt(job["job_id"], artifact))
                confirmed = self._confirmed_ids()
                remote = ()
                if self.index is not None and confirmed:
                    self._hydrate()
                    remote = self.index.query(query_vector, profile=self.profile, generation_id=self.store.generation_id,
                                              top_k=dense_limit, request_id=str(uuid.uuid4()))
                dense_scores = {current[match.id]["document_id"]: match.score for match in remote
                                if match.id in confirmed and match.id in current}
                # Every current canonical embedding outside the confirmed prefix
                # is scored locally, including pending remote mutations.
                for record in self._vectors(identity for identity in current if identity not in confirmed or self.index is None):
                    dense_scores[current[record.id]["document_id"]] = _cosine(query_vector, record.values)
                dense = sorted(dense_scores.items(), key=lambda pair: (-pair[1], pair[0]))[:dense_limit]
            except (ProviderError, SemanticValidationError) as exc:
                reasons.append(exc.code)
        else:
            reasons.append("embedding_not_configured")
        structural = structural_candidates(documents, source_ids=source_ids, case_ids=case_ids, exact_ids=exact_ids,
                                           correction_ids=correction_ids, dependencies=self._dependencies())
        fused = reciprocal_rank_fusion(lexical, dense)
        scores = dict(fused)
        ordered = sorted(structural) + [identity for identity, _score in fused if identity not in structural]
        if self.decision_provider is not None and ordered:
            subset = ordered[:rerank_limit]
            try:
                relevance = self._rerank(query, subset, by_id)
                ordered = sorted(subset, key=lambda identity: (-relevance[identity], -scores.get(identity, 0), identity)) + ordered[len(subset):]
            except (ProviderError, SemanticValidationError) as exc:
                reasons.append(exc.code)
        lexical_ids, dense_ids = {identity for identity, _ in lexical}, {identity for identity, _ in dense}
        hits = []
        for identity in ordered[:display_limit]:
            document = by_id[identity]
            channels = tuple(channel for channel, ids in (("lexical", lexical_ids), ("dense", dense_ids), ("structural", structural)) if identity in ids)
            hits.append(SearchHit(document.kind, document.object_id, identity, document.item_id, document.revision,
                                  document.text, scores.get(identity, 0), channels, document.source_ids))
        if snapshot_revision != self.store.read_revision():
            reasons.append("canonical_snapshot_changed")
        return SearchResult(tuple(hits), self._coverage(documents, unknown=reasons), len(ordered), tuple(sorted(structural)))
