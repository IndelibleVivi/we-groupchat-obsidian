"""Private local JSON handoff from the resource ledger, without projections.

The capture ledger remains the only source scanner and durable message owner.
This destination uses the existing v3 CAS/snapshot transport and its locks, but
has a distinct purpose marker so it cannot reuse a mounted-backup destination.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict

from .resource_backup import (
    MountedResourceBackup, ResourceBackupError, _canonical_json_bytes,
    _canonical_jsonl_bytes, _stable_occurrence_id,
)
from .url_safety import redact_urls_in_text


HANDOFF_SCHEMA = "we-groupchat-obsidian.quiet-archive.v1"
TEXT_PROJECTION = "wechat-visible-text-redacted-v1"


def context_event_id(archive_id, chat_key, source_message_id):
    material = "\0".join(("we-groupchat-context-v1", archive_id, chat_key, source_message_id))
    return "wgo_context_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class QuietArchiveHandoff(MountedResourceBackup):
    """Reuse the v3 transport; never render Markdown or call a remote service."""

    def __init__(self, config, **kwargs):
        super().__init__(config, target=str(config.get("quiet_archive_handoff_target") or ""),
                         purpose="quiet-archive-local", link_export_mode="redacted", **kwargs)

    def _context_records(self, occurrences):
        associations = defaultdict(list)
        for occurrence in occurrences:
            associations[(occurrence["chat_key"], occurrence["source_message_id"])].append(
                _stable_occurrence_id(occurrence)
            )
        records = []
        for context in self.capture.contexts():
            key = (context["chat_key"], context["source_message_id"])
            records.append({
                "event_id": context_event_id(self.capture.archive_id, *key),
                "chat_key": key[0], "chat_alias": context["chat_alias"],
                "source_message_id": key[1],
                "source_timestamp": context["source_timestamp"],
                "source_time": context["source_time"],
                "message_type": context["message_type"],
                "text": redact_urls_in_text(context["text"]),
                "resource_occurrence_ids": sorted(associations[key]),
            })
        return records

    def coverage(self, occurrences=None, delivery_map=None):
        with self.capture.canonical_operation():
            rows = self.capture.occurrences() if occurrences is None else occurrences
            coverage = self.capture.context_coverage()
            files = [row for row in rows if row["kind"] == "file"]
            if delivery_map is None:
                conn = self._connect()
                try:
                    has_deliveries = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='resource_deliveries'"
                    ).fetchone() is not None
                finally:
                    conn.close()
                delivery_map = self._delivery_map() if has_deliveries else {}
            summary, _ = self._file_coverage(rows, delivery_map)
            delivered = int(summary["delivered_occurrences"])
            coverage["files"] = {
                "total_occurrences": len(files),
                "ready_local": sum(row["status"] == "ready_local" for row in files),
                "delivered_occurrences": delivered,
                "pending_occurrences": len(files) - delivered,
                "by_capture_status": dict(sorted(Counter(row["status"] for row in files).items())),
            }
            return coverage

    def status(self):
        return {"state": "configured" if self.target else "target_not_configured",
                "enabled": bool(self.config.get("quiet_archive_handoff_enabled", False)),
                "coverage": self.coverage()}

    def _run_owned(self):
        # Called by the shared run wrapper under capture + transport ownership.
        if not self.target:
            return {"state": "target_not_configured", "failed": 0}
        occurrences = self.capture.occurrences()
        objects = self._object_rows(occurrences)
        boundary = self._target_boundary_error(occurrences, objects)
        if boundary:
            raise ResourceBackupError(boundary)
        with self._target_worker_lock():
            self._ensure_destination_identity_owned()
            copies = self._copy_snapshot_objects(objects)
            if copies["failed"]:
                return {"state": "target_failed", **copies}
            deliveries = self._delivery_map()
            records = self._catalog_records(occurrences, deliveries)
            for record in records:
                record["source_sender"] = ""
            contexts = self._context_records(occurrences)
            coverage = self.coverage(occurrences, deliveries)
            contexts_bytes = _canonical_jsonl_bytes(contexts)
            coverage_bytes = _canonical_json_bytes(coverage)
            extension = {
                "schema": HANDOFF_SCHEMA,
                "contexts_file": "contexts.jsonl",
                "contexts_sha256": hashlib.sha256(contexts_bytes).hexdigest(),
                "context_count": len(contexts),
                "coverage_file": "coverage.json",
                "coverage_sha256": hashlib.sha256(coverage_bytes).hexdigest(),
                "text_projection": TEXT_PROJECTION,
            }
            snapshot = self._write_snapshot(records, objects, deliveries,
                machine_files={"contexts.jsonl": contexts_bytes, "coverage.json": coverage_bytes},
                machine_handoff=extension)
            return {"state": "unchanged" if snapshot["state"] == "unchanged" else "written",
                    **copies, "snapshot": snapshot, "coverage": coverage,
                    "remote_verified": False}
