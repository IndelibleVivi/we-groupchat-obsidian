import shlex
import subprocess
import unittest

from tests.paths import repo_path


class StartupHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = repo_path("scripts", "startup_helpers.sh")

    def run_helper(self, command, stdin=""):
        script = f"source {shlex.quote(str(self.helper))}; {command}"
        return subprocess.run(
            ["bash", "-c", script],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_version_at_least_compares_major_then_minor(self):
        cases = [
            ("version_at_least 3 9 3 10", False),
            ("version_at_least 3 10 3 10", True),
            ("version_at_least 3 12 3 10", True),
            ("version_at_least 4 0 3 10", True),
            ("version_at_least 2 99 3 10", False),
        ]

        for command, accepted in cases:
            with self.subTest(command=command):
                result = self.run_helper(command)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_install_confirmations_default_to_no(self):
        for function in (
            "confirm_homebrew_python_install",
            "confirm_dependency_install",
        ):
            with self.subTest(function=function):
                self.assertEqual(
                    self.run_helper(function, stdin="y\n").returncode,
                    0,
                )
                self.assertEqual(
                    self.run_helper(function, stdin="Y\n").returncode,
                    0,
                )
                self.assertNotEqual(
                    self.run_helper(function, stdin="\n").returncode,
                    0,
                )
                self.assertNotEqual(
                    self.run_helper(function, stdin="n\n").returncode,
                    0,
                )
                self.assertNotEqual(
                    self.run_helper(function, stdin="").returncode,
                    0,
                )

    def test_launcher_confirms_before_environment_changes(self):
        launcher = repo_path("launchers", "启动.command")
        with open(launcher, encoding="utf-8") as handle:
            contents = handle.read()

        confirmation = contents.index("if ! confirm_dependency_install")
        create_venv = contents.index('"$PYTHON3_CMD" -m venv')
        install_dependencies = contents.index('"$PYTHON_BIN" -m pip install -r')
        self.assertLess(confirmation, create_venv)
        self.assertLess(confirmation, install_dependencies)
        self.assertIn("已取消安装，程序不会启动。", contents)

    def test_refresh_uses_the_exact_target_reopened_by_resign_orchestration(self):
        launcher = repo_path("launchers", "启动.command")
        contents = launcher.read_text(encoding="utf-8")

        bound = contents.index('export WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH="$app_path"')
        signed = contents.index("scripts/resign_wechat.py")
        refresh = contents.index('if [[ "$REFRESH_DATA_SOURCE" -eq 1 ]]')
        execute_refresh = contents.index("scripts/refresh_data_source.py", refresh)
        self.assertLess(bound, signed)
        self.assertLess(signed, refresh)
        self.assertLess(refresh, execute_refresh)
        self.assertNotIn('open "$app_path"', contents)

    def test_launcher_has_no_legacy_key_cache_rewrite_block(self):
        launcher = repo_path("launchers", "启动.command")
        contents = launcher.read_text(encoding="utf-8")

        self.assertNotIn("os.remove(keys_f)", contents)
        self.assertNotIn("extract_keys.log", contents)


if __name__ == "__main__":
    unittest.main()
