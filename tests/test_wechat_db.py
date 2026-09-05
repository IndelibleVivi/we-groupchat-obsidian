import hashlib
import json
import os
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from Crypto.Cipher import AES

from core.decryptor import (
    PAGE_SZ,
    WAL_MAGIC_LITTLE_ENDIAN_CHECKSUM,
    _wal_checksum,
    decrypt_wal,
)
from core.image_decoder import decode_wechat_image_data, detect_mime
from core.wechat_db import WeChatDB, WeChatSourceDegraded


class WeChatDBCacheRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cache_dir = WeChatDB.CACHE_DIR
        WeChatDB.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        self.username = "fallback@chatroom"
        self.table_name = f"Msg_{hashlib.md5(self.username.encode()).hexdigest()}"
        rel_path = "message/message_0.db"
        self.db = WeChatDB(self.tmp.name, keys={})
        self.cache_path = self.db._cache_path(rel_path)
        conn = sqlite3.connect(self.cache_path)
        try:
            conn.execute(
                f"""
                CREATE TABLE [{self.table_name}] (
                    local_type INTEGER,
                    create_time INTEGER,
                    message_content TEXT,
                    WCDB_CT_message_content INTEGER,
                    status INTEGER
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        WeChatDB.CACHE_DIR = self.old_cache_dir
        self.tmp.cleanup()

    def test_refresh_cache_view_preserves_fallback_decrypted_db_files(self):
        before_paths, before_table = self.db._find_msg_table(self.username)

        self.db.refresh_cache_view()
        after_paths, after_table = self.db._find_msg_table(self.username)

        self.assertEqual(before_paths, [self.cache_path])
        self.assertEqual(before_table, self.table_name)
        self.assertEqual(after_paths, [self.cache_path])
        self.assertEqual(after_table, self.table_name)
        self.assertTrue(os.path.exists(self.cache_path))

    def test_cache_only_message_source_is_explicitly_degraded(self):
        with self.assertRaisesRegex(WeChatSourceDegraded, "source_cache_only"):
            self.db.get_message_shards(self.username)

    def test_live_shard_identity_changes_when_same_path_is_replaced(self):
        rel_path = "message/message_4.db"
        source_path = os.path.join(self.tmp.name, rel_path)
        os.makedirs(os.path.dirname(source_path), exist_ok=True)
        conn = sqlite3.connect(source_path)
        conn.execute("CREATE TABLE first_generation(value TEXT)")
        conn.commit()
        conn.close()
        db = WeChatDB(self.tmp.name, keys={})
        first = db.get_source_inventory()["present_generation_ids"]

        replacement = os.path.join(self.tmp.name, "replacement.db")
        conn = sqlite3.connect(replacement)
        conn.execute("CREATE TABLE second_generation(value TEXT)")
        conn.commit()
        conn.close()
        os.replace(replacement, source_path)
        second = db.get_source_inventory()["present_generation_ids"]

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first, second)

    def test_live_shard_identity_changes_after_key_rotation(self):
        rel_path = "message/message_5.db"
        source_path = os.path.join(self.tmp.name, rel_path)
        os.makedirs(os.path.dirname(source_path), exist_ok=True)
        with open(source_path, "wb") as handle:
            handle.write(b"encrypted generation marker")
        db = WeChatDB(self.tmp.name, keys={
            rel_path: {"enc_key": "11" * 32},
        })
        first = db.get_source_inventory()["present_generation_ids"]

        db.keys[rel_path] = {"enc_key": "22" * 32}
        second = db.get_source_inventory()["present_generation_ids"]

        self.assertNotEqual(first, second)

    def test_encrypted_reconstruction_retries_when_source_identity_changes(self):
        rel_path = "message/message_6.db"
        source_path = os.path.join(self.tmp.name, rel_path)
        os.makedirs(os.path.dirname(source_path), exist_ok=True)
        with open(source_path, "wb") as handle:
            handle.write(b"encrypted fixture")
        db = WeChatDB(self.tmp.name, keys={
            rel_path: {"enc_key": "33" * 32},
        })
        calls = []

        def fake_decrypt(_source, output, _key):
            calls.append(output)
            conn = sqlite3.connect(output)
            conn.execute("CREATE TABLE snapshot_value(value TEXT)")
            conn.execute("INSERT INTO snapshot_value VALUES ('stable')")
            conn.commit()
            conn.close()
            if len(calls) == 1:
                with open(source_path, "ab") as handle:
                    handle.write(b"changed")
            return 1

        with (
            patch.object(db, "_is_plain_sqlite", return_value=False),
            patch("core.wechat_db.decrypt_database", side_effect=fake_decrypt),
        ):
            path = db._get_decrypted_db(rel_path)

        self.assertEqual(len(calls), 2)
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(
                conn.execute("SELECT value FROM snapshot_value").fetchone()[0],
                "stable",
            )
        finally:
            conn.close()


    def test_decrypted_cache_publish_is_atomic_and_snapshot_is_pinned(self):
        rel_path = "message/message_1.db"
        encrypted_path = os.path.join(self.tmp.name, rel_path)
        os.makedirs(os.path.dirname(encrypted_path), exist_ok=True)
        with open(encrypted_path, "wb") as handle:
            handle.write(b"encrypted fixture")
        db = WeChatDB(self.tmp.name, keys={
            rel_path: {"enc_key": "00" * 32},
        })

        def fake_decrypt(_source, output, _key):
            conn = sqlite3.connect(output)
            try:
                conn.execute("CREATE TABLE snapshot_value(value TEXT)")
                conn.execute("INSERT INTO snapshot_value VALUES ('original')")
                conn.commit()
            finally:
                conn.close()
            return 1

        with (
            patch.object(db, "_is_plain_sqlite", return_value=False),
            patch("core.wechat_db.decrypt_database", side_effect=fake_decrypt),
        ):
            with db.source_snapshot():
                pinned = db._get_decrypted_db(rel_path)
                published = db._cache_path(rel_path)
                self.assertNotEqual(pinned, published)
                self.assertTrue(os.path.isfile(published))
                replacement = os.path.join(self.tmp.name, "replacement.db")
                conn = sqlite3.connect(replacement)
                try:
                    conn.execute("CREATE TABLE snapshot_value(value TEXT)")
                    conn.execute("INSERT INTO snapshot_value VALUES ('replacement')")
                    conn.commit()
                finally:
                    conn.close()
                os.replace(replacement, published)

                self.assertEqual(db._get_decrypted_db(rel_path), pinned)
                conn = sqlite3.connect(pinned)
                try:
                    value = conn.execute("SELECT value FROM snapshot_value").fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(value, "original")

            self.assertFalse(os.path.exists(pinned))
            self.assertFalse(any(
                name.startswith(".partial-")
                for name in os.listdir(db.cache_dir)
            ))

    def test_cache_namespace_and_key_fingerprint_isolate_source_roots(self):
        rel_path = "message/message_2.db"
        first_root = os.path.join(self.tmp.name, "first")
        second_root = os.path.join(self.tmp.name, "second")
        for root in (first_root, second_root):
            source_path = os.path.join(root, rel_path)
            os.makedirs(os.path.dirname(source_path), exist_ok=True)
            with open(source_path, "wb") as handle:
                handle.write(root.encode("utf-8"))
        first = WeChatDB(first_root, {rel_path: {"enc_key": "11" * 32}})
        second = WeChatDB(second_root, {rel_path: {"enc_key": "22" * 32}})

        def fake_decrypt(source, output, _key):
            conn = sqlite3.connect(output)
            try:
                conn.execute("CREATE TABLE source_value(value TEXT)")
                conn.execute("INSERT INTO source_value VALUES (?)", (source,))
                conn.commit()
            finally:
                conn.close()
            return 1

        with (
            patch.object(WeChatDB, "_is_plain_sqlite", return_value=False),
            patch("core.wechat_db.decrypt_database", side_effect=fake_decrypt),
        ):
            first_path = first._get_decrypted_db(rel_path)
            second_path = second._get_decrypted_db(rel_path)
            first_again = first._get_decrypted_db(rel_path)

        self.assertNotEqual(first.cache_namespace, second.cache_namespace)
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(first_path, first_again)
        conn = sqlite3.connect(first_again)
        try:
            value = conn.execute("SELECT value FROM source_value").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(value, os.path.join(first_root, rel_path))

    def test_plaintext_snapshot_uses_online_backup_and_includes_wal(self):
        rel_path = "message/message_3.db"
        source_path = os.path.join(self.tmp.name, rel_path)
        os.makedirs(os.path.dirname(source_path), exist_ok=True)
        source = sqlite3.connect(source_path)
        try:
            source.execute("PRAGMA journal_mode=WAL")
            source.execute("PRAGMA wal_autocheckpoint=0")
            source.execute("CREATE TABLE snapshot_value(value TEXT)")
            source.execute("INSERT INTO snapshot_value VALUES ('from-wal')")
            source.commit()
            self.assertTrue(os.path.exists(source_path + "-wal"))
            db = WeChatDB(self.tmp.name, keys={})

            with db.source_snapshot():
                pinned = db._get_decrypted_db(rel_path)
                self.assertFalse(os.path.samefile(source_path, pinned))
                source.execute("INSERT INTO snapshot_value VALUES ('later')")
                source.commit()
                conn = sqlite3.connect(pinned)
                try:
                    values = [
                        row[0]
                        for row in conn.execute(
                            "SELECT value FROM snapshot_value ORDER BY rowid"
                        )
                    ]
                finally:
                    conn.close()

            self.assertEqual(values, ["from-wal"])
            self.assertFalse(os.path.exists(pinned))
        finally:
            source.close()


class SQLiteWalReplayTests(unittest.TestCase):
    @staticmethod
    def _wal_bytes(frames):
        salt1 = 0x11223344
        salt2 = 0x55667788
        header_prefix = struct.pack(
            ">IIIIII",
            WAL_MAGIC_LITTLE_ENDIAN_CHECKSUM,
            3_007_000,
            PAGE_SZ,
            0,
            salt1,
            salt2,
        )
        checksum = _wal_checksum(header_prefix, byteorder="little")
        payload = bytearray(header_prefix + struct.pack(">II", *checksum))
        for pgno, db_pages, page in frames:
            frame_prefix = struct.pack(">IIII", pgno, db_pages, salt1, salt2)
            checksum = _wal_checksum(
                frame_prefix[:8] + page,
                checksum,
                byteorder="little",
            )
            payload.extend(frame_prefix + struct.pack(">II", *checksum) + page)
        return bytes(payload)

    def test_wal_replay_ignores_valid_uncommitted_tail_and_truncates_to_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            wal_path = os.path.join(tmp, "fixture.db-wal")
            out_path = os.path.join(tmp, "fixture.db")
            committed = b"A" * PAGE_SZ
            uncommitted = b"B" * PAGE_SZ
            with open(wal_path, "wb") as handle:
                handle.write(self._wal_bytes([
                    (1, 1, committed),
                    (1, 0, uncommitted),
                ]))
            with open(out_path, "wb") as handle:
                handle.write(b"0" * (PAGE_SZ * 2))

            with patch(
                "core.decryptor.decrypt_page",
                side_effect=lambda _key, page, _pgno: page,
            ):
                patched = decrypt_wal(wal_path, out_path, "00" * 32)

            self.assertEqual(patched, 1)
            self.assertEqual(os.path.getsize(out_path), PAGE_SZ)
            with open(out_path, "rb") as handle:
                self.assertEqual(handle.read(), committed)

    def test_wal_replay_stops_at_checksum_failure_after_last_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            wal_path = os.path.join(tmp, "fixture.db-wal")
            out_path = os.path.join(tmp, "fixture.db")
            committed = b"C" * PAGE_SZ
            tail = b"D" * PAGE_SZ
            payload = bytearray(self._wal_bytes([
                (1, 1, committed),
                (1, 0, tail),
            ]))
            payload[-1] ^= 0x01
            with open(wal_path, "wb") as handle:
                handle.write(payload)
            with open(out_path, "wb") as handle:
                handle.write(b"0" * PAGE_SZ)

            with patch(
                "core.decryptor.decrypt_page",
                side_effect=lambda _key, page, _pgno: page,
            ):
                patched = decrypt_wal(wal_path, out_path, "00" * 32)

            self.assertEqual(patched, 1)
            with open(out_path, "rb") as handle:
                self.assertEqual(handle.read(), committed)


class WeChatDBPagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "messages.db")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE Chat_test (
                    local_type INTEGER,
                    create_time INTEGER,
                    message_content TEXT,
                    WCDB_CT_message_content INTEGER,
                    status INTEGER
                )
            """)
            for ts in range(101, 111):
                conn.execute(
                    "INSERT INTO Chat_test VALUES (?, ?, ?, ?, ?)",
                    (1, ts, f"sender:\nmsg{ts}", None, 0),
                )
            conn.commit()
        finally:
            conn.close()

        self.db = object.__new__(WeChatDB)
        self.db._contacts = {"sender": "成员"}
        self.db._nick_to_remark = {}
        self.db._load_contacts = lambda: None
        self.db._find_msg_table = lambda username: ([self.db_path], "Chat_test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_messages_default_returns_newest_page_after_bookmark(self):
        messages = self.db.get_messages("room@chatroom", since_ts=100, limit=3)

        self.assertEqual([m["timestamp"] for m in messages], [108, 109, 110])

    def test_get_messages_page_forward_returns_next_page_after_bookmark(self):
        first = self.db.get_messages("room@chatroom", since_ts=100, limit=3, page_forward=True)
        second = self.db.get_messages(
            "room@chatroom",
            since_ts=first[-1]["timestamp"],
            limit=3,
            page_forward=True,
        )

        self.assertEqual([m["timestamp"] for m in first], [101, 102, 103])
        self.assertEqual([m["timestamp"] for m in second], [104, 105, 106])

    def test_get_messages_page_forward_from_zero_returns_oldest_page(self):
        messages = self.db.get_messages(
            "room@chatroom",
            since_ts=0,
            limit=3,
            page_forward=True,
            since_inclusive=True,
        )

        self.assertEqual([m["timestamp"] for m in messages], [101, 102, 103])

    def test_cursor_page_retains_filtered_envelopes_instead_of_false_eof(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE Chat_test SET message_content = ? WHERE create_time = 102",
                ("sender:\n<sysmsg type='fixture'></sysmsg>",),
            )
            conn.commit()
        finally:
            conn.close()

        visible = self.db.get_messages(
            "room@chatroom", since_ts=0, limit=3, page_forward=True
        )
        cursor_page = self.db._get_messages_from_paths(
            "room@chatroom",
            [self.db_path],
            "Chat_test",
            since_ts=0,
            limit=3,
            page_forward=True,
            include_filtered=True,
        )

        self.assertEqual([m["timestamp"] for m in visible], [101, 103])
        self.assertEqual([m["timestamp"] for m in cursor_page], [101, 102, 103])
        self.assertEqual(cursor_page[1]["text"], "")

    def test_cursor_message_identity_does_not_depend_on_snapshot_filename(self):
        first = self.db._get_messages_from_paths(
            "room@chatroom",
            [self.db_path],
            "Chat_test",
            since_ts=100,
            limit=1,
            page_forward=True,
            include_filtered=True,
            db_shard_id="canonical-shard",
        )[0]
        copied_path = os.path.join(self.tmp.name, "random-snapshot-name.db")
        source = sqlite3.connect(self.db_path)
        target = sqlite3.connect(copied_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        second = self.db._get_messages_from_paths(
            "room@chatroom",
            [copied_path],
            "Chat_test",
            since_ts=100,
            limit=1,
            page_forward=True,
            include_filtered=True,
            db_shard_id="canonical-shard",
        )[0]

        self.assertEqual(first["source_message_id"], second["source_message_id"])

    def test_get_messages_can_include_cursor_timestamp_for_identity_dedup(self):
        messages = self.db.get_messages(
            "room@chatroom",
            since_ts=103,
            limit=3,
            page_forward=True,
            since_inclusive=True,
        )

        self.assertEqual([m["timestamp"] for m in messages], [103, 104, 105])


class WeChatDBShardCompletenessTests(unittest.TestCase):
    def test_get_messages_raises_content_free_error_instead_of_returning_partial_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            message_dir = os.path.join(tmp, "message")
            os.makedirs(message_dir)
            healthy_path = os.path.join(message_dir, "message_0.db")
            broken_path = os.path.join(message_dir, "message_1.db")
            username = "strict-room@chatroom"
            table_name = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
            conn = sqlite3.connect(healthy_path)
            try:
                conn.execute(
                    f"""
                    CREATE TABLE [{table_name}] (
                        local_type INTEGER,
                        create_time INTEGER,
                        message_content TEXT,
                        WCDB_CT_message_content INTEGER,
                        status INTEGER
                    )
                    """
                )
                conn.execute(
                    f"INSERT INTO [{table_name}] VALUES (1, 200, 'sender:\nhealthy', NULL, 0)"
                )
                conn.commit()
            finally:
                conn.close()
            with open(broken_path, "wb") as handle:
                handle.write(b"not sqlite")
            db = WeChatDB(tmp, {
                "message/message_0.db": {"enc_key": "fixture"},
                "message/message_1.db": {"enc_key": "fixture"},
            })
            db._contacts = {"sender": "成员"}
            db._contacts_full = []
            db._nick_to_remark = {}
            db._load_contacts = lambda: None
            db._get_decrypted_db = lambda rel_key: (
                healthy_path if rel_key.endswith("message_0.db") else broken_path
            )

            with self.assertRaises(WeChatSourceDegraded) as raised:
                db.get_messages(username, since_ts=100, page_forward=True)

            self.assertEqual(raised.exception.code, "source_shard_unavailable")
            self.assertNotIn(tmp, str(raised.exception))
            self.assertNotIn(username, str(raised.exception))
            self.assertNotIn("message_1.db", str(raised.exception))


class WeChatSourceEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "message_7.db")
        self.username = "room@chatroom"
        self.table_name = "Chat_source"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(f"""
                CREATE TABLE [{self.table_name}] (
                    local_id INTEGER,
                    server_id INTEGER,
                    sort_seq INTEGER,
                    local_type INTEGER,
                    create_time INTEGER,
                    message_content TEXT,
                    WCDB_CT_message_content INTEGER,
                    status INTEGER,
                    packed_info_data BLOB
                )
            """)
            file_xml = (
                "sender:\n<msg><appmsg><title>reliable source.pdf</title><type>6</type>"
                "<appattach><totallen>12</totallen><fileext>pdf</fileext>"
                "<md5>0123456789abcdef0123456789abcdef</md5><attachid>att-7</attachid>"
                "</appattach></appmsg></msg>"
            )
            conn.execute(
                f"INSERT INTO [{self.table_name}] VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (7, 7007, 70, 49, 100, file_xml, None, 0, None),
            )
            image_hash = "abcdef0123456789abcdef0123456789"
            packed = b"\x0a\x20" + image_hash.encode("ascii")
            conn.execute(
                f"INSERT INTO [{self.table_name}] VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (8, 8008, 80, 3, 101, "sender:\n<msg><img /></msg>", None, 0, packed),
            )
            conn.commit()
        finally:
            conn.close()

        self.db = object.__new__(WeChatDB)
        self.db._contacts = {"sender": "成员"}
        self.db._nick_to_remark = {}
        self.db._load_contacts = lambda: None
        self.db._find_msg_table = lambda username: ([self.db_path], self.table_name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_metadata_is_parsed_before_text_cleaning(self):
        message = self.db.get_messages(self.username, limit=10)[0]

        self.assertEqual(message["text"], "[文件] reliable source.pdf")
        self.assertRegex(message["source_message_id"], r"^wgmsg_[0-9a-f]{32}$")
        self.assertEqual(message["source_envelope"]["local_id"], 7)
        self.assertEqual(message["source_envelope"]["server_id"], 7007)
        self.assertEqual(message["source_envelope"]["sort_seq"], 70)
        self.assertTrue(message["source_envelope"]["rowid"])
        self.assertNotIn("room@chatroom", json.dumps(message["source_envelope"]))
        self.assertEqual(message["resources"], [{
            "kind": "file",
            "resource_index": 0,
            "original_name": "reliable source.pdf",
            "extension": "pdf",
            "declared_size": 12,
            "declared_hash": "0123456789abcdef0123456789abcdef",
            "md5": "0123456789abcdef0123456789abcdef",
            "attach_id": "att-7",
            "source_message_id": message["source_message_id"],
        }])

    def test_image_packed_hash_is_preserved_without_raw_blob(self):
        message = self.db.get_messages(self.username, limit=10)[1]

        self.assertEqual(message["text"], "[图片]")
        self.assertTrue(message["source_envelope"]["packed_info_present"])
        self.assertNotIn("packed_info_data", message["source_envelope"])
        self.assertEqual(message["resources"][0]["declared_hash"], "abcdef0123456789abcdef0123456789")

    def test_ai_format_excludes_source_envelope_and_internal_ids(self):
        messages = self.db.get_messages(self.username, limit=10)
        formatted = self.db.format_messages_for_ai(messages, show_group_nickname=True)

        self.assertIn("[文件] reliable source.pdf", formatted)
        self.assertNotIn("wgmsg_", formatted)
        self.assertNotIn("server_id", formatted)
        self.assertNotIn("message_7.db", formatted)

    def test_keyset_cursor_bounds_large_same_second_bucket_and_namespaces_ids(self):
        username = "same-second@chatroom"
        table_name = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"

        def build_root(name):
            root = os.path.join(self.tmp.name, name)
            message_dir = os.path.join(root, "message")
            os.makedirs(message_dir)
            path = os.path.join(message_dir, "message_0.db")
            conn = sqlite3.connect(path)
            try:
                conn.execute(f"""
                    CREATE TABLE [{table_name}] (
                        local_id INTEGER,
                        server_id INTEGER,
                        sort_seq INTEGER,
                        local_type INTEGER,
                        create_time INTEGER,
                        message_content TEXT,
                        WCDB_CT_message_content INTEGER,
                        status INTEGER,
                        packed_info_data BLOB
                    )
                """)
                conn.executemany(
                    f"INSERT INTO [{table_name}] VALUES (?, ?, ?, 1, 100, ?, NULL, 0, NULL)",
                    [
                        (index, 10_000 + index, index, f"sender:\nrow-{index}")
                        for index in range(1, 1_206)
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            return root

        first = WeChatDB(build_root("root-a"), {})
        second = WeChatDB(build_root("root-b"), {})
        for source in (first, second):
            source._contacts = {"sender": "成员"}
            source._contacts_full = []
            source._nick_to_remark = {}
            source._load_contacts = lambda: None

        first_shard = first.get_message_shards(username)[0]
        second_shard = second.get_message_shards(username)[0]
        self.assertNotEqual(first_shard, second_shard)

        token = ""
        pages = []
        identities = []
        while True:
            result = first.get_cursor_page_for_shard(
                username,
                first_shard,
                cursor_token=token,
                since_ts=0,
                limit=500,
            )
            pages.append(len(result["messages"]))
            identities.extend(
                message["source_message_id"] for message in result["messages"]
            )
            token = result["next_cursor"]
            if result["exhausted"]:
                break

        self.assertEqual(pages, [500, 500, 205])
        self.assertEqual(len(identities), 1_205)
        self.assertEqual(len(set(identities)), 1_205)
        other = second.get_cursor_page_for_shard(
            username,
            second_shard,
            since_ts=0,
            limit=1,
        )["messages"][0]
        self.assertNotEqual(identities[0], other["source_message_id"])


class WeChatStableSourceMessageIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cache_dir = WeChatDB.CACHE_DIR
        self.addCleanup(setattr, WeChatDB, "CACHE_DIR", self.old_cache_dir)
        WeChatDB.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        self.root = os.path.join(self.tmp.name, "source")
        self.rel_key = "message/message_0.db"
        self.source_path = os.path.join(self.root, self.rel_key)
        self.username = "stable-room@chatroom"
        self.table = f"Msg_{hashlib.md5(self.username.encode()).hexdigest()}"

    def _write_source(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute(f"""
                CREATE TABLE [{self.table}] (
                    local_id INTEGER, server_id INTEGER, sort_seq INTEGER,
                    local_type INTEGER, create_time INTEGER,
                    message_content TEXT, WCDB_CT_message_content INTEGER,
                    status INTEGER, packed_info_data BLOB
                )
            """)
            conn.execute(
                f"INSERT INTO [{self.table}] VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (7, 7007, 70, 1, 100, "sender:\nstable row", None, 0, None),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _prepare(db):
        db._contacts = {"sender": "成员"}
        db._contacts_full = []
        db._nick_to_remark = {}
        db._load_contacts = lambda: None
        return db

    def test_physical_generation_changes_but_message_identity_stays_logical(self):
        self._write_source(self.source_path)
        db = self._prepare(WeChatDB(self.root, keys={}))
        first_generation = db.get_message_shards(self.username)[0]
        first = db.get_cursor_page_for_shard(
            self.username, first_generation, limit=1,
        )["messages"][0]

        replacement = os.path.join(self.root, "replacement.db")
        self._write_source(replacement)
        os.replace(replacement, self.source_path)
        second_generation = db.get_message_shards(self.username)[0]
        second = db.get_cursor_page_for_shard(
            self.username, second_generation, limit=1,
        )["messages"][0]

        self.assertNotEqual(first_generation, second_generation)
        self.assertEqual(first["source_message_id"], second["source_message_id"])
        self.assertEqual(
            first["source_envelope"]["db_shard_id"],
            second["source_envelope"]["db_shard_id"],
        )
        self.assertEqual(
            first["source_envelope"]["source_generation_id"],
            first_generation,
        )
        self.assertEqual(
            second["source_envelope"]["source_generation_id"],
            second_generation,
        )


class WeChatImageDecoderTests(unittest.TestCase):
    def test_v2_image_data_decodes_with_saved_key(self):
        key = b"1234567890abcdef"
        image = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 10
        aes_size = len(image)
        padding_size = 16 - (aes_size % 16)
        padded = image + bytes([padding_size]) * padding_size
        encrypted = AES.new(key, AES.MODE_ECB).encrypt(padded)
        data = (
            b"\x07\x08\x56\x32\x08\x07"
            + struct.pack("<I", aes_size)
            + struct.pack("<I", 0)
            + b"\x01"
            + encrypted
        )

        decoded = decode_wechat_image_data(data, key.hex())

        self.assertEqual(decoded, image)
        self.assertEqual(detect_mime(decoded), "image/jpeg")

    def test_v2_image_data_without_key_returns_none(self):
        data = b"\x07\x08\x56\x32\x08\x07" + b"\x00" * 32

        self.assertIsNone(decode_wechat_image_data(data, ""))


class WeChatMediaPagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "media.db")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE Chat_media (
                    local_type INTEGER,
                    create_time INTEGER,
                    message_content TEXT,
                    WCDB_CT_message_content INTEGER,
                    status INTEGER,
                    packed_info_data BLOB
                )
            """)
            for ts in range(101, 111):
                conn.execute(
                    "INSERT INTO Chat_media VALUES (?, ?, ?, ?, ?, ?)",
                    (3, ts, "sender:\n<msg><img /></msg>", None, 0, None),
                )
            for ts in range(201, 211):
                md5 = f"{ts:032x}"[-32:]
                content = (
                    "sender:\n"
                    f'<msg><emoji md5="{md5}" cdnurl="https://example.com/{ts}.png" '
                    'fromusername="sender" /></msg>'
                )
                conn.execute(
                    "INSERT INTO Chat_media VALUES (?, ?, ?, ?, ?, ?)",
                    (47, ts, content, None, 0, None),
                )
            conn.commit()
        finally:
            conn.close()

        self.db = object.__new__(WeChatDB)
        self.db._contacts = {"sender": "成员"}
        self.db._nick_to_remark = {}
        self.db._emoticon_map = {}
        self.db._load_contacts = lambda: None
        self.db._load_emoticon_db = lambda: None
        self.db._find_msg_table = lambda username: ([self.db_path], "Chat_media")
        self.db._find_image_file = lambda *args, **kwargs: None

    def tearDown(self):
        self.tmp.cleanup()

    def test_image_messages_since_returns_latest_page_chronological(self):
        messages = self.db.get_image_messages("room@chatroom", since_ts=100, limit=3)

        self.assertEqual([m["timestamp"] for m in messages], [108, 109, 110])

    def test_emoji_messages_since_returns_latest_page_chronological(self):
        messages = self.db.get_emoji_messages("room@chatroom", since_ts=200, limit=3)

        self.assertEqual([m["timestamp"] for m in messages], [208, 209, 210])


if __name__ == "__main__":
    unittest.main()
