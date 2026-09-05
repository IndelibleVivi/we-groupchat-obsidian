import ast
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

from tests.paths import repo_path


class SetupPy2AppTests(unittest.TestCase):
    def _setup_options(self):
        tree = ast.parse(repo_path("setup.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "OPTIONS":
                        return ast.literal_eval(node.value)
        return None

    def _setup_call_keywords(self):
        tree = ast.parse(repo_path("setup.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setup":
                return {keyword.arg for keyword in node.keywords}
        return set()

    def test_py2app_plist_declares_stable_bundle_identity(self):
        options = self._setup_options()

        self.assertIsNotNone(options)
        plist = options["plist"]
        self.assertIn("CFBundleIdentifier", plist)
        self.assertEqual(
            plist["CFBundleIdentifier"],
            "io.github.indeliblevivi.we-groupchat-obsidian",
        )
        self.assertEqual(plist["CFBundleName"], "WeGroupchatObsidian")
        self.assertIn("CFBundleDisplayName", plist)
        self.assertEqual(plist["CFBundleDisplayName"], "微信总结")
        self.assertIn("NSAppDataUsageDescription", plist)
        self.assertIn("显式开启文件解析", plist["NSAppDataUsageDescription"])
        self.assertIn("NSDocumentsFolderUsageDescription", plist)
        self.assertIn("NSFileProviderDomainUsageDescription", plist)
        self.assertTrue(plist["LSUIElement"])

    def test_py2app_uses_project_notification_icon(self):
        options = self._setup_options()

        self.assertEqual(options["iconfile"], "resources/app_icon.icns")
        self.assertTrue(repo_path(options["iconfile"]).is_file())

    def test_py2app_does_not_package_repository_tests(self):
        self.assertNotIn("tests", self._setup_options()["packages"])

    def test_py2app_packages_operator_cli_modules(self):
        self.assertIn("scripts", self._setup_options()["packages"])

    def test_setup_py_keeps_dependencies_in_requirements_file(self):
        keywords = self._setup_call_keywords()

        self.assertNotIn("install_requires", keywords)
        self.assertNotIn("setup_requires", keywords)

    def test_setup_py_imports_optional_build_dependencies_only_when_executed(self):
        tree = ast.parse(repo_path("setup.py").read_text(encoding="utf-8"))
        top_level_imports = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
        }

        self.assertNotIn("py2app.build_app", top_level_imports)
        self.assertNotIn("setuptools", top_level_imports)


@unittest.skipUnless(sys.platform == "darwin", "py2app alias builds are macOS-only")
class AliasAppBuildTests(unittest.TestCase):
    def test_finalize_alias_bundle_repairs_python_313_links_and_verifies(self):
        import setup as app_setup

        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "WeGroupchatObsidian.app"
            resources = bundle / "Contents" / "Resources"
            python_lib = (
                resources
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
            )
            python_lib.mkdir(parents=True)
            (resources / "site.py").write_text("value = 1\n", encoding="utf-8")
            os.symlink("../../site.pyc", python_lib / "site.pyc")
            os.symlink("/missing/python/config", python_lib / "config")
            config_target = Path(tmp) / "python-config"
            config_target.mkdir()
            runner = Mock()

            app_setup.finalize_alias_bundle(
                bundle,
                config_target=config_target,
                runner=runner,
            )

            self.assertTrue((resources / "site.pyc").is_file())
            self.assertEqual(
                (python_lib / "config").resolve(),
                config_target.resolve(),
            )
            self.assertEqual(runner.call_count, 2)
            self.assertEqual(
                runner.call_args_list[0].args[0][:5],
                ["codesign", "--force", "--deep", "--sign", "-"],
            )
            self.assertEqual(
                runner.call_args_list[1].args[0][:2],
                ["codesign", "--verify"],
            )

    def test_finalize_alias_bundle_rejects_other_dangling_links(self):
        import setup as app_setup

        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "WeGroupchatObsidian.app"
            resources = bundle / "Contents" / "Resources"
            python_lib = (
                resources
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
            )
            python_lib.mkdir(parents=True)
            (resources / "site.py").write_text("value = 1\n", encoding="utf-8")
            os.symlink("../../site.pyc", python_lib / "site.pyc")
            os.symlink("/missing/python/config", python_lib / "config")
            os.symlink("missing-resource", resources / "broken")
            config_target = Path(tmp) / "python-config"
            config_target.mkdir()

            with self.assertRaisesRegex(RuntimeError, "dangling links"):
                app_setup.finalize_alias_bundle(
                    bundle,
                    config_target=config_target,
                    runner=Mock(),
                )


if __name__ == "__main__":
    unittest.main()
