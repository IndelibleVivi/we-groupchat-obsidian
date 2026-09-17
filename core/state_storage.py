"""Platform IO binding for config, monitor state, and source inventory.

The stores still own serialization, locking decisions, schemas and revisions.
This binding only selects an admitted path and the native private publisher.
"""
from __future__ import annotations

import os
import stat

from .platform import PathIdentityError, create_platform_services


class StateFileNotRegular(OSError):
    def __init__(self):
        super().__init__("state_file_not_regular")


class StateStorage:
    def __init__(self, path, *, platform_services=None):
        self.path = path
        self._services = platform_services

    @property
    def services(self):
        if self._services is None:
            self._services = create_platform_services()
        return self._services

    def operational_path(self):
        # A Mac path description resolves aliases. Reject an indirect final
        # file first so that this does not turn a forbidden state symlink into
        # an ordinary file before the owning store reads it.
        try:
            mode = os.lstat(self.path).st_mode
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(mode):
                raise StateFileNotRegular()
        try:
            return self.services.require("paths").describe(self.path).operational_path
        except PathIdentityError as exc:
            raise OSError(exc.code) from exc

    def prepare(self):
        self.services.require("private_storage").ensure_directory(os.path.dirname(self.path))
        path = self.operational_path()
        if os.path.lexists(path):
            self.services.require("private_storage").ensure_file(path)
        return path

    def write_bytes(self, data):
        self.services.require("atomic_publisher").write_bytes(self.operational_path(), data)
