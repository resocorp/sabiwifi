"""
Public recharge flow on sabiwifi.com — phone lookup, OTP login, plan recharge.

This is the consumer-facing recharge widget that lets subscribers top up from
the public landing page. End-to-end SabiWiFi-branded; partner identity is only
exposed in the disambiguation step (when the same phone matches multiple
partners). Backend revenue split is unchanged — the partner whose subscriber
recharged still receives their Paystack subaccount payout.

Endpoints:
    POST /api/recharge/lookup/          — phone -> matches; auto-sends OTP if single
    POST /api/recharge/send-otp/        — phone + subscriber_id -> sends OTP (N>1 case)
    POST /api/recharge/verify/          — otp_token + code -> auth_token
    POST /api/recharge/initiate-payment/ — plan_id (auth'd) -> Paystack access_code
    POST /api/recharge/complete/        — reference (auth'd) -> activates subscription
"""
import secrets
import logging
import uuid
import requests as http_requests
from django.core.cache import cache
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from accounts.models import Subscriber, Reseller
from plans.models import ServicePlan
from billing.models import Payment
from portal.views import (
    _normalize_phone,
    _get_client_ip,
    _check_otp_limits,
    _record_otp_sent,
    _generate_otp,
    _verify_paystack_payment,
    _get_paystack_secret_key,
    _get_paystack_public_key,
    _create_subscription,
)

logger = logging.getLogger(__name__)


def _mask_phone(phone):
    """08066137843 -> 080****7843. For display only."""
    if not phone or len(phone) < 7:
        return phone
    return f'{phone[:3]}****{phone[-4:]}'


def _send_recharge_otp(subscriber, phone, ip):
    """Generate + send an OTP for a single subscriber. Returns (otp_token, error)."""
    allowed, limit_error = _check_otp_limits(phone, ip)
    if not allowed:
        return None, limit_error

    otp = _generate_otp()
    otp_token = secrets.token_hex(24)
    cache.set(f'recharge_otp_{otp_token}', {
        'otp': otp,
        'phone': phone,
        'subscriber_id': subscriber.id,
        'reseller_id': subscriber.reseller_id,
        'attempts': 0,
    }, timeout=600)

    from notifications.sms import get_sms_service
    sms = get_sms_service()
    intl = subscriber.country.to_international(phone) if subscriber.country else phone
    sms.send_otp(intl, otp)
    _record_otp_sent(phone, ip)
    logger.info(f"recharge OTP sent to {phone} for subscriber {subscriber.id}")
    return otp_token, None


