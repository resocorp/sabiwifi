"""Support agent state machine + new diagnostic helpers.

Covers the Phase 2c additions layered on top of the 2b diagnostic chain:
  - lookup_subscriber accepts email alongside phone
  - check_data_cap_remaining flips categorise_cause to DATA_CAP_EXHAUSTED
  - Conversation.diagnostic_state advances across turns
  - Pre-router YES / NO handling: YES closes, NO reopens, ambiguous falls through
  - Satisfaction ping stamps awaiting_confirmation on the conversation
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.models import Reseller, Subscriber
from ai import jobs as ai_jobs
from ai.diagnostics import (
    DiagnosticFacts, categorise_cause, check_data_cap_remaining,
    check_reseller_wide_outage,
)
from ai.models import ResellerAIConfig
from ai.tools import (
    ToolContext, tool_check_data_cap_remaining,
    tool_conversation_get_state, tool_conversation_set_state,
    tool_get_account_summary_for_customer, tool_lookup_subscriber,
)
from conversations.models import Conversation, Message
from plans.models import DailyUsageSnapshot, ServicePlan, Subscription
from tickets.models import Ticket, TicketEvent
from tickets.services import change_status, create_ticket


def _seed(slug='smx'):
    user = User.objects.create_user(
        username=f'{slug}-owner@x', password='x', email=f'{slug}@x',
    )
    r = Reseller.objects.create(
        user=user, slug=slug, name=slug.title(),
        email=f'biz-{slug}@example.com',
        phone=f'+23480{abs(hash(slug)) % 10**9:09d}',
        paystack_subaccount_code='ACCT_test',
        payment_verified=True,
    )
    plan = ServicePlan.objects.create(
        reseller=r, name='Home Lite', slug='home-lite',
        price_ngn=Decimal('5000'), duration_days=30,
        download_mbps=5, upload_mbps=2, is_active=True,
        data_cap_gb=Decimal('10'),
    )
    cfg = ResellerAIConfig.objects.create(
        reseller=r, text_provider=ResellerAIConfig.PROVIDER_ANTHROPIC,
        text_model='claude-sonnet-4-6',
        capabilities={
            'ai_enabled': True, 'sales_enabled': True,
            'support_enabled': True, 'field_enabled': True,
            'auto_send_replies': True, 'auto_quote_below_ngn': 20000,
        },
    )
    cfg.text_api_key = 'sk-test'
    cfg.save()
    return r, cfg, plan


# ---------------------------------------------------------------------------
# lookup_subscriber by email
# ---------------------------------------------------------------------------

class LookupSubscriberEmailTest(TestCase):
    def test_lookup_by_email_only_resolves(self):
        r, _, _ = _seed('la')
        sub = Subscriber.objects.create(
            reseller=r, phone='2348011112222', verified=True,
            email='Jane@example.com',
        )
        ctx = ToolContext(reseller=r)
        out = tool_lookup_subscriber(ctx, {'email': 'jane@example.com'})
        self.assertTrue(out['found'])
        self.assertEqual(out['subscriber_id'], sub.pk)
        self.assertEqual(out['email'], 'Jane@example.com')

    def test_lookup_falls_back_to_last_10_digits(self):
        r, _, _ = _seed('lb')
        sub = Subscriber.objects.create(reseller=r, phone='2348011112222')
        ctx = ToolContext(reseller=r)
        # Customer wrote the E.164 form — stored form is local-ish.
        out = tool_lookup_subscriber(ctx, {'phone': '+2348011112222'})
        self.assertTrue(out['found'])
        self.assertEqual(out['subscriber_id'], sub.pk)

    def test_missing_args_error(self):
        r, _, _ = _seed('lc')
        out = tool_lookup_subscriber(ToolContext(reseller=r), {})
        self.assertIn('error', out)

    def test_cross_reseller_isolation(self):
        r1, _, _ = _seed('iso1')
        r2, _, _ = _seed('iso2')
        Subscriber.objects.create(
            reseller=r2, phone='2348099999999', email='only@r2.com',
        )
        out = tool_lookup_subscriber(
            ToolContext(reseller=r1), {'email': 'only@r2.com'},
        )
        self.assertFalse(out.get('found'))


# ---------------------------------------------------------------------------
# Data cap exhaustion
# ---------------------------------------------------------------------------

class DataCapExhaustionTest(TestCase):
    def test_exhausted_when_total_usage_exceeds_cap(self):
        r, _, plan = _seed('dc1')
        sub = Subscriber.objects.create(reseller=r, phone='2348011113333')
        Subscription.objects.create(
            subscriber=sub, plan=plan, reseller=r, status='active',
            start_date=timezone.now() - timedelta(days=5),
            expiry_date=timezone.now() + timedelta(days=25),
        )
        # 12 GB total — exceeds the 10 GB cap
        DailyUsageSnapshot.objects.create(
            subscriber=sub, date=timezone.now().date() - timedelta(days=2),
            download_bytes=6 * 1024 ** 3, upload_bytes=0,
        )
        DailyUsageSnapshot.objects.create(
            subscriber=sub, date=timezone.now().date() - timedelta(days=1),
            download_bytes=6 * 1024 ** 3, upload_bytes=0,
        )
        out = check_data_cap_remaining(sub)
        self.assertTrue(out['exhausted'])
        self.assertAlmostEqual(out['used_gb'], 12.0, places=2)
        self.assertEqual(out['cap_gb'], 10.0)

    def test_unlimited_plan_never_exhausted_even_if_heavy_usage(self):
        r, _, _ = _seed('dc2')
        uncapped = ServicePlan.objects.create(
            reseller=r, name='Unl', slug='unl',
            price_ngn=Decimal('0'), duration_days=30,
            download_mbps=5, upload_mbps=2, is_active=True,
            data_cap_gb=None,
        )
        sub = Subscriber.objects.create(reseller=r, phone='2348011114444')
        Subscription.objects.create(
            subscriber=sub, plan=uncapped, reseller=r, status='active',
            start_date=timezone.now() - timedelta(days=1),
            expiry_date=timezone.now() + timedelta(days=29),
        )
        DailyUsageSnapshot.objects.create(
            subscriber=sub, date=timezone.now().date(),
            download_bytes=500 * 1024 ** 3, upload_bytes=0,
        )
        out = check_data_cap_remaining(sub)
        self.assertFalse(out['exhausted'])

    def test_categorise_returns_data_cap_exhausted_cause(self):
        facts = DiagnosticFacts(
            subscription_active=True,
            subscription_expired=False,
            data_cap_exhausted=True,
        )
        cause, action = categorise_cause(facts)
        self.assertEqual(cause, Ticket.CAUSE_DATA_CAP_EXHAUSTED)
        self.assertEqual(action, Ticket.ACTION_CUSTOMER_ACTION)

    def test_categorise_outage_beats_data_cap(self):
        facts = DiagnosticFacts(
            router_offline=True,
            subscription_active=True,
            data_cap_exhausted=True,
        )
        cause, action = categorise_cause(facts)
        self.assertEqual(cause, Ticket.CAUSE_GENERAL_OUTAGE)

    def test_tool_wrapper_not_found(self):
        r, _, _ = _seed('dc3')
        out = tool_check_data_cap_remaining(
            ToolContext(reseller=r), {'subscriber_id': 999999},
        )
        self.assertIn('error', out)


# ---------------------------------------------------------------------------
# Reseller-wide outage
# ---------------------------------------------------------------------------

class ResellerWideOutageTest(TestCase):
    def test_no_routers_is_not_outage(self):
        r, _, _ = _seed('ow1')
        out = check_reseller_wide_outage(r)
        self.assertFalse(out['is_general_outage'])
        self.assertEqual(out['total_routers'], 0)

    def test_two_offline_routers_flags_outage(self):
        from routers.models import Router
        r, _, _ = _seed('ow2')
        now = timezone.now()
        Router.objects.create(reseller=r, serial_number='OW2-A',
                              status='offline',
                              last_seen=now - timedelta(hours=1))
        Router.objects.create(reseller=r, serial_number='OW2-B',
                              status='offline',
                              last_seen=now - timedelta(hours=1))
        Router.objects.create(reseller=r, serial_number='OW2-C',
                              status='online', last_seen=now)
        out = check_reseller_wide_outage(r)
        self.assertTrue(out['is_general_outage'])
        self.assertEqual(out['offline_count'], 2)


# ---------------------------------------------------------------------------
# Conversation state machine tools
# ---------------------------------------------------------------------------

class ConversationStateTest(TestCase):
    def setUp(self):
        self.r, _, _ = _seed('st1')
        self.sub = Subscriber.objects.create(
            reseller=self.r, phone='2348011115555', verified=True,
        )
        self.convo = Conversation.objects.create(
            reseller=self.r, channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='2348011115555@s.whatsapp.net',
            contact_phone='2348011115555', subscriber=self.sub,
        )

    def test_set_state_persists_and_merges_clues(self):
        ctx = ToolContext(reseller=self.r, conversation=self.convo)
        tool_conversation_set_state(ctx, {
            'step': 'layer1_power',
            'clues': {'power': 'lights are on'},
            'router_id': 42,
        })
        self.convo.refresh_from_db()
        state = self.convo.diagnostic_state
        self.assertEqual(state['step'], 'layer1_power')
        self.assertEqual(state['clues']['power'], 'lights are on')
        self.assertEqual(state['router_id'], 42)

        # Second call merges new clues without wiping old ones
        tool_conversation_set_state(ctx, {
            'step': 'layer1_lights',
            'clues': {'lights': 'all green'},
        })
        self.convo.refresh_from_db()
        state = self.convo.diagnostic_state
        self.assertEqual(state['step'], 'layer1_lights')
        self.assertEqual(state['clues']['power'], 'lights are on')  # kept
        self.assertEqual(state['clues']['lights'], 'all green')     # added

    def test_set_state_rejects_unknown_step(self):
        ctx = ToolContext(reseller=self.r, conversation=self.convo)
        out = tool_conversation_set_state(ctx, {'step': 'not_a_step'})
        self.assertIn('error', out)

    def test_get_state_returns_current_snapshot(self):
        self.convo.diagnostic_state = {'step': 'classify', 'clues': {'a': 1}}
        self.convo.save(update_fields=['diagnostic_state'])
        ctx = ToolContext(reseller=self.r, conversation=self.convo)
        out = tool_conversation_get_state(ctx, {})
        self.assertEqual(out['state']['step'], 'classify')


# ---------------------------------------------------------------------------
# Account summary formatting
# ---------------------------------------------------------------------------

class AccountSummaryTest(TestCase):
    def test_summary_for_active_plan_mentions_days_left(self):
        r, _, plan = _seed('as1')
        sub = Subscriber.objects.create(reseller=r, phone='2348011116666')
        Subscription.objects.create(
            subscriber=sub, plan=plan, reseller=r, status='active',
            start_date=timezone.now() - timedelta(days=1),
            expiry_date=timezone.now() + timedelta(days=7, hours=1),
        )
        ctx = ToolContext(reseller=r)
        out = tool_get_account_summary_for_customer(ctx, {'subscriber_id': sub.pk})
        self.assertIn('Home Lite', out['summary'])
        self.assertIn('7 days left', out['summary'])
        self.assertTrue(out['subscription_active'])

    def test_summary_for_no_subscription(self):
        r, _, _ = _seed('as2')
        sub = Subscriber.objects.create(reseller=r, phone='2348011117777')
        out = tool_get_account_summary_for_customer(
            ToolContext(reseller=r), {'subscriber_id': sub.pk},
        )
        self.assertFalse(out['subscription_active'])
        self.assertIn('no active subscription', out['summary'])


# ---------------------------------------------------------------------------
# Pre-router: YES / NO classifier + satisfaction reply handler
# ---------------------------------------------------------------------------

class SatisfactionReplyTest(TestCase):
    def setUp(self):
        self.r, _, _ = _seed('sr1')
        self.sub = Subscriber.objects.create(
            reseller=self.r, phone='2348011118888', verified=True,
        )
        self.convo = Conversation.objects.create(
            reseller=self.r, channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='2348011118888@s.whatsapp.net',
            contact_phone='2348011118888', subscriber=self.sub,
        )
        self.ticket = create_ticket(
            reseller=self.r, type=Ticket.TYPE_SUPPORT,
            subject='No internet', body='', conversation=self.convo,
            subscriber=self.sub,
        )
        change_status(self.ticket, Ticket.STATUS_RESOLVED,
                      actor='human:1', note='Reseated the cable')
        self.ticket.refresh_from_db()
        # Stamp awaiting flag as if the satisfaction ping had just fired.
        self.convo.diagnostic_state = {
            'step': 'awaiting_confirmation',
            'awaiting_confirmation_ticket_id': self.ticket.pk,
            'updated_at': timezone.now().isoformat(),
        }
        self.convo.save(update_fields=['diagnostic_state'])

    def _inbound(self, body):
        return Message.objects.create(
            conversation=self.convo, direction=Message.DIRECTION_IN,
            body=body, source=Message.SOURCE_CUSTOMER,
        )

    def test_yes_closes_ticket_and_clears_awaiting(self):
        msg = self._inbound('yes thanks')
        with patch('notifications.notify.send_whatsapp', return_value=True):
            verdict = ai_jobs._handle_satisfaction_reply(msg, self.convo)
        self.assertEqual(verdict['verdict'], 'yes')
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.STATUS_CLOSED)
        self.convo.refresh_from_db()
        self.assertIsNone(
            self.convo.diagnostic_state.get('awaiting_confirmation_ticket_id'),
        )

    def test_no_reopens_ticket_and_resets_step_to_classify(self):
        msg = self._inbound('no still broken')
        with patch('notifications.notify.send_whatsapp', return_value=True):
            verdict = ai_jobs._handle_satisfaction_reply(msg, self.convo)
        self.assertEqual(verdict['verdict'], 'no')
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.STATUS_IN_PROGRESS)
        # Reopen event logged
        self.assertTrue(
            TicketEvent.objects.filter(
                ticket=self.ticket, kind=TicketEvent.KIND_REOPENED,
            ).exists()
        )
        self.convo.refresh_from_db()
        self.assertEqual(self.convo.diagnostic_state['step'], 'classify')

    def test_ambiguous_reply_clears_awaiting_and_falls_through(self):
        msg = self._inbound('umm i have a new question actually')
        with patch('notifications.notify.send_whatsapp', return_value=True):
            verdict = ai_jobs._handle_satisfaction_reply(msg, self.convo)
        self.assertIsNone(verdict)  # Falls through to normal router
        self.convo.refresh_from_db()
        self.assertIsNone(
            self.convo.diagnostic_state.get('awaiting_confirmation_ticket_id'),
        )
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.STATUS_RESOLVED)

    def test_yes_no_classifier_edge_cases(self):
        # Spot-check the classifier so regressions surface loudly.
        self.assertEqual(ai_jobs._classify_yes_no('YES!'), 'yes')
        self.assertEqual(ai_jobs._classify_yes_no('thanks its working'), 'yes')
        self.assertEqual(ai_jobs._classify_yes_no('still bad'), 'no')
        self.assertEqual(ai_jobs._classify_yes_no('not working at all'), 'no')
        self.assertEqual(ai_jobs._classify_yes_no('hmm'), '')


# ---------------------------------------------------------------------------
# Satisfaction ping firing + auto-close sweep
# ---------------------------------------------------------------------------

class SatisfactionPingLifecycleTest(TestCase):
    def setUp(self):
        self.r, _, _ = _seed('sp1')
        self.sub = Subscriber.objects.create(
            reseller=self.r, phone='2348011119999',
        )
        self.convo = Conversation.objects.create(
            reseller=self.r, channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='2348011119999@s.whatsapp.net',
            contact_phone='2348011119999', subscriber=self.sub,
        )
        self.ticket = create_ticket(
            reseller=self.r, type=Ticket.TYPE_SUPPORT,
            subject='Help', body='', conversation=self.convo,
            subscriber=self.sub,
        )
        change_status(self.ticket, Ticket.STATUS_RESOLVED, actor='human:1')

    def test_send_ping_stamps_awaiting_when_still_resolved(self):
        with patch('notifications.notify.send_whatsapp', return_value=True):
            result = ai_jobs.send_satisfaction_ping(self.ticket.pk)
        self.assertTrue(result.get('sent'))
        self.convo.refresh_from_db()
        state = self.convo.diagnostic_state
        self.assertEqual(state['step'], 'awaiting_confirmation')
        self.assertEqual(
            state['awaiting_confirmation_ticket_id'], self.ticket.pk,
        )

    def test_send_ping_skips_when_reopened(self):
        # Reopen between scheduling and firing
        change_status(self.ticket, Ticket.STATUS_IN_PROGRESS, actor='human:1')
        result = ai_jobs.send_satisfaction_ping(self.ticket.pk)
        self.assertTrue(result.get('skipped'))

    def test_stale_awaiting_sweep_auto_closes_after_24h(self):
        # Stamp the awaiting flag 25h in the past
        old = (timezone.now() - timedelta(hours=25)).isoformat()
        self.convo.diagnostic_state = {
            'step': 'awaiting_confirmation',
            'awaiting_confirmation_ticket_id': self.ticket.pk,
            'updated_at': old,
        }
        self.convo.save(update_fields=['diagnostic_state'])
        result = ai_jobs.sweep_stale_awaiting_confirmations(max_age_hours=24)
        self.assertEqual(result['closed'], 1)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.STATUS_CLOSED)
