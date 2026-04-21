"""End-to-end Sales-agent tests with a mocked LLM provider.

We monkey-patch `get_provider` to return a canned sequence of ChatResponses
so we exercise the full runner loop (tool dispatch, tool result → follow-up
call, draft-mode hand-off, auto-send gating) without hitting the network.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from accounts.models import Reseller
from ai.models import AIAgentRun, ResellerAIConfig
from ai.providers.base import ChatResponse, ToolCall
from conversations.models import Conversation, Message
from conversations.services import record_inbound_message
from leads.models import Lead
from plans.models import ServicePlan


def _make_reseller(slug='acme'):
    user = User.objects.create_user(username=f'{slug}-owner', password='x')
    reseller = Reseller.objects.create(
        user=user, slug=slug, name=f'{slug.title()} Networks',
        phone='+2348000000001', paystack_subaccount_code='ACCT_test',
        payment_verified=True,
    )
    return reseller


def _make_plan(reseller, name='Home Basic', price=15000):
    return ServicePlan.objects.create(
        reseller=reseller, name=name, slug=name.lower().replace(' ', '-'),
        price_ngn=Decimal(str(price)), duration_days=30,
        download_mbps=10, upload_mbps=5, is_active=True,
    )


def _make_config(reseller, *, caps):
    c = ResellerAIConfig.objects.create(
        reseller=reseller, text_provider=ResellerAIConfig.PROVIDER_ANTHROPIC,
        text_model='claude-sonnet-4-6',
        capabilities=caps,
    )
    c.text_api_key = 'sk-test'
    c.save()
    return c


class _FakeProvider:
    """Returns a scripted list of ChatResponses. Each .chat() consumes one."""
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, *, system, messages, tools=None, max_tokens=1024, temperature=0.3):
        return self._responses.pop(0)


class SalesAgentDraftModeTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        _make_plan(self.reseller)
        self.config = _make_config(self.reseller, caps={
            'ai_enabled': True, 'sales_enabled': True,
            'auto_send_replies': False, 'auto_quote_below_ngn': 0,
        })

    def _run_with_canned(self, canned):
        msg = record_inbound_message(
            reseller=self.reseller,
            channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='2348011112222@s.whatsapp.net',
            body='Hello, I need home wifi',
            attachments=[], external_message_id='WA-1',
            sender_phone='2348011112222',
        )
        from ai.jobs import run_sales_agent
        with patch('ai.agents.runner.get_provider',
                   return_value=_FakeProvider(canned)):
            result = run_sales_agent(msg.pk)
        return msg, result

    def test_draft_reply_stored_and_conversation_marked(self):
        # Agent directly calls send_reply — runner should intercept to draft.
        canned = [
            ChatResponse(
                text='(assistant)',
                tool_calls=[ToolCall(id='t1', name='send_reply',
                                     arguments={'body': 'Hello! Our Home Basic is 15,000 / month.'})],
                prompt_tokens=20, completion_tokens=10,
            ),
        ]
        msg, _ = self._run_with_canned(canned)
        convo = msg.conversation

        drafts = list(convo.messages.filter(is_draft=True))
        self.assertEqual(len(drafts), 1)
        self.assertIn('Home Basic', drafts[0].body)
        convo.refresh_from_db()
        self.assertEqual(convo.state, Conversation.STATE_AI_DRAFTED)

        run = AIAgentRun.objects.filter(reseller=self.reseller,
                                        agent_role=AIAgentRun.ROLE_SALES).first()
        self.assertIsNotNone(run)
        self.assertEqual(run.status, AIAgentRun.STATUS_SUCCESS)
        tool_names = [tc['name'] for tc in (run.tool_calls or [])]
        self.assertIn('send_reply', tool_names)


class SalesAgentPaymentCapTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        self.config = _make_config(self.reseller, caps={
            'ai_enabled': True, 'sales_enabled': True,
            'auto_send_replies': True,
            'auto_quote_below_ngn': 20000,
        })

    def test_payment_above_cap_is_gated(self):
        lead = Lead.objects.create(reseller=self.reseller,
                                    phone='2348011112222',
                                    intent=Lead.INTENT_HOME)
        canned = [
            ChatResponse(
                text='',
                tool_calls=[ToolCall(id='t1', name='create_payment_link',
                                     arguments={'lead_id': lead.pk,
                                                'amount_ngn': 140000})],
                prompt_tokens=10, completion_tokens=5,
            ),
            ChatResponse(text='(followup)', tool_calls=[],
                         prompt_tokens=5, completion_tokens=3),
        ]
        msg = record_inbound_message(
            reseller=self.reseller,
            channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='2348011112222@s.whatsapp.net',
            body='pay please', attachments=[], external_message_id='WA-X',
            sender_phone='2348011112222',
        )
        from ai.jobs import run_sales_agent
        with patch('ai.agents.runner.get_provider',
                   return_value=_FakeProvider(canned)):
            run_sales_agent(msg.pk)

        run = AIAgentRun.objects.get(reseller=self.reseller)
        gated_steps = [tc for tc in run.tool_calls if tc.get('gated')]
        self.assertEqual(len(gated_steps), 1)
        self.assertEqual(gated_steps[0]['name'], 'create_payment_link')


class SalesAgentDisabledTest(TestCase):
    def test_ai_disabled_is_skipped(self):
        reseller = _make_reseller()
        _make_config(reseller, caps={'ai_enabled': False, 'sales_enabled': True})
        msg = record_inbound_message(
            reseller=reseller, channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='2348011112222@s.whatsapp.net',
            body='hi', attachments=[], external_message_id='WA-D',
            sender_phone='2348011112222',
        )
        from ai.jobs import run_sales_agent
        result = run_sales_agent(msg.pk)
        self.assertTrue(result['skipped'])
        self.assertEqual(AIAgentRun.objects.count(), 0)
