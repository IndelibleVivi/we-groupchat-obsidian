"""User-friendly handling for AI API errors."""
import re


RETRYABLE_STATUS_CODES = {"500", "502", "503", "504"}

QUOTA_EXHAUSTED_MARKERS = (
    "credits exhausted",
    "insufficient balance",
    "insufficient_quota",
    "余额不足",
    "额度已用完",
    "额度不足",
)


def _strip_html(text):
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def normalize_ai_error(error, provider="AI"):
    """Convert SDK/network errors into short messages suitable for notifications."""
    raw = str(error or "")
    text = _strip_html(raw)
    lower = text.lower()

    # Explicit credit/balance exhaustion wins over proxy 401/429/503 envelopes.
    # Bare "quota exceeded" also describes per-minute limits, not just billing.
    if any(marker in lower for marker in QUOTA_EXHAUSTED_MARKERS):
        return f"{provider} 额度已用完，请充值或等待额度重置后再试"
    if "429" in text or "rate" in lower:
        return f"{provider} API 请求频率超限，请稍后再试"
    if "401" in text or "auth" in lower or ("invalid" in lower and "key" in lower):
        return f"{provider} API Key 无效或已过期，请在设置中重新配置"
    if "timeout" in lower or "timed out" in lower:
        return f"{provider} API 请求超时，请检查网络连接后重试"
    if "connect" in lower or "connection" in lower:
        return f"无法连接 {provider} API 服务器，请检查网络连接"
    if "empty response" in lower or "空响应" in text:
        return f"{provider} API 返回空响应，请稍后重试"
    if any(code in text for code in RETRYABLE_STATUS_CODES) or "bad gateway" in lower:
        return f"{provider} API 服务临时不可用，请稍后再试"

    return f"{provider} 调用失败: {text or raw}"


def is_retryable_ai_error(error):
    """Return True for transient server/network errors worth retrying briefly."""
    text = _strip_html(str(error or "")).lower()
    return (
        any(code in text for code in RETRYABLE_STATUS_CODES)
        or "bad gateway" in text
        or "timeout" in text
        or "timed out" in text
        or "connect" in text
        or "connection" in text
        or "network" in text
        or "请求超时" in text
        or "无法连接" in text
        or "网络连接" in text
        or "临时不可用" in text
        or "empty response" in text
        or "空响应" in text
    )
