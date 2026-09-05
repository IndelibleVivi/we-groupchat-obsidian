"""Recover and cumulatively store local WeChat database keys."""
import json
import os
import platform
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile

from .config import (
    APP_DIR,
    DATA_DIR,
    ensure_private_dir,
    ensure_private_file,
    load_config,
)


C_SOURCE = os.path.join(APP_DIR, "c_src", "find_keys_macos.c")
C_BINARY = os.path.join(DATA_DIR, "find_keys_macos")
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
    """Return the exact app build and local architecture for scan profiles."""
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


def _protected_key_memory_mask():
    identity = get_wechat_build_identity()
    return PROTECTED_KEY_MEMORY_MASKS.get(identity) if identity else None


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


def compile_scanner():
    """Compile C key scanner."""
    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(C_BINARY):
        # Check if recompilation needed
        if os.path.getmtime(C_BINARY) >= os.path.getmtime(C_SOURCE):
            return True

    try:
        result = subprocess.run(
            ["cc", "-O2", "-o", C_BINARY, C_SOURCE, "-framework", "Foundation"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"编译失败: {result.stderr}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"编译失败: {e}", file=sys.stderr)
        return False


def _read_keys_file(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if not key.startswith("_")}


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
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def extract_keys():
    """Run the read-only C scanner, escalating only if task access is denied.

    Returns:
        dict: {db_rel_path: {"enc_key": hex_string}, ...} or None.
    """
    if not compile_scanner():
        return None

    pid = get_wechat_pid()
    if not pid:
        return None
    home_dir = os.path.expanduser("~")
    db_dir = load_config().get("db_dir", "")
    scanner_output = ""
    cached_keys = _read_keys_file(KEYS_FILE)
    scanner_args = [C_BINARY, str(pid), home_dir, db_dir]
    protected_key_mask = _protected_key_memory_mask()
    if protected_key_mask:
        scanner_args.append(protected_key_mask.hex())

    # The scanner owns only a private staging directory. A failed or empty scan
    # must never truncate the cumulative canonical cache.
    ensure_private_dir(DATA_DIR)
    with tempfile.TemporaryDirectory(prefix=".key-scan-", dir=DATA_DIR) as scan_dir:
        os.chmod(scan_dir, 0o700)
        try:
            result = subprocess.run(
                scanner_args,
                capture_output=True, text=True,
                cwd=scan_dir,
                timeout=60,
            )
            scanner_output += "\n".join((result.stdout or "", result.stderr or ""))

            if result.returncode != 0:
                result = subprocess.run(
                    ["sudo", "-n", *scanner_args],
                    capture_output=True,
                    text=True,
                    cwd=scan_dir,
                    timeout=60,
                )
                scanner_output += "\n".join((result.stdout or "", result.stderr or ""))
                if result.returncode != 0:
                    # Last resort: one explicit macOS administrator dialog.
                    shell_command = (
                        f"cd {shlex.quote(scan_dir)} && "
                        + " ".join(shlex.quote(value) for value in scanner_args)
                    )
                    result = subprocess.run(
                        [
                            "osascript",
                            "-e",
                            f"do shell script {json.dumps(shell_command)} "
                            "with administrator privileges",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    scanner_output += "\n".join(
                        (result.stdout or "", result.stderr or "")
                    )
                    if result.returncode != 0:
                        return cached_keys or None

        except subprocess.TimeoutExpired:
            return cached_keys or None
        except Exception:
            return cached_keys or None

        keys = _read_keys_file(os.path.join(scan_dir, "all_keys.json"))

    # Python runs as the logged-in user and authoritatively page-verifies both
    # legacy key+salt and current key-only scanner candidates.
    if db_dir and os.path.isdir(db_dir):
        rematched = _rematch_keys_from_output(
            db_dir, scanner_output, persist=False
        )
        keys = {**keys, **rematched}

    if not keys:
        return cached_keys or None

    merged = {**cached_keys, **keys}
    _atomic_write_keys(KEYS_FILE, merged)
    return merged


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
    """Match captured key candidates against encrypted database page one.

    Solves the issue where root cannot read macOS sandbox files.
    """
    raw_keys = _parse_raw_keys_from_text(scanner_output)
    if not raw_keys:
        return {}

    print(f"[key_extractor] 从 scanner 输出解析到 {len(raw_keys)} 个 key candidate，用 Python 重新匹配...")

    # Build salt -> key_hex index
    salt_to_key = {}
    for key_hex, salt_hex in raw_keys:
        if salt_hex:
            salt_to_key[salt_hex] = key_hex
    unique_candidates = sorted({key_hex for key_hex, _salt_hex in raw_keys})
    from .decryptor import verify_page1

    # Walk all required encrypted DBs. Current WeChat builds retain only the
    # key-only literal, so page-one verification is the authoritative match.
    matched = {}
    for root, _dirs, files in os.walk(db_dir):
        for fname in files:
            if not fname.endswith(".db"):
                continue
            full_path = os.path.join(root, fname)
            rel = os.path.relpath(full_path, db_dir).replace("\\", "/")
            if not is_required_database(rel):
                continue
            try:
                with open(full_path, "rb") as f:
                    page1 = f.read(SQLCIPHER_PAGE_SIZE)
                if len(page1) != SQLCIPHER_PAGE_SIZE:
                    continue
                # Unencrypted SQLite, skip
                if page1.startswith(b"SQLite format 3\x00"):
                    continue
                file_salt = page1[:16].hex().lower()
                ordered_candidates = []
                if file_salt in salt_to_key:
                    ordered_candidates.append(salt_to_key[file_salt])
                ordered_candidates.extend(
                    key_hex
                    for key_hex in unique_candidates
                    if key_hex not in ordered_candidates
                )
                for key_hex in ordered_candidates:
                    if verify_page1(bytes.fromhex(key_hex), page1):
                        matched[rel] = {"enc_key": key_hex}
                        print(f"  ✓ 匹配: {rel}")
                        break
            except OSError:
                continue

    if matched and persist:
        try:
            merged = {**_read_keys_file(KEYS_FILE), **matched}
            _atomic_write_keys(KEYS_FILE, merged)
            print(f"[key_extractor] Python 重新匹配成功: {len(matched)} 个数据库")
        except OSError:
            pass

    return matched


def _rematch_keys_from_log(db_dir):
    """Legacy helper for manually recovering from an existing extract_keys.log."""
    try:
        with open(EXTRACT_LOG, encoding="utf-8", errors="replace") as f:
            return _rematch_keys_from_output(db_dir, f.read())
    except OSError:
        return {}


def get_cached_keys():
    """Get cached keys (without re-extraction)."""
    if not os.path.exists(KEYS_FILE):
        return None
    try:
        with open(KEYS_FILE) as f:
            keys = json.load(f)
        keys = {k: v for k, v in keys.items() if not k.startswith("_")}
        return keys if keys else None
    except (json.JSONDecodeError, OSError):
        return None


def check_new_databases(db_dir, keys):
    """Detect new encrypted databases under db_dir that are missing keys.

    Scans db_storage directory for all .db files, reads first 16 bytes to
    check if encrypted, compares against existing keys, returns list of
    databases missing keys.

    Args:
        db_dir: db_storage directory path.
        keys: Current key dict {rel_path: {"enc_key": ...}}.

    Returns:
        list[str]: Relative paths of databases missing keys.
    """
    missing = []
    normalized_keys = {k.replace("\\", "/") for k in keys}
    for root, _dirs, files in os.walk(db_dir):
        for fname in files:
            if not fname.endswith(".db"):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, db_dir).replace("\\", "/")
            if not is_required_database(rel):
                continue
            if rel in normalized_keys:
                continue  # Already has key
            # Read first 16 bytes to check if encrypted
            try:
                with open(full, "rb") as f:
                    header = f.read(16)
                if len(header) < 16 or header[:15] == b"SQLite format 3":
                    continue  # Too small or unencrypted, skip
                missing.append(rel)
            except OSError:
                continue
    return sorted(missing)
