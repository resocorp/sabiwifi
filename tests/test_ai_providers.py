"""Adapter-layer tests.

We never hit a real provider — each test patches `requests.post` with a canned
response that matches the provider's native shape, then asserts that the
adapter returns the normalised ChatResponse we expect.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from ai.providers.anthropic import AnthropicProvider
from ai.providers.gemini import GeminiProvider
from ai.providers.openai_compat import OpenAIProvider


class _FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f'http {self.status_code}')

    def json(self):
        return self._data


class AnthropicAdapterTest(TestCase):
    def test_chat_extracts_text_and_tool_calls(self):
        fake = {
            'content': [
                {'type': 'text', 'text': 'Sure, let me check that.'},
                {'type': 'tool_use', 'id': 'toolu_1', 'name': 'lookup_plans',
                 'input': {'reseller_slug': 'acme'}},
            ],
            'stop_reason': 'tool_use',
            'usage': {'input_tokens': 42, 'output_tokens': 7},
        }
        p = AnthropicProvider(api_key='k', model='claude-sonnet-4-6')
        with patch('ai.providers.anthropic.requests.post',
                   return_value=_FakeResp(fake)):
            resp = p.chat(system='s', messages=[{'role': 'user', 'content': 'hi'}],
                          tools=[{'name': 'lookup_plans',
                                  'description': 'x',
                                  'parameters': {'type': 'object'}}])
        self.assertIn('let me check', resp.text)
        self.assertEqual(len(resp.tool_calls), 1)
        self.assertEqual(resp.tool_calls[0].name, 'lookup_plans')
        self.assertEqual(resp.tool_calls[0].arguments, {'reseller_slug': 'acme'})
        self.assertEqual(resp.prompt_tokens, 42)
        self.assertEqual(resp.completion_tokens, 7)
        self.assertGreater(resp.cost_ngn, Decimal('0'))


class OpenAIAdapterTest(TestCase):
    def test_chat_handles_function_tool_call(self):
        fake = {
            'choices': [{
                'finish_reason': 'tool_calls',
                'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{
                        'id': 'call_1', 'type': 'function',
                        'function': {'name': 'create_lead',
                                     'arguments': '{"phone": "08099"}'},
                    }],
                },
            }],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 3},
        }
        p = OpenAIProvider(api_key='k', model='gpt-4o-mini')
        with patch('ai.providers.openai_compat.requests.post',
                   return_value=_FakeResp(fake)):
            resp = p.chat(system='s', messages=[{'role': 'user', 'content': 'hi'}])
        self.assertEqual(resp.tool_calls[0].name, 'create_lead')
        self.assertEqual(resp.tool_calls[0].arguments, {'phone': '08099'})

    def test_endpoint_url_used_for_openai_compatible(self):
        p = OpenAIProvider(api_key='k', model='gemma-7b',
                           endpoint_url='http://ollama.local:11434/v1')
        self.assertTrue(p._url().endswith('/chat/completions'))
        self.assertIn('ollama.local', p._url())


class GeminiAdapterTest(TestCase):
    def test_chat_extracts_function_call(self):
        fake = {
            'candidates': [{
                'finishReason': 'STOP',
                'content': {
                    'parts': [
                        {'text': 'Here you go.'},
                        {'functionCall': {'name': 'suggest_plan',
                                           'args': {'tier': 'home_basic'}}},
                    ],
                },
            }],
            'usageMetadata': {'promptTokenCount': 30, 'candidatesTokenCount': 5},
        }
        p = GeminiProvider(api_key='k', model='gemini-2.0-flash')
        with patch('ai.providers.gemini.requests.post',
                   return_value=_FakeResp(fake)):
            resp = p.chat(system='s', messages=[{'role': 'user', 'content': 'hi'}])
        self.assertIn('Here you go', resp.text)
        self.assertEqual(resp.tool_calls[0].name, 'suggest_plan')
        self.assertEqual(resp.tool_calls[0].arguments, {'tier': 'home_basic'})
