"""Durable, revisioned state for one topic-monitor checkpoint."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import stat
import tempfile
from typing import Callable

from .config import ensure_private_dir, ensure_private_file
from .platform import LockBusy, LockMode, create_file_lock


MONITOR_STATE_SCHEMA = "we-groupchat-obsidian.monitor-state.v1"
MONITOR_PENDING_STATE_SCHEMA = "we-groupchat-obsidian.monitor-state.v2"


class MonitorStateError(RuntimeError):
    """A content-free monitor-state failure."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class MonitorStateSnapshot:
    """One parsed state revision.

    ``existed`` reports whether the canonical file existed before the operation
    that returned the snapshot. ``data`` excludes storage-owned schema and
    revision fields.
    """

    data: dict
    revision: int
    existed: bool


class MonitorStateStore:
    """Own locked reads and compare-and-swap writes for one state file."""

    def __init__(self, path: str | os.PathLike[str], *, file_lock=None):
        self.path = os.path.abspath(os.path.expanduser(os.fspath(path)))
        self.lock_path = self.path + ".lock"
        self._file_lock = file_lock

    def _lock_service(self):
        if self._file_lock is None:
            self._file_lock = create_file_lock()
        return self._file_lock

    def _lock(self, *, exclusive: bool):
        directory = os.path.dirname(self.path)
        try:
            ensure_private_dir(directory)
            return self._lock_service().acquire(
                self.lock_path,
                mode=LockMode.EXCLUSIVE if exclusive else LockMode.SHARED,
                blocking=True,
            )
        except OSError as exc:
            raise MonitorStateError("monitor_state_lock_unavailable") from exc

    @staticmethod
    def _unlock(lock_handle) -> None:
        lock_handle.close()

    def _read_locked(self) -> MonitorStateSnapshot:
        try:
            path_stat = os.lstat(self.path)
        except FileNotFoundError:
            return MonitorStateSnapshot(data={}, revision=0, existed=False)
        except OSError as exc:
            raise MonitorStateError("monitor_state_unreadable") from exc

        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise MonitorStateError("monitor_state_not_regular")

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags)
        except OSError as exc:
            raise MonitorStateError("monitor_state_not_regular") from exc

        try:
            opened_stat = os.fstat(fd)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise MonitorStateError("monitor_state_not_regular")
            try:
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = -1
                    payload = json.load(handle)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise MonitorStateError("monitor_state_corrupt") from exc
            except OSError as exc:
                raise MonitorStateError("monitor_state_unreadable") from exc
        finally:
            if fd >= 0:
                os.close(fd)

        if not isinstance(payload, dict):
            raise MonitorStateError("monitor_state_corrupt")

        has_schema = "schema" in payload
        has_revision = "revision" in payload
        if not has_schema and not has_revision:
            if "pending_source_batch" in payload:
                raise MonitorStateError("monitor_state_corrupt")
            return MonitorStateSnapshot(data=dict(payload), revision=0, existed=True)
        if (
            payload.get("schema") not in {MONITOR_STATE_SCHEMA, MONITOR_PENDING_STATE_SCHEMA}
            or ("pending_source_batch" in payload) != (
                payload.get("schema") == MONITOR_PENDING_STATE_SCHEMA
            )
            or isinstance(payload.get("revision"), bool)
            or not isinstance(payload.get("revision"), int)
            or payload["revision"] < 1
        ):
            raise MonitorStateError("monitor_state_corrupt")

        data = dict(payload)
        revision = int(data.pop("revision"))
        data.pop("schema", None)
        return MonitorStateSnapshot(data=data, revision=revision, existed=True)

    @staticmethod
    def _state_data(value: dict) -> dict:
        if not isinstance(value, dict):
            raise TypeError("monitor state must be a dict")
        data = dict(value)
        data.pop("schema", None)
        data.pop("revision", None)
        return data

    def _write_locked_unmapped(
        self,
        data: dict,
        *,
        revision: int,
        existed_before: bool,
    ) -> MonitorStateSnapshot:
        directory = os.path.dirname(self.path)
        ensure_private_dir(directory)
        temp_fd, temp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(self.path)}.",
            suffix=".tmp",
            dir=directory,
        )
        try:
            fchmod = getattr(os, "fchmod", None)
            if callable(fchmod):
                try:
                    fchmod(temp_fd, 0o600)
                except OSError:
                    pass
            payload = {
                # Old readers reject v2 rather than ignoring pending work.
                # Once acknowledged, ordinary state remains downgrade-readable.
                "schema": (
                    MONITOR_PENDING_STATE_SCHEMA
                    if "pending_source_batch" in data else MONITOR_STATE_SCHEMA
                ),
                "revision": int(revision),
                **self._state_data(data),
            }
            with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
                temp_fd = -1
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            temp_path = ""
            ensure_private_file(self.path)

            try:
                directory_fd = os.open(directory, os.O_RDONLY)
            except OSError:
                directory_fd = -1
            if directory_fd >= 0:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass
                finally:
                    os.close(directory_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        return MonitorStateSnapshot(
            data=self._state_data(data),
            revision=int(revision),
            existed=bool(existed_before),
        )

    def _write_locked(
        self,
        data: dict,
        *,
        revision: int,
        existed_before: bool,
    ) -> MonitorStateSnapshot:
        try:
            return self._write_locked_unmapped(
                data,
                revision=revision,
                existed_before=existed_before,
            )
        except OSError as exc:
            raise MonitorStateError("monitor_state_write_failed") from exc

    @contextmanager
    def execution_lock(self):
        """Serialize a whole monitor run without holding the checkpoint lock.

        A second worker may inspect state, but may not replay the same pending
        provider request. Process exit releases ownership; there is no lease
        timeout that could expire while a provider is still responding.
        """
        try:
            ensure_private_dir(os.path.dirname(self.path))
            handle = self._lock_service().acquire(
                self.path + ".run.lock", mode=LockMode.EXCLUSIVE, blocking=False,
            )
        except LockBusy as exc:
            raise MonitorStateError("monitor_worker_busy") from exc
        except OSError as exc:
            raise MonitorStateError("monitor_worker_lock_unavailable") from exc
        try:
            yield
        finally:
            handle.close()

    def read(self) -> MonitorStateSnapshot:
        fd = self._lock(exclusive=False)
        try:
            return self._read_locked()
        finally:
            self._unlock(fd)

    def inspect(self) -> MonitorStateSnapshot:
        """Read atomic state without creating a directory or lock file.

        State publication uses ``os.replace``, so an observation sees either
        the complete old file or the complete new file.  Writers still use the
        locked ``read``/``commit`` surfaces; this method exists for health and
        other strictly read-only diagnostics.
        """
        return self._read_locked()

    def initialize_if_absent(self, initial_state: dict) -> MonitorStateSnapshot:
        fd = self._lock(exclusive=True)
        try:
            current = self._read_locked()
            if current.existed:
                return current
            return self._write_locked(
                self._state_data(initial_state),
                revision=1,
                existed_before=False,
            )
        finally:
            self._unlock(fd)

    def commit(self, expected_revision: int, new_state: dict) -> MonitorStateSnapshot:
        fd = self._lock(exclusive=True)
        try:
            current = self._read_locked()
            if current.revision != int(expected_revision):
                raise MonitorStateError("monitor_state_conflict")
            return self._write_locked(
                self._state_data(new_state),
                revision=current.revision + 1,
                existed_before=current.existed,
            )
        finally:
            self._unlock(fd)

    def update(self, mutator: Callable[[dict], dict | None]) -> MonitorStateSnapshot:
        if not callable(mutator):
            raise TypeError("mutator must be callable")
        fd = self._lock(exclusive=True)
        try:
            current = self._read_locked()
            working = dict(current.data)
            result = mutator(working)
            if result is not None:
                working = self._state_data(result)
            return self._write_locked(
                working,
                revision=current.revision + 1,
                existed_before=current.existed,
            )
        finally:
            self._unlock(fd)
