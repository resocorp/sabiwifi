"""OpenAI Chat Completions API adapter.

Also reused for any OpenAI-compatible endpoint (Groq, Together, Ollama, vLLM,
self-hosted Gemma) via `endpoint_url`. When a reseller picks `openai_compatible`
their `text_endpoint_url` is honoured; otherwise OpenAI's public URL is used.
"""
from __future__ import annotations

import json
from decimal import Decimal

import requests

from ai.providers.base import ChatResponse, LLMProvider, ToolCall

# Approximate USD / MToken rate card; override with 0,0 for self-hosted
# (endpoint_url set and OPENAI_ASSUME_FREE_SELF_HOST — currently we just
# default to 0,0 when endpoint is custom).
_RATE_CARD = {
    'default':   (Decimal('2.50'), Decimal('10.00')),
    'gpt-4o':    (Decimal('2.50'), Decimal('10.00')),
    'gpt-4o-mini': (Decimal('0.15'), Decimal('0.60')),
    'o1':        (Decimal('15.00'), Decimal('60.00')),
    'o3':        (Decimal('2.00'),  Decimal('8.00')),
}


def _rates_for(model: str, is_self_host: bool):
    if is_self_host:
        return Decimal('0'), Decimal('0')
    m = (model or '').lower()
    for key in ('gpt-4o-mini', 'gpt-4o', 'o1', 'o3'):
        if key in m:
            return _RATE_CARD[key]
    return _RATE_CARD['default']


class OpenAIProvider(LLMProvider):
    name = 'openai'
    DEFAULT_URL = 'https://api.openai.com/v1/chat/completions'

    def _url(self) -> str:
        if self.endpoint_url:
            url = self.endpoint_url.rstrip('/')
            if not url.endswith('/chat/completions'):
                url = url + '/chat/completions'
            return url
        return self.DEFAULT_URL

    def chat(self, system, messages, tools=None, max_tokens=1024, temperature=0.3):
        native_msgs = []
        if system:
            native_msgs.append({'role': 'system', 'content': system})
        for m in messages:
            role = m.get('role')
            if role == 'tool':
                native_msgs.append({
                    'role': 'tool',
                    'tool_call_id': m.get('tool_call_id', ''),
                    'content': str(m.get('content', '')),
                })
            elif role == 'assistant' and m.get('tool_calls'):
                native_msgs.append({
                    'role': 'assistant',
                    'content': m.get('content', '') or None,
                    'tool_calls': [{
                        'id': tc.get('id', ''),
                        'type': 'function',
                        'function': {
                            'name': tc.get('name', ''),
                            'arguments': json.dumps(tc.get('arguments', {}) or {}),
                        },
                    } for tc in m['tool_calls']],
                })
            else:
                native_msgs.append({'role': role or 'user',
                                    'content': m.get('content', '')})

        payload = {
            'model': self.model or 'gpt-4o-mini',
            'messages': native_msgs,
            'max_tokens': max_tokens,
            'temperature': temperature,
        }
        if tools:
            payload['tools'] = [{
                'type': 'function',
                'function': {
                    'name': t['name'],
                    'description': t.get('description', ''),
                    'parameters': t['parameters'],
                },
            } for t in tools]
            payload['tool_choice'] = 'auto'

        r = requests.post(
            self._url(),
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'content-type': 'application/json',
            },
            data=json.dumps(payload),
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()

        choice = (data.get('choices') or [{}])[0]
        msg = choice.get('message', {}) or {}
        text = msg.get('content') or ''
        tool_calls = []
        for tc in msg.get('tool_calls') or []:
            fn = tc.get('function', {}) or {}
            args_raw = fn.get('arguments', '{}')
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except ValueError:
                args = {}
            tool_calls.append(ToolCall(
                id=tc.get('id', ''),
                name=fn.get('name', ''),
                arguments=args,
            ))

        usage = data.get('usage', {}) or {}
        pt = int(usage.get('prompt_tokens', 0))
        ct = int(usage.get('completion_tokens', 0))
        in_rate, out_rate = _rates_for(self.model, is_self_host=bool(self.endpoint_url))

        return ChatResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=choice.get('finish_reason', ''),
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_ngn=self._cost_ngn(pt, ct, in_rate, out_rate),
            raw=data,
        )


class OpenAICompatibleProvider(OpenAIProvider):
    """Alias — same wire protocol. Separate class name makes the intent
    explicit in factories and logs."""
    name = 'openai_compatible'
