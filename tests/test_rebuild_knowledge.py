"""CLI grants and content-free output; no source, provider or LaunchAgent."""
from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import Mock, patch

from scripts.rebuild_knowledge import main, public_result


class RebuildCommandTests(unittest.TestCase):
    def invoke(self, argv, factory):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(argv, coordinator_factory=factory)
        return code, json.loads(output.getvalue())

    def test_missing_either_grant_precedes_any_constructor(self):
        factory = Mock(side_effect=AssertionError("constructor before grants"))
        for command in ("execute", "resume"):
            for flags in ([], ["--allow-transient-wechat-source-read"], ["--allow-model-processing"]):
                code, result = self.invoke([command, "run", *flags], factory)
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

    def test_relocate_requires_explicit_apply_grant(self):
        factory = Mock(side_effect=AssertionError("constructor before grants"))
        code, result = self.invoke(["relocate", "run", "--destination-root", "/private/moved", "--apply"], factory)
        self.assertEqual(code, 2)
        self.assertEqual(result["code"], "rebuild_relocation_grant_required")
        factory.assert_not_called()

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
                               "configured_admission": admission,
                               "live_check": {"complete": False, "code": "rebuild_live_increment_pending",
                                              "process": {"executable": "/private/executable"}}})
        rendered = json.dumps(value)
        self.assertNotIn("private", rendered)
        self.assertNotIn("SECRET", rendered)
        self.assertEqual(value["configured_admission"], admission)

    def test_errors_are_content_free(self):
        code, value = self.invoke(["status", "run"], Mock(side_effect=ValueError("private URL and chat body")))
        self.assertEqual(code, 1)
        self.assertEqual(value, {"state": "incomplete", "code": "rebuild_operation_failed"})
