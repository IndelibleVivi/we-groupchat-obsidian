"""Private rebuild journal, consistent recovery copies and generation pinning.

No constructor discovers or opens the protected message source. The coordinator
supplies the source only after its explicit invocation grant. A journal is outside
the candidate generation so startup can identify every interrupted cutover.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid

from core.app_runtime import AppInstanceLock, AppAlreadyRunning
from core.platform import LockMode, create_platform_services
from core.semantic.rebuild_payloads import (Payloads, STORAGE_SCHEMA, MAX_JOURNAL,
                                           convert_inline, opened)


SCHEMA = "we-groupchat-obsidian.semantic-rebuild.v1"
ACTIVE_SCHEMA = "we-groupchat-obsidian.semantic-active.v1"
JOURNAL_SCHEMA = "we-groupchat-obsidian.semantic-cutover.v1"
RELOCATION_SCHEMA = "we-groupchat-obsidian.semantic-relocation.v1"
RETIREMENT_SCHEMA = "we-groupchat-obsidian.semantic-relocation-retirement.v1"
_RELOCATION_MARKER = ".wgo-relocation.json"
_ID = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")
_ACTIVATION_STEPS = (
    "intent", "acceptance", "projections", "checkpoints", "active_manifest",
    "consistency", "activation_committed", "provisional",
)
# Config keys whose change alters the effective TEXT or DECISION binding (the
# endpoint, model, provider, thinking parameter, timeout or admission). The
# binding comes from the profile AND config, so a live endpoint/model edit is a
# material change even when the stored profile is identical. A relocation may
# adopt a changed value only for a generation that has never dispatched a job.
_PROVIDER_CONFIG_KEYS = (
    "ai_provider", "ai_model", "ai_base_url", "ollama_url", "ollama_model",
    "monitor_ai_provider", "monitor_ai_model", "monitor_ai_thinking",
    "monitor_ai_timeout_seconds", "custom_thinking_parameter", "custom_streaming",
    "semantic_provider_limits", "semantic_text_output_tokens",
)


class RebuildError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def fingerprint(value):
    """An identity binding used by plans, receipts and compare-and-swap."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RebuildError("rebuild_invalid_identity")
    return value


def safe_path(root, relative):
    """Reject every symlink component, including the explicitly supplied root."""
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise RebuildError("rebuild_path_escape")
    result = root / rel
    for path in (root, *root.parents):
        if path.is_symlink():
            raise RebuildError("rebuild_symlink")
    current = root
    for part in rel.parts:
        current /= part
        if current.is_symlink():
            raise RebuildError("rebuild_symlink")
    return result


def read_regular(path):
    path = Path(path)
    safe_path(path.parent, path.name)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RebuildError("rebuild_not_regular")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def regular_exists(path):
    path = Path(path)
    safe_path(path.parent, path.name)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise RebuildError("rebuild_not_regular")
    return True


def file_fingerprint(path):
    """Bound a frozen file without loading a large database into memory."""
    if not regular_exists(path):
        raise RebuildError("rebuild_file_missing")
    result = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_json(path):
    value = read_regular(path)
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (UnicodeError, ValueError) as exc:
        raise RebuildError("rebuild_corrupt_json") from exc
    if not isinstance(parsed, dict):
        raise RebuildError("rebuild_corrupt_json")
    return parsed


def write_private(path, data):
    path = Path(path)
    safe_path(path.parent, path.name)
    services = create_platform_services()
    services.require("private_storage").ensure_directory(str(path.parent))
    services.require("atomic_publisher").write_bytes(str(path), data)


def write_json(path, value):
    write_private(path, canonical_json(value) + b"\n")


@dataclass(frozen=True)
class GenerationPin:
    generation_id: str
    database: str
    embedding_profile_id: str
    index_generation_id: str
    projection_root: str
    manifest: dict


class GenerationRegistry:
    """One immutable read pin; no independent config/index fallback on failure."""
    def __init__(self, runtime_root, *, require_available=False):
        self.root = Path(runtime_root)
        self.require_available = require_available
        self.active_path = safe_path(self.root, "active.json")
        self.journal_path = safe_path(self.root, "cutover.json")

    def pin(self):
        if self.require_available and not self.root.is_dir():
            raise RebuildError("semantic_runtime_root_unavailable")
        journal = read_json(self.journal_path)
        if journal is not None and journal.get("schema") != JOURNAL_SCHEMA:
            raise RebuildError("cutover_journal_invalid")
        if journal is not None and journal.get("phase") not in {
            "activation_committed", "provisional", "run_complete", "rolled_back",
        }:
            raise RebuildError("cutover_recovery_required")
        active = read_json(self.active_path)
        if active is None:
            if journal is not None and journal.get("phase") != "rolled_back":
                raise RebuildError("active_generation_missing")
            return None
        required = {
            "schema", "generation_id", "database", "embedding_profile_id",
            "index_generation_id", "projection_root", "projection_receipt_id",
            "migration_receipt_ids", "config_revision", "acceptance_receipt_id", "run_id",
        }
        if set(active) != required or active["schema"] != ACTIVE_SCHEMA:
            raise RebuildError("active_manifest_invalid")
        gen = _identifier(active["generation_id"])
        expected = safe_path(self.root, f"generations/{gen}/knowledge.sqlite")
        if str(expected) != active["database"] or not regular_exists(expected):
            raise RebuildError("active_database_binding_invalid")
        if not active["embedding_profile_id"] or not active["index_generation_id"]:
            raise RebuildError("active_index_binding_invalid")
        if not active["migration_receipt_ids"] or not active["projection_receipt_id"]:
            raise RebuildError("active_receipt_binding_invalid")
        if journal is not None and journal.get("phase") != "rolled_back":
            if journal.get("new_manifest") != active:
                raise RebuildError("active_journal_mismatch")
        return GenerationPin(gen, str(expected), active["embedding_profile_id"],
                             active["index_generation_id"], active["projection_root"], active)


class RebuildJournal:
    """Serialized durable run state; errors are sticky, successful retries are evidence."""
    def __init__(self, runtime_root, run_id):
        self.root = Path(runtime_root)
        self.run_id = _identifier(run_id)
        self.run_root = safe_path(self.root, f"runs/{run_id}")
        self.path = safe_path(self.run_root, "run.json")
        self.registry = GenerationRegistry(self.root)
        self.payloads = Payloads(self.run_root, self.run_id)
        self._locked = False

    @contextmanager
    def locked(self):
        services = create_platform_services()
        services.require("private_storage").ensure_directory(str(self.run_root))
        with services.require("locks").acquire(
            str(self.run_root / "run.lock"), mode=LockMode.EXCLUSIVE, blocking=False,
        ):
            self._locked = True
            try:
                yield
            finally:
                self._locked = False

    def create(self, plan):
        if regular_exists(self.path):
            raise RebuildError("run_already_exists")
        if plan.get("run_id") != self.run_id or plan.get("schema") != SCHEMA:
            raise RebuildError("rebuild_plan_invalid")
        plan = self.payloads.compact(plan, ("plan",))
        document = {"schema": STORAGE_SCHEMA, "run_id": self.run_id, "revision": 1,
                    "plan": plan, "plan_fingerprint": fingerprint(plan), "phase": "planned",
                    "steps": {}, "errors": [], "freeze_receipts": [], "runtime": {},
                    "created_at": time.time(), "updated_at": time.time()}
        write_json(self.path, document)
        return document

    def read(self):
        with opened(self.path) as stream:
            size = os.fstat(stream.fileno()).st_size
            raw = stream.read(MAX_JOURNAL + 1) if size <= MAX_JOURNAL else None
        if raw is None:
            if not self._locked:
                raise RebuildError("rebuild_journal_upgrade_required")
            document = convert_inline(self.path, self.payloads)
        else:
            try:
                document = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise RebuildError("rebuild_corrupt_json") from exc
            if isinstance(document, dict) and document.get("schema") == SCHEMA:
                if not self._locked:
                    raise RebuildError("rebuild_journal_upgrade_required")
                document = convert_inline(self.path, self.payloads)
        if (not isinstance(document, dict) or document.get("schema") != STORAGE_SCHEMA
                or document.get("run_id") != self.run_id
                or document.get("plan_fingerprint") != fingerprint(document.get("plan"))):
            raise RebuildError("rebuild_run_invalid")
        self.payloads.check_refs(document)
        return document

    def verification(self, run=None):
        run = self.read() if run is None else run
        from .legacy import history_policy, SOURCE_FIRST_POLICY
        if history_policy(run["plan"]) == SOURCE_FIRST_POLICY:
            # A verifier consumes history inventories one pass at a time. Phase
            # transitions must never expand all historical page/row payloads.
            return self.payloads.get(run["verification"])
        return self.payloads.expand(run["verification"], ("verification",))

    def frozen(self, run=None):
        run = self.read() if run is None else run
        return self.payloads.get(run["freeze_receipts"][-1])

    def update(self, mutator):
        current = self.read()
        before = current["revision"]
        result = mutator(current)
        current = current if result is None else result
        current["revision"] = before + 1
        current["updated_at"] = time.time()
        current = self.payloads.compact(current)
        if len(canonical_json(current)) > MAX_JOURNAL:
            raise RebuildError("rebuild_journal_metadata_limit")
        write_json(self.path, current)
        return current

    def error(self, code, phase):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code):
            code = "rebuild_operation_failed"
        def patch(row):
            entry = {"code": code, "phase": phase}
            if entry not in row["errors"]:
                row["errors"].append(entry)
            row["phase"] = "incomplete"
        return self.update(patch)

    def step(self, name, evidence):
        if not isinstance(evidence, dict) or not evidence.get("receipt_id"):
            raise RebuildError("rebuild_receipt_required")
        evidence = self.payloads.compact(evidence, ("steps", name))
        def patch(row):
            previous = row["steps"].get(name)
            if previous is not None and previous != evidence:
                raise RebuildError("rebuild_receipt_conflict")
            row["steps"][name] = evidence
            row["phase"] = name
        return self.update(patch)

    def begin_cutover(self, manifest, acceptance):
        if acceptance.get("complete") is not True or not acceptance.get("receipt_id"):
            raise RebuildError("rebuild_acceptance_incomplete")
        old = read_json(self.registry.active_path)
        existing = read_json(self.registry.journal_path)
        if existing and existing.get("phase") not in {"run_complete", "rolled_back"}:
            if existing.get("run_id") != self.run_id or existing.get("new_manifest") != manifest:
                raise RebuildError("cutover_owned_by_another_run")
            return existing
        row = {"schema": JOURNAL_SCHEMA, "run_id": self.run_id, "phase": "intent",
               "old_manifest": old, "new_manifest": manifest,
               "acceptance": acceptance, "steps": {"intent": {"receipt_id": str(uuid.uuid4())}}}
        write_json(self.registry.journal_path, row)
        return row

    def cutover_step(self, name, evidence):
        row = read_json(self.registry.journal_path)
        if not row or row.get("run_id") != self.run_id or name not in _ACTIVATION_STEPS:
            raise RebuildError("cutover_journal_invalid")
        index = _ACTIVATION_STEPS.index(name)
        if index and _ACTIVATION_STEPS[index - 1] not in row["steps"]:
            raise RebuildError("cutover_step_out_of_order")
        if not isinstance(evidence, dict) or not evidence.get("receipt_id"):
            raise RebuildError("cutover_receipt_required")
        old = row["steps"].get(name)
        if old is not None and old != evidence:
            raise RebuildError("cutover_receipt_conflict")
        row["steps"][name] = evidence
        row["phase"] = name
        write_json(self.registry.journal_path, row)
        return row

    def activate_pointer(self):
        row = read_json(self.registry.journal_path)
        if not row or row.get("run_id") != self.run_id or "checkpoints" not in row["steps"]:
            raise RebuildError("cutover_checkpoints_required")
        active = read_json(self.registry.active_path)
        if active not in (row["old_manifest"], row["new_manifest"]):
            raise RebuildError("active_generation_changed")
        write_json(self.registry.active_path, row["new_manifest"])
        return {"receipt_id": fingerprint(row["new_manifest"]), "manifest": row["new_manifest"]}

    def finalize(self, *, restored, live_check):
        run = self.read()
        cutover = read_json(self.registry.journal_path)
        sticky = any(x["code"] == "provisional_receipt_failed" for x in run["errors"])
        complete = bool(not sticky and restored is True and live_check.get("complete") is True
                        and cutover and cutover.get("phase") in {"provisional", "run_complete"})
        receipt = {"receipt_id": self.run_id, "state": "run_complete" if complete else "incomplete",
                   "restored": restored, "live_check": live_check,
                   "provisional_failure": sticky, "generation_id": run["plan"]["generation_id"]}
        write_json(self.run_root / "receipt.json", receipt)
        self.update(lambda row: row.update(phase=receipt["state"], final_receipt=receipt))
        if complete:
            cutover["phase"] = "run_complete"
            write_json(self.registry.journal_path, cutover)
        return receipt


