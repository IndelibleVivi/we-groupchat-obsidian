"""E1a bounded read-only probe for one exact Windows WeChat selection.

Scope (issue #35): given one explicitly selected WeChat executable and one
explicitly selected source root, produce a versioned, deterministic,
content-free receipt that distinguishes a unique supported candidate from
unknown, unreadable, unsupported, or ambiguous states.

The probe never writes to or changes the selected source, never reads message
or attachment bytes, never decrypts, never extracts keys, never scans outside
the two selected paths, and never makes network calls. Path admission,
NTFS/reparse enforcement and root containment are consumed through the
existing ``WindowsPathService`` boundary; this module owns only
WeChat-specific executable identity, selected-root layout classification and
receipt construction. It deliberately carries no schema, key, cache, reader,
cursor or inventory knowledge (those belong to E1b/W1.3/W2).

Public output is allowlist-only: environment versions, executable hash/build,
layout profile identifier, bounded counts, bounded status codes and explicit
``not_run`` items. Local paths, account identifiers and raw diagnostics never
enter the receipt.
"""
from __future__ import annotations

import hashlib
import os
import platform
import sys
from typing import Any, Callable, Iterable

from core.platform import PathIdentityError, ReparsePointConflict


RECEIPT_SCHEMA = "we-groupchat-obsidian.windows-e1a-receipt.v1"
PROBE_VERSION = "e1a.1"

#: The single narrow layout signature E1a recognizes: an account directory
#: directly under the selected root that contains a ``msg`` directory with at
#: least one ``*.db`` file. Anything else stays explicitly unknown; later
#: phases own schema-level profiles.
LAYOUT_PROFILE = "wechat-win-msg-tree:v1"

VERDICTS = (
    "unique_supported_candidate",
    "unknown",
    "unreadable",
    "unsupported",
    "ambiguous",
)

EXE_STATUSES = (
    "exe_ok",
    "exe_missing",
    "exe_not_regular",
    "exe_unreadable",
    "exe_reparse_conflict",
)
BUILD_STATUSES = ("build_observed", "build_unknown", "build_unsupported")
PROCESS_STATUSES = (
    "process_none",
    "process_unique",
    "process_ambiguous",
    "process_not_run",
)
ROOT_STATUSES = (
    "root_ok",
    "root_missing",
    "root_not_directory",
    "root_unreadable",
    "root_reparse_conflict",
    "root_unsupported_filesystem",
    "root_unsupported_namespace",
    "root_escape",
    "root_unavailable",
)
LAYOUT_STATUSES = (
    "unique_candidate",
    "no_candidate",
    "ambiguous_candidates",
    "unexpected_layout",
    "layout_partially_unreadable",
    "layout_not_run",
)

#: Bounded enumeration guards; exceeding one keeps the receipt truthful
#: instead of pretending a complete observation.
_MAX_ROOT_ENTRIES = 512
_MAX_STORE_FILES = 4096

_WECHAT_PRODUCT_MARKERS = ("wechat", "微信")

#: FILE_ATTRIBUTE_REPARSE_POINT; junctions/symlinks inside the selection are
#: never traversed.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def current_environment() -> dict[str, Any]:
    """Return the allowlisted environment summary for the receipt."""
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "arm64": "arm64"}.get(machine, "other")
    environment: dict[str, Any] = {
        "os": "windows" if os.name == "nt" else os.name,
        "arch": arch,
        "python": platform.python_version(),
    }
    if os.name == "nt":
        environment["os_build"] = sys.getwindowsversion().build
    return environment


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_dll():
    import ctypes
    from ctypes import wintypes

    version_dll = ctypes.WinDLL("version", use_last_error=True)
    version_dll.GetFileVersionInfoSizeW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    version_dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version_dll.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    version_dll.GetFileVersionInfoW.restype = wintypes.BOOL
    version_dll.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    version_dll.VerQueryValueW.restype = wintypes.BOOL
    return version_dll


