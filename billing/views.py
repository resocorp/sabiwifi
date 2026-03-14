"""Billing API views — Paystack payment initialization, webhooks, callbacks."""
import hashlib
import hmac
import json
import secrets
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status

from accounts.models import Subscriber
from plans.models import ServicePlan, Subscription
from billing.models import Payment
from radius.utils import assign_subscriber_to_plan

logger = logging.getLogger(__name__)


def _generate_reference():
    """Generate a unique payment reference."""
    return f'sw_{secrets.token_hex(16)}'


@api_view(['POST'])
@permission_classes([AllowAny])
def payment_initialize(request):
    """
    Create a Paystack payment with split to reseller subaccount.
    Called when subscriber selects a paid plan.
    """
    auth_token = request.headers.get('X-Auth-Token', '')
    plan_id = request.data.get('plan_id')

    if not auth_token:
        return Response({'error': 'Authentication required.'}, status=401)

    try:
        subscriber = Subscriber.objects.select_related('reseller').get(auth_token=auth_token)
    except Subscriber.DoesNotExist:
        return Response({'error': 'Invalid session.'}, status=401)

    try:
        plan = ServicePlan.objects.get(
            id=plan_id, reseller=subscriber.reseller, is_active=True
        )
    except ServicePlan.DoesNotExist:
        return Response({'error': 'Plan not found.'}, status=404)

    if plan.is_free:
        return Response({'error': 'This is a free plan. No payment needed.'}, status=400)

    reseller = subscriber.reseller

    # Guard: reseller must have bank verified
    if not reseller.payment_verified or not reseller.paystack_subaccount_code:
        return Response({'error': 'Payment not available for this network.'}, status=400)

    # Generate reference
    reference = _generate_reference()

    # Create pending payment record
    commission_pct = reseller.get_commission_pct()
    fee_bearer = reseller.get_fee_bearer()
    platform_amount = plan.price_ngn * commission_pct / Decimal('100')
    reseller_amount = plan.price_ngn - platform_amount

    payment = Payment.objects.create(
        subscriber=subscriber,
        plan=plan,
        reseller=reseller,
        amount_ngn=plan.price_ngn,
        paystack_reference=reference,
        paystack_status='pending',
        commission_pct_applied=commission_pct,
        fee_bearer_applied=fee_bearer,
        platform_amount_ngn=platform_amount,
        reseller_amount_ngn=reseller_amount,
    )

    # Initialize Paystack transaction
    from billing.providers.paystack import PaystackProvider
    provider = PaystackProvider()

    paystack_data = provider.initialize_payment(
        amount=float(plan.price_ngn),
        email=subscriber.email or f'{subscriber.phone}@sabiwifi.ng',
        subaccount_code=reseller.paystack_subaccount_code,
        reference=reference,
        metadata={
            'subscriber_phone': subscriber.phone,
            'plan_id': plan.id,
            'reseller_slug': reseller.slug,
            'payment_id': payment.id,
        }
    )

    if not paystack_data:
        payment.paystack_status = 'failed'
        payment.save()
        return Response({'error': 'Payment service unavailable. Please try again.'}, status=502)

    return Response({
        'authorization_url': paystack_data.get('authorization_url', ''),
        'reference': reference,
        'access_code': paystack_data.get('access_code', ''),
        'amount_ngn': str(plan.price_ngn),
        'plan_name': plan.name,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def payment_webhook(request):
    """
    Paystack webhook handler (charge.success).
    Verifies HMAC SHA-512 signature, activates plan.
    """
    # Verify signature
    signature = request.headers.get('X-Paystack-Signature', '')
    if settings.PAYSTACK_SECRET_KEY and signature:
        computed = hmac.new(
            settings.PAYSTACK_SECRET_KEY.encode('utf-8'),
            request.body,
            hashlib.sha512,
        ).hexdigest()
        if not hmac.compare_digest(computed, signature):
            logger.warning("Invalid Paystack webhook signature")
            return Response({'error': 'Invalid signature.'}, status=400)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return Response({'error': 'Invalid payload.'}, status=400)

    event = payload.get('event', '')
    data = payload.get('data', {})

    if event != 'charge.success':
        return Response({'status': 'ignored'})

    reference = data.get('reference', '')
    if not reference:
        return Response({'error': 'Missing reference.'}, status=400)

    # Idempotency check
    try:
        payment = Payment.objects.get(paystack_reference=reference)
    except Payment.DoesNotExist:
        logger.warning(f"Webhook for unknown reference: {reference}")
        return Response({'status': 'unknown_reference'})

    if payment.paystack_status == 'success':
        return Response({'status': 'already_processed'})

    # Activate the plan
    _activate_payment(payment, data)

    return Response({'status': 'ok'})


def _activate_payment(payment, paystack_data=None):
    """Activate a payment: create subscription, assign RADIUS group."""
    payment.paystack_status = 'success'

    # Extract payment method from Paystack data
    if paystack_data:
        channel = paystack_data.get('channel', '')
        if channel == 'card':
            payment.payment_method = 'card'
        elif channel == 'bank':
            payment.payment_method = 'bank_transfer'
        elif channel == 'ussd':
            payment.payment_method = 'ussd'

        # Update split amounts from actual Paystack data if available
        fees_split = paystack_data.get('fees_split', {})
        if fees_split:
            payment.gateway_fee_ngn = Decimal(str(paystack_data.get('fees', 0))) / 100

    payment.save()

    plan = payment.plan
    subscriber = payment.subscriber

    if not plan:
        logger.error(f"Payment {payment.id} has no plan.")
        return

    # Calculate expiry
    now = timezone.now()
    if plan.duration_days > 0:
        expiry = now + timedelta(days=plan.duration_days)
    elif plan.duration_hours > 0:
        expiry = now + timedelta(hours=float(plan.duration_hours))
    else:
        expiry = now + timedelta(days=365)

    # Expire current active subscriptions
    Subscription.objects.filter(
        subscriber=subscriber, status='active'
    ).update(status='expired')

    # Create new subscription
    Subscription.objects.create(
        subscriber=subscriber,
        plan=plan,
        reseller=payment.reseller,
        start_date=now,
        expiry_date=expiry,
        status='active',
    )

    # Update RADIUS group
    assign_subscriber_to_plan(subscriber, plan)

    logger.info(f"Plan {plan.name} activated for {subscriber.phone} (ref: {payment.paystack_reference})")


@api_view(['GET'])
@permission_classes([AllowAny])
def payment_callback(request):
    """Paystack redirect after payment completion."""
    reference = request.GET.get('reference', '') or request.GET.get('ref', '')

    if not reference:
        return Response({'error': 'Missing reference.'}, status=400)

    try:
        payment = Payment.objects.select_related('subscriber', 'plan', 'reseller').get(
            paystack_reference=reference
        )
    except Payment.DoesNotExist:
        return Response({'error': 'Payment not found.'}, status=404)

    # If still pending, verify with Paystack
    if payment.paystack_status == 'pending':
        from billing.providers.paystack import PaystackProvider
        provider = PaystackProvider()
        verify_data = provider.verify_payment(reference)

        if verify_data and verify_data.get('status') == 'success':
            _activate_payment(payment, verify_data)
        else:
            payment.paystack_status = 'failed'
            payment.save()

    return Response({
        'status': payment.paystack_status,
        'plan_name': payment.plan.name if payment.plan else '',
        'amount': str(payment.amount_ngn),
        'reference': reference,
    })


@api_view(['GET'])
@permission_classes([AllowAny])
def current_subscription(request):
    """Get current subscription details for the authenticated subscriber."""
    auth_token = request.headers.get('X-Auth-Token', '') or request.GET.get('token', '')

    if not auth_token:
        return Response({'error': 'Authentication required.'}, status=401)

    try:
        subscriber = Subscriber.objects.get(auth_token=auth_token)
    except Subscriber.DoesNotExist:
        return Response({'error': 'Invalid session.'}, status=401)

    current_sub = Subscription.objects.filter(
        subscriber=subscriber, status='active'
    ).select_related('plan').first()

    if not current_sub:
        return Response({'subscription': None})

    return Response({
        'subscription': {
            'plan_name': current_sub.plan.name,
            'speed_display': current_sub.plan.speed_display,
            'data_cap_display': current_sub.plan.data_cap_display,
            'max_devices': current_sub.plan.max_devices,
            'start_date': current_sub.start_date.isoformat(),
            'expiry_date': current_sub.expiry_date.isoformat(),
            'days_remaining': max(0, (current_sub.expiry_date - timezone.now()).days),
            'status': current_sub.status,
            'price_ngn': str(current_sub.plan.price_ngn),
        }
    })
