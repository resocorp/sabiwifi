"""Anthropic Messages API adapter.

Uses the REST endpoint directly (no SDK) to keep the droplet lean. Supports
tool use via the native tool_use / tool_result blocks.
"""
from __future__ import annotations

import json
from decimal import Decimal

import requests

from ai.providers.base import ChatResponse, LLMProvider, ToolCall

# Rough USD / MToken rate card. Keep in sync with Anthropic pricing; these are
# used only for operator-facing cost roll-up, so approximate is fine.
_RATE_CARD = {
    'default':            (Decimal('3.00'),  Decimal('15.00')),
    'claude-opus':        (Decimal('15.00'), Decimal('75.00')),
    'claude-sonnet':      (Decimal('3.00'),  Decimal('15.00')),
    'claude-haiku':       (Decimal('0.80'),  Decimal('4.00')),
}


def _rates_for(model: str):
    m = (model or '').lower()
    if 'opus' in m:
        return _RATE_CARD['claude-opus']
    if 'haiku' in m:
        return _RATE_CARD['claude-haiku']
    if 'sonnet' in m:
        return _RATE_CARD['claude-sonnet']
    return _RATE_CARD['default']


class AnthropicProvider(LLMProvider):
    name = 'anthropic'
    API_URL = 'https://api.anthropic.com/v1/messages'
    API_VERSION = '2023-06-01'

    def chat(self, system, messages, tools=None, max_tokens=1024, temperature=0.3):
        payload = {
            'model': self.model or 'claude-sonnet-4-6',
            'max_tokens': max_tokens,
            'temperature': temperature,
            'system': system or '',
            'messages': _to_anthropic_messages(messages),
        }
        if tools:
            payload['tools'] = [
                {'name': t['name'], 'description': t.get('description', ''),
                 'input_schema': t['parameters']}
                for t in tools
            ]
        r = requests.post(
            self.API_URL,
            headers={
                'x-api-key': self.api_key,
                'anthropic-version': self.API_VERSION,
                'content-type': 'application/json',
            },
            data=json.dumps(payload),
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()

        text_parts, tool_calls = [], []
        for block in data.get('content', []) or []:
            if block.get('type') == 'text':
                text_parts.append(block.get('text', ''))
            elif block.get('type') == 'tool_use':
                tool_calls.append(ToolCall(
                    id=block.get('id', ''),
                    name=block.get('name', ''),
                    arguments=block.get('input', {}) or {},
                ))

        usage = data.get('usage', {}) or {}
        pt = int(usage.get('input_tokens', 0))
        ct = int(usage.get('output_tokens', 0))
        in_rate, out_rate = _rates_for(self.model)

        return ChatResponse(
            text='\n'.join(p for p in text_parts if p),
            tool_calls=tool_calls,
            stop_reason=data.get('stop_reason', ''),
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_ngn=self._cost_ngn(pt, ct, in_rate, out_rate),
            raw=data,
        )


def _to_anthropic_messages(messages: list) -> list:
    """Accepts a list of dicts in normalised form:
        { 'role': 'user'|'assistant', 'content': str } or
        { 'role': 'assistant', 'tool_calls': [...] } or
        { 'role': 'tool', 'tool_call_id': str, 'content': str }
    Returns Anthropic-native message shape (role + content blocks).
    """
    out = []
    for m in messages:
        role = m.get('role')
        if role == 'tool':
            out.append({
                'role': 'user',
                'content': [{
                    'type': 'tool_result',
                    'tool_use_id': m.get('tool_call_id', ''),
                    'content': str(m.get('content', '')),
                }],
            })
        elif role == 'assistant' and m.get('tool_calls'):
            blocks = []
            if m.get('content'):
                blocks.append({'type': 'text', 'text': m['content']})
            for tc in m['tool_calls']:
                blocks.append({
                    'type': 'tool_use',
                    'id': tc.get('id', ''),
                    'name': tc.get('name', ''),
                    'input': tc.get('arguments', {}) or {},
                })
            out.append({'role': 'assistant', 'content': blocks})
        else:
            out.append({'role': role or 'user',
                        'content': [{'type': 'text', 'text': m.get('content', '')}]})
    return out
