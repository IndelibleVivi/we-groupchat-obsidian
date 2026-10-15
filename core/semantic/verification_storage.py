"""Disposable, private disk state for independent semantic verification.

No object here writes the source database or owns canonical progress. Payloads
are read one row/group at a time; only the caller's current row is materialized.
"""
from collections.abc import Mapping, MutableMapping
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory


def freeze_json(value):
    """Keep JSON round-trip semantics without a whole-document string/copy.

    Validate with the encoder's bounded iterator, then copy containers while
    reusing immutable strings. Tuples and non-string JSON keys normalize just
    as they do through dumps/loads. Cycles/unsupported values fail validation.
    """
    for _ in json.JSONEncoder(ensure_ascii=False, sort_keys=True, allow_nan=False).iterencode(value):
        pass
    def clone(node):
        if isinstance(node, dict):
            return {(key if isinstance(key, str) else json.dumps(key, allow_nan=False)): clone(child)
                    for key, child in node.items()}
        if isinstance(node, (list, tuple)):
            return [clone(child) for child in node]
        if isinstance(node, bool) or node is None:
            return node
        if isinstance(node, int):
            return int(node)
        if isinstance(node, float):
            return float(node)
        return node
    return clone(value)


class Rows:
    """Reiterable SQL rows, with no retained cursor or payload list."""
    def __init__(self, db, sql, parameters=(), transform=dict):
        self.db, self.sql, self.parameters, self.transform = db, sql, parameters, transform

    def __iter__(self):
        cursor = self.db.execute(self.sql, self.parameters)
        try:
            for row in cursor:
                yield self.transform(row)
        finally:
            cursor.close()

    def __len__(self):
        return self.db.execute("SELECT COUNT(*) FROM (" + self.sql + ")", self.parameters).fetchone()[0]

    def where(self, clause, parameters=()):
        # SQL and identifiers are fixed by the verifier, never provider input.
        return Rows(self.db, "SELECT * FROM (" + self.sql + ") WHERE " + clause,
                    (*self.parameters, *parameters), self.transform)


class RowMap(Mapping):
    """Dictionary interface over a unique-key SQL relation, without caching."""
    def __init__(self, db, sql, key, parameters=()):
        self.db, self.sql, self.key, self.parameters = db, sql, key, parameters

    def __iter__(self):
        for row in self.db.execute('SELECT "' + self.key + '" FROM (' + self.sql + ')', self.parameters):
            yield row[0]

    def __len__(self):
        return len(self.values())

    def __contains__(self, key):
        return self.db.execute('SELECT 1 FROM (' + self.sql + ') WHERE "' + self.key + '"=? LIMIT 1',
                               (*self.parameters, key)).fetchone() is not None

    def __getitem__(self, key):
        row = self.db.execute('SELECT * FROM (' + self.sql + ') WHERE "' + self.key + '"=?',
                              (*self.parameters, key)).fetchone()
        if row is None:
            raise KeyError(key)
        return dict(row)

    def values(self):
        return Rows(self.db, self.sql, self.parameters)

    def items(self):
        for row in self.values():
            yield row[self.key], row


class GroupMap(Mapping):
    """defaultdict-style current group, scoped by an indexed SQL column."""
    def __init__(self, db, table, key, *, order="rowid", transform=dict, collection=list, clause="1"):
        self.db, self.table, self.key = db, table, key
        self.order, self.transform, self.collection, self.clause = order, transform, collection, clause

    def __iter__(self):
        for row in self.db.execute(f'SELECT DISTINCT "{self.key}" FROM "{self.table}" WHERE {self.clause}'):
            yield row[0]

    def __len__(self):
        return self.db.execute(f'SELECT COUNT(DISTINCT "{self.key}") FROM "{self.table}" WHERE {self.clause}').fetchone()[0]

    def __getitem__(self, key):
        return self.collection(Rows(self.db,
            f'SELECT * FROM "{self.table}" WHERE "{self.key}"=? AND {self.clause} ORDER BY {self.order}',
            (key,), self.transform))


class ScratchMap(MutableMapping):
    """Computed receipts/vectors on private scratch disk, not in a Python dict."""
    def __init__(self, db, name):
        self.db, self.name = db, name
        db.execute(f'CREATE TABLE "{name}"(key TEXT PRIMARY KEY,value TEXT NOT NULL)')

    def __iter__(self):
        for row in self.db.execute(f'SELECT key FROM "{self.name}"'):
            yield row[0]

    def __len__(self):
        return self.db.execute(f'SELECT COUNT(*) FROM "{self.name}"').fetchone()[0]

    def __getitem__(self, key):
        row = self.db.execute(f'SELECT value FROM "{self.name}" WHERE key=?', (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def __setitem__(self, key, value):
        self.db.execute(f'INSERT OR REPLACE INTO "{self.name}" VALUES (?,?)',
                        (key, json.dumps(value, ensure_ascii=False, allow_nan=False)))

    def __delitem__(self, key):
        if not self.db.execute(f'DELETE FROM "{self.name}" WHERE key=?', (key,)).rowcount:
            raise KeyError(key)


@contextmanager
def workspace(source):
    """WAL-aware Online Backup and computed state, both private and disposable."""
    with TemporaryDirectory(prefix="wgo-verification-") as directory:
        root = Path(directory).resolve()
        snapshot = scratch = None
        def connect(name):
            path = root / name
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)  # Private mode precedes the first SQLite payload byte.
            connection = sqlite3.connect(path)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA cache_size=-4096")
                connection.execute("PRAGMA mmap_size=0")
                connection.execute("PRAGMA temp_store=FILE")
            except BaseException:
                connection.close()
                raise
            return connection
        try:
            snapshot = connect("snapshot.sqlite")
            source.backup(snapshot)
            scratch = connect("computed.sqlite")
            yield snapshot, scratch
        finally:
            try:
                if scratch is not None:
                    scratch.close()
            finally:
                if snapshot is not None:
                    snapshot.close()
