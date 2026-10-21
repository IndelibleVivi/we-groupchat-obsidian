"""CLI grants and content-free output; no source, provider or LaunchAgent."""
from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import Mock, patch

from scripts.rebuild_knowledge import main, public_result


class RebuildCommandTests(unittest.TestCase):
    def invoke(self, argv, factory, configured=None):
        output = io.StringIO()
        with patch("core.config.ConfigStore.inspect", return_value=configured), redirect_stdout(output):
            code = main(argv, coordinator_factory=factory)
        return code, json.loads(output.getvalue())

    def test_all_commands_resolve_configured_default_and_explicit_runtime_roots(self):
        commands = (
            ["plan", "--provider-profile", "synthetic-profile.json", "--run-id", "run"],
            ["execute", "run", "--allow-transient-wechat-source-read", "--allow-model-processing"],
            ["resume", "run", "--allow-transient-wechat-source-read", "--allow-model-processing"],
            ["status", "run"], ["verify", "run"], ["compact-journal", "run"],
            ["restore-runtime", "run"], ["renew-plan", "run"],
            ["rollback", "run"], ["relocate", "run", "--destination-root", "/synthetic/moved"],
        )
        for command in commands:
            with self.subTest(command=command[0], source="configured"):
                factory = Mock()
                coordinator = factory.return_value
                coordinator.plan.return_value = {
                    "run_id": "run", "generation_id": "generation", "history_policy": "source-first-v1",
                    "reading_policy": "editorial-composition-v2", "scope": {}, "provider_budget": {},
                    "configured_admission": {}, "storage": {}}
                coordinator.execute.return_value = {"state": "incomplete"}
                coordinator.resume.return_value = {"state": "incomplete"}
                coordinator.status.return_value = {"state": "incomplete"}
                coordinator.verify.return_value = {"complete": True}
                coordinator.compact_journal.return_value = {"state": "compact"}
                coordinator.restore_runtime.return_value = {"state": "runtime_restore_planned"}
                coordinator.renew_plan.return_value = {"state": "plan_renewal_planned"}
                coordinator.rollback.return_value = {"state": "rollback_planned"}
                coordinator.relocate.return_value = {"state": "relocation_planned"}
                with patch("core.semantic.rebuild.read_json", return_value={}):
                    self.invoke(command, factory, configured={"semantic_runtime_root": "/synthetic/configured"})
                self.assertEqual(factory.call_args.args[0], "/synthetic/configured")
            with self.subTest(command=command[0], source="default"):
                factory = Mock()
                coordinator = factory.return_value
                coordinator.plan.return_value = {
                    "run_id": "run", "generation_id": "generation", "history_policy": "source-first-v1",
                    "reading_policy": "editorial-composition-v2", "scope": {}, "provider_budget": {},
                    "configured_admission": {}, "storage": {}}
                coordinator.execute.return_value = {"state": "incomplete"}
                coordinator.resume.return_value = {"state": "incomplete"}
                coordinator.status.return_value = {"state": "incomplete"}
                coordinator.verify.return_value = {"complete": True}
                coordinator.compact_journal.return_value = {"state": "compact"}
                coordinator.restore_runtime.return_value = {"state": "runtime_restore_planned"}
                coordinator.renew_plan.return_value = {"state": "plan_renewal_planned"}
                coordinator.rollback.return_value = {"state": "rollback_planned"}
                coordinator.relocate.return_value = {"state": "relocation_planned"}
                with patch("core.config.DATA_DIR", "/synthetic/data"), \
                     patch("core.semantic.rebuild.read_json", return_value={}):
                    self.invoke(command, factory, configured={})
                self.assertEqual(factory.call_args.args[0], "/synthetic/data/semantic")
            with self.subTest(command=command[0], source="explicit"):
                factory = Mock()
                coordinator = factory.return_value
                coordinator.plan.return_value = {
                    "run_id": "run", "generation_id": "generation", "history_policy": "source-first-v1",
                    "reading_policy": "editorial-composition-v2", "scope": {}, "provider_budget": {},
                    "configured_admission": {}, "storage": {}}
                coordinator.execute.return_value = {"state": "incomplete"}
                coordinator.resume.return_value = {"state": "incomplete"}
                coordinator.status.return_value = {"state": "incomplete"}
                coordinator.verify.return_value = {"complete": True}
                coordinator.compact_journal.return_value = {"state": "compact"}
                coordinator.restore_runtime.return_value = {"state": "runtime_restore_planned"}
                coordinator.renew_plan.return_value = {"state": "plan_renewal_planned"}
                coordinator.rollback.return_value = {"state": "rollback_planned"}
                coordinator.relocate.return_value = {"state": "relocation_planned"}
                with patch("core.semantic.rebuild.read_json", return_value={}):
                    self.invoke(["--runtime-root", "/synthetic/explicit", *command], factory,
                                configured={"semantic_runtime_root": "/synthetic/configured"})
                self.assertEqual(factory.call_args.args[0], "/synthetic/explicit")

    def test_missing_either_grant_precedes_any_constructor(self):
        class GuardedConfig:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("config constructed before grants")

        factory = Mock(side_effect=AssertionError("constructor before grants"))
        with patch("core.config.ConfigStore", GuardedConfig):
            for command in ("execute", "resume"):
                for flags in ([], ["--allow-transient-wechat-source-read"], ["--allow-model-processing"]):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = main([command, "run", *flags], coordinator_factory=factory)
                    result = json.loads(output.getvalue())
                    self.assertEqual(code, 2)
                    self.assertEqual(result["code"], "rebuild_explicit_grants_required")
        factory.assert_not_called()

    def test_execute_and_resume_wire_both_explicit_grants(self):
        coordinator = Mock()
        coordinator.execute.return_value = coordinator.resume.return_value = {"state": "incomplete", "code": "index_confirmation_pending"}
        for command in ("execute", "resume"):
            code, result = self.invoke([command, "run", "--allow-transient-wechat-source-read", "--allow-model-processing"],
                                       Mock(return_value=coordinator))
            getattr(coordinator, command).assert_called_once_with("run", allow_source_read=True, allow_model_processing=True)
            self.assertEqual(code, 1)
            self.assertEqual(result["code"], "index_confirmation_pending")

    def test_rollback_plan_and_explicit_apply(self):
        factory = Mock()
        code, _ = self.invoke(["rollback", "run", "--apply"], factory)
        self.assertEqual(code, 2)
        factory.assert_not_called()
        coordinator = Mock()
        coordinator.rollback.return_value = {"state": "rollback_planned", "retained_generation": True}
        code, _ = self.invoke(["rollback", "run"], Mock(return_value=coordinator))
        self.assertEqual(code, 0)
        coordinator.rollback.assert_called_once_with("run", apply=False, allow_runtime_changes=False)

    def test_restore_only_requires_runtime_grant_before_constructor(self):
        factory = Mock(side_effect=AssertionError("constructor before grants"))
        code, result = self.invoke(["restore-runtime", "run", "--apply"], factory)
        self.assertEqual((code, result["code"]), (2, "rebuild_restore_grant_required"))
        factory.assert_not_called()
        for flags, state in (([], "runtime_restore_planned"),
                             (["--apply", "--allow-runtime-changes"], "runtime_restored")):
            coordinator = Mock()
            coordinator.restore_runtime.return_value = {"state": state, "run_id": "run"}
            code, _ = self.invoke(["restore-runtime", "run", *flags], Mock(return_value=coordinator))
            self.assertEqual(code, 0)
            coordinator.restore_runtime.assert_called_once_with("run", apply=bool(flags),
                                                               allow_runtime_changes=bool(flags))
            coordinator.execute.assert_not_called()
            coordinator.resume.assert_not_called()

    def test_relocate_requires_explicit_apply_grant(self):
        factory = Mock(side_effect=AssertionError("constructor before grants"))
        code, result = self.invoke(["relocate", "run", "--destination-root", "/private/moved", "--apply"], factory)
        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "rebuild_relocation_grant_required")
        factory.assert_not_called()

    def test_renew_plan_requires_runtime_grant_before_constructor(self):
        factory = Mock(side_effect=AssertionError("constructor before grants"))
        code, result = self.invoke(["renew-plan", "run", "--apply"], factory)
        self.assertEqual((code, result["code"]), (2, "rebuild_plan_renewal_grant_required"))
        factory.assert_not_called()

    def test_renew_plan_and_apply_wire_expiry_without_source_or_model_grants(self):
        for flags, state in (([], "plan_renewal_planned"),
                             (["--apply", "--allow-runtime-changes"], "plan_renewed")):
            with self.subTest(state=state):
                coordinator = Mock()
                coordinator.renew_plan.return_value = {"state": state, "run_id": "run",
                    "generation_id": "gen", "renewal_count": 1,
                    "old_expires_at": 100, "new_expires_at": 200,
                    "renewal_receipt": "a" * 64, "old_plan": {"private": "content"},
                    "database": "/private/knowledge.sqlite"}
                code, result = self.invoke(["--runtime-root", "/private/synthetic-runtime", "renew-plan", "run", "--expires-in", "604800", *flags],
                                           Mock(return_value=coordinator))
                self.assertEqual(code, 0)
                coordinator.renew_plan.assert_called_once_with("run", apply=bool(flags),
                    allow_runtime_changes=bool(flags), expires_in=604800)
                self.assertEqual(result["renewal_count"], 1)
                self.assertEqual((result["old_expires_at"], result["new_expires_at"]), (100, 200))
                self.assertNotIn("private", json.dumps(result))
                coordinator.execute.assert_not_called()
                coordinator.resume.assert_not_called()

    def test_relocate_plan_and_apply_wire_arguments_and_stay_content_free(self):
        coordinator = Mock()
        coordinator.relocate.return_value = {"state": "relocation_planned", "run_id": "run",
            "generation_id": "gen", "file_count": 3, "total_bytes": 10, "job_count": 0}
        code, result = self.invoke(["--runtime-root", "/private/old", "relocate", "run",
                                    "--destination-root", "/private/moved"], Mock(return_value=coordinator))
        self.assertEqual(code, 0)
        self.assertFalse(coordinator.relocate.call_args.kwargs["apply"])
        self.assertNotIn("destination_root", result)
        coordinator.relocate.return_value = {"state": "relocated", "run_id": "run",
            "generation_id": "gen", "relocation_receipt": "a" * 64, "job_count": 4,
            "file_count": 12, "total_bytes": 900, "destination_root": "/private/moved"}
        code, result = self.invoke(["--runtime-root", "/private/old", "relocate", "run",
                                    "--destination-root", "/private/moved",
                                    "--apply", "--allow-runtime-changes"], Mock(return_value=coordinator))
        self.assertEqual(code, 0)
        kwargs = coordinator.relocate.call_args.kwargs
        self.assertEqual(kwargs["destination_root"], "/private/moved")
        self.assertTrue(kwargs["apply"])
        self.assertTrue(kwargs["allow_runtime_changes"])
        self.assertEqual(result["relocation_receipt"], "a" * 64)
        self.assertNotIn("private", json.dumps(result))

    def test_public_output_omits_private_runtime_identity(self):
        admission = {"text": {"max_inflight": 2, "requests_per_minute": None,
                              "tokens_per_minute": None, "unset": ["requests_per_minute", "tokens_per_minute"]}}
        value = public_result({"state": "incomplete", "private_chat": "SECRET", "database": "/private/path",
                               "configured_admission": admission, "reading_policy": "content-responsibility-v1",
                               "restore_code": "runtime_restore_failed",
                               "restore_attempt_count": 2,
                               "job_states": {"failed": 1},
                               "job_error_codes": ["provider_timeout"],
                               "job_blocker": None,
                               "live_check": {"complete": False, "code": "rebuild_live_increment_pending",
                                              "process": {"executable": "/private/executable"}}})
        rendered = json.dumps(value)
        self.assertNotIn("private", rendered)
        self.assertNotIn("SECRET", rendered)
        self.assertEqual(value["configured_admission"], admission)
        self.assertEqual(value["reading_policy"], "content-responsibility-v1")
        self.assertEqual(value["restore_code"], "runtime_restore_failed")
        self.assertEqual(value["restore_attempt_count"], 2)
        self.assertEqual(value["job_states"], {"failed": 1})
        self.assertEqual(value["job_error_codes"], ["provider_timeout"])
        self.assertIsNone(value["job_blocker"])

    def test_plan_and_status_expose_the_frozen_reading_mode(self):
        coordinator = Mock()
        coordinator.plan.return_value = {"run_id": "run", "generation_id": "generation",
            "history_policy": "source-first-v1", "reading_policy": "content-responsibility-v1",
            "scope": {}, "provider_budget": {}, "configured_admission": {}, "storage": {}}
        with patch("core.semantic.rebuild.read_json", return_value={}):
            code, result = self.invoke(["plan", "--provider-profile", "synthetic-profile.json", "--run-id", "run"],
                                       Mock(return_value=coordinator))
        self.assertEqual(code, 0)
        self.assertEqual(result["reading_policy"], "content-responsibility-v1")
        coordinator.status.return_value = {"state": "incomplete", "reading_policy": "derived-verbatim-v1"}
        code, result = self.invoke(["status", "run"], Mock(return_value=coordinator))
        self.assertEqual(result["reading_policy"], "derived-verbatim-v1")

    def test_errors_are_content_free(self):
        code, value = self.invoke(["status", "run"], Mock(side_effect=ValueError("private URL and chat body")))
        self.assertEqual(code, 1)
        self.assertEqual(value, {"state": "incomplete", "code": "rebuild_operation_failed"})