def _version_info_buffer(path: str):
    """Return (version_dll, buffer) for the PE version resource, or None."""
    if os.name != "nt":
        return None
    import ctypes

    version_dll = _version_dll()
    size = version_dll.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version_dll.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    return version_dll, buffer


def _read_file_version(path: str) -> str | None:
    """Read the PE file version via version.dll; None when unavailable."""
    import ctypes
    from ctypes import wintypes

    info = _version_info_buffer(path)
    if info is None:
        return None
    version_dll, buffer = info
    value = ctypes.c_void_p()
    length = wintypes.UINT(0)
    if not version_dll.VerQueryValueW(
        buffer, "\\", ctypes.byref(value), ctypes.byref(length)
    ):
        return None
    fields = (wintypes.DWORD * 13).from_address(value.value)
    ms, ls = int(fields[2]), int(fields[3])
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _read_product_name(path: str) -> str | None:
    """Read the PE StringFileInfo product name; None when unavailable."""
    import ctypes
    from ctypes import wintypes

    info = _version_info_buffer(path)
    if info is None:
        return None
    version_dll, buffer = info
    value = ctypes.c_void_p()
    length = wintypes.UINT(0)
    if not version_dll.VerQueryValueW(
        buffer,
        "\\VarFileInfo\\Translation",
        ctypes.byref(value),
        ctypes.byref(length),
    ):
        return None
    pairs = min(int(length.value) // 4, 8)
    translations = (wintypes.DWORD * pairs).from_address(value.value)
    for translation in translations:
        sub_block = (
            f"\\StringFileInfo\\{int(translation) & 0xFFFF:04x}"
            f"{int(translation) >> 16:04x}\\ProductName"
        )
        text = ctypes.c_void_p()
        if version_dll.VerQueryValueW(
            buffer, sub_block, ctypes.byref(text), ctypes.byref(length)
        ) and text.value:
            return ctypes.wstring_at(text.value)
    return None


def _list_process_image_names() -> list[str] | None:
    """Return running process image names via Toolhelp32; None off Windows.

    Identity is bounded to the selected executable's image name: no memory
    inspection, no general process detail, no key material.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        names: list[str] = []
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            names.append(str(entry.szExeFile))
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return names
    finally:
        kernel32.CloseHandle(snapshot)


def _entry_is_reparse(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    if os.name == "nt":
        attributes = entry.stat(follow_symlinks=False).st_file_attributes
        return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _scandir_names(path: str) -> Iterable[tuple[str, bool, bool]]:
    """Yield (name, is_dir, is_reparse) without following reparse points."""
    with os.scandir(path) as entries:
        for entry in entries:
            yield (entry.name, entry.is_dir(follow_symlinks=False), _entry_is_reparse(entry))


def _map_root_error(exc: PathIdentityError) -> str:
    if isinstance(exc, ReparsePointConflict):
        return "root_reparse_conflict"
    reason = getattr(exc, "reason", "")
    return {
        "missing_final_component": "root_missing",
        "ancestor_not_directory": "root_not_directory",
        "unsupported_filesystem": "root_unsupported_filesystem",
        "remote_path_unsupported": "root_unsupported_namespace",
        "remote_source_root_unsupported": "root_unsupported_namespace",
        "unc_share_missing": "root_unsupported_namespace",
        "source_root_escape": "root_escape",
    }.get(reason, "root_unavailable")


def _map_exe_error(exc: PathIdentityError) -> str:
    if isinstance(exc, ReparsePointConflict):
        return "exe_reparse_conflict"
    reason = getattr(exc, "reason", "")
    if reason == "missing_final_component":
        return "exe_missing"
    return "exe_unreadable"


def probe_selection(
    wechat_exe: str,
    source_root: str,
    *,
    path_service: Any,
    version_reader: Callable[[str], str | None] | None = None,
    product_reader: Callable[[str], str | None] | None = None,
    process_lister: Callable[[], list[str] | None] | None = None,
    hasher: Callable[[str], str] | None = None,
    root_lister: Callable[[str], Iterable[tuple[str, bool, bool]]] | None = None,
    environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Probe one explicit (executable, source root) selection, read-only.

    All OS-touching seams are injectable so synthetic tests can exercise every
    classification without a real WeChat installation. The function never
    raises for expected host conditions; every failure lands in a bounded
    content-free status code.
    """
    version_reader = version_reader or _read_file_version
    product_reader = product_reader or _read_product_name
    process_lister = process_lister or _list_process_image_names
    hasher = hasher or _sha256_file
    root_lister = root_lister or _scandir_names

    not_run: list[str] = []

    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "probe_version": PROBE_VERSION,
        "environment": (
            dict(environment) if environment is not None else current_environment()
        ),
        "executable": {"status": "exe_missing"},
        "build": {"status": "build_unknown"},
        "process": {"status": "process_not_run"},
        "source_root": {"status": "root_unavailable"},
        "layout": {"status": "layout_not_run"},
        "verdict": "unknown",
        "not_run": not_run,
    }

    # --- Executable admission and identity -------------------------------
    exe_path = os.path.abspath(os.fspath(wechat_exe))
    exe_identity = None
    try:
        exe_identity = path_service.describe(exe_path)
    except PathIdentityError as exc:
        receipt["executable"] = {"status": _map_exe_error(exc)}
    except OSError:
        receipt["executable"] = {"status": "exe_unreadable"}
    if exe_identity is not None:
        display_path = exe_identity.display_path
        if not os.path.isfile(display_path):
            receipt["executable"] = {"status": "exe_not_regular"}
        else:
            try:
                digest = hasher(display_path)
            except OSError:
                receipt["executable"] = {"status": "exe_unreadable"}
            else:
                receipt["executable"] = {"status": "exe_ok", "sha256": digest}
                receipt["build"] = _classify_build(
                    display_path, version_reader, product_reader, not_run
                )

    # --- Process state, bounded to the selected image name ---------------
    if receipt["executable"]["status"] == "exe_ok":
        image_names = process_lister()
        if image_names is None:
            not_run.append("process_observation")
        else:
            selected_name = os.path.basename(exe_identity.display_path).lower()
            count = sum(1 for name in image_names if name.lower() == selected_name)
            status = (
                "process_none"
                if count == 0
                else "process_unique"
                if count == 1
                else "process_ambiguous"
            )
            receipt["process"] = {"status": status, "candidate_count": count}

    # --- Source root admission -------------------------------------------
    root_path = os.path.abspath(os.fspath(source_root))
    root_identity = None
    try:
        root_identity = path_service.describe(root_path)
    except PathIdentityError as exc:
        receipt["source_root"] = {"status": _map_root_error(exc)}
    except OSError:
        receipt["source_root"] = {"status": "root_unreadable"}
    else:
        receipt["source_root"] = {"status": "root_ok"}

    # --- Bounded layout classification ------------------------------------
    if root_identity is not None:
        layout, matched = _classify_layout(
            root_identity.display_path, root_lister, receipt
        )
        receipt["layout"] = layout
        if layout["status"] == "layout_partially_unreadable":
            not_run.append("complete_layout_observation")
        if matched and receipt["source_root"]["status"] == "root_ok":
            _verify_candidate_containment(
                matched, root_identity.display_path, path_service, receipt
            )

    receipt["verdict"] = _combine_verdict(receipt)
    return receipt


