"""Safety-rail tests: circuit breaker + outbound cap + kill switch."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.models import Reseller
from ai.models import AIAgentRun, ResellerAIConfig
from ai.safety import (
    outbound_cap_reached, outbound_today_count,
    pause_ai, resume_ai, trip_circuit_breaker,
)
from conversations.models import Conversation, Message


def _reseller(slug='acme'):
    u = User.objects.create_user(username=f'{slug}-u', password='x')
    return Reseller.objects.create(
        user=u, slug=slug, name=slug.title(), phone='+234800',
        paystack_subaccount_code='ACCT_x', payment_verified=True,
    )


def _config(reseller, **caps):
    c = ResellerAIConfig.objects.create(
        reseller=reseller,
        text_provider=ResellerAIConfig.PROVIDER_ANTHROPIC,
        text_model='claude-sonnet-4-6',
        capabilities=caps or {'ai_enabled': True},
    )
    c.text_api_key = 'sk-x'
    c.save()
    return c


class CircuitBreakerTest(TestCase):
    def test_trips_after_half_plus_failures(self):
        r = _reseller()
        c = _config(r)
        for _ in range(3):
            AIAgentRun.objects.create(reseller=r, agent_role='sales',
                                      provider='anthropic',
                                      status=AIAgentRun.STATUS_FAILED)
        for _ in range(2):
            AIAgentRun.objects.create(reseller=r, agent_role='sales',
                                      provider='anthropic',
                                      status=AIAgentRun.STATUS_SUCCESS)
        self.assertTrue(trip_circuit_breaker(c))
        c.refresh_from_db()
        self.assertIsNotNone(c.ai_paused_at)
        self.assertIn('circuit_breaker', c.ai_pause_reason)

    def test_does_not_trip_below_min_runs(self):
        r = _reseller()
        c = _config(r)
        for _ in range(3):
            AIAgentRun.objects.create(reseller=r, agent_role='sales',
                                      provider='anthropic',
                                      status=AIAgentRun.STATUS_FAILED)
        self.assertFalse(trip_circuit_breaker(c))


class OutboundCapTest(TestCase):
    def test_cap_reached_reflects_ai_sent_today(self):
        r = _reseller()
        c = _config(r, ai_enabled=True, max_outbound_per_customer_per_day=2)
        convo = Conversation.objects.create(
            reseller=r, channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='2348011@s.whatsapp.net',
            contact_phone='2348011',
        )
        self.assertFalse(outbound_cap_reached(c, convo))

        for _ in range(2):
            Message.objects.create(
                conversation=convo, direction=Message.DIRECTION_OUT,
                body='x', source=Message.SOURCE_AI_SALES,
            )
        self.assertEqual(outbound_today_count(convo), 2)
        self.assertTrue(outbound_cap_reached(c, convo))


class KillSwitchTest(TestCase):
    def test_pause_and_resume_toggle_flags(self):
        r = _reseller()
        c = _config(r, ai_enabled=True, sales_enabled=True)
        self.assertTrue(c.is_agent_enabled('sales'))
        pause_ai(c, 'operator_manual')
        self.assertFalse(c.is_agent_enabled('sales'))
        resume_ai(c)
        self.assertTrue(c.is_agent_enabled('sales'))


class OperatorKillSwitchEndpointTest(TestCase):
    def test_operator_can_pause_and_resume(self):
        r = _reseller(slug='acme2')
        c = _config(r, ai_enabled=True, sales_enabled=True)
        staff = User.objects.create_user(username='op', password='x', is_staff=True)
        self.client.force_login(staff)

        resp = self.client.post(f'/operator/ai/{r.pk}/pause/', {'reason': 'testing'})
        self.assertEqual(resp.status_code, 200)
        c.refresh_from_db()
        self.assertIsNotNone(c.ai_paused_at)
        self.assertIn('testing', c.ai_pause_reason)

        resp = self.client.post(f'/operator/ai/{r.pk}/resume/')
        self.assertEqual(resp.status_code, 200)
        c.refresh_from_db()
        self.assertIsNone(c.ai_paused_at)
