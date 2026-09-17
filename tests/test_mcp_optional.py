import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

from tests.paths import REPO_ROOT


# Import app.py in a fresh interpreter with the optional MCP SDK blocked, so
# ordinary macOS bootstrap is proven not to require or touch that surface.
_BOOTSTRAP = r"""
import importlib.abc
import json
import socket
import sys


class _BlockMcpFinder(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.attempts = []

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "mcp" or fullname.startswith("mcp."):
            self.attempts.append(fullname)
            raise ModuleNotFoundError(
                "optional MCP SDK blocked for this test", name=fullname
            )
        return None


class _NoNetworkSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise AssertionError("ordinary app bootstrap must not open a socket")


_blocker = _BlockMcpFinder()
sys.meta_path.insert(0, _blocker)
socket.socket = _NoNetworkSocket

import app  # noqa: F401

print(json.dumps({
    "sdk_import_attempts": _blocker.attempts,
    "sdk_loaded": sorted(
        name for name in sys.modules
        if name == "mcp" or name.startswith("mcp.")
    ),
    "mcp_named_app_attributes": sorted(
        name for name in dir(app) if "mcp" in name.lower()
    ),
}))
"""


# Import a target module under a synthetic MCP SDK state and report what the
# import machinery did, so an absent SDK and a broken installed SDK stay
# distinguishable.
_IMPORT_PROBE = r"""
import importlib.abc
import json
import sys


mode = sys.argv[1]
target = sys.argv[2]
if mode == "absent":
    sys.executable = "/fixture with spaces/python"


class _McpFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "mcp":
            return None
        if mode == "absent":
            raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
        if mode == "broken":
            # Installed but incompatible: its import chain needs a missing
            # unrelated dependency, which must not read as "SDK absent".
            raise ModuleNotFoundError(
                "No module named 'mcp_transport_extra'",
                name="mcp_transport_extra",
            )
        return None


sys.meta_path.insert(0, _McpFinder())

try:
    module = __import__(target, fromlist=["*"])
except BaseException as exc:
    print(json.dumps({
        "outcome": "raised",
        "type": type(exc).__name__,
        "name": getattr(exc, "name", None),
        "message": str(exc),
        "executable": sys.executable,
    }))
    raise SystemExit(0)

print(json.dumps({
    "outcome": "imported",
    "sdk_available": getattr(module, "_MCP_SDK_AVAILABLE", None),
    "executable": sys.executable,
}))
"""


class _FakeMenuItem:
    def __init__(self, title):
        self.title = title


class _OrderedMenu:
    """Ordered-menu double with rumps insert_after/insert_before semantics."""

    def __init__(self, titles):
        self._items = {title: _FakeMenuItem(title) for title in titles}
        self._order = list(titles)

    def __contains__(self, title):
        return title in self._items

    def __delitem__(self, title):
        self._anchor_index(title)
        del self._items[title]
        self._order.remove(title)

    def insert_after(self, anchor, item):
        self._insert(anchor, item, offset=1)

    def insert_before(self, anchor, item):
        self._insert(anchor, item, offset=0)

    def _insert(self, anchor, item, offset):
        index = self._anchor_index(anchor)
        if item.title == anchor:
            raise ValueError("same key provided for location and insertion")
        if item.title in self._items:
            raise ValueError(f"duplicate menu key: {item.title}")
        self._items[item.title] = item
        self._order.insert(index + offset, item.title)

    def _anchor_index(self, title):
        if title not in self._items:
            raise KeyError(f"missing anchor: {title}")
        return self._order.index(title)

    def titles(self):
        return list(self._order)


def _probe_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