def _classify_build(
    display_path: str,
    version_reader: Callable[[str], str | None],
    product_reader: Callable[[str], str | None],
    not_run: list[str],
) -> dict[str, Any]:
    file_version = version_reader(display_path)
    if file_version is None:
        not_run.append("executable_version")
        return {"status": "build_unknown"}
    product_name = product_reader(display_path)
    if product_name is None:
        not_run.append("executable_product_name")
        return {"status": "build_unknown", "file_version": file_version}
    if any(marker in product_name.lower() for marker in _WECHAT_PRODUCT_MARKERS):
        return {"status": "build_observed", "file_version": file_version}
    return {"status": "build_unsupported"}


def _verify_candidate_containment(
    matched: list[str],
    root_display: str,
    path_service: Any,
    receipt: dict[str, Any],
) -> None:
    """Prove each candidate stays inside the admitted root, fail closed."""
    for name in matched:
        try:
            path_service.describe(
                os.path.join(root_display, name), source_root=root_display
            )
        except ReparsePointConflict:
            receipt["source_root"] = {"status": "root_reparse_conflict"}
            receipt["layout"] = {"status": "layout_not_run"}
            return
        except PathIdentityError as exc:
            receipt["source_root"] = {"status": _map_root_error(exc)}
            receipt["layout"] = {"status": "layout_not_run"}
            return
        except OSError:
            receipt["source_root"] = {"status": "root_unreadable"}
            receipt["layout"] = {"status": "layout_not_run"}
            return


