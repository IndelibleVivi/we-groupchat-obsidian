"""Recover and cumulatively store local WeChat database keys."""
import json
import hashlib
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import stat
from dataclasses import dataclass, field

from .config import (
    APP_DIR,
    DATA_DIR,
    ensure_private_dir,
    ensure_private_file,
    load_config,
)


C_SOURCE = os.path.join(APP_DIR, "c_src", "find_keys_macos.c")
KEYS_FILE = os.path.join(DATA_DIR, "all_keys.json")
EXTRACT_LOG = os.path.join(DATA_DIR, "extract_keys.log")
DEFAULT_WECHAT_APP = "/Applications/WeChat.app"
WECHAT_PROCESS_NAMES = ("WeChat", "WeChatAppEx", "微信")
WECHAT_PROCESS_PATTERNS = (
    r"/WeChat\.app/Contents/MacOS/WeChat($| )",
    r"/WeChatAppEx\.app/Contents/MacOS/WeChatAppEx($| )",
)
REQUIRED_DATABASE_PATTERNS = (
    re.compile(r"^contact/contact\.db$"),
    re.compile(r"^session/session\.db$"),
    re.compile(r"^emoticon/emoticon\.db$"),
    re.compile(r"^message/(?:biz_)?message_\d+\.db$"),
    re.compile(r"^message/message_fts\.db$"),
)
SQLCIPHER_PAGE_SIZE = 4096
PROTECTED_KEY_MEMORY_MASKS = {
    ("4.1.11", "269136", "arm64"): bytes.fromhex(
        "e8ac38191bd59c963f4654d8f9d7437e"
        "1acc81a5cad6312c7bd0f5e73238d4af"
    ),
}