@api_view(['POST'])
@permission_classes([AllowAny])
def recharge_lookup(request):
    """
    Phone -> list of matching subscriber accounts. If exactly one match,
    auto-sends OTP and returns otp_token. If N>1, frontend prompts the user
    to pick a network, then calls send-otp with the chosen subscriber_id.
    """
    raw_phone = request.data.get('phone', '')
    phone, country = _normalize_phone(raw_phone)
    if not phone:
        return Response({'error': 'Please enter a valid phone number.'}, status=400)

    subscribers = list(
        Subscriber.objects.filter(phone=phone).select_related('reseller')
    )

    if not subscribers:
        return Response({
            'matches': [],
            'error': "We couldn't find an account for that number. Connect to a SabiWiFi network first to create an account.",
        }, status=404)

    if len(subscribers) == 1:
        otp_token, err = _send_recharge_otp(subscribers[0], phone, _get_client_ip(request))
        if err:
            return Response({'error': err}, status=429)
        return Response({
            'matches': [{
                'subscriber_id': subscribers[0].id,
                'network_name': subscribers[0].reseller.name,
            }],
            'otp_sent': True,
            'otp_token': otp_token,
            'masked_phone': _mask_phone(phone),
        })

    # N>1 — return options without sending OTP
    return Response({
        'matches': [
            {'subscriber_id': s.id, 'network_name': s.reseller.name}
            for s in subscribers
        ],
        'otp_sent': False,
        'masked_phone': _mask_phone(phone),
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def recharge_send_otp(request):
    """
    Send OTP for a chosen subscriber account when phone matched multiple
    networks. Frontend supplies the selected subscriber_id from the lookup
    response.
    """
    raw_phone = request.data.get('phone', '')
    subscriber_id = request.data.get('subscriber_id')

    phone, _ = _normalize_phone(raw_phone)
    if not phone or not subscriber_id:
        return Response({'error': 'Missing phone or selection.'}, status=400)

    try:
        subscriber = Subscriber.objects.select_related('reseller').get(
            id=subscriber_id, phone=phone,
        )
    except Subscriber.DoesNotExist:
        return Response({'error': 'Account not found.'}, status=404)

    otp_token, err = _send_recharge_otp(subscriber, phone, _get_client_ip(request))
    if err:
        return Response({'error': err}, status=429)
    return Response({
        'otp_token': otp_token,
        'masked_phone': _mask_phone(phone),
        'network_name': subscriber.reseller.name,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def recharge_verify(request):
    """Verify OTP -> rotate auth_token, return it for subsequent recharge calls."""
    otp_token = request.data.get('otp_token', '')
    code = (request.data.get('code') or '').strip()

    if not otp_token or not code:
        return Response({'error': 'Code required.'}, status=400)

    cache_key = f'recharge_otp_{otp_token}'
    data = cache.get(cache_key)
    if not data:
        return Response({'error': 'Code expired. Please request a new one.'}, status=400)

    if data['attempts'] >= 5:
        cache.delete(cache_key)
        return Response({'error': 'Too many attempts. Please request a new code.'}, status=400)

    if data['otp'] != code:
        data['attempts'] += 1
        cache.set(cache_key, data, timeout=600)
        return Response({'error': 'Invalid code.'}, status=400)

    try:
        subscriber = Subscriber.objects.select_related('reseller').get(id=data['subscriber_id'])
    except Subscriber.DoesNotExist:
        cache.delete(cache_key)
        return Response({'error': 'Account not found.'}, status=404)

    subscriber.generate_auth_token()
    subscriber.verified = True
    subscriber.save(update_fields=['auth_token', 'verified'])
    cache.delete(cache_key)

    return Response({
        'auth_token': subscriber.auth_token,
        'reseller_slug': subscriber.reseller.slug,
        'phone': subscriber.phone,
    })


def _auth_subscriber(request):
    """Resolve subscriber from X-Auth-Token. Returns (subscriber, None) or (None, Response)."""
    token = request.headers.get('X-Auth-Token', '') or request.META.get('HTTP_X_AUTH_TOKEN', '')
    if not token:
        return None, Response({'error': 'Authentication required.'}, status=401)
    try:
        return Subscriber.objects.select_related('reseller').get(auth_token=token), None
    except Subscriber.DoesNotExist:
        return None, Response({'error': 'Invalid session. Please log in again.'}, status=401)


@api_view(['POST'])
@permission_classes([AllowAny])
def recharge_initiate_payment(request):
    """
    Init Paystack for a recharge. Auth'd via X-Auth-Token. The plan must
    belong to the subscriber's reseller; the existing split logic routes
    the partner's share to their Paystack subaccount.
    """
    subscriber, err_resp = _auth_subscriber(request)
    if err_resp:
        return err_resp

    plan_id = request.data.get('plan_id')
    if not plan_id:
        return Response({'error': 'plan_id required.'}, status=400)

    reseller = subscriber.reseller
    try:
        plan = ServicePlan.objects.get(id=plan_id, reseller=reseller, is_active=True)
    except ServicePlan.DoesNotExist:
        return Response({'error': 'Plan not found.'}, status=404)

    if plan.is_free:
        # Free plan — activate immediately, no Paystack
        from plans.services import activate_subscription
        sub = activate_subscription(subscriber, plan, reseller=reseller)
        Payment.objects.create(
            subscriber=subscriber, plan=plan, reseller=reseller,
            amount_ngn=0,
            paystack_reference=f'free_{secrets.token_hex(16)}',
            paystack_status='success', payment_method='free',
        )
        return Response({
            'requires_payment': False,
            'message': f'{plan.name} activated.',
            'expiry_date': sub.expiry_date.isoformat(),
        })

    if not reseller.payment_verified or not reseller.paystack_subaccount_code:
        return Response({'error': 'This network does not accept online payments yet.'}, status=400)

    secret_key = _get_paystack_secret_key()
    public_key = _get_paystack_public_key()
    if not secret_key or not public_key:
        return Response({'error': 'Payment gateway not configured.'}, status=503)

    amount_kobo = int(plan.price_ngn * 100)
    reference = f'rcg_{uuid.uuid4().hex[:20]}'
    commission_pct = reseller.get_commission_pct()
    fee_bearer = reseller.get_fee_bearer()
    platform_share_kobo = int(amount_kobo * commission_pct / 100)

    payload = {
        'email': subscriber.email or f'{subscriber.phone}@sabiwifi.local',
        'amount': amount_kobo,
        'reference': reference,
        'subaccount': reseller.paystack_subaccount_code,
        'bearer': fee_bearer,
        'transaction_charge': platform_share_kobo,
        'metadata': {
            'custom_fields': [
                {'display_name': 'Phone', 'variable_name': 'phone', 'value': subscriber.phone},
                {'display_name': 'Plan', 'variable_name': 'plan', 'value': plan.name},
                {'display_name': 'Network', 'variable_name': 'network', 'value': reseller.name},
                {'display_name': 'Source', 'variable_name': 'source', 'value': 'sabiwifi.com recharge'},
            ],
        },
    }

    try:
        resp = http_requests.post(
            'https://api.paystack.co/transaction/initialize',
            json=payload,
            headers={'Authorization': f'Bearer {secret_key}'},
            timeout=15,
        )
        body = resp.json()
    except Exception as exc:
        logger.error(f'Paystack recharge init error: {exc}')
        return Response({'error': 'Payment gateway unavailable.'}, status=502)

    if not body.get('status'):
        logger.error(f'Paystack recharge init failed: {body}')
        return Response({'error': body.get('message', 'Could not initialize payment.')}, status=400)

    cache.set(f'recharge_pending_{reference}', {
        'subscriber_id': subscriber.id,
        'plan_id': plan.id,
        'amount_kobo': amount_kobo,
    }, timeout=1800)

    return Response({
        'requires_payment': True,
        'reference': reference,
        'access_code': body['data']['access_code'],
        'public_key': public_key,
        'amount_kobo': amount_kobo,
        'plan_name': plan.name,
        'email': payload['email'],
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def recharge_complete(request):
    """Verify Paystack reference + activate the chosen plan for the subscriber."""
    subscriber, err_resp = _auth_subscriber(request)
    if err_resp:
        return err_resp

    reference = (request.data.get('reference') or '').strip()
    if not reference:
        return Response({'error': 'reference required.'}, status=400)

    pending = cache.get(f'recharge_pending_{reference}')
    if not pending or pending.get('subscriber_id') != subscriber.id:
        return Response({'error': 'Unknown payment reference.'}, status=400)

    paystack_data, pmt_error = _verify_paystack_payment(reference, pending['amount_kobo'])
    if not paystack_data:
        return Response({'error': pmt_error}, status=400)

    try:
        plan = ServicePlan.objects.get(id=pending['plan_id'])
    except ServicePlan.DoesNotExist:
        return Response({'error': 'Plan no longer available.'}, status=400)

    sub = _create_subscription(subscriber, plan, subscriber.reseller, paystack_data)
    cache.delete(f'recharge_pending_{reference}')

    return Response({
        'message': f'{plan.name} activated. You can browse now.',
        'plan_name': plan.name,
        'expiry_date': sub.expiry_date.isoformat(),
    })
