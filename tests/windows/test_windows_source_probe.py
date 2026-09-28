"""Synthetic coverage for the E1a bounded read-only Windows source probe.

All classifications are exercised through injected fakes; no real WeChat
installation, process table, network or credential is touched. Native-only
smoke coverage is skipped off Windows.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests.paths import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from core.platform import (  # noqa: E402
    PathIdentity,
    PathIdentityError,
    ReparsePointConflict,
)
from core.windows_source_probe import (  # noqa: E402
    LAYOUT_PROFILE,
    RECEIPT_SCHEMA,
    probe_selection,
)
import probe_wechat_windows  # noqa: E402


FIXED_ENV = {"os": "windows", "arch": "x64", "python": "3.14.5", "os_build": 26200}
SECRET_ACCOUNT = "wxid_secretaccount"
SECOND_ACCOUNT = "wxid_secondaccount"


class FakePathService:
    """Path-service double with per-path admission failures."""

    def __init__(self, failures: dict[str, Exception] | None = None):
        self.failures = failures or {}
        self.containment_calls: list[str] = []

    def describe(self, path, *, source_root=None):
        key = os.fspath(path)
        if source_root is not None:
            self.containment_calls.append(key)
        if key in self.failures:
            raise self.failures[key]
        return PathIdentity(
            display_path=key,
            operational_path=key,
            identity_key="stub:" + key,
        )


def fake_root_lister(tree: dict[str, list[tuple[str, bool, bool]]]):
    def lister(path: str):
        if path not in tree:
            raise OSError("unreadable")
        return list(tree[path])

    return lister


def make_exe(root: Path, name: str = "WeChat.exe") -> Path:
    exe = root / name
    exe.write_bytes(b"synthetic pe bytes")
    return exe


def version_reader_ok(path: str) -> str:
    return "4.1.2.34"


def product_reader_wechat(path: str) -> str:
    return "WeChat"


def hash_stub(path: str) -> str:
    return "ab" * 32


def probe_on(exe, root, **overrides):
    kwargs = {
        "path_service": FakePathService(),
        "version_reader": version_reader_ok,
        "product_reader": product_reader_wechat,
        "process_lister": lambda: [],
        "hasher": hash_stub,
        "root_lister": fake_root_lister({str(root): []}),
        "environment": dict(FIXED_ENV),
    }
    kwargs.update(overrides)
    return probe_selection(str(exe), str(root), **kwargs)


def assert_receipt_content_free(case: unittest.TestCase, receipt: dict, forbidden: list[str]):
    """Recursively inspect the complete serialized receipt for private data."""
    payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True)

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield from walk(key)
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)
        else:
            yield value

    for value in walk(receipt):
        case.assertIsInstance(value, (str, int, float, bool, type(None)))
        if isinstance(value, str):
            for marker in forbidden:
                case.assertNotIn(marker, value)
            case.assertNotIn(str(Path(tempfile.gettempdir())), payload)


class ProbeExecutableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_exe_missing_maps_to_unknown(self):
        missing = self.root / "WeChat.exe"
        service = FakePathService({str(missing): PathIdentityError("missing_final_component")})
        receipt = probe_on(missing, self.root, path_service=service)
        self.assertEqual(receipt["executable"]["status"], "exe_missing")
        self.assertEqual(receipt["verdict"], "unknown")
        self.assertEqual(receipt["process"]["status"], "process_not_run")

    def test_exe_reparse_conflict_maps_to_unreadable(self):
        exe = make_exe(self.root)
        service = FakePathService({str(exe): ReparsePointConflict("tag_a0000003")})
        receipt = probe_on(exe, self.root, path_service=service)
        self.assertEqual(receipt["executable"]["status"], "exe_reparse_conflict")
        self.assertEqual(receipt["verdict"], "unreadable")

    def test_exe_not_regular_is_unsupported(self):
        receipt = probe_on(self.root, self.root)
        self.assertEqual(receipt["executable"]["status"], "exe_not_regular")
        self.assertEqual(receipt["verdict"], "unsupported")

    def test_exe_hash_failure_is_unreadable(self):
        exe = make_exe(self.root)

        def failing_hash(path: str) -> str:
            raise OSError("denied")

        receipt = probe_on(exe, self.root, hasher=failing_hash)
        self.assertEqual(receipt["executable"]["status"], "exe_unreadable")
        self.assertEqual(receipt["verdict"], "unreadable")


class ProbeBuildTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.exe = make_exe(self.root)

    def test_missing_version_marks_not_run(self):
        receipt = probe_on(self.exe, self.root, version_reader=lambda path: None)
        self.assertEqual(receipt["build"]["status"], "build_unknown")
        self.assertIn("executable_version", receipt["not_run"])
        self.assertEqual(receipt["verdict"], "unknown")

    def test_missing_product_name_marks_not_run(self):
        receipt = probe_on(self.exe, self.root, product_reader=lambda path: None)
        self.assertEqual(receipt["build"]["status"], "build_unknown")
        self.assertIn("executable_product_name", receipt["not_run"])

    def test_non_wechat_product_is_unsupported(self):
        receipt = probe_on(
            self.exe, self.root, product_reader=lambda path: "Notepad"
        )
        self.assertEqual(receipt["build"]["status"], "build_unsupported")
        self.assertEqual(receipt["verdict"], "unsupported")


class ProbeProcessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.exe = make_exe(self.root)

    def test_process_none(self):
        receipt = probe_on(self.exe, self.root, process_lister=lambda: ["other.exe"])
        self.assertEqual(receipt["process"]["status"], "process_none")
        self.assertEqual(receipt["process"]["candidate_count"], 0)

    def test_process_unique(self):
        receipt = probe_on(
            self.exe, self.root, process_lister=lambda: ["WeChat.exe", "other.exe"]
        )
        self.assertEqual(receipt["process"]["status"], "process_unique")

    def test_process_ambiguous_blocks_unique_verdict(self):
        receipt = probe_on(
            self.exe,
            self.root,
            process_lister=lambda: ["WeChat.exe", "wechat.EXE"],
            root_lister=fake_root_lister(
                {
                    str(self.root): [(SECRET_ACCOUNT, True, False)],
                    str(self.root / SECRET_ACCOUNT / "msg"): [("Msg0.db", False, False)],
                }
            ),
        )
        self.assertEqual(receipt["process"]["status"], "process_ambiguous")
        self.assertEqual(receipt["verdict"], "ambiguous")

    def test_process_unavailable_marks_not_run(self):
        receipt = probe_on(self.exe, self.root, process_lister=lambda: None)
        self.assertEqual(receipt["process"]["status"], "process_not_run")
        self.assertIn("process_observation", receipt["not_run"])


class ProbeSourceRootTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.exe = make_exe(self.root)

    def probe_with_root_failure(self, failure: Exception):
        service = FakePathService({str(self.root): failure})
        return probe_on(self.exe, self.root, path_service=service)

    def test_root_missing(self):
        receipt = self.probe_with_root_failure(PathIdentityError("missing_final_component"))
        self.assertEqual(receipt["source_root"]["status"], "root_missing")
        self.assertEqual(receipt["layout"]["status"], "layout_not_run")
        self.assertEqual(receipt["verdict"], "unknown")

    def test_root_not_directory(self):
        receipt = self.probe_with_root_failure(PathIdentityError("ancestor_not_directory"))
        self.assertEqual(receipt["source_root"]["status"], "root_not_directory")
        self.assertEqual(receipt["verdict"], "unsupported")

    def test_root_unsupported_filesystem(self):
        receipt = self.probe_with_root_failure(PathIdentityError("unsupported_filesystem"))
        self.assertEqual(receipt["source_root"]["status"], "root_unsupported_filesystem")
        self.assertEqual(receipt["verdict"], "unsupported")

    def test_root_reparse_conflict(self):
        receipt = self.probe_with_root_failure(ReparsePointConflict("tag_a0000003"))
        self.assertEqual(receipt["source_root"]["status"], "root_reparse_conflict")
        self.assertEqual(receipt["verdict"], "unreadable")

    def test_root_remote_namespace(self):
        receipt = self.probe_with_root_failure(PathIdentityError("remote_path_unsupported"))
        self.assertEqual(receipt["source_root"]["status"], "root_unsupported_namespace")
        self.assertEqual(receipt["verdict"], "unsupported")

    def test_root_unreadable(self):
        service = FakePathService({str(self.root): OSError("denied")})
        receipt = probe_on(self.exe, self.root, path_service=service)
        self.assertEqual(receipt["source_root"]["status"], "root_unreadable")
        self.assertEqual(receipt["verdict"], "unreadable")


class ProbeLayoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.exe = make_exe(self.root)

    def test_no_candidate_when_root_has_no_subdirs(self):
        receipt = probe_on(self.exe, self.root, root_lister=fake_root_lister({str(self.root): []}))
        self.assertEqual(receipt["layout"]["status"], "no_candidate")
        self.assertEqual(receipt["layout"]["candidate_count"], 0)
        self.assertEqual(receipt["verdict"], "unknown")

    def test_unique_candidate_is_supported(self):
        tree = {
            str(self.root): [(SECRET_ACCOUNT, True, False)],
            str(self.root / SECRET_ACCOUNT / "msg"): [
                ("Msg0.db", False, False),
                ("Msg0.db-wal", False, False),
            ],
        }
        receipt = probe_on(self.exe, self.root, root_lister=fake_root_lister(tree))
        self.assertEqual(receipt["layout"]["status"], "unique_candidate")
        self.assertEqual(receipt["layout"]["layout_profile"], LAYOUT_PROFILE)
        self.assertEqual(receipt["verdict"], "unique_supported_candidate")

    def test_ambiguous_candidates(self):
        tree = {
            str(self.root): [(SECRET_ACCOUNT, True, False), (SECOND_ACCOUNT, True, False)],
            str(self.root / SECRET_ACCOUNT / "msg"): [("Msg0.db", False, False)],
            str(self.root / SECOND_ACCOUNT / "msg"): [("Msg0.db", False, False)],
        }
        receipt = probe_on(self.exe, self.root, root_lister=fake_root_lister(tree))
        self.assertEqual(receipt["layout"]["status"], "ambiguous_candidates")
        self.assertEqual(receipt["layout"]["candidate_count"], 2)
        self.assertEqual(receipt["verdict"], "ambiguous")

    def test_unexpected_layout_when_nothing_matches(self):
        tree = {
            str(self.root): [(SECRET_ACCOUNT, True, False)],
            str(self.root / SECRET_ACCOUNT / "msg"): [("readme.txt", False, False)],
        }
        receipt = probe_on(self.exe, self.root, root_lister=fake_root_lister(tree))
        self.assertEqual(receipt["layout"]["status"], "unexpected_layout")
        self.assertEqual(receipt["verdict"], "unknown")

    def test_partially_unreadable_layout_is_not_unique(self):
        tree = {
            str(self.root): [(SECRET_ACCOUNT, True, False), (SECOND_ACCOUNT, True, False)],
            str(self.root / SECRET_ACCOUNT / "msg"): [("Msg0.db", False, False)],
            # SECOND_ACCOUNT/msg missing from the tree -> OSError -> unreadable
        }
        receipt = probe_on(self.exe, self.root, root_lister=fake_root_lister(tree))
        self.assertEqual(receipt["layout"]["status"], "layout_partially_unreadable")
        self.assertIn("complete_layout_observation", receipt["not_run"])
        self.assertEqual(receipt["verdict"], "unknown")

    def test_reparse_dir_inside_root_fails_closed(self):
        tree = {
            str(self.root): [(SECRET_ACCOUNT, True, True)],
        }
        receipt = probe_on(self.exe, self.root, root_lister=fake_root_lister(tree))
        self.assertEqual(receipt["source_root"]["status"], "root_reparse_conflict")
        self.assertEqual(receipt["layout"]["status"], "layout_not_run")
        self.assertEqual(receipt["verdict"], "unreadable")

    def test_root_listing_unreadable(self):
        def failing(path: str):
            raise OSError("denied")

        receipt = probe_on(self.exe, self.root, root_lister=failing)
        self.assertEqual(receipt["source_root"]["status"], "root_unreadable")
        self.assertEqual(receipt["layout"]["status"], "layout_not_run")

    def test_root_entry_limit_is_bounded(self):
        entries = [(f"dir{i}", True, False) for i in range(600)]
        receipt = probe_on(
            self.exe, self.root, root_lister=fake_root_lister({str(self.root): entries})
        )
        self.assertEqual(receipt["layout"]["status"], "unexpected_layout")
        self.assertTrue(receipt["layout"]["root_entry_limit_exceeded"])

    def test_containment_escape_fails_closed(self):
        tree = {
            str(self.root): [(SECRET_ACCOUNT, True, False)],
            str(self.root / SECRET_ACCOUNT / "msg"): [("Msg0.db", False, False)],
        }
        candidate = str(self.root / SECRET_ACCOUNT)
        service = FakePathService(
            {candidate: PathIdentityError("source_root_escape")}
        )
        receipt = probe_on(
            self.exe, self.root, path_service=service,
            root_lister=fake_root_lister(tree),
        )
        self.assertEqual(receipt["source_root"]["status"], "root_escape")
        self.assertEqual(receipt["layout"]["status"], "layout_not_run")
        self.assertEqual(receipt["verdict"], "unknown")
        self.assertIn(candidate, service.containment_calls)


class ProbeReceiptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.exe = make_exe(self.root)

    def test_receipt_is_deterministic(self):
        tree = {
            str(self.root): [(SECRET_ACCOUNT, True, False)],
            str(self.root / SECRET_ACCOUNT / "msg"): [("Msg0.db", False, False)],
        }
        kwargs = {"root_lister": fake_root_lister(tree)}
        first = probe_on(self.exe, self.root, **kwargs)
        second = probe_on(self.exe, self.root, **kwargs)
        self.assertEqual(first, second)
        self.assertEqual(first["schema"], RECEIPT_SCHEMA)
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )

    def test_success_receipt_is_content_free(self):
        tree = {
            str(self.root): [(SECRET_ACCOUNT, True, False)],
            str(self.root / SECRET_ACCOUNT / "msg"): [("Msg0.db", False, False)],
        }
        receipt = probe_on(self.exe, self.root, root_lister=fake_root_lister(tree))
        self.assertEqual(receipt["verdict"], "unique_supported_candidate")
        assert_receipt_content_free(
            self, receipt, [SECRET_ACCOUNT, str(self.root), str(self.exe)]
        )

    def test_failure_receipt_is_content_free(self):
        service = FakePathService({str(self.root): OSError("denied")})
        receipt = probe_on(self.exe, self.root, path_service=service)
        self.assertEqual(receipt["verdict"], "unreadable")
        assert_receipt_content_free(
            self, receipt, [SECRET_ACCOUNT, str(self.root), str(self.exe)]
        )


class ProbeCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.exe = make_exe(self.root)

    def run_cli(self, argv, receipt):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            probe_wechat_windows, "probe_selection", return_value=receipt
        ), mock.patch.object(
            probe_wechat_windows, "create_path_service", lambda platform: FakePathService()
        ), mock.patch.object(
            probe_wechat_windows, "detect_platform",
            lambda: probe_wechat_windows.PlatformName.WINDOWS,
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = probe_wechat_windows.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def base_receipt(self, verdict: str) -> dict:
        return {
            "schema": RECEIPT_SCHEMA,
            "probe_version": "e1a.1",
            "environment": dict(FIXED_ENV),
            "executable": {"status": "exe_ok", "sha256": "ab" * 32},
            "build": {"status": "build_observed", "file_version": "4.1.2.34"},
            "process": {"status": "process_none", "candidate_count": 0},
            "source_root": {"status": "root_ok"},
            "layout": {"status": "unique_candidate", "candidate_count": 1,
                       "account_dir_count": 1, "layout_profile": LAYOUT_PROFILE},
            "verdict": verdict,
            "not_run": [],
        }

    def test_cli_success_exit_zero(self):
        code, stdout, stderr = self.run_cli(
            ["--wechat-exe", str(self.exe), "--source-root", str(self.root)],
            self.base_receipt("unique_supported_candidate"),
        )
        self.assertEqual(code, 0)
        parsed = json.loads(stdout)
        assert_receipt_content_free(
            self, parsed, [SECRET_ACCOUNT, str(self.root), str(self.exe)]
        )

    def test_cli_non_unique_exit_one(self):
        code, stdout, _ = self.run_cli(
            ["--wechat-exe", str(self.exe), "--source-root", str(self.root)],
            self.base_receipt("ambiguous"),
        )
        self.assertEqual(code, 1)
        assert_receipt_content_free(
            self, json.loads(stdout), [str(self.root), str(self.exe)]
        )

    def test_cli_requires_both_selections(self):
        with self.assertRaises(SystemExit) as raised:
            probe_wechat_windows.main(["--wechat-exe", str(self.exe)])
        self.assertEqual(raised.exception.code, 2)

    def test_cli_refuses_non_windows(self):
        with mock.patch.object(
            probe_wechat_windows, "detect_platform",
            lambda: probe_wechat_windows.PlatformName.MACOS,
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = probe_wechat_windows.main(
                    ["--wechat-exe", str(self.exe), "--source-root", str(self.root)]
                )
        self.assertEqual(code, 2)
        self.assertNotIn(str(self.exe), stderr.getvalue())


@unittest.skipUnless(sys.platform == "win32", "native probe seam runs on Windows")
class ProbeNativeSeamTests(unittest.TestCase):
    """Smoke-check the real ctypes seams against the Python interpreter itself."""

    def test_pe_version_and_product_name_readable(self):
        from core.windows_source_probe import _read_file_version, _read_product_name

        exe = sys.executable
        version = _read_file_version(exe)
        self.assertIsNotNone(version)
        self.assertRegex(version, r"^\d+\.\d+\.\d+\.\d+$")
        self.assertIsNotNone(_read_product_name(exe))

    def test_process_image_listing_is_nonempty(self):
        from core.windows_source_probe import _list_process_image_names

        names = _list_process_image_names()
        self.assertIsInstance(names, list)
        self.assertTrue(any(name.lower() == "python.exe" for name in names))

    def test_real_path_service_probe_on_synthetic_tree(self):
        from core.platform import PlatformName, create_path_service

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            account = root / SECRET_ACCOUNT
            (account / "msg").mkdir(parents=True)
            (account / "msg" / "Msg0.db").write_bytes(b"synthetic")
            exe = make_exe(root)
            receipt = probe_selection(
                str(exe),
                str(root),
                path_service=create_path_service(PlatformName.WINDOWS),
                version_reader=lambda path: "4.1.2.34",
                product_reader=lambda path: "WeChat",
                process_lister=lambda: [],
                hasher=hash_stub,
                environment=dict(FIXED_ENV),
            )
        self.assertEqual(receipt["verdict"], "unique_supported_candidate")
        assert_receipt_content_free(
            self, receipt, [SECRET_ACCOUNT, str(root), str(exe)]
        )


if __name__ == "__main__":
    unittest.main()