class McpImportGuardTests(unittest.TestCase):
    def _probe(self, mode, target):
        result = subprocess.run(
            [sys.executable, "-c", _IMPORT_PROBE, mode, target],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=_probe_env(),
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_absent_sdk_raises_opt_in_guidance_for_the_same_interpreter(self):
        payload = self._probe("absent", "mcp_server")

        self.assertEqual(payload["outcome"], "raised")
        self.assertEqual(payload["type"], "ImportError")
        self.assertEqual(
            shlex.split(payload["message"].splitlines()[-1]),
            [payload["executable"], "-m", "pip", "install", "-r",
             str(REPO_ROOT / "requirements-mcp.txt")],
        )

    def test_broken_sdk_import_failure_propagates(self):
        payload = self._probe("broken", "mcp_server")

        self.assertEqual(payload["outcome"], "raised")
        self.assertEqual(payload["type"], "ModuleNotFoundError")
        self.assertEqual(payload["name"], "mcp_transport_extra")

    def test_read_only_contract_skips_only_for_an_absent_top_level_sdk(self):
        absent = self._probe("absent", "tests.test_mcp_read_only")
        self.assertEqual(absent["outcome"], "imported")
        self.assertFalse(absent["sdk_available"])

        broken = self._probe("broken", "tests.test_mcp_read_only")
        self.assertEqual(broken["outcome"], "raised")
        self.assertEqual(broken["type"], "ModuleNotFoundError")
        self.assertEqual(broken["name"], "mcp_transport_extra")


class McpOptionalDependencyTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "imports the macOS app shell")
    def test_app_bootstrap_never_imports_the_optional_mcp_sdk(self):
        env = dict(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            env["WE_GROUPCHAT_OBSIDIAN_DATA_DIR"] = os.path.join(tmp, "data")
            env["TMPDIR"] = tmp
            env["WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH"] = os.path.join(
                tmp, "no-such-wechat"
            )
            env["PYTHONPATH"] = str(REPO_ROOT)
            result = subprocess.run(
                [sys.executable, "-c", _BOOTSTRAP],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["sdk_import_attempts"], [])
        self.assertEqual(payload["sdk_loaded"], [])
        self.assertEqual(payload["mcp_named_app_attributes"], [])

    def test_default_requirements_drop_the_optional_sdk(self):
        default = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        optional = (REPO_ROOT / "requirements-mcp.txt").read_text(encoding="utf-8")

        self.assertNotIn("mcp[cli]", default)
        self.assertIn("mcp[cli]", optional)
        self.assertIn("-r requirements.txt", optional)

    def test_launcher_installs_only_default_requirements(self):
        launcher = (REPO_ROOT / "launchers" / "启动.command").read_text(
            encoding="utf-8"
        )

        self.assertIn('REQ_FILE="$PROJECT_DIR/requirements.txt"', launcher)
        self.assertNotIn("requirements-mcp", launcher)

    def test_menu_app_has_no_mcp_menu_or_probe_surface(self):
        source = (REPO_ROOT / "app.py").read_text(encoding="utf-8")

        for removed in (
            "_build_mcp_menu",
            "_rebuild_mcp_menu",
            "_check_mcp_ready",
            "_test_mcp_server",
            "_is_mcp_running",
            "_get_mcp_config_snippet",
            "_copy_claude_desktop_config",
            "_copy_claude_code_config",
            "_do_mcp_test",
            "mcp_server.py",
            "core.mcp_config",
            "MCP 服务",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, source)

    def test_client_snippet_helper_module_is_retired(self):
        self.assertFalse((REPO_ROOT / "core" / "mcp_config.py").exists())
        self.assertIsNone(importlib.util.find_spec("core.mcp_config"))

    def test_ci_checks_base_bootstrap_before_installing_optional_sdk(self):
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "portability.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("requirements-mcp.txt", workflow)
        base_bootstrap = workflow.index(
            "python -m unittest tests.test_mcp_optional"
        )
        install_optional = workflow.index(
            "python -m pip install -r requirements-mcp.txt"
        )
        full_suite = workflow.index("python -m unittest discover")
        self.assertLess(base_bootstrap, install_optional)
        self.assertLess(install_optional, full_suite)


class McpMenuAnchorTests(unittest.TestCase):
    def _menu_app(self, titles):
        from app import WeGroupchatObsidianApp

        class _MenuHost(WeGroupchatObsidianApp):
            # Shadow the rumps.App menu property so tests can inject a fake
            # ordered menu without an AppKit UI session.
            menu = None

        app = _MenuHost.__new__(_MenuHost)
        app.menu = _OrderedMenu(titles)
        app._build_monitor_menu = lambda: _FakeMenuItem("🔔 关注推送")
        app._build_resource_backup_menu = lambda: _FakeMenuItem(
            "🔗 资源索引与本地备份"
        )
        app._build_drive_sync_menu = lambda: _FakeMenuItem(
            "☁️ Google Drive 群文件备份"
        )
        return app

    @unittest.skipUnless(sys.platform == "darwin", "imports the macOS app shell")
    def test_rebuilds_keep_order_without_the_removed_mcp_entry(self):
        app = self._menu_app([
            "刷新群聊列表",
            "🔍 关键词搜索",
            "⛔ 停止当前任务",
            "📋 最近总结",
            "🔔 关注推送",
            "🔗 资源索引与本地备份",
            "☁️ Google Drive 群文件备份",
            "⚙️ 设置",
            "🔄 刷新数据源",
        ])

        for _ in range(3):
            app._rebuild_monitor_menu()
            app._rebuild_resource_backup_menu()
            app._rebuild_drive_sync_menu()

        titles = app.menu.titles()
        self.assertNotIn("🔌 MCP 服务", titles)
        self.assertLess(
            titles.index("🔔 关注推送"),
            titles.index("🔗 资源索引与本地备份"),
        )
        self.assertLess(
            titles.index("🔗 资源索引与本地备份"),
            titles.index("☁️ Google Drive 群文件备份"),
        )
        self.assertLess(
            titles.index("☁️ Google Drive 群文件备份"),
            titles.index("⚙️ 设置"),
        )

    @unittest.skipUnless(sys.platform == "darwin", "imports the macOS app shell")
    def test_rebuilds_use_fallbacks_without_monitor_or_resource_anchors(self):
        app = self._menu_app(["📋 最近总结", "⚙️ 设置", "🔄 刷新数据源"])

        app._rebuild_monitor_menu()
        app._rebuild_resource_backup_menu()
        app._rebuild_drive_sync_menu()

        titles = app.menu.titles()
        self.assertLess(titles.index("📋 最近总结"), titles.index("⚙️ 设置"))
        for title in (
            "🔔 关注推送",
            "🔗 资源索引与本地备份",
            "☁️ Google Drive 群文件备份",
        ):
            self.assertIn(title, titles)


if __name__ == "__main__":
    unittest.main()
