import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from ai.openai_provider import OpenAIProvider
from ai.base import OutputTruncatedError
from core.api_errors import is_retryable_ai_error


class OpenAIProviderTests(unittest.TestCase):
    @staticmethod
    def streamed(chunks):
        captured = {}
        @contextmanager
        def create(**kwargs):
            captured.update(kwargs)
            try:
                yield iter(chunks)
            finally:
                captured["closed"] = True
        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider.model = "synthetic"
        provider.streaming = True
        provider.json_object_output = True
        provider.client = types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create)))
        return provider, captured

    @staticmethod
    def chunk(content=None, reason=None):
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            delta=types.SimpleNamespace(content=content), finish_reason=reason)])

    def test_stream_assembles_only_complete_content_and_closes_response(self):
        chunks = [types.SimpleNamespace(choices=[]), self.chunk('{"字":'),
                  self.chunk('"值"}'), self.chunk(reason="stop")]
        provider, captured = self.streamed(chunks)
        self.assertEqual(provider.summarize("synthetic prompt"), '{"字":"值"}')
        self.assertIs(captured["stream"], True)
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertTrue(captured["closed"])

    def test_incomplete_or_failed_stream_never_returns_a_partial_result(self):
        def disconnected():
            yield self.chunk("partial private text")
            raise ConnectionError("synthetic disconnect")
        for chunks in ([self.chunk("partial private text")], disconnected(),
                       [self.chunk("partial private text"), self.chunk(reason="content_filter")]):
            with self.subTest(chunks=type(chunks).__name__):
                provider, captured = self.streamed(chunks)
                with self.assertRaises(RuntimeError) as failure:
                    provider.summarize("synthetic prompt")
                self.assertNotIn("private text", str(failure.exception))
                self.assertTrue(captured["closed"])

    def test_known_truncated_stream_never_returns_partial_output_and_closes(self):
        for reason in ("length", "repetition_truncation"):
            with self.subTest(reason=reason):
                provider, captured = self.streamed([
                    self.chunk("partial"), self.chunk("discarded tail", reason=reason)])
                with self.assertRaises(OutputTruncatedError):
                    provider.summarize("synthetic prompt")
                self.assertTrue(captured["closed"])

    def test_known_truncated_completion_never_returns_partial_output(self):
        for reason in ("length", "repetition_truncation"):
            with self.subTest(reason=reason):
                response = types.SimpleNamespace(choices=[types.SimpleNamespace(
                    finish_reason=reason,
                    message=types.SimpleNamespace(content="partial private text"))])
                provider = OpenAIProvider.__new__(OpenAIProvider)
                provider.model = "synthetic"
                create = Mock(return_value=response)
                provider.client = types.SimpleNamespace(chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=create)))
                with self.assertRaises(OutputTruncatedError):
                    provider.summarize("synthetic prompt")
                create.assert_called_once()

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

    def test_can_disable_qwen_thinking_with_enable_thinking_parameter(self):
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
        provider.model = "qwen-test"
        provider.thinking = False
        provider.thinking_parameter = "enable_thinking"

        provider.summarize("test prompt")

        self.assertEqual(captured["extra_body"], {"enable_thinking": False})

    def test_exact_gateway_thinking_off_uses_its_declared_effort_value(self):
        for endpoint in ("https://api.commandcode.ai/provider/v1/",
                         "https://amr-link.open-design.ai/v1/",
                         "https://api.deepseek.com", "https://api.commandcode.ai.example.test/provider/v1",
                         "https://amr-link.open-design.ai.example.test/v1"):
            for thinking in (False, True, None):
                with self.subTest(endpoint=endpoint, thinking=thinking):
                    captured = {}
                    class FakeOpenAI:
                        def __init__(self, **kwargs):
                            self.chat = types.SimpleNamespace(completions=self)
                        def create(self, **kwargs):
                            captured.update(kwargs)
                            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                                finish_reason="stop", message=types.SimpleNamespace(content='{"ready":true}'))])
                    with patch.dict(sys.modules, {"openai": types.SimpleNamespace(OpenAI=FakeOpenAI)}):
                        provider = OpenAIProvider("synthetic-key", model="deepseek/deepseek-v4.1-flash",
                                                  base_url=endpoint, thinking=thinking)
                    provider.max_output_tokens = 16384
                    provider.json_object_output = thinking is False
                    provider.summarize("synthetic prompt")
                    self.assertEqual(captured["max_tokens"], 16384)
                    if thinking is False:
                        self.assertEqual(captured["response_format"], {"type": "json_object"})
                    else:
                        self.assertNotIn("response_format", captured)
                    effort = {"https://api.commandcode.ai/provider/v1/": "off",
                              "https://amr-link.open-design.ai/v1/": "none"}.get(endpoint)
                    self.assertEqual(provider._json_object_gateway, effort is not None)
                    if effort is not None and thinking is False:
                        self.assertEqual(captured["reasoning_effort"], effort)
                        self.assertEqual(captured["extra_body"], {"thinking": {"type": "disabled"}})
                    else:
                        self.assertNotIn("reasoning_effort", captured)


if __name__ == "__main__":
    unittest.main()
