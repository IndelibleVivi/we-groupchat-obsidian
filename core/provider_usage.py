"""Content-free, application-wide provider admission and request receipts.

The ledger is not a Cloudflare spending cap. Explicit current-day external
budget evidence is a precondition; generations never get separate allowances.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import stat
import time


class ProviderUsageError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _integer(value, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0) or value > 2**62:
        raise ProviderUsageError("usage_invalid_units")
    return value


def _token(value, *, empty=False):
    if empty and value == "":
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,240}", value):
        raise ProviderUsageError("usage_invalid_identifier")
    if "://" in value:
        raise ProviderUsageError("usage_invalid_identifier")
    return value


@dataclass(frozen=True)
class UsageReservation:
    request_id: str
    account_key: str
    utc_day: str
    route: str
    generation_id: str
    request_fingerprint: str
    upper_bound_units: int
    charged_units: int
    status: str
    receipt_id: str | None = None


class ProviderUsageStore:
    """SQLite is the sole writer of this usage ledger, never source progress."""

    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        self.clock = clock or time.time
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ProviderUsageError("usage_storage_invalid")
            finally:
                os.close(fd)
            with self._db() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS provider_budgets (
                      account_key TEXT NOT NULL, utc_day TEXT NOT NULL,
                      daily_limit_units INTEGER NOT NULL,
                      external_usage_units INTEGER NOT NULL,
                      confirmation_id TEXT NOT NULL, valid_until REAL NOT NULL,
                      blocked INTEGER NOT NULL DEFAULT 0,
                      PRIMARY KEY(account_key, utc_day)
                    );
                    CREATE TABLE IF NOT EXISTS provider_requests (
                      request_id TEXT PRIMARY KEY, account_key TEXT NOT NULL,
                      utc_day TEXT NOT NULL, route TEXT NOT NULL,
                      generation_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
                      upper_bound_units INTEGER NOT NULL,
                      charged_units INTEGER NOT NULL, status TEXT NOT NULL,
                      receipt_id TEXT UNIQUE, created_at REAL NOT NULL,
                      sent_at REAL, settled_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS provider_requests_day
                      ON provider_requests(account_key, utc_day);
                """)
        except (OSError, sqlite3.Error):
            raise ProviderUsageError("usage_storage_unavailable") from None

    @contextmanager
    def _db(self):
        db = None
        try:
            if not stat.S_ISREG(self.path.lstat().st_mode):
                raise ProviderUsageError("usage_storage_invalid")
            db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=15000")
            yield db
        except (OSError, sqlite3.Error):
            raise ProviderUsageError("usage_storage_unavailable") from None
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def _transaction(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def _now(self):
        value = float(self.clock())
        day = datetime.fromtimestamp(value, timezone.utc).date().isoformat()
        return value, day

    @staticmethod
    def _row(row):
        if row is None:
            raise ProviderUsageError("usage_request_missing")
        names = UsageReservation.__dataclass_fields__
        return UsageReservation(**{key: row[key] for key in names})

    @staticmethod
    def _used(db, account_key, day):
        return db.execute(
            "SELECT COALESCE(SUM(charged_units), 0) FROM provider_requests "
            "WHERE account_key=? AND utc_day=?", (account_key, day)
        ).fetchone()[0]

    def _budget(self, db, account_key, day, now):
        row = db.execute(
            "SELECT * FROM provider_budgets WHERE account_key=? AND utc_day=?",
            (account_key, day),
        ).fetchone()
        if row is None or row["valid_until"] <= now:
            raise ProviderUsageError("external_budget_unconfirmed")
        if row["blocked"]:
            raise ProviderUsageError("usage_bound_exceeded")
        return row

    def confirm_budget(self, account_key, *, utc_day, daily_limit_units,
                       external_usage_units, confirmation_id, valid_until):
        _token(account_key)
        _token(confirmation_id)
        _integer(daily_limit_units, positive=True)
        _integer(external_usage_units)
        now, today = self._now()
        if utc_day != today or type(valid_until) not in (int, float):
            raise ProviderUsageError("usage_invalid_budget_day")
        end = datetime.fromisoformat(today).replace(tzinfo=timezone.utc).timestamp() + 86400
        if not now < valid_until <= end:
            raise ProviderUsageError("usage_invalid_budget_expiry")
        with self._transaction() as db:
            # Renewed external evidence must never erase local reservations or
            # a previously observed violation of the conservative bound.
            db.execute("""INSERT INTO provider_budgets
                (account_key,utc_day,daily_limit_units,external_usage_units,
                 confirmation_id,valid_until) VALUES(?,?,?,?,?,?)
                ON CONFLICT(account_key,utc_day) DO UPDATE SET
                 daily_limit_units=excluded.daily_limit_units,
                 external_usage_units=excluded.external_usage_units,
                 confirmation_id=excluded.confirmation_id,
                 valid_until=excluded.valid_until""",
                (account_key, utc_day, daily_limit_units, external_usage_units,
                 confirmation_id, valid_until))

    def reserve(self, account_key, *, request_id, route, upper_bound_units,
                request_fingerprint, generation_id=""):
        for value in (account_key, request_id, route):
            _token(value)
        _token(generation_id, empty=True)
        _integer(upper_bound_units, positive=True)
        if not isinstance(request_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", request_fingerprint):
            raise ProviderUsageError("usage_invalid_fingerprint")
        now, day = self._now()
        with self._transaction() as db:
            previous = db.execute("SELECT * FROM provider_requests WHERE request_id=?", (request_id,)).fetchone()
            if previous is not None:
                expected = (account_key, day, route, generation_id, request_fingerprint, upper_bound_units)
                actual = tuple(previous[key] for key in (
                    "account_key", "utc_day", "route", "generation_id", "request_fingerprint", "upper_bound_units"))
                if expected != actual:
                    raise ProviderUsageError("usage_request_conflict")
                if previous["status"] not in {"reserved", "cancelled"}:
                    raise ProviderUsageError("usage_request_already_sent")
                budget = self._budget(db, account_key, day, now)
                if previous["status"] == "reserved":
                    return self._row(previous)
                # Cancellation proves no send took place. It may be admitted
                # again after new budget evidence, preserving the same intent.
                if self._used(db, account_key, day) + budget["external_usage_units"] + upper_bound_units > budget["daily_limit_units"]:
                    raise ProviderUsageError("usage_budget_exhausted")
                db.execute("UPDATE provider_requests SET status='reserved',charged_units=? WHERE request_id=?",
                           (upper_bound_units, request_id))
                return self._row(db.execute("SELECT * FROM provider_requests WHERE request_id=?", (request_id,)).fetchone())
            budget = self._budget(db, account_key, day, now)
            if self._used(db, account_key, day) + budget["external_usage_units"] + upper_bound_units > budget["daily_limit_units"]:
                raise ProviderUsageError("usage_budget_exhausted")
            db.execute("""INSERT INTO provider_requests
                (request_id,account_key,utc_day,route,generation_id,request_fingerprint,
                 upper_bound_units,charged_units,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,'reserved',?)""",
                (request_id, account_key, day, route, generation_id, request_fingerprint,
                 upper_bound_units, upper_bound_units, now))
            return self._row(db.execute("SELECT * FROM provider_requests WHERE request_id=?", (request_id,)).fetchone())

    def mark_sent(self, request_id):
        now, day = self._now()
        with self._transaction() as db:
            row = self._row(db.execute("SELECT * FROM provider_requests WHERE request_id=?", (request_id,)).fetchone())
            if row.status != "reserved":
                raise ProviderUsageError("usage_request_already_sent")
            if row.utc_day != day:
                raise ProviderUsageError("usage_day_changed")
            budget = self._budget(db, row.account_key, day, now)
            if self._used(db, row.account_key, day) + budget["external_usage_units"] > budget["daily_limit_units"]:
                raise ProviderUsageError("usage_budget_exhausted")
            db.execute("UPDATE provider_requests SET status='sent',sent_at=? WHERE request_id=?", (now, request_id))

    def mark_unknown(self, request_id):
        with self._transaction() as db:
            row = self._row(db.execute("SELECT * FROM provider_requests WHERE request_id=?", (request_id,)).fetchone())
            if row.status not in {"sent", "unknown"}:
                raise ProviderUsageError("usage_invalid_transition")
            db.execute("UPDATE provider_requests SET status='unknown' WHERE request_id=?", (request_id,))

    def cancel(self, request_id):
        with self._transaction() as db:
            row = self._row(db.execute("SELECT * FROM provider_requests WHERE request_id=?", (request_id,)).fetchone())
            if row.status != "reserved":
                raise ProviderUsageError("usage_invalid_transition")
            db.execute("UPDATE provider_requests SET status='cancelled',charged_units=0 WHERE request_id=?", (request_id,))

    def settle(self, request_id, *, actual_units, receipt_id):
        _integer(actual_units)
        _token(receipt_id)
        now, _day = self._now()
        exceeded = False
        with self._transaction() as db:
            row = self._row(db.execute("SELECT * FROM provider_requests WHERE request_id=?", (request_id,)).fetchone())
            if row.status == "settled":
                if row.receipt_id != receipt_id or row.charged_units != actual_units:
                    raise ProviderUsageError("usage_receipt_conflict")
                return row
            if row.status not in {"sent", "unknown"}:
                raise ProviderUsageError("usage_invalid_transition")
            owner = db.execute("SELECT request_id FROM provider_requests WHERE receipt_id=?", (receipt_id,)).fetchone()
            if owner is not None and owner[0] != request_id:
                raise ProviderUsageError("usage_receipt_conflict")
            exceeded = actual_units > row.upper_bound_units
            db.execute("UPDATE provider_requests SET status='settled',charged_units=?,receipt_id=?,settled_at=? WHERE request_id=?",
                       (actual_units, receipt_id, now, request_id))
            if exceeded:
                db.execute("UPDATE provider_budgets SET blocked=1 WHERE account_key=? AND utc_day=?", (row.account_key, row.utc_day))
        if exceeded:
            raise ProviderUsageError("usage_bound_exceeded")
        return self.get(request_id)

    def get(self, request_id):
        with self._db() as db:
            return self._row(db.execute("SELECT * FROM provider_requests WHERE request_id=?", (request_id,)).fetchone())

    def day_usage(self, account_key, utc_day):
        with self._db() as db:
            return self._used(db, account_key, utc_day)

    def budget_status(self, account_key):
        """Source-free preflight; an absent/expired account observation is blocked."""
        now, day = self._now()
        with self._db() as db:
            try:
                budget = self._budget(db, account_key, day, now)
            except ProviderUsageError as exc:
                return {"ready": False, "code": exc.code, "utc_day": day}
            remaining = budget["daily_limit_units"] - budget["external_usage_units"] - self._used(db, account_key, day)
            return {"ready": remaining > 0, "code": "ready" if remaining > 0 else "provider_budget_exhausted",
                    "utc_day": day, "remaining_units": max(0, remaining),
                    "confirmation_id": budget["confirmation_id"], "valid_until": budget["valid_until"]}
