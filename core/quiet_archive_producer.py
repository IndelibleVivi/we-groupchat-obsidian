"""Explicit private profile and identity-preserving adoption for headless capture.

This module supplies configuration and local storage to the existing owners. It
does not discover an app configuration, recover keys, read monitor state, or own
a second message reader. Adoption plans contain a frozen SQLite/CAS candidate;
apply never follows later changes to the source ledger or app configuration.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import uuid
from urllib.parse import quote

from .resource_capture import SelectedResourceCapture, resource_capture_operation_lock
from .resource_backup import _fsync_dir_best_effort
from .source_inventory import SOURCE_INVENTORY_SCHEMA, SourceInventoryStore


PROFILE_SCHEMA = "we-groupchat-obsidian.quiet-archive.producer.v1"
STATE_SCHEMA = "we-groupchat-obsidian.quiet-archive.producer-state.v1"
ADOPTION_SCHEMA = "we-groupchat-obsidian.quiet-archive.adoption.v1"


class ProducerError(RuntimeError):
    def __init__(self, code, **details):
        super().__init__(code)
        self.code = code
        self.details = details


def _absolute(value):
    if not isinstance(value, str) or not os.path.isabs(value) or "\0" in value:
        raise ProducerError("profile_absolute_path_required")
    return os.path.normpath(value)


def _regular(path, *, private=False):
    value = os.lstat(path)
    if not stat.S_ISREG(value.st_mode):
        raise ProducerError("producer_file_not_regular")
    if private and (value.st_uid != os.getuid() or value.st_mode & 0o077):
        raise ProducerError("producer_file_not_private")


def _read_json(path, *, private=True):
    _regular(path, private=private)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_dir_best_effort(os.path.dirname(path))


def _digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _file_identity(path):
    _regular(path)
    digest = hashlib.sha256()
    size = 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size": size}


def _copy_private(source, destination):
    _regular(source)
    Path(destination).parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        target_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(source_fd, "rb") as src, os.fdopen(target_fd, "wb") as dst:
            source_fd = -1
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        _fsync_dir_best_effort(os.path.dirname(destination))
    finally:
        if source_fd >= 0:
            os.close(source_fd)


def _relative_file(root, relative):
    if not isinstance(relative, str) or not relative or os.path.isabs(relative):
        raise ProducerError("adoption_payload_invalid")
    if any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ProducerError("adoption_payload_invalid")
    path = os.path.join(root, relative)
    if os.path.commonpath((os.path.realpath(root), os.path.realpath(path))) != os.path.realpath(root):
        raise ProducerError("adoption_payload_invalid")
    _regular(path)
    return path


@contextmanager
def _state_lock(root):
    parent = os.path.dirname(root)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = os.open(root + ".producer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProducerError("producer_worker_busy") from exc
        yield
    finally:
        os.close(fd)


class ProducerProfile:
    def __init__(self, path, value):
        self.path = path
        self.value = value

    @classmethod
    def read(cls, path):
        path = _absolute(os.path.abspath(path))
        try:
            raw = _read_json(path)
            if not isinstance(raw, dict) or raw.get("schema") != PROFILE_SCHEMA:
                raise ProducerError("profile_schema_invalid")
            if set(raw) != {"schema", "source", "state_dir", "target", "chats", "budget"}:
                raise ProducerError("profile_fields_invalid")
            if set(raw["source"]) != {"db_dir", "keys_file"}:
                raise ProducerError("profile_source_invalid")
            source = {name: _absolute(raw["source"][name]) for name in ("db_dir", "keys_file")}
            state_dir, target = _absolute(raw["state_dir"]), _absolute(raw["target"])
            roots = (state_dir, target, source["db_dir"])
            for index, left in enumerate(roots):
                for right in roots[index + 1:]:
                    if os.path.commonpath((left, right)) in {left, right}:
                        raise ProducerError("profile_paths_overlap")
            chats = []
            seen = set()
            for row in raw["chats"]:
                if set(row) != {"username", "alias", "selection_id", "selected_since"}:
                    raise ProducerError("profile_selection_invalid")
                username = row["username"]
                if (not isinstance(username, str) or not username.endswith("@chatroom")
                        or username != username.strip()
                        or username in seen or not isinstance(row["alias"], str)
                        or not row["alias"].strip() or not isinstance(row["selection_id"], str)
                        or isinstance(row["selected_since"], bool)
                        or not isinstance(row["selected_since"], int) or row["selected_since"] < 0):
                    raise ProducerError("profile_selection_invalid")
                if row["selection_id"] and str(uuid.UUID(row["selection_id"])) != row["selection_id"]:
                    raise ProducerError("profile_selection_invalid")
                seen.add(username)
                chats.append(dict(row))
            budget = dict(raw["budget"])
            required = {"max_rounds", "max_seconds", "page_size"}
            optional = {"resolve_limit", "min_free_bytes", "max_object_bytes"}
            if not required <= set(budget) or set(budget) - required - optional:
                raise ProducerError("profile_budget_invalid")
            budget.setdefault("resolve_limit", 50)
            budget.setdefault("min_free_bytes", 1024 * 1024 * 1024)
            budget.setdefault("max_object_bytes", 512 * 1024 * 1024)
            for name, number in budget.items():
                minimum = 0 if name == "min_free_bytes" else 1
                if (isinstance(number, bool) or not isinstance(number, (int, float))
                        or not math.isfinite(number) or number < minimum
                        or (name != "max_seconds" and not isinstance(number, int))):
                    raise ProducerError("profile_budget_invalid")
            return cls(path, {"schema": PROFILE_SCHEMA, "source": source, "state_dir": state_dir,
                              "target": target, "chats": chats, "budget": budget})
        except (KeyError, TypeError, ValueError) as exc:
            raise ProducerError("profile_invalid") from exc

    @property
    def state_dir(self):
        return self.value["state_dir"]

    def paths(self, root=None):
        root = root or self.state_dir
        return {"ledger": os.path.join(root, "capture.db"),
                "inventory": os.path.join(root, "source_inventory.json"),
                "cache": os.path.join(root, "cache"), "objects": os.path.join(root, "objects"),
                "marker": os.path.join(root, "producer-state.json"),
                "initialization_lock": root + ".producer.lock",
                "projection_locks": os.path.join(root, "projection-locks")}

    def config(self, root=None):
        paths = self.paths(root)
        chats = self.value["chats"]
        budget = self.value["budget"]
        return {"db_dir": self.value["source"]["db_dir"],
                "resource_capture_db": paths["ledger"], "attachment_archive_root": paths["objects"],
                "resource_projection_lock_dir": paths["projection_locks"],
                # Existing constructors keep these as unused path/boundary fields.
                "monitor_knowledge_db": os.path.join(root or self.state_dir, "unused-knowledge.db"),
                "monitor_obsidian_root": os.path.join(root or self.state_dir, "unused-projection"),
                "monitor_chats": [{"username": row["username"], "name": row["alias"]} for row in chats],
                "resource_backup_selected_chats": chats, "quiet_archive_handoff_enabled": True,
                "quiet_archive_handoff_target": self.value["target"],
                "resource_backup_max_messages_per_scan": budget["page_size"],
                "attachment_archive_max_object_bytes": budget["max_object_bytes"],
                "attachment_archive_min_free_bytes": budget["min_free_bytes"],
                "resource_backup_min_free_bytes": budget["min_free_bytes"]}

    def _reload_config(self):
        current = self.read(self.path)
        if (current.value["source"] != self.value["source"] or current.paths() != self.paths()
                or current.value["target"] != self.value["target"]):
            raise ProducerError("producer_profile_changed")
        return current.config()

    def capture(self, *, source=None):
        return SelectedResourceCapture(self.config(), source=source, capture_contexts=True,
                                       config_loader=self._reload_config)

    def source(self):
        from .key_extractor import read_keys_file
        from .wechat_db import WeChatDB
        keys_file = self.value["source"]["keys_file"]
        _regular(keys_file, private=True)
        keys = read_keys_file(keys_file)
        source = WeChatDB(self.value["source"]["db_dir"], keys,
                          source_inventory_store=SourceInventoryStore(self.paths()["inventory"]),
                          cache_root=self.paths()["cache"])
        expected_namespace = (self.marker(required=True) or {}).get("source_namespace")
        if expected_namespace and source.source_namespace != expected_namespace:
            raise ProducerError("producer_source_namespace_mismatch")
        return source

    def marker(self, *, required=False):
        path = self.paths()["marker"]
        if not os.path.lexists(path):
            if required:
                raise ProducerError("producer_not_initialized")
            return None
        value = _read_json(path)
        if (value.get("schema") != STATE_SCHEMA
                or value.get("source_db_dir") != self.value["source"]["db_dir"]):
            raise ProducerError("producer_state_binding_mismatch")
        _regular(self.paths()["ledger"], private=True)
        conn = sqlite3.connect(f"file:{quote(self.paths()['ledger'])}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM resource_meta WHERE key='archive_id'").fetchone()
            if row is None or row[0] != value.get("archive_id"):
                raise ProducerError("producer_archive_identity_mismatch")
        finally:
            conn.close()
        return value

    def plan(self):
        marker = self.marker()
        return {"schema": PROFILE_SCHEMA, "state": "initialized" if marker else "not_initialized",
                "profile": self.value, "paths": self.paths(),
                "archive_id": marker["archive_id"] if marker else None,
                "source_read": False, "keys_read": False}

    def initialize(self):
        with _state_lock(self.state_dir):
            if os.path.lexists(self.state_dir):
                raise ProducerError("producer_state_exists")
            stage = tempfile.mkdtemp(prefix=".producer-init-", dir=os.path.dirname(self.state_dir))
            try:
                capture = SelectedResourceCapture(self.config(stage), capture_contexts=True)
                capture.initialize_selected_chat_cursors()
                archive_id = capture.archive_id
                _write_json(self.paths(stage)["marker"], {
                    "schema": STATE_SCHEMA, "archive_id": archive_id,
                    "source_db_dir": self.value["source"]["db_dir"], "origin": "new"})
                os.rename(stage, self.state_dir)
                _fsync_dir_best_effort(os.path.dirname(self.state_dir))
                stage = ""
                return {"state": "initialized", "archive_id": archive_id}
            finally:
                if stage:
                    shutil.rmtree(stage)


def _ledger_review(path, profile):
    conn = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        meta = dict(conn.execute("SELECT key, value FROM resource_meta"))
        archive_id = str(uuid.UUID(meta["archive_id"]))
        rows = {row["chat_username"]: dict(row) for row in conn.execute("SELECT * FROM resource_chats")}
        receipt = json.loads(meta.get("context_scan_receipt") or "{}")
        selected_keys = {row["chat_key"] for row in receipt.get("selection", [])}
        required = [{"username": row["chat_username"], "alias": row["chat_alias"],
                     "selection_id": row["selection_id"], "selected_since": row["selected_since"]}
                    for row in rows.values() if "selection" not in receipt or row["chat_key"] in selected_keys]
        expected = {row["username"]: (row["selection_id"], row["selected_since"]) for row in required}
        actual = {row["username"]: (row["selection_id"], row["selected_since"])
                  for row in profile.value["chats"]}
        if actual != expected:
            raise ProducerError("adoption_selection_mismatch", required_chats=required)
        objects = [dict(row) for row in conn.execute(
            "SELECT DISTINCT object_relpath, object_sha256, object_size "
            "FROM resource_occurrences WHERE object_relpath <> ''")]
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("resource_chats", "resource_shards", "resource_occurrences",
                                "resource_contexts", "resource_context_history")}
        evidence = json.loads(meta.get("source_inventory_evidence") or "{}")
        return {"archive_id": archive_id, "selection": required, "counts": counts,
                "source_namespace": evidence.get("source_namespace", "")}, objects
    finally:
        conn.close()


def adoption_plan(profile, *, ledger, objects, inventory=None, output, wait_seconds=0):
    """Freeze only explicitly named local inputs, never app config or source DBs."""
    ledger, objects, output = _absolute(ledger), _absolute(objects), _absolute(output)
    if os.path.lexists(profile.state_dir):
        raise ProducerError("producer_state_exists")
    payload = output + ".payload"
    if os.path.lexists(output) or os.path.lexists(payload):
        raise ProducerError("adoption_plan_exists")
    _regular(ledger)
    for original in (ledger, objects):
        destination = os.path.realpath(profile.state_dir)
        original = os.path.realpath(original)
        if os.path.commonpath((destination, original)) in {destination, original}:
            raise ProducerError("adoption_paths_overlap")
    stage = ""
    try:
        with resource_capture_operation_lock({"resource_capture_db": ledger}, timeout=wait_seconds):
            os.makedirs(os.path.dirname(output), mode=0o700, exist_ok=True)
            stage = tempfile.mkdtemp(prefix=".adoption-", dir=os.path.dirname(output))
            source = sqlite3.connect(f"file:{quote(ledger)}?mode=ro", uri=True)
            destination = sqlite3.connect(os.path.join(stage, "capture.db"))
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            os.chmod(os.path.join(stage, "capture.db"), 0o600)
            review, object_rows = _ledger_review(os.path.join(stage, "capture.db"), profile)
            if review["source_namespace"] and not inventory:
                raise ProducerError("adoption_inventory_required")
            if inventory:
                inventory_value = _read_json(_absolute(inventory))
                SourceInventoryStore._validate_payload(inventory_value)
                if review["source_namespace"] and review["source_namespace"] not in inventory_value["sources"]:
                    raise ProducerError("adoption_inventory_mismatch")
            else:
                inventory_value = {"schema": SOURCE_INVENTORY_SCHEMA, "revision": 0, "sources": {}}
            _write_json(os.path.join(stage, "source_inventory.json"), inventory_value)
            files = {name: _file_identity(os.path.join(stage, name))
                     for name in ("capture.db", "source_inventory.json")}
            for row in object_rows:
                relative = row["object_relpath"]
                source_path = _relative_file(objects, relative)
                target_relative = "objects/" + relative
                if target_relative in files:
                    continue
                target_path = os.path.join(stage, target_relative)
                _copy_private(source_path, target_path)
                identity = _file_identity(target_path)
                if identity != {"sha256": row["object_sha256"], "size": row["object_size"]}:
                    raise ProducerError("adoption_object_mismatch")
                files[target_relative] = identity
        plan = {"schema": ADOPTION_SCHEMA, "plan_id": str(uuid.uuid4()),
                "profile_digest": _digest_json(profile.value), "review": review, "files": files,
                "source": {"ledger": ledger, "objects": objects, "inventory": inventory},
                "state_dir": profile.state_dir}
        os.rename(stage, payload)
        _fsync_dir_best_effort(os.path.dirname(payload))
        stage = ""
        _write_json(output, plan)
        return {"state": "planned", "plan_id": plan["plan_id"], "plan_file": output,
                "review": review, "object_count": len(files) - 2}
    finally:
        if stage:
            shutil.rmtree(stage)


def adoption_apply(profile, plan_file):
    plan_file = _absolute(plan_file)
    plan = _read_json(plan_file)
    if (plan.get("schema") != ADOPTION_SCHEMA
            or plan.get("profile_digest") != _digest_json(profile.value)
            or plan.get("state_dir") != profile.state_dir):
        raise ProducerError("adoption_profile_mismatch")
    with _state_lock(profile.state_dir):
        if os.path.lexists(profile.state_dir):
            marker = profile.marker(required=True)
            if marker.get("adoption_plan_id") == plan.get("plan_id"):
                return {"state": "already_applied", "plan_id": plan["plan_id"],
                        "archive_id": marker["archive_id"]}
            raise ProducerError("producer_state_exists")
        stage = tempfile.mkdtemp(prefix=".producer-adopt-", dir=os.path.dirname(profile.state_dir))
        try:
            for relative, expected in plan["files"].items():
                source = _relative_file(plan_file + ".payload", relative)
                target = os.path.join(stage, relative)
                _copy_private(source, target)
                if _file_identity(target) != expected:
                    raise ProducerError("adoption_payload_changed")
            review, objects = _ledger_review(os.path.join(stage, "capture.db"), profile)
            if review != plan["review"]:
                raise ProducerError("adoption_payload_changed")
            required = {"capture.db", "source_inventory.json"} | {
                "objects/" + row["object_relpath"] for row in objects}
            if set(plan["files"]) != required:
                raise ProducerError("adoption_payload_invalid")
            _write_json(profile.paths(stage)["marker"], {
                "schema": STATE_SCHEMA, "archive_id": review["archive_id"],
                "source_db_dir": profile.value["source"]["db_dir"], "origin": "adopted",
                "source_namespace": review["source_namespace"],
                "adoption_plan_id": plan["plan_id"]})
            os.rename(stage, profile.state_dir)
            _fsync_dir_best_effort(os.path.dirname(profile.state_dir))
            stage = ""
            return {"state": "applied", "plan_id": plan["plan_id"], "archive_id": review["archive_id"]}
        finally:
            if stage:
                shutil.rmtree(stage)
