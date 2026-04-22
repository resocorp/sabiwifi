"""End-to-end workflow tests against the seeded prompt overrides.

These exercise the full path:
  inbound message → AIPromptVersion-injected system prompt → stubbed LLM
  with scripted tool calls → tools mutate Lead/Ticket/Conversation/Staff.

Provider is stubbed so no real API calls happen. The point is to confirm:
  1. Sales agent: asks for area → creates lead → quotes plan → drafts a reply.
  2. Field agent: picks the right tech and dispatches a WA brief.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from accounts.models import Reseller
from ai.agents.field import FieldSupervisorAgent
from ai.models import AIAgentRun, AIPromptVersion, ResellerAIConfig
from ai.providers.base import ChatResponse, ToolCall
from conversations.models import Conversation, Message
from conversations.services import record_inbound_message
from leads.models import Lead
from plans.models import ServicePlan
from staff.models import StaffMember
from tickets.models import Ticket
from tickets.services import create_ticket


# Pull seed bodies straight from the script so this test asserts against the
# same overrides that production loads — keeps prompts and tests in sync.
from scripts.seed_ai_prompts import SALES_OVERRIDE, FIELD_OVERRIDE


def _seed_reseller(slug='nedu-test'):
    user = User.objects.create_user(username=f'{slug}-owner', password='x')
    r = Reseller.objects.create(
        user=user, slug=slug, name='Nedu WiFi (test)',
        phone='+2348000000099', paystack_subaccount_code='ACCT_test',
        payment_verified=True,
    )
    ServicePlan.objects.create(
        reseller=r, name='Home Lite', slug='home-lite',
        price_ngn=Decimal('5000'), duration_days=30,
        download_mbps=5, upload_mbps=2, is_active=True,
    )
    ServicePlan.objects.create(
        reseller=r, name='Home Plus', slug='home-plus',
        price_ngn=Decimal('15000'), duration_days=30,
        download_mbps=10, upload_mbps=5, is_active=True,
    )
    cfg = ResellerAIConfig.objects.create(
        reseller=r, text_provider=ResellerAIConfig.PROVIDER_ANTHROPIC,
        text_model='claude-sonnet-4-6',
        capabilities={
            'ai_enabled': True, 'sales_enabled': True,
            'support_enabled': True, 'field_enabled': True,
            'auto_send_replies': False, 'auto_quote_below_ngn': 20000,
        },
    )
    cfg.text_api_key = 'sk-test'
    cfg.save()
    AIPromptVersion.objects.create(config=cfg, agent_role='sales',
                                   body=SALES_OVERRIDE)
    AIPromptVersion.objects.create(config=cfg, agent_role='field',
                                   body=FIELD_OVERRIDE)
    return r, cfg


class _ScriptedProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.systems_seen = []

    def chat(self, *, system, messages, tools=None, max_tokens=1024, temperature=0.3):
        self.systems_seen.append(system)
        return self._responses.pop(0)


class SalesWorkflowE2ETest(TestCase):
    def test_sales_creates_lead_and_drafts_reply_with_seeded_prompt(self):
        reseller, cfg = _seed_reseller()
        plan = ServicePlan.objects.get(reseller=reseller, slug='home-plus')

        # Scripted LLM:
        #  step 1 → call create_lead with the inbound phone
        #  step 2 → call suggest_plan to narrow catalogue
        #  step 3 → call send_reply with the recommendation (will be drafted)
        canned = [
            ChatResponse(
                text='', prompt_tokens=15, completion_tokens=10,
                tool_calls=[ToolCall(id='c1', name='create_lead', arguments={
                    'phone': '2348011112222', 'name': 'Ada',
                    'address': 'Lekki Phase 1', 'intent': 'home',
                })],
            ),
            ChatResponse(
                text='', prompt_tokens=20, completion_tokens=8,
                tool_calls=[ToolCall(id='c2', name='suggest_plan', arguments={
                    'min_download_mbps': 8,
                })],
            ),
            ChatResponse(
                text='', prompt_tokens=22, completion_tokens=18,
                tool_calls=[ToolCall(id='c3', name='send_reply', arguments={
                    'body': 'Hi Ada — Home Plus is ₦15,000 / month with 10 Mbps. Shall I send a payment link?',
                })],
            ),
        ]

        msg = record_inbound_message(
            reseller=reseller, channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='2348011112222@s.whatsapp.net',
            body='I need home wifi in Lekki Phase 1, what do you have?',
            attachments=[], external_message_id='WA-E2E-1',
            sender_phone='2348011112222',
        )

        provider = _ScriptedProvider(canned)
        from ai.jobs import run_sales_agent
        with patch('ai.agents.runner.get_provider', return_value=provider):
            result = run_sales_agent(msg.pk)

        self.assertFalse(result['skipped'])
        self.assertTrue(result['drafted'])

        # Seeded sales prompt actually reached the provider
        self.assertIn('SabiWiFi sales playbook', provider.systems_seen[0])
        self.assertIn('NEDU', provider.systems_seen[0].upper())

        # Lead was created with the address + intent (phone is normalised
        # by Lead.save → only assert one lead exists for this reseller)
        leads = list(Lead.objects.filter(reseller=reseller))
        self.assertEqual(len(leads), 1)
        lead = leads[0]
        self.assertIn('11112222', lead.phone)
        self.assertEqual(lead.name, 'Ada')
        self.assertEqual(lead.address, 'Lekki Phase 1')
        self.assertEqual(lead.intent, Lead.INTENT_HOME)
        self.assertEqual(lead.source, 'ai_sales')

        # Conversation is in ai_drafted state with one draft outbound message
        convo = msg.conversation
        convo.refresh_from_db()
        self.assertEqual(convo.state, Conversation.STATE_AI_DRAFTED)
        drafts = list(convo.messages.filter(is_draft=True))
        self.assertEqual(len(drafts), 1)
        self.assertIn('Home Plus', drafts[0].body)

        # Run was logged with the right tool sequence
        run = AIAgentRun.objects.get(reseller=reseller, agent_role='sales')
        names = [tc['name'] for tc in (run.tool_calls or [])]
        self.assertEqual(names, ['create_lead', 'suggest_plan', 'send_reply'])
        self.assertEqual(run.status, AIAgentRun.STATUS_SUCCESS)


class FieldProposeWorkflowE2ETest(TestCase):
    """Field-supervisor is now PROPOSE-ONLY. It lists techs and posts a
    KIND_COMMENT TicketEvent with the recommendation; it does NOT call
    assign_ticket. (Detailed propose+dispatch coverage lives in
    test_autonomous_flow.py — this test just confirms the seeded prompt
    reaches the LLM via _latest_override.)"""

    def test_propose_runs_with_seeded_prompt_and_no_assignment(self):
        reseller, cfg = _seed_reseller(slug='nedu-field')

        free = StaffMember.objects.create(
            reseller=reseller, name='Bola', phone='+2348011110002',
            whatsapp='+2348011110002',
            role=StaffMember.ROLE_FIELD_TECH, active=True,
            coverage_areas=['Lekki'], current_load=1,
        )

        ticket = create_ticket(
            reseller=reseller, type=Ticket.TYPE_SUPPORT,
            subject='Slow internet', body='Customer reports buffering on Netflix',
            priority=Ticket.PRIORITY_NORMAL,
        )
        canned = [
            ChatResponse(text='', prompt_tokens=12, completion_tokens=8,
                tool_calls=[ToolCall(id='f1', name='list_available_techs',
                                     arguments={})]),
            ChatResponse(text='', prompt_tokens=18, completion_tokens=10,
                tool_calls=[ToolCall(id='f2', name='add_ticket_comment', arguments={
                    'ticket_id': ticket.pk,
                    'note': 'Bola — lowest load in Lekki',
                    'metadata': {'recommended_staff_id': free.pk,
                                 'recommended_staff_name': 'Bola'},
                })]),
            ChatResponse(text='proposed', tool_calls=[],
                         prompt_tokens=5, completion_tokens=2),
        ]
        provider = _ScriptedProvider(canned)
        with patch('ai.agents.runner.get_provider', return_value=provider):
            result = FieldSupervisorAgent(cfg).propose_assignment(ticket=ticket)

        self.assertFalse(result.skipped)
        self.assertIn('SabiWiFi field-dispatch playbook', provider.systems_seen[0])
        ticket.refresh_from_db()
        self.assertIsNone(ticket.assigned_staff_id, 'must NOT auto-assign')
        run = AIAgentRun.objects.get(reseller=reseller, agent_role='field')
        names = [tc['name'] for tc in (run.tool_calls or [])]
        self.assertEqual(names, ['list_available_techs', 'add_ticket_comment'])
