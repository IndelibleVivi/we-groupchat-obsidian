import sys
import types
import unittest
from unittest.mock import patch

from ai.openai_provider import OpenAIProvider
from core.api_errors import is_retryable_ai_error


class OpenAIProviderTests(unittest.TestCase):
    def test_configures_bounded_single_attempt_client_requests(self):
        captured = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_module = types.SimpleNamespace(OpenAI=FakeOpenAI)
        with patch.dict(sys.modules, {"openai": fake_module}):
            OpenAIProvider("test-key", model="test-model")

        self.assertEqual(captured["timeout"], 45.0)
        self.assertEqual(captured["max_retries"], 0)

    def test_accepts_an_explicit_request_timeout(self):
        captured = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_module = types.SimpleNamespace(OpenAI=FakeOpenAI)
        with patch.dict(sys.modules, {"openai": fake_module}):
            OpenAIProvider(
                "test-key",
                model="test-model",
                timeout_seconds=90,
            )

        self.assertEqual(captured["timeout"], 90.0)

    def test_empty_completion_is_a_retryable_provider_failure(self):
        class FakeCompletions:
            @staticmethod
            def create(**_kwargs):
                message = types.SimpleNamespace(content="")
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(message=message)],
                )

        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=FakeCompletions()),
        )
        provider.model = "test-model"

        with self.assertRaises(RuntimeError) as caught:
            provider.summarize("test prompt")

        self.assertIn("空响应", str(caught.exception))
        self.assertTrue(is_retryable_ai_error(caught.exception))

    def test_can_disable_thinking_for_structured_monitor_requests(self):
        captured = {}

        class FakeCompletions:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)
                message = types.SimpleNamespace(content='{"match": false}')
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(message=message)],
                )

        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=FakeCompletions()),
        )
        provider.model = "deepseek-v4-flash"
        provider.thinking = False

        provider.summarize("test prompt")

        self.assertEqual(
            captured["extra_body"],
            {"thinking": {"type": "disabled"}},
        )


if __name__ == "__main__":
    unittest.main()
