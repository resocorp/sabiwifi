"""Shadow-mode replay harness test — runs past inbound messages through the
Sales agent with a canned provider and asserts drafts are produced."""
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Reseller
from ai.models import AIAgentRun, ResellerAIConfig
from ai.providers.base import ChatResponse, ToolCall
from conversations.models import Conversation, Message
from conversations.services import record_inbound_message


class _FakeProvider:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, *, system, messages, tools=None, max_tokens=1024, temperature=0.3):
        return self._responses.pop(0)


class ReplayHarnessTest(TestCase):
    def setUp(self):
        u = User.objects.create_user(username='rplay', password='x')
        self.reseller = Reseller.objects.create(
            user=u, slug='rplay', name='Rplay', phone='+2348000000001',
            paystack_subaccount_code='ACCT_x', payment_verified=True,
        )
        cfg = ResellerAIConfig.objects.create(
            reseller=self.reseller,
            text_provider=ResellerAIConfig.PROVIDER_ANTHROPIC,
            text_model='claude-sonnet-4-6',
            # Auto-send ON — the replay harness must still force drafts.
            capabilities={'ai_enabled': True, 'sales_enabled': True,
                          'auto_send_replies': True, 'auto_quote_below_ngn': 0},
        )
        cfg.text_api_key = 'sk-test'
        cfg.save()
        self.cfg = cfg

    def _seed_inbound(self, body, ext_id):
        with patch('conversations.services.transaction.on_commit', lambda fn: None):
            return record_inbound_message(
                reseller=self.reseller, channel=Conversation.CHANNEL_WHATSAPP,
                external_thread_id='2348011@s.whatsapp.net',
                body=body, attachments=[], external_message_id=ext_id,
                sender_phone='2348011',
            )

    def test_replay_forces_draft_mode_even_if_auto_send_on(self):
        m1 = self._seed_inbound('Hi, need wifi', 'R1')
        m2 = self._seed_inbound('Any plans?', 'R2')

        canned = [
            ChatResponse(
                text='', tool_calls=[ToolCall(id='t1', name='send_reply',
                                              arguments={'body': 'Hi! Here are our plans.'})],
                prompt_tokens=10, completion_tokens=5,
            ),
            ChatResponse(
                text='', tool_calls=[ToolCall(id='t2', name='send_reply',
                                              arguments={'body': 'Home Basic 15k.'})],
                prompt_tokens=10, completion_tokens=5,
            ),
        ]
        out = StringIO()
        with patch('ai.agents.runner.get_provider',
                   return_value=_FakeProvider(canned)):
            call_command('ai_replay', reseller='rplay', days=30, limit=10, stdout=out)

        # Both messages should have produced drafts, not real sends.
        drafts = Message.objects.filter(is_draft=True)
        self.assertEqual(drafts.count(), 2)

        # The convo should end up in AI_DRAFTED state.
        convo = Conversation.objects.get(pk=m1.conversation_id)
        self.assertEqual(convo.state, Conversation.STATE_AI_DRAFTED)

        # AIAgentRun audit rows produced.
        self.assertEqual(
            AIAgentRun.objects.filter(reseller=self.reseller,
                                      agent_role=AIAgentRun.ROLE_SALES).count(),
            2,
        )
        self.assertIn('drafted=2', out.getvalue())

    def test_dry_run_writes_nothing(self):
        self._seed_inbound('hi', 'DRY1')
        out = StringIO()
        call_command('ai_replay', reseller='rplay', days=30, limit=5,
                     dry_run=True, stdout=out)
        self.assertEqual(Message.objects.filter(is_draft=True).count(), 0)
        self.assertEqual(AIAgentRun.objects.count(), 0)
        self.assertIn('dry-run', out.getvalue())
