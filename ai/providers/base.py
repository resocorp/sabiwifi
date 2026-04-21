"""Abstract LLM provider interface.

Every provider (Anthropic, OpenAI, Gemini, OpenAI-compatible) implements `chat`
and returns a normalised ChatResponse. Agents speak only to this interface —
they never import a concrete provider directly.

Tool-call schema (provider-agnostic):
    Tool = {
        'name': str,
        'description': str,
        'parameters': { json-schema object },
    }
Each provider is responsible for translating the schema to/from its own
native format.

ChatResponse.tool_calls is a list of:
    { 'id': str, 'name': str, 'arguments': dict }
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResponse:
    text: str = ''
    tool_calls: list = field(default_factory=list)  # list[ToolCall]
    stop_reason: str = ''
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_ngn: Decimal = Decimal('0')
    raw: Any = None


class LLMProvider:
    name: str = ''

    def __init__(self, *, api_key: str, model: str, endpoint_url: str = '',
                 usd_to_ngn_rate: Decimal = Decimal('1500')):
        self.api_key = api_key
        self.model = model
        self.endpoint_url = endpoint_url
        self.usd_to_ngn_rate = usd_to_ngn_rate

    def chat(self, system: str, messages: list, tools: list | None = None,
             max_tokens: int = 1024, temperature: float = 0.3) -> ChatResponse:
        raise NotImplementedError

    def _cost_ngn(self, prompt_tokens: int, completion_tokens: int,
                  in_per_mtok_usd: Decimal, out_per_mtok_usd: Decimal) -> Decimal:
        """Compute NGN cost given per-MToken USD rates. Subclasses call this
        with their own rate card — rate cards live close to the adapter so
        they stay honest as pricing changes."""
        in_usd = (Decimal(prompt_tokens) * in_per_mtok_usd) / Decimal('1000000')
        out_usd = (Decimal(completion_tokens) * out_per_mtok_usd) / Decimal('1000000')
        return (in_usd + out_usd) * self.usd_to_ngn_rate