def backup_sqlite(source, destination):
    """Include WAL commits; target is private before SQLite writes its first page."""
    source, destination = Path(source), Path(destination)
    if not regular_exists(source):
        raise RebuildError("backup_database_missing")
    if destination.exists():
        raise RebuildError("backup_target_exists")
    write_private(destination, b"")
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RebuildError("backup_integrity_failed")
    finally:
        dst.close()
        src.close()
    return {"path": str(destination), "sha256": file_fingerprint(destination)}


def backup_files(entries, destination):
    """Copy only an explicit frozen ownership list; unknown files are never moved."""
    receipts = []
    for index, entry in enumerate(entries):
        path = Path(entry["path"])
        payload = read_regular(path)
        if payload is None:
            raise RebuildError("backup_file_disappeared")
        actual = hashlib.sha256(payload).hexdigest()
        if entry.get("sha256") and entry["sha256"] != actual:
            raise RebuildError("backup_file_changed")
        target = safe_path(destination, f"{index:08d}.bin")
        write_private(target, payload)
        receipts.append({"path": str(path), "backup": str(target), "sha256": actual,
                         "size": len(payload), "role": entry.get("role", "projection")})
    write_json(safe_path(destination, "manifest.json"), {"files": receipts})
    return receipts


def copy_regular(source, destination):
    """Stream one regular file into a secured destination, then replace atomically.

    Used for relocation of large immutable evidence (payloads, inline backups)
    without loading whole documents into memory. The temporary file is created
    mode-0600 before any payload byte and the target is never removed first.
    """
    source, destination = Path(source), Path(destination)
    if not regular_exists(source):
        raise RebuildError("rebuild_relocation_source_invalid")
    safe_path(destination.parent, destination.name)
    services = create_platform_services()
    services.require("private_storage").ensure_directory(str(destination.parent))
    fd, temporary = tempfile.mkstemp(prefix=".pending-copy-", dir=str(destination.parent))
    digest = hashlib.sha256()
    length = 0
    try:
        with open(source, "rb") as handle, os.fdopen(fd, "wb") as output:
            fd = -1
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                output.write(chunk)
                digest.update(chunk)
                length += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return digest.hexdigest(), length


class ManagedRuntime:
    """Exact managed LaunchAgent; no process-name kill or implicit different app."""
    def __init__(self, project_dir, *, runner=subprocess.run, lock_factory=AppInstanceLock,
                 sleep=time.sleep, clock=time.monotonic):
        self.project_dir, self.runner = project_dir, runner
        self.lock_factory, self.sleep, self.clock = lock_factory, sleep, clock
        self.record = self.status = self.lock = None

    def inspect(self):
        from core.launch_agent import launch_agent_report
        self.record, self.status = launch_agent_report(self.project_dir, runner=self.runner)
        if self.record.match_kind != "exact":
            raise RebuildError("runtime_exact_binding_required")
        data = read_regular(self.record.plist_path)
        return {"label": self.record.label, "plist_path": str(self.record.plist_path),
                "plist_sha256": hashlib.sha256(data).hexdigest() if data else "",
                "was_loaded": self.status.loaded, "was_running": self.status.running,
                "program_arguments": list(self.record.program_arguments),
                "working_directory": self.record.working_directory}

    def stop_and_lock(self, observation):
        fresh = self.inspect()
        if fresh != observation:
            raise RebuildError("runtime_changed_before_stop")
        if self.status.loaded:
            result = self.runner(["launchctl", "bootout", f"gui/{os.getuid()}",
                                  str(self.record.plist_path)], capture_output=True, text=True)
            if result.returncode:
                raise RebuildError("runtime_stop_failed")
        deadline = self.clock() + 20
        while True:
            try:
                self.lock = self.lock_factory().acquire()
                return
            except AppAlreadyRunning:
                if self.clock() >= deadline:
                    raise RebuildError("runtime_did_not_stop")
                self.sleep(.2)

    def validate_execution(self, observation):
        arguments = observation.get("program_arguments", [])
        if not arguments or ".app/Contents/MacOS/" not in arguments[0]:
            raise RebuildError("runtime_app_bundle_binding_required")

    def release(self):
        if self.lock:
            self.lock.release()
            self.lock = None

    def restore(self, observation):
        if self.lock is not None:
            raise RebuildError("restore_requires_released_lock")
        if not observation["was_loaded"]:
            return True
        data = read_regular(observation["plist_path"])
        if data is None or hashlib.sha256(data).hexdigest() != observation["plist_sha256"]:
            raise RebuildError("runtime_binding_changed")
        result = self.runner(["launchctl", "bootstrap", f"gui/{os.getuid()}",
                              observation["plist_path"]], capture_output=True, text=True)
        from core.launch_agent import launch_agent_status
        status = launch_agent_status(observation["label"], runner=self.runner)
        if result.returncode and not status.loaded:
            raise RebuildError("runtime_restore_failed")
        if not status.loaded:
            raise RebuildError("runtime_restore_unobserved")
        return True

    def observe_process(self, observation):
        """Bind a live PID/start/executable to the exact managed LaunchAgent."""
        fresh = self.inspect()
        for key in ("label", "plist_path", "plist_sha256", "program_arguments", "working_directory"):
            if fresh[key] != observation[key]:
                raise RebuildError("runtime_binding_changed")
        result = self.runner(["launchctl", "print", f"gui/{os.getuid()}/{observation['label']}"],
                             capture_output=True, text=True)
        match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", result.stdout or "", re.MULTILINE)
        if result.returncode or not match:
            return None
        pid = int(match.group(1))
        self.validate_execution(observation)
        owner_path = self.lock_factory().path
        owner = read_regular(owner_path)
        if owner is None or owner.strip() != str(pid).encode("ascii"):
            return None
        process = self.runner(["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "comm="],
                              capture_output=True, text=True)
        fields = (process.stdout or "").strip().split(None, 5)
        if process.returncode or len(fields) != 6:
            return None
        executable = fields[5]
        if os.path.realpath(executable) != os.path.realpath(observation["program_arguments"][0]):
            raise RebuildError("runtime_process_identity_mismatch")
        return {"pid": pid, "started_at": " ".join(fields[:5]), "executable": executable,
                "label": observation["label"], "plist_sha256": observation["plist_sha256"]}


def _regular_tree(root):
    """Inventory explicit application-owned roots, without following symlinks."""
    root = safe_path(root, "")
    if not root.exists():
        return []
    if not root.is_dir():
        raise RebuildError("rebuild_root_not_directory")
    result = []
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            safe_path(root, Path(directory, name).relative_to(root))
        for name in files:
            path = Path(directory, name)
            if path.relative_to(root).as_posix() == ".wgo/projection.lock":
                continue
            result.append({"path": str(path), "relative": path.relative_to(root).as_posix(),
                           "sha256": file_fingerprint(path), "size": path.stat().st_size})
    return sorted(result, key=lambda row: row["relative"])


def _job_count(path):
    """Count durable jobs without creating or initializing a generation DB.

    A missing or not-yet-created database has zero accepted inventory, so a
    pre-activation relocation may safely refresh the provider binding. Only the
    jobs table matters: unknown outcomes and accepted artifacts are jobs too.
    """
    path = Path(path)
    if not regular_exists(path):
        return 0
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "jobs" not in tables:
            return 0
        return int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])


def _job_state_summary(path, now):
    """Inspect durable job outcomes without creating or mutating the database.

    Unknown provider outcomes and unexpired running claims block before a new
    source capture. Known failed attempts remain retryable through the normal
    fresh freeze/source path and are reported for the operator.
    """
    path = Path(path)
    result = {"states": {}, "blocker": None, "error_codes": []}
    if not regular_exists(path):
        return result
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "jobs" not in tables:
                return result
            states = {row[0]: int(row[1]) for row in conn.execute(
                "SELECT state,COUNT(*) FROM jobs GROUP BY state")}
            invalid = set(states) - {"pending", "running", "failed", "unknown", "accepted", "done"}
            if invalid:
                raise RebuildError("rebuild_job_state_invalid")
            result["states"] = dict(sorted(states.items()))
            running = 0
            if states.get("running"):
                running = int(conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE state='running' "
                    "AND lease_until IS NOT NULL AND lease_until>?", (now,)).fetchone()[0])
            if states.get("unknown") or running:
                result["blocker"] = "semantic_job_busy_or_unknown"
            if "job_attempts" in tables:
                codes = []
                for row in conn.execute(
                        "SELECT error_code FROM job_attempts WHERE error_code IS NOT NULL "
                        "ORDER BY COALESCE(finished_at,started_at) DESC,started_at DESC"):
                    code = row[0]
                    if (isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code)
                            and code not in codes):
                        codes.append(code)
                        if len(codes) == 10:
                            break
                result["error_codes"] = codes
            return result
    except RebuildError:
        raise
    except sqlite3.Error as exc:
        raise RebuildError("rebuild_job_state_unavailable") from exc


def _default_source(config):
    from core.key_extractor import get_cached_keys
    from core.wechat_db import WeChatDB
    keys = get_cached_keys()
    if not keys:
        raise RebuildError("rebuild_source_keys_missing")
    source = WeChatDB.for_runtime(config.get("db_dir"), keys, config=config)
    source.refresh_cache_view()
    return source


@contextmanager
def _default_locks(config, state_stores):
    from core.resource_capture import resource_capture_operation_lock
    from core.resource_backup import MountedResourceBackup
    with ExitStack() as stack:
        stack.enter_context(resource_capture_operation_lock(config))
        backup = MountedResourceBackup(config, target="")
        stack.enter_context(backup._worker_lock())
        stack.enter_context(backup._projection_worker_lock())
        for store in state_stores.values():
            stack.enter_context(store.execution_lock())
        yield


def _admission_snapshot(config):
    """Planned operator ceilings only; no provider or runtime gate is created."""
    from .scheduling import validate_limits
    return {role: {**values, "unset": [key for key in ("requests_per_minute", "tokens_per_minute")
                                     if values[key] is None]}
            for role, values in validate_limits(config.get("semantic_provider_limits")).items()}


