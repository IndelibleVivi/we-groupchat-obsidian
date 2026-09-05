"""AI provider factory."""
from core.keychain import load_key


def _get_api_key(config):
    """Get API key: prefer Keychain, fall back to config."""
    key = load_key("ai-api-key")
    if key:
        return key
    return config.get("ai_api_key", "")


def create_provider(config):
    """Create AI provider based on config."""
    provider = config.get("ai_provider", "qwen")
    api_key = _get_api_key(config)
    model = config.get("ai_model", "")

    if provider == "claude":
        from .claude_provider import ClaudeProvider
        if not api_key:
            raise ValueError("请先设置 API Key（点击菜单 → 设置 → 设置 API Key）")
        return ClaudeProvider(
            api_key=api_key,
            model=model or "claude-sonnet-4-20250514",
        )

    elif provider == "openai":
        from .openai_provider import OpenAIProvider
        if not api_key:
            raise ValueError("请先设置 API Key")
        return OpenAIProvider(
            api_key=api_key,
            model=model or "gpt-4o-mini",
        )

    elif provider == "deepseek":
        from .openai_provider import OpenAIProvider
        if not api_key:
            raise ValueError("请先设置 DeepSeek API Key")
        return OpenAIProvider(
            api_key=api_key,
            model=model or "deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            thinking=config.get("ai_thinking"),
        )

    elif provider == "qwen":
        from .openai_provider import OpenAIProvider
        if not api_key:
            raise ValueError("请先设置通义千问 API Key（dashscope.console.aliyun.com）")
        return OpenAIProvider(
            api_key=api_key,
            model=model or "qwen-turbo",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

    elif provider == "ollama":
        from .ollama_provider import OllamaProvider
        return OllamaProvider(
            model=model or config.get("ollama_model", "qwen3:8b"),
            base_url=config.get("ollama_url", "http://localhost:11434"),
        )

    elif provider == "custom":
        from .openai_provider import OpenAIProvider
        if not model:
            raise ValueError("自定义提供者需要设置模型名称（设置 → AI 模型）")
        if not config.get("ai_base_url"):
            raise ValueError("自定义提供者需要设置 Base URL（设置 → AI Base URL）")
        return OpenAIProvider(
            api_key=api_key,
            model=model,
            base_url=config.get("ai_base_url", ""),
            thinking=config.get("ai_thinking"),
            timeout_seconds=config.get("ai_timeout_seconds", 45.0),
        )

    else:
        raise ValueError(f"未知的 AI 提供者: {provider}")
