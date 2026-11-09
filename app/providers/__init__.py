from app.config import get_settings
from app.providers.openai_compat import OpenAICompatibleProvider
from app.providers.anthropic import AnthropicProvider
from app.providers.gemini import GeminiProvider
from app.providers.mock import MockProvider


def build_providers():
    s = get_settings()
    return {
        "openai": OpenAICompatibleProvider("openai", s.openai_api_key, s.openai_base_url),
        "deepseek": OpenAICompatibleProvider("deepseek", s.deepseek_api_key, s.deepseek_base_url),
        "anthropic": AnthropicProvider(s.anthropic_api_key, s.anthropic_base_url, s.anthropic_version),
        "gemini": GeminiProvider(s.gemini_api_key, s.gemini_base_url),
        "mock": MockProvider(s.enable_mock_provider),
    }
