"""OpenAI-compatible API provider (supports OpenAI, DeepSeek, Qwen, etc.)."""
from .base import AIProvider, OutputTruncatedError
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
        streaming=False,
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
        self.max_output_tokens = 4096
        self.json_object_output = False
        self.thinking = thinking
        self.thinking_parameter = thinking_parameter
        if type(streaming) is not bool:
            raise ValueError("streaming must be boolean")
        self.streaming = streaming
        _reasoning_off = {
            "https://api.commandcode.ai/provider/v1": "off",
            "https://amr-link.open-design.ai/v1": "none",
        }
        base = str(base_url or "").rstrip("/")
        self._json_object_gateway = base in _reasoning_off
        self._reasoning_off_value = _reasoning_off.get(base)

    def summarize(self, prompt: str) -> str:
        print(f"[ai] 调用 {self.model}, prompt 长度: {len(prompt)} 字符...")
        try:
            request = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": getattr(self, "max_output_tokens", 4096),
            }
            if getattr(self, "json_object_output", False):
                request["response_format"] = {"type": "json_object"}
            thinking = getattr(self, "thinking", None)
            if thinking is not None:
                if getattr(self, "thinking_parameter", "thinking") == "enable_thinking":
                    request["extra_body"] = {"enable_thinking": bool(thinking)}
                else:
                    request["extra_body"] = {
                        "thinking": {"type": "enabled" if thinking else "disabled"},
                    }
                    # Gateways that keep their own reasoning default despite
                    # DeepSeek's thinking field accept an explicit off value
                    # ("off" on commandcode, "none" on the amr-link relay).
                    # Preserve the caller's explicit off intent and token cap.
                    if not thinking and getattr(self, "_reasoning_off_value", None):
                        request["reasoning_effort"] = self._reasoning_off_value
            if getattr(self, "streaming", False):
                parts, finished = [], False
                with self.client.chat.completions.create(**request, stream=True) as chunks:
                    for chunk in chunks:
                        if not chunk.choices:
                            continue
                        choice = chunk.choices[0]
                        reason = getattr(choice, "finish_reason", None)
                        if reason in ("length", "repetition_truncation"):
                            raise OutputTruncatedError()
                        if reason is not None:
                            if reason != "stop":
                                raise RuntimeError("AI API 返回未完成响应，请稍后重试")
                            finished = True
                        content = getattr(choice.delta, "content", None)
                        if content is not None:
                            if not isinstance(content, str):
                                raise RuntimeError("AI API 返回无效响应，请稍后重试")
                            parts.append(content)
                        if finished:
                            break
                if not finished:
                    raise RuntimeError("AI API 返回未完成响应，请稍后重试")
                result = "".join(parts)
            else:
                response = self.client.chat.completions.create(**request)
                if getattr(response.choices[0], "finish_reason", None) in ("length", "repetition_truncation"):
                    raise OutputTruncatedError()
                result = response.choices[0].message.content
            if not isinstance(result, str) or not result.strip():
                raise RuntimeError("AI API 返回空响应，请稍后重试")
            print(f"[ai] 返回 {len(result)} 字符")
            return result
        except OutputTruncatedError:
            raise
        except Exception as e:
            raise RuntimeError(normalize_ai_error(e, "AI")) from None
