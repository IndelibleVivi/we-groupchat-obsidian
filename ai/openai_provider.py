"""OpenAI-compatible API provider (supports OpenAI, DeepSeek, Qwen, etc.)."""
from .base import AIProvider
from core.api_errors import normalize_ai_error


class OpenAIProvider(AIProvider):
    def __init__(
        self,
        api_key,
        model="gpt-4o-mini",
        base_url=None,
        thinking=None,
        timeout_seconds=45.0,
        thinking_parameter="thinking",
    ):
        from openai import OpenAI
        # TopicMonitor already owns one bounded retry and persistent backoff.
        # Avoid SDK-level retries turning a transient provider stall into a
        # multi-minute monitor lock that blocks later checkpoint pages.
        kwargs = {
            "api_key": api_key,
            "timeout": float(timeout_seconds),
            "max_retries": 0,
        }
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model
        self.thinking = thinking
        self.thinking_parameter = thinking_parameter

    def summarize(self, prompt: str) -> str:
        print(f"[ai] 调用 {self.model}, prompt 长度: {len(prompt)} 字符...")
        try:
            request = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4096,
            }
            thinking = getattr(self, "thinking", None)
            if thinking is not None:
                if getattr(self, "thinking_parameter", "thinking") == "enable_thinking":
                    request["extra_body"] = {"enable_thinking": bool(thinking)}
                else:
                    request["extra_body"] = {
                        "thinking": {"type": "enabled" if thinking else "disabled"},
                    }
            response = self.client.chat.completions.create(**request)
            result = response.choices[0].message.content
            if not isinstance(result, str) or not result.strip():
                raise RuntimeError("AI API 返回空响应，请稍后重试")
            print(f"[ai] 返回 {len(result)} 字符")
            return result
        except Exception as e:
            raise RuntimeError(normalize_ai_error(e, "AI")) from None
