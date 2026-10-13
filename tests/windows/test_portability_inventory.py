from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PORT_MAP = REPOSITORY_ROOT / "docs" / "WINDOWS-PORT-MAP.md"
ROW_RE = re.compile(
    r"^\| `(?P<path>[^`]+\.py)` \| `(?P<classification>[^`]+)` \|",
    re.MULTILINE,
)
ALLOWED_CLASSIFICATIONS = {
    "windows-import-safe",
    "deferred-w0.2",
    "deferred-w1+",
    "macos-only",
    "operator-deferred",
}
FORBIDDEN_IMPORT_ROOTS = {
    "AppKit",
    "Foundation",
    "Quartz",
    "fcntl",
    "objc",
    "rumps",
}


def python_inventory() -> set[str]:
    files = {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in REPOSITORY_ROOT.glob("*.py")
    }
    for directory in ("ai", "core", "ui", "scripts"):
        files.update(
            path.relative_to(REPOSITORY_ROOT).as_posix()
            for path in (REPOSITORY_ROOT / directory).rglob("*.py")
            if "__pycache__" not in path.parts
        )
    return files


def port_map_inventory() -> dict[str, str]:
    text = PORT_MAP.read_text(encoding="utf-8")
    rows = ROW_RE.findall(text)
    inventory = dict(rows)
    if len(rows) != len(inventory):
        raise AssertionError("duplicate Python path in WINDOWS-PORT-MAP.md")
    return inventory


def module_name(path: str) -> str:
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    return module


class WindowsPortabilityInventoryTests(unittest.TestCase):
    def test_every_owned_python_module_is_classified_once(self):
        mapped = port_map_inventory()
        self.assertEqual(set(mapped), python_inventory())
        self.assertEqual(set(mapped.values()) - ALLOWED_CLASSIFICATIONS, set())

    def test_semantic_runtime_and_provider_storage_stay_outside_windows_gate(self):
        mapped = port_map_inventory()
        for path in ("core/provider_usage.py", "core/semantic/store.py", "core/semantic/projections.py",
                     "core/semantic/runtime.py", "core/semantic/reader_runtime.py", "core/semantic/rebuild.py",
                     "core/semantic/health.py", "ai/cloudflare_provider.py", "scripts/rebuild_knowledge.py"):
            with self.subTest(path=path):
                self.assertIn(path, mapped)
                self.assertNotEqual(mapped[path], "windows-import-safe")

    def test_import_safe_modules_have_no_direct_platform_imports(self):
        mapped = port_map_inventory()
        for path, classification in mapped.items():
            if classification != "windows-import-safe":
                continue
            with self.subTest(path=path):
                tree = ast.parse(
                    (REPOSITORY_ROOT / path).read_text(encoding="utf-8"),
                    filename=path,
                )
                imported_roots = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported_roots.update(
                            alias.name.partition(".")[0] for alias in node.names
                        )
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported_roots.add(node.module.partition(".")[0])
                self.assertEqual(imported_roots & FORBIDDEN_IMPORT_ROOTS, set())

    @unittest.skipUnless(sys.platform == "win32", "Windows import gate")
    def test_windows_import_safe_modules_import_in_fresh_processes(self):
        mapped = port_map_inventory()
        import_safe = sorted(
            module_name(path)
            for path, classification in mapped.items()
            if classification == "windows-import-safe"
        )
        for module in import_safe:
            with self.subTest(module=module):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import importlib,sys; importlib.import_module(sys.argv[1])",
                        module,
                    ],
                    cwd=REPOSITORY_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    msg=(result.stderr or result.stdout).strip(),
                )


if __name__ == "__main__":
    unittest.main()
