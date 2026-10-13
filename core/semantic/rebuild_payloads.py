"""Private, immutable rebuild payloads and bounded conversion of inline journals.

Only the rebuild journal owns this store. References bind a run, byte length and
content digest; no caller-supplied file path is followed. The old writer emitted
sorted canonical JSON. Conversion accepts that ordering and parses one scalar at
a time, never the entire document or inventory. It retains the exact old file.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


STORAGE_SCHEMA = "we-groupchat-obsidian.semantic-rebuild.v2"
REF_KEY = "$rebuild_payload"
MAX_JOURNAL = 16 * 1024 * 1024
MAX_SCALAR = 64 * 1024 * 1024
_SPECIAL = re.compile(r'["\\]')
_ENCODER = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)


def fail(code):
    from .rebuild import RebuildError
    raise RebuildError(code)


def is_ref(value):
    return isinstance(value, dict) and REF_KEY in value


def inventory_path(path):
    return (path in (("plan", "legacy_inventory"), ("verification", "legacy_inventory")) or
            len(path) == 3 and path[0] in {"freeze_receipts", "steps"} and path[-1] == "legacy_inventory")


def external_path(path):
    return (inventory_path(path) or
            len(path) == 2 and path[0] in {"freeze_receipts", "steps"} or
            len(path) == 3 and path[0] == "capture_attempts" and path[-1] == "receipt" or
            path == ("verification",))


@contextmanager
def opened(path):
    from .rebuild import safe_path
    path = safe_path(Path(path).parent, Path(path).name)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            fail("rebuild_not_regular")
        yield stream


def identity(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


class Payloads:
    def __init__(self, run_root, run_id):
        self.root, self.run_id = Path(run_root), run_id
        self._validated = {}

    @contextmanager
    def writer(self):
        from .rebuild import safe_path
        from core.platform import create_platform_services
        folder = safe_path(self.root, "payloads")
        create_platform_services().require("private_storage").ensure_directory(str(folder))
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=folder)
        digest, length, reference = hashlib.sha256(), 0, {}
        try:
            with os.fdopen(fd, "wb") as output:
                def emit(text):
                    nonlocal length
                    data = text.encode("utf-8")
                    output.write(data)
                    digest.update(data)
                    length += len(data)
                yield emit, reference
                output.flush()
                os.fsync(output.fileno())
            sha = digest.hexdigest()
            target = safe_path(folder, sha + ".json")
            reference.update({REF_KEY: sha, "bytes": length, "run_id": self.run_id})
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.check(reference)
            self._validated[sha] = identity(target.stat())
            # Publish payload bytes durably before a journal can refer to them.
            directory = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(temporary)

    def put(self, value):
        if is_ref(value):
            self.path(value)  # Validate binding without rereading immutable bytes.
            return value
        with self.writer() as (emit, reference):
            for chunk in _ENCODER.iterencode(value):
                emit(chunk)
        return reference

    def path(self, reference):
        from .rebuild import safe_path
        if (set(reference) != {REF_KEY, "bytes", "run_id"} or
                reference["run_id"] != self.run_id or
                not isinstance(reference[REF_KEY], str) or
                not re.fullmatch(r"[0-9a-f]{64}", reference[REF_KEY]) or
                type(reference["bytes"]) is not int or reference["bytes"] < 0):
            fail("rebuild_payload_binding_invalid")
        return safe_path(self.root, "payloads/" + reference[REF_KEY] + ".json")

    def check(self, reference):
        path = self.path(reference)
        digest, length = hashlib.sha256(), 0
        try:
            current = identity(path.stat())
            if self._validated.get(reference[REF_KEY]) == current and current[2] == reference["bytes"]:
                return
            with opened(path) as source:
                before = identity(os.fstat(source.fileno()))
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    length += len(chunk)
                if before != identity(os.fstat(source.fileno())) or before != identity(path.stat()):
                    fail("rebuild_payload_changed")
        except FileNotFoundError:
            fail("rebuild_payload_missing")
        if digest.hexdigest() != reference[REF_KEY] or length != reference["bytes"]:
            fail("rebuild_payload_changed")
        self._validated[reference[REF_KEY]] = before

    def check_refs(self, value):
        if is_ref(value):
            self.check(value)
        elif isinstance(value, dict):
            for child in value.values():
                self.check_refs(child)
        elif isinstance(value, list):
            for child in value:
                self.check_refs(child)

    def get(self, value):
        if not is_ref(value):
            return value
        path = self.path(value)
        # The explicit consumer needs this payload. Hash and decode the same fd;
        # phase updates never enter this path. File size is checked before load.
        try:
            with opened(path) as source:
                before = identity(os.fstat(source.fileno()))
                if before[2] != value["bytes"]:
                    fail("rebuild_payload_changed")
                raw = source.read()
                if (before != identity(os.fstat(source.fileno())) or
                        before != identity(path.stat()) or
                        hashlib.sha256(raw).hexdigest() != value[REF_KEY]):
                    fail("rebuild_payload_changed")
                return json.loads(raw)
        except FileNotFoundError:
            fail("rebuild_payload_missing")

    def compact(self, value, path=()):
        if is_ref(value):
            self.path(value)
            return value
        # Inventories are immutable documents, not recursively fragmented rows.
        if inventory_path(path):
            return self.put(value)
        if isinstance(value, dict):
            value = {k: self.compact(v, path + (k,)) for k, v in value.items()}
        elif isinstance(value, list):
            value = [self.compact(v, path + (i,)) for i, v in enumerate(value)]
        return self.put(value) if external_path(path) else value

    def expand(self, value, path=()):
        value = self.get(value)
        if inventory_path(path):
            return value
        if isinstance(value, dict):
            return {k: self.expand(v, path + (k,)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.expand(v, path + (i,)) for i, v in enumerate(value)]
        return value


class InlineReader:
    """Canonical JSON grammar with chunked input and per-scalar memory bounds."""
    def __init__(self, source, payloads):
        self.source, self.payloads = source, payloads
        self.buffer, self.pos = "", 0
        self.taps, self.plan_digest = [], None
        self.metadata_bytes = 0

    def retain(self, text):
        self.metadata_bytes += len(text.encode("utf-8"))
        if self.metadata_bytes > MAX_JOURNAL:
            fail("rebuild_journal_metadata_limit")

    def peek(self):
        if self.pos == len(self.buffer):
            self.buffer, self.pos = self.source.read(64 * 1024), 0
        return self.buffer[self.pos:self.pos + 1]

    def whitespace(self):
        while self.peek() and self.peek() in " \n\r\t":
            self.pos += 1

    def token(self, text):
        data = text.encode("utf-8")
        for tap in self.taps:
            tap.update(data)

    def char(self, expected):
        self.whitespace()
        if self.peek() != expected:
            fail("rebuild_corrupt_json")
        self.pos += 1
        self.token(expected)

    def string(self):
        self.whitespace()
        if self.peek() != '"':
            fail("rebuild_corrupt_json")
        self.pos += 1
        parts, size = ['"'], 1
        while True:
            if not self.peek():
                fail("rebuild_corrupt_json")
            match = _SPECIAL.search(self.buffer, self.pos)
            end = match.end() if match else len(self.buffer)
            piece = self.buffer[self.pos:end]
            parts.append(piece)
            size += len(piece.encode("utf-8"))
            self.pos = end
            if size > MAX_SCALAR:
                fail("rebuild_journal_scalar_limit")
            if not match:
                continue
            if piece[-1] == '"':
                break
            escaped = self.peek()
            if not escaped:
                fail("rebuild_corrupt_json")
            parts.append(escaped)
            size += 1
            self.pos += 1
        try:
            value = json.loads("".join(parts))
        except (ValueError, UnicodeError):
            fail("rebuild_corrupt_json")
        encoded = _ENCODER.encode(value)
        self.token(encoded)
        return value, encoded

    def value(self, path=(), emit=None, external=True):
        # Hash the ORIGINAL plan, before compacting its inventory into a ref.
        plan_tap = hashlib.sha256() if path == ("plan",) else None
        if plan_tap:
            self.taps.append(plan_tap)
        try:
            if external and external_path(path):
                with self.payloads.writer() as (target, reference):
                    self.value(path, target, external=False)
                if emit:
                    emit(_ENCODER.encode(reference))
                else:
                    self.retain(_ENCODER.encode(reference))
                return reference
            self.whitespace()
            start = self.peek()
            if start in ('{', '['):
                mapping = start == '{'
                close = '}' if mapping else ']'
                result = {} if mapping else []
                self.char(start)
                if emit:
                    emit(start)
                else:
                    self.retain(start + close)
                self.whitespace()
                index, previous = 0, None
                while self.peek() != close:
                    if index:
                        self.char(',')
                        if emit:
                            emit(',')
                    if mapping:
                        key, encoded = self.string()
                        if previous is not None and key <= previous:
                            fail("rebuild_inline_key_order_invalid")
                        previous = key
                        self.char(':')
                        if emit:
                            emit(encoded + ':')
                        else:
                            self.retain(encoded + ':,')
                    else:
                        key = index
                    # A raw inventory stays one file. Other payloads may refer
                    # to their inventory without embedding it a second time.
                    nested = not inventory_path(path)
                    item = self.value(path + (key,), emit, external=nested)
                    if emit is None:
                        if mapping:
                            result[key] = item
                        else:
                            result.append(item)
                    index += 1
                    self.whitespace()
                self.char(close)
                if emit:
                    emit(close)
                return result
            if start == '"':
                value, encoded = self.string()
            else:
                parts = []
                while self.peek() and self.peek() not in ",]} \t\r\n":
                    parts.append(self.peek())
                    self.pos += 1
                    if len(parts) > 128:
                        fail("rebuild_corrupt_json")
                try:
                    value = json.loads("".join(parts))
                    encoded = _ENCODER.encode(value)
                except (ValueError, UnicodeError):
                    fail("rebuild_corrupt_json")
                self.token(encoded)
            if emit:
                emit(encoded)
            else:
                self.retain(encoded)
            return value
        finally:
            if plan_tap:
                self.taps.remove(plan_tap)
                self.plan_digest = plan_tap.hexdigest()


def convert_inline(path, payloads):
    """Called under the run lock. Original file survives any partial conversion."""
    import io
    from .rebuild import SCHEMA, fingerprint, write_json, safe_path
    with opened(path) as source:
        before = identity(os.fstat(source.fileno()))
        reader = InlineReader(io.TextIOWrapper(source, encoding="utf-8"), payloads)
        document = reader.value()
        reader.whitespace()
        if reader.peek():
            fail("rebuild_corrupt_json")
        if (not isinstance(document, dict) or document.get("schema") != SCHEMA or
                document.get("run_id") != payloads.run_id or
                document.get("plan_fingerprint") != reader.plan_digest or
                document.get("plan", {}).get("run_id") != payloads.run_id):
            fail("rebuild_run_invalid")
        if before != identity(os.fstat(source.fileno())) or before != identity(Path(path).stat()):
            fail("rebuild_inline_changed")
        document["original_plan_fingerprint"] = document["plan_fingerprint"]
        document["plan_fingerprint"] = fingerprint(document["plan"])
        document["schema"] = STORAGE_SCHEMA
        if len(_ENCODER.encode(document).encode("utf-8")) > MAX_JOURNAL:
            fail("rebuild_journal_metadata_limit")
        backup = safe_path(payloads.root, "inline-v1.json")
        try:
            os.link(path, backup, follow_symlinks=False)
        except FileExistsError:
            if identity(backup.stat()) != identity(Path(path).stat()):
                fail("rebuild_inline_backup_conflict")
        write_json(path, document)
        return document
