"""One read-only codesign predicate for startup, health, and explicit re-sign.

No discovery, privilege escalation, process control, or source reads occur here.
A caller supplies the already selected app path. This is a macOS adapter; its
subprocess runner is injectable so signature parsing can be tested off-device.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import subprocess


@dataclass(frozen=True)
class WeChatSignatureStatus:
    code: str

    @property
    def ok(self) -> bool:
        return self.code == "wechat_signature_valid"


def inspect_wechat_signature(app_path, *, runner=None) -> WeChatSignatureStatus:
    """Require strict verification, ad-hoc identity, and no runtime flag."""
    if not isinstance(app_path, str) or not app_path or "\0" in app_path:
        return WeChatSignatureStatus("wechat_signature_unavailable")
    run = runner if runner is not None else subprocess.run
    try:
        verified = run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", app_path],
            capture_output=True, text=True, timeout=60,
        )
        if verified.returncode != 0:
            return WeChatSignatureStatus("wechat_signature_invalid")
        displayed = run(
            ["/usr/bin/codesign", "--display", "--verbose=4", app_path],
            capture_output=True, text=True, timeout=60,
        )
        if displayed.returncode != 0:
            return WeChatSignatureStatus("wechat_signature_invalid")
        detail = "\n".join((displayed.stdout or "", displayed.stderr or ""))
        signatures = [line.strip() for line in detail.splitlines()
                      if line.strip().startswith("Signature=")]
        if signatures != ["Signature=adhoc"]:
            return WeChatSignatureStatus("wechat_signature_not_adhoc")
        directories = [line.strip() for line in detail.splitlines()
                       if line.strip().startswith("CodeDirectory ")]
        if not directories:
            return WeChatSignatureStatus("wechat_signature_invalid")
        for directory in directories:
            flags = re.search(r"\bflags=(0x[0-9a-fA-F]+)\b", directory)
            if flags is None:
                return WeChatSignatureStatus("wechat_signature_invalid")
            # CS_RUNTIME is 0x10000; inspect CodeDirectory flags only, never
            # a path or unrelated codesign text that happens to say runtime.
            if int(flags.group(1), 16) & 0x10000:
                return WeChatSignatureStatus("wechat_hardened_runtime_still_enabled")
        return WeChatSignatureStatus("wechat_signature_valid")
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, AttributeError):
        return WeChatSignatureStatus("wechat_signature_unavailable")
