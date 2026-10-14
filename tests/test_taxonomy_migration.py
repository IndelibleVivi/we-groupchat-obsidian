import hashlib
import io
import json
import os
import errno
import fcntl
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import core.taxonomy_migration as taxonomy_migration
from core.knowledge import KnowledgeStore
from core.taxonomy_migration import (
    MigrationError,
    apply_migration,
    load_sealed_run,
    preview_migration,
    rollback_migration,
    status_migration,
)
from scripts import migrate_taxonomy


DARWIN_ONLY = unittest.skipUnless(
    sys.platform == "darwin",
    "requires Darwin renameatx_np",
)


class TaxonomyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "knowledge.db")
        self.vault_root = os.path.join(self.tmp.name, "vault")
        self.run_dir = os.path.join(self.tmp.name, "sealed-run")
        self.unrelated_note = os.path.join(self.vault_root, "personal.md")
        self.config = {
            "monitor_knowledge_db": self.db_path,
            "monitor_obsidian_root": self.vault_root,
            "monitor_obsidian_subdir": "关注推送",
            "monitor_chat_taxonomy_profiles": {
                "room@chatroom": "human_ai_intimacy_v1",
            },
            "monitor_chat_aliases": {"room@chatroom": "示例人机互动群"},
        }
        self.store = KnowledgeStore.from_config(self.config)
        os.makedirs(self.vault_root, exist_ok=True)
        with open(self.unrelated_note, "w", encoding="utf-8") as handle:
            handle.write("unrelated user content\n")
        result = self.store.apply_event(
            {
                "title": "AI伴侣互动测试",
                "summary": "群里测试 AI 伴侣互动。",
                "topic_key": "sealed-preview-topic",
                "category": "互动实验与玩法",
                "entities": [],
                "key_facts": ["测试互动"],
                "links": [],
                "event_type": "discussion",
                "status_hint": "active",
            },
            [{
                "timestamp": 1,
                "time_str": "2026-07-13 10:00",
                "sender": "Example Sender",
                "text": "测试互动",
            }],
            {
                "monitor_chat_username": "room@chatroom",
                "monitor_chat_display_name": "示例人机互动群",
                **self.config,
            },
            {"relation": "new"},
        )
        old_path = result["knowledge_path"]
        legacy_rel = os.path.join(
            "关注推送", "示例人机互动群", "AI伴侣交互", os.path.basename(old_path)
        )
        self.source_note = os.path.join(self.vault_root, legacy_rel)
        os.makedirs(os.path.dirname(self.source_note), exist_ok=True)
        os.replace(old_path, self.source_note)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE topics
                SET category = 'AI伴侣交互', obsidian_path = ?,
                    taxonomy_profile = '', taxonomy_version = 0
                WHERE topic_id = ?
                """,
                (legacy_rel, result["topic_id"]),
            )
            conn.execute(
                """
                UPDATE events
                SET category = 'AI伴侣交互', taxonomy_profile = '',
                    taxonomy_version = 0
                WHERE topic_id = ?
                """,
                (result["topic_id"],),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                "scripts.migrate_taxonomy.load_config",
                return_value=dict(self.config),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = migrate_taxonomy.main(arguments)
        return result, stdout.getvalue() or stderr.getvalue()

    def test_cli_preview_json_is_counts_and_hashes_only(self):
        result, output = self.run_cli([
            "preview", "--profile", "human_ai_intimacy_v1",
            "--run-dir", self.run_dir, "--json",
        ])

        payload = json.loads(output)
        self.assertEqual(result, 0)
        self.assertRegex(payload["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(payload["state"], "planned")
        self.assertNotIn("db_path", payload)
        self.assertNotIn("vault_root", payload)
        self.assertNotIn("titles", payload)
        self.assertNotIn(self.tmp.name, output)
        self.assertNotIn("Example Sender", output)

    def test_empty_projection_cli_preview_and_status_remain_loadable(self):
        empty_projection = {
            "profile": "human_ai_intimacy_v1",
            "taxonomy_version": 2,
            "topic_changes": [],
            "render_topic_ids": [],
            "managed_date_index_paths": [],
        }
        with mock.patch.object(
            KnowledgeStore,
            "taxonomy_projection",
            return_value=empty_projection,
        ):
            result, output = self.run_cli([
                "preview", "--profile", "human_ai_intimacy_v1",
                "--run-dir", self.run_dir, "--json",
            ])
            self.assertEqual(result, 0, output)
            preview = json.loads(output)
            self.assertEqual(preview["file_count"], 0)
            payload_dir = os.path.join(self.run_dir, "payload")
            self.assertTrue(os.path.isdir(payload_dir))
            self.assertEqual(stat.S_IMODE(os.stat(payload_dir).st_mode), 0o700)

            result, output = self.run_cli([
                "status", "--run-dir", self.run_dir, "--json",
            ])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output)["state"], "already_clean")

    @DARWIN_ONLY
    def test_empty_projection_cli_apply_remains_loadable(self):
        empty_projection = {
            "profile": "human_ai_intimacy_v1",
            "taxonomy_version": 2,
            "topic_changes": [],
            "render_topic_ids": [],
            "managed_date_index_paths": [],
        }
        with mock.patch.object(
            KnowledgeStore,
            "taxonomy_projection",
            return_value=empty_projection,
        ):
            result, output = self.run_cli([
                "preview", "--profile", "human_ai_intimacy_v1",
                "--run-dir", self.run_dir, "--json",
            ])
            self.assertEqual(result, 0, output)
            preview = json.loads(output)

            manifest_sha = preview["manifest_sha256"]
            result, output = self.run_cli([
                "apply", "--run-dir", self.run_dir,
                "--manifest-sha256", manifest_sha,
                "--confirm", f"APPLY_TAXONOMY_MIGRATION:{manifest_sha}",
                "--json",
            ])
            self.assertEqual(result, 0)
            applied = json.loads(output)
            self.assertEqual(applied["state"], "applied")
            self.assertEqual(applied["file_count"], 0)

    def test_sensitive_examples_are_bounded_and_apply_rejects_sensitive_flag(self):
        self.assertEqual(self.run_cli([
            "preview", "--profile", "human_ai_intimacy_v1",
            "--run-dir", self.run_dir,
        ])[0], 0)

        result, output = self.run_cli([
            "status", "--run-dir", self.run_dir,
            "--sensitive", "--example-limit", "2", "--json",
        ])
        self.assertEqual(result, 0)
        examples = json.loads(output)["examples"]
        self.assertLessEqual(len(examples), 2)
        self.assertTrue(all(set(item) == {"relative_path", "title"} for item in examples))

        result, output = self.run_cli([
            "apply", "--run-dir", self.run_dir,
            "--manifest-sha256", "0" * 64,
            "--confirm", "anything", "--sensitive",
        ])
        self.assertEqual(result, 2)
        self.assertNotIn(self.tmp.name, output)

    def test_sensitive_examples_never_expose_configured_usernames_in_paths(self):
        config = dict(self.config)
        config["monitor_chat_aliases"] = {"room@chatroom": "room@chatroom"}
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE topics SET title = 'room@chatroom private title'")
            conn.commit()
        finally:
            conn.close()
        stdout = io.StringIO()
        with (
            mock.patch("scripts.migrate_taxonomy.load_config", return_value=config),
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(migrate_taxonomy.main([
                "preview", "--profile", "human_ai_intimacy_v1",
                "--run-dir", self.run_dir,
            ]), 0)
            stdout.seek(0)
            stdout.truncate(0)
            self.assertEqual(migrate_taxonomy.main([
                "status", "--run-dir", self.run_dir,
                "--sensitive", "--json",
            ]), 0)

        output = stdout.getvalue()
        self.assertNotIn("room@chatroom", output)
        self.assertEqual(json.loads(output)["examples"], [])

    def test_sensitive_examples_require_canonical_posix_relative_paths(self):
        unsafe_paths = (
            "/private/topic.md",
            "C:/private/topic.md",
            "C:private/topic.md",
            "z:folder/topic.md",
            "C:\\private\\topic.md",
            "//server/share/topic.md",
            "\\\\server\\share\\topic.md",
            "folder\\topic.md",
            "../private/topic.md",
            "folder/../private/topic.md",
            "./folder/topic.md",
            "folder//topic.md",
            "folder/topic.md/",
        )
        for relative_path in unsafe_paths:
            with self.subTest(relative_path=relative_path):
                with self.assertRaisesRegex(
                    MigrationError, "public_report_schema"
                ):
                    migrate_taxonomy.public_report({
                        "_examples": [{
                            "relative_path": relative_path,
                            "title": "private",
                        }],
                    }, sensitive=True)

        report = migrate_taxonomy.public_report({
            "_examples": [{
                "relative_path": "关注推送/安全/topic.md",
                "title": "safe",
            }],
        }, sensitive=True)
        self.assertEqual(
            report["examples"][0]["relative_path"],
            "关注推送/安全/topic.md",
        )

    def test_sensitive_examples_reject_bidi_spoof_controls_but_allow_emoji_zwj(self):
        bidi_controls = (
            "\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
            "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069",
        )
        for control in bidi_controls:
            with self.subTest(control=ord(control)):
                with self.assertRaisesRegex(
                    MigrationError, "public_report_schema"
                ):
                    migrate_taxonomy.public_report({
                        "_examples": [{
                            "relative_path": f"关注推送/safe{control}txt/topic.md",
                            "title": f"safe{control}private",
                        }],
                    }, sensitive=True)

        report = migrate_taxonomy.public_report({
            "_examples": [{
                "relative_path": "关注推送/家庭/topic.md",
                "title": "家庭\U0001f469\u200d\U0001f4bb",
            }],
        }, sensitive=True)
        self.assertEqual(report["examples"][0]["title"], "家庭\U0001f469\u200d\U0001f4bb")

    def test_sensitive_examples_normalize_username_variants_with_nfkc_casefold(self):
        cases = (
            ("Café@chatroom", "Cafe\u0301 chatroom private"),
            ("Cafe\u0301@chatroom", "CAFÉ@CHATROOM private"),
        )
        for username, title in cases:
            with self.subTest(username=username, title=title):
                self.reset_sealed_run()
                conn = sqlite3.connect(self.db_path)
                try:
                    conn.execute("UPDATE topics SET title = ?", (title,))
                    conn.commit()
                finally:
                    conn.close()
                config = dict(self.config)
                config["monitor_chats"] = [{"username": username}]
                stdout = io.StringIO()
                with (
                    mock.patch(
                        "scripts.migrate_taxonomy.load_config",
                        return_value=config,
                    ),
                    redirect_stdout(stdout),
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(migrate_taxonomy.main([
                        "preview", "--profile", "human_ai_intimacy_v1",
                        "--run-dir", self.run_dir,
                    ]), 0)
                    stdout.seek(0)
                    stdout.truncate(0)
                    self.assertEqual(migrate_taxonomy.main([
                        "status", "--run-dir", self.run_dir,
                        "--sensitive", "--json",
                    ]), 0)
                self.assertEqual(json.loads(stdout.getvalue())["examples"], [])

    def test_apply_and_rollback_help_show_exact_confirmation_tokens(self):
        for action, token in (
            ("apply", "APPLY_TAXONOMY_MIGRATION:<full-sha256>"),
            ("rollback", "ROLLBACK_TAXONOMY_MIGRATION:<full-sha256>"),
        ):
            with self.subTest(action=action):
                stdout = io.StringIO()
                with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
                    migrate_taxonomy.build_parser().parse_args([action, "--help"])
                self.assertEqual(raised.exception.code, 0)
                self.assertIn(token, stdout.getvalue())

    def test_cli_help_requires_every_vault_writer_to_be_quiescent(self):
        for action in ("apply", "rollback"):
            with self.subTest(action=action):
                stdout = io.StringIO()
                with redirect_stdout(stdout), self.assertRaises(SystemExit):
                    migrate_taxonomy.build_parser().parse_args([action, "--help"])
                help_text = stdout.getvalue()
                self.assertIn("all vault writers", help_text)
                self.assertIn("Obsidian", help_text)
                self.assertIn("external vault writer", help_text)

    def test_cli_rejects_abbreviations_and_out_of_range_examples_safely(self):
        result, output = self.run_cli([
            "preview", "--prof", "human_ai_intimacy_v1",
            "--run-dir", self.run_dir,
        ])
        self.assertEqual(result, 2)
        self.assertIn("cli_usage", output)

        result, output = self.run_cli([
            "status", "--run-dir", self.run_dir,
            "--example-limit", "21",
        ])
        self.assertEqual(result, 2)
        self.assertIn("example_limit", output)

    def test_cli_sanitizes_unexpected_runtime_errors(self):
        sensitive_value = "/private/example-vault/private-title"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch("scripts.migrate_taxonomy.load_config", return_value=self.config),
            mock.patch(
                "scripts.migrate_taxonomy.preview_migration",
                side_effect=RuntimeError(sensitive_value),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = migrate_taxonomy.main([
                "preview", "--profile", "human_ai_intimacy_v1",
                "--run-dir", self.run_dir,
            ])

        output = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("migration_error", output)
        self.assertNotIn(sensitive_value, output)

    def test_cli_passes_fresh_config_to_preview_status_and_apply_only(self):
        config = {"fresh": "sanitized"}
        reports = {
            "preview": {"state": "planned", "manifest_sha256": "1" * 64},
            "status": {"state": "planned", "manifest_sha256": "1" * 64},
            "apply": {"state": "applied", "manifest_sha256": "1" * 64},
            "rollback": {"state": "rolled_back", "manifest_sha256": "1" * 64},
        }
        with (
            mock.patch("scripts.migrate_taxonomy.load_config", return_value=config) as load,
            mock.patch("scripts.migrate_taxonomy.preview_migration", return_value=reports["preview"]) as preview,
            mock.patch("scripts.migrate_taxonomy.status_migration", return_value=reports["status"]) as status,
            mock.patch("scripts.migrate_taxonomy.apply_migration", return_value=reports["apply"]) as apply,
            mock.patch("scripts.migrate_taxonomy.rollback_migration", return_value=reports["rollback"]) as rollback,
            mock.patch("scripts.migrate_taxonomy._manifest_metadata", return_value={}),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(migrate_taxonomy.main([
                "preview", "--profile", "profile", "--run-dir", "run",
            ]), 0)
            self.assertEqual(migrate_taxonomy.main([
                "status", "--run-dir", "run",
            ]), 0)
            self.assertEqual(migrate_taxonomy.main([
                "apply", "--run-dir", "run", "--manifest-sha256", "1" * 64,
                "--confirm", "confirm",
            ]), 0)
            self.assertEqual(migrate_taxonomy.main([
                "rollback", "--run-dir", "run", "--manifest-sha256", "1" * 64,
                "--confirm", "confirm",
            ]), 0)

        preview.assert_called_once_with(config, "profile", "run")
        status.assert_called_once_with(config, "run")
        apply.assert_called_once_with(config, "run", "1" * 64, "confirm")
        rollback.assert_called_once_with("run", "1" * 64, "confirm")
        self.assertEqual(load.call_count, 3)

    def test_public_report_allowlists_values_and_bounds_opt_in_examples(self):
        report = migrate_taxonomy.public_report({
            "state": "planned",
            "manifest_sha256": "a" * 64,
            "topic_change_count": 1,
            "db_path": self.db_path,
            "body": "private payload",
            "_examples": [
                {"relative_path": "关注推送/a.md", "title": "A"},
                {"relative_path": "关注推送/b.md", "title": "B"},
            ],
        }, sensitive=True, example_limit=1)

        self.assertEqual(set(report), {
            "state", "manifest_sha256", "topic_change_count", "examples",
        })
        self.assertEqual(report["examples"], [
            {"relative_path": "关注推送/a.md", "title": "A"},
        ])

    def test_public_report_preserves_applied_file_count(self):
        report = migrate_taxonomy.public_report({
            "state": "mixed",
            "file_count": 3,
            "pending": 2,
            "applied": 1,
            "already_clean": 0,
        })

        self.assertEqual(report["applied"], 1)

    def test_public_report_preserves_database_status_counts(self):
        report = migrate_taxonomy.public_report({
            "state": "drifted",
            "file_count": 1,
            "pending": 1,
            "drifted": 0,
            "database_total": 4,
            "database_pending": 3,
            "database_applied": 0,
            "database_already_clean": 0,
            "database_drifted": 1,
            "database_records": [{"private": "must not escape"}],
        })

        self.assertEqual(report, {
            "state": "drifted",
            "file_count": 1,
            "pending": 1,
            "drifted": 0,
            "database_total": 4,
            "database_pending": 3,
            "database_applied": 0,
            "database_already_clean": 0,
            "database_drifted": 1,
        })

    @staticmethod
    def digest(path):
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    @classmethod
    def tree_digests(cls, root):
        return {
            os.path.relpath(os.path.join(dirpath, filename), root): cls.digest(
                os.path.join(dirpath, filename)
            )
            for dirpath, _dirnames, filenames in os.walk(root)
            for filename in sorted(filenames)
        }

    def database_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return {
                table: conn.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
                for table in ("topics", "events", "relations")
            }
        finally:
            conn.close()

    def vault_operation_artifacts(self):
        return sorted(
            os.path.relpath(os.path.join(dirpath, name), self.vault_root)
            for dirpath, dirnames, filenames in os.walk(self.vault_root)
            for name in [*dirnames, *filenames]
            if name.startswith(".taxonomy-migration-")
        )

    def rollback_mutation_snapshot(self):
        with open(os.path.join(self.run_dir, "state.json"), "rb") as handle:
            state_bytes = handle.read()
        return {
            "database": self.database_rows(),
            "database_digest": self.digest(self.db_path),
            "vault": self.tree_digests(self.vault_root),
            "artifacts": self.vault_operation_artifacts(),
            "state": state_bytes,
        }

    def create_preledger_staging_leaf(self, record):
        destination = os.path.join(
            self.vault_root, record["destination_relative_path"]
        )
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        staging = os.path.join(
            os.path.dirname(destination), record["operation_leaves"]["staging"]
        )
        with open(
            os.path.join(self.run_dir, record["payload_relative_path"]), "rb"
        ) as handle:
            payload = handle.read()
        with open(staging, "xb") as handle:
            handle.write(payload)
        os.chmod(staging, record["before_mode"])
        return staging

    def crash_apply_before_first_file_operation(self, report):
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with mock.patch(
            "core.taxonomy_migration._apply_file_record",
            side_effect=RuntimeError("crash before first file operation"),
        ):
            with self.assertRaisesRegex(RuntimeError, "crash before first file"):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        state = load_sealed_run(self.run_dir)[2]
        self.assertEqual(state["state"], "applying")
        self.assertTrue(state["database_applied"])
        self.assertEqual(set(state["file_operations"].values()), {"pending"})
        return token

    def reseal_manifest(self, mutate):
        manifest_path = os.path.join(self.run_dir, "manifest.json")
        state_path = os.path.join(self.run_dir, "state.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        mutate(manifest)
        raw = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with open(manifest_path, "wb") as handle:
            handle.write(raw)
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        state["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
        with open(state_path, "wb") as handle:
            handle.write(json.dumps(
                state,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"))

    def write_state(self, state):
        with open(os.path.join(self.run_dir, "state.json"), "wb") as handle:
            handle.write(json.dumps(
                state,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"))

    def reset_sealed_run(self):
        if os.path.exists(self.run_dir):
            shutil.rmtree(self.run_dir)

    def add_relation_dependent_topic(self):
        related = self.store.apply_event(
            {
                "title": "Relation source", "summary": "source",
                "topic_key": "relation-source", "category": "待归类",
                "entities": [], "key_facts": [], "links": [],
                "event_type": "discussion", "status_hint": "active",
            },
            [{
                "timestamp": 2, "time_str": "2026-07-13 11:00",
                "sender": "F", "text": "s",
            }],
            {
                "monitor_chat_username": "other@chatroom",
                "monitor_chat_display_name": "Other",
                **self.config,
            },
            {"relation": "new"},
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO relations(source_topic_id,target_topic_id,relation,reason,created_at) VALUES(?,?,?,?,?)",
                (related["topic_id"], 1, "related", "test", 1),
            )
            conn.commit()
        finally:
            conn.close()
        return related["topic_id"]

    def apply_manifest_post_state(self, *, database=True, files=True):
        manifest, _manifest_sha, _state = load_sealed_run(self.run_dir)
        if database:
            conn = sqlite3.connect(self.db_path)
            try:
                for change in manifest["projection"]["topic_changes"]:
                    after = change["after"]
                    conn.execute(
                        """
                        UPDATE topics SET category = ?, obsidian_path = ?,
                            taxonomy_profile = ?, taxonomy_version = ?
                        WHERE topic_id = ?
                        """,
                        (
                            after["category"], after["obsidian_path"],
                            after["taxonomy_profile"], after["taxonomy_version"],
                            change["topic_id"],
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE events SET category = ?, taxonomy_profile = ?,
                            taxonomy_version = ? WHERE topic_id = ?
                        """,
                        (
                            after["category"], after["taxonomy_profile"],
                            after["taxonomy_version"], change["topic_id"],
                        ),
                    )
                conn.commit()
            finally:
                conn.close()
        if files:
            for record in manifest["files"]:
                source = os.path.join(self.vault_root, record["source_relative_path"])
                destination = os.path.join(
                    self.vault_root, record["destination_relative_path"]
                )
                payload = os.path.join(self.run_dir, record["payload_relative_path"])
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with open(payload, "rb") as handle:
                    data = handle.read()
                if source != destination and os.path.exists(source):
                    os.unlink(source)
                with open(destination, "wb") as handle:
                    handle.write(data)
                # Model the migration's preserved mode, independent of the
                # test runner's umask when a new destination is created.
                os.chmod(destination, record["before_mode"])

    def test_preview_seals_private_manifest_without_source_mutation(self):
        before_db = self.digest(self.db_path)
        before_files = self.tree_digests(self.vault_root)

        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        manifest, manifest_sha, state = load_sealed_run(self.run_dir)

        self.assertEqual(report["manifest_sha256"], manifest_sha)
        self.assertEqual(state["state"], "planned")
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(state["schema_version"], 2)
        self.assertGreater(len(manifest["files"]), 0)
        self.assertEqual(
            state["file_operations"],
            {record["id"]: "pending" for record in manifest["files"]},
        )
        lock_path = os.path.join(self.run_dir, "operation.lock")
        self.assertEqual(oct(os.stat(lock_path).st_mode & 0o777), "0o600")
        self.assertEqual(os.path.getsize(lock_path), 0)
        operation_leaves = []
        for record in manifest["files"]:
            self.assertEqual(
                set(record["operation_leaves"]), {"staging", "quarantine"}
            )
            for leaf in record["operation_leaves"].values():
                self.assertRegex(
                    leaf,
                    rf"^\.taxonomy-migration-[0-9a-f]{{32}}-{record['id']}-(staging|quarantine)$",
                )
                operation_leaves.append(leaf)
        self.assertEqual(len(operation_leaves), len(set(operation_leaves)))
        self.assertEqual(
            oct(os.stat(self.run_dir).st_mode & 0o777),
            "0o700",
        )
        self.assertEqual(
            oct(os.stat(os.path.join(self.run_dir, "manifest.json")).st_mode & 0o777),
            "0o600",
        )
        self.assertEqual(self.digest(self.db_path), before_db)
        self.assertEqual(self.tree_digests(self.vault_root), before_files)

    def test_load_rejects_v1_manifest_and_state_with_clear_codes(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        self.reseal_manifest(lambda manifest: manifest.__setitem__("schema_version", 1))
        with self.assertRaisesRegex(
            MigrationError, "manifest_schema_version_unsupported"
        ):
            load_sealed_run(self.run_dir)

        self.reset_sealed_run()
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        _manifest, _sha, state = load_sealed_run(self.run_dir)
        state["schema_version"] = 1
        self.write_state(state)
        with self.assertRaisesRegex(
            MigrationError, "state_schema_version_unsupported"
        ):
            load_sealed_run(self.run_dir)

    @DARWIN_ONLY
    def test_apply_probes_volume_flags_before_backup_or_database_mutation(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        before_db = self.database_rows()
        before_files = self.tree_digests(self.vault_root)
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with mock.patch(
            "core.taxonomy_migration._renameatx_np",
            side_effect=OSError(errno.EINVAL, "unsupported flags on volume"),
        ):
            with self.assertRaisesRegex(
                MigrationError, "atomic_leaf_capability_unsupported"
            ):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        self.assertEqual(self.database_rows(), before_db)
        self.assertEqual(self.tree_digests(self.vault_root), before_files)
        self.assertFalse(os.path.lexists(os.path.join(self.run_dir, "backups")))

    @DARWIN_ONLY
    def test_capability_probe_cleanup_failure_refuses_without_lasting_artifact(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        before_files = self.tree_digests(self.vault_root)
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        original_rmdir = os.rmdir
        failed = False

        def fail_probe_cleanup_once(path, *args, **kwargs):
            nonlocal failed
            if "capability-probe" in os.fspath(path) and not failed:
                failed = True
                raise OSError(errno.EIO, "transient probe cleanup failure")
            return original_rmdir(path, *args, **kwargs)

        with mock.patch(
            "core.taxonomy_migration.os.rmdir",
            side_effect=fail_probe_cleanup_once,
        ):
            with self.assertRaisesRegex(
                MigrationError, "atomic_leaf_probe_cleanup_failed"
            ):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        self.assertTrue(failed)
        self.assertEqual(self.tree_digests(self.vault_root), before_files)
        self.assertEqual(self.vault_operation_artifacts(), [])
        self.assertFalse(os.path.lexists(os.path.join(self.run_dir, "backups")))

    def test_apply_refuses_when_run_operation_lock_is_already_held(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        lock_fd = os.open(os.path.join(self.run_dir, "operation.lock"), os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(MigrationError, "operation_lock_busy"):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        self.assertFalse(os.path.lexists(os.path.join(self.run_dir, "backups")))

    def test_preview_uses_one_wal_safe_read_only_snapshot(self):
        writer = sqlite3.connect(self.db_path)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute(
                "UPDATE topics SET summary = 'committed only in WAL' WHERE topic_id = 1"
            )
            writer.commit()
            with mock.patch.object(
                KnowledgeStore,
                "connect",
                side_effect=AssertionError("projection reopened configured database"),
            ):
                preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        finally:
            writer.close()

        manifest, _manifest_sha, _state = load_sealed_run(self.run_dir)
        self.assertEqual(
            manifest["source_database"]["counts"]["topics"],
            1,
        )
        render_record = next(
            record for record in manifest["database_records"]
            if record["kind"] == "topic_render"
        )
        self.assertNotEqual(render_record["before_sha256"], render_record["after_sha256"])
        self.assertNotIn("sha256", manifest["source_database"])
        topic_payload = next(
            record for record in manifest["files"] if record["kind"] == "topic"
        )
        with open(
            os.path.join(self.run_dir, topic_payload["payload_relative_path"]),
            encoding="utf-8",
        ) as handle:
            self.assertIn("committed only in WAL", handle.read())

    def test_preview_writes_canonical_manifest_and_private_payloads(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        manifest_path = os.path.join(self.run_dir, "manifest.json")
        with open(manifest_path, "rb") as handle:
            raw = handle.read()
        manifest = json.loads(raw)
        self.assertEqual(
            raw,
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        for record in manifest["files"]:
            payload_path = os.path.join(self.run_dir, record["payload_relative_path"])
            self.assertEqual(oct(os.stat(payload_path).st_mode & 0o777), "0o600")
            self.assertEqual(self.digest(payload_path), record["payload_sha256"])

    def test_preview_refuses_existing_run_dir(self):
        os.mkdir(self.run_dir)
        with self.assertRaisesRegex(MigrationError, "run_dir_exists"):
            preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)

    def test_preview_refuses_symlinked_configured_vault_root(self):
        vault_link = os.path.join(self.tmp.name, "vault-link")
        os.symlink(self.vault_root, vault_link)
        linked_config = dict(self.config)
        linked_config["monitor_obsidian_root"] = vault_link

        with self.assertRaisesRegex(MigrationError, "symlink_refused"):
            preview_migration(
                linked_config,
                "human_ai_intimacy_v1",
                self.run_dir,
            )

    def test_preview_refuses_symlinked_configured_input_ancestor(self):
        real_parent = os.path.join(self.tmp.name, "real-parent")
        os.mkdir(real_parent)
        db_path = os.path.join(real_parent, "knowledge.db")
        with open(self.db_path, "rb") as source, open(db_path, "wb") as target:
            target.write(source.read())
        link_parent = os.path.join(self.tmp.name, "linked-parent")
        os.symlink(real_parent, link_parent)
        linked_config = dict(self.config)
        linked_config["monitor_knowledge_db"] = os.path.join(
            link_parent, "knowledge.db"
        )

        with self.assertRaisesRegex(MigrationError, "symlink_refused"):
            preview_migration(linked_config, "human_ai_intimacy_v1", self.run_dir)

    def test_preview_refuses_run_directory_inside_vault(self):
        inside = os.path.join(self.vault_root, "private-run")
        with self.assertRaisesRegex(MigrationError, "run_dir_inside_vault"):
            preview_migration(self.config, "human_ai_intimacy_v1", inside)
        self.assertFalse(os.path.exists(inside))

    def test_preview_refuses_symlinked_run_parent(self):
        real_parent = os.path.join(self.tmp.name, "run-parent")
        os.mkdir(real_parent)
        link_parent = os.path.join(self.tmp.name, "run-parent-link")
        os.symlink(real_parent, link_parent)
        with self.assertRaisesRegex(MigrationError, "symlink_refused"):
            preview_migration(
                self.config,
                "human_ai_intimacy_v1",
                os.path.join(link_parent, "run"),
            )

    def test_preview_cleanup_failure_has_stable_error(self):
        with mock.patch.object(
            KnowledgeStore,
            "taxonomy_projection",
            side_effect=ValueError("forced post-create failure"),
        ), mock.patch("core.taxonomy_migration.shutil.rmtree", return_value=None):
            with self.assertRaisesRegex(MigrationError, "cleanup_failed"):
                preview_migration(
                    self.config,
                    "human_ai_intimacy_v1",
                    self.run_dir,
                )
        self.assertTrue(os.path.exists(self.run_dir))

    def test_preview_reports_missing_inputs_without_leaking_raw_os_error(self):
        missing_config = dict(self.config)
        missing_config["monitor_knowledge_db"] = os.path.join(
            self.tmp.name,
            "missing.db",
        )

        with self.assertRaisesRegex(MigrationError, "invalid_inputs"):
            preview_migration(
                missing_config,
                "human_ai_intimacy_v1",
                self.run_dir,
            )
        self.assertFalse(os.path.exists(self.run_dir))

    def test_preview_wraps_missing_generated_parent_without_leaking_path(self):
        missing_parent = os.path.dirname(self.source_note)
        shutil.rmtree(missing_parent)

        with self.assertRaises(MigrationError) as caught:
            preview_migration(
                self.config,
                "human_ai_intimacy_v1",
                self.run_dir,
            )

        self.assertEqual(caught.exception.code, "source_state_invalid")
        self.assertNotIn(missing_parent, str(caught.exception))
        self.assertNotIn(self.source_note, str(caught.exception))
        self.assertFalse(os.path.exists(self.run_dir))

    def test_load_refuses_modified_manifest(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        manifest_path = os.path.join(self.run_dir, "manifest.json")
        with open(manifest_path, "ab") as handle:
            handle.write(b" ")

        with self.assertRaisesRegex(MigrationError, "manifest_not_canonical"):
            load_sealed_run(self.run_dir)

    def test_load_refuses_modified_payload(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        manifest, _manifest_sha, _state = load_sealed_run(self.run_dir)
        payload_path = os.path.join(
            self.run_dir,
            manifest["files"][0]["payload_relative_path"],
        )
        with open(payload_path, "ab") as handle:
            handle.write(b"drift")

        with self.assertRaisesRegex(MigrationError, "payload_hash_mismatch"):
            load_sealed_run(self.run_dir)

    def test_load_refuses_authenticated_unsafe_projection_paths(self):
        unsafe_paths = (
            ("before", "关注推送/../private.md"),
            ("after", "/private/escaped.md"),
        )
        for side, unsafe_path in unsafe_paths:
            with self.subTest(side=side):
                self.reset_sealed_run()
                preview_migration(
                    self.config, "human_ai_intimacy_v1", self.run_dir
                )
                self.reseal_manifest(
                    lambda manifest, side=side, unsafe_path=unsafe_path:
                    manifest["projection"]["topic_changes"][0][side].__setitem__(
                        "obsidian_path", unsafe_path
                    )
                )
                with self.assertRaisesRegex(
                    MigrationError, "manifest_schema_invalid"
                ):
                    load_sealed_run(self.run_dir)

    def test_load_refuses_topic_file_projection_path_mismatch(self):
        for field, projection_side in (
            ("source_relative_path", "before"),
            ("destination_relative_path", "after"),
        ):
            with self.subTest(field=field):
                self.reset_sealed_run()
                preview_migration(
                    self.config, "human_ai_intimacy_v1", self.run_dir
                )

                def mismatch(manifest):
                    topic_file = next(
                        record for record in manifest["files"]
                        if record["kind"] == "topic"
                    )
                    expected = manifest["projection"]["topic_changes"][0][
                        projection_side
                    ]["obsidian_path"]
                    topic_file[field] = (
                        expected[:-3] + "txt" if expected.endswith(".md")
                        else expected + ".mismatch"
                    )

                self.reseal_manifest(mismatch)
                with self.assertRaisesRegex(
                    MigrationError, "manifest_schema_invalid"
                ):
                    load_sealed_run(self.run_dir)

    def test_load_refuses_duplicate_source_relative_paths(self):
        related_topic_id = self.add_relation_dependent_topic()
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)

        def duplicate_source(manifest):
            changed = next(
                record for record in manifest["files"]
                if record["kind"] == "topic" and record["topic_id"] != related_topic_id
            )
            related = next(
                record for record in manifest["files"]
                if record.get("topic_id") == related_topic_id
            )
            related["source_relative_path"] = changed["source_relative_path"]
            related["destination_relative_path"] = changed["source_relative_path"]

        self.reseal_manifest(duplicate_source)
        with self.assertRaisesRegex(MigrationError, "manifest_schema_invalid"):
            load_sealed_run(self.run_dir)

    def test_preview_refuses_hardlinked_source_identity(self):
        related_topic_id = self.add_relation_dependent_topic()
        conn = sqlite3.connect(self.db_path)
        try:
            relative_path = conn.execute(
                "SELECT obsidian_path FROM topics WHERE topic_id = ?",
                (related_topic_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        related_path = os.path.join(self.vault_root, relative_path)
        os.unlink(related_path)
        os.link(self.source_note, related_path)

        with self.assertRaisesRegex(MigrationError, "duplicate_source_identity"):
            preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)

    def test_apply_refuses_identical_bytes_from_replaced_source_inode_before_database(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        manifest = load_sealed_run(self.run_dir)[0]
        record = manifest["files"][0]
        source = os.path.join(self.vault_root, record["source_relative_path"])
        replacement = source + ".replacement"
        with open(source, "rb") as handle:
            original_bytes = handle.read()
        with open(replacement, "wb") as handle:
            handle.write(original_bytes)
        os.chmod(replacement, record["before_mode"])
        os.replace(replacement, source)
        before_db = self.database_rows()

        self.assertEqual(status_migration(self.config, self.run_dir)["state"], "drifted")
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with self.assertRaisesRegex(MigrationError, "file_drift"):
            apply_migration(
                self.config, self.run_dir, report["manifest_sha256"], token
            )
        self.assertEqual(self.database_rows(), before_db)

    def test_load_refuses_duplicate_sealed_source_identity(self):
        related_topic_id = self.add_relation_dependent_topic()
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)

        def duplicate_identity(manifest):
            changed = next(
                record for record in manifest["files"]
                if record["kind"] == "topic" and record["topic_id"] != related_topic_id
            )
            related = next(
                record for record in manifest["files"]
                if record.get("topic_id") == related_topic_id
            )
            related["source_device"] = changed["source_device"]
            related["source_inode"] = changed["source_inode"]

        self.reseal_manifest(duplicate_identity)
        with self.assertRaisesRegex(MigrationError, "manifest_schema_invalid"):
            load_sealed_run(self.run_dir)

    def test_load_refuses_relation_dependent_topic_move(self):
        related_topic_id = self.add_relation_dependent_topic()
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)

        def move_unchanged_topic(manifest):
            topic_file = next(
                record for record in manifest["files"]
                if record.get("topic_id") == related_topic_id
            )
            topic_file["destination_relative_path"] = (
                topic_file["source_relative_path"][:-3] + "-moved.md"
            )

        self.reseal_manifest(move_unchanged_topic)
        with self.assertRaisesRegex(MigrationError, "manifest_schema_invalid"):
            load_sealed_run(self.run_dir)

    def test_load_refuses_changed_topic_without_render_relationship(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)

        def remove_changed_topic_relationship(manifest):
            changed_topic_id = manifest["projection"]["topic_changes"][0][
                "topic_id"
            ]
            manifest["projection"]["render_topic_ids"].remove(changed_topic_id)
            manifest["database_records"] = [
                record for record in manifest["database_records"]
                if not (
                    record["kind"] == "topic_render"
                    and record["topic_id"] == changed_topic_id
                )
            ]
            removed = next(
                record for record in manifest["files"]
                if record.get("topic_id") == changed_topic_id
            )
            os.unlink(os.path.join(self.run_dir, removed["payload_relative_path"]))
            remaining = [
                record for record in manifest["files"]
                if record is not removed
            ]
            for sequence, record in enumerate(remaining, 1):
                old_payload = os.path.join(
                    self.run_dir, record["payload_relative_path"]
                )
                record["id"] = f"file-{sequence:06d}"
                record["payload_relative_path"] = (
                    f"payload/{record['id']}.md"
                )
                new_payload = os.path.join(
                    self.run_dir, record["payload_relative_path"]
                )
                if old_payload != new_payload:
                    os.replace(old_payload, new_payload)
            manifest["files"] = remaining

        self.reseal_manifest(remove_changed_topic_relationship)
        with self.assertRaisesRegex(MigrationError, "manifest_schema_invalid"):
            load_sealed_run(self.run_dir)

    def test_preview_links_file_payload_evidence_to_semantic_after_records(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        manifest = load_sealed_run(self.run_dir)[0]
        semantic_by_topic = {
            record["topic_id"]: record
            for record in manifest["database_records"]
            if record["kind"] == "topic_render"
        }
        semantic_by_path = {
            record["relative_path"]: record
            for record in manifest["database_records"]
            if record["kind"] == "managed_date_index"
        }
        for record in manifest["files"]:
            semantic = (
                semantic_by_topic[record["topic_id"]]
                if record["kind"] == "topic"
                else semantic_by_path[record["source_relative_path"]]
            )
            self.assertEqual(record["payload_sha256"], semantic["after_sha256"])
            self.assertEqual(record["payload_size"], semantic["after_size"])

    def test_load_refuses_payload_semantic_evidence_mismatch(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)

        def mismatch_topic_size(manifest):
            topic_file = next(
                record for record in manifest["files"]
                if record["kind"] == "topic"
            )
            semantic = next(
                record for record in manifest["database_records"]
                if record["kind"] == "topic_render"
                and record["topic_id"] == topic_file["topic_id"]
            )
            semantic["after_size"] += 1

        self.reseal_manifest(mismatch_topic_size)
        with self.assertRaisesRegex(MigrationError, "manifest_schema_invalid"):
            load_sealed_run(self.run_dir)

    def test_load_authenticates_private_modes_for_all_sealed_artifacts(self):
        cases = (
            ("run", lambda manifest: self.run_dir, 0o755),
            ("payload_dir", lambda manifest: os.path.join(self.run_dir, "payload"), 0o755),
            ("manifest", lambda manifest: os.path.join(self.run_dir, "manifest.json"), 0o644),
            ("state", lambda manifest: os.path.join(self.run_dir, "state.json"), 0o644),
            (
                "payload",
                lambda manifest: os.path.join(
                    self.run_dir, manifest["files"][0]["payload_relative_path"]
                ),
                0o644,
            ),
        )
        for name, target, mode in cases:
            with self.subTest(name=name):
                self.reset_sealed_run()
                preview_migration(
                    self.config, "human_ai_intimacy_v1", self.run_dir
                )
                manifest = load_sealed_run(self.run_dir)[0]
                os.chmod(target(manifest), mode)
                with self.assertRaisesRegex(
                    MigrationError, "privacy_mode_invalid"
                ):
                    load_sealed_run(self.run_dir)

    def test_load_validates_complete_manifest_schema_without_builtin_errors(self):
        malformed_cases = (
            lambda manifest: manifest.pop("projection"),
            lambda manifest: manifest.__setitem__("files", "private"),
            lambda manifest: manifest["files"][0].pop("kind"),
            lambda manifest: manifest["database_records"][0].__setitem__("topic_id", []),
            lambda manifest: manifest["database_records"][0].__setitem__("id", "wrong"),
            lambda manifest: manifest["files"].append(dict(manifest["files"][0])),
            lambda manifest: manifest["files"][0].__setitem__(
                "payload_relative_path", "payload/unlisted.md"
            ),
        )
        for index, mutate in enumerate(malformed_cases):
            with self.subTest(index=index):
                if os.path.exists(self.run_dir):
                    import shutil
                    shutil.rmtree(self.run_dir)
                preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
                self.reseal_manifest(mutate)
                with self.assertRaises(MigrationError) as caught:
                    load_sealed_run(self.run_dir)
                self.assertIn(
                    caught.exception.code,
                    {"manifest_schema_invalid", "payload_inventory_invalid"},
                )

    def test_load_refuses_unexpected_payload_inventory(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        with open(os.path.join(self.run_dir, "payload", "extra.md"), "wb") as handle:
            handle.write(b"extra")
        with self.assertRaisesRegex(MigrationError, "payload_inventory_invalid"):
            load_sealed_run(self.run_dir)

    def test_load_refuses_state_only_tamper_and_unknown_task2_state(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        _manifest, manifest_sha, state = load_sealed_run(self.run_dir)
        self.write_state({**state, "state": "applied", "manifest_sha256": manifest_sha})
        with self.assertRaisesRegex(MigrationError, "state_schema_invalid"):
            load_sealed_run(self.run_dir)

    def test_load_refuses_state_extra_fields(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        _manifest, _manifest_sha, state = load_sealed_run(self.run_dir)
        self.write_state({**state, "private": "tamper"})
        with self.assertRaisesRegex(MigrationError, "state_schema_invalid"):
            load_sealed_run(self.run_dir)

    def test_status_reports_pending_without_private_fields(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)

        report = status_migration(self.config, self.run_dir)

        self.assertEqual(report["state"], "planned")
        self.assertGreater(report["pending"], 0)
        self.assertNotIn("title", report)
        self.assertNotIn("relative_path", report)
        self.assertNotIn("files", report)
        self.assertGreater(report["database_pending"], 0)
        self.assertEqual(
            report["database_total"],
            sum(report[f"database_{name}"] for name in (
                "pending", "applied", "already_clean", "drifted"
            )),
        )

    def test_status_reports_applied_and_is_repeatable(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        self.apply_manifest_post_state()
        first = status_migration(self.config, self.run_dir)
        second = status_migration(self.config, self.run_dir)
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "applied")
        self.assertEqual(first["pending"], 0)
        self.assertEqual(first["database_pending"], 0)
        self.assertGreater(first["applied"] + first["already_clean"], 0)
        self.assertGreater(
            first["database_applied"] + first["database_already_clean"], 0
        )

    def test_status_reports_mixed_database_and_file_state(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        self.apply_manifest_post_state(database=True, files=False)
        report = status_migration(self.config, self.run_dir)
        self.assertEqual(report["state"], "mixed")
        self.assertGreater(report["pending"], 0)
        self.assertGreater(report["database_applied"], 0)

    def test_status_reports_mixed_file_and_database_state(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        self.apply_manifest_post_state(database=False, files=True)
        report = status_migration(self.config, self.run_dir)
        self.assertEqual(report["state"], "mixed")
        self.assertGreater(report["applied"], 0)
        self.assertGreater(report["database_pending"], 0)

    def test_status_reports_already_clean_for_noop_records(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        self.reseal_manifest(
            lambda manifest: manifest["database_records"][0].__setitem__(
                "after_sha256",
                manifest["database_records"][0]["before_sha256"],
            )
        )
        report = status_migration(self.config, self.run_dir)
        self.assertGreaterEqual(report["already_clean"], 0)
        self.assertGreater(report["database_already_clean"], 0)

    def test_status_detects_source_drift_without_printing_private_fields(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        with open(self.source_note, "a", encoding="utf-8") as handle:
            handle.write("drift")

        report = status_migration(self.config, self.run_dir)

        self.assertEqual(report["state"], "drifted")
        self.assertEqual(report["drifted"], 1)
        self.assertNotIn("title", report)
        self.assertNotIn("relative_path", report)

    def test_status_detects_db_only_topic_render_dependency_drift(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE topics SET summary = 'db only drift' WHERE topic_id = 1")
            conn.commit()
        finally:
            conn.close()
        report = status_migration(self.config, self.run_dir)
        self.assertEqual(report["state"], "drifted")
        self.assertEqual(report["database_drifted"], 1)

    def test_status_ignores_unrelated_database_content(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE topics SET topic_key = 'not-rendered' WHERE topic_id = 1")
            conn.commit()
        finally:
            conn.close()
        report = status_migration(self.config, self.run_dir)
        self.assertNotEqual(report["state"], "drifted")
        self.assertEqual(report["database_drifted"], 0)

    def test_status_detects_relation_dependent_render_drift(self):
        source = self.store.apply_event(
            {
                "title": "Relation source", "summary": "source",
                "topic_key": "relation-source", "category": "待归类",
                "entities": [], "key_facts": [], "links": [],
                "event_type": "discussion", "status_hint": "active",
            },
            [{"timestamp": 2, "time_str": "2026-07-13 11:00", "sender": "F", "text": "s"}],
            {"monitor_chat_username": "other@chatroom", "monitor_chat_display_name": "Other", **self.config},
            {"relation": "new"},
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO relations(source_topic_id,target_topic_id,relation,reason,created_at) VALUES(?,?,?,?,?)",
                (source["topic_id"], 1, "related", "test", 1),
            )
            conn.commit()
        finally:
            conn.close()
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE relations SET relation = 'contradicts' WHERE source_topic_id = ?",
                (source["topic_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        report = status_migration(self.config, self.run_dir)
        self.assertEqual(report["state"], "drifted")
        self.assertEqual(report["database_drifted"], 1)

    def test_status_detects_managed_index_database_drift(self):
        unaffected = self.store.apply_event(
            {
                "title": "Unassigned index topic", "summary": "index source",
                "topic_key": "unassigned-index-topic", "category": "待归类",
                "entities": [], "key_facts": [], "links": [],
                "event_type": "discussion", "status_hint": "active",
            },
            [{"timestamp": 3, "time_str": "2026-07-13 12:00", "sender": "F", "text": "i"}],
            {"monitor_chat_username": "other@chatroom", "monitor_chat_display_name": "Other", **self.config},
            {"relation": "new"},
        )
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        manifest = load_sealed_run(self.run_dir)[0]
        self.assertNotIn(
            unaffected["topic_id"],
            manifest["projection"]["render_topic_ids"],
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE topics SET title = 'index-only DB drift' WHERE topic_id = ?",
                (unaffected["topic_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        report = status_migration(self.config, self.run_dir)
        kinds = {record["kind"] for record in load_sealed_run(self.run_dir)[0]["database_records"]}
        self.assertIn("managed_date_index", kinds)
        self.assertEqual(report["state"], "drifted")
        self.assertEqual(report["database_drifted"], 1)

    def test_status_rejects_migration_relevant_config_drift(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)
        drifted_config = dict(self.config)
        drifted_config["monitor_chat_aliases"] = {"room@chatroom": "别名变化"}

        with self.assertRaisesRegex(MigrationError, "config_drift"):
            status_migration(drifted_config, self.run_dir)

    def test_status_keeps_never_applied_before_state_planned(self):
        preview_migration(self.config, "human_ai_intimacy_v1", self.run_dir)

        report = status_migration(self.config, self.run_dir)

        self.assertEqual(load_sealed_run(self.run_dir)[2]["state"], "planned")
        self.assertEqual(report["state"], "planned")
        self.assertGreater(report["pending"] + report["database_pending"], 0)

    def test_wrong_confirmation_refuses_before_backup_or_mutation(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        before_db = self.database_rows()
        before_files = self.tree_digests(self.vault_root)
        with self.assertRaisesRegex(MigrationError, "confirmation_mismatch"):
            apply_migration(
                self.config, self.run_dir, report["manifest_sha256"], "wrong"
            )
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "backups")))
        self.assertEqual(self.database_rows(), before_db)
        self.assertEqual(self.tree_digests(self.vault_root), before_files)

    def test_non_full_manifest_hash_refuses_before_backup(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        short_sha = report["manifest_sha256"][:12]
        with self.assertRaisesRegex(MigrationError, "manifest_hash_mismatch"):
            apply_migration(
                self.config,
                self.run_dir,
                short_sha,
                f"APPLY_TAXONOMY_MIGRATION:{short_sha}",
            )
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "backups")))

    @DARWIN_ONLY
    def test_apply_backs_up_and_changes_only_manifest_records(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        before = status_migration(self.config, self.run_dir)
        before_unrelated = self.digest(self.unrelated_note)
        result = apply_migration(
            self.config,
            self.run_dir,
            report["manifest_sha256"],
            f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}",
        )
        self.assertEqual(result["state"], "applied")
        self.assertGreater(result["applied_this_invocation"], 0)
        self.assertEqual(
            result["already_clean"],
            before["applied"] + before["already_clean"],
        )
        self.assertEqual(self.digest(self.unrelated_note), before_unrelated)
        backup_db = os.path.join(self.run_dir, "backups", "knowledge.db")
        self.assertTrue(os.path.exists(backup_db))
        self.assertEqual(oct(os.stat(backup_db).st_mode & 0o777), "0o600")
        self.assertEqual(
            oct(os.stat(os.path.dirname(backup_db)).st_mode & 0o777), "0o700"
        )
        manifest = load_sealed_run(self.run_dir)[0]
        for record in manifest["files"]:
            backup = os.path.join(
                self.run_dir, "backups", "files", record["source_relative_path"]
            )
            self.assertEqual(self.digest(backup), record["before_sha256"])
            self.assertEqual(oct(os.stat(backup).st_mode & 0o777), "0o600")

    @DARWIN_ONLY
    def test_second_apply_is_idempotent(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(self.config, self.run_dir, report["manifest_sha256"], token)
        second = apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], token
        )
        self.assertEqual(second["applied_this_invocation"], 0)
        self.assertEqual(second["already_clean"], second["file_count"])

    @DARWIN_ONLY
    def test_load_refuses_invalid_apply_ledger_relationships(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(self.config, self.run_dir, report["manifest_sha256"], token)
        with open(os.path.join(self.run_dir, "state.json"), encoding="utf-8") as handle:
            state = json.load(handle)
        state["applied_file_ids"] = ["foreign-record"]
        self.write_state(state)
        with self.assertRaisesRegex(MigrationError, "state_schema_invalid"):
            load_sealed_run(self.run_dir)

    def test_apply_refuses_assignment_config_drift(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        changed_config = dict(self.config)
        changed_config["monitor_chat_taxonomy_profiles"] = {}
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with self.assertRaisesRegex(MigrationError, "config_drift"):
            apply_migration(
                changed_config, self.run_dir, report["manifest_sha256"], token
            )
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "backups")))

    def test_apply_refuses_database_drift_before_backup(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE topics SET summary = 'post preview drift'")
            conn.commit()
        finally:
            conn.close()
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with self.assertRaisesRegex(MigrationError, "database_drift"):
            apply_migration(
                self.config, self.run_dir, report["manifest_sha256"], token
            )
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "backups")))

    @DARWIN_ONLY
    def test_apply_refuses_unrelated_database_write_after_backup_without_taxonomy_mutation(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        manifest = load_sealed_run(self.run_dir)[0]
        topic_id = manifest["projection"]["topic_changes"][0]["topic_id"]
        original = taxonomy_migration._ensure_verified_backups

        def backup_then_unrelated_write(*args, **kwargs):
            state = original(*args, **kwargs)
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "UPDATE topics SET topic_key = 'between-backup-and-apply' WHERE topic_id = ?",
                    (topic_id,),
                )
                conn.commit()
            finally:
                conn.close()
            return state

        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with mock.patch(
            "core.taxonomy_migration._ensure_verified_backups",
            side_effect=backup_then_unrelated_write,
        ):
            with self.assertRaisesRegex(MigrationError, "database_drift"):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT category, taxonomy_profile, taxonomy_version, topic_key "
                "FROM topics WHERE topic_id = ?",
                (topic_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[:3], ("AI伴侣交互", "", 0))
        self.assertEqual(row[3], "between-backup-and-apply")

    @DARWIN_ONLY
    def test_backup_creation_resumes_after_partial_staging_crash(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        original = taxonomy_migration._create_sqlite_backup

        def crash_after_database(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("crash during staging")

        with mock.patch(
            "core.taxonomy_migration._create_sqlite_backup",
            side_effect=crash_after_database,
        ):
            with self.assertRaisesRegex(RuntimeError, "crash during staging"):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        self.assertTrue(os.path.isdir(os.path.join(self.run_dir, ".backups.staging")))
        self.assertFalse(os.path.lexists(os.path.join(self.run_dir, "backups")))
        resumed = apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], token
        )
        self.assertEqual(resumed["state"], "applied")
        self.assertFalse(os.path.lexists(os.path.join(self.run_dir, ".backups.staging")))

    @DARWIN_ONLY
    def test_backup_creation_resumes_after_publish_before_state_crash(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        original = taxonomy_migration._write_state
        crashed = False

        def crash_before_backup_state(path, state):
            nonlocal crashed
            if state["state"] == "backups_verified" and not crashed:
                crashed = True
                raise RuntimeError("crash before backup ledger")
            return original(path, state)

        with mock.patch(
            "core.taxonomy_migration._write_state", side_effect=crash_before_backup_state
        ):
            with self.assertRaisesRegex(RuntimeError, "crash before backup ledger"):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        self.assertTrue(os.path.isdir(os.path.join(self.run_dir, "backups")))
        resumed = apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], token
        )
        self.assertEqual(resumed["state"], "applied")

    @DARWIN_ONLY
    def test_resume_refuses_file_drift_before_database_mutation(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        before_database = self.database_rows()

        with mock.patch(
            "core.taxonomy_migration._apply_database",
            side_effect=RuntimeError("crash after backup verification"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "crash after backup verification"
            ):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )

        self.assertEqual(load_sealed_run(self.run_dir)[2]["state"], "backups_verified")
        with open(self.source_note, "a", encoding="utf-8") as handle:
            handle.write("operator edit after backup\n")

        with self.assertRaisesRegex(MigrationError, "file_drift"):
            apply_migration(
                self.config, self.run_dir, report["manifest_sha256"], token
            )
        self.assertEqual(self.database_rows(), before_database)

    def test_backup_source_read_stays_on_open_parent_after_visible_parent_swap(self):
        preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        manifest, _manifest_sha, state = load_sealed_run(self.run_dir)
        record = next(
            item for item in reversed(manifest["files"])
            if os.path.dirname(item["source_relative_path"])
            != manifest["inputs"]["obsidian_subdir"]
        )
        source = os.path.join(self.vault_root, record["source_relative_path"])
        parent = os.path.dirname(source)
        held_parent = parent + "-held-for-backup"
        outside = os.path.join(self.tmp.name, "outside-parent")
        os.mkdir(outside)
        outside_bytes = b"outside bytes must never enter backup staging\n"
        outside_source = os.path.join(outside, os.path.basename(source))
        with open(source, "rb") as handle:
            original_bytes = handle.read()
        with open(outside_source, "wb") as handle:
            handle.write(outside_bytes)
        parent_identity = (os.stat(parent).st_dev, os.stat(parent).st_ino)
        original_open = os.open
        swapped = False

        def swap_visible_parent_before_leaf_open(path, flags, *args, **kwargs):
            nonlocal swapped
            dir_fd = kwargs.get("dir_fd")
            if (
                not swapped
                and path == os.path.basename(source)
                and dir_fd is not None
                and (os.fstat(dir_fd).st_dev, os.fstat(dir_fd).st_ino)
                == parent_identity
            ):
                swapped = True
                os.replace(parent, held_parent)
                os.symlink(outside, parent)
            return original_open(path, flags, *args, **kwargs)

        with mock.patch(
            "core.taxonomy_migration.os.open",
            side_effect=swap_visible_parent_before_leaf_open,
        ):
            taxonomy_migration._ensure_verified_backups(
                taxonomy_migration.Path(self.run_dir), manifest, state
            )

        backup = os.path.join(
            self.run_dir, "backups", "files", record["source_relative_path"]
        )
        with open(backup, "rb") as handle:
            staged_bytes = handle.read()
        self.assertTrue(swapped)
        self.assertEqual(staged_bytes, original_bytes)
        self.assertNotEqual(staged_bytes, outside_bytes)

    def test_status_and_apply_refuse_managed_file_mode_drift(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        original_mode = stat.S_IMODE(os.stat(self.source_note).st_mode)
        changed_mode = 0o600 if original_mode != 0o600 else 0o644
        os.chmod(self.source_note, changed_mode)
        self.assertEqual(status_migration(self.config, self.run_dir)["drifted"], 1)
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with self.assertRaisesRegex(MigrationError, "file_drift"):
            apply_migration(
                self.config, self.run_dir, report["manifest_sha256"], token
            )

    @DARWIN_ONLY
    def test_backup_inventory_rejects_unexpected_directory_and_symlink(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(self.config, self.run_dir, report["manifest_sha256"], token)
        unexpected = os.path.join(self.run_dir, "backups", "files", "unexpected")
        os.mkdir(unexpected, 0o700)
        with self.assertRaisesRegex(MigrationError, "backup_invalid"):
            apply_migration(self.config, self.run_dir, report["manifest_sha256"], token)
        os.rmdir(unexpected)
        os.symlink("knowledge.db", os.path.join(self.run_dir, "backups", "extra-link"))
        with self.assertRaisesRegex(MigrationError, "backup_invalid"):
            apply_migration(self.config, self.run_dir, report["manifest_sha256"], token)
        os.unlink(os.path.join(self.run_dir, "backups", "extra-link"))
        manifest = load_sealed_run(self.run_dir)[0]
        missing = os.path.join(
            self.run_dir,
            "backups",
            "files",
            manifest["files"][0]["source_relative_path"],
        )
        os.unlink(missing)
        with self.assertRaisesRegex(MigrationError, "backup_invalid"):
            apply_migration(self.config, self.run_dir, report["manifest_sha256"], token)

    def test_apply_refuses_exact_partial_state_before_backup(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        self.apply_manifest_post_state(database=False, files=True)
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with self.assertRaisesRegex(MigrationError, "pre_apply_state_invalid"):
            apply_migration(
                self.config, self.run_dir, report["manifest_sha256"], token
            )
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "backups")))

    @DARWIN_ONLY
    def test_resume_after_injected_partial_write(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with mock.patch(
            "core.taxonomy_migration._apply_file_record",
            side_effect=[None, RuntimeError("boom")],
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        before_resume = status_migration(self.config, self.run_dir)
        resumed = apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], token
        )
        self.assertEqual(resumed["state"], "applied")
        self.assertEqual(
            resumed["already_clean"],
            before_resume["applied"] + before_resume["already_clean"],
        )

    @DARWIN_ONLY
    def test_resume_after_destination_write_before_source_remove(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with mock.patch(
            "core.taxonomy_migration._unlink_generated",
            side_effect=RuntimeError("interrupt before source remove"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupt"):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        resumed = apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], token
        )
        self.assertEqual(resumed["state"], "applied")

    @DARWIN_ONLY
    def test_apply_resumes_preledger_staging_leaf_for_create(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = self.crash_apply_before_first_file_operation(report)
        manifest = load_sealed_run(self.run_dir)[0]
        record = next(
            item for item in manifest["files"]
            if item["source_relative_path"] != item["destination_relative_path"]
        )
        self.create_preledger_staging_leaf(record)

        self.assertNotEqual(
            status_migration(self.config, self.run_dir)["state"], "drifted"
        )
        resumed = apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], token
        )
        self.assertEqual(resumed["state"], "applied")
        self.assertEqual(self.vault_operation_artifacts(), [])

    @DARWIN_ONLY
    def test_apply_resumes_preledger_staging_leaf_for_replacement(self):
        self.add_relation_dependent_topic()
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = self.crash_apply_before_first_file_operation(report)
        manifest = load_sealed_run(self.run_dir)[0]
        record = next(
            item for item in manifest["files"]
            if item["source_relative_path"] == item["destination_relative_path"]
            and item["before_sha256"] != item["payload_sha256"]
        )
        self.create_preledger_staging_leaf(record)

        self.assertNotEqual(
            status_migration(self.config, self.run_dir)["state"], "drifted"
        )
        resumed = apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], token
        )
        self.assertEqual(resumed["state"], "applied")
        self.assertEqual(self.vault_operation_artifacts(), [])

    @DARWIN_ONLY
    def test_rollback_resumes_preledger_staging_leaf_for_create(self):
        before_db = self.database_rows()
        before_files = self.tree_digests(self.vault_root)
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        self.crash_apply_before_first_file_operation(report)
        manifest = load_sealed_run(self.run_dir)[0]
        record = next(
            item for item in manifest["files"]
            if item["source_relative_path"] != item["destination_relative_path"]
        )
        self.create_preledger_staging_leaf(record)
        rollback_token = (
            f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        )

        result = rollback_migration(
            self.run_dir, report["manifest_sha256"], rollback_token
        )

        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(self.database_rows(), before_db)
        self.assertEqual(self.tree_digests(self.vault_root), before_files)
        self.assertEqual(self.vault_operation_artifacts(), [])
        resumed = rollback_migration(
            self.run_dir, report["manifest_sha256"], rollback_token
        )
        self.assertEqual(resumed["state"], "rolled_back")
        self.assertEqual(resumed["restored_this_invocation"], 0)

    @DARWIN_ONLY
    def test_rollback_resumes_preledger_staging_leaf_for_replacement(self):
        self.add_relation_dependent_topic()
        before_db = self.database_rows()
        before_files = self.tree_digests(self.vault_root)
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        self.crash_apply_before_first_file_operation(report)
        manifest = load_sealed_run(self.run_dir)[0]
        record = next(
            item for item in manifest["files"]
            if item["source_relative_path"] == item["destination_relative_path"]
            and item["before_sha256"] != item["payload_sha256"]
        )
        self.create_preledger_staging_leaf(record)
        rollback_token = (
            f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        )

        result = rollback_migration(
            self.run_dir, report["manifest_sha256"], rollback_token
        )

        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(self.database_rows(), before_db)
        self.assertEqual(self.tree_digests(self.vault_root), before_files)
        self.assertEqual(self.vault_operation_artifacts(), [])
        resumed = rollback_migration(
            self.run_dir, report["manifest_sha256"], rollback_token
        )
        self.assertEqual(resumed["state"], "rolled_back")
        self.assertEqual(resumed["restored_this_invocation"], 0)

    @DARWIN_ONLY
    def test_rollback_preflights_all_files_before_resuming_preledger_staging(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        self.crash_apply_before_first_file_operation(report)
        manifest = load_sealed_run(self.run_dir)[0]
        recoverable = manifest["files"][0]
        drifted = manifest["files"][-1]
        self.assertNotEqual(
            recoverable["source_relative_path"],
            recoverable["destination_relative_path"],
        )
        self.create_preledger_staging_leaf(recoverable)
        drifted_path = os.path.join(
            self.vault_root, drifted["source_relative_path"]
        )
        with open(drifted_path, "a", encoding="utf-8") as handle:
            handle.write("later managed file drift\n")
        before_rollback = self.rollback_mutation_snapshot()
        rollback_token = (
            f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        )

        with self.assertRaisesRegex(MigrationError, "post_apply_drift"):
            rollback_migration(
                self.run_dir, report["manifest_sha256"], rollback_token
            )

        self.assertEqual(self.rollback_mutation_snapshot(), before_rollback)

    @DARWIN_ONLY
    def test_rollback_preflights_database_before_resuming_preledger_staging(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        self.crash_apply_before_first_file_operation(report)
        manifest = load_sealed_run(self.run_dir)[0]
        recoverable = manifest["files"][0]
        self.create_preledger_staging_leaf(recoverable)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE topics SET topic_key = 'database drift before rollback'"
            )
            conn.commit()
        finally:
            conn.close()
        before_rollback = self.rollback_mutation_snapshot()
        rollback_token = (
            f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        )

        with self.assertRaisesRegex(MigrationError, "post_apply_drift"):
            rollback_migration(
                self.run_dir, report["manifest_sha256"], rollback_token
            )

        self.assertEqual(self.rollback_mutation_snapshot(), before_rollback)

    @DARWIN_ONLY
    def test_rollback_refuses_preledger_staging_in_backups_verified_state(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        with mock.patch(
            "core.taxonomy_migration._apply_database",
            side_effect=RuntimeError("crash after backup verification"),
        ):
            with self.assertRaisesRegex(RuntimeError, "backup verification"):
                apply_migration(
                    self.config,
                    self.run_dir,
                    report["manifest_sha256"],
                    f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}",
                )
        manifest, _manifest_sha, state = load_sealed_run(self.run_dir)
        self.assertEqual(state["state"], "backups_verified")
        self.assertFalse(state["database_applied"])
        recoverable = manifest["files"][0]
        self.create_preledger_staging_leaf(recoverable)
        before_rollback = self.rollback_mutation_snapshot()
        rollback_token = (
            f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        )

        with self.assertRaisesRegex(MigrationError, "post_apply_drift"):
            rollback_migration(
                self.run_dir, report["manifest_sha256"], rollback_token
            )

        self.assertEqual(self.rollback_mutation_snapshot(), before_rollback)

    @DARWIN_ONLY
    def test_resume_and_rollback_after_crash_immediately_after_apply_swap(self):
        self.add_relation_dependent_topic()
        before_db = self.database_rows()
        before_files = self.tree_digests(self.vault_root)
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"

        def crash(name, _record_id):
            if name == "after_swap_rename":
                raise RuntimeError("crash after swap")

        with mock.patch(
            "core.taxonomy_migration._fault_point", side_effect=crash
        ):
            with self.assertRaisesRegex(RuntimeError, "crash after swap"):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        self.assertIn(
            "apply_destination_prepared",
            load_sealed_run(self.run_dir)[2]["file_operations"].values(),
        )
        self.assertTrue(self.vault_operation_artifacts())
        self.assertNotEqual(
            status_migration(self.config, self.run_dir)["state"], "drifted"
        )

        before_resume = status_migration(self.config, self.run_dir)
        resumed = apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], token
        )
        self.assertEqual(resumed["state"], "applied")
        self.assertEqual(
            resumed["already_clean"],
            before_resume["applied"] + before_resume["already_clean"],
        )
        self.assertEqual(self.vault_operation_artifacts(), [])
        rollback_migration(
            self.run_dir, report["manifest_sha256"], rollback_token
        )
        self.assertEqual(self.database_rows(), before_db)
        self.assertEqual(self.tree_digests(self.vault_root), before_files)
        self.assertEqual(self.vault_operation_artifacts(), [])

    @DARWIN_ONLY
    def test_resume_and_rollback_after_crash_immediately_after_apply_quarantine(self):
        before_db = self.database_rows()
        before_files = self.tree_digests(self.vault_root)
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"

        def crash(name, _record_id):
            if name == "after_quarantine_rename":
                raise RuntimeError("crash after quarantine")

        with mock.patch(
            "core.taxonomy_migration._fault_point", side_effect=crash
        ):
            with self.assertRaisesRegex(RuntimeError, "crash after quarantine"):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        self.assertIn(
            "apply_source_quarantine_prepared",
            load_sealed_run(self.run_dir)[2]["file_operations"].values(),
        )
        self.assertTrue(self.vault_operation_artifacts())
        self.assertNotEqual(
            status_migration(self.config, self.run_dir)["state"], "drifted"
        )

        before_resume = status_migration(self.config, self.run_dir)
        resumed = apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], token
        )
        self.assertEqual(resumed["state"], "applied")
        self.assertEqual(
            resumed["already_clean"],
            before_resume["applied"] + before_resume["already_clean"],
        )
        self.assertEqual(self.vault_operation_artifacts(), [])
        rollback_migration(
            self.run_dir, report["manifest_sha256"], rollback_token
        )
        self.assertEqual(self.database_rows(), before_db)
        self.assertEqual(self.tree_digests(self.vault_root), before_files)
        self.assertEqual(self.vault_operation_artifacts(), [])

    @DARWIN_ONLY
    def test_status_and_resume_refuse_unauthenticated_crash_quarantine(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"

        def crash(name, _record_id):
            if name == "after_quarantine_rename":
                raise RuntimeError("crash after quarantine")

        with mock.patch(
            "core.taxonomy_migration._fault_point", side_effect=crash
        ):
            with self.assertRaisesRegex(RuntimeError, "crash after quarantine"):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        artifact = self.vault_operation_artifacts()[0]
        with open(os.path.join(self.vault_root, artifact), "wb") as handle:
            handle.write(b"unauthenticated quarantine bytes\n")

        self.assertEqual(
            status_migration(self.config, self.run_dir)["state"], "drifted"
        )
        with self.assertRaisesRegex(MigrationError, "file_drift"):
            apply_migration(
                self.config, self.run_dir, report["manifest_sha256"], token
            )

    @DARWIN_ONLY
    def test_status_rejects_operation_leaf_in_wrong_manifest_parent(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], token
        )
        manifest = load_sealed_run(self.run_dir)[0]
        moved = next(
            record for record in manifest["files"]
            if record["source_relative_path"] != record["destination_relative_path"]
        )
        wrong_parent_leaf = os.path.join(
            self.vault_root,
            os.path.dirname(moved["source_relative_path"]),
            moved["operation_leaves"]["staging"],
        )
        os.makedirs(os.path.dirname(wrong_parent_leaf), exist_ok=True)
        with open(wrong_parent_leaf, "wb") as handle:
            handle.write(b"unexpected operation artifact\n")

        self.assertEqual(
            status_migration(self.config, self.run_dir)["state"], "drifted"
        )

    @DARWIN_ONLY
    def test_rollback_resumes_after_swap_and_quarantine_crash_boundaries(self):
        self.add_relation_dependent_topic()
        before_db = self.database_rows()
        before_files = self.tree_digests(self.vault_root)
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )

        for boundary in ("after_swap_rename", "after_quarantine_rename"):
            crashed = False

            def crash(name, _record_id):
                nonlocal crashed
                if name == boundary and not crashed:
                    crashed = True
                    raise RuntimeError(f"crash at {boundary}")

            with mock.patch(
                "core.taxonomy_migration._fault_point", side_effect=crash
            ):
                with self.assertRaisesRegex(RuntimeError, "crash at"):
                    rollback_migration(
                        self.run_dir, report["manifest_sha256"], rollback_token
                    )
            self.assertTrue(self.vault_operation_artifacts())
            expected_phase = (
                "rollback_source_prepared"
                if boundary == "after_swap_rename"
                else "rollback_destination_quarantine_prepared"
            )
            self.assertIn(
                expected_phase,
                load_sealed_run(self.run_dir)[2]["file_operations"].values(),
            )
            self.assertNotEqual(
                status_migration(self.config, self.run_dir)["state"], "drifted"
            )
            result = rollback_migration(
                self.run_dir, report["manifest_sha256"], rollback_token
            )
            self.assertEqual(result["state"], "rolled_back")
            self.assertEqual(self.database_rows(), before_db)
            self.assertEqual(self.tree_digests(self.vault_root), before_files)
            self.assertEqual(self.vault_operation_artifacts(), [])
            if boundary != "after_quarantine_rename":
                # Reapply the same logical fixture through a fresh sealed run.
                self.reset_sealed_run()
                report = preview_migration(
                    self.config, "human_ai_intimacy_v1", self.run_dir
                )
                apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
                rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], apply_token
                )

    @DARWIN_ONLY
    def test_rollback_restores_database_and_exact_files(self):
        before_db_rows = self.database_rows()
        before_files = self.tree_digests(self.vault_root)
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        result = rollback_migration(
            self.run_dir, report["manifest_sha256"], rollback_token
        )
        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(self.database_rows(), before_db_rows)
        self.assertEqual(self.tree_digests(self.vault_root), before_files)

    @DARWIN_ONLY
    def test_status_reports_rolled_back_after_exact_restore(self):
        before_db_rows = self.database_rows()
        before_files = self.tree_digests(self.vault_root)
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        rollback_migration(
            self.run_dir, report["manifest_sha256"], rollback_token
        )

        status = status_migration(self.config, self.run_dir)

        self.assertEqual(status["state"], "rolled_back")
        self.assertGreater(status["pending"] + status["database_pending"], 0)
        self.assertEqual(status["drifted"] + status["database_drifted"], 0)
        self.assertEqual(self.database_rows(), before_db_rows)
        self.assertEqual(self.tree_digests(self.vault_root), before_files)

    @DARWIN_ONLY
    def test_rolled_back_ledger_does_not_mask_file_or_database_drift(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        _manifest, _manifest_sha, state = load_sealed_run(self.run_dir)
        self.write_state({
            **state,
            "state": "rolled_back",
            "database_applied": False,
            "applied_file_ids": [],
            "file_operations": {
                record_id: "rolled_back"
                for record_id in state["file_operations"]
            },
        })

        forged_status = status_migration(self.config, self.run_dir)
        self.assertEqual(forged_status["state"], "applied")

        manifest = load_sealed_run(self.run_dir)[0]
        destination = os.path.join(
            self.vault_root, manifest["files"][0]["destination_relative_path"]
        )
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write("post-ledger file drift")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE topics SET summary = 'post-ledger database drift'")
            conn.commit()
        finally:
            conn.close()

        drifted_status = status_migration(self.config, self.run_dir)
        self.assertEqual(drifted_status["state"], "drifted")
        self.assertGreater(drifted_status["drifted"], 0)
        self.assertGreater(drifted_status["database_drifted"], 0)

    @DARWIN_ONLY
    def test_rollback_restores_source_before_removing_moved_destination(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        manifest = load_sealed_run(self.run_dir)[0]
        moved = next(
            record for record in manifest["files"]
            if record["source_relative_path"] != record["destination_relative_path"]
        )
        source = os.path.join(self.vault_root, moved["source_relative_path"])
        destination = os.path.join(self.vault_root, moved["destination_relative_path"])
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        original_unlink = taxonomy_migration._unlink_generated

        def crash_on_destination(inputs, relative_path, **kwargs):
            if relative_path == moved["destination_relative_path"]:
                raise RuntimeError("crash before destination removal")
            return original_unlink(inputs, relative_path, **kwargs)

        with mock.patch(
            "core.taxonomy_migration._unlink_generated", side_effect=crash_on_destination
        ):
            with self.assertRaisesRegex(RuntimeError, "crash before destination removal"):
                rollback_migration(
                    self.run_dir, report["manifest_sha256"], rollback_token
                )
        self.assertTrue(os.path.isfile(source))
        self.assertEqual(self.digest(source), moved["before_sha256"])
        self.assertEqual(stat.S_IMODE(os.stat(source).st_mode), moved["before_mode"])
        self.assertTrue(os.path.isfile(destination))
        resumed = rollback_migration(
            self.run_dir, report["manifest_sha256"], rollback_token
        )
        self.assertEqual(resumed["state"], "rolled_back")
        self.assertFalse(os.path.lexists(destination))

    @DARWIN_ONLY
    def test_rollback_restores_and_verifies_exact_source_mode(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        manifest = load_sealed_run(self.run_dir)[0]
        moved = next(
            record for record in manifest["files"]
            if record["source_relative_path"] != record["destination_relative_path"]
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        rollback_migration(
            self.run_dir, report["manifest_sha256"], rollback_token
        )
        source = os.path.join(self.vault_root, moved["source_relative_path"])
        self.assertEqual(stat.S_IMODE(os.stat(source).st_mode), moved["before_mode"])

    @DARWIN_ONLY
    def test_fd_relative_replace_stays_bound_when_parent_is_swapped_to_symlink(self):
        generated_root = os.path.join(self.vault_root, "关注推送")
        parent = os.path.join(generated_root, "swap-target")
        held_parent = os.path.join(generated_root, "held-parent")
        outside = os.path.join(self.tmp.name, "outside")
        os.mkdir(parent)
        os.mkdir(outside)
        inputs = {
            "obsidian_root": self.vault_root,
            "obsidian_subdir": "关注推送",
            "generated_root": generated_root,
            "knowledge_db": self.db_path,
        }
        original_rename = taxonomy_migration._renameatx_np
        swapped = False

        def swap_then_rename(src_fd, src, dst_fd, dst, flags):
            nonlocal swapped
            if not swapped:
                swapped = True
                os.replace(parent, held_parent)
                os.symlink(outside, parent)
            return original_rename(src_fd, src, dst_fd, dst, flags)

        with mock.patch(
            "core.taxonomy_migration._renameatx_np", side_effect=swap_then_rename
        ):
            taxonomy_migration._atomic_replace_generated(
                inputs,
                "关注推送/swap-target/note.md",
                b"bound",
                0o640,
                expected=None,
                drift_code="file_drift",
            )
        self.assertFalse(os.path.lexists(os.path.join(outside, "note.md")))
        held_file = os.path.join(held_parent, "note.md")
        self.assertEqual(self.digest(held_file), hashlib.sha256(b"bound").hexdigest())
        self.assertEqual(stat.S_IMODE(os.stat(held_file).st_mode), 0o640)

    def test_atomic_leaf_mutation_refuses_unsupported_platform_without_overwrite(self):
        generated_root = os.path.join(self.vault_root, "关注推送")
        os.makedirs(generated_root, exist_ok=True)
        inputs = {
            "obsidian_root": self.vault_root,
            "obsidian_subdir": "关注推送",
            "generated_root": generated_root,
            "knowledge_db": self.db_path,
        }
        destination = os.path.join(generated_root, "unsupported.md")
        with mock.patch("core.taxonomy_migration.sys.platform", "linux"):
            with self.assertRaisesRegex(MigrationError, "atomic_leaf_unsupported"):
                taxonomy_migration._atomic_replace_generated(
                    inputs,
                    "关注推送/unsupported.md",
                    b"payload",
                    0o600,
                    expected=None,
                    drift_code="file_drift",
                )
        self.assertFalse(os.path.lexists(destination))

    def test_apply_refuses_unsupported_platform_before_backup_or_mutation(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        before_db = self.database_rows()
        before_files = self.tree_digests(self.vault_root)
        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with mock.patch("core.taxonomy_migration.sys.platform", "linux"):
            with self.assertRaisesRegex(MigrationError, "atomic_leaf_unsupported"):
                apply_migration(
                    self.config, self.run_dir, report["manifest_sha256"], token
                )
        self.assertEqual(self.database_rows(), before_db)
        self.assertEqual(self.tree_digests(self.vault_root), before_files)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "backups")))

    @DARWIN_ONLY
    def test_rollback_refuses_post_apply_file_drift(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        manifest = load_sealed_run(self.run_dir)[0]
        destination = os.path.join(
            self.vault_root, manifest["files"][0]["destination_relative_path"]
        )
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write("user edit after apply")
        with self.assertRaisesRegex(MigrationError, "post_apply_drift"):
            rollback_migration(
                self.run_dir, report["manifest_sha256"], rollback_token
            )

    @DARWIN_ONLY
    def test_rollback_refuses_post_apply_database_drift(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE topics SET topic_key = 'unrelated user edit after apply'")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(MigrationError, "post_apply_drift"):
            rollback_migration(
                self.run_dir, report["manifest_sha256"], rollback_token
            )

    @DARWIN_ONLY
    def test_rollback_rechecks_complete_digest_under_exclusive_restore_lock(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        original = taxonomy_migration._rollback_file_state
        injected = False

        def inject_unrelated_write(inputs, record):
            nonlocal injected
            if not injected:
                injected = True
                conn = sqlite3.connect(self.db_path)
                try:
                    conn.execute(
                        "UPDATE topics SET topic_key = 'restore-window-write' WHERE topic_id = 1"
                    )
                    conn.commit()
                finally:
                    conn.close()
            return original(inputs, record)

        with mock.patch(
            "core.taxonomy_migration._rollback_file_state",
            side_effect=inject_unrelated_write,
        ):
            with self.assertRaisesRegex(MigrationError, "post_apply_drift"):
                rollback_migration(
                    self.run_dir, report["manifest_sha256"], rollback_token
                )
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT topic_key FROM topics WHERE topic_id = 1").fetchone()[0],
                "restore-window-write",
            )
        finally:
            conn.close()

    @DARWIN_ONLY
    def test_rollback_reclassifies_file_after_database_restore_before_mutation(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        manifest = load_sealed_run(self.run_dir)[0]
        moved = next(
            record for record in manifest["files"]
            if record["source_relative_path"] != record["destination_relative_path"]
        )
        source = os.path.join(self.vault_root, moved["source_relative_path"])
        destination = os.path.join(
            self.vault_root, moved["destination_relative_path"]
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        original_restore = taxonomy_migration._restore_database_backup
        user_edit = b"edit injected between rollback preflight and file restore\n"

        def restore_then_edit(*args, **kwargs):
            original_restore(*args, **kwargs)
            with open(destination, "wb") as handle:
                handle.write(user_edit)

        with mock.patch(
            "core.taxonomy_migration._restore_database_backup",
            side_effect=restore_then_edit,
        ):
            with self.assertRaisesRegex(MigrationError, "post_apply_drift"):
                rollback_migration(
                    self.run_dir, report["manifest_sha256"], rollback_token
                )

        self.assertFalse(os.path.lexists(source))
        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), user_edit)

    @DARWIN_ONLY
    def test_apply_refuses_concurrent_destination_create_without_overwriting_it(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        manifest = load_sealed_run(self.run_dir)[0]
        moved = next(
            record for record in manifest["files"]
            if record["source_relative_path"] != record["destination_relative_path"]
        )
        destination = os.path.join(
            self.vault_root, moved["destination_relative_path"]
        )
        user_bytes = b"concurrent destination create\n"
        original = taxonomy_migration._renameatx_np
        injected = False

        def create_then_rename(src_fd, src_leaf, dst_fd, dst_leaf, flags):
            nonlocal injected
            if (
                dst_leaf == os.path.basename(destination)
                and flags & taxonomy_migration._RENAME_EXCL
                and not injected
            ):
                injected = True
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with open(destination, "xb") as handle:
                    handle.write(user_bytes)
            return original(src_fd, src_leaf, dst_fd, dst_leaf, flags)

        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with mock.patch(
            "core.taxonomy_migration._renameatx_np",
            side_effect=create_then_rename,
        ):
            with self.assertRaisesRegex(MigrationError, "file_drift"):
                apply_migration(self.config, self.run_dir, report["manifest_sha256"], token)
        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), user_bytes)

    @DARWIN_ONLY
    def test_apply_refuses_concurrent_same_leaf_write_without_overwriting_it(self):
        related_topic_id = self.add_relation_dependent_topic()
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        manifest = load_sealed_run(self.run_dir)[0]
        same_path = next(
            record for record in manifest["files"]
            if record.get("topic_id") == related_topic_id
        )
        target = os.path.join(self.vault_root, same_path["source_relative_path"])
        user_bytes = b"concurrent same-leaf write\n"
        original = taxonomy_migration._renameatx_np
        injected = False

        def write_then_rename(src_fd, src_leaf, dst_fd, dst_leaf, flags):
            nonlocal injected
            if (
                dst_leaf == os.path.basename(target)
                and flags & taxonomy_migration._RENAME_SWAP
                and not injected
            ):
                injected = True
                with open(target, "wb") as handle:
                    handle.write(user_bytes)
            return original(src_fd, src_leaf, dst_fd, dst_leaf, flags)

        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with mock.patch(
            "core.taxonomy_migration._renameatx_np",
            side_effect=write_then_rename,
        ):
            with self.assertRaisesRegex(MigrationError, "file_drift"):
                apply_migration(self.config, self.run_dir, report["manifest_sha256"], token)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), user_bytes)

    @DARWIN_ONLY
    def test_rollback_refuses_concurrent_source_create_without_overwriting_it(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        manifest = load_sealed_run(self.run_dir)[0]
        moved = next(
            record for record in manifest["files"]
            if record["source_relative_path"] != record["destination_relative_path"]
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        source = os.path.join(self.vault_root, moved["source_relative_path"])
        user_bytes = b"concurrent rollback source create\n"
        original = taxonomy_migration._renameatx_np
        injected = False

        def create_then_restore(src_fd, src_leaf, dst_fd, dst_leaf, flags):
            nonlocal injected
            if (
                dst_leaf == os.path.basename(source)
                and flags & taxonomy_migration._RENAME_EXCL
                and not injected
            ):
                injected = True
                os.makedirs(os.path.dirname(source), exist_ok=True)
                with open(source, "xb") as handle:
                    handle.write(user_bytes)
            return original(src_fd, src_leaf, dst_fd, dst_leaf, flags)

        with mock.patch(
            "core.taxonomy_migration._renameatx_np",
            side_effect=create_then_restore,
        ):
            with self.assertRaisesRegex(MigrationError, "post_apply_drift"):
                rollback_migration(
                    self.run_dir, report["manifest_sha256"], rollback_token
                )
        with open(source, "rb") as handle:
            self.assertEqual(handle.read(), user_bytes)

    @DARWIN_ONLY
    def test_apply_quarantine_restores_concurrent_source_write_before_delete(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        manifest = load_sealed_run(self.run_dir)[0]
        moved = next(
            record for record in manifest["files"]
            if record["source_relative_path"] != record["destination_relative_path"]
        )
        source = os.path.join(self.vault_root, moved["source_relative_path"])
        user_bytes = b"concurrent write before source quarantine\n"
        original = taxonomy_migration._renameatx_np
        injected = False

        def write_then_quarantine(src_fd, src_leaf, dst_fd, dst_leaf, flags):
            nonlocal injected
            if dst_leaf.endswith("-quarantine") and src_leaf == os.path.basename(source) and not injected:
                injected = True
                with open(source, "wb") as handle:
                    handle.write(user_bytes)
            return original(src_fd, src_leaf, dst_fd, dst_leaf, flags)

        token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        with mock.patch(
            "core.taxonomy_migration._renameatx_np",
            side_effect=write_then_quarantine,
        ):
            with self.assertRaisesRegex(MigrationError, "file_drift"):
                apply_migration(self.config, self.run_dir, report["manifest_sha256"], token)
        with open(source, "rb") as handle:
            self.assertEqual(handle.read(), user_bytes)

    @DARWIN_ONLY
    def test_rollback_quarantine_restores_concurrent_destination_write_before_delete(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        manifest = load_sealed_run(self.run_dir)[0]
        moved = next(
            record for record in manifest["files"]
            if record["source_relative_path"] != record["destination_relative_path"]
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        destination = os.path.join(
            self.vault_root, moved["destination_relative_path"]
        )
        user_bytes = b"concurrent write before rollback quarantine\n"
        original = taxonomy_migration._renameatx_np
        injected = False

        def write_then_quarantine(src_fd, src_leaf, dst_fd, dst_leaf, flags):
            nonlocal injected
            if (
                dst_leaf.endswith("-quarantine")
                and src_leaf == os.path.basename(destination)
                and not injected
            ):
                injected = True
                with open(destination, "wb") as handle:
                    handle.write(user_bytes)
            return original(src_fd, src_leaf, dst_fd, dst_leaf, flags)

        with mock.patch(
            "core.taxonomy_migration._renameatx_np",
            side_effect=write_then_quarantine,
        ):
            with self.assertRaisesRegex(MigrationError, "post_apply_drift"):
                rollback_migration(
                    self.run_dir, report["manifest_sha256"], rollback_token
                )
        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), user_bytes)

    @DARWIN_ONLY
    def test_rollback_requires_complete_restored_database_digest(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        original_verify = taxonomy_migration._verify_database_before
        injected = False

        def verify_then_inject_unrelated_restore_drift(store, conn, manifest):
            nonlocal injected
            original_verify(store, conn, manifest)
            database_path = conn.execute("PRAGMA database_list").fetchone()[2]
            if not injected and os.path.realpath(database_path) == os.path.realpath(
                self.db_path
            ):
                injected = True
                conn.execute(
                    "UPDATE topics SET topic_key = 'restored-digest-mismatch' "
                    "WHERE topic_id = 1"
                )
                conn.commit()

        with mock.patch(
            "core.taxonomy_migration._verify_database_before",
            side_effect=verify_then_inject_unrelated_restore_drift,
        ):
            with self.assertRaisesRegex(MigrationError, "restore_failed"):
                rollback_migration(
                    self.run_dir, report["manifest_sha256"], rollback_token
                )
        self.assertTrue(injected)

    @DARWIN_ONLY
    def test_second_rollback_is_idempotent(self):
        report = preview_migration(
            self.config, "human_ai_intimacy_v1", self.run_dir
        )
        apply_token = f"APPLY_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        rollback_token = f"ROLLBACK_TAXONOMY_MIGRATION:{report['manifest_sha256']}"
        apply_migration(
            self.config, self.run_dir, report["manifest_sha256"], apply_token
        )
        rollback_migration(
            self.run_dir, report["manifest_sha256"], rollback_token
        )
        second = rollback_migration(
            self.run_dir, report["manifest_sha256"], rollback_token
        )
        self.assertEqual(second["state"], "rolled_back")
        self.assertEqual(second["restored_this_invocation"], 0)


if __name__ == "__main__":
    unittest.main()
