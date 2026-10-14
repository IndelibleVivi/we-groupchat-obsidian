"""Source-free, read-only legacy inventory and lossless import preparation.

This module never constructs a WeChat reader, loads runtime configuration, opens
attachment payloads. Explicit staged navigation uses the reading publisher.
Its manifest is PRIVATE: exact old
rows, URLs and owned-page bytes must not be printed or exported as a report.
"""
from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from typing import Any, Iterable, Iterator, Mapping

from core.url_safety import redact_urls_in_text


SCHEMA = "we-groupchat-obsidian.legacy-inventory.v1"
TABLE_KEYS = {
    "topics": "topic_id", "events": "event_id", "relations": "relation_id",
    "attachment_mentions": "mention_id", "attachment_objects": "sha256",
    "attachment_attempts": "attempt_id", "attachment_worker_state": "worker_name",
    "daily_digest_changes": "change_id",
}
SELECTION_KEYS = (
    "monitor_chats", "monitor_chat_username", "monitor_chat_display_name",
    "monitor_chat_aliases", "resource_backup_selected_chats",
    "google_drive_file_sync_selected_chats",
)


class LegacyError(RuntimeError):
    """An error whose public message never contains paths or source content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def _digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _opaque(*parts: Any) -> str:
    return "legacy_" + _json_digest(parts)[:40]


def _json_digest(value: Any) -> str:
    """Same canonical bytes/identity, without allocating a whole encoded inventory."""
    def encode_dataclass(obj):
        if is_dataclass(obj) and not isinstance(obj, type):
            return {member.name: getattr(obj, member.name) for member in fields(obj)}
        raise TypeError("unsupported legacy identity value")
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                               allow_nan=False, default=encode_dataclass)
    digest = hashlib.sha256()
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"sqlite_blob_base64": base64.b64encode(value).decode("ascii")}
    return value


def _checked_path(path: str | Path, *, directory: bool = False) -> Path:
    """Reject every symlink component before opening, including parent aliases."""
    result = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    for component in reversed((result, *result.parents)):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError as exc:
            raise LegacyError("legacy_input_missing") from exc
        if stat.S_ISLNK(mode):
            raise LegacyError("legacy_symlink")
        if component != result and not stat.S_ISDIR(mode):
            raise LegacyError("legacy_non_regular")
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise LegacyError("legacy_non_regular")
    return result


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise LegacyError("legacy_path_escape")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"..", "."} for p in value.split("/")):
        raise LegacyError("legacy_path_escape")
    return str(path)


def _under(root: Path, relative: str) -> Path:
    relative = _relative(relative)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    if not candidate.is_relative_to(root):
        raise LegacyError("legacy_path_escape")
    return candidate


def _read_regular_once(path: Path, max_bytes: int) -> bytes:
    _checked_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise LegacyError("legacy_file_limit")
            data = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
            live = path.lstat()
        if (len(data) > max_bytes or
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or
                (after.st_dev, after.st_ino) != (live.st_dev, live.st_ino) or
                stat.S_ISLNK(live.st_mode)):
            raise LegacyError("legacy_input_changed")
        return data
    except OSError as exc:
        raise LegacyError("legacy_read_failed") from exc


def _read_regular(path: Path, max_bytes: int, *, retry_changed: bool = False) -> bytes:
    """Initial inventory may retry one unstable read; frozen checks never do.

    Each attempt independently verifies path, type, size and fd/live identity.
    Only a complete stable attempt is returned; persistent mutation fails closed.
    """
    for attempt in range(2 if retry_changed else 1):
        try:
            return _read_regular_once(path, max_bytes)
        except LegacyError as exc:
            if not retry_changed or attempt or exc.code != "legacy_input_changed":
                raise


@dataclass(frozen=True)
class LegacyInput:
    legacy_ref: str
    kind: str
    table: str
    old_id: str
    raw_level: str
    text: str
    source_ref: str
    source_message_id: str = ""
    topic_ref: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LegacyFile:
    relative_path: str
    sha256: str
    byte_length: int
    ownership: str
    topic_refs: tuple[str, ...]
    bytes_base64: str = ""
    anchors: tuple[dict[str, Any], ...] = ()
    references: tuple[dict[str, Any], ...] = ()
    aliases: tuple[str, ...] = ()

    @property
    def owned(self) -> bool:
        return self.ownership != "unowned"

    def original_bytes(self) -> bytes:
        if not self.owned:
            raise LegacyError("legacy_unowned_file")
        try:
            data = base64.b64decode(self.bytes_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise LegacyError("legacy_manifest_invalid") from exc
        if len(data) != self.byte_length or _digest(data) != self.sha256:
            raise LegacyError("legacy_manifest_bytes_changed")
        return data


@dataclass(frozen=True)
class LegacyInventory:
    namespace: str
    db_path: str
    managed_root: str
    managed_subdir: str
    database_snapshot_digest: str
    records: tuple[LegacyInput, ...]
    files: tuple[LegacyFile, ...]
    missing_paths: tuple[str, ...]
    selection_snapshot: dict[str, Any]
    tables: tuple[str, ...]
    external_files: tuple[dict[str, Any], ...] = ()
    schema: str = SCHEMA

    @property
    def manifest_id(self) -> str:
        return _opaque(self)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LegacyInventory":
        try:
            raw = dict(value)
            if raw.get("schema") != SCHEMA:
                raise LegacyError("legacy_manifest_schema")
            raw["records"] = tuple(LegacyInput(**r) for r in raw["records"])
            raw["files"] = tuple(LegacyFile(**{
                **f, "topic_refs": tuple(f["topic_refs"]),
                "anchors": tuple(f.get("anchors", ())),
                "references": tuple(f.get("references", ())),
                "aliases": tuple(f.get("aliases", ())),
            }) for f in raw["files"])
            raw["missing_paths"] = tuple(raw["missing_paths"])
            raw["tables"] = tuple(raw["tables"])
            raw["external_files"] = tuple(raw.get("external_files", ()))
            result = cls(**raw)
            result.validate()
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise LegacyError("legacy_manifest_invalid") from exc

    def validate(self) -> None:
        refs = [r.legacy_ref for r in self.records]
        paths = [f.relative_path for f in self.files]
        if len(refs) != len(set(refs)) or len(paths) != len(set(paths)):
            raise LegacyError("legacy_duplicate_identity")
        topic_refs = {r.legacy_ref for r in self.records if r.table == "topics"}
        for row in self.records:
            if row.raw_level not in {"raw", "excerpt_only", "derived_summary", "manual_annotation"}:
                raise LegacyError("legacy_raw_level_invalid")
            if row.topic_ref and row.topic_ref not in topic_refs:
                raise LegacyError("legacy_orphan_topic")
        pages = {r.old_id: r for r in self.records if r.table == "files"}
        for file in self.files:
            _relative(file.relative_path)
            if not set(file.topic_refs) <= topic_refs:
                raise LegacyError("legacy_orphan_topic")
            if file.owned:
                data = file.original_bytes()
                try:
                    page_text = data.decode("utf-8")
                except UnicodeError:
                    page_text = ""
                page = pages.get(file.relative_path)
                if (page is None or page.payload.get("bytes_base64") != file.bytes_base64
                        or page.payload.get("sha256") != file.sha256
                        or page.payload.get("byte_length") != file.byte_length
                        or page.text != page_text):
                    raise LegacyError("legacy_manifest_bytes_changed")
        expected = _json_digest(tuple(r for r in self.records if r.table != "files"))
        if expected != self.database_snapshot_digest:
            raise LegacyError("legacy_manifest_rows_changed")
        external_records = []
        for entry in self.external_files:
            if entry.get("role") != "review_queue" or not Path(entry["path"]).is_absolute():
                raise LegacyError("legacy_external_file_invalid")
            data = base64.b64decode(entry["bytes_base64"], validate=True)
            if len(data) != entry["byte_length"] or _digest(data) != entry["sha256"]:
                raise LegacyError("legacy_manifest_bytes_changed")
            external_records.extend(_queue_records(self.namespace, entry["path"], data, topic_refs))
        if [asdict(r) for r in external_records] != [asdict(r) for r in self.records if r.table == "review_queue"]:
            raise LegacyError("legacy_queue_membership_mismatch")


def _queue_records(namespace, path, raw, topics):
    records = []
    for index, line in enumerate(raw.splitlines(keepends=True)):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise LegacyError("legacy_queue_invalid") from exc
        if not isinstance(value, dict) or value.get("schema_version") != "review_queue.v1" or not isinstance(value.get("id"), str) or not value["id"]:
            raise LegacyError("legacy_queue_invalid")
        reference = _opaque(namespace, "review_queue", path, index)
        topic = _opaque(namespace, "topics", str(value.get("knowledge_topic_id")))
        resources = value.get("resources", {})
        if not isinstance(resources, dict):
            raise LegacyError("legacy_queue_invalid")
        payload = {"queue_item": value, "source_file": path, "line_number": index + 1,
                   "raw_line_base64": base64.b64encode(line).decode("ascii"),
                   "links_json": resources.get("links", []), "files_json": resources.get("files", [])}
        text = value.get("summary") or value.get("title") or ""
        if not isinstance(text, str):
            raise LegacyError("legacy_queue_invalid")
        records.append(LegacyInput(reference, "queue", "review_queue", str(index), "derived_summary", text,
                                   reference, topic_ref=topic if topic in topics else "", payload=payload))
    return tuple(records)


def include_review_queue(manifest: LegacyInventory, queue_dir: str | Path) -> LegacyInventory:
    """Add the exact append history without normalization, truncation or writes."""
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(queue_dir))))
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise LegacyError("legacy_symlink")
    if not root.exists():
        return manifest
    root = _checked_path(root, directory=True)
    path = _under(root, "pending.jsonl")
    if not path.exists():
        return manifest
    raw = _read_regular(path, 128 * 1024 * 1024, retry_changed=True)
    topics = {r.legacy_ref for r in manifest.records if r.table == "topics"}
    records = manifest.records + _queue_records(manifest.namespace, str(path), raw, topics)
    entry = {"role": "review_queue", "path": str(path), "sha256": _digest(raw), "byte_length": len(raw),
             "bytes_base64": base64.b64encode(raw).decode("ascii")}
    result = replace(manifest, records=records, external_files=manifest.external_files + (entry,),
                     database_snapshot_digest=_json_digest(tuple(r for r in records if r.table != "files")))
    result.validate()
    return result


def _source_fields(row: dict[str, Any], fallback: str) -> tuple[str, str]:
    # A batch ID or an excerpt hash is NOT a source-message ID.
    message_id = row.get("source_message_id")
    if not isinstance(message_id, str) or not message_id.strip():
        return fallback, ""
    namespace = str(row.get("source_namespace") or "legacy-observed")
    return _opaque("message", namespace, message_id), message_id


def _record(namespace: str, table: str, row: dict[str, Any]) -> LegacyInput:
    old_id = str(row[TABLE_KEYS[table]])
    reference = _opaque(namespace, table, old_id)
    topic_ref = (_opaque(namespace, "topics", str(row["topic_id"]))
                 if table != "topics" and row.get("topic_id") is not None else "")
    source_ref, message_id = _source_fields(row, reference)
    kind, level = "metadata", "derived_summary"
    if table == "topics":
        kind = "topic"
        text = str(row.get("summary") or row.get("title") or "")
    elif table == "events":
        kind = "event"
        if isinstance(row.get("raw_text"), str) and message_id:
            text, level = row["raw_text"], "raw"
        elif isinstance(row.get("messages_excerpt"), str) and row["messages_excerpt"]:
            text, level = row["messages_excerpt"], "excerpt_only"
        else:
            text = str(row.get("summary") or row.get("title") or "")
    elif table == "relations":
        kind = "relation"
        text = str(row.get("reason") or row.get("relation") or "")
    else:
        kind = "resource" if table.startswith("attachment_") else "metadata"
        text = ""  # Resource metadata must not be promoted into message prose.
    return LegacyInput(reference, kind, table, old_id, level, text, source_ref,
                       message_id, topic_ref, row)


def _read_database(path: Path, namespace: str) -> tuple[tuple[LegacyInput, ...], tuple[str, ...]]:
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "topics" not in names:
            raise LegacyError("legacy_schema_unsupported")
        result = []
        present = []
        for table, key in TABLE_KEYS.items():
            if table not in names:
                continue
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if key not in columns or (table == "topics" and "obsidian_path" not in columns):
                raise LegacyError("legacy_schema_unsupported")
            present.append(table)
            for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY "{key}"'):
                result.append(_record(namespace, table, {k: _value(row[k]) for k in row.keys()}))
        return tuple(result), tuple(present)
    except sqlite3.Error as exc:
        raise LegacyError("legacy_database_unreadable") from exc
    finally:
        if "conn" in locals():
            conn.close()


def _header_owned(text: str) -> bool:
    # Metadata applies only in the leading frontmatter, never arbitrary body text.
    front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, re.S)
    if front:
        fields = {}
        for line in front.group(1).splitlines():
            match = re.fullmatch(r"([a-z_]+):\s*(.*?)\s*", line)
            if match:
                fields[match[1]] = match[2].strip("\"'")
        if (fields.get("source_app") == "we-groupchat-obsidian" and
                fields.get("source_kind") in {"knowledge_topic", "projection"}):
            return True
    return any(marker in text.splitlines()[:16] for marker in (
        "<!-- wechat-summary:managed-date-index v1 -->",
        "<!-- wechat-summary:managed-date-index v2 -->",
        "<!-- we-groupchat-obsidian:managed-date-index v3 -->",
    ))


def _file_references(text: str) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], tuple[str, ...]]:
    anchors, references = [], []
    for match in re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*#*\s*$|\^([A-Za-z0-9][A-Za-z0-9_-]*)\s*$", text):
        anchors.append({"kind": "heading" if match[1] else "block",
                        "target": match[1] or "^" + match[2],
                        "start": len(text[:match.start()].encode()),
                        "end": len(text[:match.end()].encode())})
    pattern = r"!?\[\[([^\]\n]+)\]\]|!?\[[^\]\n]*\]\(([^)\n]+)\)|https?://[^\s<>\]\)]+"
    for match in re.finditer(pattern, text):
        target = (match[1].split("|", 1)[0] if match[1] else match[2] or match[0])
        references.append({"syntax": match[0], "target": target,
                           "start": len(text[:match.start()].encode()),
                           "end": len(text[:match.end()].encode())})
    front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, re.S)
    aliases = []
    if front:
        lines = front.group(1).splitlines(keepends=True)
        for index, line in enumerate(lines):
            if re.match(r"^aliases?\s*:", line):
                block = [line]
                for continuation in lines[index + 1:]:
                    if not continuation.startswith((" ", "\t", "-")):
                        break
                    block.append(continuation)
                aliases.append("".join(block))
    return tuple(anchors), tuple(references), tuple(aliases)


def plan_legacy(config: Mapping[str, Any] | None = None, *, db_path: str | Path | None = None,
                managed_root: str | Path | None = None, managed_subdir: str | None = None,
                prior_selected_chats: Iterable[Any] | None = None,
                namespace: str | None = None, max_file_bytes: int = 32 * 1024 * 1024) -> LegacyInventory:
    """Inventory only explicit old DB/managed roots. No implicit config/source read.

    Unknown files remain unowned and are checked by bytes/hash, never imported as
    knowledge. A recorded missing page is retained as a missing projection, not a
    deleted source. Oversized/unreadable/symlink inputs stop planning, not truncate.
    """
    config = config or {}
    db_path = db_path or config.get("monitor_knowledge_db")
    managed_root = managed_root or config.get("monitor_obsidian_root")
    managed_subdir = managed_subdir if managed_subdir is not None else config.get("monitor_obsidian_subdir")
    if not db_path or not managed_root or not isinstance(managed_subdir, str):
        raise LegacyError("legacy_explicit_inputs_required")
    db = _checked_path(db_path)
    root = _checked_path(managed_root, directory=True)
    subtree = root if not managed_subdir else _under(root, managed_subdir)
    _checked_path(subtree, directory=True)
    namespace = namespace or _opaque("database", str(db))
    records, tables = _read_database(db, namespace)
    known_paths: dict[str, list[str]] = {}
    for record in records:
        if record.table == "topics" and record.payload.get("obsidian_path"):
            relative = _relative(record.payload["obsidian_path"])
            known_paths.setdefault(relative, []).append(record.legacy_ref)
    # DB-recorded paths may belong to a previous explicit subdir. Do not silently
    # drop those known paths, but never recursively scan outside the chosen tree.
    candidates = set(known_paths)
    for directory, dirs, files in os.walk(subtree, followlinks=False):
        for name in (*dirs, *files):
            path = Path(directory, name)
            if path.is_symlink():
                raise LegacyError("legacy_symlink")
        candidates.update(str(Path(directory, f).relative_to(root).as_posix()) for f in files)
    files_out, missing, page_records = [], [], []
    for relative in sorted(candidates):
        path = _under(root, relative)
        if not path.exists():
            # Check the extant parents too; a missing descendant under a symlink
            # must not masquerade as a harmless missing file.
            for parent in path.parents:
                if parent == root.parent:
                    break
                if parent.is_symlink():
                    raise LegacyError("legacy_symlink")
            missing.append(relative)
            continue
        data = _read_regular(path, max_file_bytes, retry_changed=True)
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            text = ""
        ownership = ("recorded_topic_path" if relative in known_paths else
                     "source_header" if _header_owned(text) else "unowned")
        anchors, references, aliases = _file_references(text)
        owned = ownership != "unowned"
        file = LegacyFile(relative, _digest(data), len(data), ownership,
                          tuple(known_paths.get(relative, ())),
                          base64.b64encode(data).decode("ascii") if owned else "",
                          anchors, references, aliases)
        files_out.append(file)
        if owned:
            reference = _opaque(namespace, "files", relative)
            page_records.append(LegacyInput(reference, "page", "files", relative,
                "manual_annotation", text, reference, payload={
                    "relative_path": relative, "sha256": file.sha256,
                    "byte_length": len(data), "bytes_base64": file.bytes_base64,
                    "ownership": ownership, "baseline": "unknown_preserve_entire_page",
                    "topic_refs": list(file.topic_refs), "anchors": list(anchors),
                    "references": list(references), "aliases": list(aliases),
                }))
    selection = {key: config[key] for key in SELECTION_KEYS if key in config}
    if prior_selected_chats is not None:
        selection["prior_selected_chats"] = list(prior_selected_chats)
    inventory = LegacyInventory(namespace, str(db), str(root), managed_subdir,
        _json_digest(records), records + tuple(page_records),
        tuple(files_out), tuple(missing), json.loads(_json(selection)), tables)
    inventory.validate()
    return inventory


def iter_legacy_inputs(manifest: LegacyInventory | Mapping[str, Any], *, batch_size: int = 100) -> Iterator[tuple[LegacyInput, ...]]:
    """Yield bounded immutable rows without reopening the old DB or file tree."""
    if not isinstance(manifest, LegacyInventory):
        manifest = LegacyInventory.from_dict(manifest)
    manifest.validate()
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise LegacyError("legacy_batch_size_invalid")
    for start in range(0, len(manifest.records), batch_size):
        yield manifest.records[start:start + batch_size]


@dataclass(frozen=True)
class LegacyImportResult:
    manifest_id: str
    batch_ids: tuple[str, ...]
    capture_receipt_ids: tuple[str, ...]
    record_versions: dict[str, str]
    status: str = "captured_semantic_pending"


def legacy_resources(record: LegacyInput) -> tuple[dict[str, Any], ...]:
    """Exact old references, with no claim that attachment bytes are accessible."""
    resources = []
    occurrence = record.source_message_id or record.legacy_ref
    if record.table == "attachment_mentions":
        metadata = dict(record.payload)
        resource_index = metadata.get("resource_index", 0)
        resource_id = _opaque("legacy-attachment", record.source_ref, resource_index)
        resources.append({"resource_id": resource_id, "occurrence_id": occurrence,
                          "purpose": "legacy_attachment", "status": "pending", "location": None,
                          "metadata": {"legacy_reference": metadata}})
    elif record.table == "attachment_objects":
        resources.append({"resource_id": _opaque("legacy-object", record.old_id),
                          "occurrence_id": occurrence, "purpose": "legacy_object_reference",
                          "status": "pending", "location": None,
                          "metadata": {"legacy_reference": record.payload}})
    for field, purpose in (("links_json", "legacy_link"), ("files_json", "legacy_file")):
        if field not in record.payload:
            continue
        original = record.payload[field]
        try:
            parsed = json.loads(original) if isinstance(original, str) else original
            if not isinstance(parsed, list):
                raise ValueError()
        except (ValueError, TypeError):
            # Malformed old JSON is still a conserved reference, not silently
            # omitted. It needs later interpretation before a usable link exists.
            parsed = [{"unparsed_legacy_value": original}]
        seen = set()
        for value in parsed:
            resource_id = _opaque(purpose, value)
            if resource_id in seen:
                continue
            seen.add(resource_id)
            resources.append({"resource_id": resource_id, "occurrence_id": occurrence,
                              "purpose": purpose, "status": "pending", "location": None,
                              "metadata": {"legacy_reference": value}})
    return tuple(resources)


def import_legacy(store, manifest: LegacyInventory | Mapping[str, Any], *,
                  job_id: str | None = None, generation_id: str | None = None,
                  batch_size: int = 100, allow_preserved_refresh: bool = False) -> LegacyImportResult:
    """Capture a frozen inventory and conservation evidence in the canonical DB.

    No model call, Item decision, subscription or source-applied ACK is invented.
    The returned batches must pass the normal extraction/resolver pipeline. A
    crash between capture and mapping is resumed using immutable batch IDs.
    """
    if not isinstance(manifest, LegacyInventory):
        manifest = LegacyInventory.from_dict(manifest)
    manifest.validate()
    manifest_id = manifest.manifest_id
    if generation_id is not None and generation_id != store.generation_id:
        raise LegacyError("legacy_generation_mismatch")
    # A fresh freeze may change one note while retaining historical A-B-A rows.
    # Replaying unchanged historical rows as new captures would manufacture
    # revisions. Freeze the exact changed-record set before the first capture;
    # retries then adopt the same bounded batch identities after a crash.
    versions = {}
    with store.transaction() as conn:
        saved = conn.execute("SELECT metadata_json FROM legacy_mappings WHERE kind='import_plan' AND legacy_id=?",
                             (manifest_id,)).fetchone()
        if saved:
            planned = set(json.loads(saved[0])["record_refs"])
        else:
            planned = set()
            for row in manifest.records:
                candidates = conn.execute("SELECT target_id,metadata_json FROM legacy_mappings WHERE legacy_id=? AND target_type='source_version'",
                                          (row.legacy_ref,)).fetchall()
                unchanged = [candidate for candidate in candidates if json.loads(candidate[1]).get("input") == asdict(row)]
                if not unchanged:
                    planned.add(row.legacy_ref)
                else:
                    candidate = unchanged[-1]
                    metadata = json.loads(candidate[1])
                    metadata["manifest_id"] = manifest_id
                    conn.execute("UPDATE legacy_mappings SET metadata_json=? WHERE legacy_id=? AND target_type='source_version' AND target_id=?",
                                 (_json(metadata), row.legacy_ref, candidate[0]))
                    versions[row.legacy_ref] = candidate[0]
            conn.execute("INSERT INTO legacy_mappings VALUES(?,?,?,?,?,?)", ("import_plan", manifest_id,
                         "frozen_record_set", manifest_id, "captured_plan", _json({"record_refs": sorted(planned)})))
        for row in manifest.records:
            if row.legacy_ref in versions:
                continue
            prior = conn.execute("SELECT target_id,metadata_json FROM legacy_mappings WHERE legacy_id=? AND target_type='source_version'",
                                 (row.legacy_ref,)).fetchall()
            for target, metadata in prior:
                if json.loads(metadata).get("input") == asdict(row):
                    versions[row.legacy_ref] = target
    # Validate the bounds once, then split additionally at repeated occurrences.
    # In A-B-A, every transition must be captured before its next observation.
    list_batches = (tuple(row for row in page if row.legacy_ref in planned)
                    for page in iter_legacy_inputs(manifest, batch_size=batch_size))
    batches: list[tuple[LegacyInput, ...]] = []
    for rows in list_batches:
        current, seen = [], set()
        for row in rows:
            source_id = row.source_ref if row.raw_level == "raw" else row.legacy_ref
            if source_id in seen:
                batches.append(tuple(current))
                current, seen = [], set()
            current.append(row)
            seen.add(source_id)
        if current:
            batches.append(tuple(current))
    batch_ids, receipts = [], []
    position = 0
    shard = "legacy-records"
    def cursors(offset):
        return {shard: {"generation_id": manifest.database_snapshot_digest,
                        "cursor_token": _json([offset, 0]), "start_cursor": "[0,0]"}}
    for index, rows in enumerate(batches):
        batch_id = _opaque("import", manifest_id, index)
        payloads = []
        for offset, row in enumerate(rows, position + 1):
            exact = row.raw_level == "raw"
            # Row provenance is kept separately below, so two old rows referring
            # to the same unmodified occurrence do not manufacture independence.
            payload = {"text": row.text} if exact else {"legacy_row": row.payload}
            source_time = row.payload.get("source_timestamp", 0)
            if type(source_time) not in (int, float):
                source_time = 0
            payloads.append({
                "source_id": row.source_ref if exact else row.legacy_ref,
                "logical_shard_id": shard,
                "physical_generation": manifest.database_snapshot_digest,
                "source_cursor_token": _json([offset, 0]),
                "source_sent_at": source_time, "text": row.text,
                "payload": payload, "kind": "legacy_" + row.kind,
                "provenance_grade": "raw_exact" if exact else row.raw_level, "core": True,
                "resources": list(legacy_resources(row)),
            })
        receipt = store.capture_batch(batch_id, payloads,
            source_namespace="legacy:" + manifest.namespace,
            chat_id="legacy-unsubscribed", inventory_digest=manifest.database_snapshot_digest,
            inventory_revision=1, input_cursors=cursors(position),
            output_cursors=cursors(position + len(rows)),
            source_eof=index == len(batches) - 1, purpose="legacy_import")
        member_ids = receipt["member_version_ids"]
        if len(member_ids) != len(rows):
            raise LegacyError("legacy_capture_membership_mismatch")
        with store.transaction() as conn:
            for row, version_id in zip(rows, member_ids):
                metadata = {"manifest_id": manifest_id, "input": asdict(row),
                            "batch_id": batch_id, "capture_receipt_id": receipt["receipt_id"],
                            "import_job_id": job_id, "semantic_status": "pending"}
                conn.execute("""INSERT INTO legacy_mappings
                    (kind,legacy_id,target_type,target_id,disposition,metadata_json)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(kind,legacy_id,target_type,target_id)
                    DO UPDATE SET metadata_json=excluded.metadata_json""", (row.kind, row.legacy_ref, "source_version", version_id,
                                     "captured_semantic_pending", _json(metadata)))
                versions[row.legacy_ref] = version_id
        position += len(rows)
        batch_ids.append(batch_id)
        receipts.append(receipt["receipt_id"])
    with store.transaction() as conn:
        for external in manifest.external_files:
            conn.execute("INSERT OR IGNORE INTO legacy_mappings VALUES(?,?,?,?,?,?)",
                         ("external_file", _opaque(manifest.namespace, external["path"]), "file", external["sha256"],
                          "captured_pending_backup", _json({"manifest_id": manifest_id, "file": external})))
        for file in manifest.files:
            metadata = {"manifest_id": manifest_id, "file": asdict(file)}
            conn.execute("""INSERT INTO legacy_mappings
                (kind,legacy_id,target_type,target_id,disposition,metadata_json)
                VALUES(?,?,?,?,?,?) ON CONFLICT(kind,legacy_id,target_type,target_id) DO NOTHING""",
                ("file", _opaque(manifest.namespace, "file-inventory", file.relative_path),
                 "file", file.sha256, "preserve_in_place", _json(metadata)))
            if file.owned:
                existing = conn.execute("SELECT * FROM annotation_preservation WHERE path=?",
                                        (file.relative_path,)).fetchone()
                if existing is not None and existing["sha256"] != file.sha256:
                    if not allow_preserved_refresh or existing["status"] not in {"preserved", "preserved_in_place", "user_preserved"}:
                        raise LegacyError("legacy_annotation_baseline_changed")
                    old = _checked_path(existing["backup_path"])
                    if _digest(_read_regular(old, max(1, old.stat().st_size))) != existing["sha256"]:
                        raise LegacyError("legacy_backup_bytes_mismatch")
                    conn.execute("INSERT OR IGNORE INTO legacy_mappings VALUES(?,?,?,?,?,?)",
                        ("annotation_history", file.relative_path, "backup", existing["sha256"],
                         "preserved", _json(dict(existing))))
                    conn.execute("DELETE FROM annotation_preservation WHERE path=?", (file.relative_path,))
                conn.execute("""INSERT INTO annotation_preservation
                    (path,sha256,backup_path,status,metadata_json) VALUES(?,?,?,?,?)
                    ON CONFLICT(path) DO NOTHING""",
                    (file.relative_path, file.sha256, "", "captured_pending_backup", _json(metadata)))
        conn.execute("""INSERT INTO legacy_mappings
            (kind,legacy_id,target_type,target_id,disposition,metadata_json)
            VALUES(?,?,?,?,?,?) ON CONFLICT(kind,legacy_id,target_type,target_id) DO NOTHING""",
            ("selection", manifest.namespace, "selection_snapshot", manifest_id,
             "preserved_no_subscription", _json(manifest.selection_snapshot)))
    return LegacyImportResult(manifest_id, tuple(batch_ids), tuple(receipts), versions)


def build_publication_plan(manifest: LegacyInventory, topic_item_map: Mapping[str, Iterable[str]],
                           item_paths: Mapping[str, str]) -> dict[str, Any]:
    """Plan explicit old-ID mappings; never resolve by title or write a file.

    Unknown-baseline bytes stay at their original path. Redirect/hub is a
    navigation sidecar, and the old page is retained history. An operator may
    later accept a reading-consumer proposal after a full backup; this plan is
    not that permission. All old block/heading references retain a history home.
    """
    manifest.validate()
    topics = {r.legacy_ref: r for r in manifest.records if r.table == "topics"}
    if not set(topic_item_map) <= topics.keys():
        raise LegacyError("legacy_mapping_unknown_topic")
    mapping = {}
    for reference, ids in topic_item_map.items():
        if isinstance(ids, str):
            raise LegacyError("legacy_mapping_invalid")
        values = tuple(ids)
        if len(values) != len(set(values)) or any(i not in item_paths for i in values):
            raise LegacyError("legacy_mapping_invalid")
        for item_id in values:
            target = _relative(item_paths[item_id])
            if any(c in target for c in "[]|#^\n\r"):
                raise LegacyError("legacy_navigation_path_invalid")
        mapping[reference] = values
    pages = []
    for file in manifest.files:
        if not file.owned:
            continue
        ids = tuple(sorted({item for ref in file.topic_refs for item in mapping.get(ref, ())}))
        unresolved = tuple(ref for ref in file.topic_refs if ref not in mapping)
        mode = "pending" if unresolved else "redirect" if len(ids) == 1 else "hub" if ids else "history"
        sidecar = "Legacy/" + _opaque(manifest.namespace, file.relative_path) + ".md"
        if any(c in file.relative_path for c in "[]|#^\n\r"):
            raise LegacyError("legacy_navigation_path_invalid")
        history_link = "[[" + redact_urls_in_text(file.relative_path).removesuffix(".md") + "|历史原页]]"
        navigation = "# 历史笔记索引\n\n" + history_link + "\n\n" + "\n".join(
            "- [[WGO/" + redact_urls_in_text(item_paths[item]).removesuffix(".md") + "]]" for item in ids)
        if mode == "pending":
            navigation += "\n\n语义整理尚未完成；历史原页与个人笔记已保留。"
        elif mode == "history":
            navigation += "\n\n此页保留为历史资料。"
        pages.append({"old_path": file.relative_path, "old_sha256": file.sha256,
                      "old_byte_length": file.byte_length, "mode": mode,
                      "preserve_original": True, "history_path": file.relative_path,
                      "navigation_path": sidecar, "navigation_body": navigation + "\n",
                      "history_backup_path": ".wgo/legacy-history/" + _opaque(manifest.namespace, file.relative_path, file.sha256) + ".bin",
                      "item_ids": list(ids), "unresolved_topic_refs": list(unresolved),
                      "anchor_maps": [{"old_anchor": a["target"],
                                       "history_path": file.relative_path,
                                       "history_anchor": a["target"],
                                       "item_ids": list(ids)} for a in file.anchors],
                      "references": list(file.references), "aliases": list(file.aliases)})
    queue_lines = []
    for record in manifest.records:
        if record.table != "review_queue":
            continue
        item = record.payload["queue_item"]
        # Retain every append transition; this is historical state, never a new
        # action, subscription or automatic acknowledgement in reader controls.
        visible = str(item.get("title", "历史待办")) + " · " + str(item.get("status", "unknown"))
        visible = redact_urls_in_text(visible).replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")
        queue_lines.append("- " + visible + " ^queue-" + record.legacy_ref[:20])
    return {"schema": "we-groupchat-obsidian.legacy-publication.v1",
            "manifest_id": manifest.manifest_id, "requires_verified_backup": True,
            "topic_item_map": {ref: list(ids) for ref, ids in mapping.items()},
            "item_paths": {item: item_paths[item] for item in sorted({i for ids in mapping.values() for i in ids})},
            "pages": pages, "missing_paths": list(manifest.missing_paths),
            "subscriptions": [], "queue_history": {
                "object_id": "control:legacy-queue:" + manifest.namespace, "path": "Legacy/Review Queue.md",
                "body": "# 历史 Review Queue\n\n" + "\n".join(queue_lines) + "\n",
                "record_count": len(queue_lines)} if queue_lines else None}


def _topic_families(manifest, conn):
    """Return only versions proven to belong to this exact frozen inventory."""
    versions = {}
    manifest_id = manifest.manifest_id
    for row in conn.execute("SELECT legacy_id,target_id,metadata_json FROM legacy_mappings WHERE target_type='source_version'"):
        metadata = json.loads(row[2])
        if metadata.get("manifest_id") == manifest_id:
            versions.setdefault(row[0], set()).add(row[1])
    families = {}
    for topic in (r for r in manifest.records if r.kind == "topic"):
        refs = {topic.legacy_ref} | {r.legacy_ref for r in manifest.records if r.topic_ref == topic.legacy_ref or
                                    topic.legacy_ref in r.payload.get("topic_refs", ())}
        if all(ref in versions for ref in refs):
            families[topic.legacy_ref] = set().union(*(versions[ref] for ref in refs))
    return families


def _history_dispositions(conn, versions):
    for version in versions:
        row = conn.execute("""SELECT d.status,e.receipt_json,e.receipt_id FROM source_dispositions d
            JOIN extraction_receipts e USING(job_id) JOIN accepted_job_artifacts a USING(job_id)
            WHERE d.source_version_id=?""", (version,)).fetchone()
        if row is None or row[0] not in {"noise", "context_only", "repetition_only", "unsupported_type"}:
            return False
        receipt = json.loads(row[1])
        if (receipt.get("receipt_id") != row[2]
                or receipt.get("dispositions", {}).get(version, {}).get("status") != row[0]):
            return False
        if conn.execute("""SELECT 1 FROM observations o JOIN observation_evidence e USING(observation_id)
            JOIN source_spans s USING(span_id) WHERE s.source_version_id=?""", (version,)).fetchone():
            return False
    return bool(versions)


def derive_topic_item_map(store, manifest: LegacyInventory) -> dict[str, list[str]]:
    """Derive identity only from canonical cited-source joins, never titles.

    An omitted topic is unresolved. An explicit empty list is permitted only
    when all its captured source versions have terminal non-observed receipts.
    """
    manifest.validate()
    result = {}
    with closing(store.connect()) as conn:
        for topic, versions in _topic_families(manifest, conn).items():
            targets = set()
            for version in versions:
                targets.update(r[0] for r in conn.execute("""SELECT DISTINCT i.item_id
                    FROM source_spans s JOIN observation_evidence e USING(span_id)
                    JOIN item_observations io USING(observation_id) JOIN items i USING(item_id)
                    WHERE s.source_version_id=? AND i.lifecycle='active'""", (version,)))
            if targets:
                result[topic] = sorted(targets)
            elif _history_dispositions(conn, versions):
                result[topic] = []
    return result


def record_legacy_resolution(store, manifest: LegacyInventory,
                             topic_item_map: Mapping[str, Iterable[str]],
                             item_paths: Mapping[str, str]) -> dict[str, Any]:
    """Bind explicit resolver decisions to Items that actually cite old evidence.

    This stores mappings, not semantic actions; accepted canonical resolver jobs
    must already have created/updated the Items. There is no title-based fallback.
    """
    plan = build_publication_plan(manifest, topic_item_map, item_paths)
    manifest_id = manifest.manifest_id
    with store.transaction() as conn:
        families = _topic_families(manifest, conn)
        for topic_ref, targets in plan["topic_item_map"].items():
            source_versions = families.get(topic_ref, set())
            conn.execute("UPDATE legacy_mappings SET disposition='superseded' WHERE kind='topic' AND legacy_id=? AND target_type IN ('item','history_topic')",
                         (topic_ref,))
            if not targets:
                if not _history_dispositions(conn, source_versions):
                    raise LegacyError("legacy_history_disposition_missing")
                conn.execute("""INSERT INTO legacy_mappings VALUES(?,?,?,?,?,?)
                    ON CONFLICT(kind,legacy_id,target_type,target_id) DO UPDATE SET
                    disposition=excluded.disposition,metadata_json=excluded.metadata_json""",
                    ("topic", topic_ref, "history_topic", topic_ref, "history_preserved",
                     _json({"manifest_id": manifest_id, "source_version_ids": sorted(source_versions)})))
            for item_id in targets:
                cited = {r[0] for r in conn.execute("""SELECT DISTINCT s.source_version_id
                    FROM item_observations i JOIN observation_evidence e USING(observation_id)
                    JOIN source_spans s USING(span_id) WHERE i.item_id=?""", (item_id,))}
                witnessed = source_versions & cited
                if not witnessed:
                    raise LegacyError("legacy_mapping_evidence_missing")
                conn.execute("""INSERT INTO legacy_mappings
                    (kind,legacy_id,target_type,target_id,disposition,metadata_json)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(kind,legacy_id,target_type,target_id)
                    DO UPDATE SET disposition=excluded.disposition,metadata_json=excluded.metadata_json""",
                    ("topic", topic_ref, "item", item_id, "resolved",
                     _json({"manifest_id": manifest_id,
                            "source_version_ids": sorted(witnessed),
                            "item_path": item_paths[item_id]})))
        for page in plan["pages"]:
            entries = [("path", _opaque(manifest.namespace, "path", page["old_path"]), {"old_path": page["old_path"]})]
            entries.extend(("anchor", _opaque(manifest.namespace, page["old_path"], anchor["old_anchor"], index), anchor)
                           for index, anchor in enumerate(page["anchor_maps"]))
            for kind, legacy_id, detail in entries:
                conn.execute("UPDATE legacy_mappings SET disposition='superseded' WHERE kind=? AND legacy_id=?",
                             (kind, legacy_id))
                for target in page["item_ids"] or [page["history_path"]]:
                    conn.execute("""INSERT INTO legacy_mappings
                        (kind,legacy_id,target_type,target_id,disposition,metadata_json)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(kind,legacy_id,target_type,target_id)
                        DO UPDATE SET disposition=excluded.disposition,metadata_json=excluded.metadata_json""",
                        (kind, legacy_id, "item" if page["item_ids"] else "history_path", target,
                         page["mode"], _json({"manifest_id": manifest_id, "history_path": page["history_path"],
                                              "mapping": detail})))
    return plan


def record_legacy_backups(store, manifest: LegacyInventory,
                          backups: Mapping[str, str | Path]) -> None:
    """Verify coordinator-made recovery copies before acknowledging conservation.

    This function neither creates nor modifies a recovery file or legacy page.
    Relative backup locations are not guessed; the coordinator supplies exact
    private regular files after its own durable backup step.
    """
    manifest.validate()
    manifest_id = manifest.manifest_id
    verified = []
    for file in manifest.files:
        if not file.owned:
            continue
        if file.relative_path not in backups:
            raise LegacyError("legacy_backup_missing")
        path = _checked_path(backups[file.relative_path])
        raw = _read_regular(path, max(1, file.byte_length))
        if raw != file.original_bytes():
            raise LegacyError("legacy_backup_bytes_mismatch")
        current = _read_regular(_under(_checked_path(manifest.managed_root, directory=True), file.relative_path),
                                max(1, file.byte_length))
        if current != raw:
            raise LegacyError("legacy_original_bytes_changed")
        verified.append((str(path), file.relative_path, file.sha256))
    with store.transaction() as conn:
        for path, relative, sha in verified:
            cursor = conn.execute("""UPDATE annotation_preservation SET backup_path=?,status='preserved_in_place'
                WHERE path=? AND sha256=?""", (path, relative, sha))
            if cursor.rowcount != 1:
                raise LegacyError("legacy_annotation_unaccounted")
        for external in manifest.external_files:
            backup_path = backups.get("external:" + external["path"])
            if not backup_path:
                raise LegacyError("legacy_backup_missing")
            expected = base64.b64decode(external["bytes_base64"])
            current = _read_regular(_checked_path(external["path"]), max(1, external["byte_length"]))
            recovery = _read_regular(_checked_path(backup_path), max(1, external["byte_length"]))
            if current != expected or recovery != expected:
                raise LegacyError("legacy_backup_bytes_mismatch")
            conn.execute("UPDATE legacy_mappings SET disposition='preserved_in_place',metadata_json=? WHERE kind='external_file' AND legacy_id=? AND target_id=?",
                         (_json({"manifest_id": manifest_id, "file": external, "backup_path": str(backup_path)}),
                          _opaque(manifest.namespace, external["path"]), external["sha256"]))


def publish_legacy_navigation(store, manifest: LegacyInventory, publication_plan: Mapping[str, Any],
                              publisher) -> dict[str, Any]:
    """Publish replayable navigation and private byte copies after exact backups.

    Original pages are never edited. Byte copies are private recovery objects,
    not new human-readable projections. Ownership, atomic writes, conflict
    proposals and root locking stay with the canonical reading publisher.
    """
    from .projections import ReadingError

    manifest.validate()
    manifest_id = manifest.manifest_id
    if publisher.ledger.generation_id != store.generation_id:
        raise LegacyError("legacy_generation_mismatch")
    with publisher.ledger.connection() as conn:
        database = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), None)
        if database is None or _checked_path(database) != _checked_path(store.path):
            raise LegacyError("legacy_publisher_store_mismatch")
    try:
        supplied = json.loads(_json(dict(publication_plan)))
        expected = build_publication_plan(manifest, supplied["topic_item_map"], supplied["item_paths"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LegacyError("legacy_publication_plan_invalid") from exc
    if supplied != expected:
        raise LegacyError("legacy_publication_plan_invalid")
    old_root = _checked_path(manifest.managed_root, directory=True)
    files = {f.relative_path: f for f in manifest.files if f.owned}

    def verified_original(file):
        with closing(store.connect()) as conn:
            row = conn.execute("SELECT * FROM annotation_preservation WHERE path=?", (file.relative_path,)).fetchone()
        if (row is None or row["sha256"] != file.sha256
                or row["status"] not in {"preserved", "preserved_in_place", "user_preserved"}):
            raise LegacyError("legacy_backup_missing")
        expected_bytes = file.original_bytes()
        backup = _read_regular(_checked_path(row["backup_path"]), max(file.byte_length, 1))
        if backup != expected_bytes:
            raise LegacyError("legacy_backup_bytes_mismatch")
        if _read_regular(_under(old_root, file.relative_path), max(file.byte_length, 1)) != expected_bytes:
            raise LegacyError("legacy_original_bytes_changed")
        return expected_bytes

    # Check all preconditions before publishing any navigation.
    for file in files.values():
        verified_original(file)
    for item_id, path in supplied["item_paths"].items():
        resolved = publisher.resolve(item_id)
        row = publisher.record(item_id)
        if resolved is None and row is not None and row["status"] == "personal":
            resolved = row["path"]
        if resolved != path or publisher.read(path) is None:
            raise LegacyError("legacy_item_projection_missing")
    record_legacy_resolution(store, manifest, supplied["topic_item_map"], supplied["item_paths"])
    revision = store.read_revision()
    receipts, histories = [], []
    try:
        with publisher.locked():
            for page in supplied["pages"]:
                file = files[page["old_path"]]
                raw = verified_original(file)
                old = publisher.read(page["history_backup_path"])
                if old is not None and old != raw:
                    raise LegacyError("legacy_history_copy_collision")
                publisher.atomic_write(page["history_backup_path"], raw,
                                       expected=_digest(old) if old is not None else None)
                if publisher.read(page["history_backup_path"]) != raw:
                    raise LegacyError("legacy_history_copy_failed")
                object_id = "control:legacy:" + _opaque(manifest.namespace, file.relative_path)
                receipt = publisher.publish_document(object_id=object_id,
                    path=page["navigation_path"], title="历史笔记索引", body=page["navigation_body"],
                    content_revision=max(1, revision), is_current=lambda: store.read_revision() == revision)
                receipts.append(asdict(receipt))
                histories.append({"old_path": file.relative_path, "path": page["history_backup_path"],
                                  "sha256": file.sha256, "byte_length": file.byte_length})
                with store.transaction() as conn:
                    conn.execute("""INSERT INTO legacy_mappings VALUES(?,?,?,?,?,?)
                        ON CONFLICT(kind,legacy_id,target_type,target_id) DO UPDATE SET
                        disposition=excluded.disposition,metadata_json=excluded.metadata_json""",
                        ("navigation", _opaque(manifest.namespace, "path", file.relative_path),
                         "projection", object_id, receipt.status,
                         _json({"manifest_id": manifest_id, "page": page, "receipt": asdict(receipt)})))
            queue = supplied.get("queue_history")
            if queue:
                receipt = publisher.publish_document(object_id=queue["object_id"], path=queue["path"],
                    title="历史 Review Queue", body=queue["body"], content_revision=max(1, revision),
                    is_current=lambda: store.read_revision() == revision)
                receipts.append(asdict(receipt))
                with store.transaction() as conn:
                    conn.execute("INSERT INTO legacy_mappings VALUES(?,?,?,?,?,?) ON CONFLICT(kind,legacy_id,target_type,target_id) DO UPDATE SET disposition=excluded.disposition,metadata_json=excluded.metadata_json",
                        ("queue_navigation", manifest.namespace, "projection", queue["object_id"], receipt.status,
                         _json({"manifest_id": manifest_id, "queue": queue, "receipt": asdict(receipt)})))
    except ReadingError as exc:
        raise LegacyError("legacy_publication_conflict") from exc
    status = ("projection_pending" if any(r["status"] not in {"published", "unchanged"} for r in receipts)
              else "semantic_pending" if any(p["mode"] == "pending" for p in supplied["pages"])
              else "published")
    return {"manifest_id": manifest_id, "status": status,
            "receipts": receipts, "history_copies": histories}
