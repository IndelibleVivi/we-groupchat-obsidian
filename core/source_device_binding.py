"""macOS source identity checks for the existing standalone producer marker."""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import os
import stat
import sys



# macOS getattrlist(2) attribute identifiers. Requesting only the fixed-width
# volume UUID keeps the returned stream unambiguous: one ``uint32`` byte count
# followed directly by the 16 UUID bytes (values follow in bit order with no
# per-attribute length prefix for this single attribute).
_ATTR_BIT_MAP_COUNT = 5
_ATTR_VOL_UUID = 0x00040000


class SourceDeviceBindingError(RuntimeError):
    """A content-free source-volume binding failure."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


class _AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


def _darwin_volume_uuid(path: str) -> str:
    """Return the stable volume UUID for ``path`` or fail with a bounded code."""
    library = ctypes.util.find_library("c")
    if not library:
        raise SourceDeviceBindingError("volume_identity_unavailable")
    libc = ctypes.CDLL(library, use_errno=True)
    getattrlist = getattr(libc, "getattrlist", None)
    if getattrlist is None:
        raise SourceDeviceBindingError("volume_identity_unavailable")
    buffer = ctypes.create_string_buffer(4096)
    attributes = _AttrList()
    attributes.bitmapcount = _ATTR_BIT_MAP_COUNT
    attributes.volattr = _ATTR_VOL_UUID
    result = getattrlist(
        os.fsencode(path),
        ctypes.byref(attributes),
        ctypes.byref(buffer),
        ctypes.c_size_t(len(buffer)),
        0,
    )
    if result != 0:
        raise SourceDeviceBindingError("volume_identity_unavailable")
    raw = buffer.raw
    total = int.from_bytes(raw[:4], "little")
    if total != 20:
        raise SourceDeviceBindingError("volume_identity_unavailable")
    uuid_bytes = raw[4:20]
    if uuid_bytes == b"\0" * 16:
        raise SourceDeviceBindingError("volume_identity_unavailable")
    return uuid_bytes.hex()


def read_volume_identity(path: str) -> str:
    """Return a stable filesystem-volume identity for an existing directory."""
    if sys.platform == "darwin":
        return _darwin_volume_uuid(path)
    # No stable, non-guessing volume identity is defined for this platform.
    raise SourceDeviceBindingError("volume_identity_unsupported")


def resolve_root_identity(path: str) -> dict:
    """Return the observed root identity used to build or check a binding."""
    source_root = os.path.realpath(os.path.expanduser(os.fspath(path or "")))
    try:
        value = os.stat(source_root)
    except OSError as exc:
        raise SourceDeviceBindingError("source_root_unavailable") from exc
    if not stat.S_ISDIR(value.st_mode):
        raise SourceDeviceBindingError("source_root_not_directory")
    return {
        "source_db_dir": source_root,
        "root_inode": int(value.st_ino),
        "device": int(value.st_dev),
        "volume_identity": read_volume_identity(source_root),
    }


def source_namespace(root, device, inode):
    return hashlib.sha256(
        f"wechat-source-namespace-v1\0{root}\0{device}:{inode}".encode()
    ).hexdigest()[:32]


def validate_binding(value):
    if (not isinstance(value, dict)
            or set(value) != {"root_inode", "volume_identity", "original_device"}
            or any(isinstance(value[k], bool) or not isinstance(value[k], int)
                   or value[k] < (1 if k == "root_inode" else 0)
                   for k in ("root_inode", "original_device"))
            or not isinstance(value["volume_identity"], str)
            or len(value["volume_identity"]) != 32
            or any(c not in "0123456789abcdef" for c in value["volume_identity"])):
        raise SourceDeviceBindingError("source_device_binding_invalid")
    return dict(value)


def check_binding(root, value):
    binding = validate_binding(value)
    observed = resolve_root_identity(root)
    for key, code in (("root_inode", "inode"), ("volume_identity", "volume")):
        if observed[key] != binding[key]:
            raise SourceDeviceBindingError(f"source_device_binding_{code}_changed")
    return observed
