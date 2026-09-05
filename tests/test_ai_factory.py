import unittest
from unittest.mock import patch

from ai.factory import create_provider


class AIProviderFactoryTests(unittest.TestCase):
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

    def test_custom_provider_accepts_structured_monitor_options(self):
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
            thinking=False,
            timeout_seconds=90,
        )


if __name__ == "__main__":
    unittest.main()
