"""PR B — build_account_summary pure helper.

Unit tests for ai.tools.build_account_summary. Covers the four
subscription states the helper must handle (active, expired, none, with
payment history) plus the regression path — `tool_get_account_summary_for_customer`
is a thin wrapper and must return identical content for an active subscription.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.models import Reseller, Subscriber
from billing.models import Payment
from plans.models import ServicePlan, Subscription

from ai.tools import build_account_summary, tool_get_account_summary_for_customer


def _mk_reseller(slug='summary-test'):
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


def _mk_subscriber(reseller, phone='08012345678'):
    return Subscriber.objects.create(reseller=reseller, phone=phone)


def _mk_plan(reseller, name='Unlimited', price=Decimal('2000')):
    return ServicePlan.objects.create(
        reseller=reseller, name=name, slug=name.lower().replace(' ', '-'),
        download_mbps=10, upload_mbps=5, duration_days=30,
        price_ngn=price,
    )


class BuildAccountSummaryTests(TestCase):
    def test_active_subscription_shows_plan_and_days_left(self):
        r = _mk_reseller('active-sub')
        sub = _mk_subscriber(r)
        plan = _mk_plan(r, name='Unlimited')
        now = timezone.now()
        Subscription.objects.create(
            subscriber=sub, plan=plan, reseller=r,
            start_date=now - timedelta(days=3),
            expiry_date=now + timedelta(days=24),
            status='active',
        )
        result = build_account_summary(sub)
        self.assertTrue(result['subscription_active'])
        self.assertEqual(result['plan_name'], 'Unlimited')
        self.assertIn('Unlimited', result['summary'])
        # 24d - microsecond drift rounds down to 23 days left — tolerate both.
        self.assertRegex(result['summary'], r'(23|24) days left')

    def test_expired_subscription_flagged_in_summary(self):
        r = _mk_reseller('expired-sub')
        sub = _mk_subscriber(r)
        plan = _mk_plan(r, name='Starter')
        now = timezone.now()
        # Only an expired subscription exists.
        Subscription.objects.create(
            subscriber=sub, plan=plan, reseller=r,
            start_date=now - timedelta(days=40),
            expiry_date=now - timedelta(days=10),
            status='expired',
        )
        result = build_account_summary(sub)
        self.assertFalse(result['subscription_active'])
        self.assertIn('Starter', result['summary'])
        self.assertIn('expired', result['summary'])

    def test_no_subscription_yields_no_plan_summary(self):
        r = _mk_reseller('no-sub')
        sub = _mk_subscriber(r)
        result = build_account_summary(sub)
        self.assertFalse(result['subscription_active'])
        self.assertEqual(result['plan_name'], '')
        self.assertIn('no active subscription', result['summary'])

    def test_payment_history_appended_to_summary(self):
        r = _mk_reseller('pay-history')
        sub = _mk_subscriber(r)
        plan = _mk_plan(r, name='Unlimited', price=Decimal('2000'))
        now = timezone.now()
        Subscription.objects.create(
            subscriber=sub, plan=plan, reseller=r,
            start_date=now - timedelta(days=3),
            expiry_date=now + timedelta(days=24),
            status='active',
        )
        Payment.objects.create(
            reseller=r, subscriber=sub, plan=plan,
            amount_ngn=Decimal('2000'),
            paystack_status='success',
            paystack_reference='ref-paid-1',
        )
        result = build_account_summary(sub)
        self.assertIn('last paid ₦2000', result['summary'])


class ToolWrapperParityTests(TestCase):
    """Guardrail: the tool must remain a thin wrapper over the helper so we
    never regress the contract the prompt relies on.
    """

    def test_tool_wrapper_matches_helper_output(self):
        r = _mk_reseller('wrapper')
        sub = _mk_subscriber(r)
        plan = _mk_plan(r)
        now = timezone.now()
        Subscription.objects.create(
            subscriber=sub, plan=plan, reseller=r,
            start_date=now - timedelta(days=1),
            expiry_date=now + timedelta(days=14),
            status='active',
        )

        # Fabricate a minimal ToolContext for the tool call.
        from ai.tools import ToolContext
        ctx = ToolContext(reseller=r, conversation=None, lead=None, run_id=None)

        helper = build_account_summary(sub)
        wrapper = tool_get_account_summary_for_customer(ctx, {'subscriber_id': sub.pk})
        self.assertEqual(helper, wrapper)

    def test_tool_wrapper_returns_error_when_subscriber_missing(self):
        r = _mk_reseller('wrapper-missing')
        from ai.tools import ToolContext
        ctx = ToolContext(reseller=r, conversation=None, lead=None, run_id=None)
        result = tool_get_account_summary_for_customer(ctx, {'subscriber_id': 99999})
        self.assertEqual(result, {'error': 'subscriber not found'})
