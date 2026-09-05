"""
WeChat database queries - contacts, group chats, and message reading.
Based on the query logic from the wechat-decrypt project's mcp_server.py.
"""
import html
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import quote

import zstandard as zstd

from .decryptor import WALSnapshotError, decrypt_database, decrypt_wal
from .source_inventory import (
    COMPLETE_STATES,
    SourceInventoryError,
    SourceInventoryStore,
    source_namespaces_for_root,
)

_zstd_dctx = zstd.ZstdDecompressor()

# Regex to extract <title> from XML messages
_RE_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
_RE_XML_FIELD = {}
_RE_RECORDITEM = re.compile(r"<recorditem>(.*?)</recorditem>", re.DOTALL)


def _clean_xml_field(value):
    text = str(value or "").strip()
    if text.startswith("<![CDATA[") and text.endswith("]]>"):
        text = text[9:-3]
    return html.unescape(text).strip()


def _xml_field(text, tag):
    """Extract a simple XML field from WeChat's sometimes-imperfect appmsg XML."""
    try:
        root = ET.fromstring(text)
        elem = root.find(f".//{tag}")
        if elem is not None and elem.text:
            return _clean_xml_field(elem.text)
    except ET.ParseError:
        pass

    pattern = _RE_XML_FIELD.get(tag)
    if pattern is None:
        pattern = re.compile(rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", re.DOTALL)
        _RE_XML_FIELD[tag] = pattern
    match = pattern.search(text)
    if not match:
        return ""
    return _clean_xml_field(match.group(1))


def _recordinfo_candidates(text):
    """Extract embedded WeChat forwarded chat-record XML snippets."""
    candidates = []
    seen = set()

    def add(value):
        value = _clean_xml_field(value)
        if "<recordinfo" not in value:
            return
        key = value[:200]
        if key in seen:
            return
        seen.add(key)
        candidates.append(value)

    try:
        root = ET.fromstring(text)
        for elem in root.findall(".//recorditem"):
            if elem.text:
                add(elem.text)
    except ET.ParseError:
        pass

    for source in (text, html.unescape(text)):
        for match in _RE_RECORDITEM.findall(source):
            add(match)

    return candidates


def _elem_text(elem, tag):
    child = elem.find(tag)
    if child is None or child.text is None:
        return ""
    return _clean_xml_field(child.text)


def _parse_recordinfo(record_xml, max_items=8):
    """Parse WeChat recordinfo XML into a compact readable excerpt."""
    try:
        root = ET.fromstring(record_xml)
    except ET.ParseError:
        return None

    title = _elem_text(root, "title") or "聊天记录"
    desc = _elem_text(root, "desc") or _elem_text(root, "info")
    items = []

    for item in root.findall(".//dataitem"):
        source = _elem_text(item, "sourcename")
        source_time = _elem_text(item, "sourcetime")
        data_title = _elem_text(item, "datatitle")
        data_desc = _elem_text(item, "datadesc")
        data_fmt = _elem_text(item, "datafmt")
        datatype = item.attrib.get("datatype", "")

        if datatype == "8" or data_fmt:
            content = f"[文件] {data_title or data_desc or data_fmt}".strip()
        else:
            content = data_desc or data_title
        content = _clean_xml_field(content)
        if not content:
            continue

        prefix_parts = [x for x in (source_time, source) if x]
        prefix = " ".join(prefix_parts)
        items.append(f"{prefix}: {content}" if prefix else content)
        if len(items) >= max_items:
            break

    if not items and desc:
        items = [
            line.strip()
            for line in desc.splitlines()
            if line.strip()
        ][:max_items]

    if not items:
        return None

    total_count = 0
    datalist = root.find(".//datalist")
    if datalist is not None:
        try:
            total_count = int(datalist.attrib.get("count") or 0)
        except ValueError:
            total_count = 0

    if total_count > len(items):
        items.append(f"... 另有 {total_count - len(items)} 条")

    body = "\n".join(f"- {line}" for line in items)
    return f"[聊天记录] {title}\n{body}"


def _extract_record_summary(text):
    for record_xml in _recordinfo_candidates(text):
        summary = _parse_recordinfo(record_xml)
        if summary:
            return summary
    return ""


def _clean_msg_text(text):
    """Clean WeChat message text, handle XML-formatted special messages.

    Converts pat-pat, quoted replies, images, stickers, etc. from XML to readable text.
    Plain text messages are returned as-is.

    Returns:
        str | None: Cleaned text, or None if the message should be skipped.
    """
    if not text:
        return None

    stripped = text.strip()

    # Plain text: return directly if not starting with <
    if not stripped.startswith("<"):
        return text

    # ── System messages (sysmsg) → skip ──
    if "<sysmsg" in stripped:
        return None

    # ── appmsg: quoted replies, links, files, pat ──
    if "<appmsg" in stripped:
        title = _xml_field(stripped, "title")
        if not title:
            m = _RE_TITLE.search(stripped)
            title = _clean_xml_field(m.group(1)) if m else ""
        if not title:
            record_summary = _extract_record_summary(stripped)
            if record_summary:
                return record_summary
            url = _xml_field(stripped, "url")
            if url:
                return f"[链接] {url}"
            return None
        # Pat-pat: title contains "拍了拍"
        if "拍了拍" in title:
            return None
        # Quoted reply (type=57): extract title as reply content
        if "<type>57</type>" in stripped:
            record_summary = _extract_record_summary(stripped)
            if record_summary:
                return f"[回复] {title}\n{record_summary}"
            return f"[回复] {title}"
        # File (type=6)
        if "<type>6</type>" in stripped:
            return f"[文件] {title}"
        # Forwarded chat record (type=19)
        if "<type>19</type>" in stripped:
            record_summary = _extract_record_summary(stripped)
            if record_summary:
                return record_summary
            return f"[聊天记录] {title}"
        # Link/article (type=5 or other)
        url = _xml_field(stripped, "url")
        return f"[链接] {title} {url}".strip()

    # ── Image ──
    if "<img " in stripped or "<img>" in stripped:
        return "[图片]"

    # ── Sticker ──
    if "<emoji " in stripped or "<emoji>" in stripped:
        return "[表情]"

    # ── Voice ──
    if "<voicemsg " in stripped:
        return "[语音]"

    # ── Video ──
    if "<videomsg " in stripped:
        return "[视频]"

    # ── Other unrecognizable XML → skip ──
    if stripped.startswith("<?xml") or stripped.startswith("<msg"):
        return None

    # Other text: return as-is
    return text


def _optional_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_source_value(value):
    if value is None or isinstance(value, (str, int, float)):
        return value
    return str(value)


def _xml_attribute(text, element_name, attribute_name):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return ""
    elem = root.find(f".//{element_name}")
    return _clean_xml_field(elem.attrib.get(attribute_name, "")) if elem is not None else ""


def _file_resource_metadata(text):
    if "<appmsg" not in text or "<type>6</type>" not in text:
        return None
    name = _xml_field(text, "title")
    if not name:
        return None
    size = None
    for tag in ("totallen", "filesize", "size"):
        size = _optional_int(_xml_field(text, tag))
        if size is not None:
            break
    declared_hash = ""
    for tag in ("md5", "filemd5"):
        value = _xml_field(text, tag).strip().lower()
        if re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", value):
            declared_hash = value
            break
    attach_id = _xml_field(text, "attachid")
    extension = _xml_field(text, "fileext").lstrip(".")
    if not extension:
        suffix = os.path.splitext(name)[1].lstrip(".")
        extension = suffix[:20]
    resource = {
        "kind": "file",
        "resource_index": 0,
        "original_name": name[:240],
        "extension": extension[:20],
    }
    if size is not None and size >= 0:
        resource["declared_size"] = size
    if declared_hash:
        resource["declared_hash"] = declared_hash
        if len(declared_hash) == 32:
            resource["md5"] = declared_hash
    if attach_id:
        resource["attach_id"] = attach_id[:240]
    return resource


class WeChatSourceDegraded(RuntimeError):
    """A privacy-safe source read failure that must not advance a consumer cursor."""

    def __init__(self, code="source_shard_unavailable"):
        super().__init__(code)
        self.code = str(code or "source_shard_unavailable")


class WeChatDB:
    """WeChat database query interface."""

    CACHE_DIR = os.path.join(tempfile.gettempdir(), "we_groupchat_obsidian_cache")

    def __init__(self, db_dir, keys, *, source_inventory_store=None):
        """
        Args:
            db_dir: WeChat db_storage directory path.
            keys: {rel_path: {"enc_key": hex}, ...} encryption key dictionary.
        """
        self.db_dir = os.path.abspath(os.path.expanduser(db_dir))
        self.keys = keys
        self._db_cache = {}
        self._contacts = None  # {username: display_name}
        self._contacts_full = None  # [{username, nick_name, remark}]
        self._nick_to_remark = {}  # {nick_name: remark} reverse mapping for nickname→alias lookup
        self._emoticon_map = None  # {md5: {cdn_url, aes_key, thumb_url}}
        self._source_snapshot_depth = 0
        self._source_snapshot_paths = {}
        self.source_inventory_store = (
            source_inventory_store
            if source_inventory_store is not None
            else SourceInventoryStore(path=None)
        )
        self._inventory_specs_by_generation = {}
        self.cache_namespace, self.source_namespace = source_namespaces_for_root(
            self.db_dir
        )
        self.cache_dir = os.path.join(self.CACHE_DIR, self.cache_namespace)
        os.makedirs(self.cache_dir, exist_ok=True)
        try:
            os.chmod(self.CACHE_DIR, 0o700)
            os.chmod(self.cache_dir, 0o700)
        except OSError:
            pass

    @classmethod
    def for_runtime(cls, db_dir, keys):
        """Create a source reader bound to the durable private inventory."""
        return cls(
            db_dir,
            keys,
            source_inventory_store=SourceInventoryStore(),
        )

    @staticmethod
    def _file_identity(path):
        try:
            value = os.stat(path)
        except OSError:
            return None
        return (
            int(value.st_dev), int(value.st_ino), int(value.st_size),
            int(value.st_mtime_ns),
        )

    def _key_fingerprint(self, rel_path):
        key = self._get_key(rel_path)
        if not key:
            return "no-key"
        return hashlib.sha256(str(key).encode("utf-8")).hexdigest()

    def _cache_path(self, rel_path):
        normalized = str(rel_path or "").replace("\\", "/")
        rel_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        identity = hashlib.sha256(
            f"{normalized}\0{self._key_fingerprint(rel_path)}".encode("utf-8")
        ).hexdigest()
        return os.path.join(self.cache_dir, f"{rel_digest}-{identity}.db")

    def _fallback_cache_path(self, rel_path):
        normalized = str(rel_path or "").replace("\\", "/")
        rel_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        try:
            matches = [
                os.path.join(self.cache_dir, name)
                for name in os.listdir(self.cache_dir)
                if name.startswith(rel_digest + "-") and name.endswith(".db")
            ]
        except OSError:
            return ""
        return matches[0] if len(matches) == 1 else ""

    def refresh_cache_view(self):
        """Clear in-memory DB/cache metadata without deleting cached DB files."""
        self._db_cache.clear()
        self._contacts = None
        self._contacts_full = None
        self._nick_to_remark = {}
        self._emoticon_map = None

    @contextmanager
    def source_snapshot(self):
        """Pin complete decrypted files for one multi-page source traversal."""
        self._source_snapshot_depth += 1
        try:
            yield self
        finally:
            self._source_snapshot_depth -= 1
            if self._source_snapshot_depth == 0:
                paths = list(self._source_snapshot_paths.values())
                self._source_snapshot_paths.clear()
                for path in paths:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def _pin_source_snapshot(self, rel_path, path):
        if self._source_snapshot_depth <= 0:
            return path
        pinned = self._source_snapshot_paths.get(rel_path)
        if pinned and os.path.isfile(pinned):
            return pinned
        fd, pinned = tempfile.mkstemp(
            prefix=f".snapshot-{hashlib.md5(rel_path.encode()).hexdigest()[:12]}-",
            suffix=".db",
            dir=self.cache_dir,
        )
        os.close(fd)
        source = None
        destination = None
        try:
            source = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True)
            destination = sqlite3.connect(pinned)
            source.backup(destination)
            destination.commit()
        except sqlite3.Error as exc:
            try:
                os.unlink(pinned)
            except OSError:
                pass
            raise WeChatSourceDegraded("source_snapshot_failed") from exc
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
        try:
            os.chmod(pinned, 0o600)
        except OSError:
            pass
        self._source_snapshot_paths[rel_path] = pinned
        return pinned

    def invalidate_cache(self):
        """Clear all DB decryption caches, force re-decrypt on next query."""
        self.refresh_cache_view()
        # Clean up cached files on disk
        if os.path.isdir(self.cache_dir):
            import glob as _glob
            for f in _glob.glob(os.path.join(self.cache_dir, "*.db")):
                try:
                    os.remove(f)
                except OSError:
                    pass

    def _get_key(self, rel_path):
        """Get database encryption key."""
        normalized = rel_path.replace("\\", "/")
        for candidate in (rel_path, normalized, normalized.replace("/", "\\")):
            if candidate in self.keys:
                return self.keys[candidate]["enc_key"]
        return None

    @staticmethod
    def _is_plain_sqlite(path):
        """Check if file is an unencrypted SQLite database."""
        try:
            with open(path, "rb") as f:
                return f.read(15) == b"SQLite format 3"
        except OSError:
            return False

    def _get_decrypted_db(self, rel_path):
        """Get decrypted database path (with caching).

        If the database is plaintext SQLite (unencrypted), returns the original path directly.
        """
        db_path = os.path.join(self.db_dir, rel_path)
        pinned = self._source_snapshot_paths.get(rel_path)
        if pinned and os.path.isfile(pinned):
            return pinned
        if not os.path.exists(db_path):
            return None

        # ── If plaintext SQLite, return original path directly ──
        if self._is_plain_sqlite(db_path):
            return self._pin_source_snapshot(rel_path, db_path)

        # ── Decryption needed ──
        enc_key = self._get_key(rel_path)
        if not enc_key:
            return None

        key_fingerprint = self._key_fingerprint(rel_path)
        wal_path = db_path + "-wal"
        cache_path = self._cache_path(rel_path)
        for _attempt in range(2):
            db_identity = self._file_identity(db_path)
            wal_identity = self._file_identity(wal_path)
            if db_identity is None:
                return None

            record = self._db_cache.get(rel_path)
            if record is not None and (
                record["db_identity"] == db_identity
                and record["wal_identity"] == wal_identity
                and record["key_fingerprint"] == key_fingerprint
                and record["published_identity"] == self._file_identity(cache_path)
            ):
                return self._pin_source_snapshot(rel_path, cache_path)

            fd, temp_path = tempfile.mkstemp(
                prefix=".partial-", suffix=".db", dir=self.cache_dir
            )
            os.close(fd)
            try:
                pages = decrypt_database(db_path, temp_path, enc_key)
                if pages == 0:
                    return None
                if wal_identity is not None:
                    decrypt_wal(wal_path, temp_path, enc_key)

                # A checkpoint/reset/replacement during reconstruction can mix
                # database generations.  Publish only when both source files
                # still have the exact identities observed before decryption.
                if (
                    self._file_identity(db_path) != db_identity
                    or self._file_identity(wal_path) != wal_identity
                ):
                    continue
                try:
                    os.chmod(temp_path, 0o600)
                except OSError:
                    pass
                os.replace(temp_path, cache_path)
                temp_path = ""
            except WALSnapshotError as exc:
                raise WeChatSourceDegraded("source_snapshot_failed") from exc
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

            self._db_cache[rel_path] = {
                "db_identity": db_identity,
                "wal_identity": wal_identity,
                "key_fingerprint": key_fingerprint,
                "cache_path": cache_path,
                "published_identity": self._file_identity(cache_path),
            }
            return self._pin_source_snapshot(rel_path, cache_path)
        raise WeChatSourceDegraded("source_snapshot_failed")

    def _load_contacts(self):
        """Load contacts."""
        if self._contacts is not None:
            return

        path = self._get_decrypted_db(os.path.join("contact", "contact.db"))
        if not path:
            self._contacts = {}
            self._contacts_full = []
            self._nick_to_remark = {}
            return

        names = {}
        full = []
        nick_to_remark = {}  # nickname → alias (reverse mapping for looking up alias by nickname in group messages)
        conn = sqlite3.connect(path)
        try:
            for r in conn.execute("SELECT username, nick_name, remark FROM contact"):
                uname, nick, remark = r
                remark = (remark or "").strip()
                nick = (nick or "").strip()
                display = remark if remark else nick if nick else uname
                names[uname] = display
                full.append({"username": uname, "nick_name": nick or "", "remark": remark or ""})
                # If alias exists, build nickname→alias reverse mapping
                if remark and nick:
                    nick_to_remark[nick] = remark
        except Exception as e:
            print(f"[wechat_db] warning: 加载联系人失败: {e}", file=__import__('sys').stderr)
        finally:
            conn.close()

        self._contacts = names
        self._contacts_full = full
        self._nick_to_remark = nick_to_remark

    def _load_emoticon_db(self):
        """Lazy-load emoticon.db to build md5→CDN URL mapping for stickers."""
        if self._emoticon_map is not None:
            return
        self._emoticon_map = {}
        path = self._get_decrypted_db(os.path.join("emoticon", "emoticon.db"))
        if not path:
            return
        try:
            conn = sqlite3.connect(path)
            rows = conn.execute(
                "SELECT md5, cdn_url, aes_key, thumb_url"
                " FROM kNonStoreEmoticonTable"
            ).fetchall()
            for md5, cdn, aes, thumb in rows:
                if md5 and cdn:
                    self._emoticon_map[md5] = {
                        "cdn_url": cdn.replace("&amp;", "&"),
                        "aes_key": aes or "",
                        "thumb_url": (thumb or "").replace("&amp;", "&"),
                    }
            conn.close()
        except Exception:
            pass

    def get_groups(self, include_unnamed=False):
        """Get all group chats list.

        Args:
            include_unnamed: Whether to include unnamed groups (those displaying raw IDs).

        Returns:
            list[dict]: [{"username": "xxx@chatroom", "name": "group name"}, ...]
        """
        self._load_contacts()
        groups = []
        unnamed_idx = 0
        for c in self._contacts_full:
            if "@chatroom" in c["username"]:
                name = c["remark"] or c["nick_name"] or c["username"]
                if name == c["username"]:
                    if include_unnamed:
                        unnamed_idx += 1
                        groups.append({"username": c["username"],
                                       "name": f"无名称群{unnamed_idx}"})
                    else:
                        # Check for messages; if found, keep and assign number
                        msgs = self.get_messages(c["username"], limit=1)
                        if msgs:
                            unnamed_idx += 1
                            groups.append({"username": c["username"],
                                           "name": f"无名称群{unnamed_idx}"})
                else:
                    groups.append({"username": c["username"], "name": name})
        return groups

    def get_recent_sessions(self, limit=200):
        """Get recent sessions (groups and private chats)."""
        path = self._get_decrypted_db(os.path.join("session", "session.db"))
        if not path:
            return []

        self._load_contacts()
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute("""
                SELECT username, unread_count, summary, last_timestamp
                FROM SessionTable
                WHERE last_timestamp > 0
                ORDER BY last_timestamp DESC
                LIMIT ?
            """, (limit,)).fetchall()
        except Exception:
            return []
        finally:
            conn.close()

        sessions = []
        unnamed_idx = 0
        for username, unread, summary, ts in rows:
            display = self._contacts.get(username, username)
            is_group = "@chatroom" in username

            # Unnamed group: has SessionTable record indicating activity, show with number
            if is_group and display == username:
                unnamed_idx += 1
                display = f"无名称群{unnamed_idx}"

            if isinstance(summary, bytes):
                try:
                    summary = _zstd_dctx.decompress(summary).decode("utf-8", errors="replace")
                except Exception:
                    summary = ""
            if isinstance(summary, str) and ":\n" in summary:
                summary = summary.split(":\n", 1)[1]

            sessions.append({
                "username": username,
                "name": display,
                "is_group": is_group,
                "unread": unread or 0,
                "summary": summary or "",
                "timestamp": ts,
                "time_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
            })

        return sessions

    def _find_msg_table(self, username):
        """Find all databases containing a user's message table.

        Searches all message-related database files (message_N.db and biz_message_N.db),
        returns all database paths containing the table (messages may span multiple DBs).

        Returns:
            (list[str], str): (db_paths, table_name), returns ([], None) if not found.
        """
        table_hash = hashlib.md5(username.encode()).hexdigest()
        table_name = f"Msg_{table_hash}"

        found_paths = []

        # 1) Find message databases with keys
        msg_keys = sorted([
            k for k in self.keys
            if re.search(r"message[/\\](?:biz_)?message_\d+\.db$", k.replace("\\", "/"))
        ])

        for rel_key in msg_keys:
            path = self._get_decrypted_db(rel_key)
            if not path:
                continue
            conn = sqlite3.connect(path)
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,),
                ).fetchone()
                if exists:
                    found_paths.append(path)
            except Exception:
                pass
            finally:
                conn.close()

        # 2) If not found, search for old decrypted files in cache
        if not found_paths:
            for prefix in ("message", "biz_message"):
                for i in range(10):
                    rel = f"message/{prefix}_{i}.db"
                    if rel in self.keys:
                        continue
                    cache_path = self._fallback_cache_path(rel)
                    if not os.path.exists(cache_path):
                        continue
                    conn = sqlite3.connect(cache_path)
                    try:
                        exists = conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table_name,),
                        ).fetchone()
                        if exists:
                            found_paths.append(cache_path)
                    except Exception:
                        pass
                    finally:
                        conn.close()

        if found_paths:
            return found_paths, table_name
        return [], None

    def count_messages_since(self, username, since_ts):
        """Quick count of new messages since since_ts (reads cached copy only, safe).

        Returns:
            int: Number of new messages, -1 on query failure.
        """
        db_paths, table_name = self._find_msg_table(username)
        if not db_paths:
            return -1

        total = 0
        for db_path in db_paths:
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM [{table_name}] WHERE create_time > ?",
                    (since_ts,),
                ).fetchone()
                total += row[0] if row else 0
            except Exception:
                pass
            finally:
                conn.close()
        return total

    @staticmethod
    def _table_columns(conn, table_name):
        return {
            str(row[1]).lower(): str(row[1])
            for row in conn.execute(f"PRAGMA table_info([{table_name}])")
        }

    @staticmethod
    def _column_expr(columns, aliases, output_name, default="NULL"):
        for alias in aliases:
            actual = columns.get(alias.lower())
            if actual:
                return f"[{actual}] AS [{output_name}]"
        return f"{default} AS [{output_name}]"

    @staticmethod
    def _rowid_expr(conn, table_name):
        try:
            conn.execute(f"SELECT rowid FROM [{table_name}] LIMIT 0")
            return "rowid AS [__rowid]"
        except sqlite3.Error:
            return "NULL AS [__rowid]"

    def _db_shard_identity(self, db_path):
        path = os.path.abspath(str(db_path))
        try:
            source_stat = os.lstat(path)
        except OSError:
            source_identity = "missing"
        else:
            source_identity = (
                f"{source_stat.st_dev}:{source_stat.st_ino}"
                if stat.S_ISREG(source_stat.st_mode)
                else "not-regular"
            )
        namespace = str(getattr(self, "cache_namespace", "legacy-unscoped"))
        return hashlib.sha256(
            f"wechat-db-shard-v3\0{namespace}\0{path}\0{source_identity}".encode()
        ).hexdigest()[:20]

    def _source_message_shard_identity(self, rel_key, *, cache_path=""):
        """Return a logical shard identity for durable source-message IDs.

        Cursor admission continues to use the physical generation identity.
        Keeping that generation out of the message ID lets an explicitly
        reconciled replacement recognize the same logical source row instead
        of manufacturing a second identity solely because cache bytes/inodes
        were rebuilt.
        """
        source_path = os.path.join(self.db_dir, rel_key)
        if cache_path:
            identity_path = cache_path
        elif self._is_plain_sqlite(source_path):
            identity_path = source_path
        else:
            identity_path = self._cache_path(rel_key)
        basename = os.path.basename(str(identity_path))
        namespace = str(getattr(self, "cache_namespace", "legacy-unscoped"))
        return hashlib.sha256(
            f"wechat-db-shard-v2\0{namespace}\0{basename}".encode()
        ).hexdigest()[:20]

    @staticmethod
    def _source_message_id(username, envelope):
        parts = ["wechat-source-message-v1", hashlib.sha256(username.encode()).hexdigest()]
        for key in ("db_shard_id", "server_id", "local_id", "sort_seq", "rowid", "create_time", "local_type"):
            parts.append(f"{key}={envelope.get(key, '')}")
        return "wgmsg_" + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]

    def _resource_metadata(self, text, local_type, packed_info):
        resources = []
        file_resource = _file_resource_metadata(text)
        if file_resource:
            resources.append(file_resource)
        if int(local_type or 0) == 3 or "<img " in text or "<img>" in text:
            file_hash = ""
            if packed_info and isinstance(packed_info, bytes):
                file_hash = self._extract_file_hash_from_protobuf(packed_info) or ""
            if not file_hash:
                for attribute in ("md5", "cdnmidimgurl"):
                    value = _xml_attribute(text, "img", attribute).strip().lower()
                    if re.fullmatch(r"[0-9a-f]{32}", value):
                        file_hash = value
                        break
            image = {"kind": "image", "resource_index": len(resources), "original_name": ""}
            if file_hash:
                image["declared_hash"] = file_hash
                image["md5"] = file_hash
            resources.append(image)
        return resources

    def _get_messages_from_paths(
        self,
        username,
        db_paths,
        table_name,
        since_ts=0,
        limit=500,
        page_forward=False,
        since_inclusive=False,
        include_filtered=False,
        db_shard_id="",
        after_cursor=None,
    ):
        self._load_contacts()
        is_group = "@chatroom" in username
        if not db_paths:
            return []

        # Collect rows from all DBs, merge, sort, and truncate
        all_rows = []
        for db_path in db_paths:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                columns = self._table_columns(conn, table_name)
                select_fields = [
                    self._column_expr(columns, ("local_type",), "local_type", "0"),
                    self._column_expr(columns, ("create_time",), "create_time", "0"),
                    self._column_expr(columns, ("message_content",), "message_content", "NULL"),
                    self._column_expr(columns, ("WCDB_CT_message_content",), "content_type", "NULL"),
                    self._column_expr(columns, ("status",), "status", "0"),
                    self._column_expr(columns, ("local_id", "localid"), "local_id"),
                    self._column_expr(columns, ("server_id", "serverid"), "server_id"),
                    self._column_expr(columns, ("sort_seq", "sortseq"), "sort_seq"),
                    self._column_expr(columns, ("packed_info_data", "packedinfodata"), "packed_info_data"),
                    self._rowid_expr(conn, table_name),
                ]
                select_sql = ", ".join(select_fields)
                if after_cursor is not None and page_forward:
                    after_timestamp, after_rowid = after_cursor
                    rows = conn.execute(f"""
                        SELECT {select_sql}
                        FROM [{table_name}]
                        WHERE create_time > ?
                           OR (create_time = ? AND rowid > ?)
                        ORDER BY create_time ASC, rowid ASC
                        LIMIT ?
                    """, (
                        int(after_timestamp), int(after_timestamp),
                        int(after_rowid), max(1, int(limit)),
                    )).fetchall()
                elif since_ts > 0:
                    order = "ASC" if page_forward else "DESC"
                    comparison = ">=" if since_inclusive else ">"
                    rows = conn.execute(f"""
                        SELECT {select_sql}
                        FROM [{table_name}]
                        WHERE create_time {comparison} ?
                        ORDER BY create_time {order}, rowid {order}
                        LIMIT ?
                    """, (since_ts, limit * 2)).fetchall()
                else:
                    order = "ASC" if page_forward else "DESC"
                    rows = conn.execute(f"""
                        SELECT {select_sql}
                        FROM [{table_name}]
                        ORDER BY create_time {order}, rowid {order}
                        LIMIT ?
                    """, (limit,)).fetchall()
                shard_id = str(db_shard_id or self._db_shard_identity(db_path))
                for row in rows:
                    item = dict(row)
                    item["db_shard_id"] = shard_id
                    all_rows.append(item)
            except Exception as exc:
                raise WeChatSourceDegraded("source_shard_unavailable") from exc
            finally:
                conn.close()

        if not all_rows:
            return []

        # Sort by time, take the latest limit entries
        all_rows.sort(key=lambda row: (
            int(row.get("create_time") or 0),
            int(row.get("__rowid") or 0),
        ))
        if page_forward:
            rows = all_rows[:limit]
        else:
            rows = all_rows[-limit:]

        # In private chat, label counterpart's messages with display name
        contact_name = ""
        if not is_group:
            contact_name = self._contacts.get(username, username)

        messages = []
        for row in rows:
            local_type = row["local_type"]
            create_time = row["create_time"]
            content = row["message_content"]
            ct = row["content_type"]
            status = row["status"]
            packed_info = row.get("packed_info_data")
            # zstd decompression
            if ct and ct == 4 and isinstance(content, bytes):
                try:
                    content = _zstd_dctx.decompress(content).decode("utf-8", errors="replace")
                except Exception as exc:
                    # Failed decoding is an unreadable source row, not an
                    # intentionally filtered envelope that may advance a cursor.
                    raise WeChatSourceDegraded("source_message_decode_failed") from exc
            elif isinstance(content, bytes):
                try:
                    content = content.decode("utf-8", errors="replace")
                except Exception:
                    content = None

            if content is None:
                if not include_filtered:
                    continue
                content = ""

            sender = ""
            raw_sender_id = ""
            text = content
            if is_group and ":\n" in content:
                raw_sender_id, text = content.split(":\n", 1)
                sender = self._contacts.get(raw_sender_id)
                if not sender or not sender.strip():
                    sender = self._nick_to_remark.get(raw_sender_id)
                if not sender or not sender.strip():
                    sender = raw_sender_id
            elif not is_group:
                sender = "我" if status == 2 else contact_name

            if is_group and (not sender or not sender.strip()):
                sender = "您"

            source_envelope = {
                "db_shard_id": row["db_shard_id"],
                "local_id": _safe_source_value(row.get("local_id")),
                "server_id": _safe_source_value(row.get("server_id")),
                "sort_seq": _safe_source_value(row.get("sort_seq")),
                "rowid": _safe_source_value(row.get("__rowid")),
                "create_time": create_time,
                "local_type": local_type,
                "packed_info_present": packed_info is not None,
            }
            source_message_id = self._source_message_id(username, source_envelope)
            source_envelope["source_message_id"] = source_message_id
            resources = self._resource_metadata(text, local_type, packed_info)
            for index, resource in enumerate(resources):
                resource["resource_index"] = index
                resource["source_message_id"] = source_message_id

            # Clean XML special messages only after structured source metadata is captured.
            cleaned = _clean_msg_text(text)
            if cleaned is None:
                if not include_filtered:
                    continue
                text = ""
            else:
                text = cleaned

            messages.append({
                "sender": sender,
                "raw_sender_id": raw_sender_id,
                "text": text,
                "timestamp": create_time,
                "time_str": datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M"),
                "type": local_type,
                "source_message_id": source_message_id,
                "source_envelope": source_envelope,
                "resources": resources,
            })

        return messages

    @staticmethod
    def _opened_regular_prefix(path):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            source_stat = os.fstat(fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise OSError("not regular")
            prefix = os.read(fd, 16)
        finally:
            os.close(fd)
        return source_stat, prefix

    def _source_generation_marker(
        self,
        rel_key,
        *,
        cache_path="",
        source_stat=None,
        prefix=None,
    ):
        if source_stat is None or prefix is None:
            path = cache_path or os.path.join(self.db_dir, rel_key)
            try:
                source_stat, prefix = self._opened_regular_prefix(path)
            except OSError:
                return "missing"
        return hashlib.sha256(
            b"wechat-db-generation-v1\0"
            + f"{source_stat.st_dev}:{source_stat.st_ino}\0".encode("ascii")
            + prefix
            + b"\0"
            + self._key_fingerprint(rel_key).encode("ascii")
        ).hexdigest()

    def _source_shard_identity(
        self,
        rel_key,
        *,
        cache_path="",
        source_stat=None,
        prefix=None,
    ):
        normalized = str(rel_key or "").replace("\\", "/").lower()
        namespace = str(getattr(self, "source_namespace", "legacy-unscoped"))
        generation = self._source_generation_marker(
            rel_key,
            cache_path=cache_path,
            source_stat=source_stat,
            prefix=prefix,
        )
        return hashlib.sha256(
            f"wechat-message-source-shard-v3\0{namespace}\0{normalized}\0{generation}".encode(
                "utf-8"
            )
        ).hexdigest()[:20]

    def _message_shard_observations(self, known_paths=()):
        observations = []
        specs_by_generation = {}
        error_codes = []
        keys = getattr(self, "keys", {}) or {}
        db_dir = str(getattr(self, "db_dir", "") or "")
        rel_key_set = {
            str(key).replace("\\", "/")
            for key in keys
            if re.search(r"message/(?:biz_)?message_\d+\.db$", str(key).replace("\\", "/"))
        }
        rel_key_set.update(str(value).replace("\\", "/") for value in known_paths)
        message_dir = os.path.join(db_dir, "message") if db_dir else ""
        if message_dir and os.path.isdir(message_dir):
            try:
                for entry in os.scandir(message_dir):
                    if entry.is_file(follow_symlinks=False) and re.fullmatch(
                        r"(?:biz_)?message_\d+\.db", entry.name
                    ):
                        rel_key_set.add("message/" + entry.name)
            except OSError:
                error_codes.append("source_inventory_scan_failed")
        elif message_dir and os.path.lexists(message_dir):
            error_codes.append("source_inventory_scan_failed")

        # Recover compatibility cache candidates even when neither a current
        # key nor a live file names the old shard.
        for prefix in ("message", "biz_message"):
            for index in range(10):
                rel_key = f"message/{prefix}_{index}.db"
                if self._fallback_cache_path(rel_key):
                    rel_key_set.add(rel_key)

        for rel_key in sorted(rel_key_set):
            source_path = os.path.join(db_dir, rel_key) if db_dir else ""
            cache_path = self._fallback_cache_path(rel_key)
            state = "missing_file"
            generation_id = ""
            spec = None
            if source_path and os.path.lexists(source_path):
                try:
                    source_stat, prefix = self._opened_regular_prefix(source_path)
                except OSError:
                    state = "unreadable"
                else:
                    generation_id = self._source_shard_identity(
                        rel_key,
                        source_stat=source_stat,
                        prefix=prefix,
                    )
                    is_plain = prefix[:15] == b"SQLite format 3"
                    if not is_plain and not self._get_key(rel_key):
                        state = "key_missing"
                    else:
                        state = "present"
                        spec = {
                            "source_shard_id": generation_id,
                            "rel_key": rel_key,
                            "cache_path": "",
                            "cache_only": False,
                        }
            elif cache_path:
                try:
                    cache_stat, cache_prefix = self._opened_regular_prefix(cache_path)
                except OSError:
                    state = "unreadable"
                else:
                    state = "cache_only"
                    generation_id = self._source_shard_identity(
                        rel_key,
                        cache_path=cache_path,
                        source_stat=cache_stat,
                        prefix=cache_prefix,
                    )
                    spec = {
                        "source_shard_id": generation_id,
                        "rel_key": rel_key,
                        "cache_path": cache_path,
                        "cache_only": True,
                    }

            observations.append({
                "relative_path": rel_key,
                "generation_id": generation_id,
                "state": state,
            })
            if spec is not None:
                specs_by_generation[generation_id] = spec
        return observations, specs_by_generation, error_codes

    def _source_inventory_snapshot(self, *, update=True):
        try:
            if not update:
                return self.source_inventory_store.inspect(self.source_namespace)
            prior = self.source_inventory_store.inspect(self.source_namespace)
            known_paths = [row["relative_path"] for row in prior.shards]
            observations, specs, error_codes = self._message_shard_observations(
                known_paths
            )
            snapshot = self.source_inventory_store.reconcile(
                self.source_namespace,
                observations,
                error_codes=error_codes,
            )
        except SourceInventoryError as exc:
            raise WeChatSourceDegraded(exc.code) from exc
        self._inventory_specs_by_generation = specs
        return snapshot

    def get_source_inventory(self, *, update=True, sensitive=False):
        """Return one completeness snapshot without absolute source paths."""
        return self._source_inventory_snapshot(update=update).as_dict(
            sensitive=bool(sensitive)
        )

    def _message_shard_specs(self):
        snapshot = self._source_inventory_snapshot(update=True)
        return [
            dict(self._inventory_specs_by_generation[generation_id])
            for generation_id in snapshot.present_generation_ids
            if generation_id in self._inventory_specs_by_generation
        ]

    def get_message_shards(self, _username):
        """Return privacy-safe identities for every known message shard."""
        snapshot = self._source_inventory_snapshot(update=True)
        if not snapshot.complete:
            if (
                snapshot.counts.get("cache_only")
                and sum(snapshot.counts.get(state, 0) for state in COMPLETE_STATES) == 0
            ):
                raise WeChatSourceDegraded("source_cache_only")
            if snapshot.counts.get("key_missing"):
                raise WeChatSourceDegraded("source_key_missing")
            raise WeChatSourceDegraded("source_inventory_incomplete")
        return list(snapshot.present_generation_ids)

    def get_messages_for_shard(
        self,
        username,
        source_shard_id,
        since_ts=0,
        limit=500,
        page_forward=False,
        since_inclusive=False,
    ):
        """Read one user-visible shard page or raise a content-free degraded error."""
        return self._get_messages_for_shard(
            username,
            source_shard_id,
            since_ts=since_ts,
            limit=limit,
            page_forward=page_forward,
            since_inclusive=since_inclusive,
            include_filtered=False,
        )

    def get_cursor_messages_for_shard(
        self,
        username,
        source_shard_id,
        since_ts=0,
        limit=500,
        page_forward=False,
        since_inclusive=False,
    ):
        """Read a cursor-complete shard page, retaining filtered source envelopes.

        Resource scanners must advance over raw source rows, not only messages that
        survive presentation cleaning.  Filtered rows therefore remain as stable
        empty-text envelopes so a short decoded page cannot masquerade as EOF.
        """
        return self._get_messages_for_shard(
            username,
            source_shard_id,
            since_ts=since_ts,
            limit=limit,
            page_forward=page_forward,
            since_inclusive=since_inclusive,
            include_filtered=True,
        )

    def get_cursor_page_for_shard(
        self,
        username,
        source_shard_id,
        *,
        cursor_token="",
        since_ts=0,
        limit=500,
    ):
        """Read one bounded cursor-complete keyset page from a frozen shard.

        The opaque token binds the exact ``(create_time, rowid)`` position, so a
        large same-second bucket never expands a request or an in-memory
        ``seen_ids`` set beyond the configured page size.
        """
        page_limit = max(1, int(limit))
        if cursor_token:
            try:
                decoded = json.loads(str(cursor_token))
                after_cursor = (int(decoded[0]), int(decoded[1]))
            except (TypeError, ValueError, IndexError, json.JSONDecodeError) as exc:
                raise WeChatSourceDegraded("source_cursor_invalid") from exc
        else:
            after_cursor = (max(0, int(since_ts)), 0)

        spec = next(
            (
                item
                for item in self._message_shard_specs()
                if item["source_shard_id"] == str(source_shard_id or "")
            ),
            None,
        )
        if spec is None:
            raise WeChatSourceDegraded("source_shard_unknown")
        if spec.get("cache_only"):
            raise WeChatSourceDegraded("source_cache_only")
        path = spec["cache_path"] or self._get_decrypted_db(spec["rel_key"])
        if not path:
            raise WeChatSourceDegraded("source_shard_unavailable")
        table_name = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
        conn = None
        try:
            conn = sqlite3.connect(path)
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
        except Exception as exc:
            raise WeChatSourceDegraded("source_shard_unavailable") from exc
        finally:
            if conn is not None:
                conn.close()
        if not exists:
            return {"messages": [], "next_cursor": cursor_token, "exhausted": True}

        messages = self._get_messages_from_paths(
            username,
            [path],
            table_name,
            limit=page_limit + 1,
            page_forward=True,
            include_filtered=True,
            db_shard_id=self._source_message_shard_identity(
                spec["rel_key"],
                cache_path=spec["cache_path"],
            ),
            after_cursor=after_cursor,
        )
        for message in messages:
            envelope = message.get("source_envelope")
            if isinstance(envelope, dict):
                envelope["source_generation_id"] = str(spec["source_shard_id"])
        exhausted = len(messages) <= page_limit
        page = messages[:page_limit]
        next_cursor = str(cursor_token or "")
        if page:
            envelope = page[-1].get("source_envelope") or {}
            next_cursor = json.dumps([
                int(envelope.get("create_time") or 0),
                int(envelope.get("rowid") or 0),
            ], separators=(",", ":"))
        return {
            "messages": page,
            "next_cursor": next_cursor,
            "exhausted": exhausted,
        }

    def _get_messages_for_shard(
        self,
        username,
        source_shard_id,
        *,
        since_ts=0,
        limit=500,
        page_forward=False,
        since_inclusive=False,
        include_filtered=False,
    ):
        spec = next(
            (
                item
                for item in self._message_shard_specs()
                if item["source_shard_id"] == str(source_shard_id or "")
            ),
            None,
        )
        if spec is None:
            raise WeChatSourceDegraded("source_shard_unknown")
        if spec.get("cache_only"):
            raise WeChatSourceDegraded("source_cache_only")
        path = spec["cache_path"] or self._get_decrypted_db(spec["rel_key"])
        if not path:
            raise WeChatSourceDegraded("source_shard_unavailable")
        table_name = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
        conn = None
        try:
            conn = sqlite3.connect(path)
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
        except Exception as exc:
            raise WeChatSourceDegraded("source_shard_unavailable") from exc
        finally:
            if conn is not None:
                conn.close()
        if not exists:
            return []
        return self._get_messages_from_paths(
            username,
            [path],
            table_name,
            since_ts=since_ts,
            limit=limit,
            page_forward=page_forward,
            since_inclusive=since_inclusive,
            include_filtered=include_filtered,
            db_shard_id=self._source_message_shard_identity(
                spec["rel_key"],
                cache_path=spec["cache_path"],
            ),
        )

    def get_messages(
        self,
        username,
        since_ts=0,
        limit=500,
        page_forward=False,
        since_inclusive=False,
    ):
        """Get messages only when every known source shard page is complete."""
        # Small fixture/legacy adapters historically supply only _find_msg_table.
        if not hasattr(self, "keys") or not hasattr(self, "db_dir"):
            db_paths, table_name = self._find_msg_table(username)
            return self._get_messages_from_paths(
                username,
                db_paths,
                table_name,
                since_ts=since_ts,
                limit=limit,
                page_forward=page_forward,
                since_inclusive=since_inclusive,
            )

        all_messages = []
        for source_shard_id in self.get_message_shards(username):
            all_messages.extend(self.get_messages_for_shard(
                username,
                source_shard_id,
                since_ts=since_ts,
                limit=limit,
                page_forward=page_forward,
                since_inclusive=since_inclusive,
            ))
        all_messages.sort(
            key=lambda message: (
                int(message.get("timestamp") or 0),
                str(message.get("source_message_id") or ""),
            )
        )
        if since_ts > 0 and page_forward:
            return all_messages[:limit]
        return all_messages[-limit:]

    def _get_fts_db(self):
        """Get decrypted FTS full-text search database path."""
        return self._get_decrypted_db("message/message_fts.db")

    def _build_fts_lookup(self, fts_conn):
        """Build FTS helper mapping tables.

        Returns:
            (name2id, id2name, id2sender_name)
            - name2id: {username: session_id}
            - id2name: {session_id: username}
            - id2sender_name: {sender_id: display_name}
        """
        self._load_contacts()
        name2id = {}
        id2name = {}
        id2sender_name = {}

        for row in fts_conn.execute("SELECT rowid, username FROM name2id"):
            rid, uname = row
            name2id[uname] = rid
            id2name[rid] = uname
            # Parse sender display name
            display = self._contacts.get(uname, uname)
            id2sender_name[rid] = display

        return name2id, id2name, id2sender_name

    def search_messages(self, keywords, usernames, start_ts, end_ts, limit_per_group=2000):
        """Cross-chat message search (AND boolean search + sliding window).

        Prefers FTS full-text search database (covers all historical messages).
        Falls back to direct database scan if FTS is unavailable.
        Only reads decrypted cache copies, never touches original WeChat databases.

        Args:
            keywords: list[str] - Keyword list (already split by spaces).
            usernames: list[str] - List of group chat usernames to search.
            start_ts: int/float - Start timestamp.
            end_ts: int/float - End timestamp.
            limit_per_group: int - Max messages to retrieve per group.

        Returns:
            dict: {username: [{"sender", "text", "timestamp", "time_str", "group_name"}]}
        """
        # Try FTS search (covers all historical data)
        fts_path = self._get_fts_db()
        if fts_path:
            print("[search] 使用 FTS 全文索引搜索（覆盖全部历史数据）")
            return self._search_via_fts(fts_path, keywords, usernames, start_ts, end_ts, limit_per_group)

        # FTS unavailable, fall back to direct database search
        print("[search] FTS 不可用，回退到直接数据库搜索")
        return self._search_via_db(keywords, usernames, start_ts, end_ts, limit_per_group)

    def _search_via_fts(self, fts_path, keywords, usernames, start_ts, end_ts, limit_per_group=2000):
        """Search messages via FTS full-text search database.

        The FTS database contains full-text indexes of all messages, independent of
        message database decryption keys, covering all historical messages (back to 2022).
        """
        self._load_contacts()
        results = {}

        fts_conn = sqlite3.connect(fts_path, timeout=5)
        try:
            name2id, id2name, id2sender_name = self._build_fts_lookup(fts_conn)
        except Exception as e:
            print(f"[search] FTS 映射表构建失败: {e}", file=__import__('sys').stderr)
            fts_conn.close()
            return results

        try:
            return self._search_fts_inner(
                fts_conn, name2id, id2name, id2sender_name,
                keywords, usernames, start_ts, end_ts, limit_per_group,
            )
        finally:
            fts_conn.close()

    def _search_fts_inner(self, fts_conn, name2id, id2name, id2sender_name,
                          keywords, usernames, start_ts, end_ts, limit_per_group):
        results = {}

        # Preprocess keywords
        keywords_lower = [kw.lower() for kw in keywords]

        # Build set of session_ids to search
        target_sessions = {}  # session_id → username
        for uname in usernames:
            sid = name2id.get(uname)
            if sid is not None:
                target_sessions[sid] = uname
            else:
                group_name = self._contacts.get(uname, uname)
                print(f"[search]   ⚠ {group_name}: 不在 FTS 索引中")

        print(f"[search] 正在搜索 {len(target_sessions)} 个群聊...")

        # FTS has 4 content shard tables (message_fts_v4_0_content ~ _3_content)
        # Schema: id, c0(acontent), c1(message_local_id), c2(sort_seq),
        #         c3(local_type), c4(session_id), c5(sender_id), c6(create_time)
        FTS_TABLES = [f"message_fts_v4_{i}_content" for i in range(4)]

        for idx, (sid, uname) in enumerate(target_sessions.items()):
            group_name = self._contacts.get(uname, uname)
            print(f"[search]   ({idx+1}/{len(target_sessions)}) 搜索 {group_name}...")

            # Collect messages from all FTS shards
            all_msgs = []
            total_rows = 0
            for tbl in FTS_TABLES:
                try:
                    rows = fts_conn.execute(f"""
                        SELECT c0, c5, c6 FROM [{tbl}]
                        WHERE c4 = ? AND c6 >= ? AND c6 <= ?
                        ORDER BY c6 ASC
                        LIMIT ?
                    """, (sid, int(start_ts), int(end_ts), limit_per_group)).fetchall()
                except Exception:
                    continue

                total_rows += len(rows)
                for text, sender_id, create_time in rows:
                    if not text or not create_time:
                        continue
                    # FTS content is already cleaned text, no XML processing needed
                    sender = id2sender_name.get(sender_id, str(sender_id))
                    all_msgs.append({
                        "sender": sender,
                        "text": text,
                        "text_lower": text.lower(),
                        "timestamp": create_time,
                        "time_str": datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M"),
                        "group_name": group_name,
                    })

            # Sort by time (messages from different shards need merge sort)
            all_msgs.sort(key=lambda m: m["timestamp"])

            # Limit max messages per group
            if len(all_msgs) > limit_per_group:
                all_msgs = all_msgs[:limit_per_group]

            if not all_msgs:
                if total_rows > 0:
                    print(f"[search]   · {group_name}: 该时间段无文本消息")
                else:
                    print(f"[search]   · {group_name}: 该时间段无消息")
                continue

            print(f"[search]     {total_rows} 条记录 → {len(all_msgs)} 条文本")

            # Sliding window search (1 msg above + below = 3 msg window)
            CONTEXT = 1
            matched_indices = set()
            for i in range(len(all_msgs)):
                win_start = max(0, i - CONTEXT)
                win_end = min(len(all_msgs), i + CONTEXT + 1)
                combined = " ".join(all_msgs[j]["text_lower"] for j in range(win_start, win_end))
                if all(kw in combined for kw in keywords_lower):
                    for j in range(win_start, win_end):
                        matched_indices.add(j)

            matched = []
            for mi in sorted(matched_indices):
                msg = all_msgs[mi]
                matched.append({
                    "sender": msg["sender"],
                    "text": msg["text"],
                    "timestamp": msg["timestamp"],
                    "time_str": msg["time_str"],
                    "group_name": msg["group_name"],
                })

            if matched:
                results[uname] = matched
                print(f"[search]   ✓ {group_name}: {len(matched)} 条命中（共扫描 {total_rows} 条）")
            else:
                print(f"[search]   · {group_name}: 扫描 {len(all_msgs)} 条，无命中")

        total_hits = sum(len(msgs) for msgs in results.values())
        print(f"[search] ✅ 搜索完成，共命中 {total_hits} 条消息，涉及 {len(results)} 个群")
        return results

    def _search_via_db(self, keywords, usernames, start_ts, end_ts, limit_per_group=2000):
        """Direct database scan search (fallback when FTS unavailable)."""
        self._load_contacts()
        results = {}

        # Pre-build username → (db_path, table_name) mapping
        table_cache = {}
        total = len(usernames)
        print(f"[search] 正在定位 {total} 个群聊的消息表...")
        for i, username in enumerate(usernames):
            db_paths, table_name = self._find_msg_table(username)
            if db_paths:
                table_cache[username] = (db_paths, table_name)
        print(f"[search] 定位完成，{len(table_cache)}/{total} 个群有消息表")

        keywords_lower = [kw.lower() for kw in keywords]

        for i, username in enumerate(table_cache):
            is_group = "@chatroom" in username
            group_name = self._contacts.get(username, username)
            db_paths, table_name = table_cache[username]

            print(f"[search]   ({i+1}/{len(table_cache)}) 搜索 {group_name}...")

            rows = []
            for db_path in db_paths:
                conn = sqlite3.connect(db_path)
                try:
                    r = conn.execute(f"""
                        SELECT local_type, create_time, message_content,
                               WCDB_CT_message_content
                        FROM [{table_name}]
                        WHERE create_time >= ? AND create_time <= ?
                        ORDER BY create_time ASC
                    """, (int(start_ts), int(end_ts))).fetchall()
                    rows.extend(r)
                except Exception as e:
                    print(f"[search]   ⚠ {group_name} 查询失败: {e}")
                finally:
                    conn.close()

            # Sort by time after multi-DB merge (context window depends on order)
            rows.sort(key=lambda r: r[1])

            if not rows:
                continue

            all_msgs = []
            for local_type, create_time, content, ct in rows:
                if ct and ct == 4 and isinstance(content, bytes):
                    try:
                        content = _zstd_dctx.decompress(content).decode("utf-8", errors="replace")
                    except Exception:
                        continue
                elif isinstance(content, bytes):
                    try:
                        content = content.decode("utf-8", errors="replace")
                    except Exception:
                        continue

                if content is None:
                    continue

                sender = ""
                text = content
                if is_group and ":\n" in content:
                    sender, text = content.split(":\n", 1)
                    sender = self._contacts.get(sender, sender)

                text = _clean_msg_text(text)
                if text is None:
                    continue

                all_msgs.append({
                    "sender": sender,
                    "text": text,
                    "text_lower": text.lower(),
                    "timestamp": create_time,
                    "time_str": datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M"),
                    "group_name": group_name,
                })

            print(f"[search]     {len(rows)} 条记录 → {len(all_msgs)} 条文本")

            CONTEXT = 1
            matched_indices = set()
            for i in range(len(all_msgs)):
                win_start = max(0, i - CONTEXT)
                win_end = min(len(all_msgs), i + CONTEXT + 1)
                combined = " ".join(all_msgs[j]["text_lower"] for j in range(win_start, win_end))
                if all(kw in combined for kw in keywords_lower):
                    for j in range(win_start, win_end):
                        matched_indices.add(j)

            matched = []
            for idx in sorted(matched_indices):
                msg = all_msgs[idx]
                matched.append({
                    "sender": msg["sender"],
                    "text": msg["text"],
                    "timestamp": msg["timestamp"],
                    "time_str": msg["time_str"],
                    "group_name": msg["group_name"],
                })

            if matched:
                results[username] = matched
                print(f"[search]   ✓ {group_name}: {len(matched)} 条命中（共扫描 {len(rows)} 条）")
            elif rows:
                print(f"[search]   · {group_name}: 扫描 {len(rows)} 条，无命中")
            else:
                print(f"[search]   · {group_name}: 该时间段无文本消息")

        total_hits = sum(len(msgs) for msgs in results.values())
        print(f"[search] ✅ 搜索完成，共命中 {total_hits} 条消息，涉及 {len(results)} 个群")
        return results

    def get_fts_coverage(self, usernames):
        """Get data coverage range per chat in FTS.

        Args:
            usernames: list[str] - List of chat usernames.

        Returns:
            dict: {username: {"earliest": timestamp, "latest": timestamp, "count": int}}
                  If FTS is unavailable or chat has no data, the username is absent from results.
        """
        fts_path = self._get_fts_db()
        if not fts_path:
            return {}

        fts_conn = sqlite3.connect(fts_path, timeout=5)
        try:
            name2id = dict(fts_conn.execute("SELECT username, rowid FROM name2id").fetchall())
        except Exception:
            fts_conn.close()
            return {}

        try:
            coverage = {}
            FTS_TABLES = [f"message_fts_v4_{i}_content" for i in range(4)]

            for uname in usernames:
                sid = name2id.get(uname)
                if sid is None:
                    continue
                earliest = None
                latest = None
                total = 0
                for tbl in FTS_TABLES:
                    try:
                        row = fts_conn.execute(
                            f"SELECT MIN(c6), MAX(c6), COUNT(*) FROM [{tbl}] WHERE c4 = ?",
                            (sid,),
                        ).fetchone()
                        if row[2] > 0:
                            total += row[2]
                            if row[0] and (earliest is None or row[0] < earliest):
                                earliest = row[0]
                            if row[1] and (latest is None or row[1] > latest):
                                latest = row[1]
                    except Exception:
                        continue
                if total > 0:
                    coverage[uname] = {"earliest": earliest, "latest": latest, "count": total}

            return coverage
        finally:
            fts_conn.close()

    def resolve_username(self, name):
        """Resolve group name / alias / nickname to username."""
        self._load_contacts()
        if name in self._contacts or "@chatroom" in name:
            return name
        name_lower = name.lower()
        # Exact match
        for uname, display in self._contacts.items():
            if name_lower == display.lower():
                return uname
        # Fuzzy match
        for uname, display in self._contacts.items():
            if name_lower in display.lower():
                return uname
        return None

    # ── Image message utilities ─────────────────────────────────────

    @staticmethod
    def _extract_file_hash_from_protobuf(data):
        """Extract 32-char hex file hash from packed_info_data protobuf.

        Manually parses protobuf wire format (including nested sub-messages),
        looking for 32-byte ASCII hex strings or 16-byte raw hashes.

        Returns:
            str | None: 32-char hex hash, or None.
        """
        if not data or len(data) < 10:
            return None

        def _scan(buf, depth=0):
            """Recursively scan protobuf fields."""
            if depth > 3:
                return []
            pos = 0
            found = []
            while pos < len(buf):
                try:
                    tag_byte = buf[pos]
                    wire_type = tag_byte & 0x07
                    pos += 1
                    if tag_byte & 0x80:
                        while pos < len(buf) and buf[pos] & 0x80:
                            pos += 1
                        pos += 1

                    if wire_type == 0:  # varint
                        while pos < len(buf) and buf[pos] & 0x80:
                            pos += 1
                        pos += 1
                    elif wire_type == 1:
                        pos += 8
                    elif wire_type == 5:
                        pos += 4
                    elif wire_type == 2:  # length-delimited
                        length = 0
                        shift = 0
                        while pos < len(buf):
                            b = buf[pos]
                            length |= (b & 0x7F) << shift
                            pos += 1
                            if not (b & 0x80):
                                break
                            shift += 7
                        if pos + length > len(buf):
                            break
                        field_data = buf[pos:pos + length]
                        pos += length

                        if length == 32:
                            try:
                                ascii_str = field_data.decode("ascii")
                                bytes.fromhex(ascii_str)
                                found.append(ascii_str)
                                continue
                            except (UnicodeDecodeError, ValueError):
                                pass
                        if length == 16 and not all(b == 0 for b in field_data):
                            found.append(field_data.hex())
                            continue
                        # Recursively parse sub-messages
                        if length > 10:
                            found.extend(_scan(field_data, depth + 1))
                    else:
                        break
                except (IndexError, ValueError):
                    break
            return found

        candidates = _scan(data)
        return candidates[0] if candidates else None

    def _get_attach_base_dir(self):
        """Derive msg/attach image attachment directory from db_dir.

        db_dir = .../xwechat_files/{wxid}/db_storage
        attach = .../xwechat_files/{wxid}/msg/attach
        """
        wxid_dir = os.path.dirname(self.db_dir)
        attach_dir = os.path.join(wxid_dir, "msg", "attach")
        return attach_dir if os.path.isdir(attach_dir) else None

    def _find_image_file(self, username, create_time, file_hash=None, prefer_thumbnail=True):
        """Locate image file on disk.

        Primary strategy: look up by file_hash in attach/{md5(username)}/{YYYY-MM}/Img/{hash}_M.dat
        Fallback: match by file modification time (closest file within +/-60 seconds).

        Args:
            username: Chat username.
            create_time: Message timestamp.
            file_hash: 32-char hex hash extracted from packed_info_data.
            prefer_thumbnail: Whether to prefer returning thumbnails.

        Returns:
            str | None: Absolute path to image file.
        """
        attach_base = self._get_attach_base_dir()
        if not attach_base:
            return None

        user_hash = hashlib.md5(username.encode()).hexdigest()
        user_dir = os.path.join(attach_base, user_hash)
        if not os.path.isdir(user_dir):
            return None

        dt = datetime.fromtimestamp(create_time)
        month_str = dt.strftime("%Y-%m")
        img_dir = os.path.join(user_dir, month_str, "Img")

        # ── Primary strategy: locate by hash ──
        if file_hash:
            months_to_try = [month_str]
            # Month boundary: also check adjacent months
            if dt.day <= 2:
                prev = dt.replace(day=1)
                prev = prev.replace(
                    year=prev.year - 1, month=12
                ) if prev.month == 1 else prev.replace(month=prev.month - 1)
                months_to_try.append(prev.strftime("%Y-%m"))
            elif dt.day >= 28:
                nxt = dt.replace(day=1)
                nxt = nxt.replace(
                    year=nxt.year + 1, month=1
                ) if nxt.month == 12 else nxt.replace(month=nxt.month + 1)
                months_to_try.append(nxt.strftime("%Y-%m"))

            # New format (2026+): _t.dat=thumbnail, .dat=medium, _h.dat=original
            # Old format (~2025): _t_M.dat=thumbnail, _M.dat=original
            thumb_suffixes = ("_t_M.dat", "_t.dat")
            full_suffixes = ("_h.dat", "_M.dat", ".dat")

            for m in months_to_try:
                d = os.path.join(user_dir, m, "Img")
                if not os.path.isdir(d):
                    continue
                if prefer_thumbnail:
                    for sfx in thumb_suffixes:
                        p = os.path.join(d, file_hash + sfx)
                        if os.path.isfile(p):
                            return p
                for sfx in full_suffixes:
                    p = os.path.join(d, file_hash + sfx)
                    if os.path.isfile(p):
                        return p
                if not prefer_thumbnail:
                    for sfx in thumb_suffixes:
                        p = os.path.join(d, file_hash + sfx)
                        if os.path.isfile(p):
                            return p

        # ── Fallback: match by modification time ──
        if not os.path.isdir(img_dir):
            return None

        if prefer_thumbnail:
            target_suffixes = ("_t_M.dat", "_t.dat")
        else:
            target_suffixes = ("_h.dat", "_M.dat", ".dat")
        best_path = None
        best_delta = float("inf")

        for fname in os.listdir(img_dir):
            if not any(fname.endswith(sfx) for sfx in target_suffixes):
                continue
            # Exclude files with underscore prefixes that don't match the target suffix
            fpath = os.path.join(img_dir, fname)
            try:
                delta = abs(os.path.getmtime(fpath) - create_time)
                if delta < best_delta:
                    best_delta = delta
                    best_path = fpath
            except OSError:
                continue

        return best_path if best_path and best_delta < 60 else None

    def get_image_messages(self, username, since_ts=0, limit=10):
        """Get image messages with file paths from a chat.

        Similar to get_messages(), but only queries local_type=3 (images),
        and additionally extracts packed_info_data to locate image files on disk.

        Returns:
            list[dict]: Each contains sender, time_str, timestamp,
                        thumb_path, image_path, file_hash.
        """
        self._load_contacts()
        is_group = "@chatroom" in username

        db_paths, table_name = self._find_msg_table(username)
        if not db_paths:
            return []

        rows = []
        for db_path in db_paths:
            conn = sqlite3.connect(db_path)
            try:
                if since_ts > 0:
                    r = conn.execute(f"""
                        SELECT local_type, create_time, message_content,
                               WCDB_CT_message_content, status, packed_info_data
                        FROM [{table_name}]
                        WHERE local_type = 3 AND create_time > ?
                        ORDER BY create_time DESC
                        LIMIT ?
                    """, (since_ts, limit * 2)).fetchall()
                else:
                    r = conn.execute(f"""
                        SELECT local_type, create_time, message_content,
                               WCDB_CT_message_content, status, packed_info_data
                        FROM [{table_name}]
                        WHERE local_type = 3
                        ORDER BY create_time DESC
                        LIMIT ?
                    """, (limit * 2,)).fetchall()
                rows.extend(r)
            except Exception:
                pass
            finally:
                conn.close()

        if not rows:
            return []

        # Sort + truncate after multi-DB merge: return the latest page in
        # chronological order, regardless of whether a time window is provided.
        rows.sort(key=lambda r: r[1])
        rows = rows[-limit:]

        contact_name = ""
        if not is_group:
            contact_name = self._contacts.get(username, username)

        messages = []
        for local_type, create_time, content, ct, status, packed_info in rows:
            # Decompress content
            if ct and ct == 4 and isinstance(content, bytes):
                try:
                    content = _zstd_dctx.decompress(content).decode("utf-8", errors="replace")
                except Exception:
                    content = ""
            elif isinstance(content, bytes):
                try:
                    content = content.decode("utf-8", errors="replace")
                except Exception:
                    content = ""

            # Extract sender
            sender = ""
            text = content or ""
            if is_group and ":\n" in text:
                raw_id, text = text.split(":\n", 1)
                sender = self._contacts.get(raw_id)
                if not sender:
                    sender = self._nick_to_remark.get(raw_id, raw_id)
            elif not is_group:
                sender = "我" if status == 2 else contact_name
            if is_group and not sender:
                sender = "您"

            # Extract file hash
            file_hash = None
            if packed_info and isinstance(packed_info, bytes):
                file_hash = self._extract_file_hash_from_protobuf(packed_info)

            # Locate file
            thumb_path = self._find_image_file(
                username, create_time, file_hash, prefer_thumbnail=True
            )
            image_path = self._find_image_file(
                username, create_time, file_hash, prefer_thumbnail=False
            )

            messages.append({
                "sender": sender,
                "timestamp": create_time,
                "time_str": datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M"),
                "file_hash": file_hash,
                "thumb_path": thumb_path,
                "image_path": image_path,
            })

        return messages

    def get_emoji_messages(self, username, since_ts=0, limit=10):
        """Get sticker/animated emoji messages from a chat.

        Queries local_type=47 messages, parses XML to extract cdnurl, md5, etc.

        Returns:
            list[dict]: Each contains sender, time_str, timestamp,
                        md5, cdnurl, aeskey, width, height.
        """
        import re

        self._load_contacts()
        is_group = "@chatroom" in username

        db_paths, table_name = self._find_msg_table(username)
        if not db_paths:
            return []

        rows = []
        for db_path in db_paths:
            conn = sqlite3.connect(db_path)
            try:
                if since_ts > 0:
                    r = conn.execute(f"""
                        SELECT local_type, create_time, message_content,
                               WCDB_CT_message_content, status
                        FROM [{table_name}]
                        WHERE local_type = 47 AND create_time > ?
                        ORDER BY create_time DESC
                        LIMIT ?
                    """, (since_ts, limit * 2)).fetchall()
                else:
                    r = conn.execute(f"""
                        SELECT local_type, create_time, message_content,
                               WCDB_CT_message_content, status
                        FROM [{table_name}]
                        WHERE local_type = 47
                        ORDER BY create_time DESC
                        LIMIT ?
                    """, (limit * 2,)).fetchall()
                rows.extend(r)
            except Exception:
                pass
            finally:
                conn.close()

        if not rows:
            return []

        # Sort + truncate after multi-DB merge: return the latest page in
        # chronological order, regardless of whether a time window is provided.
        rows.sort(key=lambda r: r[1])
        rows = rows[-limit:]

        contact_name = ""
        if not is_group:
            contact_name = self._contacts.get(username, username)

        messages = []
        for local_type, create_time, content, ct, status in rows:
            # Decompress content
            if ct and ct == 4 and isinstance(content, bytes):
                try:
                    content = _zstd_dctx.decompress(content).decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    content = ""
            elif isinstance(content, bytes):
                try:
                    content = content.decode("utf-8", errors="replace")
                except Exception:
                    content = ""

            if not content:
                continue

            # Parse emoji XML
            md5_match = re.search(r'md5\s*=\s*"([a-fA-F0-9]{32})"', content)
            cdn_match = re.search(r'cdnurl\s*=\s*"([^"]+)"', content)
            aes_match = re.search(r'aeskey\s*=\s*"([^"]*)"', content)
            w_match = re.search(r'width\s*=\s*"(\d+)"', content)
            h_match = re.search(r'height\s*=\s*"(\d+)"', content)
            from_match = re.search(
                r'fromusername\s*=\s*"([^"]+)"', content
            )

            if not md5_match:
                continue

            emoji_md5 = md5_match.group(1)
            cdnurl = cdn_match.group(1).replace("&amp;", "&") if cdn_match else ""
            aeskey = aes_match.group(1) if aes_match else ""
            width = int(w_match.group(1)) if w_match else 0
            height = int(h_match.group(1)) if h_match else 0

            # Extract sender
            sender = ""
            if is_group:
                # In group chat emoji XML, fromusername is the sender
                if from_match:
                    raw_id = from_match.group(1)
                    sender = self._contacts.get(raw_id)
                    if not sender:
                        sender = self._nick_to_remark.get(raw_id, raw_id)
            else:
                sender = "我" if status == 2 else contact_name

            messages.append({
                "sender": sender,
                "timestamp": create_time,
                "time_str": datetime.fromtimestamp(create_time).strftime(
                    "%m-%d %H:%M"
                ),
                "md5": emoji_md5,
                "cdnurl": cdnurl,
                "aeskey": aeskey,
                "width": width,
                "height": height,
                "msg_type": "emoji",
            })

        # Backfill CDN URLs from emoticon.db for stickers missing cdnurl
        self._load_emoticon_db()
        for msg in messages:
            if not msg.get("cdnurl") and msg["md5"] in self._emoticon_map:
                info = self._emoticon_map[msg["md5"]]
                msg["cdnurl"] = info["cdn_url"]
                if not msg.get("aeskey"):
                    msg["aeskey"] = info["aes_key"]

        return messages

    @staticmethod
    def _is_unresolved_id(sender, raw_sender_id):
        """Check if sender is an unresolved ID (not a human-readable name)."""
        if not sender or not raw_sender_id:
            return False
        if sender != raw_sender_id:
            return False
        if raw_sender_id.startswith("wxid_"):
            return True
        if re.fullmatch(r'[A-Za-z0-9_\-]+', raw_sender_id) and len(raw_sender_id) >= 8:
            return True
        return False

    def format_messages_for_ai(self, messages, show_group_nickname=False):
        """Format message list into text suitable for AI summary.

        Args:
            messages: List of messages.
            show_group_nickname: Whether to show group nickname (raw sender ID + known name).
        """
        anon_map = {}
        anon_counter = [0]

        def get_display_name(msg):
            sender = msg.get("sender", "")
            raw_id = msg.get("raw_sender_id", "")
            if sender and self._is_unresolved_id(sender, raw_id):
                if raw_id not in anon_map:
                    anon_counter[0] += 1
                    anon_map[raw_id] = f"成员{anon_counter[0]}"
                return anon_map[raw_id]
            return sender

        lines = []
        for msg in messages:
            if msg["type"] in (10000, 10002):
                continue
            sender = get_display_name(msg)
            if sender:
                lines.append(f"[{msg['time_str']}] {sender}: {msg['text']}")
            else:
                lines.append(f"[{msg['time_str']}] {msg['text']}")
        return "\n".join(lines)
