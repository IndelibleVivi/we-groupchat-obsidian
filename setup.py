"""py2app 打包配置（可选；默认仍推荐源码目录 + 启动.command 分发）"""
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import sysconfig

APP = ["app.py"]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "resources/app_icon.icns",
    "plist": {
        "CFBundleIdentifier": "io.github.indeliblevivi.we-groupchat-obsidian",
        "CFBundleName": "WeGroupchatObsidian",
        "CFBundleDisplayName": "微信总结",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement": True,  # 不在 Dock 显示图标
        "NSAppDataUsageDescription": (
            "读取本机微信消息数据库来生成你选择的群聊总结与资源索引；只有显式开启文件解析时才读取附件缓存。"
        ),
        "NSDocumentsFolderUsageDescription": (
            "把生成的总结与资源索引写入你选择的 Documents 或 Obsidian 目录。"
        ),
        "NSFileProviderDomainUsageDescription": (
            "把你显式选择的资源目录交给已挂载的云盘客户端同步。"
        ),
    },
    "packages": [
        "rumps",
        "Crypto",
        "zstandard",
        "anthropic",
        "openai",
        "requests",
        "scripts",
        "objc",
        "ai",
        "core",
        "ui",
    ],
    "resources": ["c_src", "使用说明.txt"],
}


def finalize_alias_bundle(bundle, *, config_target=None, runner=subprocess.run):
    """Repair py2app's Python 3.13 alias links and verify the local bundle."""
    bundle = Path(bundle)
    resources = bundle / "Contents" / "Resources"
    python_lib = resources / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"

    # py2app 0.28.10 creates ../../site.pyc but omits its target on
    # Python 3.13. Compile the bundled alias bootstrap at that target.
    site_source = resources / "site.py"
    site_target = resources / "site.pyc"
    py_compile.compile(str(site_source), cfile=str(site_target), doraise=True)

    # A venv has no lib/pythonX.Y/config directory. Point the alias at the
    # interpreter's real, machine-local config directory instead.
    config_target = Path(str(
        config_target or sysconfig.get_config_var("LIBPL") or ""
    ))
    if not config_target.is_dir():
        raise RuntimeError("Python build config directory is unavailable")
    config_link = python_lib / "config"
    if config_link.is_symlink() or config_link.exists():
        config_link.unlink()
    os.symlink(config_target, config_link)

    broken_links = [
        str(path.relative_to(bundle))
        for path in bundle.rglob("*")
        if path.is_symlink() and not path.exists()
    ]
    if broken_links:
        raise RuntimeError(
            "Alias bundle contains dangling links: " + ", ".join(broken_links)
        )

    runner(
        ["codesign", "--force", "--deep", "--sign", "-", str(bundle)],
        check=True,
    )
    runner(["codesign", "--verify", str(bundle)], check=True)


def validated_alias_py2app_command():
    """Load the optional build dependency only for an actual py2app command."""
    from py2app.build_app import py2app as py2app_command

    class ValidatedAliasPy2App(py2app_command):
        """Repair alias links before the final local signature check."""

        def run(self):
            super().run()
            if not self.alias:
                return

            finalize_alias_bundle(Path(self.dist_dir) / "WeGroupchatObsidian.app")

    return ValidatedAliasPy2App


def main():
    from setuptools import setup

    setup(
        app=APP,
        name="WeGroupchatObsidian",
        options={"py2app": OPTIONS},
        cmdclass={"py2app": validated_alias_py2app_command()},
    )


if __name__ == "__main__":
    main()
