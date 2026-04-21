"""Google Gemini adapter (generativelanguage.googleapis.com).

Uses REST. Tool use via `functionDeclarations` / `functionCall` /
`functionResponse` parts. Mapped to the normalised ChatResponse.
"""
from __future__ import annotations

import json
from decimal import Decimal

import requests

from ai.providers.base import ChatResponse, LLMProvider, ToolCall

_RATE_CARD = {
    'default':        (Decimal('0.50'), Decimal('1.50')),
    'gemini-2.0-flash': (Decimal('0.10'), Decimal('0.40')),
    'gemini-1.5-pro':   (Decimal('1.25'), Decimal('5.00')),
    'gemini-1.5-flash': (Decimal('0.075'), Decimal('0.30')),
}


def _rates_for(model: str):
    m = (model or '').lower()
    for key in _RATE_CARD:
        if key != 'default' and key in m:
            return _RATE_CARD[key]
    return _RATE_CARD['default']


class GeminiProvider(LLMProvider):
    name = 'gemini'
    BASE_URL = 'https://generativelanguage.googleapis.com/v1beta/models'

    def chat(self, system, messages, tools=None, max_tokens=1024, temperature=0.3):
        model = self.model or 'gemini-2.0-flash'
        url = f'{self.BASE_URL}/{model}:generateContent?key={self.api_key}'

        contents = []
        for m in messages:
            role = m.get('role')
            if role == 'tool':
                contents.append({
                    'role': 'user',
                    'parts': [{
                        'functionResponse': {
                            'name': m.get('tool_name', ''),
                            'response': {'content': str(m.get('content', ''))},
                        },
                    }],
                })
            elif role == 'assistant' and m.get('tool_calls'):
                parts = []
                if m.get('content'):
                    parts.append({'text': m['content']})
                for tc in m['tool_calls']:
                    parts.append({
                        'functionCall': {
                            'name': tc.get('name', ''),
                            'args': tc.get('arguments', {}) or {},
                        },
                    })
                contents.append({'role': 'model', 'parts': parts})
            else:
                contents.append({
                    'role': 'user' if role != 'assistant' else 'model',
                    'parts': [{'text': m.get('content', '')}],
                })

        payload = {
            'contents': contents,
            'generationConfig': {
                'maxOutputTokens': max_tokens,
                'temperature': temperature,
            },
        }
        if system:
            payload['systemInstruction'] = {'parts': [{'text': system}]}
        if tools:
            payload['tools'] = [{
                'functionDeclarations': [{
                    'name': t['name'],
                    'description': t.get('description', ''),
                    'parameters': t['parameters'],
                } for t in tools],
            }]

        r = requests.post(url,
                          headers={'content-type': 'application/json'},
                          data=json.dumps(payload), timeout=60)
        r.raise_for_status()
        data = r.json()

        cand = (data.get('candidates') or [{}])[0]
        parts = (cand.get('content') or {}).get('parts') or []
        text_parts, tool_calls = [], []
        for p in parts:
            if 'text' in p:
                text_parts.append(p['text'])
            elif 'functionCall' in p:
                fc = p['functionCall']
                tool_calls.append(ToolCall(
                    id=fc.get('name', ''),  # Gemini has no call-id; reuse name
                    name=fc.get('name', ''),
                    arguments=fc.get('args', {}) or {},
                ))

        usage = data.get('usageMetadata', {}) or {}
        pt = int(usage.get('promptTokenCount', 0))
        ct = int(usage.get('candidatesTokenCount', 0))
        in_rate, out_rate = _rates_for(model)

        return ChatResponse(
            text='\n'.join(t for t in text_parts if t),
            tool_calls=tool_calls,
            stop_reason=cand.get('finishReason', ''),
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_ngn=self._cost_ngn(pt, ct, in_rate, out_rate),
            raw=data,
        )