def _budget_snapshot(profile, path, now):
    """Read the app-level usage owner without creating a DB or provider client."""
    services = []
    if profile["embedding"] == "cloudflare" or profile["decision"] == "cloudflare":
        services.append("cf-ai:")
    if profile["vector_index"] == "cloudflare":
        services.append("cf-vectorize:")
    if not services:
        return {"ready": True, "accounts": []}
    if not regular_exists(path):
        return {"ready": False, "code": "external_budget_unconfirmed", "accounts": []}
    account = hashlib.sha256(profile["account_id"].lower().encode("ascii")).hexdigest()
    keys = [service + account for service in services]
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    results = []
    with closing(sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        try:
            for key in keys:
                row = conn.execute("SELECT * FROM provider_budgets WHERE account_key=? AND utc_day=?", (key, day)).fetchone()
                if row is None or row["valid_until"] <= now or row["blocked"]:
                    results.append({"ready": False, "code": "external_budget_unconfirmed"})
                    continue
                used = conn.execute("SELECT COALESCE(SUM(charged_units),0) FROM provider_requests WHERE account_key=? AND utc_day=?",
                                    (key, day)).fetchone()[0]
                remaining = max(0, row["daily_limit_units"] - row["external_usage_units"] - used)
                results.append({"ready": remaining > 0, "remaining_units": remaining, "valid_until": row["valid_until"]})
        except sqlite3.Error:
            return {"ready": False, "code": "provider_usage_unavailable", "accounts": []}
    return {"ready": all(row["ready"] for row in results), "accounts": results}


class RebuildCoordinator:
    """The one plan/execute/resume/verify/rollback orchestration path.

    Dependencies are lazy. Default execution uses the same source, store,
    providers, checkpoints and publisher as the app. Injection is for synthetic
    tests; it does not bypass the independent staged acceptance verifier.
    """
    def __init__(self, runtime_root, config_store=None, *, project_dir=None,
                 source_factory=None, roles_factory=None, managed_runtime=None,
                 state_paths=None, runtime_factory=None, lock_context=None,
                 usage_path=None, review_queue_dir=None, clock=time.time, fault_hook=None):
        from core.config import ConfigStore, DATA_DIR
        # Keep construction metadata-free. Inspection first compares lexical
        # configured roots with the protected source, before checking symlinks.
        self.root = Path(os.path.abspath(os.path.expanduser(os.fspath(runtime_root))))
        self.config_store = config_store or ConfigStore()
        self.project_dir = Path(project_dir or Path(__file__).resolve().parents[2])
        self.source_factory = source_factory or _default_source
        self.roles_factory, self.runtime_factory = roles_factory, runtime_factory
        self.managed = managed_runtime or ManagedRuntime(self.project_dir)
        self.state_paths = state_paths
        self.lock_context = lock_context or _default_locks
        self.usage_path = Path(usage_path) if usage_path else Path(DATA_DIR) / "provider-usage.sqlite"
        self.review_queue_dir = Path(review_queue_dir or Path(DATA_DIR) / "review_queue")
        self.clock, self.fault_hook = clock, fault_hook or (lambda _phase: None)

    def _config(self):
        value = self.config_store.inspect()
        if value is None:
            raise RebuildError("rebuild_config_missing")
        return value

    def _admit_private_paths(self, config):
        """Reject source overlap lexically, without stat/open of that root."""
        from core.config import DATA_DIR
        from core.resource_capture import resource_capture_db_path
        def lexical(value):
            return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))
        paths = [self.root, self.usage_path, self.review_queue_dir,
                 config.get("monitor_knowledge_db"), config.get("monitor_obsidian_root"),
                 config.get("attachment_archive_root") or Path(DATA_DIR) / "attachment_archive",
                 resource_capture_db_path(config), *(self.state_paths or {}).values()]
        if config.get("db_dir"):
            source = lexical(config["db_dir"])
            for path in filter(None, paths):
                candidate = lexical(path)
                if candidate == source or candidate.is_relative_to(source) or source.is_relative_to(candidate):
                    raise RebuildError("rebuild_private_source_overlap")
        safe_path(self.root, "")

    def _states(self, config):
        from core.config import active_monitor_chats
        from core.monitor import state_file_for_chat
        from core.monitor_state import MonitorStateStore
        chats = active_monitor_chats(config)
        if not chats:
            raise RebuildError("rebuild_no_selected_chats")
        paths = self.state_paths or {chat["username"]: state_file_for_chat(chat["username"]) for chat in chats}
        if set(paths) != {chat["username"] for chat in chats}:
            raise RebuildError("rebuild_checkpoint_scope_mismatch")
        return {chat: MonitorStateStore(safe_path(Path(path).parent, Path(path).name)) for chat, path in paths.items()}

    def _locations(self, config, run_id, generation):
        from core.config import DATA_DIR
        configured_runtime = safe_path(config.get("semantic_runtime_root") or Path(DATA_DIR) / "semantic", "")
        if configured_runtime != self.root:
            raise RebuildError("rebuild_runtime_root_binding_mismatch")
        vault = config.get("monitor_obsidian_root")
        if not vault or not config.get("monitor_knowledge_db"):
            raise RebuildError("rebuild_explicit_roots_required")
        vault = safe_path(vault, "")
        if not vault.is_dir():
            raise RebuildError("rebuild_vault_missing")
        fixed = safe_path(vault, "WGO")
        if self.root == vault or self.root.is_relative_to(fixed) or fixed.is_relative_to(self.root):
            raise RebuildError("rebuild_root_overlap")
        return {"database": str(safe_path(self.root, f"generations/{generation}/knowledge.sqlite")),
                "staging_root": str(safe_path(self.root, f"runs/{run_id}/staging/WGO")),
                "projection_root": str(fixed)}

    def plan(self, provider_profile, *, run_id=None, expires_in=86400):
        from core.semantic.runtime import validate_profile
        from core.semantic.legacy import SOURCE_FIRST_POLICY
        from core.config import DATA_DIR
        config = self._config()
        self._admit_private_paths(config)
        if GenerationRegistry(self.root).pin() is not None:
            raise RebuildError("rebuild_initial_migration_only")
        profile = validate_profile(provider_profile)
        run_id, generation = _identifier(run_id or uuid.uuid4().hex), uuid.uuid4().hex
        states = self._states(config)
        locations = self._locations(config, run_id, generation)
        inventory = self._legacy_inventory(config)
        snapshots = {chat: asdict(store.inspect()) for chat, store in states.items()}
        runtime = self.managed.inspect()
        existing = _regular_tree(locations["projection_root"])
        from core.resource_capture import resource_capture_db_path
        archive = safe_path(config.get("attachment_archive_root") or Path(DATA_DIR) / "attachment_archive", "")
        resource_files = _regular_tree(archive)
        resource_db = resource_capture_db_path(config)
        resource_bytes = Path(resource_db).stat().st_size if regular_exists(resource_db) else 0
        required_bytes = sum(f.byte_length for f in inventory.files) * 3 + sum(f["size"] for f in existing) * 3
        required_bytes += Path(inventory.db_path).stat().st_size * 4 + 16 * 1024 * 1024
        required_bytes += sum(entry["byte_length"] for entry in inventory.external_files) * 3
        required_bytes += resource_bytes * 2 + sum(entry["size"] for entry in resource_files) * 2
        parent = self.root
        while not parent.exists():
            parent = parent.parent
        free = shutil.disk_usage(parent).free
        if free < required_bytes:
            raise RebuildError("rebuild_storage_insufficient")
        if type(expires_in) not in (int, float) or not 0 < expires_in <= 7 * 86400:
            raise RebuildError("rebuild_plan_expiry_invalid")
        plan = {"schema": SCHEMA, "run_id": run_id, "generation_id": generation,
                "history_policy": SOURCE_FIRST_POLICY,
                "created_at": self.clock(), "expires_at": self.clock() + expires_in,
                "config": config, "config_fingerprint": fingerprint(config),
                "config_revision": config["config_revision"], "provider_profile": profile,
                "provider_budget": _budget_snapshot(profile, self.usage_path, self.clock()),
                "configured_admission": _admission_snapshot(config),
                "runtime": runtime, "chat_ids": list(states), "checkpoints": snapshots,
                "state_paths": {chat: store.path for chat, store in states.items()},
                "legacy_inventory": inventory.to_dict(), "existing_projections": existing,
                "scope": {"selected_chats": len(states), "legacy_records": len(inventory.records),
                          "legacy_files": len(inventory.files), "legacy_text_characters": sum(len(r.text) for r in inventory.records),
                          "review_queue_records": sum(r.table == "review_queue" for r in inventory.records),
                          "resource_files": len(resource_files), "source_message_count": None},
                "review_queue_root": str(self.review_queue_dir),
                "storage": {"required_bytes": required_bytes, "free_bytes": free}, **locations}
        journal = RebuildJournal(self.root, run_id)
        with journal.locked():
            journal.create(plan)
        return plan

    def _legacy_inventory(self, config):
        from core.semantic.legacy import plan_legacy, include_review_queue
        return include_review_queue(plan_legacy(config), self.review_queue_dir)

    def status(self, run_id):
        from .legacy import history_policy
        self._admit_private_paths(self._config())
        run = RebuildJournal(self.root, run_id).read()
        state = ("rolled_back" if run["rollback"].get("restored") is True else "incomplete") if run.get("rollback") else run.get("final_receipt", {}).get("state", "incomplete")
        cutover = read_json(GenerationRegistry(self.root).journal_path)
        own_cutover = cutover is not None and cutover.get("run_id") == run_id
        phase = run["phase"]
        if own_cutover and cutover.get("phase") in {"rollback_intent", "rolled_back"}:
            phase = cutover["phase"]
            state = "rolled_back" if phase == "rolled_back" and run.get("rollback", {}).get("restored") is True else "incomplete"
        elif state == "run_complete" and (not own_cutover or cutover.get("phase") != "run_complete"):
            state = "incomplete"
        jobs = _job_state_summary(run["plan"]["database"], self.clock())
        runtime = run.get("runtime", {})
        attempts = runtime.get("restore_attempts", [])
        if run.get("rollback"):
            rollback = run["rollback"]
            restored = rollback.get("restored")
            restore_code = "rebuild_rollback_restored" if restored is True else "runtime_restore_pending"
            restore_observed_at = rollback.get("observed_at")
        elif attempts:
            latest = attempts[-1]
            restored = latest.get("restored")
            restore_code = latest.get("code")
            restore_observed_at = latest.get("observed_at")
        else:
            restored = runtime.get("restored")
            restore_code = runtime.get("restore_code")
            restore_observed_at = runtime.get("restored_at")
            if (restored is True and any(e.get("code") == "runtime_restore_failed"
                                         and e.get("phase") == "restore" for e in run["errors"])):
                # Older journals did not record an attempt. A prior successful
                # value cannot establish the latest result after a restore error.
                restored = None
                restore_code = "runtime_restore_observation_required"
        return {"run_id": run_id, "generation_id": run["plan"]["generation_id"],
                "history_policy": history_policy(run["plan"]),
                "phase": phase, "error_codes": [e["code"] for e in run["errors"]],
                "freeze_count": len(run["freeze_receipts"]),
                "relocated": bool((run.get("relocation") or {}).get("complete")),
                "configured_admission": _admission_snapshot(run["plan"]["config"]),
                "state": state, "restored": restored, "restore_code": restore_code,
                "restore_observed_at": restore_observed_at,
                "restore_attempt_count": len(attempts),
                "job_states": jobs["states"], "job_error_codes": jobs["error_codes"],
                "job_blocker": jobs["blocker"]}

    def _event(self, journal, phase, value):
        self.fault_hook("before_" + phase)
        journal.step(phase, value)
        self.fault_hook("after_" + phase)

    def _restore_runtime(self, journal, observation, phase="restore"):
        """Record every restore observation while keeping prior attempts."""
        started = self.clock()
        restored, code = False, None
        try:
            restored = self.managed.restore(observation) is True
            if not restored:
                code = "runtime_restore_unobserved"
        except Exception as exc:
            code = getattr(exc, "code", "runtime_restore_failed")
            if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code) is None:
                code = "runtime_restore_failed"
        observed = self.clock()
        attempt = {"started_at": started, "observed_at": observed, "restored": restored}
        if code:
            attempt["code"] = code
        def patch(row):
            runtime = row["runtime"]
            runtime.setdefault("restore_attempts", []).append(attempt)
            runtime.update(restored=restored, restored_at=started, restore_code=code)
        journal.update(patch)
        if code:
            journal.error(code, phase)
        return restored, code

    def restore_runtime(self, run_id, *, apply=False, allow_runtime_changes=False):
        """Restore the frozen managed app without source, jobs or provider work.

        This observes restoration only; it never advances a run, reconciles a
        provider outcome, activates a generation or supplies live acceptance.
        """
        if apply and allow_runtime_changes is not True:
            raise RebuildError("rebuild_restore_grant_required")
        self._admit_private_paths(self._config())
        journal = RebuildJournal(self.root, run_id)
        run = journal.read()
        result = {"run_id": run_id, "generation_id": run["plan"]["generation_id"],
                  "state": "runtime_restore_planned", "requires_runtime_grant": not apply}
        if not apply:
            return result
        with journal.locked():
            run = journal.read()
            cutover = read_json(journal.registry.journal_path)
            if cutover and cutover.get("phase") not in {
                    "activation_committed", "provisional", "run_complete", "rolled_back"}:
                raise RebuildError("cutover_recovery_required")
            observation = run["plan"]["runtime"]
            fresh = self.managed.inspect()
            for key in ("label", "plist_path", "plist_sha256", "program_arguments", "working_directory"):
                if fresh[key] != observation[key]:
                    raise RebuildError("rebuild_runtime_changed")
            validator = getattr(self.managed, "validate_execution", None)
            if callable(validator):
                validator(observation)
            restored, code = self._restore_runtime(journal, observation)
            return {**result, "state": "runtime_restored" if restored else "incomplete",
                    "requires_runtime_grant": False, "restored": restored, "restore_code": code}

    def _roles(self, config, profile):
        if self.roles_factory:
            return self.roles_factory(config, profile)
        from core.semantic.runtime import ProviderRoles
        return ProviderRoles.from_profile(config, profile, usage_path=str(self.usage_path))

    def _runtime(self, store, roles, root, config):
        if self.runtime_factory:
            return self.runtime_factory(store, roles, root, config=config)
        from core.semantic.runtime import SemanticRuntime
        return SemanticRuntime(store, roles, root, config=config)

    def execute(self, run_id, *, allow_source_read=False, allow_model_processing=False):
        if allow_source_read is not True or allow_model_processing is not True:
            raise RebuildError("rebuild_explicit_grants_required")
        self._admit_private_paths(self._config())
        journal = RebuildJournal(self.root, run_id)
        with journal.locked(), create_platform_services().require("locks").acquire(
                str(self.root / "rebuild.lock"), mode=LockMode.EXCLUSIVE, blocking=False):
            return self._execute_locked(journal)

    def resume(self, run_id, *, allow_source_read=False, allow_model_processing=False):
        return self.execute(run_id, allow_source_read=allow_source_read,
                            allow_model_processing=allow_model_processing)

    def compact_journal(self, run_id):
        """Convert only local run storage; no source/provider/app construction."""
        journal = RebuildJournal(self.root, run_id)
        with journal.locked():
            run = journal.read()
            return {"run_id": run_id, "generation_id": run["plan"]["generation_id"],
                    "state": "journal_ready", "phase": run["phase"]}

    # --- Pre-activation relocation to another private local runtime root ---

    def _configured_root(self, config):
        from core.config import DATA_DIR
        return Path(os.path.abspath(os.path.expanduser(
            os.fspath(config.get("semantic_runtime_root") or Path(DATA_DIR) / "semantic"))))

    def _admit_destination(self, config, destination_root):
        """Reject overlap with the source, vault, legacy or state paths, lexically."""
        from core.config import DATA_DIR
        from core.resource_capture import resource_capture_db_path
        def lexical(value):
            return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))
        destination = lexical(destination_root)
        safe_path(destination, "")
        protected = [self.root, self.usage_path, self.review_queue_dir,
                     config.get("monitor_knowledge_db"), config.get("monitor_obsidian_root"),
                     config.get("attachment_archive_root") or Path(DATA_DIR) / "attachment_archive",
                     resource_capture_db_path(config), *(self.state_paths or {}).values()]
        if config.get("db_dir"):
            protected.append(config["db_dir"])
        for value in protected:
            if not value:
                continue
            other = lexical(value)
            if destination == other or destination.is_relative_to(other) or other.is_relative_to(destination):
                raise RebuildError("rebuild_relocation_path_overlap")
        # Fail closed for an unavailable or untrusted root. An absent path (for
        # example an unmounted volume mount point) must never be fabricated into
        # a substitute tree on the boot volume; the operator creates the exact
        # private destination directory first.
        if not destination.exists():
            raise RebuildError("rebuild_relocation_destination_unavailable")
        if destination.is_symlink() or not destination.is_dir():
            raise RebuildError("rebuild_relocation_destination_invalid")
        return destination

    def _relocation_guard(self, journal, run, destination):
        """Relocation is pre-activation only: no active pin and no live cutover."""
        if run.get("final_receipt", {}).get("state") == "run_complete" or run.get("phase") == "run_complete":
            raise RebuildError("rebuild_relocation_run_complete")
        if read_json(journal.registry.journal_path) is not None:
            raise RebuildError("rebuild_relocation_cutover_in_progress")
        if journal.registry.pin() is not None:
            raise RebuildError("rebuild_relocation_active_generation")

    def _effective_profile(self, plan, provider_profile):
        if provider_profile is None:
            return plan["provider_profile"], False
        from core.semantic.runtime import validate_profile
        try:
            refreshed = validate_profile(provider_profile)
        except Exception as exc:
            raise RebuildError(getattr(exc, "code", "rebuild_relocation_profile_invalid")) from exc
        return refreshed, refreshed != plan["provider_profile"]

    @staticmethod
    def _config_provider_changed(old, new):
        return any(old.get(key) != new.get(key) for key in _PROVIDER_CONFIG_KEYS)

    @staticmethod
    def _relocation_binding_check(old, new):
        """Only the runtime root, revision and rolled-back provider binding may move."""
        for key in ("monitor_knowledge_db", "monitor_obsidian_root", "monitor_obsidian_subdir",
                    "monitor_chats", "db_dir", "wechat_source_identity",
                    "attachment_archive_root", "resource_capture_db"):
            if old.get(key) != new.get(key):
                raise RebuildError("rebuild_relocation_binding_changed")

    def _relocation_inventory(self, run_id, generation):
        """Bound the exact run + generation tree to copy; reject symlinks/non-regulars."""
        roots, total = [], 0
        for relative in (f"runs/{run_id}", f"generations/{generation}"):
            root = safe_path(self.root, relative)
            if root.exists():
                if not root.is_dir():
                    raise RebuildError("rebuild_relocation_source_invalid")
                roots.append((root, relative))
        if not roots or roots[0][1] != f"runs/{run_id}":
            raise RebuildError("rebuild_run_missing")
        entries = []
        for root, prefix in roots:
            for directory, directories, files in os.walk(root, followlinks=False):
                for name in (*directories, *files):
                    safe_path(self.root, Path(directory, name).relative_to(self.root).as_posix())
                for name in files:
                    if name.endswith(".lock") or name.startswith(".pending") or name == _RELOCATION_MARKER:
                        continue
                    if name in {"relocation-receipt.json", "relocation-retired.json"}:
                        continue
                    if name.endswith(("-wal", "-shm")):
                        # Online Backup consolidates committed WAL state; a raw
                        # sidecar beside a consolidated copy would be corrupt.
                        continue
                    path = Path(directory, name)
                    if not regular_exists(path):
                        raise RebuildError("rebuild_relocation_source_invalid")
                    relative = prefix + "/" + path.relative_to(root).as_posix()
                    size = path.stat().st_size
                    entries.append({"source": str(path), "relative": relative, "size": size})
                    total += size
        return entries, total

    def _relocation_prepare_marker(self, destination, run_id, generation, plan):
        """Admit only an existing empty or already-owned destination, then publish intent."""
        if not destination.exists() or destination.is_symlink() or not destination.is_dir():
            # Never create the tree: a missing root may be an unmounted volume.
            raise RebuildError("rebuild_relocation_destination_unavailable")
        data = self._relocation_destination_state(destination, run_id, generation, plan)
        if data is not None:
            return data
        data = {"schema": RELOCATION_SCHEMA, "state": "transferring", "run_id": run_id,
                "generation_id": generation, "original_root": str(self.root),
                "original_plan_fingerprint": fingerprint(plan), "created_at": self.clock()}
        write_json(safe_path(destination, _RELOCATION_MARKER), data)
        return data

    def _relocation_destination_state(self, destination, run_id, generation, plan):
        """Validate the destination before any app stop; never create the tree."""
        marker = destination / _RELOCATION_MARKER
        if regular_exists(marker):
            data = read_json(marker)
            if (data.get("run_id") != run_id or data.get("generation_id") != generation
                    or data.get("original_root") != str(self.root)
                    or data.get("original_plan_fingerprint") != fingerprint(plan)):
                raise RebuildError("rebuild_relocation_destination_foreign")
            return data
        if any(name.name != _RELOCATION_MARKER for name in destination.iterdir()):
            raise RebuildError("rebuild_relocation_destination_not_empty")
        return None

    def _relocation_copy_entry(self, source, destination, relative):
        if not regular_exists(source):
            raise RebuildError("rebuild_relocation_source_invalid")
        create_platform_services().require("private_storage").ensure_directory(str(destination.parent))
        if source.suffix == ".sqlite":
            # Main-file equality cannot prove equality of committed WAL state.
            # Publish a fresh validated backup atomically; a failed retry must
            # leave the previously verified destination intact.
            temporary = safe_path(destination.parent, ".pending-sqlite-" + uuid.uuid4().hex + ".sqlite")
            try:
                digest = backup_sqlite(source, temporary)["sha256"]
                os.replace(temporary, destination)
            finally:
                if regular_exists(temporary):
                    temporary.unlink()
        else:
            digest = file_fingerprint(source)
            if regular_exists(destination) and file_fingerprint(destination) == digest:
                return {"relative": relative, "sha256": digest, "size": destination.stat().st_size,
                        "copied": False}
            digest, _ = copy_regular(source, destination)
        if file_fingerprint(destination) != digest:
            raise RebuildError("rebuild_relocation_copy_mismatch")
        return {"relative": relative, "sha256": digest, "size": destination.stat().st_size,
                "copied": True}

    def _relocation_transfer(self, entries, destination):
        receipts = []
        for entry in entries:
            target = safe_path(destination, entry["relative"])
            receipts.append(self._relocation_copy_entry(Path(entry["source"]), target, entry["relative"]))
            self.fault_hook("after_relocation_copy")
        return receipts

    def _relocation_rebind(self, journal, run, config, destination, profile, provider_changed,
                           job_count, expires_in, receipts, total):
        """Write the operational plan bound to the destination and the durable receipt."""
        plan = run["plan"]
        generation = plan["generation_id"]
        original_plan_fingerprint = run["plan_fingerprint"]
        new_plan = dict(plan)
        new_plan["database"] = str(safe_path(destination, f"generations/{generation}/knowledge.sqlite"))
        new_plan["staging_root"] = str(safe_path(destination, f"runs/{journal.run_id}/staging/WGO"))
        new_plan["config"] = config
        new_plan["config_fingerprint"] = fingerprint(config)
        new_plan["config_revision"] = config["config_revision"]
        new_plan["provider_profile"] = profile
        new_plan["provider_budget"] = _budget_snapshot(profile, self.usage_path, self.clock())
        new_plan["configured_admission"] = _admission_snapshot(config)
        new_plan["expires_at"] = self.clock() + expires_in
        receipt = {"schema": RELOCATION_SCHEMA, "state": "transferred", "complete": False,
                   "run_id": journal.run_id, "generation_id": generation,
                   "original_root": str(self.root), "destination_root": str(destination),
                   "original_plan_fingerprint": original_plan_fingerprint,
                   "job_count": job_count, "provider_refreshed": bool(provider_changed),
                   "file_count": len(receipts), "total_bytes": total, "files": receipts,
                   "created_at": self.clock()}
        destination_journal = RebuildJournal(destination, journal.run_id)
        with destination_journal.locked():
            def mutator(row):
                if row.get("run_id") != journal.run_id or row["plan"]["generation_id"] != generation:
                    raise RebuildError("rebuild_relocation_identity_mismatch")
                row["original_plan"] = destination_journal.payloads.put(row["plan"])
                row.setdefault("original_plan_fingerprint", original_plan_fingerprint)
                row["plan"] = destination_journal.payloads.compact(dict(new_plan), ("plan",))
                row["plan_fingerprint"] = fingerprint(row["plan"])
                row["relocation"] = {key: value for key, value in receipt.items() if key != "files"}
                row["relocation"]["plan_fingerprint"] = row["plan_fingerprint"]
                row["relocation_files"] = destination_journal.payloads.put(receipt["files"])
                return row
            destination_journal.update(mutator)
        receipt["plan_fingerprint"] = destination_journal.read()["plan_fingerprint"]
        receipt["receipt_id"] = fingerprint({key: value for key, value in receipt.items()
                                             if key != "receipt_id"})
        with destination_journal.locked():
            destination_journal.update(lambda row: row["relocation"].update(
                receipt_id=receipt["receipt_id"], plan_fingerprint=receipt["plan_fingerprint"]))
        return receipt

    def _relocation_retire(self, run_id, destination, receipt):
        """Publish a durable retirement record; final reclamation stays with review."""
        record = {"schema": RETIREMENT_SCHEMA, "run_id": run_id,
                  "generation_id": receipt["generation_id"],
                  "original_root": receipt["original_root"], "destination_root": str(destination),
                  "destination_receipt_id": receipt["receipt_id"],
                  "original_plan_fingerprint": receipt["original_plan_fingerprint"],
                  "state": "retired_by_relocation", "reclaim": "operator_review_required",
                  "created_at": self.clock()}
        destination_run = safe_path(destination, f"runs/{run_id}")
        write_json(safe_path(destination_run, "relocation-retired.json"), record)
        original = Path(receipt["original_root"]) / "runs" / run_id
        if original.is_dir() and original != destination_run:
            try:
                write_json(safe_path(original, "relocation-retired.json"), record)
            except RebuildError:
                pass
        return record

    def _relocation_adopt_config(self, destination, plan, job_count, provider_changed):
        """Adopt the runtime root through the locked ConfigStore writer only."""
        from core.config import DATA_DIR
        target = str(destination)
        def adopt(working):
            current = Path(os.path.abspath(os.path.expanduser(os.fspath(
                working.get("semantic_runtime_root") or Path(DATA_DIR) / "semantic"))))
            if current not in (self.root, destination):
                raise RebuildError("rebuild_relocation_config_conflict")
            if job_count and (provider_changed or self._config_provider_changed(plan["config"], working)):
                raise RebuildError("rebuild_relocation_provider_changed")
            self._relocation_binding_check(plan["config"], working)
            working["semantic_runtime_root"] = target
        return self.config_store.update(adopt)

    def _relocation_complete(self, journal, receipt, adopted_config):
        """Finalize the relocated plan; the caller holds ``journal.locked()``."""
        final = {}
        def mutator(row):
            plan = dict(row["plan"])
            plan["config"] = adopted_config
            plan["config_fingerprint"] = fingerprint(adopted_config)
            plan["config_revision"] = adopted_config["config_revision"]
            plan["provider_budget"] = _budget_snapshot(plan["provider_profile"], self.usage_path, self.clock())
            plan["configured_admission"] = _admission_snapshot(adopted_config)
            row["plan"] = journal.payloads.compact(plan, ("plan",))
            row["plan_fingerprint"] = fingerprint(row["plan"])
            row["relocation"] = {key: value for key, value in receipt.items() if key != "files"}
            row["relocation"].update(state="relocated", complete=True,
                                     config_revision=adopted_config["config_revision"],
                                     plan_fingerprint=row["plan_fingerprint"],
                                     completed_at=self.clock())
            final["plan_fingerprint"] = row["plan_fingerprint"]
            final["relocation"] = row["relocation"]
            return row
        journal.update(mutator)
        receipt = dict(receipt, **final.get("relocation", {}))
        write_json(safe_path(journal.run_root, "relocation-receipt.json"),
                   {key: value for key, value in receipt.items() if key != "files"})
        return receipt

    def relocate(self, run_id, *, destination_root, apply=False, allow_runtime_changes=False,
                 provider_profile=None, expires_in=86400):
        """Move a pre-activation interrupted run to another private local root.

        Plan is read-only and source/provider-free. Apply requires the explicit
        runtime grant; it copies durable run/generation state with bounded reads,
        adopts the new runtime root through the locked ConfigStore writer and
        publishes a durable relocation plus a retirement record for the retained
        original root. It never reads the WeChat source or calls a provider.
        """
        config = self._config()
        self._admit_private_paths(config)
        if apply and allow_runtime_changes is not True:
            raise RebuildError("rebuild_relocation_grant_required")
        if self._configured_root(config) != self.root:
            raise RebuildError("rebuild_runtime_root_binding_mismatch")
        if type(expires_in) not in (int, float) or not 0 < expires_in <= 7 * 86400:
            raise RebuildError("rebuild_relocation_expiry_invalid")
        journal = RebuildJournal(self.root, run_id)
        run = journal.read()
        if run.get("plan_fingerprint") != fingerprint(run["plan"]):
            raise RebuildError("rebuild_run_invalid")
        existing = run.get("relocation")
        if existing is not None:
            target = Path(os.path.abspath(os.path.expanduser(os.fspath(destination_root))))
            if target != self.root or existing.get("destination_root") != str(self.root):
                raise RebuildError("rebuild_relocation_already_moved")
            if not apply:
                return self._relocation_result(run, planned=existing.get("complete") is not True)
            with journal.locked(), create_platform_services().require("locks").acquire(
                    str(self.root / "rebuild.lock"), mode=LockMode.EXCLUSIVE, blocking=False):
                return self._relocate_resume_in_place(journal, journal.read(), config)
        destination = self._admit_destination(config, destination_root)
        if destination == self.root:
            raise RebuildError("rebuild_relocation_same_root")
        self._relocation_guard(journal, run, destination)
        self._relocation_destination_state(destination, journal.run_id,
                                           run["plan"]["generation_id"], run["plan"])
        profile, provider_changed = self._effective_profile(run["plan"], provider_profile)
        if not apply:
            return self._relocation_preview(journal, run, config, provider_changed)
        with journal.locked(), create_platform_services().require("locks").acquire(
                str(self.root / "rebuild.lock"), mode=LockMode.EXCLUSIVE, blocking=False):
            return self._relocate_locked(journal, journal.read(), config, destination,
                                         profile, provider_changed, expires_in)

    def _relocation_result(self, run, *, planned):
        relocation = run.get("relocation") or {}
        return {"run_id": run["run_id"], "generation_id": run["plan"]["generation_id"],
                "state": "relocation_planned" if planned else "relocated",
                "code": "rebuild_relocation_pending" if planned else "rebuild_relocation_complete",
                "provider_refreshed": bool(relocation.get("provider_refreshed")),
                "job_count": relocation.get("job_count", 0), "requires_runtime_grant": planned}

    def _relocation_preview(self, journal, run, config, provider_changed):
        plan = run["plan"]
        entries, total = self._relocation_inventory(journal.run_id, plan["generation_id"])
        job_count = _job_count(plan["database"])
        material = provider_changed or self._config_provider_changed(plan["config"], config)
        return {"run_id": journal.run_id, "generation_id": plan["generation_id"],
                "state": "relocation_planned", "code": "rebuild_relocation_pending",
                "file_count": len(entries), "total_bytes": total, "job_count": job_count,
                "provider_refreshed": bool(material),
                "provider_change_blocked": bool(material and job_count),
                "requires_runtime_grant": True}

    def _relocate_locked(self, journal, run, config, destination, profile, provider_changed, expires_in):
        plan = run["plan"]
        generation = plan["generation_id"]
        job_count = _job_count(plan["database"])
        provider_changed = provider_changed or self._config_provider_changed(plan["config"], config)
        if provider_changed and job_count:
            raise RebuildError("rebuild_relocation_provider_changed")
        self._relocation_binding_check(plan["config"], config)
        observation = self.managed.inspect()
        validator = getattr(self.managed, "validate_execution", None)
        if callable(validator):
            validator(observation)
        for key in ("label", "plist_path", "plist_sha256", "program_arguments", "working_directory"):
            if observation[key] != plan["runtime"][key]:
                raise RebuildError("rebuild_runtime_changed")
        states = self._states(config)
        entries, total = self._relocation_inventory(journal.run_id, generation)
        parent = destination
        while not parent.exists():
            parent = parent.parent
        if shutil.disk_usage(parent).free < total:
            raise RebuildError("rebuild_storage_insufficient")
        stopped, transferred, adopted, failure, receipt = False, False, False, None, None
        lock_stack = ExitStack()
        try:
            self.managed.stop_and_lock(observation)
            stopped = True
            lock_stack.enter_context(self.lock_context(config, states))
            self._relocation_prepare_marker(destination, journal.run_id, generation, plan)
            receipts = self._relocation_transfer(entries, destination)
            transferred = True
            receipt = self._relocation_rebind(journal, run, config, destination, profile,
                                              provider_changed, job_count, expires_in, receipts, total)
            self._relocation_retire(journal.run_id, destination, receipt)
            adopted_config = self._relocation_adopt_config(destination, plan, job_count, provider_changed)
            adopted = True
            destination_journal = RebuildJournal(destination, journal.run_id)
            with destination_journal.locked():
                receipt = self._relocation_complete(destination_journal, receipt, adopted_config)
        except (Exception, KeyboardInterrupt) as exc:
            failure = RebuildError("rebuild_interrupted") if isinstance(exc, KeyboardInterrupt) else exc
            if not transferred:
                code = ("rebuild_interrupted" if isinstance(exc, KeyboardInterrupt)
                        else getattr(exc, "code", "rebuild_relocation_failed"))
                journal.error(code, "relocation")
        finally:
            try:
                lock_stack.close()
            finally:
                self.managed.release()
        restored = False
        if stopped:
            try:
                restored = self.managed.restore(plan["runtime"]) is True
            except Exception:
                restored = False
        if failure is not None:
            return {"run_id": journal.run_id, "generation_id": generation, "state": "incomplete",
                    "code": getattr(failure, "code", "rebuild_relocation_failed"),
                    "restored": restored,
                    # Once the runtime root is adopted a plain execute would run
                    # the copied tree; require an explicit relocate resume first.
                    "recovery_required": bool(adopted),
                    "relocation_receipt": (receipt or {}).get("receipt_id")}
        return {"run_id": journal.run_id, "generation_id": generation, "state": "relocated",
                "code": "rebuild_relocation_complete", "relocation_receipt": receipt["receipt_id"],
                "provider_refreshed": bool(receipt.get("provider_refreshed")),
                "job_count": job_count, "file_count": len(entries), "total_bytes": total,
                "restored": restored}

    def _relocate_resume_in_place(self, journal, run, config):
        relocation = run["relocation"]
        destination = self.root
        plan = run["plan"]
        generation = plan["generation_id"]
        # run.json is intentionally rebound in place; every other copied byte is
        # verified against its recorded digest.
        journal_relative = f"runs/{journal.run_id}/run.json"
        for entry in journal.payloads.get(run["relocation_files"]):
            if entry["relative"] == journal_relative:
                continue
            path = safe_path(destination, entry["relative"])
            if not regular_exists(path) or file_fingerprint(path) != entry["sha256"]:
                raise RebuildError("rebuild_relocation_copy_incomplete")
        observation = self.managed.inspect()
        for key in ("label", "plist_path", "plist_sha256", "program_arguments", "working_directory"):
            if observation[key] != plan["runtime"][key]:
                raise RebuildError("rebuild_runtime_changed")
        states = self._states(config)
        stopped, failure, receipt = False, None, None
        lock_stack = ExitStack()
        try:
            self.managed.stop_and_lock(observation)
            stopped = True
            lock_stack.enter_context(self.lock_context(config, states))
            self._relocation_retire(journal.run_id, destination, relocation)
            adopted_config = self._relocation_adopt_config(destination, plan,
                                                           relocation.get("job_count", 0),
                                                           relocation.get("provider_refreshed", False))
            receipt = self._relocation_complete(journal, relocation, adopted_config)
        except (Exception, KeyboardInterrupt) as exc:
            failure = RebuildError("rebuild_interrupted") if isinstance(exc, KeyboardInterrupt) else exc
        finally:
            try:
                lock_stack.close()
            finally:
                self.managed.release()
        restored = False
        if stopped:
            try:
                restored = self.managed.restore(plan["runtime"]) is True
            except Exception:
                restored = False
        if failure is not None:
            return {"run_id": journal.run_id, "generation_id": generation, "state": "incomplete",
                    "code": getattr(failure, "code", "rebuild_relocation_failed"),
                    "restored": restored, "recovery_required": True}
        return {"run_id": journal.run_id, "generation_id": generation, "state": "relocated",
                "code": "rebuild_relocation_complete", "relocation_receipt": receipt["receipt_id"],
                "provider_refreshed": bool(receipt.get("provider_refreshed")),
                "job_count": relocation.get("job_count", 0), "restored": restored}

    def _execute_locked(self, journal):
        from core.semantic.store import SemanticStore
        from core.semantic.legacy import history_policy, SOURCE_FIRST_POLICY
        run, config = journal.read(), self._config()
        plan = run["plan"]
        relocation = run.get("relocation")
        if relocation is not None and relocation.get("complete") is not True:
            raise RebuildError("rebuild_relocation_recovery_required")
        cutover = read_json(journal.registry.journal_path)
        own_cutover = cutover and cutover.get("run_id") == journal.run_id
        if cutover and not own_cutover and cutover.get("phase") not in {"run_complete", "rolled_back"}:
            raise RebuildError("cutover_owned_by_another_run")
        if own_cutover and cutover["phase"] == "rolled_back":
            raise RebuildError("rebuild_run_rolled_back")
        if own_cutover and cutover["phase"] == "rollback_intent":
            raise RebuildError("rebuild_rollback_recovery_required")
        if run["phase"] == "run_complete" and run.get("final_receipt", {}).get("state") == "run_complete":
            if not own_cutover or cutover.get("phase") != "run_complete":
                raise RebuildError("cutover_recovery_required")
            journal.registry.pin()
            return run["final_receipt"]
        if fingerprint(config) != plan["config_fingerprint"]:
            raise RebuildError("rebuild_config_changed")
        if own_cutover and cutover["phase"] in {"activation_committed", "provisional", "run_complete"}:
            return self._resume_finalization(journal, config)
        if not own_cutover and journal.registry.pin() is not None:
            raise RebuildError("rebuild_initial_migration_only")
        if self.clock() > plan["expires_at"] and not own_cutover:
            raise RebuildError("rebuild_plan_expired")
        jobs = _job_state_summary(plan["database"], self.clock())
        if jobs["blocker"] is not None:
            restored, _ = self._restore_runtime(journal, plan["runtime"])
            return {"run_id": journal.run_id, "generation_id": plan["generation_id"],
                    "state": "incomplete", "code": jobs["blocker"],
                    "restored": restored, "recovery_required": True,
                    "job_states": jobs["states"], "job_error_codes": jobs["error_codes"],
                    "job_blocker": jobs["blocker"]}
        roles = self._roles(config, plan["provider_profile"])
        if roles.preflight().get("ready") is not True:
            raise RebuildError("rebuild_provider_budget_unready")
        self._locations(config, journal.run_id, plan["generation_id"])
        if shutil.disk_usage(self.root).free < plan["storage"]["required_bytes"]:
            raise RebuildError("rebuild_storage_insufficient")
        observation = self.managed.inspect()
        validator = getattr(self.managed, "validate_execution", None)
        if callable(validator):
            validator(observation)
        for key in ("label", "plist_path", "plist_sha256", "program_arguments", "working_directory"):
            if observation[key] != plan["runtime"][key]:
                raise RebuildError("rebuild_runtime_changed")
        states = self._states(config)
        store = SemanticStore(plan["database"], plan["generation_id"])
        with store.transaction() as conn:
            row = json.loads(conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0])
            policy = history_policy(plan)
            if history_policy(row) != policy:
                # Only a pristine, newly created generation can acquire the
                # new policy. Existing old captures/jobs (including unknown)
                # retain their immutable contract and cannot be rebound.
                pristine = all(conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
                    for table in ("source_records", "capture_batches", "jobs", "legacy_mappings", "items"))
                if policy != SOURCE_FIRST_POLICY or "history_policy" in row or not pristine:
                    raise RebuildError("rebuild_history_policy_mismatch")
            if "history_policy" in plan:
                row["history_policy"] = policy
            row["provider_profile"] = plan["provider_profile"]
            conn.execute("UPDATE generation_manifest SET metadata_json=?", (canonical_json(row).decode(),))
        stopped, lock_stack, failure = False, ExitStack(), None
        try:
            before_process = self.managed.observe_process(observation)
            journal.update(lambda r: r["runtime"].update(observation=observation, previous_process=before_process))
            stopped = True
            self.managed.stop_and_lock(observation)
            lock_stack.enter_context(self.lock_context(config, states))
            fixed_publisher = self._runtime(store, roles, plan["projection_root"], config).publisher
            lock_stack.enter_context(fixed_publisher.locked())
            self._fixed_publisher = fixed_publisher
            if own_cutover and cutover["phase"] not in {"rolled_back", "run_complete"}:
                self._finish_cutover(journal, store, roles, config, states)
            else:
                frozen = self._freeze(journal, config, states)
                source = self.source_factory(config)
                try:
                    self._build(journal, store, roles, config, states, frozen, source)
                finally:
                    close = getattr(source, "close", None)
                    if callable(close):
                        close()
                self._finish_cutover(journal, store, roles, config, states)
        except (Exception, KeyboardInterrupt) as exc:
            failure = RebuildError("rebuild_interrupted") if isinstance(exc, KeyboardInterrupt) else exc
            code = "rebuild_interrupted" if isinstance(exc, KeyboardInterrupt) else getattr(exc, "code", "rebuild_operation_failed")
            journal.error(code, journal.read()["phase"])
        finally:
            try:
                lock_stack.close()
            finally:
                self.managed.release()
        cutover = read_json(journal.registry.journal_path)
        safe_restore = not cutover or cutover.get("run_id") != journal.run_id or cutover.get("phase") in {
            "activation_committed", "provisional", "run_complete", "rolled_back"}
        restored = False
        if stopped and safe_restore:
            restored, _ = self._restore_runtime(journal, plan["runtime"])
        if failure is not None:
            return {"run_id": journal.run_id, "state": "incomplete", "code": getattr(failure, "code", "rebuild_operation_failed"),
                    "restored": restored, "recovery_required": not safe_restore}
        return journal.finalize(restored=restored, live_check=self._live_check(journal, states))

    def _freeze(self, journal, config, states):
        """Refresh every old authority after all owners are frozen, on each retry."""
        from core.resource_capture import resource_capture_db_path
        from core.config import DATA_DIR
        if fingerprint(self._config()) != fingerprint(config):
            raise RebuildError("rebuild_config_changed")
        run = journal.read()
        identity = "freeze-" + str(len(run["freeze_receipts"]) + 1)
        root = safe_path(journal.run_root, "backups/" + identity)
        inventory = self._legacy_inventory(config)
        databases = {"knowledge": backup_sqlite(inventory.db_path, root / "knowledge.sqlite")}
        resources = resource_capture_db_path(config)
        if regular_exists(resources):
            databases["resources"] = backup_sqlite(resources, root / "resources.sqlite")
        previous = read_json(journal.registry.active_path)
        if previous:
            raise RebuildError("rebuild_initial_migration_only")
        snapshots = {chat: asdict(store.inspect()) for chat, store in states.items()}
        entries = [{"path": str(Path(inventory.managed_root) / f.relative_path), "sha256": f.sha256,
                    "role": "legacy"} for f in inventory.files]
        fixed_files = _regular_tree(run["plan"]["projection_root"])
        entries += [dict(row, role="projection") for row in fixed_files]
        archive = safe_path(config.get("attachment_archive_root") or Path(DATA_DIR) / "attachment_archive", "")
        entries += [dict(row, role="resource_file") for row in _regular_tree(archive)]
        entries += [dict(row, role="review_queue") for row in _regular_tree(self.review_queue_dir)]
        config_path = getattr(self.config_store, "path", None)
        if config_path and regular_exists(config_path):
            entries.append({"path": str(config_path), "role": "config"})
        entries += [{"path": str(store.path), "role": "checkpoint"} for store in states.values()
                    if regular_exists(store.path)]
        # One exact copy is sufficient when legacy and fixed roots overlap.
        unique = {row["path"]: row for row in entries}
        copies = backup_files(list(unique.values()), root / "files")
        by_path = {row["path"]: row for row in copies}
        backup_paths = {f.relative_path: by_path[str(Path(inventory.managed_root) / f.relative_path)]["backup"]
                        for f in inventory.files if f.owned}
        backup_paths.update({"external:" + entry["path"]: by_path[entry["path"]]["backup"] for entry in inventory.external_files})
        checkpoint_backups = {}
        for chat, state in states.items():
            if str(state.path) in by_path:
                checkpoint_backups[chat] = by_path[str(state.path)]["backup"]
            else:
                # A missing checkpoint means empty legacy state, not lost data.
                empty = root / ("absent-checkpoint-" + hashlib.sha256(chat.encode()).hexdigest() + ".json")
                write_private(empty, b"{}\n")
                checkpoint_backups[chat] = str(empty)
        frozen = {"receipt_id": identity, "created_at": self.clock(), "databases": databases,
                  "files": copies, "legacy_inventory": journal.payloads.put(inventory.to_dict()), "legacy_backups": backup_paths,
                  "checkpoints": snapshots, "checkpoint_backups": checkpoint_backups,
                  "fixed_files": fixed_files, "previous_manifest": previous,
                  "config_fingerprint": fingerprint(config)}
        write_json(root / "freeze.json", frozen)
        journal.update(lambda r: r["freeze_receipts"].append(frozen))
        self._event(journal, identity, frozen)
        return frozen

    def _snapshot(self, journal, capture, label, chat_ids):
        # Write intent first. A crash resumes the exact pass identity; a new
        # successful freeze receives a different label and another full pass.
        run = journal.read()
        attempt = run.get("capture_attempts", {}).get(label)
        if attempt and attempt.get("receipt"):
            return journal.payloads.get(attempt["receipt"])
        identity = attempt["snapshot_id"] if attempt else journal.run_id + "-" + label
        if not attempt:
            journal.update(lambda r: r.setdefault("capture_attempts", {}).update({label: {"snapshot_id": identity}}))
        receipt = capture.snapshot(chat_ids, snapshot_id=identity)
        journal.update(lambda r: r["capture_attempts"][label].update(receipt=receipt))
        self._event(journal, label, receipt)
        return receipt

    def _prepare_staging(self, journal, frozen):
        plan = journal.read()["plan"]
        root = safe_path(plan["staging_root"], "")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        previous = journal.read().get("staging_baseline", {})
        baseline = {}
        for entry in frozen["fixed_files"]:
            relative, original = entry["relative"], read_regular(entry["path"])
            if hashlib.sha256(original).hexdigest() != entry["sha256"]:
                raise RebuildError("rebuild_projection_changed_during_freeze")
            target = safe_path(root, relative)
            current = read_regular(target)
            old_hash = previous.get(relative)
            if current is not None and current != original:
                if old_hash is None or hashlib.sha256(current).hexdigest() != old_hash:
                    raise RebuildError("rebuild_staging_edit_conflict")
            write_private(target, original)
            baseline[relative] = entry["sha256"]
        journal.update(lambda r: r.update(staging_baseline=baseline))

    def _process_all(self, runtime, *, run_id):
        """Every extraction precedes global retrieval and any Item resolver."""
        from core.semantic.contracts import SemanticStoreError
        with closing(runtime.store.connect()) as conn:
            # Only batch identity and capture order drive wave ownership here;
            # the full capture receipt is never needed. Read lightweight batch
            # ids and per-batch core members so a large historical inventory
            # never materializes every receipt or member copy at once.
            batch_ids = [row[0] for row in conn.execute(
                "SELECT batch_id FROM capture_batches ORDER BY captured_at,rowid")]
            # Independent source batches can generate concurrently. Overlapping
            # core versions must first adopt their prior owner's exact artifacts;
            # timing must never choose a different owner or duplicate Observations.
            waves, last_owner = {}, {}
            for batch_id in batch_ids:
                members = [row[0] for row in conn.execute(
                    "SELECT source_version_id FROM capture_batch_members WHERE batch_id=? AND core=1",
                    (batch_id,))]
                wave = 1 + max((last_owner.get(source_id, -1) for source_id in members), default=-1)
                waves.setdefault(wave, []).append(batch_id)
                last_owner.update({source_id: wave for source_id in members})
        def prepare_extraction(batch_id):
            # extract_batch durably accepts and commits its artifact. Resolution
            # reloads one batch below; retaining all returned observations in
            # scheduler.map would make a full historical wave occupy RAM.
            runtime.engine.extract_batch(batch_id)

        for wave in sorted(waves):
            runtime.scheduler.map("text", waves[wave], prepare_extraction)
        index = runtime.index_all()
        if index.get("complete") is not True:
            raise RebuildError(index.get("code", "rebuild_index_pending"))
        pending_batches = []
        for batch_id in batch_ids:
            with closing(runtime.store.connect()) as conn:
                done = conn.execute("SELECT receipt_json FROM processing_receipts WHERE batch_id=?", (batch_id,)).fetchone()
            if done:
                runtime.store.verify_processing_receipt(json.loads(done[0]))
                continue
            extracted = runtime.engine.extract_batch(batch_id)
            for revision in range(1, 4):
                try:
                    result = runtime.engine.resolve_batch(batch_id, extracted=extracted,
                                                          plan_revision=revision)
                except SemanticStoreError as exc:
                    if exc.code == "semantic_stale_read_set" and revision < 3:
                        continue
                    raise
                if result.get("status") == "stale":
                    continue
                if result.get("status") != "complete":
                    pending_batches.append(batch_id)
                break
            else:
                raise RebuildError("rebuild_resolution_stale")
        while pending_batches:
            before_revision = runtime.store.read_revision()
            remaining = []
            for batch_id in pending_batches:
                try:
                    runtime.process(batch_id)
                except SemanticStoreError as exc:
                    if exc.code != "semantic_processing_pending":
                        raise
                    remaining.append(batch_id)
            if len(remaining) == len(pending_batches) and runtime.store.read_revision() == before_revision:
                raise RebuildError("rebuild_semantic_pending")
            pending_batches = remaining
        consolidated = runtime.engine.consolidate(run_id)
        if consolidated.get("status") not in {"complete", "unchanged"}:
            raise RebuildError("rebuild_consolidation_pending")
        views = runtime.engine.reconcile_views(run_id)
        if views.get("status") not in {"complete", "unchanged"}:
            raise RebuildError("rebuild_views_pending")
        index = runtime.index_all()
        if index.get("complete") is not True:
            raise RebuildError(index.get("code", "rebuild_index_pending"))
        runtime.publish_all()
        return {"receipt_id": run_id, "index": index, "consolidation": consolidated, "views": views}

    def _build(self, journal, store, roles, config, states, frozen, source):
        from core.semantic.legacy import (LegacyInventory, import_legacy, record_legacy_backups,
            derive_topic_item_map, record_legacy_resolution, publish_legacy_navigation,
            history_policy, SOURCE_FIRST_POLICY)
        from core.semantic.source import SemanticCapture, MonitorReceiptBridge
        from core.monitor_source import pending_monitor_source_batch, read_monitor_source_batch, verify_pending_monitor_source_batch
        from core.monitor_state import MonitorStateSnapshot
        from core.monitor import TopicMonitor
        self._prepare_staging(journal, frozen)
        runtime = self._runtime(store, roles, journal.read()["plan"]["staging_root"], config)
        policy = history_policy(journal.read()["plan"])
        if policy == SOURCE_FIRST_POLICY:
            self._preserve_history(journal, store, runtime.publisher)
        capture = SemanticCapture(store, source)
        pending = {}
        for chat, state in frozen["checkpoints"].items():
            monitor = TopicMonitor(source, dict(config, monitor_chat_username=chat), state_store=states[chat])
            descriptor = pending_monitor_source_batch(state["data"], monitor._batch_policy_fingerprint())
            if descriptor:
                batch = read_monitor_source_batch(source, chat, state["data"], raw_limit=descriptor["raw_count"])
                verify_pending_monitor_source_batch(descriptor, batch)
                pending[chat] = batch
        # A crash may leave a valid raw prefix without final inventory proof.
        # Retain it explicitly as incomplete; never relabel it a full snapshot
        # or replay changed live bytes into its immutable batch identities.
        for label, attempt in journal.read().get("capture_attempts", {}).items():
            if not attempt.get("receipt"):
                journal.update(lambda r, label=label: r["capture_attempts"][label].update(abandoned=True))
        prefix = frozen["receipt_id"]
        self._snapshot(journal, capture, prefix + "-C0", list(states))
        if policy != SOURCE_FIRST_POLICY:
            inventory = LegacyInventory.from_dict(journal.payloads.get(frozen["legacy_inventory"]))
            imported = import_legacy(store, inventory, allow_preserved_refresh=True)
            record_legacy_backups(store, inventory, frozen["legacy_backups"])
            if imported.batch_ids:
                journal.update(lambda r: r.setdefault("legacy_imports", {}).update({imported.manifest_id: {
                    "receipt_id": imported.manifest_id, "batch_ids": list(imported.batch_ids)}}))
            # The immutable journal payload remains the evidence owner. Do not
            # keep the decoded legacy corpus alive throughout provider work.
            del inventory
        self._event(journal, prefix + "-processed-C0", self._process_all(runtime, run_id=journal.run_id + "-" + prefix + "-C0"))
        final = self._snapshot(journal, capture, prefix + "-C1", list(states))
        self._event(journal, prefix + "-processed-C1", self._process_all(runtime, run_id=journal.run_id + "-" + prefix + "-C1"))
        brief_at = datetime.fromtimestamp(self.clock(), timezone.utc)
        brief = runtime.brief(now=brief_at)
        from core.semantic.projections import provider_binding
        required_brief = {"edition_id": brief.edition_id, "local_day": brief.local_day,
                          "timezone": runtime.briefing.timezone, "as_of": brief_at.isoformat(),
                          "content_revision": brief.revision,
                          "max_input_bytes": runtime.briefing.max_input_bytes,
                          "provider_route": provider_binding(roles.text)}
        journal.update(lambda r: r.update(required_brief=required_brief))
        inventory = LegacyInventory.from_dict(journal.payloads.get(frozen["legacy_inventory"]))
        if policy == SOURCE_FIRST_POLICY:
            publication = journal.payloads.get(journal.read()["history_preservations"][inventory.manifest_id]["publication_plan"])
        else:
            mapping = derive_topic_item_map(store, inventory)
            mapped_ids = {iid for ids in mapping.values() for iid in ids}
            item_paths = {iid: runtime.publisher.resolve(iid) for iid in mapped_ids}
            publication = record_legacy_resolution(store, inventory, mapping, item_paths)
            legacy_receipt = publish_legacy_navigation(store, inventory, publication, runtime.publisher)
            if legacy_receipt["status"] != "published":
                raise RebuildError("rebuild_legacy_publication_pending")
        migrations = {}
        bridge = MonitorReceiptBridge(store)
        batch_ids = [b["batch_id"] for b in final["batches"]]
        for boundary in final["h1"]:
            chat = boundary["chat_id"]
            old = MonitorStateSnapshot(**frozen["checkpoints"][chat])
            new, receipt = bridge.migration(old, boundary, batch_ids, pending_batch=pending.get(chat))
            migrations[chat] = {"state": new, "receipt": receipt, "old_revision": old.revision}
        manifest = self._verification_manifest(journal, store, roles, final, inventory, publication)
        del inventory
        write_json(journal.run_root / "verification.json", journal.payloads.compact(manifest, ("verification",)))
        from core.semantic.verification import RebuildVerifier
        report = RebuildVerifier(store.path, manifest, projection_root=runtime.publisher.root,
            history_payload_loader=journal.payloads.get).verify().to_dict()
        acceptance = {"receipt_id": fingerprint(report), **report}
        journal.update(lambda r: r.update(migrations=migrations, verification=manifest,
                                          publication_plan=publication, acceptance=acceptance,
                                          staged_files=_regular_tree(runtime.publisher.root)))
        self._event(journal, prefix + "-acceptance", acceptance)
        if acceptance.get("complete") is not True:
            raise RebuildError("rebuild_acceptance_incomplete")

    def _preserve_history(self, journal, store, publisher):
        from .legacy import (LegacyInventory, preserve_legacy_history, record_legacy_backups,
                             build_history_publication_plan, publish_legacy_navigation)
        # A failure before source construction can leave a freeze that has never
        # reached preservation. Conserve every retained freeze on the next build,
        # including earlier versions of personal pages and queue append history.
        for reference in journal.read()["freeze_receipts"]:
            frozen = journal.payloads.get(reference)
            database_backup = frozen.get("databases", {}).get("knowledge", {})
            if (not isinstance(database_backup.get("path"), str)
                    or not isinstance(database_backup.get("sha256"), str)
                    or not regular_exists(database_backup["path"])
                    or file_fingerprint(database_backup["path"]) != database_backup["sha256"]):
                raise RebuildError("rebuild_history_database_backup_invalid")
            inventory = LegacyInventory.from_dict(journal.payloads.get(frozen["legacy_inventory"]))
            receipt = preserve_legacy_history(store, inventory, allow_preserved_refresh=True)
            record_legacy_backups(store, inventory, frozen["legacy_backups"])
            publication = build_history_publication_plan(inventory)
            result = publish_legacy_navigation(store, inventory, publication, publisher)
            if result["status"] != "published":
                raise RebuildError("rebuild_legacy_publication_pending")
            declaration = {"receipt_id": receipt["receipt_id"], "legacy_inventory": frozen["legacy_inventory"],
                           "publication_plan": journal.payloads.put(publication)}
            def record(row):
                preservations = row.setdefault("history_preservations", {})
                freeze_references = list(preservations.get(receipt["manifest_id"], {}).get("freeze_receipts", ()))
                if reference not in freeze_references:
                    freeze_references.append(reference)
                preservations[receipt["manifest_id"]] = dict(declaration, freeze_receipts=freeze_references)
                order = row.setdefault("history_preservation_order", [])
                if receipt["manifest_id"] in order:
                    order.remove(receipt["manifest_id"])
                order.append(receipt["manifest_id"])
            journal.update(record)
            del inventory, publication

    def _verification_manifest(self, journal, store, roles, final, inventory, publication):
        from .legacy import history_policy, SOURCE_FIRST_POLICY
        with closing(store.connect()) as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM capture_batches ORDER BY rowid")]
            batches = [{"batch_id": row["batch_id"], "member_version_ids": json.loads(row["receipt_json"])["member_version_ids"]} for row in rows]
        run = journal.read()
        position = {row["batch_id"]: index for index, row in enumerate(rows)}
        passes = [journal.payloads.get(a["receipt"]) for a in run.get("capture_attempts", {}).values() if a.get("receipt")]
        passes.sort(key=lambda value: position[value["batches"][0]["batch_id"]])
        abandoned = []
        for attempt in run.get("capture_attempts", {}).values():
            if not attempt.get("abandoned"):
                continue
            captured = [json.loads(row["receipt_json"]) for row in rows if row["purpose"] == "snapshot:" + attempt["snapshot_id"]]
            if captured:
                abandoned.append({"receipt_id": attempt["snapshot_id"], "generation_id": store.generation_id,
                                  "complete": False, "batches": captured})
        manifest = {"generation_id": store.generation_id, "expected_batches": batches,
                "capture_passes": passes, "abandoned_capture_passes": abandoned,
                "legacy_capture_passes": list(run.get("legacy_imports", {}).values()),
                "source_inventory": final["source_inventory"], "h1": final["h1"],
                "capture_seq": final["capture_seq"],
                "embedding_profile_id": roles.embedding.profile.profile_id,
                "index_generation_id": roles.index.index_identity,
                "rebuild_reading_required": True, "required_brief": run.get("required_brief")}
        if "history_policy" in run["plan"]:
            manifest["history_policy"] = history_policy(run["plan"])
        if history_policy(run["plan"]) == SOURCE_FIRST_POLICY:
            declarations = [run["history_preservations"][identity] for identity in run.get("history_preservation_order", ())]
            if not declarations or declarations[-1]["receipt_id"] != inventory.manifest_id:
                raise RebuildError("rebuild_history_final_binding_mismatch")
            manifest.update(history_preservation_passes=declarations,
                history_freeze_receipts=list(run["freeze_receipts"]),
                legacy_inventory=declarations[-1]["legacy_inventory"],
                publication_plan=declarations[-1]["publication_plan"])
        else:
            manifest.update(legacy_inventory=inventory.to_dict(), publication_plan=publication)
        return manifest

    def _cutover_step(self, journal, name, evidence):
        current = read_json(journal.registry.journal_path)
        if name in current["steps"]:
            return current["steps"][name]
        self.fault_hook("before_cutover_" + name)
        journal.cutover_step(name, evidence)
        self.fault_hook("after_cutover_" + name)
        return evidence

    def _publish_staged(self, journal):
        run = journal.read()
        staged = run["staged_files"]
        originals = {r["relative"]: r["sha256"] for r in journal.frozen(run)["fixed_files"]}
        for entry in staged:
            relative = entry["relative"]
            raw = read_regular(entry["path"])
            if raw is None or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise RebuildError("rebuild_staged_projection_changed")
            old = self._fixed_publisher.read(relative)
            actual = hashlib.sha256(old).hexdigest() if old is not None else None
            if actual == entry["sha256"]:
                continue
            if actual != originals.get(relative):
                raise RebuildError("rebuild_projection_publication_conflict")
            if self._fixed_publisher.atomic_write(relative, raw, expected=actual) is not True:
                raise RebuildError("rebuild_projection_publication_conflict")
        # A concurrent unknown addition is preserved but cannot be mistaken for
        # a candidate-owned file; only listed files participate in this receipt.
        return {"receipt_id": fingerprint(staged), "files": staged}

    def _finish_cutover(self, journal, store, roles, config, states):
        from core.semantic.verification import RebuildVerifier
        from core.monitor_state import monitor_state_fingerprint
        run = journal.read()
        if not run.get("acceptance", {}).get("complete"):
            raise RebuildError("rebuild_acceptance_incomplete")
        plan = run["plan"]
        projection_id = fingerprint(run["staged_files"])
        manifest = {"schema": ACTIVE_SCHEMA, "generation_id": store.generation_id,
                    "database": store.path, "embedding_profile_id": roles.embedding.profile.profile_id,
                    "index_generation_id": roles.index.index_identity, "projection_root": plan["projection_root"],
                    "projection_receipt_id": projection_id,
                    "migration_receipt_ids": [m["receipt"]["receipt_id"] for m in run["migrations"].values()],
                    "config_revision": config["config_revision"], "acceptance_receipt_id": run["acceptance"]["receipt_id"],
                    "run_id": journal.run_id}
        self.fault_hook("before_cutover_intent")
        journal.begin_cutover(manifest, run["acceptance"])
        self.fault_hook("after_cutover_intent")
        self._cutover_step(journal, "acceptance", run["acceptance"])
        self._cutover_step(journal, "projections", self._publish_staged(journal))
        checkpoints = {}
        for chat, migration in run["migrations"].items():
            current = states[chat].inspect()
            if (current.data.get("semantic_generation_id") == store.generation_id and
                    current.data.get("migration_receipt") == migration["receipt"]):
                if current.revision != migration["old_revision"] + 1:
                    raise RebuildError("rebuild_checkpoint_changed_during_cutover")
            else:
                self.fault_hook("before_checkpoint_cas")
                current = states[chat].install_v3(migration["old_revision"], migration["state"], migration["receipt"])
                self.fault_hook("after_checkpoint_cas")
            checkpoints[chat] = {"revision": current.revision, "fingerprint": monitor_state_fingerprint(current)}
        self._cutover_step(journal, "checkpoints", {"receipt_id": fingerprint(checkpoints), "checkpoints": checkpoints})
        self.fault_hook("before_active_pointer")
        active = journal.activate_pointer()
        self.fault_hook("after_active_pointer")
        self._cutover_step(journal, "active_manifest", active)
        report = RebuildVerifier(store.path, journal.verification(run), projection_root=plan["projection_root"],
            history_payload_loader=journal.payloads.get).verify().to_dict()
        if report.get("complete") is not True or fingerprint(self._config()) != plan["config_fingerprint"]:
            raise RebuildError("rebuild_cutover_consistency_failed")
        self._cutover_step(journal, "consistency", {"receipt_id": fingerprint(report), **report})
        with store.transaction() as conn:
            row = json.loads(conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0])
            if "activation" not in row:
                floor = conn.execute("SELECT COALESCE(MAX(change_id),0) FROM change_events").fetchone()[0]
                row["activation"] = {"run_id": journal.run_id, "activated_at": self.clock(),
                                     "delivery_floor_change_id": floor, "h1": journal.payloads.get(run["verification"])["h1"]}
                conn.execute("UPDATE generation_manifest SET metadata_json=?", (canonical_json(row).decode(),))
            activation = row["activation"]
        self._cutover_step(journal, "activation_committed", {"receipt_id": fingerprint(activation), **activation})
        try:
            self._cutover_step(journal, "provisional", {"receipt_id": journal.run_id + "-provisional", "generation_id": store.generation_id})
        except Exception:
            journal.error("provisional_receipt_failed", "provisional")
            raise

    def _live_check(self, journal, states):
        run = journal.read()
        process = self.managed.observe_process(run["plan"]["runtime"])
        if not process or process == run.get("runtime", {}).get("previous_process"):
            return {"complete": False, "code": "rebuild_new_process_unobserved"}
        restored_at = run.get("runtime", {}).get("restored_at")
        if not restored_at or run.get("runtime", {}).get("restored") is not True:
            return {"complete": False, "code": "runtime_restore_unobserved"}
        receipts = []
        with closing(sqlite3.connect(Path(run["plan"]["database"]).as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "semantic_live_observations" not in tables:
                return {"complete": False, "code": "rebuild_live_increment_pending"}
            for chat, state_store in states.items():
                row = conn.execute("SELECT * FROM semantic_live_observations WHERE chat_id=?", (chat,)).fetchone()
                state = state_store.inspect()
                if (row is None or row["generation_id"] != run["plan"]["generation_id"]
                        or row["phase"] != "applied" or row["observed_at"] < restored_at
                        or row["state_revision"] != state.revision
                        or state.data.get("semantic_generation_id") != run["plan"]["generation_id"]
                        or state.revision <= run["migrations"][chat]["old_revision"] + 1):
                    return {"complete": False, "code": "rebuild_live_increment_pending"}
                receipt = json.loads(row["receipt_json"])
                processing = conn.execute("SELECT * FROM processing_receipts WHERE receipt_id=?", (receipt.get("receipt_id"),)).fetchone()
                if (receipt != state.data.get("processing_receipt") or processing is None
                        or receipt.get("state_revision") != state.revision - 1
                        or receipt.get("output_cursors") != state.data.get("source_cursors")
                        or conn.execute("SELECT 1 FROM canonical_transactions WHERE transaction_id=?", (processing["transaction_id"],)).fetchone() is None
                        or conn.execute("SELECT 1 FROM semantic_applied_batches WHERE batch_id=? AND state_revision=?", (processing["batch_id"], state.revision)).fetchone() is None):
                    return {"complete": False, "code": "rebuild_live_receipt_mismatch"}
                receipts.append(receipt)
        return {"complete": True, "receipt_id": fingerprint([process, receipts]), "process": process,
                "chat_count": len(receipts)}

    def _resume_finalization(self, journal, config):
        run = journal.read()
        cutover = read_json(journal.registry.journal_path)
        if cutover["phase"] == "activation_committed":
            try:
                self._cutover_step(journal, "provisional", {"receipt_id": journal.run_id + "-provisional", "generation_id": run["plan"]["generation_id"]})
            except Exception:
                journal.error("provisional_receipt_failed", "provisional")
                raise
        runtime = run.get("runtime", {})
        restored = runtime.get("restored") is True
        # Legacy journals have no per-attempt restore history. A sticky error
        # from a later restore makes an older true value untrustworthy; observe
        # the exact managed runtime again instead of reporting stale success.
        if (restored and not runtime.get("restore_attempts")
                and any(entry.get("phase") == "restore" for entry in run["errors"])):
            restored = False
        if not restored:
            restored, _ = self._restore_runtime(journal, run["plan"]["runtime"])
        return journal.finalize(restored=restored, live_check=self._live_check(journal, self._states(config)))

    def verify(self, run_id):
        """No source, keys, providers or schema initialization on inspection."""
        from core.semantic.verification import RebuildVerifier
        self._admit_private_paths(self._config())
        journal = RebuildJournal(self.root, run_id)
        run = journal.read()
        manifest = journal.verification(run) if run.get("verification") else None
        if manifest is None:
            return {"complete": False, "issues": [{"code": "rebuild_staged_manifest_missing", "category": "source_pending", "count": 1}], "counts": {}}
        cutover = read_json(journal.registry.journal_path)
        published = cutover and cutover.get("run_id") == run_id and "projections" in cutover["steps"]
        root = run["plan"]["projection_root" if published else "staging_root"]
        return RebuildVerifier(run["plan"]["database"], manifest, projection_root=root,
            history_payload_loader=journal.payloads.get).verify().to_dict()

    def rollback(self, run_id, *, apply=False, allow_runtime_changes=False):
        """Plan by default; explicit apply freezes owners and uses the state CAS."""
        if apply and allow_runtime_changes is not True:
            raise RebuildError("rebuild_rollback_grant_required")
        self._admit_private_paths(self._config())
        journal = RebuildJournal(self.root, run_id)
        run = journal.read()
        cutover = read_json(journal.registry.journal_path)
        if not cutover or cutover.get("run_id") != run_id:
            raise RebuildError("rebuild_rollback_cutover_missing")
        if cutover.get("phase") == "rolled_back":
            if run.get("rollback", {}).get("restored") is True:
                return {"run_id": run_id, "state": "rolled_back", "restored": True, "retained_generation": True}
            if not apply:
                return {"run_id": run_id, "state": "rollback_planned", "code": "runtime_restore_pending",
                        "retained_generation": True, "requires_runtime_grant": True}
            with journal.locked(), create_platform_services().require("locks").acquire(
                    str(self.root / "rebuild.lock"), mode=LockMode.EXCLUSIVE, blocking=False):
                return self._finish_rollback_restore(journal)
        result = {"run_id": run_id, "state": "rollback_planned", "retained_generation": True,
                  "checkpoint_count": len(run.get("migrations", {})), "requires_runtime_grant": True}
        if not apply:
            return result
        with journal.locked(), create_platform_services().require("locks").acquire(
                str(self.root / "rebuild.lock"), mode=LockMode.EXCLUSIVE, blocking=False):
            config = self._config()
            if fingerprint(config) != run["plan"]["config_fingerprint"]:
                raise RebuildError("rebuild_config_changed")
            states = self._states(config)
            observation = self.managed.inspect()
            for key in ("label", "plist_path", "plist_sha256", "program_arguments", "working_directory"):
                if observation[key] != run["plan"]["runtime"][key]:
                    raise RebuildError("rebuild_runtime_changed")
            restore_allowed = False
            try:
                self.managed.stop_and_lock(observation)
                with self.lock_context(config, states):
                    services = create_platform_services()
                    lock_path = safe_path(run["plan"]["projection_root"], ".wgo/projection.lock")
                    services.require("private_storage").ensure_directory(str(lock_path.parent))
                    with services.require("locks").acquire(str(lock_path), mode=LockMode.EXCLUSIVE, blocking=False):
                        replay = self._rollback_checkpoints(journal, states)
                        self._rollback_projections(journal)
                        if cutover.get("old_manifest") is None:
                            if regular_exists(journal.registry.active_path):
                                os.replace(journal.registry.active_path, journal.run_root / "retained-active-manifest.json")
                        else:
                            write_json(journal.registry.active_path, cutover["old_manifest"])
                        cutover["phase"] = "rolled_back"
                        cutover["rollback"] = {"receipt_id": replay["receipt_id"], "retained_generation": True}
                        write_json(journal.registry.journal_path, cutover)
                        journal.update(lambda r: r.update(phase="rolled_back", rollback=cutover["rollback"]))
                        restore_allowed = True
            finally:
                self.managed.release()
            return self._finish_rollback_restore(journal) if restore_allowed else {
                "run_id": run_id, "state": "incomplete", "restored": False, "retained_generation": True}

    def _finish_rollback_restore(self, journal):
        run = journal.read()
        if fingerprint(self._config()) != run["plan"]["config_fingerprint"]:
            raise RebuildError("rebuild_config_changed")
        restored, code = False, "runtime_restore_pending"
        try:
            restored = self.managed.restore(run["plan"]["runtime"]) is True
        except Exception as exc:
            code = getattr(exc, "code", "runtime_restore_failed")
            journal.error(code, "rollback_restore")
        journal.update(lambda r: r.setdefault("rollback", {}).update(restored=restored, observed_at=self.clock()))
        if restored:
            journal.update(lambda r: r.update(phase="rolled_back"))
        return {"run_id": journal.run_id, "state": "rolled_back" if restored else "incomplete",
                "code": "rebuild_rollback_restored" if restored else code,
                "restored": restored, "retained_generation": True}

    def _rollback_checkpoints(self, journal, states):
        from core.monitor_state import monitor_state_fingerprint
        run = journal.read()
        frozen = journal.frozen(run)
        path = journal.run_root / "rollback-replay.json"
        replay = read_json(path)
        if replay is None:
            with closing(sqlite3.connect(Path(run["plan"]["database"]).as_uri() + "?mode=ro", uri=True)) as conn:
                captures = [r[0] for r in conn.execute("SELECT receipt_id FROM capture_batches ORDER BY rowid")]
            replay = {"receipt_id": journal.run_id + "-rollback-replay", "run_id": journal.run_id,
                      "retained_database": run["plan"]["database"], "retained_capture_receipt_ids": captures,
                      "h1": journal.payloads.get(run["verification"])["h1"], "checkpoints": {}, "applied": {}}
            for chat, state_store in states.items():
                current = state_store.inspect()
                raw = read_regular(frozen["checkpoint_backups"][chat])
                if raw is None:
                    raise RebuildError("rebuild_rollback_backup_missing")
                # Parse with the sole checkpoint owner's own strict parser.
                backup = state_store._parse_payload(json.loads(raw))
                replay["checkpoints"][chat] = {"current": asdict(current), "backup_path": frozen["checkpoint_backups"][chat],
                    "backup_bytes_digest": hashlib.sha256(raw).hexdigest(), "backup_fingerprint": monitor_state_fingerprint(backup)}
            write_json(path, replay)
        cutover = read_json(journal.registry.journal_path)
        cutover["phase"] = "rollback_intent"
        write_json(journal.registry.journal_path, cutover)
        self.fault_hook("after_rollback_intent")
        journal.update(lambda r: r.update(phase="rollback_intent"))
        for chat, entry in replay["checkpoints"].items():
            current = states[chat].inspect()
            raw = read_regular(entry["backup_path"])
            if raw is None or hashlib.sha256(raw).hexdigest() != entry["backup_bytes_digest"]:
                raise RebuildError("rebuild_rollback_backup_changed")
            backup = states[chat]._parse_payload(json.loads(raw))
            expected = entry["current"]
            if current.data == backup.data and current.revision in {expected["revision"], expected["revision"] + 1}:
                # Exact old state was either never migrated, or this rollback CAS
                # committed immediately before a crash. No second downgrade.
                replay["applied"][chat] = {"revision": current.revision, "fingerprint": monitor_state_fingerprint(current)}
                write_json(path, replay)
                continue
            if asdict(current) != expected:
                raise RebuildError("rebuild_rollback_checkpoint_changed")
            binding = {key: current.data[key] for key in ("semantic_generation_id", "source_namespace", "chat_id",
                       "source_inventory_digest", "source_inventory_revision")}
            receipt = {"schema": "we-groupchat-obsidian.monitor-rollback-receipt.v1", **binding,
                "receipt_id": journal.run_id + ":rollback:" + hashlib.sha256(chat.encode()).hexdigest(),
                "rollback_id": journal.run_id + "-rollback", "run_id": journal.run_id, "journal_id": journal.run_id,
                "state_revision": current.revision, "current_state_fingerprint": monitor_state_fingerprint(current),
                "backup_bytes_digest": entry["backup_bytes_digest"], "backup_state_fingerprint": entry["backup_fingerprint"],
                "retained_raw_store_id": run["plan"]["generation_id"], "retained_raw_receipt_id": replay["receipt_id"],
                "retained_capture_receipt_ids": replay["retained_capture_receipt_ids"],
                "retained_captured_cursors": current.data["captured_cursors"],
                "replay_receipt_id": replay["receipt_id"], "replay_destination_id": "retained-generation:" + run["plan"]["generation_id"],
                "replay_from_cursors": backup.data.get("source_cursors", {}), "replay_through_cursors": current.data["captured_cursors"]}
            write_json(journal.run_root / ("rollback-cas-" + hashlib.sha256(chat.encode()).hexdigest() + ".json"), receipt)
            self.fault_hook("before_rollback_cas")
            restored = states[chat].restore_checkpoint(current.revision, raw, receipt)
            self.fault_hook("after_rollback_cas")
            replay["applied"][chat] = {"revision": restored.revision, "fingerprint": monitor_state_fingerprint(restored)}
            write_json(path, replay)
        return replay

    def _rollback_projections(self, journal):
        run = journal.read()
        root = Path(run["plan"]["projection_root"])
        generated = {row["relative"]: row["sha256"] for row in run["staged_files"]}
        copies = {row["path"]: row for row in journal.frozen(run)["files"]}
        for original in journal.frozen(run)["fixed_files"]:
            current = read_regular(safe_path(root, original["relative"]))
            digest = hashlib.sha256(current).hexdigest() if current is not None else None
            if digest == original["sha256"]:
                continue
            if digest != generated.get(original["relative"]):
                raise RebuildError("rebuild_rollback_personal_edit_conflict")
            recovery = copies[original["path"]]
            raw = read_regular(recovery["backup"])
            if raw is None or hashlib.sha256(raw).hexdigest() != original["sha256"]:
                raise RebuildError("rebuild_rollback_backup_changed")
            write_private(original["path"], raw)
