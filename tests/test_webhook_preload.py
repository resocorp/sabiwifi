"""PR B — webhook-time account preload.

Verifies that `_link_existing_contact` populates `_preloaded_account` and
`phone_matches_account` into `Conversation.diagnostic_state` at webhook
time, so the CustomerAgent's first turn on a known subscriber can greet
with plan / expiry without a tool roundtrip. Runs inside the
`select_for_update()` lock held by `record_inbound_message`.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.models import Reseller, Subscriber
from conversations.models import Conversation, Message
from conversations.services import record_inbound_message
from plans.models import ServicePlan, Subscription


def _mk_reseller(slug='preload-test'):
    user = User.objects.create_user(
        username=f'{slug}@x', password='x', email=f'user-{slug}@x',
    )
    return Reseller.objects.create(
        user=user, slug=slug, name=slug.title(),
        email=f'biz-{slug}@example.com',
        phone=f'+23480{abs(hash(slug)) % 10**9:09d}',
        paystack_subaccount_code='ACCT_test',
        payment_verified=True,
    )


def _mk_active_subscriber(r, phone='08011112222'):
    sub = Subscriber.objects.create(reseller=r, phone=phone)
    plan = ServicePlan.objects.create(
        reseller=r, name='Unlimited', slug='unlimited',
        download_mbps=10, upload_mbps=5, duration_days=30,
        price_ngn=Decimal('2000'),
    )
    now = timezone.now()
    Subscription.objects.create(
        subscriber=sub, plan=plan, reseller=r,
        start_date=now - timedelta(days=1),
        expiry_date=now + timedelta(days=29),
        status='active',
    )
    return sub


class WebhookPreloadTests(TestCase):
    def _inbound(self, *, reseller, sender_phone, external_thread_id=None,
                 body='Hi', external_message_id=''):
        return record_inbound_message(
            reseller=reseller,
            channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id=external_thread_id or sender_phone,
            body=body,
            attachments=None,
            external_message_id=external_message_id,
            sender_phone=sender_phone,
            display_name='Test Customer',
        )

    def test_matching_phone_preloads_account_summary(self):
        """Inbound from the registered phone → conversation.subscriber_id set,
        _preloaded_account.summary populated, phone_matches_account=True.
        """
        r = _mk_reseller('matching')
        _mk_active_subscriber(r, phone='08011112222')

        msg = self._inbound(reseller=r, sender_phone='08011112222')
        self.assertIsNotNone(msg)

        conv = msg.conversation
        self.assertIsNotNone(conv.subscriber_id)
        state = conv.diagnostic_state or {}
        self.assertIn('_preloaded_account', state)
        summary = state['_preloaded_account']
        self.assertIn('summary', summary)
        self.assertIn('Unlimited', summary['summary'])
        self.assertTrue(summary['subscription_active'])
        self.assertTrue(state.get('phone_matches_account'))

    def test_unknown_phone_does_not_preload(self):
        """No matching subscriber → no subscriber link, no preload."""
        r = _mk_reseller('unknown')
        msg = self._inbound(reseller=r, sender_phone='08099990000')
        conv = msg.conversation
        self.assertIsNone(conv.subscriber_id)
        state = conv.diagnostic_state or {}
        self.assertNotIn('_preloaded_account', state)
        self.assertNotIn('phone_matches_account', state)

    def test_preload_does_not_clobber_existing_state(self):
        """If diagnostic_state already has content (e.g. from an earlier turn),
        preload should add to it, not replace it wholesale.
        """
        r = _mk_reseller('preserve-state')
        _mk_active_subscriber(r, phone='08044445555')
        # Seed a conversation with prior state, no subscriber link yet.
        pre = Conversation.objects.create(
            reseller=r, channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='08044445555',
            contact_phone='08044445555',
            diagnostic_state={'step': 'enquiry', 'clues': {'intent': 'pricing'}},
        )
        msg = self._inbound(
            reseller=r, sender_phone='08044445555',
            external_thread_id='08044445555',
        )
        conv = msg.conversation
        self.assertEqual(conv.pk, pre.pk)
        state = conv.diagnostic_state or {}
        # Prior state preserved.
        self.assertEqual(state.get('step'), 'enquiry')
        self.assertEqual((state.get('clues') or {}).get('intent'), 'pricing')
        # New fields added.
        self.assertIn('_preloaded_account', state)
        self.assertTrue(state.get('phone_matches_account'))

    def test_preload_failure_does_not_block_inbound(self):
        """If build_account_summary raises, the inbound write path must still
        succeed — the agent will fall back to calling the tool itself.
        """
        from unittest.mock import patch
        r = _mk_reseller('preload-fail')
        _mk_active_subscriber(r, phone='08077778888')

        with patch('ai.tools.build_account_summary', side_effect=RuntimeError('boom')):
            msg = self._inbound(reseller=r, sender_phone='08077778888')

        conv = msg.conversation
        # Subscriber still linked; preload silently absent.
        self.assertIsNotNone(conv.subscriber_id)
        state = conv.diagnostic_state or {}
        self.assertNotIn('_preloaded_account', state)
