"""Content-free inspection of existing semantic generations and their queues.

This is a reader, never a generation, checkpoint or budget writer. It does not
construct runtime/providers, open source text/CAS objects, inspect credentials,
or repair a missing schema. Counts describe local evidence, not a new verifier
receipt or permission to advance a checkpoint.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from string import Formatter
import time
from urllib.parse import urlsplit

from core.config import DATA_DIR
from core.monitor_state import MONITOR_V3_STATE_SCHEMA, MonitorStateError, MonitorStateStore
from core.source_adapter import decode_cursor_token
from .rebuild import GenerationRegistry, RebuildError, read_json, regular_exists, safe_path
from .store import SCHEMA as GENERATION_SCHEMA


HEALTH_SCHEMA = "we-groupchat-obsidian.semantic-health.v1"
_PROFILE_SCHEMA = "we-groupchat-obsidian.semantic-providers.v1"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_JOB_STATES = ("pending", "running", "failed", "unknown", "accepted", "done")


class _InspectionError(Exception):
    def __init__(self, code):
        self.code = code


def _admit_path(path, protected_root):
    # Compare strings before any metadata lookup. A mistaken config must not
    # turn health into a protected-source stat, including through a parent root.
    path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if protected_root:
        protected = Path(os.path.abspath(os.path.expanduser(os.fspath(protected_root))))
        if path == protected or path.is_relative_to(protected) or protected.is_relative_to(path):
            raise _InspectionError("semantic_health_source_overlap")
    return safe_path(path.parent, path.name)


@contextmanager
def _readonly(path):
    if not regular_exists(path):
        raise _InspectionError("semantic_health_database_missing")
    for suffix in ("-wal", "-shm", "-journal"):
        regular_exists(Path(str(path) + suffix))  # Reject unsafe sidecar nodes.
    wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
    def signature(file):
        try:
            observed = file.stat()
            return observed.st_ino, observed.st_size, observed.st_mtime_ns
        except FileNotFoundError:
            return None
    before = (signature(Path(path)), signature(wal))
    journal = signature(Path(str(path) + "-journal"))
    if journal is not None and journal[1] > 0:
        raise _InspectionError("semantic_health_database_busy")
    has_wal = before[1] is not None and before[1][1] > 0
    if has_wal and not regular_exists(shm):
        raise _InspectionError("semantic_health_wal_unavailable")
    # SQLite mode=ro alone creates empty WAL/SHM files for an idle WAL database.
    # Immutable admission avoids that on a checkpointed DB. A real WAL must be
    # read normally; ignoring it would report stale counts. Compare afterwards
    # so a writer starting during an immutable read cannot produce healthy data.
    mode = "?mode=ro" if has_wal else "?mode=ro&immutable=1"
    conn = sqlite3.connect(Path(path).absolute().as_uri() + mode, uri=True, timeout=1)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("BEGIN")
        yield conn
        if (signature(Path(path)), signature(wal)) != before:
            raise _InspectionError("semantic_health_changed_during_inspection")
    finally:
        conn.close()


def _valid_local_embedding(profile):
    """Check the v1 descriptor as data, without importing or probing a provider."""
    required = {"endpoint", "model", "dimensions", "context_window_tokens"}
    optional = {"api", "pooling", "normalization", "document_template", "query_template", "chunker",
                "metric", "max_input_bytes", "max_batch_size", "max_batch_bytes", "timeout"}
    if not isinstance(profile, dict) or not required <= profile.keys() or profile.keys() - required - optional:
        return False
    model, dimensions, context = profile["model"], profile["dimensions"], profile["context_window_tokens"]
    if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", model) or "://" in model
            or type(dimensions) is not int or not 1 <= dimensions <= 4096
            or type(context) is not int or not 65 <= context <= 1_000_000):
        return False
    limit = profile.get("max_input_bytes", min(8000, context - 64))
    batch_size, batch_bytes = profile.get("max_batch_size", 32), profile.get("max_batch_bytes", 32000)
    timeout = profile.get("timeout", 45)
    if (type(limit) is not int or not 1 <= limit <= context - 64
            or type(batch_size) is not int or not 1 <= batch_size <= 128
            or type(batch_bytes) is not int or not limit <= batch_bytes <= 8_000_000
            or type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= 300
            or profile.get("normalization", "l2") not in ("l2", "none")
            or profile.get("metric", "cosine") != "cosine"
            or any(not isinstance(profile.get(field, default), str) or not profile.get(field, default)
                   for field, default in (("pooling", "provider-default"), ("chunker", "semantic-v1")))):
        return False
    for field in ("document_template", "query_template"):
        fields = [(name, spec, conversion) for _, name, spec, conversion in
                  Formatter().parse(profile.get(field, "{text}")) if name is not None]
        if fields != [("text", "", None)]:
            return False
    endpoint, api = profile["endpoint"], profile.get("api", "openai")
    if (not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048
            or any(character.isspace() or ord(character) < 32 for character in endpoint)
            or any(character in endpoint for character in ("\\", "%", "?", "#"))
            or api not in ("openai", "ollama")):
        return False
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None
            or parsed.port is None or not 1 <= parsed.port <= 65535
            or parsed.path != ("/v1/embeddings" if api == "openai" else "/api/embed")):
        return False
    address = ipaddress.ip_address("127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname)
    return address.is_loopback and (address.version == 4 or address == ipaddress.IPv6Address("::1"))


def _profile_accounts(raw, count, kind):
    """A valid zero-Cloudflare selection is distinct from missing/invalid data."""
    if count == 0:
        return "profile_missing", ()
    if count != 1 or kind != "object" or not isinstance(raw, str):
        return "profile_invalid", ()
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def invalid_constant(_value):
        raise ValueError
    try:
        profile = json.loads(raw, object_pairs_hook=unique_fields, parse_constant=invalid_constant)
        fields = {"schema", "decision", "embedding", "vector_index", "account_id", "vectorize_index", "secret_name", "local_embedding"}
        if (not isinstance(profile, dict) or set(profile) - fields or profile.get("schema") != _PROFILE_SCHEMA
                or profile.get("decision") not in ("text", "cloudflare")
                or profile.get("embedding") not in ("local", "cloudflare")
                or profile.get("vector_index") not in ("local", "cloudflare")
                or (profile["embedding"] == "local" and not _valid_local_embedding(profile.get("local_embedding")))
                or (profile["embedding"] != "local" and "local_embedding" in profile)):
            return "profile_invalid", ()
        roles = {key for key in ("decision", "embedding", "vector_index") if profile[key] == "cloudflare"}
        if not roles:
            return "valid", ()
        account, secret = profile.get("account_id"), profile.get("secret_name")
        if (not isinstance(account, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", account)
                or not isinstance(secret, str) or not secret.strip()
                or ("vector_index" in roles and (not isinstance(profile.get("vectorize_index"), str)
                                                 or not profile["vectorize_index"]))):
            return "profile_invalid", ()
        account_hash = hashlib.sha256(account.lower().encode("ascii")).hexdigest()
        keys = []
        if roles & {"decision", "embedding"}:
            keys.append("cf-ai:" + account_hash)
        if "vector_index" in roles:
            keys.append("cf-vectorize:" + account_hash)
        return "valid", tuple(keys)
    except (TypeError, ValueError, OverflowError):
        return "profile_invalid", ()


def _manifest(conn, generation):
    # Read only the provider descriptor and activation marker, never unrelated
    # generation metadata, source payloads, model artifacts or credentials.
    rows = conn.execute("SELECT generation_id,schema,revision, "
        "json_extract(metadata_json,'$.provider_profile') AS profile, "
        "json_type(metadata_json,'$.provider_profile') AS profile_type, "
        "(SELECT COUNT(*) FROM json_each(metadata_json) WHERE key='provider_profile') AS profile_count, "
        "json_type(metadata_json,'$.activation') AS activated FROM generation_manifest").fetchall()
    if (len(rows) != 1 or rows[0]["generation_id"] != generation or rows[0]["schema"] != GENERATION_SCHEMA
            or type(rows[0]["revision"]) is not int or rows[0]["revision"] < 0):
        raise _InspectionError("semantic_health_generation_invalid")
    row = rows[0]
    return row["revision"], _profile_accounts(row["profile"], row["profile_count"], row["profile_type"]), row["activated"] == "object"


def _counts(conn, table, states):
    result = {state: 0 for state in states}
    result["invalid"] = 0
    for row in conn.execute(f"SELECT status,COUNT(*) FROM {table} GROUP BY status"):
        key = row[0] if row[0] in result else "invalid"
        result[key] += int(row[1])
    return result


def _database(path, generation, now):
    with _readonly(path) as conn:
        revision, budget_profile, activated = _manifest(conn, generation)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        scalar = lambda sql: int(conn.execute(sql).fetchone()[0])
        raw = {
            "captured_sources": scalar("SELECT COUNT(*) FROM source_records"),
            "captured_batches": scalar("SELECT COUNT(*) FROM capture_batches"),
            "processed_batches": scalar("SELECT COUNT(*) FROM capture_batches JOIN processing_receipts USING(batch_id)"),
            "processing_pending_batches": scalar("SELECT COUNT(*) FROM capture_batches c WHERE NOT EXISTS "
                "(SELECT 1 FROM processing_receipts p WHERE p.batch_id=c.batch_id)"),
            "unread_core_sources": scalar("SELECT COUNT(DISTINCT m.source_version_id) FROM capture_batch_members m "
                "WHERE m.core=1 AND NOT EXISTS (SELECT 1 FROM source_dispositions d WHERE d.source_version_id=m.source_version_id)"),
            "unresolved_observations": scalar("SELECT COUNT(*) FROM observations WHERE resolution_status='unresolved'"),
        }
        jobs = {state: 0 for state in _JOB_STATES}
        jobs["invalid"] = 0
        for row in conn.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state"):
            jobs[row[0] if row[0] in jobs else "invalid"] += int(row[1])
        jobs["expired_leases"] = int(conn.execute("SELECT COUNT(*) FROM jobs WHERE state='running' AND lease_until<=?", (now,)).fetchone()[0])
        jobs["reader_unknown"] = (scalar("SELECT COUNT(*) FROM reader_text_artifacts WHERE status='unknown'")
                                  if "reader_text_artifacts" in tables else 0)
        jobs["reader_running"] = (scalar("SELECT COUNT(*) FROM reader_text_artifacts WHERE status='running'")
                                  if "reader_text_artifacts" in tables else 0)
        outbox = {"index": _counts(conn, "index_outbox", ("pending", "done")),
                  "projection": _counts(conn, "projection_outbox", ("pending", "done", "superseded")),
                  "projection_conflicts": (scalar("SELECT COUNT(*) FROM reader_proposals WHERE status='pending'")
                                           if "reader_proposals" in tables else 0)}
        return {"state": "available", "revision": revision, "activation_recorded": activated,
                "raw": raw, "jobs": jobs, "outbox": outbox}, budget_profile


def _checkpoints(paths, generation, protected_root):
    result = {"state": "not_configured", "selected_chats": len(paths), "v3_chats": 0,
              "legacy_chats": 0, "missing_chats": 0, "corrupt_chats": 0, "generation_conflicts": 0,
              "captured_shards": 0, "applied_shards": 0, "lagging_shards": 0, "max_lag_seconds": 0}
    for path in paths:
        try:
            path = _admit_path(path, protected_root)
            snapshot = MonitorStateStore(path).inspect()
        except MonitorStateError:
            result["corrupt_chats"] += 1
            continue
        if not snapshot.existed:
            result["missing_chats"] += 1
            continue
        if snapshot.schema != MONITOR_V3_STATE_SCHEMA:
            result["legacy_chats"] += 1
            continue
        result["v3_chats"] += 1
        if snapshot.data["semantic_generation_id"] != generation:
            result["generation_conflicts"] += 1
            continue
        captured, applied = snapshot.data["captured_cursors"], snapshot.data["source_cursors"]
        result["captured_shards"] += len(captured)
        result["applied_shards"] += len(applied)
        for shard, value in captured.items():
            high = decode_cursor_token(value["cursor_token"])
            low = decode_cursor_token(applied[shard]["cursor_token"])
            result["lagging_shards"] += int(high > low)
            result["max_lag_seconds"] = max(result["max_lag_seconds"], high[0] - low[0])
    if any(result[key] for key in ("generation_conflicts", "corrupt_chats", "missing_chats", "legacy_chats")):
        result["state"] = "blocked"
    elif result["lagging_shards"]:
        result["state"] = "pending"
    elif paths:
        result["state"] = "current"
    return result


def _budget(path, profiles, now, protected_root):
    keys = sorted({key for _, accounts in profiles for key in accounts})
    states = {key: 0 for key in ("ready", "missing", "expired", "blocked", "exhausted")}
    profile_state = next((state for state in ("profile_invalid", "profile_missing")
                          if any(status == state for status, _ in profiles)), "valid" if profiles else "profile_missing")
    result = {"state": profile_state, "ready": False, "required_accounts": len(keys),
              "counts": states, "unknown_requests": 0, "sent_requests": 0,
              "network_requests": 0, "remote_balance_verified": False}
    if profile_state != "valid":
        return result
    if not keys:
        result.update(state="not_required", ready=True)
        return result
    path = _admit_path(path, protected_root)
    if not regular_exists(path):
        states["missing"] = len(keys)
        result["state"] = "missing"
        return result
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    with _readonly(path) as conn:
        for key in keys:
            row = conn.execute("SELECT daily_limit_units,external_usage_units,valid_until,blocked,confirmation_id "
                               "FROM provider_budgets WHERE account_key=? AND utc_day=?", (key, day)).fetchone()
            if row is None:
                previous = conn.execute("SELECT 1 FROM provider_budgets WHERE account_key=? LIMIT 1", (key,)).fetchone()
                states["expired" if previous else "missing"] += 1
                continue
            if (any(type(row[field]) is not int or row[field] < 0 for field in ("daily_limit_units", "external_usage_units"))
                    or type(row["valid_until"]) not in (float, int) or not math.isfinite(row["valid_until"]) or row["blocked"] not in (0, 1)
                    or not isinstance(row["confirmation_id"], str) or not row["confirmation_id"].strip()):
                raise _InspectionError("semantic_health_budget_invalid")
            charged = conn.execute("SELECT COALESCE(SUM(charged_units),0) FROM provider_requests WHERE account_key=? AND utc_day=?", (key, day)).fetchone()[0]
            state = ("expired" if row["valid_until"] <= now else "blocked" if row["blocked"] else
                     "exhausted" if row["daily_limit_units"] <= row["external_usage_units"] + charged else "ready")
            states[state] += 1
        for status, field in (("unknown", "unknown_requests"), ("sent", "sent_requests")):
            result[field] = sum(int(conn.execute("SELECT COUNT(*) FROM provider_requests WHERE account_key=? AND status=?", (key, status)).fetchone()[0]) for key in keys)
    result["state"] = next((state for state in ("blocked", "expired", "missing", "exhausted") if states[state]), "ready")
    result["ready"] = result["state"] == "ready"
    return result


def semantic_health(config, *, checkpoint_paths=(), runtime_root=None, usage_path=None, now=None):
    """Return a versioned path/content/account-free local evidence report.

    Checkpoint paths are supplied by the existing per-chat path owner. Source
    cursor gaps count lagging shards, never infer message counts from sparse IDs.
    Candidate databases have no activation metadata; retained former generations
    are counted separately. A cutover blocked by GenerationRegistry is not opened.
    """
    now = time.time() if now is None else now
    report = {"schema": HEALTH_SCHEMA, "state": "inactive", "active_state": "inactive",
              "candidate_count": 0, "retained_generation_count": 0, "cutover_state": "none",
              "database": {"state": "not_active"}, "checkpoints": {"state": "not_active"},
              "provider_budget": {"state": "not_active", "ready": False, "network_requests": 0,
                                  "remote_balance_verified": False}, "error_codes": []}
    protected = (config or {}).get("db_dir")
    try:
        root = _admit_path(runtime_root or (config or {}).get("semantic_runtime_root") or Path(DATA_DIR) / "semantic", protected)
        registry = GenerationRegistry(root, require_available=bool(runtime_root or (config or {}).get("semantic_runtime_root")))
        journal = read_json(registry.journal_path)
        pin = registry.pin()
        phase = journal.get("phase") if journal else "none"
        report["cutover_state"] = phase if phase in {"none", "activation_committed", "provisional", "run_complete", "rolled_back"} else "blocked"
        budget_profiles = []
        generations = safe_path(root, "generations")
        if generations.exists():
            if not generations.is_dir():
                raise _InspectionError("semantic_health_generation_invalid")
            for child in generations.iterdir():
                if pin and child.name == pin.generation_id:
                    continue
                if not _ID.fullmatch(child.name):
                    raise _InspectionError("semantic_health_generation_invalid")
                database = safe_path(root, f"generations/{child.name}/knowledge.sqlite")
                if not regular_exists(database):
                    continue
                with _readonly(database) as conn:
                    _revision, candidate_profile, activated = _manifest(conn, child.name)
                report["retained_generation_count" if activated else "candidate_count"] += 1
                if not pin and not activated:
                    budget_profiles.append(candidate_profile)
        if pin:
            report["active_state"] = "active"
            report["database"], profile = _database(pin.database, pin.generation_id, now)
            budget_profiles = [profile]
            report["checkpoints"] = _checkpoints(tuple(checkpoint_paths), pin.generation_id, protected)
        elif report["candidate_count"]:
            report["state"] = "candidate"
        if pin or report["candidate_count"]:
            report["provider_budget"] = _budget(usage_path or Path(DATA_DIR) / "provider-usage.sqlite",
                                               budget_profiles, now, protected)
        # A concurrent pointer/journal change invalidates this composite view.
        final_pin = registry.pin()
        if final_pin != pin or read_json(registry.journal_path) != journal:
            raise _InspectionError("semantic_health_changed_during_inspection")
        if pin:
            db, checkpoints, budget = report["database"], report["checkpoints"], report["provider_budget"]
            jobs, outbox = db["jobs"], db["outbox"]
            if not db["activation_recorded"]:
                report["state"] = "blocked"
                report["error_codes"] = ["semantic_health_activation_missing"]
            elif checkpoints["state"] == "blocked" or not budget["ready"] or jobs["failed"] or jobs["invalid"] or outbox["index"]["invalid"] or outbox["projection"]["invalid"]:
                report["state"] = "blocked"
            elif jobs["unknown"] or jobs["reader_unknown"] or budget["unknown_requests"] or budget["sent_requests"]:
                report["state"] = "unknown"
            elif (checkpoints["state"] == "pending" or any(db["raw"][key] for key in
                    ("processing_pending_batches", "unread_core_sources", "unresolved_observations"))
                  or any(jobs[key] for key in ("pending", "running", "accepted", "reader_running"))
                  or outbox["index"]["pending"] or outbox["projection"]["pending"] or outbox["projection_conflicts"]
                  or phase in {"activation_committed", "provisional"}):
                report["state"] = "pending"
            else:
                report["state"] = "healthy"
    except (RebuildError, _InspectionError) as exc:
        report["state"] = "blocked"
        report["error_codes"] = [exc.code]
        if exc.code.startswith(("cutover_", "active_")):
            report["active_state"] = "blocked"
            report["cutover_state"] = "blocked"
        elif report["active_state"] == "active" and report["database"]["state"] == "not_active":
            report["database"]["state"] = "unavailable"
        if (report["database"]["state"] == "available" or report["candidate_count"]) and report["provider_budget"]["state"] == "not_active":
            report["provider_budget"]["state"] = "unavailable"
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        report["state"] = "blocked"
        report["error_codes"] = ["semantic_health_evidence_unavailable"]
        if report["active_state"] == "active" and report["database"]["state"] == "not_active":
            report["database"]["state"] = "unavailable"
        if (report["database"]["state"] == "available" or report["candidate_count"]) and report["provider_budget"]["state"] == "not_active":
            report["provider_budget"]["state"] = "unavailable"
    return report
