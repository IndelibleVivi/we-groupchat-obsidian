"""Private, content-addressed archive for WeChat attachment cache files.

The knowledge transaction only records attachment mentions.  This module is a
separate, retryable consumer: cache resolution and byte copying can fail without
rolling back a Knowledge event or causing another AI pass.
"""
from __future__ import annotations

import errno
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from .config import DATA_DIR
from .image_decoder import decode_wechat_image_data, detect_mime
from .knowledge import KNOWLEDGE_DB, KnowledgeStore


AUTO_RETRY_STATUSES = (
    "missing_retryable",
    "decode_unavailable",
    "source_changed",
    "archive_failed",
    "insufficient_archive_space",
)
MANUAL_RETRY_STATUSES = (
    "ambiguous",
    "decode_unavailable",
    "missing_retryable",
    "source_changed",
    "source_rejected",
    "archive_failed",
    "insufficient_archive_space",
    "object_too_large",
)
FULL_IMAGE_SUFFIXES = ("_h.dat", "_M.dat", ".dat")
THUMB_IMAGE_SUFFIXES = ("_t_M.dat", "_t.dat")
MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


class ArchiveError(RuntimeError):
    """A privacy-safe attachment archive failure with a stable code."""

    def __init__(self, code):
        super().__init__(str(code))
        self.code = str(code)


@dataclass(frozen=True)
class Resolution:
    status: str
    method: str = ""
    path: str = ""
    decoded: bytes | None = None
    object_name: str = ""


def _safe_object_name(value, fallback="attachment"):
    name = os.path.basename(str(value or "").strip())
    name = re.sub(r"[\x00-\x1f/:\\]+", "_", name).strip(" ._")
    name = re.sub(r"\s+", " ", name)
    name = name or fallback
    if len(name.encode("utf-8")) <= 189:
        return name
    stem, extension = os.path.splitext(name)
    if len(extension.encode("utf-8")) > 32:
        extension = _utf8_prefix(extension, 32)
    suffix = "--" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8] + extension
    stem = _utf8_prefix(stem, 189 - len(suffix.encode("utf-8"))).rstrip(" ._")
    return (stem + suffix) if stem else _utf8_prefix(suffix, 189)


def _utf8_prefix(value, max_bytes):
    result = []
    used = 0
    for char in str(value or ""):
        encoded = char.encode("utf-8")
        if used + len(encoded) > max(0, int(max_bytes)):
            break
        result.append(char)
        used += len(encoded)
    return "".join(result)


def _hash_stream(stream):
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        sha256.update(chunk)
        md5.update(chunk)
    return size, sha256.hexdigest(), md5.hexdigest()


def _hash_path(path):
    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ArchiveError("source_not_regular")
        with os.fdopen(fd, "rb") as source:
            fd = -1
            return _hash_stream(source)
    finally:
        if fd >= 0:
            os.close(fd)


def _write_all(fd, data):
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("short write")
        written += count


def _within(path, root):
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except ValueError:
        return False


def _ensure_private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _declared_hash_matches(declared_hash, sha256, md5):
    declared = str(declared_hash or "").strip().lower()
    if not declared:
        return True
    if len(declared) == 32:
        return declared == md5
    if len(declared) == 64:
        return declared == sha256
    return False


