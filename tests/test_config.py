import json
import multiprocessing
import os
import tempfile
import unittest
from unittest.mock import patch

from core.config import (
    ConfigConflictError,
    ConfigError,
    ConfigStore,
    DATA_DIR,
    DEFAULT_CONFIG,
    _sanitize_config,
    active_monitor_chats,
    merge_monitor_chat_preferences,
    load_config,
    selected_resource_backup_chats,
    selected_drive_sync_chats,
)
from core.taxonomy_assignment import FREE_FORM_PROFILE


def _config_patch_worker(path, field, value, start_event, iterations=1):
    store = ConfigStore(path)
    start_event.wait(5)
    for _ in range(iterations):
        store.update(lambda config: {**config, field: value})


class ConfigTests(unittest.TestCase):
    def test_monitor_ai_timeout_defaults_and_is_sanitized(self):
        self.assertEqual(DEFAULT_CONFIG["monitor_ai_timeout_seconds"], 90)
        self.assertEqual(
            _sanitize_config({"monitor_ai_timeout_seconds": 120})[
                "monitor_ai_timeout_seconds"
            ],
            120,
        )
        self.assertEqual(
            _sanitize_config({"monitor_ai_timeout_seconds": 0})[
                "monitor_ai_timeout_seconds"
            ],
            90,
        )

    def test_auto_detect_install_does_not_overwrite_concurrent_explicit_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            explicit = os.path.join(tmp, "explicit-but-unmounted")
            detected = os.path.join(tmp, "detected")
            store = ConfigStore(path)
            store.replace({"db_dir": ""})

            def detect_after_explicit_write():
                store.update(lambda config: {**config, "db_dir": explicit})
                return detected

            with (
                patch("core.config.CONFIG_FILE", path),
                patch("core.config.DATA_DIR", tmp),
                patch("core.config.auto_detect_db_dir", side_effect=detect_after_explicit_write),
            ):
                loaded = load_config()

            self.assertEqual(loaded["db_dir"], explicit)
            self.assertEqual(store.read()["db_dir"], explicit)

    def test_nonempty_unavailable_explicit_source_is_not_auto_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            explicit = os.path.join(tmp, "temporarily-unavailable")
            ConfigStore(path).replace({"db_dir": explicit})

            with (
                patch("core.config.CONFIG_FILE", path),
                patch("core.config.DATA_DIR", tmp),
                patch("core.config.auto_detect_db_dir") as detect,
            ):
                loaded = load_config()

            detect.assert_not_called()
            self.assertEqual(loaded["db_dir"], explicit)

    def test_config_store_noop_patch_keeps_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(os.path.join(tmp, "config.json"))
            initial = store.replace({"monitor_enabled": False})

            unchanged = store.update(
                lambda config: {**config, "monitor_enabled": False}
            )

            self.assertEqual(unchanged, initial)

    def test_config_store_preserves_concurrent_disjoint_process_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            store = ConfigStore(path)
            initial = store.replace({})
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            left = context.Process(
                target=_config_patch_worker,
                args=(path, "monitor_enabled", True, start),
            )
            right = context.Process(
                target=_config_patch_worker,
                args=(path, "daily_digest_enabled", False, start),
            )
            left.start()
            right.start()
            start.set()
            left.join(10)
            right.join(10)

            self.assertEqual(left.exitcode, 0)
            self.assertEqual(right.exitcode, 0)
            current = store.read()
            self.assertTrue(current["monitor_enabled"])
            self.assertFalse(current["daily_digest_enabled"])
            self.assertEqual(
                current["config_revision"],
                initial["config_revision"] + 2,
            )

    def test_config_store_readers_observe_only_complete_json_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            store = ConfigStore(path)
            store.replace({"monitor_interval_minutes": 1})
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            writer = context.Process(
                target=_config_patch_worker,
                args=(path, "monitor_interval_minutes", 2, start, 80),
            )
            writer.start()
            start.set()
            observed = 0
            while writer.is_alive():
                with open(path, encoding="utf-8") as handle:
                    value = json.load(handle)
                self.assertIn(value["monitor_interval_minutes"], {1, 2})
                observed += 1
            writer.join(10)

            self.assertEqual(writer.exitcode, 0)
            self.assertGreater(observed, 0)

    def test_config_store_interrupted_publish_keeps_previous_canonical_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            store = ConfigStore(path)
            before = store.replace({"monitor_enabled": False})

            with patch("core.config.os.replace", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    store.update(
                        lambda config: {**config, "monitor_enabled": True}
                    )

            after = store.read()
            self.assertEqual(after, before)
            self.assertFalse(after["monitor_enabled"])

    def test_corrupt_primary_fails_closed_without_default_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"monitor_enabled":')
            with open(path, "rb") as handle:
                before = handle.read()

            with self.assertRaisesRegex(ConfigError, "config_corrupt"):
                ConfigStore(path).read()

            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), before)

    def test_stale_whole_document_replace_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(os.path.join(tmp, "config.json"))
            stale = store.replace({"monitor_enabled": False})
            store.update(lambda config: {**config, "daily_digest_enabled": False})

            with self.assertRaisesRegex(
                ConfigConflictError, "config_revision_conflict"
            ):
                store.replace(stale, expected_revision=stale["config_revision"])

    def test_active_monitor_chats_falls_back_to_valid_v1_singleton(self):
        self.assertEqual(
            active_monitor_chats({
                "monitor_chats": [],
                "monitor_chat_username": " room@chatroom ",
                "monitor_chat_display_name": " Legacy Room ",
            }),
            [{"username": "room@chatroom", "name": "Legacy Room"}],
        )

    def test_active_monitor_chats_prefers_non_empty_valid_multi_chat_config(self):
        self.assertEqual(
            active_monitor_chats({
                "monitor_chats": [
                    {"username": " current@chatroom ", "name": " Current Room "},
                    {"username": "", "name": "Ignored"},
                ],
                "monitor_chat_username": "legacy@chatroom",
                "monitor_chat_display_name": "Legacy Room",
            }),
            [{"username": "current@chatroom", "name": "Current Room"}],
        )

    def test_default_runtime_paths_follow_effective_project_data_dir(self):
        self.assertEqual(
            DEFAULT_CONFIG["keys_file"], os.path.join(DATA_DIR, "all_keys.json")
        )
        self.assertEqual(
            DEFAULT_CONFIG["decrypted_dir"], os.path.join(DATA_DIR, "decrypted")
        )
        self.assertEqual(
            DEFAULT_CONFIG["monitor_knowledge_db"],
            os.path.join(DATA_DIR, "monitor_knowledge.db"),
        )
        self.assertEqual(
            DEFAULT_CONFIG["monitor_obsidian_root"],
            os.path.join(DATA_DIR, "obsidian_knowledge"),
        )

    def test_legacy_default_paths_are_rebased_to_new_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_data = os.path.join(tmp, ".wechat-summary")
            new_data = os.path.join(tmp, ".we-groupchat-obsidian")
            cfg = _sanitize_config({
                "keys_file": os.path.join(old_data, "all_keys.json"),
                "decrypted_dir": os.path.join(old_data, "decrypted"),
                "monitor_knowledge_db": os.path.join(old_data, "monitor_knowledge.db"),
                "monitor_obsidian_root": os.path.join(old_data, "obsidian_knowledge"),
            })

            with patch("core.config.DATA_DIR", new_data), \
                 patch("core.config.LEGACY_DATA_DIR", old_data):
                cfg = _sanitize_config(cfg)

            self.assertEqual(cfg["keys_file"], os.path.join(new_data, "all_keys.json"))
            self.assertEqual(cfg["decrypted_dir"], os.path.join(new_data, "decrypted"))
            self.assertEqual(cfg["monitor_knowledge_db"], os.path.join(new_data, "monitor_knowledge.db"))
            self.assertEqual(cfg["monitor_obsidian_root"], os.path.join(new_data, "obsidian_knowledge"))

    def test_monitor_notification_flags_are_preserved(self):
        cfg = _sanitize_config({
            "background_notifications_enabled": False,
            "monitor_notify_writes": False,
            "monitor_notify_checkins": True,
        })

        self.assertFalse(cfg["background_notifications_enabled"])
        self.assertFalse(cfg["monitor_notify_writes"])
        self.assertTrue(cfg["monitor_notify_checkins"])

    def test_monitor_notification_defaults(self):
        self.assertTrue(DEFAULT_CONFIG["background_notifications_enabled"])
        self.assertTrue(DEFAULT_CONFIG["monitor_notify_writes"])
        self.assertFalse(DEFAULT_CONFIG["monitor_notify_checkins"])

    def test_legacy_link_preview_enablement_is_forced_off(self):
        self.assertFalse(DEFAULT_CONFIG["monitor_fetch_links"])
        cfg = _sanitize_config({"monitor_fetch_links": True})
        self.assertFalse(cfg["monitor_fetch_links"])

    def test_mcp_send_mode_defaults_and_sanitized_config(self):
        self.assertEqual(DEFAULT_CONFIG["mcp_send_mode"], "disabled")
        cfg = _sanitize_config({
            "mcp_send_mode": " allowlist ",
            "mcp_send_allowlist": [" room@chatroom ", "", 42, "wxid_example"],
        })

        self.assertEqual(cfg["mcp_send_mode"], "allowlist")
        self.assertEqual(cfg["mcp_send_allowlist"], ["room@chatroom", "wxid_example"])

    def test_legacy_mcp_send_boolean_does_not_activate_send_mode(self):
        cfg = _sanitize_config({"mcp_enable_send_message": True})

        self.assertTrue(cfg["mcp_enable_send_message"])
        self.assertEqual(cfg["mcp_send_mode"], "disabled")

    def test_daily_digest_defaults_and_sanitized_config(self):
        cfg = _sanitize_config({
            "daily_digest_enabled": False,
            "daily_digest_notify": False,
            "daily_digest_time": "22:15",
            "daily_digest_timezone": "Asia/Shanghai",
            "daily_digest_dir": "~/daily digests",
        })

        self.assertTrue(DEFAULT_CONFIG["daily_digest_enabled"])
        self.assertTrue(DEFAULT_CONFIG["daily_digest_notify"])
        self.assertEqual(DEFAULT_CONFIG["daily_digest_time"], "21:30")
        self.assertEqual(DEFAULT_CONFIG["daily_digest_dir"], "")
        self.assertFalse(cfg["daily_digest_enabled"])
        self.assertFalse(cfg["daily_digest_notify"])
        self.assertEqual(cfg["daily_digest_time"], "22:15")
        self.assertEqual(cfg["daily_digest_timezone"], "Asia/Shanghai")
        self.assertTrue(cfg["daily_digest_dir"].endswith("daily digests"))

    def test_source_reliability_defaults_and_config_are_sanitized(self):
        self.assertFalse(DEFAULT_CONFIG["wechat_source_guard_enabled"])
        self.assertFalse(DEFAULT_CONFIG["attachment_archive_enabled"])
        self.assertEqual(DEFAULT_CONFIG["attachment_archive_kinds"], ["file"])
        self.assertEqual(DEFAULT_CONFIG["attachment_archive_max_object_bytes"], 512 * 1024 * 1024)
        self.assertEqual(DEFAULT_CONFIG["attachment_archive_min_free_bytes"], 1024 * 1024 * 1024)
        self.assertEqual(DEFAULT_CONFIG["attachment_backup_target"], "")
        self.assertEqual(DEFAULT_CONFIG["resource_backup_selected_chats"], [])
        self.assertFalse(DEFAULT_CONFIG["resource_backup_enabled"])
        self.assertEqual(DEFAULT_CONFIG["resource_backup_interval_seconds"], 300)
        self.assertEqual(DEFAULT_CONFIG["resource_backup_max_messages_per_scan"], 500)
        self.assertEqual(
            DEFAULT_CONFIG["resource_backup_min_free_bytes"],
            1024 * 1024 * 1024,
        )
        self.assertFalse(DEFAULT_CONFIG["google_drive_file_sync_enabled"])
        self.assertFalse(DEFAULT_CONFIG["google_drive_file_sync_paused"])
        self.assertEqual(DEFAULT_CONFIG["google_drive_file_sync_selected_chats"], [])
        self.assertEqual(DEFAULT_CONFIG["google_drive_file_sync_root_name"], "微信群文件归档")
        self.assertTrue(DEFAULT_CONFIG["google_drive_file_sync_keep_local_objects"])

        cfg = _sanitize_config({
            "wechat_source_guard_enabled": True,
            "wechat_source_guard_grace_seconds": 45,
            "wechat_source_guard_interval_seconds": 30,
            "wechat_source_guard_restart_budget": 4,
            "wechat_source_guard_pause_until": "indefinite",
            "attachment_archive_enabled": False,
            "attachment_archive_kinds": [" image ", "file", "image", "video", 7],
            "attachment_archive_root": "~/private attachment archive",
            "attachment_archive_max_object_bytes": 8 * 1024 * 1024,
            "attachment_archive_min_free_bytes": 2 * 1024 * 1024,
            "attachment_archive_retry_base_seconds": 15,
            "attachment_archive_retry_max_seconds": 300,
            "attachment_backup_target": "~/Google Drive/WeChat backup",
            "resource_backup_enabled": True,
        })

        self.assertTrue(cfg["wechat_source_guard_enabled"])
        self.assertEqual(cfg["wechat_source_guard_grace_seconds"], 45)
        self.assertEqual(
            cfg["wechat_source_guard_interval_seconds"],
            DEFAULT_CONFIG["wechat_source_guard_interval_seconds"],
        )
        self.assertEqual(cfg["wechat_source_guard_restart_budget"], 4)
        self.assertEqual(cfg["wechat_source_guard_pause_until"], "indefinite")
        self.assertFalse(cfg["attachment_archive_enabled"])
        self.assertEqual(cfg["attachment_archive_kinds"], ["image", "file"])
        self.assertTrue(cfg["attachment_archive_root"].endswith("private attachment archive"))
        self.assertEqual(cfg["attachment_archive_max_object_bytes"], 8 * 1024 * 1024)
        self.assertEqual(cfg["attachment_archive_min_free_bytes"], 2 * 1024 * 1024)
        self.assertEqual(cfg["attachment_archive_retry_base_seconds"], 15)
        self.assertEqual(cfg["attachment_archive_retry_max_seconds"], 300)
        self.assertTrue(cfg["attachment_backup_target"].endswith("Google Drive/WeChat backup"))
        self.assertTrue(cfg["resource_backup_enabled"])
        self.assertNotIn("resource_backup_file_resolution_enabled", cfg)

    def test_google_drive_file_sync_config_is_private_opt_in_and_sanitized(self):
        cfg = _sanitize_config({
            "google_drive_file_sync_enabled": True,
            "google_drive_file_sync_paused": True,
            "google_drive_file_sync_selected_chats": [
                {"username": " room@chatroom ", "alias": " 稳定群名 "},
                {"username": "room@chatroom", "alias": "duplicate"},
                {"username": "wxid_person", "alias": "ignored"},
                {"username": "other@chatroom", "alias": 7},
            ],
            "google_drive_file_sync_interval_seconds": 600,
            "google_drive_file_sync_max_messages_per_scan": 750,
            "google_drive_file_sync_max_uploads_per_run": 30,
            "google_drive_file_sync_max_bytes_per_run": 1024 * 1024 * 1024,
            "google_drive_file_sync_root_name": " 私有群文件 ",
            "google_drive_file_sync_keep_local_objects": False,
            "google_drive_file_sync_db": "~/.we-groupchat-obsidian/drive.db",
        })

        self.assertTrue(cfg["google_drive_file_sync_enabled"])
        self.assertTrue(cfg["google_drive_file_sync_paused"])
        self.assertEqual(
            selected_drive_sync_chats(cfg),
            [
                {"username": "room@chatroom", "alias": "稳定群名"},
                {"username": "other@chatroom", "alias": ""},
            ],
        )
        self.assertEqual(cfg["google_drive_file_sync_interval_seconds"], 600)
        self.assertEqual(cfg["google_drive_file_sync_max_messages_per_scan"], 750)
        self.assertEqual(cfg["google_drive_file_sync_max_uploads_per_run"], 30)
        self.assertEqual(cfg["google_drive_file_sync_max_bytes_per_run"], 1024 * 1024 * 1024)
        self.assertEqual(cfg["google_drive_file_sync_root_name"], "私有群文件")
        self.assertTrue(cfg["google_drive_file_sync_keep_local_objects"])
        self.assertTrue(cfg["google_drive_file_sync_db"].endswith("drive.db"))

        invalid = _sanitize_config({
            "wechat_source_guard_pause_until": "tomorrow",
            "attachment_archive_kinds": ["video"],
            "attachment_archive_max_object_bytes": 1,
            "attachment_archive_min_free_bytes": -1,
        })
        self.assertEqual(invalid["wechat_source_guard_pause_until"], "")
        self.assertEqual(invalid["attachment_archive_kinds"], ["file"])
        self.assertEqual(
            invalid["attachment_archive_max_object_bytes"],
            DEFAULT_CONFIG["attachment_archive_max_object_bytes"],
        )
        self.assertEqual(
            invalid["attachment_archive_min_free_bytes"],
            DEFAULT_CONFIG["attachment_archive_min_free_bytes"],
        )

    def test_resource_backup_selection_is_private_and_independent_from_oauth_lane(self):
        cfg = _sanitize_config({
            "google_drive_file_sync_selected_chats": [
                {"username": "oauth@chatroom", "alias": "OAuth lane"},
            ],
            "resource_backup_selected_chats": [
                {
                    "username": " mounted@chatroom ",
                    "alias": " Mounted lane ",
                    "selected_since": 123,
                },
                {"username": "mounted@chatroom", "alias": "duplicate"},
                {"username": "wxid_person", "alias": "ignored"},
            ],
            "resource_backup_interval_seconds": 600,
            "resource_backup_max_messages_per_scan": 750,
            "resource_backup_min_free_bytes": 2 * 1024 * 1024,
        })

        self.assertEqual(
            selected_drive_sync_chats(cfg),
            [{"username": "oauth@chatroom", "alias": "OAuth lane"}],
        )
        self.assertEqual(
            selected_resource_backup_chats(cfg),
            [{
                "username": "mounted@chatroom",
                "alias": "Mounted lane",
                "selected_since": 123,
            }],
        )
        self.assertEqual(cfg["resource_backup_interval_seconds"], 600)
        self.assertEqual(cfg["resource_backup_max_messages_per_scan"], 750)
        self.assertEqual(cfg["resource_backup_min_free_bytes"], 2 * 1024 * 1024)

    def test_monitor_chat_aliases_are_sanitized(self):
        cfg = _sanitize_config({
            "monitor_chat_aliases": {
                " room@chatroom ": " Stable Vault Name ",
                "empty@chatroom": "",
                "bad-value@chatroom": 42,
                "bad-key": "Ignored",
                1: "Also ignored",
            },
        })

        self.assertEqual(
            cfg["monitor_chat_aliases"],
            {"room@chatroom": "Stable Vault Name"},
        )

    def test_monitor_chat_taxonomy_profiles_are_sanitized(self):
        cfg = _sanitize_config({
            "monitor_chat_taxonomy_profiles": {
                " room@chatroom ": " human_ai_intimacy_v1 ",
                "unknown@chatroom": "future_profile",
                "bad-key": "human_ai_intimacy_v1",
                "empty@chatroom": "",
                1: "human_ai_intimacy_v1",
            },
        })
        self.assertEqual(
            cfg["monitor_chat_taxonomy_profiles"],
            {
                "room@chatroom": "human_ai_intimacy_v1",
                "unknown@chatroom": "future_profile",
            },
        )

    def test_merge_monitor_chat_preferences_preserves_existing_maps(self):
        config = {
            "monitor_chat_aliases": {"existing@chatroom": "Existing"},
            "monitor_chat_taxonomy_profiles": {
                "existing@chatroom": "human_ai_intimacy_v1",
            },
        }

        updated = merge_monitor_chat_preferences(
            config,
            [{"username": "new@chatroom", "name": "New Chat"}],
            profile_by_username={"new@chatroom": "future_profile"},
            alias_by_username={"new@chatroom": "Stable New"},
        )

        self.assertEqual(
            updated["monitor_chat_aliases"],
            {
                "existing@chatroom": "Existing",
                "new@chatroom": "Stable New",
            },
        )
        self.assertEqual(
            updated["monitor_chat_taxonomy_profiles"],
            {
                "existing@chatroom": "human_ai_intimacy_v1",
                "new@chatroom": "future_profile",
            },
        )
        self.assertNotIn("new@chatroom", config["monitor_chat_aliases"])

    def test_merge_monitor_chat_preferences_seeds_alias_without_changing_profiles(self):
        config = {
            "monitor_chat_aliases": {},
            "monitor_chat_taxonomy_profiles": {
                "room@chatroom": "human_ai_intimacy_v1",
            },
        }
        updated = merge_monitor_chat_preferences(
            config,
            [{"username": "room@chatroom", "name": "群已经改名"}],
        )
        self.assertEqual(updated["monitor_chat_aliases"]["room@chatroom"], "群已经改名")
        self.assertEqual(
            updated["monitor_chat_taxonomy_profiles"],
            {"room@chatroom": "human_ai_intimacy_v1"},
        )

    def test_merge_monitor_chat_preferences_persists_blank_as_free_form(self):
        config = {
            "monitor_chat_taxonomy_profiles": {
                "room@chatroom": "human_ai_intimacy_v1",
            },
        }

        updated = merge_monitor_chat_preferences(
            config,
            [{"username": "room@chatroom", "name": "Room"}],
            profile_by_username={"room@chatroom": ""},
        )

        self.assertEqual(
            updated["monitor_chat_taxonomy_profiles"],
            {"room@chatroom": FREE_FORM_PROFILE},
        )

    def test_merge_monitor_chat_preferences_requires_stable_username(self):
        with self.assertRaisesRegex(
            ValueError, "selected chat lacks a stable @chatroom username"
        ):
            merge_monitor_chat_preferences(
                {}, [{"username": "wxid_unstable", "name": "Unstable"}]
            )


if __name__ == "__main__":
    unittest.main()