def _classify_layout(
    root_display: str,
    root_lister: Callable[[str], Iterable[tuple[str, bool, bool]]],
    receipt: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Classify the admitted root; matched names stay local, never reported."""
    try:
        entries = list(root_lister(root_display))
    except OSError:
        receipt["source_root"] = {"status": "root_unreadable"}
        return {"status": "layout_not_run"}, []
    if len(entries) > _MAX_ROOT_ENTRIES:
        return {"status": "unexpected_layout", "root_entry_limit_exceeded": True}, []

    # Reparse points inside the selection are never traversed.
    subdirs = [name for name, is_dir, is_reparse in entries if is_dir and not is_reparse]
    reparse_dirs = [name for name, is_dir, is_reparse in entries if is_dir and is_reparse]
    if reparse_dirs:
        receipt["source_root"] = {"status": "root_reparse_conflict"}
        return {"status": "layout_not_run"}, []
    if not subdirs:
        return {
            "status": "no_candidate",
            "candidate_count": 0,
            "account_dir_count": 0,
        }, []

    matched: list[str] = []
    unreadable = 0
    for name in subdirs:
        store = os.path.join(root_display, name, "msg")
        try:
            store_entries = list(root_lister(store))
        except OSError:
            unreadable += 1
            continue
        if len(store_entries) > _MAX_STORE_FILES:
            return {"status": "unexpected_layout", "store_entry_limit_exceeded": True}, []
        db_count = sum(
            1
            for entry_name, is_dir, _ in store_entries
            if not is_dir and entry_name.lower().endswith(".db")
        )
        if db_count >= 1:
            matched.append(name)

    layout: dict[str, Any] = {
        "candidate_count": len(matched),
        "account_dir_count": len(subdirs),
    }
    if unreadable:
        layout["unreadable_dir_count"] = unreadable
    if len(matched) > 1:
        layout["status"] = "ambiguous_candidates"
    elif len(matched) == 1:
        if unreadable:
            layout["status"] = "layout_partially_unreadable"
        else:
            layout["status"] = "unique_candidate"
            layout["layout_profile"] = LAYOUT_PROFILE
    else:
        layout["status"] = "unexpected_layout"
    return layout, matched


def _combine_verdict(receipt: dict[str, Any]) -> str:
    exe = receipt["executable"]["status"]
    build = receipt["build"]["status"]
    process = receipt["process"]["status"]
    root = receipt["source_root"]["status"]
    layout = receipt["layout"]["status"]

    if exe in ("exe_unreadable", "exe_reparse_conflict") or root in (
        "root_unreadable",
        "root_reparse_conflict",
    ):
        return "unreadable"
    if exe == "exe_not_regular" or build == "build_unsupported" or root in (
        "root_unsupported_filesystem",
        "root_unsupported_namespace",
        "root_not_directory",
    ):
        return "unsupported"
    if process == "process_ambiguous" or layout == "ambiguous_candidates":
        return "ambiguous"
    if (
        exe == "exe_ok"
        and build == "build_observed"
        and root == "root_ok"
        and layout == "unique_candidate"
        and process in ("process_none", "process_unique")
    ):
        return "unique_supported_candidate"
    return "unknown"