class AttachmentArchive:
    """Resolve catalog mentions and preserve unique bytes in a private CAS."""

    def __init__(
        self,
        db_path=KNOWLEDGE_DB,
        archive_root=None,
        *,
        db_dir="",
        archive_kinds=("file",),
        image_aes_key="",
        obsidian_root=None,
        obsidian_subdir=None,
        max_object_bytes=512 * 1024 * 1024,
        min_free_bytes=1024 * 1024 * 1024,
        retry_base_seconds=300,
        retry_max_seconds=6 * 60 * 60,
        now_func=time.time,
    ):
        self.db_path = os.path.abspath(os.path.expanduser(db_path))
        self.archive_root = os.path.abspath(os.path.expanduser(
            archive_root or os.path.join(DATA_DIR, "attachment_archive")
        ))
        self.db_dir = os.path.abspath(os.path.expanduser(db_dir)) if db_dir else ""
        self.archive_kinds = tuple(
            kind for kind in dict.fromkeys(str(item).lower() for item in archive_kinds)
            if kind in {"file", "image"}
        )
        self.image_aes_key = str(image_aes_key or "")
        self.obsidian_root = obsidian_root
        self.obsidian_subdir = obsidian_subdir
        self.max_object_bytes = max(1, int(max_object_bytes))
        self.min_free_bytes = max(0, int(min_free_bytes))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, int(retry_max_seconds))
        self.now_func = now_func

    @classmethod
    def from_config(cls, config, now_func=time.time):
        return cls(
            config.get("monitor_knowledge_db") or KNOWLEDGE_DB,
            config.get("attachment_archive_root"),
            db_dir=config.get("db_dir") or "",
            archive_kinds=config.get("attachment_archive_kinds") or ("file",),
            image_aes_key=config.get("image_aes_key") or "",
            obsidian_root=config.get("monitor_obsidian_root"),
            obsidian_subdir=config.get("monitor_obsidian_subdir"),
            max_object_bytes=config.get("attachment_archive_max_object_bytes", 512 * 1024 * 1024),
            min_free_bytes=config.get("attachment_archive_min_free_bytes", 1024 * 1024 * 1024),
            retry_base_seconds=config.get("attachment_archive_retry_base_seconds", 300),
            retry_max_seconds=config.get("attachment_archive_retry_max_seconds", 6 * 60 * 60),
            now_func=now_func,
        )

    @property
    def file_cache_root(self):
        return os.path.join(os.path.dirname(self.db_dir), "msg", "file") if self.db_dir else ""

    @property
    def image_cache_root(self):
        return os.path.join(os.path.dirname(self.db_dir), "msg", "attach") if self.db_dir else ""

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @property
    def cas_catalog_path(self):
        return os.path.join(self.archive_root, "cas_catalog.db")

    def _cas_connect(self):
        conn = sqlite3.connect(self.cas_catalog_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_cas_catalog(self):
        conn = self._cas_connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cas_objects (
                    sha256 TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    object_relpath TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cas_sources (
                    source_message_id TEXT NOT NULL,
                    resource_index INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resolution_method TEXT NOT NULL,
                    object_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(source_message_id, resource_index),
                    FOREIGN KEY(object_sha256) REFERENCES cas_objects(sha256)
                );
                """
            )
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(self.cas_catalog_path, 0o600)
        except OSError:
            pass

    def _record_cas_object(self, digest, size, relpath, original_name):
        self._ensure_cas_catalog()
        now = self.now_func()
        conn = self._cas_connect()
        try:
            conn.execute(
                """
                INSERT INTO cas_objects(
                    sha256, size, object_relpath, original_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    size = excluded.size,
                    object_relpath = excluded.object_relpath,
                    updated_at = excluded.updated_at
                """,
                (
                    digest,
                    int(size),
                    str(relpath),
                    _safe_object_name(original_name),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _record_cas_source(self, mention, digest, resolution_method):
        source_message_id = str(mention.get("source_message_id") or "")
        if not source_message_id:
            return
        now = self.now_func()
        conn = self._cas_connect()
        try:
            conn.execute(
                """
                INSERT INTO cas_sources(
                    source_message_id, resource_index, kind, original_name,
                    status, resolution_method, object_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'ready_local', ?, ?, ?, ?)
                ON CONFLICT(source_message_id, resource_index) DO UPDATE SET
                    kind = excluded.kind,
                    original_name = excluded.original_name,
                    status = excluded.status,
                    resolution_method = excluded.resolution_method,
                    object_sha256 = excluded.object_sha256,
                    updated_at = excluded.updated_at
                """,
                (
                    source_message_id[:80],
                    int(mention.get("resource_index") or 0),
                    str(mention.get("kind") or "file")[:20],
                    _safe_object_name(mention.get("original_name") or "attachment"),
                    str(resolution_method or "shared_cas")[:80],
                    digest,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def cas_catalog_snapshot(self):
        """Return provider-neutral CAS metadata without creating catalog state."""
        if not os.path.isfile(self.cas_catalog_path):
            return None
        conn = self._cas_connect()
        try:
            conn.execute("BEGIN")
            objects = [
                dict(row)
                for row in conn.execute(
                    "SELECT sha256, size, object_relpath, original_name FROM cas_objects ORDER BY sha256"
                )
            ]
            sources = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT source_message_id, resource_index, kind, original_name,
                           status, resolution_method, object_sha256
                    FROM cas_sources
                    ORDER BY object_sha256, source_message_id, resource_index
                    """
                )
            ]
            conn.commit()
            return {"objects": objects, "sources": sources}
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_layout(self):
        """Create private archive directories and catalogs without deleting data."""
        _ensure_private_dir(self.archive_root)
        _ensure_private_dir(os.path.join(self.archive_root, "objects"))
        _ensure_private_dir(os.path.join(self.archive_root, "objects", "sha256"))
        _ensure_private_dir(os.path.join(self.archive_root, "tmp"))
        self._ensure_cas_catalog()

    def _enforce_object_policy(self, size):
        size = max(0, int(size))
        if size > self.max_object_bytes:
            raise ArchiveError("object_too_large")
        free = shutil.disk_usage(self.archive_root).free
        if free - size < self.min_free_bytes:
            raise ArchiveError("insufficient_archive_space")

    def _recover_partials_locked(self):
        """Remove worker partials only while locked; final objects are immutable."""
        tmp_root = os.path.join(self.archive_root, "tmp")
        if not os.path.isdir(tmp_root):
            return 0
        removed = 0
        for entry in os.scandir(tmp_root):
            if not entry.name.startswith(".partial-"):
                continue
            try:
                if entry.is_file(follow_symlinks=False):
                    os.unlink(entry.path)
                    removed += 1
            except OSError:
                continue
        return removed

    @contextmanager
    def _worker_lock(self):
        self.ensure_layout()
        lock_path = os.path.join(self.archive_root, ".archive.lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ArchiveError("worker_busy") from exc
                raise
            self._recover_partials_locked()
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _regular_source(path, allowed_root):
        if not path or not allowed_root or not _within(path, allowed_root):
            raise ArchiveError("source_outside_cache_root")
        try:
            source_stat = os.lstat(path)
        except OSError as exc:
            raise ArchiveError("source_missing") from exc
        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
            raise ArchiveError("source_not_regular")
        return source_stat

    def _file_candidates(self, mention):
        root = self.file_cache_root
        month = str(mention["source_month"] or "")
        name = os.path.basename(str(mention["original_name"] or ""))
        if not root or not month or not name:
            return [], False
        directory = os.path.join(root, month)
        if not os.path.isdir(directory) or not _within(directory, root):
            return [], False
        stem, suffix = os.path.splitext(name)
        variant = re.compile(rf"^{re.escape(stem)} ?\([1-9][0-9]*\){re.escape(suffix)}$")
        candidates = []
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            return [], False
        rejected = False
        for entry in entries:
            if entry.name != name and not variant.fullmatch(entry.name):
                continue
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    rejected = True
                    continue
                if not _within(entry.path, root):
                    rejected = True
                    continue
                if mention["declared_size"] is not None and entry.stat(follow_symlinks=False).st_size != int(mention["declared_size"]):
                    continue
            except OSError:
                continue
            candidates.append(entry.path)
        return candidates, rejected

    def resolve_file(self, mention):
        candidates, rejected = self._file_candidates(mention)
        if not candidates:
            if rejected:
                return Resolution("source_rejected", "file_nonregular_or_symlink")
            return Resolution("missing_retryable", "file_exact_or_variant")

        matched = []
        for path in candidates:
            try:
                size, sha256, md5 = _hash_path(path)
            except (ArchiveError, OSError):
                continue
            if mention["declared_size"] is not None and size != int(mention["declared_size"]):
                continue
            if not _declared_hash_matches(mention["declared_hash"], sha256, md5):
                continue
            matched.append((path, sha256))
        if not matched:
            return Resolution("missing_retryable", "file_metadata_mismatch")

        digests = {item[1] for item in matched}
        if len(digests) > 1:
            return Resolution("ambiguous", "file_distinct_duplicate_variants")

        exact_name = os.path.basename(str(mention["original_name"] or ""))
        chosen = min(matched, key=lambda item: (os.path.basename(item[0]) != exact_name, item[0]))[0]
        method = "unique_candidate"
        if len(matched) > 1:
            method = "equivalent_duplicates"
        return Resolution("resolved", method, chosen, object_name=exact_name)

    def resolve_image(self, mention):
        declared_hash = str(mention["declared_hash"] or "").lower()
        source_chat = str(mention["source_chat_username"] or "")
        month = str(mention["source_month"] or "")
        if not re.fullmatch(r"[0-9a-f]{32}", declared_hash) or not source_chat or not month:
            return Resolution("missing_retryable", "image_hash_chat_month")

        root = self.image_cache_root
        if not root:
            return Resolution("missing_retryable", "image_cache_root")
        chat_hash = hashlib.md5(source_chat.encode()).hexdigest()
        image_dir = os.path.join(root, chat_hash, month, "Img")
        if not os.path.isdir(image_dir) or not _within(image_dir, root):
            return Resolution("missing_retryable", "image_hash_chat_month")

        saw_undecodable = False
        for is_thumb, suffixes in ((False, FULL_IMAGE_SUFFIXES), (True, THUMB_IMAGE_SUFFIXES)):
            for suffix in suffixes:
                path = os.path.join(image_dir, declared_hash + suffix)
                if not os.path.lexists(path):
                    continue
                try:
                    data = self._read_stable_source(path, root)
                    decoded = decode_wechat_image_data(data, self.image_aes_key)
                except ArchiveError as exc:
                    if exc.code == "source_changed":
                        return Resolution("source_changed", "image_source_changed")
                    if exc.code == "object_too_large":
                        return Resolution("object_too_large", "image_size_policy")
                    continue
                except OSError:
                    continue
                if decoded is None:
                    saw_undecodable = True
                    continue
                mime = detect_mime(decoded)
                extension = MIME_EXTENSIONS.get(mime, "img")
                status = "thumbnail_only" if is_thumb else "original_archived"
                method = "image_hash_thumbnail" if is_thumb else "image_hash_original"
                return Resolution(
                    status,
                    method,
                    path,
                    decoded=decoded,
                    object_name=f"{declared_hash}.{extension}",
                )
        if saw_undecodable:
            return Resolution("decode_unavailable", "image_v2_decode")
        return Resolution("missing_retryable", "image_hash_chat_month")

    def _read_stable_source(self, path, allowed_root):
        self._regular_source(path, allowed_root)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ArchiveError("source_not_regular")
            if before.st_size > self.max_object_bytes:
                raise ArchiveError("object_too_large")
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(fd)
            if any(
                getattr(before, field) != getattr(after, field)
                for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
            ):
                raise ArchiveError("source_changed")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def resolve(self, mention):
        if mention["kind"] == "file":
            return self.resolve_file(mention)
        if mention["kind"] == "image":
            return self.resolve_image(mention)
        return Resolution("source_rejected", "unsupported_kind")

    def _existing_object(self, digest):
        prefix_dir = os.path.join(self.archive_root, "objects", "sha256", digest[:2])
        for path in sorted(glob.glob(os.path.join(prefix_dir, digest + "--*"))):
            try:
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                size, actual, _ = _hash_path(path)
                if actual != digest:
                    raise ArchiveError("object_corrupt")
                return path, size
            except (ArchiveError, OSError):
                continue
        return None

    def _finalize_temp(self, temp_path, digest, size, object_name):
        existing = self._existing_object(digest)
        if existing:
            os.unlink(temp_path)
            return existing[0], existing[1]

        prefix_dir = os.path.join(self.archive_root, "objects", "sha256", digest[:2])
        _ensure_private_dir(prefix_dir)
        final_path = os.path.join(prefix_dir, f"{digest}--{_safe_object_name(object_name)}")
        if os.path.lexists(final_path):
            existing_size, existing_digest, _ = _hash_path(final_path)
            if existing_digest != digest:
                raise ArchiveError("object_corrupt")
            os.unlink(temp_path)
            return final_path, existing_size
        os.replace(temp_path, final_path)
        os.chmod(final_path, 0o600)
        _fsync_dir(prefix_dir)
        return final_path, size

    def store_source(self, path, allowed_root, object_name, declared_size=None, declared_hash=""):
        self._regular_source(path, allowed_root)
        if declared_size is not None and int(declared_size) > self.max_object_bytes:
            raise ArchiveError("object_too_large")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            source_fd = os.open(path, flags)
        except OSError as exc:
            raise ArchiveError("source_open_failed") from exc
        temp_fd = None
        temp_path = ""
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ArchiveError("source_not_regular")
            self._enforce_object_policy(before.st_size)
            temp_fd, temp_path = tempfile.mkstemp(prefix=".partial-", dir=os.path.join(self.archive_root, "tmp"))
            os.fchmod(temp_fd, 0o600)
            sha256 = hashlib.sha256()
            md5 = hashlib.md5()
            size = 0
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                _write_all(temp_fd, chunk)
                size += len(chunk)
                sha256.update(chunk)
                md5.update(chunk)
            os.fsync(temp_fd)
            after = os.fstat(source_fd)
            stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
            if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
                raise ArchiveError("source_changed")
            if declared_size is not None and size != int(declared_size):
                raise ArchiveError("declared_size_mismatch")
            digest = sha256.hexdigest()
            if not _declared_hash_matches(declared_hash, digest, md5.hexdigest()):
                raise ArchiveError("declared_hash_mismatch")
            os.close(temp_fd)
            temp_fd = None
            final_path, size = self._finalize_temp(temp_path, digest, size, object_name)
            temp_path = ""
            relpath = os.path.relpath(final_path, self.archive_root)
            self._record_cas_object(digest, size, relpath, object_name)
            return digest, size, relpath
        finally:
            os.close(source_fd)
            if temp_fd is not None:
                os.close(temp_fd)
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def store_bytes(self, data, object_name):
        self._enforce_object_policy(len(data))
        temp_fd, temp_path = tempfile.mkstemp(prefix=".partial-", dir=os.path.join(self.archive_root, "tmp"))
        try:
            os.fchmod(temp_fd, 0o600)
            _write_all(temp_fd, data)
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = -1
            digest = hashlib.sha256(data).hexdigest()
            final_path, size = self._finalize_temp(temp_path, digest, len(data), object_name)
            temp_path = ""
            relpath = os.path.relpath(final_path, self.archive_root)
            self._record_cas_object(digest, size, relpath, object_name)
            return digest, size, relpath
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def preserve_file_mention(self, mention):
        """Resolve one selected-chat file into the shared CAS without Knowledge state."""
        mention = dict(mention)
        if str(mention.get("kind") or "") != "file":
            return {"status": "source_rejected", "resolution_method": "unsupported_kind"}
        try:
            with self._worker_lock():
                resolution = self.resolve_file(mention)
                if resolution.status != "resolved":
                    return {
                        "status": resolution.status,
                        "resolution_method": resolution.method,
                    }
                digest, size, relpath = self.store_source(
                    resolution.path,
                    self.file_cache_root,
                    resolution.object_name or mention.get("original_name") or "attachment",
                    declared_size=mention.get("declared_size"),
                    declared_hash=mention.get("declared_hash") or "",
                )
                self._record_cas_source(mention, digest, resolution.method)
                return {
                    "status": "ready_local",
                    "resolution_method": resolution.method,
                    "sha256": digest,
                    "size": size,
                    "object_relpath": relpath,
                }
        except ArchiveError as exc:
            status = {
                "worker_busy": "retry_wait",
                "insufficient_archive_space": "insufficient_local_space",
            }.get(exc.code, exc.code)
            return {
                "status": status,
                "resolution_method": "shared_cas",
                "error_code": exc.code,
            }
        except (OSError, ValueError, sqlite3.Error):
            return {
                "status": "retry_wait",
                "resolution_method": "shared_cas",
                "error_code": "archive_failed",
            }

    def _record_failure(self, conn, mention, status, method, code, now):
        attempt_count = int(mention["attempt_count"] or 0) + 1
        if status in AUTO_RETRY_STATUSES:
            delay = min(
                self.retry_base_seconds * (2 ** max(0, attempt_count - 1)),
                self.retry_max_seconds,
            )
            next_retry_at = now + delay
        else:
            next_retry_at = 0
        conn.execute(
            """
            UPDATE attachment_mentions
            SET status = ?, resolution_method = ?, last_error_code = ?,
                attempt_count = ?, next_retry_at = ?, updated_at = ?
            WHERE mention_id = ?
            """,
            (status, method, code, attempt_count, next_retry_at, now, mention["mention_id"]),
        )
        conn.execute(
            """
            INSERT INTO attachment_attempts(
                mention_id, action, status, resolution_method, error_code, created_at
            ) VALUES (?, 'archive', ?, ?, ?, ?)
            """,
            (mention["mention_id"], status, method, code, now),
        )

    @staticmethod
    def _record_success(conn, mention, resolution, digest, size, relpath, now):
        conn.execute(
            """
            INSERT INTO attachment_objects(sha256, size, object_relpath, original_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                size = excluded.size,
                object_relpath = excluded.object_relpath
            """,
            (digest, size, relpath, mention["original_name"], now),
        )
        status = resolution.status if resolution.status == "thumbnail_only" else "original_archived"
        conn.execute(
            """
            UPDATE attachment_mentions
            SET status = ?, resolution_method = ?, object_sha256 = ?,
                last_error_code = '', attempt_count = attempt_count + 1,
                next_retry_at = 0, updated_at = ?
            WHERE mention_id = ?
            """,
            (status, resolution.method, digest, now, mention["mention_id"]),
        )
        conn.execute(
            """
            INSERT INTO attachment_attempts(
                mention_id, action, status, resolution_method, error_code, created_at
            ) VALUES (?, 'archive', ?, ?, '', ?)
            """,
            (mention["mention_id"], status, resolution.method, now),
        )
        return status

    def _rewrite_topics(self, topic_ids):
        if not self.obsidian_root:
            return
        store = KnowledgeStore(
            self.db_path,
            self.obsidian_root,
            self.obsidian_subdir or "微信群聊/关注推送",
            attachment_archive_root=self.archive_root,
        )
        for topic_id in sorted(set(topic_ids)):
            try:
                store.rewrite_topic_markdown(topic_id)
            except (OSError, sqlite3.Error, ValueError):
                continue

    def _request_wake(self):
        conn = self._connect()
        try:
            now = self.now_func()
            conn.execute(
                """
                UPDATE attachment_worker_state
                SET wake_generation = wake_generation + 1, updated_at = ?
                WHERE worker_name = 'attachment_archive'
                """,
                (now,),
            )
            row = conn.execute(
                """
                SELECT wake_generation
                FROM attachment_worker_state
                WHERE worker_name = 'attachment_archive'
                """
            ).fetchone()
            conn.commit()
            return int(row["wake_generation"])
        finally:
            conn.close()

    @staticmethod
    def _worker_generation(conn):
        row = conn.execute(
            """
            SELECT wake_generation, drained_generation
            FROM attachment_worker_state
            WHERE worker_name = 'attachment_archive'
            """
        ).fetchone()
        return int(row["wake_generation"]), int(row["drained_generation"])

    def _mark_drained(self, conn):
        wake_generation, _ = self._worker_generation(conn)
        conn.execute(
            """
            UPDATE attachment_worker_state
            SET drained_generation = ?, updated_at = ?
            WHERE worker_name = 'attachment_archive'
            """,
            (wake_generation, self.now_func()),
        )
        conn.commit()
        latest_wake, drained = self._worker_generation(conn)
        return latest_wake == drained

    def _wake_pending(self):
        conn = self._connect()
        try:
            wake_generation, drained_generation = self._worker_generation(conn)
            return wake_generation > drained_generation
        finally:
            conn.close()

    def process_pending(self, limit=50):
        if not self.archive_kinds:
            return {"state": "disabled", "processed": 0, "archived": 0, "failed": 0}
        if not os.path.exists(self.db_path):
            return {"state": "knowledge_db_missing", "processed": 0, "archived": 0, "failed": 0}

        try:
            wake_generation = self._request_wake()
        except sqlite3.Error:
            return {"state": "catalog_unavailable", "processed": 0, "archived": 0, "failed": 0}

        batch_size = max(1, int(limit))
        processed = 0
        archived = 0
        failed = 0
        acquired_once = False
        while True:
            try:
                lock = self._worker_lock()
                lock.__enter__()
            except ArchiveError as exc:
                if acquired_once:
                    return {
                        "state": "healthy",
                        "processed": processed,
                        "archived": archived,
                        "failed": failed,
                    }
                return {
                    "state": exc.code,
                    "processed": 0,
                    "archived": 0,
                    "failed": 0,
                    "wake_generation": wake_generation,
                }
            acquired_once = True
            touched_topics = []
            try:
                conn = self._connect()
                try:
                    placeholders = ",".join("?" for _ in self.archive_kinds)
                    retry_placeholders = ",".join("?" for _ in AUTO_RETRY_STATUSES)
                    while True:
                        now = self.now_func()
                        rows = conn.execute(
                            f"""
                            SELECT m.*, COALESCE(e.source_chat_username, '') AS source_chat_username
                            FROM attachment_mentions m
                            LEFT JOIN events e ON e.event_id = m.event_id
                            WHERE m.kind IN ({placeholders})
                              AND (
                                m.status = 'pending'
                                OR (m.status IN ({retry_placeholders}) AND m.next_retry_at <= ?)
                              )
                            ORDER BY CASE WHEN m.status = 'pending' THEN 0 ELSE 1 END,
                                     m.next_retry_at,
                                     m.mention_id
                            LIMIT ?
                            """,
                            (*self.archive_kinds, *AUTO_RETRY_STATUSES, now, batch_size),
                        ).fetchall()
                        if not rows:
                            if self._mark_drained(conn):
                                break
                            continue

                        for mention in rows:
                            now = self.now_func()
                            if (
                                mention["declared_size"] is not None
                                and int(mention["declared_size"]) > self.max_object_bytes
                            ):
                                resolution = Resolution("object_too_large", "declared_size_policy")
                            else:
                                resolution = self.resolve(mention)
                            if resolution.status not in {"resolved", "original_archived", "thumbnail_only"}:
                                self._record_failure(
                                    conn,
                                    mention,
                                    resolution.status,
                                    resolution.method,
                                    resolution.status,
                                    now,
                                )
                                conn.commit()
                                failed += 1
                                processed += 1
                                touched_topics.append(mention["topic_id"])
                                continue
                            try:
                                if resolution.decoded is not None:
                                    digest, size, relpath = self.store_bytes(
                                        resolution.decoded,
                                        resolution.object_name,
                                    )
                                else:
                                    allowed_root = self.file_cache_root if mention["kind"] == "file" else self.image_cache_root
                                    digest, size, relpath = self.store_source(
                                        resolution.path,
                                        allowed_root,
                                        resolution.object_name or mention["original_name"],
                                        declared_size=mention["declared_size"],
                                        declared_hash=mention["declared_hash"],
                                    )
                                self._record_success(conn, mention, resolution, digest, size, relpath, now)
                                conn.commit()
                                archived += 1
                            except (ArchiveError, OSError, ValueError, sqlite3.Error) as exc:
                                code = exc.code if isinstance(exc, ArchiveError) else "archive_failed"
                                status = code if code in {
                                    "source_changed",
                                    "source_rejected",
                                    "object_too_large",
                                    "insufficient_archive_space",
                                } else "archive_failed"
                                self._record_failure(conn, mention, status, resolution.method, code, now)
                                conn.commit()
                                failed += 1
                            processed += 1
                            touched_topics.append(mention["topic_id"])
                finally:
                    conn.close()
            finally:
                lock.__exit__(None, None, None)

            self._rewrite_topics(topic_id for topic_id in touched_topics if topic_id is not None)
            if self._wake_pending():
                continue
            return {
                "state": "healthy",
                "processed": processed,
                "archived": archived,
                "failed": failed,
            }

    def status(self):
        cas_snapshot = None
        cas_catalog_error = False
        try:
            cas_snapshot = self.cas_catalog_snapshot()
        except sqlite3.Error:
            cas_catalog_error = True
        if cas_catalog_error:
            return {
                "state": "catalog_unavailable",
                "counts": {},
                "objects": 0,
                "object_bytes": 0,
            }
        cas_objects = {
            row["sha256"]: (int(row["size"]), row["object_relpath"])
            for row in (cas_snapshot or {}).get("objects", [])
        }
        if not os.path.exists(self.db_path):
            return {
                "state": "knowledge_db_missing",
                "counts": {},
                "objects": len(cas_objects),
                "object_bytes": sum(value[0] for value in cas_objects.values()),
            }
        conn = self._connect()
        try:
            counts = {
                row["status"]: int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM attachment_mentions GROUP BY status"
                )
            }
            for row in conn.execute("SELECT sha256, size, object_relpath FROM attachment_objects"):
                cas_objects.setdefault(
                    str(row["sha256"]),
                    (int(row["size"]), str(row["object_relpath"])),
                )
            return {
                "state": "healthy",
                "counts": counts,
                "objects": len(cas_objects),
                "object_bytes": sum(value[0] for value in cas_objects.values()),
            }
        except sqlite3.Error:
            return {"state": "catalog_unavailable", "counts": {}, "objects": 0, "object_bytes": 0}
        finally:
            conn.close()

    def retry(self, mention_ids=(), statuses=MANUAL_RETRY_STATUSES):
        if not os.path.exists(self.db_path):
            return 0
        conn = self._connect()
        try:
            clauses = ["object_sha256 = ''"]
            params = []
            if mention_ids:
                ids = [int(value) for value in mention_ids]
                clauses.append("mention_id IN (" + ",".join("?" for _ in ids) + ")")
                params.extend(ids)
            else:
                status_values = tuple(statuses)
                clauses.append("status IN (" + ",".join("?" for _ in status_values) + ")")
                params.extend(status_values)
            where = " AND ".join(clauses)
            topic_ids = [
                row[0]
                for row in conn.execute(
                    f"SELECT DISTINCT topic_id FROM attachment_mentions WHERE {where}",
                    tuple(params),
                )
                if row[0] is not None
            ]
            cursor = conn.execute(
                f"""
                UPDATE attachment_mentions
                SET status = 'pending', last_error_code = '', attempt_count = 0,
                    next_retry_at = 0, updated_at = ?
                WHERE {where}
                """,
                (self.now_func(), *params),
            )
            conn.commit()
            changed = int(cursor.rowcount)
        finally:
            conn.close()
        if changed:
            self._rewrite_topics(topic_ids)
        return changed

    def plan_backfill(self):
        if not os.path.exists(self.db_path):
            return {"events_scanned": 0, "mentions_found": 0, "new_mentions": 0}
        conn = self._connect()
        try:
            events_scanned = 0
            found = 0
            new_mentions = 0
            for event in conn.execute(
                "SELECT event_id, topic_id, files_json, window_end, created_at FROM events ORDER BY event_id"
            ):
                events_scanned += 1
                try:
                    files = json.loads(event["files_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    files = []
                if not isinstance(files, list):
                    continue
                for item in files:
                    if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                        continue
                    found += 1
                    exists = conn.execute(
                        """
                        SELECT 1 FROM attachment_mentions
                        WHERE event_id = ? AND kind = 'file'
                          AND lower(original_name) = lower(?)
                          AND source_month = ?
                        LIMIT 1
                        """,
                        (event["event_id"], item.get("name"), item.get("month") or ""),
                    ).fetchone()
                    if not exists:
                        new_mentions += 1
            return {
                "events_scanned": events_scanned,
                "mentions_found": found,
                "new_mentions": new_mentions,
            }
        finally:
            conn.close()

    def apply_backfill(self):
        conn = self._connect()
        inserted = 0
        topic_ids = set()
        try:
            for event in conn.execute(
                "SELECT event_id, topic_id, files_json, window_end, created_at FROM events ORDER BY event_id"
            ).fetchall():
                try:
                    files = json.loads(event["files_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    files = []
                if not isinstance(files, list):
                    continue
                for resource_index, item in enumerate(files):
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    month = str(item.get("month") or "")[:7]
                    if not name:
                        continue
                    exists = conn.execute(
                        """
                        SELECT 1 FROM attachment_mentions
                        WHERE event_id = ? AND kind = 'file'
                          AND lower(original_name) = lower(?)
                          AND source_month = ?
                        LIMIT 1
                        """,
                        (event["event_id"], name, month),
                    ).fetchone()
                    if exists:
                        continue
                    identity = f"attachment-backfill-v1\0{event['event_id']}\0{name}\0{month}"
                    source_message_id = "wgbackfill_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
                    now = self.now_func()
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO attachment_mentions(
                            event_id, topic_id, source_message_id, resource_index, kind,
                            original_name, extension, source_month, source_time,
                            source_timestamp, source_sender, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'file', ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            event["event_id"],
                            event["topic_id"],
                            source_message_id,
                            resource_index,
                            name[:240],
                            os.path.splitext(name)[1].lstrip(".")[:20],
                            month,
                            str(item.get("time") or event["window_end"] or "")[:40],
                            float(event["created_at"] or 0),
                            str(item.get("sender") or "")[:80],
                            now,
                            now,
                        ),
                    )
                    inserted += int(cursor.rowcount > 0)
                    if cursor.rowcount > 0 and event["topic_id"] is not None:
                        topic_ids.add(int(event["topic_id"]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if inserted:
            self._rewrite_topics(topic_ids)
        return inserted


def process_pending_from_config(config, limit=50):
    """Best-effort post-commit consumer used by the monitor background lane."""
    if not config.get("attachment_archive_enabled", False):
        return {"state": "disabled", "processed": 0, "archived": 0, "failed": 0}
    store = KnowledgeStore.from_config(config)
    conn = store.connect()
    conn.close()
    return AttachmentArchive.from_config(config).process_pending(limit=limit)
