"""Exact-target macOS WeChat re-sign orchestration.

This module performs no work at import time.  The mutation path is entered only
by the explicit operator script and keeps one bundle/process identity bound
through privilege acquisition, termination, signing, verification and reopen.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import threading
import time

from . import key_extractor


class WeChatResignError(RuntimeError):
    def __init__(self, code: str):
        self.code = str(code or "wechat_resign_failed")
        super().__init__(self.code)


@dataclass(frozen=True)
class WeChatBundleIdentity:
    app_path: str
    bundle_identifier: str
    executable_path: str
    executable_name: str
    version: str
    build: str
    bundle_identity: tuple[int, int]
    executable_identity: tuple[int, int, int, int]
    info_plist_sha256: str


@dataclass(frozen=True)
class WeChatResignTarget:
    bundle: WeChatBundleIdentity
    running: key_extractor.ScanTarget | None


@dataclass(frozen=True)
class WeChatResignOutcome:
    bundle: WeChatBundleIdentity
    running: key_extractor.ScanTarget


def _read_regular(path: str) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode):
            raise WeChatResignError("wechat_target_invalid")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def inspect_wechat_bundle(app_path) -> WeChatBundleIdentity:
    app = os.path.realpath(os.path.expanduser(os.fspath(app_path)))
    try:
        bundle_stat = os.stat(app, follow_symlinks=False)
        if not stat.S_ISDIR(bundle_stat.st_mode):
            raise WeChatResignError("wechat_target_invalid")
        info_path = os.path.join(app, "Contents", "Info.plist")
        info_bytes = _read_regular(info_path)
        info = plistlib.loads(info_bytes)
        bundle_id = str(info.get("CFBundleIdentifier") or "")
        executable_name = str(info.get("CFBundleExecutable") or "")
        version = str(info.get("CFBundleShortVersionString") or "").strip()
        build = str(info.get("CFBundleVersion") or "").strip()
        if (
            bundle_id != "com.tencent.xinWeChat"
            or executable_name != "WeChat"
            or not version
            or not build
        ):
            raise WeChatResignError("wechat_target_invalid")
        executable = os.path.realpath(
            os.path.join(app, "Contents", "MacOS", executable_name)
        )
        expected_parent = os.path.realpath(os.path.join(app, "Contents", "MacOS"))
        if os.path.dirname(executable) != expected_parent:
            raise WeChatResignError("wechat_target_invalid")
        executable_stat = os.stat(executable, follow_symlinks=False)
        if not stat.S_ISREG(executable_stat.st_mode):
            raise WeChatResignError("wechat_target_invalid")
    except WeChatResignError:
        raise
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise WeChatResignError("wechat_target_invalid") from exc
    return WeChatBundleIdentity(
        app,
        bundle_id,
        executable,
        executable_name,
        version,
        build,
        (int(bundle_stat.st_dev), int(bundle_stat.st_ino)),
        (
            int(executable_stat.st_dev),
            int(executable_stat.st_ino),
            int(executable_stat.st_size),
            int(executable_stat.st_mtime_ns),
        ),
        hashlib.sha256(info_bytes).hexdigest(),
    )


def resolve_wechat_resign_target(
    app_path,
    *,
    explicit_target: bool = False,
) -> WeChatResignTarget:
    bundle = inspect_wechat_bundle(app_path)
    running = key_extractor.get_wechat_scan_targets()
    if not explicit_target and len(running) > 1:
        raise WeChatResignError("wechat_target_ambiguous")
    matching = tuple(
        target
        for target in running
        if target.app_path == bundle.app_path
        and target.executable_path == bundle.executable_path
    )
    if len(matching) > 1:
        raise WeChatResignError("wechat_target_ambiguous")
    if running and not matching and not explicit_target:
        raise WeChatResignError("wechat_target_ambiguous")
    return WeChatResignTarget(bundle, matching[0] if matching else None)


def revalidate_wechat_resign_target(
    target: WeChatResignTarget,
    *,
    explicit_target: bool = False,
) -> WeChatResignTarget:
    if inspect_wechat_bundle(target.bundle.app_path) != target.bundle:
        raise WeChatResignError("wechat_target_changed")
    current = resolve_wechat_resign_target(
        target.bundle.app_path,
        explicit_target=explicit_target,
    )
    if current.running != target.running:
        raise WeChatResignError("wechat_target_changed")
    return current


def terminate_wechat_target(
    target: key_extractor.ScanTarget | None,
    *,
    timeout_seconds: float = 15.0,
) -> None:
    if target is None:
        return
    try:
        from AppKit import NSRunningApplication
        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(
            target.pid
        )
        if running is None or key_extractor.get_wechat_scan_target(target.pid) != target:
            raise WeChatResignError("wechat_target_changed")
        if not running.terminate():
            raise WeChatResignError("wechat_target_quit_rejected")
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            if running.isTerminated():
                return
            time.sleep(0.1)
    except WeChatResignError:
        raise
    except Exception as exc:
        raise WeChatResignError("wechat_target_quit_failed") from exc
    raise WeChatResignError("wechat_target_quit_timeout")


def _same_post_sign_bundle(
    expected: WeChatBundleIdentity,
    observed: WeChatBundleIdentity,
) -> bool:
    return (
        expected.app_path == observed.app_path
        and expected.bundle_identifier == observed.bundle_identifier
        and expected.executable_path == observed.executable_path
        and expected.executable_name == observed.executable_name
        and expected.version == observed.version
        and expected.build == observed.build
        and expected.bundle_identity == observed.bundle_identity
        and expected.info_plist_sha256 == observed.info_plist_sha256
    )


def verify_resigned_wechat_bundle(
    expected: WeChatBundleIdentity,
    *,
    runner=subprocess.run,
) -> WeChatBundleIdentity:
    from .wechat_signature import inspect_wechat_signature
    status = inspect_wechat_signature(expected.app_path, runner=runner)
    if not status.ok:
        raise WeChatResignError(status.code)
    observed = inspect_wechat_bundle(expected.app_path)
    if not _same_post_sign_bundle(expected, observed):
        raise WeChatResignError("wechat_target_changed")
    return observed


def reopen_wechat_target(
    expected: WeChatBundleIdentity,
    *,
    previous: key_extractor.ScanTarget | None,
    timeout_seconds: float = 30.0,
) -> key_extractor.ScanTarget:
    try:
        from AppKit import NSWorkspace, NSWorkspaceOpenConfiguration
        from Foundation import NSURL

        completed = threading.Event()
        outcome = {}

        def callback(application, error):
            outcome["application"] = application
            outcome["error"] = error
            completed.set()

        url = NSURL.fileURLWithPath_(expected.app_path)
        configuration = NSWorkspaceOpenConfiguration.configuration()
        NSWorkspace.sharedWorkspace().openApplicationAtURL_configuration_completionHandler_(
            url,
            configuration,
            callback,
        )
        if not completed.wait(max(0.1, float(timeout_seconds))):
            raise WeChatResignError("wechat_reopen_timeout")
        application = outcome.get("application")
        if application is None or outcome.get("error") is not None:
            raise WeChatResignError("wechat_reopen_failed")
        target = key_extractor.get_wechat_scan_target(int(application.processIdentifier()))
    except WeChatResignError:
        raise
    except Exception as exc:
        raise WeChatResignError("wechat_reopen_failed") from exc
    if (
        target is None
        or target.app_path != expected.app_path
        or target.executable_path != expected.executable_path
        or target.build_identity[:2] != (expected.version, expected.build)
        or (previous is not None and target.launched_at <= previous.launched_at)
    ):
        raise WeChatResignError("wechat_reopen_identity_mismatch")
    exact = key_extractor.select_wechat_scan_target(expected.app_path)
    if exact != target:
        raise WeChatResignError("wechat_reopen_identity_mismatch")
    return target


def resign_wechat_bundle(
    app_path,
    *,
    allow: bool,
    explicit_target: bool = False,
    runner=subprocess.run,
) -> WeChatResignOutcome:
    if not allow:
        raise WeChatResignError("wechat_resign_not_authorized")
    target = resolve_wechat_resign_target(
        app_path,
        explicit_target=explicit_target,
    )
    privilege = runner(["sudo", "-v"], timeout=60)
    if privilege.returncode:
        raise WeChatResignError("wechat_privilege_unavailable")
    target = revalidate_wechat_resign_target(
        target,
        explicit_target=explicit_target,
    )
    terminate_wechat_target(target.running)
    if inspect_wechat_bundle(target.bundle.app_path) != target.bundle:
        raise WeChatResignError("wechat_target_changed")
    try:
        signed = runner(
            [
                "sudo", "-n", "codesign", "--force", "--deep", "--sign", "-",
                target.bundle.app_path,
            ],
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise WeChatResignError("wechat_resign_outcome_ambiguous") from exc
    if signed.returncode:
        raise WeChatResignError("wechat_resign_failed")
    verified = verify_resigned_wechat_bundle(target.bundle, runner=runner)
    reopened = reopen_wechat_target(verified, previous=target.running)
    return WeChatResignOutcome(verified, reopened)
