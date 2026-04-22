"""
Billing helpers that span models — kept thin so views can stay DRF-shaped.
"""
import logging
import secrets
from decimal import Decimal

from django.conf import settings

from billing.models import Payment

logger = logging.getLogger(__name__)


def _generate_reference(prefix='sw'):
    return f'{prefix}_{secrets.token_hex(16)}'


def create_lead_payment_link(lead, amount, description=''):
    """
    Build a Paystack payment link for a Lead (pre-subscriber). Returns
    (Payment, authorization_url).

    The payment still routes through the reseller's subaccount so the existing
    split-payment snapshot logic is preserved. On webhook success, the lead is
    converted to a Subscriber and an InstallationOrder ticket is raised.
    """
    reseller = lead.reseller
    if not reseller.paystack_subaccount_code or not reseller.payment_verified:
        raise ValueError('Reseller has no verified Paystack subaccount.')
    if not lead.phone:
        raise ValueError('Lead has no phone number.')

    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError('amount must be positive.')

    commission_pct = reseller.get_commission_pct()
    fee_bearer = reseller.get_fee_bearer()
    platform_amount = (amount * commission_pct / Decimal('100')).quantize(Decimal('0.01'))
    reseller_amount = amount - platform_amount

    reference = _generate_reference()
    payment = Payment.objects.create(
        subscriber=None,
        plan=lead.interested_plan,
        reseller=reseller,
        lead=lead,
        payment_type='lead_install',
        amount_ngn=amount,
        paystack_reference=reference,
        paystack_status='pending',
        commission_pct_applied=commission_pct,
        fee_bearer_applied=fee_bearer,
        platform_amount_ngn=platform_amount,
        reseller_amount_ngn=reseller_amount,
    )

    email = lead.email or f'{lead.phone}@sabiwifi.ng'
    from billing.providers.paystack import PaystackProvider
    provider = PaystackProvider()
    paystack_data = provider.initialize_payment(
        amount=float(amount),
        email=email,
        subaccount_code=reseller.paystack_subaccount_code,
        reference=reference,
        metadata={
            'kind': 'lead_install',
            'lead_id': lead.pk,
            'reseller_slug': reseller.slug,
            'payment_id': payment.pk,
            'description': description,
        },
    )
    if not paystack_data:
        payment.paystack_status = 'failed'
        payment.save(update_fields=['paystack_status', 'updated_at'])
        raise ValueError('Paystack initialisation failed.')

    return payment, paystack_data.get('authorization_url', '')


def create_renewal_payment_link(subscriber, plan=None, amount=None, description=''):
    """Build a Paystack payment link for an existing Subscriber renewing
    their plan. Returns (Payment, authorization_url).

    `plan` defaults to the subscriber's most recently active subscription's
    plan. `amount` defaults to that plan's price.
    """
    reseller = subscriber.reseller
    if not reseller.paystack_subaccount_code or not reseller.payment_verified:
        raise ValueError('Reseller has no verified Paystack subaccount.')
    if not subscriber.phone:
        raise ValueError('Subscriber has no phone number.')

    if plan is None:
        from plans.models import Subscription
        latest = (Subscription.objects.filter(subscriber=subscriber)
                  .order_by('-expiry_date').select_related('plan').first())
        if latest is None or latest.plan is None:
            raise ValueError('No prior subscription to derive a plan from.')
        plan = latest.plan
    if not plan.is_active:
        raise ValueError(f'Plan {plan.name!r} is no longer active.')

    if amount is None:
        amount = plan.price_ngn
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError('amount must be positive.')

    commission_pct = reseller.get_commission_pct()
    fee_bearer = reseller.get_fee_bearer()
    platform_amount = (amount * commission_pct / Decimal('100')).quantize(Decimal('0.01'))
    reseller_amount = amount - platform_amount

    reference = _generate_reference(prefix='rn')
    payment = Payment.objects.create(
        subscriber=subscriber,
        plan=plan,
        reseller=reseller,
        payment_type='plan',
        amount_ngn=amount,
        paystack_reference=reference,
        paystack_status='pending',
        commission_pct_applied=commission_pct,
        fee_bearer_applied=fee_bearer,
        platform_amount_ngn=platform_amount,
        reseller_amount_ngn=reseller_amount,
    )

    email = subscriber.email or f'{subscriber.phone}@sabiwifi.ng'
    from billing.providers.paystack import PaystackProvider
    provider = PaystackProvider()
    paystack_data = provider.initialize_payment(
        amount=float(amount),
        email=email,
        subaccount_code=reseller.paystack_subaccount_code,
        reference=reference,
        metadata={
            'kind': 'renewal',
            'subscriber_id': subscriber.pk,
            'plan_id': plan.pk,
            'reseller_slug': reseller.slug,
            'payment_id': payment.pk,
            'description': description,
        },
    )
    if not paystack_data:
        payment.paystack_status = 'failed'
        payment.save(update_fields=['paystack_status', 'updated_at'])
        raise ValueError('Paystack initialisation failed.')

    return payment, paystack_data.get('authorization_url', '')
