"""One read-only codesign predicate for startup, health, and explicit re-sign.

No discovery, privilege escalation, process control, or source reads occur here.
A caller supplies the already selected app path. A strict-verified bundle is
acceptable when the CS_RUNTIME bit is absent and the signature is either the
legacy ad-hoc form or anchored to the managed stable signing identity; explicit
re-sign verification passes ``expected_certificate_root`` so ad-hoc output and
foreign certificates are both rejected. This is a macOS adapter; its subprocess
runner is injectable so signature parsing can be tested off-device.
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


def inspect_wechat_signature(
    app_path,
    *,
    runner=None,
    expected_certificate_root=None,
) -> WeChatSignatureStatus:
    """Require strict verification, an acceptable identity, and no runtime flag."""
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
        signatures = [line.strip() for line in detail.splitlines()
                      if line.strip().startswith("Signature=")]
        if signatures == ["Signature=adhoc"]:
            if expected_certificate_root is None:
                return WeChatSignatureStatus("wechat_signature_valid")
            return WeChatSignatureStatus("wechat_signature_not_stable_identity")
        if len(signatures) > 1:
            return WeChatSignatureStatus("wechat_signature_invalid")
        root = expected_certificate_root
        if root is None:
            from .wechat_signing_identity import managed_certificate_root_hash
            root = managed_certificate_root_hash(runner=run)
        if not root:
            return WeChatSignatureStatus("wechat_signature_not_stable_identity")
        designated = run(
            ["/usr/bin/codesign", "--display", "-r-", app_path],
            capture_output=True, text=True, timeout=60,
        )
        if designated.returncode != 0:
            return WeChatSignatureStatus("wechat_signature_invalid")
        requirement = "\n".join(
            (designated.stdout or "", designated.stderr or "")
        ).lower()
        anchor = f'certificate root = h"{str(root).lower()}"'
        if anchor not in requirement:
            return WeChatSignatureStatus("wechat_signature_not_stable_identity")
        return WeChatSignatureStatus("wechat_signature_valid")
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, AttributeError):
        return WeChatSignatureStatus("wechat_signature_unavailable")
