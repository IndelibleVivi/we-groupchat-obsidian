"""Claude API provider."""
from .base import AIProvider, OutputTruncatedError
from core.api_errors import normalize_ai_error


class ClaudeProvider(AIProvider):
    def __init__(self, api_key, model="claude-sonnet-4-20250514"):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
        self.model = model
        self.max_output_tokens = 4096

    def summarize(self, prompt: str) -> str:
        print(f"[ai] 调用 {self.model}, prompt 长度: {len(prompt)} 字符...")
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=getattr(self, "max_output_tokens", 4096),
                messages=[{"role": "user", "content": prompt}],
            )
            if getattr(response, "stop_reason", None) == "max_tokens":
                raise OutputTruncatedError()
            result = response.content[0].text
            print(f"[ai] 返回 {len(result)} 字符")
            return result
        except OutputTruncatedError:
            raise
        except Exception as e:
            raise RuntimeError(normalize_ai_error(e, "Claude")) from None