def _first_pid(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
        return None
    except Exception:
        return None


def process_lookup_available():
    """Return whether this process can inspect macOS process names."""
    try:
        pid = str(os.getpid())
        result = subprocess.run(
            ["ps", "-p", pid, "-o", "pid="],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and any(
            line.strip() == pid for line in result.stdout.splitlines()
        )
    except Exception:
        return False


def get_wechat_pid():
    """Get WeChat main process PID for key scanning."""
    for name in WECHAT_PROCESS_NAMES:
        pid = _first_pid(["pgrep", "-x", name])
        if pid:
            return pid

    for pattern in WECHAT_PROCESS_PATTERNS:
        pid = _first_pid(["pgrep", "-f", pattern])
        if pid:
            return pid

    return None


def is_wechat_running():
    """Check if WeChat is running."""
    return get_wechat_pid() is not None


def get_wechat_app_path():
    """Get WeChat.app path, preferring system-installed location."""
    if os.path.isdir(DEFAULT_WECHAT_APP):
        return DEFAULT_WECHAT_APP
    try:
        result = subprocess.run(
            ["osascript", "-e", 'POSIX path of (path to application "WeChat")'],
            capture_output=True,
            text=True,
        )
        path = result.stdout.strip()
        if result.returncode == 0 and path and os.path.isdir(path):
            return path.rstrip("/")
    except Exception:
        pass

    return None


def get_wechat_build_identity(app_path=None):
    """Installed-bundle diagnostic; interpreter CPU is never scan authority."""
    app_path = app_path or get_wechat_app_path()
    if not app_path:
        return None
    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return None
    version = str(info.get("CFBundleShortVersionString") or "").strip()
    build = str(info.get("CFBundleVersion") or "").strip()
    if not version or not build:
        return None
    return version, build, platform.machine()


def _protected_key_memory_mask(identity=None):
    """Select only an explicitly supplied target identity, never the default app."""
    return PROTECTED_KEY_MEMORY_MASKS.get(identity) if identity else None


@dataclass(frozen=True)
class ScanTarget:
    pid: int
    app_path: str
    executable_path: str
    launched_at: float
    build_identity: tuple
    executable_identity: tuple


@dataclass(frozen=True)
class KeyRecoveryResult:
    status: str
    keys: dict = field(default_factory=dict, repr=False)
    verified_count: int = 0
    missing_databases: tuple = field(default=(), repr=False)
    reason: str = ""

    @property
    def ok(self):
        return bool(self.status == "fresh_verified" and self.verified_count > 0
                    and self.keys and not self.missing_databases)


def get_wechat_scan_target(pid):
    """Bind profile to the selected running app, including its executing CPU.

    AppKit is already a macOS runtime dependency. No AppKit import occurs on
    portable import-only paths. Missing LaunchServices identity fails closed.
    """
    try:
        from AppKit import NSRunningApplication
        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if running is None or running.isTerminated():
            return None
        bundle_url = running.bundleURL()
        executable_url = running.executableURL()
        launch_date = running.launchDate()
        if bundle_url is None or executable_url is None or launch_date is None:
            return None
        app_path = os.path.realpath(str(bundle_url.path()))
        executable = os.path.realpath(str(executable_url.path()))
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as handle:
            info = plistlib.load(handle)
        # The protected profile is for the main application, not a helper.
        if (str(running.bundleIdentifier()) != "com.tencent.xinWeChat"
                or info.get("CFBundleIdentifier") != "com.tencent.xinWeChat"):
            return None
        expected = os.path.realpath(os.path.join(
            app_path, "Contents", "MacOS", str(info.get("CFBundleExecutable") or "")
        ))
        if executable != expected or os.path.basename(executable) != "WeChat":
            return None
        architecture = {0x0100000C: "arm64", 0x01000007: "x86_64"}.get(
            int(running.executableArchitecture())
        )
        version = str(info.get("CFBundleShortVersionString") or "").strip()
        build = str(info.get("CFBundleVersion") or "").strip()
        if not architecture or not version or not build:
            return None
        st = os.stat(executable)
        if not stat.S_ISREG(st.st_mode):
            return None
        return ScanTarget(
            pid, app_path, executable, float(launch_date.timeIntervalSince1970()),
            (version, build, architecture),
            (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns),
        )
    except Exception:
        # Do not surface Cocoa exception text or paths in operator output.
        return None


def get_wechat_scan_targets():
    """Return every verified running main-app target for the WeChat bundle id."""
    try:
        from AppKit import NSRunningApplication
        applications = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            "com.tencent.xinWeChat"
        )
    except Exception:
        return ()
    targets = []
    for application in applications or ():
        try:
            pid = int(application.processIdentifier())
        except Exception:
            continue
        target = get_wechat_scan_target(pid)
        if target is not None:
            targets.append(target)
    return tuple(sorted(targets, key=lambda item: (item.app_path, item.pid)))


def select_wechat_scan_target(app_path=None):
    """Select one exact running main app, rejecting ambiguous installations."""
    targets = get_wechat_scan_targets()
    if app_path:
        expected = os.path.realpath(os.path.expanduser(str(app_path)))
        targets = tuple(item for item in targets if item.app_path == expected)
    return targets[0] if len(targets) == 1 else None


class KeyCacheError(RuntimeError):
    pass


def _private_regular_path(path):
    """Reject non-regular cache/lock entries without changing their targets."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise KeyCacheError("key_cache_path_unsafe")


def _valid_key_entry(item):
    return (isinstance(item, dict) and isinstance(item.get("enc_key"), str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", item["enc_key"]) is not None)


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KeyCacheError("key_cache_duplicate_property")
        result[key] = value
    return result


def _required_pages(db_dir):
    """Read only current required page-one bytes; retain uncertainty explicitly.

    This proves the observed key set only. SourceInventoryStore remains the
    separate authority for missing historical shards and source completeness.
    """
    pages, issues = {}, []
    if not db_dir or not os.path.isdir(db_dir):
        return pages, ["source_unavailable"]
    def onerror(_exc):
        issues.append("source_unreadable")
    for root, dirs, files in os.walk(db_dir, followlinks=False, onerror=onerror):
        for name in list(dirs):
            if os.path.islink(os.path.join(root, name)):
                dirs.remove(name)
                issues.append("source_symlink")
        for name in sorted(files):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, db_dir).replace("\\", "/")
            if not is_required_database(rel):
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as handle:
                    before = os.fstat(handle.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        issues.append(rel)
                        continue
                    page = handle.read(SQLCIPHER_PAGE_SIZE)
                    after = os.fstat(handle.fileno())
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                        after.st_ino, after.st_size, after.st_mtime_ns):
                    issues.append(rel)
                elif page.startswith(b"SQLite format 3\x00"):
                    continue
                elif len(page) != SQLCIPHER_PAGE_SIZE:
                    issues.append(rel)
                else:
                    pages[rel] = page
            except OSError:
                issues.append(rel)
    return pages, sorted(set(issues))


def _verified_mapping(pages, keys):
    from .decryptor import verify_page1
    return {
        rel: {"enc_key": keys[rel]["enc_key"].lower()}
        for rel, page in pages.items()
        if rel in keys and _valid_key_entry(keys[rel])
        and verify_page1(bytes.fromhex(keys[rel]["enc_key"]), page)
    }


def _publish_verified_keys(db_dir, candidates):
    """Reverify, fresh-read, merge and durably replace under one shared lock."""
    from .platform import create_file_lock, LockMode
    directory = os.path.dirname(KEYS_FILE) or "."
    ensure_private_dir(directory)
    lock_path = KEYS_FILE + ".lock"
    _private_regular_path(lock_path)
    with create_file_lock().acquire(lock_path, mode=LockMode.EXCLUSIVE, blocking=True):
        _private_regular_path(KEYS_FILE)
        current = _read_keys_file(KEYS_FILE)
        pages, _issues = _required_pages(db_dir)
        verified = _verified_mapping(pages, candidates)
        if verified:
            current.update(verified)
            _atomic_write_keys(KEYS_FILE, current)
        return current, verified


def is_required_database(rel_path):
    """Return whether the app reads this database directly."""
    normalized = rel_path.replace("\\", "/")
    return any(pattern.match(normalized) for pattern in REQUIRED_DATABASE_PATTERNS)


def is_wechat_signed():
    """Check if WeChat has been re-signed (hardened runtime removed)."""
    app_path = get_wechat_app_path()
    if not app_path:
        return False

    try:
        result2 = subprocess.run(
            ["codesign", "-dvv", app_path],
            capture_output=True, text=True,
        )
        if result2.returncode != 0:
            return False
        flags = result2.stderr
        # Hardened runtime shows "runtime" in flags
        return "runtime" not in flags.lower()
    except Exception:
        return False


_SCANNER_RECEIPT_SCHEMA = "we-groupchat-obsidian.scanner-build.v1"
_SCANNER_POINTER_SCHEMA = "we-groupchat-obsidian.scanner-current.v1"


def _regular_file_bytes(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode):
            raise OSError("not_regular")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _compiler_identity():
    compiler = shutil.which("cc")
    if not compiler:
        raise OSError("compiler_unavailable")
    compiler = os.path.realpath(compiler)
    version = subprocess.run(
        [compiler, "--version"], capture_output=True, text=True, timeout=10,
    )
    target = subprocess.run(
        [compiler, "-dumpmachine"], capture_output=True, text=True, timeout=10,
    )
    if version.returncode or target.returncode:
        raise OSError("compiler_identity_unavailable")
    version_text = (version.stdout or version.stderr or "").strip()
    target_text = (target.stdout or "").strip()
    if not version_text or not target_text:
        raise OSError("compiler_identity_unavailable")
    return {
        "path": compiler,
        "sha256": hashlib.sha256(_regular_file_bytes(compiler)).hexdigest(),
        "version": version_text,
        "target": target_text,
    }


def _scanner_build_spec(target_architecture):
    source = _regular_file_bytes(C_SOURCE)
    compiler = _compiler_identity()
    architecture = str(target_architecture or "").strip()
    if architecture not in {"arm64", "x86_64"}:
        raise OSError("scanner_target_unsupported")
    flags = ["-O2", "-arch", architecture, "-framework", "Foundation"]
    return {
        "schema": _SCANNER_RECEIPT_SCHEMA,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "source_size": len(source),
        "compiler": compiler,
        "target_platform": sys.platform,
        "target_architecture": architecture,
        "flags": flags,
    }


def _scanner_builds_dir():
    return os.path.join(DATA_DIR, "scanner-builds")


def _scanner_pointer_path():
    return os.path.join(DATA_DIR, "scanner-current.json")


def _read_json_regular(path):
    try:
        data = _regular_file_bytes(path)
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _private_directory(path):
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path, 0o700)
        return
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_ISLNK(value.st_mode)
    ):
        raise OSError("scanner_build_directory_unsafe")


@dataclass(frozen=True)
class ScannerBuild:
    binary_path: str
    receipt_path: str
    input_identity: str
    binary_sha256: str


def _scanner_input_identity(spec):
    encoded = json.dumps(
        spec, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_scanner_build(spec):
    pointer = _read_json_regular(_scanner_pointer_path())
    if not pointer or pointer.get("schema") != _SCANNER_POINTER_SCHEMA:
        return None
    input_identity = _scanner_input_identity(spec)
    if pointer.get("input_identity") != input_identity:
        return None
    directory_name = str(pointer.get("build_directory") or "")
    if not re.fullmatch(r"[0-9a-f]{16}-[0-9a-f]{16}-[0-9a-f]{12}", directory_name):
        return None
    builds_dir = os.path.realpath(_scanner_builds_dir())
    build_dir = os.path.realpath(os.path.join(builds_dir, directory_name))
    if os.path.commonpath((builds_dir, build_dir)) != builds_dir:
        return None
    try:
        build_stat = os.lstat(build_dir)
    except OSError:
        return None
    if (
        not stat.S_ISDIR(build_stat.st_mode)
        or stat.S_ISLNK(build_stat.st_mode)
        or build_stat.st_uid != os.getuid()
    ):
        return None
    receipt_path = os.path.join(build_dir, "receipt.json")
    binary_path = os.path.join(build_dir, "find_keys_macos")
    receipt = _read_json_regular(receipt_path)
    if not receipt:
        return None
    for key, value in spec.items():
        if receipt.get(key) != value:
            return None
    if receipt.get("input_identity") != input_identity:
        return None
    expected_binary = str(receipt.get("binary_sha256") or "")
    if expected_binary != str(pointer.get("binary_sha256") or ""):
        return None
    try:
        binary = _regular_file_bytes(binary_path)
        binary_stat = os.stat(binary_path, follow_symlinks=False)
    except OSError:
        return None
    if not (
        re.fullmatch(r"[0-9a-f]{64}", expected_binary)
        and hashlib.sha256(binary).hexdigest() == expected_binary
        and int(receipt.get("binary_size") or -1) == len(binary)
        and binary_stat.st_uid == os.getuid()
        and stat.S_ISREG(binary_stat.st_mode)
        and binary_stat.st_mode & stat.S_IXUSR
    ):
        return None
    return ScannerBuild(binary_path, receipt_path, input_identity, expected_binary)


def _atomic_write_private_json(path, value):
    directory = os.path.dirname(path) or "."
    fd, temporary = tempfile.mkstemp(prefix=".scanner-pointer.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def compile_scanner(target_architecture=None):
    """Return an immutable scanner build admitted by one atomic current pointer."""
    ensure_private_dir(DATA_DIR)
    lock_path = os.path.join(DATA_DIR, "scanner-build.lock")
    try:
        from .platform import LockMode, create_file_lock
        _private_regular_path(lock_path)
        with create_file_lock().acquire(
            lock_path, mode=LockMode.EXCLUSIVE, blocking=True
        ):
            builds_dir = _scanner_builds_dir()
            _private_directory(builds_dir)
            _private_regular_path(_scanner_pointer_path())
            architecture = target_architecture or platform.machine()
            spec = _scanner_build_spec(architecture)
            current = _validated_scanner_build(spec)
            if current is not None:
                return current
            input_identity = _scanner_input_identity(spec)
            source = _regular_file_bytes(C_SOURCE)
            stage = tempfile.mkdtemp(prefix=".scanner-build.", dir=builds_dir)
            try:
                source_path = os.path.join(stage, "find_keys_macos.c")
                source_fd = os.open(
                    source_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(source_fd, "wb") as handle:
                    handle.write(source)
                    handle.flush()
                    os.fsync(handle.fileno())
                binary_path = os.path.join(stage, "find_keys_macos")
                result = subprocess.run(
                    [
                        spec["compiler"]["path"],
                        *spec["flags"],
                        "-o",
                        binary_path,
                        source_path,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode != 0:
                    return None
                os.chmod(binary_path, 0o700)
                binary = _regular_file_bytes(binary_path)
                with open(binary_path, "rb") as handle:
                    os.fsync(handle.fileno())
                receipt = {
                    **spec,
                    "input_identity": input_identity,
                    "binary_sha256": hashlib.sha256(binary).hexdigest(),
                    "binary_size": len(binary),
                }
                receipt_path = os.path.join(stage, "receipt.json")
                receipt_fd = os.open(
                    receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(receipt_fd, "w", encoding="utf-8") as handle:
                    json.dump(receipt, handle, sort_keys=True, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                stage_fd = os.open(stage, os.O_RDONLY)
                try:
                    os.fsync(stage_fd)
                finally:
                    os.close(stage_fd)
                nonce = os.path.basename(stage).rsplit(".", 1)[-1]
                directory_name = (
                    f"{input_identity[:16]}-{receipt['binary_sha256'][:16]}-"
                    f"{hashlib.sha256(nonce.encode()).hexdigest()[:12]}"
                )
                final_dir = os.path.join(builds_dir, directory_name)
                os.rename(stage, final_dir)
                stage = ""
                builds_fd = os.open(builds_dir, os.O_RDONLY)
                try:
                    os.fsync(builds_fd)
                finally:
                    os.close(builds_fd)
                _atomic_write_private_json(_scanner_pointer_path(), {
                    "schema": _SCANNER_POINTER_SCHEMA,
                    "build_directory": directory_name,
                    "input_identity": input_identity,
                    "binary_sha256": receipt["binary_sha256"],
                })
                return _validated_scanner_build(spec)
            finally:
                if stage:
                    shutil.rmtree(stage, ignore_errors=True)
    except Exception:
        return None


def _read_keys_file(path):
    """Missing is empty; corrupt or unsafe is an error, never permission to reset."""
    _private_regular_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            st = os.fstat(handle.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise KeyCacheError("key_cache_path_unsafe")
            if st.st_size > 4 * 1024 * 1024:
                raise KeyCacheError("key_cache_too_large")
            value = json.load(handle, object_pairs_hook=_unique_json_object)
    except (ValueError, UnicodeError) as exc:
        raise KeyCacheError("key_cache_corrupt") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(value, dict):
        raise KeyCacheError("key_cache_corrupt")
    keys = {key: item for key, item in value.items() if not key.startswith("_")}
    for name, item in keys.items():
        normalized = name.replace("\\", "/")
        if (not _valid_key_entry(item) or "\0" in name
                or any(part in {"", ".", ".."} for part in normalized.split("/"))
                or not normalized.endswith(".db")):
            raise KeyCacheError("key_cache_corrupt")
    return keys


def _atomic_write_keys(path, keys):
    directory = os.path.dirname(path) or "."
    ensure_private_dir(directory)
    fd, temporary = tempfile.mkstemp(prefix=".all-keys.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(keys, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        ensure_private_file(path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def recover_keys():
    """Recover freshly verified keys; preserved cache is never fresh success."""
    try:
        cached_keys = _read_keys_file(KEYS_FILE)
    except (OSError, KeyCacheError):
        return KeyRecoveryResult("failed", reason="key_cache_unreadable")
    expected_app = str(
        os.environ.get("WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH") or ""
    ).strip()
    target = select_wechat_scan_target(expected_app or None)
    if target is None:
        return KeyRecoveryResult("failed", reason="target_identity_unavailable")
    pid = target.pid
    try:
        db_dir = os.path.expanduser(str(load_config().get("db_dir") or ""))
        if not db_dir or not os.path.isdir(db_dir):
            return KeyRecoveryResult("failed", reason="source_unavailable")
        db_dir = os.path.realpath(db_dir)
        root_stat = os.stat(db_dir)
        root_identity = (root_stat.st_dev, root_stat.st_ino)
        scanner_build = compile_scanner(target.build_identity[2])
        if not scanner_build:
            return KeyRecoveryResult("failed", reason="scanner_compile_failed")
    except Exception:
        return KeyRecoveryResult("failed", reason="recovery_setup_failed")
    scanner_path = getattr(scanner_build, "binary_path", "")
    if not isinstance(scanner_path, str) or not scanner_path:
        return KeyRecoveryResult("failed", reason="scanner_identity_unavailable")
    scanner_args = [
        scanner_path,
        str(pid),
        os.path.expanduser("~"),
        db_dir,
    ]
    mask = _protected_key_memory_mask(target.build_identity)
    if mask:
        scanner_args.append(mask.hex())
    try:
        ensure_private_dir(DATA_DIR)
        with tempfile.TemporaryDirectory(prefix=".key-scan-", dir=DATA_DIR) as scan_dir:
            os.chmod(scan_dir, 0o700)
            def scan(args):
                if select_wechat_scan_target(expected_app or None) != target:
                    raise KeyCacheError("target_changed")
                return subprocess.run(
                    args, capture_output=True, text=True, cwd=scan_dir, timeout=60,
                )
            result = scan(scanner_args)
            # The current C scanner reports task access denial explicitly.
            # Generic scanner failures must not trigger administrator prompts.
            if result.returncode and "task_for_pid failed:" in (result.stderr or ""):
                result = scan(["sudo", "-n", *scanner_args])
                if result.returncode and any(
                    marker in (result.stderr or "") for marker in (
                        "task_for_pid failed:", "a password is required",
                        "a terminal is required",
                    )
                ):
                    shell_command = (f"cd {shlex.quote(scan_dir)} && "
                                     + " ".join(shlex.quote(v) for v in scanner_args))
                    result = scan([
                        "osascript", "-e",
                        f"do shell script {json.dumps(shell_command)} with administrator privileges",
                    ])
            if result.returncode:
                return KeyRecoveryResult("failed", reason="scanner_failed")
            output = "\n".join((result.stdout or "", result.stderr or ""))
            # Deliberately ignore all_keys.json in staging. C salt association
            # is candidate discovery, never cryptographic evidence.
        if select_wechat_scan_target(expected_app or None) != target:
            return KeyRecoveryResult("failed", reason="target_changed")
        current_root = os.stat(db_dir)
        if (current_root.st_dev, current_root.st_ino) != root_identity:
            return KeyRecoveryResult("failed", reason="source_root_changed")
        matched = _rematch_keys_from_output(db_dir, output, persist=False)
        if not matched:
            status = "unsupported_build" if mask is None else (
                "cache_only" if cached_keys else "failed"
            )
            return KeyRecoveryResult(status, reason="no_fresh_verified_keys")
        merged, fresh = _publish_verified_keys(db_dir, matched)
        pages, issues = _required_pages(db_dir)
        verified = _verified_mapping(pages, merged)
        missing = tuple(sorted(set(issues) | (set(pages) - set(verified))))
        if (not fresh or not pages or set(fresh) != set(matched)
                or any(verified.get(rel) != entry for rel, entry in fresh.items())):
            return KeyRecoveryResult("failed", reason="source_changed_before_publication")
        if select_wechat_scan_target(expected_app or None) != target:
            return KeyRecoveryResult("failed", reason="target_changed")
        final_root = os.stat(db_dir)
        if (final_root.st_dev, final_root.st_ino) != root_identity:
            return KeyRecoveryResult("failed", reason="source_root_changed")
        if missing:
            return KeyRecoveryResult("partial", verified, len(fresh), missing,
                                     "observed_keys_incomplete")
        return KeyRecoveryResult("fresh_verified", verified, len(fresh))
    except subprocess.TimeoutExpired:
        return KeyRecoveryResult("failed", reason="scanner_timeout")
    except KeyCacheError as exc:
        return KeyRecoveryResult("failed", reason=str(exc))
    except Exception:
        return KeyRecoveryResult("failed", reason="key_recovery_failed")


def extract_keys():
    """Compatibility API: only a fresh complete observed-key refresh succeeds.

    Failure/partial recovery leaves the cache on disk for separately verified
    consumers. Call recover_keys() for the structured outcome.
    """
    result = recover_keys()
    return result.keys if result.ok else None


def _parse_raw_keys_from_text(text):
    """Parse scanner key candidates with an optional embedded DB salt."""
    raw_keys = []  # [(key_hex, salt_hex_or_none), ...]
    for line in str(text or "").splitlines():
        line = line.strip()
        parts = line.split()
        if len(parts) == 3 and parts[0] == "WGO_KEY":
            key_hex = parts[1]
            salt_hex = None if parts[2] == "-" else parts[2]
            if len(key_hex) != 64 or (
                salt_hex is not None and len(salt_hex) != 32
            ):
                continue
            try:
                bytes.fromhex(key_hex)
                if salt_hex is not None:
                    bytes.fromhex(salt_hex)
            except ValueError:
                continue
            raw_keys.append(
                (key_hex.lower(), salt_hex.lower() if salt_hex else None)
            )
            continue
        # 格式: "(unknown)  <key_hex 64>  <salt_hex 32>"
        # 或:   "db_name   <key_hex 64>  <salt_hex 32>"
        if len(parts) < 3:
            continue
        key_hex = parts[-2]
        salt_hex = parts[-1]
        if len(key_hex) == 64 and len(salt_hex) == 32:
            try:
                bytes.fromhex(key_hex)
                bytes.fromhex(salt_hex)
                raw_keys.append((key_hex.lower(), salt_hex.lower()))
            except ValueError:
                continue
    return raw_keys


def _parse_raw_keys_from_log(log_path=EXTRACT_LOG):
    """Parse legacy key+salt pairs from extract_keys.log if it exists."""
    raw_keys = []
    if not os.path.exists(log_path):
        return raw_keys
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            raw_keys = _parse_raw_keys_from_text(f.read())
    except OSError:
        pass
    return raw_keys


def _rematch_keys_from_output(db_dir, scanner_output, *, persist=True):
    """Only HMAC-verified candidates may cross the cache publication boundary."""
    from .decryptor import verify_page1
    candidates = sorted({key for key, _salt in _parse_raw_keys_from_text(scanner_output)})
    if not candidates:
        return {}
    pages, _issues = _required_pages(db_dir)
    matched = {}
    for rel, page in pages.items():
        for candidate in candidates:
            if verify_page1(bytes.fromhex(candidate), page):
                matched[rel] = {"enc_key": candidate}
                break
    if matched and persist:
        _merged, matched = _publish_verified_keys(db_dir, matched)
    return matched


def _rematch_keys_from_log(db_dir):
    """Legacy helper for manually recovering from an existing extract_keys.log."""
    try:
        with open(EXTRACT_LOG, encoding="utf-8", errors="replace") as f:
            return _rematch_keys_from_output(db_dir, f.read())
    except OSError:
        return {}


def get_cached_keys():
    """Read the existing cache; availability does not imply a fresh recovery."""
    try:
        return _read_keys_file(KEYS_FILE) or None
    except (OSError, KeyCacheError):
        return None


def check_new_databases(db_dir, keys):
    """Report absent/invalid keys and unreadable current required source files.

    Historical missing shards are still owned by SourceInventoryStore. This
    helper must never equate dictionary membership with a usable current key.
    """
    pages, issues = _required_pages(db_dir)
    normalized = {k.replace("\\", "/"): v for k, v in (keys or {}).items()}
    verified = _verified_mapping(pages, normalized)
    return sorted(set(issues) | (set(pages) - set(verified)))
