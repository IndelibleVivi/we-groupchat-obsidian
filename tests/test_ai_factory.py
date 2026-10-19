import unittest
from unittest.mock import patch

from ai.factory import create_provider
from core.config import ConfigError, _sanitize_config


class AIProviderFactoryTests(unittest.TestCase):
    def test_custom_streaming_is_strictly_opt_in_without_changing_old_snapshots(self):
        config = {"ai_provider": "custom", "ai_model": "synthetic", "ai_base_url": "http://127.0.0.1:12345/v1"}
        self.assertNotIn("custom_streaming", _sanitize_config(config))
        with patch("ai.factory.load_key", return_value="synthetic-key"), \
                patch("ai.openai_provider.OpenAIProvider") as provider:
            create_provider(config)
            self.assertNotIn("streaming", provider.call_args.kwargs)
            for value in (True, False):
                selected = _sanitize_config(dict(config, custom_streaming=value))
                create_provider(selected)
                self.assertIs(provider.call_args.kwargs["streaming"], value)
            for value in (1, 0, None, "true", []):
                with self.subTest(value=value):
                    with self.assertRaises(ConfigError):
                        _sanitize_config(dict(config, custom_streaming=value))
                    with self.assertRaises(ValueError):
                        create_provider(dict(config, custom_streaming=value))

    def test_custom_thinking_is_explicit_and_preserves_other_custom_endpoints(self):
        config = {"ai_provider": "custom", "ai_model": "synthetic",
                  "ai_base_url": "http://127.0.0.1:12345/v1", "ai_thinking": False}
        with patch("ai.factory.load_key", return_value="synthetic-key"), \
                patch("ai.openai_provider.OpenAIProvider") as provider:
            create_provider(config)
            self.assertNotIn("thinking", provider.call_args.kwargs)
            create_provider(dict(config, custom_thinking_parameter="thinking"))
            self.assertIs(provider.call_args.kwargs["thinking"], False)
            self.assertEqual(provider.call_args.kwargs["thinking_parameter"], "thinking")
            create_provider(dict(config, custom_thinking_parameter="enable_thinking"))
            self.assertEqual(provider.call_args.kwargs["thinking_parameter"], "enable_thinking")
            for invalid in ([], {}, True, None, "guess"):
                with self.subTest(invalid=invalid):
                    value = dict(config, custom_thinking_parameter=invalid)
                    with self.assertRaises(ValueError):
                        create_provider(value)
                    with self.assertRaises(ConfigError):
                        _sanitize_config(value)

    def test_ollama_does_not_open_credential_store(self):
        with patch("ai.factory.load_key", side_effect=AssertionError("credential access")), \
             patch("ai.ollama_provider.OllamaProvider") as provider:
            self.assertIs(create_provider({"ai_provider": "ollama"}), provider.return_value)

    def test_plaintext_config_key_is_not_a_fallback(self):
        with patch("ai.factory.load_key", return_value=None):
            with self.assertRaises(ValueError):
                create_provider({"ai_provider": "openai", "ai_api_key": "plaintext-fixture"})

    def test_deepseek_uses_v4_flash_when_model_is_unset(self):
        with (
            patch("ai.factory.load_key", return_value="test-api-key"),
            patch("ai.openai_provider.OpenAIProvider") as provider_class,
        ):
            provider = create_provider({"ai_provider": "deepseek"})

        self.assertIs(provider, provider_class.return_value)
        provider_class.assert_called_once_with(
            api_key="test-api-key",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            thinking=None,
        )

    def test_custom_provider_accepts_timeout_without_guessing_thinking_schema(self):
        with (
            patch("ai.factory.load_key", return_value="test-api-key"),
            patch("ai.openai_provider.OpenAIProvider") as provider_class,
        ):
            provider = create_provider({
                "ai_provider": "custom",
                "ai_model": "test-model",
                "ai_base_url": "https://example.invalid/v1",
                "ai_thinking": False,
                "ai_timeout_seconds": 90,
            })

        self.assertIs(provider, provider_class.return_value)
        provider_class.assert_called_once_with(
            api_key="test-api-key",
            model="test-model",
            base_url="https://example.invalid/v1",
            timeout_seconds=90,
        )

    def test_qwen_accepts_regional_endpoint_and_structured_monitor_options(self):
        with (
            patch("ai.factory.load_key", return_value="test-api-key"),
            patch("ai.openai_provider.OpenAIProvider") as provider_class,
        ):
            provider = create_provider({
                "ai_provider": "qwen",
                "ai_model": "qwen-test",
                "ai_base_url": "https://example.invalid/v1",
                "ai_thinking": False,
                "ai_timeout_seconds": 90,
            })

        self.assertIs(provider, provider_class.return_value)
        provider_class.assert_called_once_with(
            api_key="test-api-key",
            model="qwen-test",
            base_url="https://example.invalid/v1",
            thinking=False,
            timeout_seconds=90,
            thinking_parameter="enable_thinking",
        )


if __name__ == "__main__":
    unittest.main()
