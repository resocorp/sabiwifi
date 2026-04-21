"""Provider factory.

Agents resolve a provider by calling `get_provider(config)`. Concrete providers
are imported eagerly so a missing dep surfaces at boot rather than mid-call.
"""
from decimal import Decimal

from django.conf import settings

from ai.models import ResellerAIConfig
from ai.providers.anthropic import AnthropicProvider
from ai.providers.base import ChatResponse, LLMProvider, ToolCall
from ai.providers.gemini import GeminiProvider
from ai.providers.openai_compat import OpenAICompatibleProvider, OpenAIProvider

__all__ = [
    'AnthropicProvider', 'OpenAIProvider', 'GeminiProvider',
    'OpenAICompatibleProvider', 'LLMProvider', 'ChatResponse', 'ToolCall',
    'get_provider',
]

_MAP = {
    ResellerAIConfig.PROVIDER_ANTHROPIC: AnthropicProvider,
    ResellerAIConfig.PROVIDER_OPENAI: OpenAIProvider,
    ResellerAIConfig.PROVIDER_GEMINI: GeminiProvider,
    ResellerAIConfig.PROVIDER_OPENAI_COMPAT: OpenAICompatibleProvider,
}


def get_provider(config: ResellerAIConfig) -> LLMProvider:
    cls = _MAP.get(config.text_provider)
    if not cls:
        raise ValueError(f'Unknown provider: {config.text_provider}')
    rate = Decimal(str(getattr(settings, 'AI_USD_TO_NGN_RATE', '1500')))
    return cls(
        api_key=config.text_api_key,
        model=config.text_model,
        endpoint_url=config.text_endpoint_url,
        usd_to_ngn_rate=rate,
    )
