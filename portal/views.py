"""Portal API views — subscriber signup, OTP, login, plans, account, PIN management."""
import secrets
import logging
import re
import requests as http_requests
from django.utils import timezone
from django.core.cache import cache
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework import status

from accounts.models import Reseller, Subscriber
from plans.models import ServicePlan, Subscription
from billing.models import Payment
from radius.utils import assign_subscriber_to_plan, update_radcheck_password

logger = logging.getLogger(__name__)


class OTPRateThrottle(AnonRateThrottle):
    rate = '3/hour'

    def get_cache_key(self, request, view):
        phone = request.data.get('phone', '')
        if phone:
            return f'otp_throttle_{phone}'
        return self.get_ident(request)


def _normalize_phone(raw_phone, country=None):
    """
    Normalize a phone number to local storage format (e.g. 08066137843 for Nigeria).
    Accepts +234..., 234..., or 0... input.
    If country is None, defaults to Nigeria.
    Returns (local_phone, country) or (None, None) if invalid.
    """
    from accounts.models import Country as CountryModel
    if country is None:
        try:
            country = CountryModel.objects.get(code='NG')
        except CountryModel.DoesNotExist:
            return None, None
    local = country.normalize_to_local(raw_phone)
    if local is None:
        return None, None
    return local, country


def _get_client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR', '')


def _check_otp_limits(phone, ip):
    """
    Multi-layer rate limiting for OTP requests.
    Returns (allowed: bool, error_message: str | None).
    """
    # 1. Per-phone cooldown: 60s between sends
    cooldown_key = f'otp_cooldown_{phone}'
    if cache.get(cooldown_key):
        remaining = cache.ttl(cooldown_key) if hasattr(cache, 'ttl') else 60
        return False, f'Please wait before requesting another code.'

    # 2. Per-phone hourly limit: 5 OTPs/hour
    phone_hour_key = f'otp_phone_hour_{phone}'
    phone_hour = cache.get(phone_hour_key, 0)
    if phone_hour >= 5:
        return False, 'Too many codes requested for this number. Try again in an hour.'

    # 3. Per-phone daily limit: 10 OTPs/day
    phone_day_key = f'otp_phone_day_{phone}'
    phone_day = cache.get(phone_day_key, 0)
    if phone_day >= 10:
        return False, 'Daily verification limit reached for this number. Try again tomorrow.'

    # 4. Per-IP hourly limit: 15 OTPs/hour
    ip_hour_key = f'otp_ip_hour_{ip}'
    ip_hour = cache.get(ip_hour_key, 0)
    if ip_hour >= 15:
        return False, 'Too many requests from your device. Try again later.'

    # 5. Global circuit breaker: 200 OTPs/hour across all users
    global_key = 'otp_global_hour'
    global_count = cache.get(global_key, 0)
    if global_count >= 200:
        logger.error('OTP global circuit breaker triggered!')
        return False, 'Service is temporarily busy. Please try again in a few minutes.'

    return True, None


def _record_otp_sent(phone, ip):
    """Increment all OTP rate limit counters after a successful send."""
    # 60s cooldown per phone
    cache.set(f'otp_cooldown_{phone}', 1, timeout=60)

    # Hourly phone counter
    phone_hour_key = f'otp_phone_hour_{phone}'
    try:
        cache.incr(phone_hour_key)
    except ValueError:
        cache.set(phone_hour_key, 1, timeout=3600)

    # Daily phone counter
    phone_day_key = f'otp_phone_day_{phone}'
    try:
        cache.incr(phone_day_key)
    except ValueError:
        cache.set(phone_day_key, 1, timeout=86400)

    # Hourly IP counter
    ip_hour_key = f'otp_ip_hour_{ip}'
    try:
        cache.incr(ip_hour_key)
    except ValueError:
        cache.set(ip_hour_key, 1, timeout=3600)

    # Global hourly counter
    try:
        cache.incr('otp_global_hour')
    except ValueError:
        cache.set('otp_global_hour', 1, timeout=3600)


def _resolve_reseller(request):
    """Resolve reseller from serial (captive portal) or session (self-service)."""
    serial = request.data.get('serial') or request.GET.get('r', '')
    reseller_slug = request.data.get('reseller_slug') or request.GET.get('reseller', '')

    if serial:
        from routers.models import Router
        try:
            router = Router.objects.select_related('reseller').get(serial_number=serial.upper())
            return router.reseller
        except Router.DoesNotExist:
            return None
    elif reseller_slug:
        try:
            return Reseller.objects.get(slug=reseller_slug)
        except Reseller.DoesNotExist:
            return None
    return None


def _generate_otp():
    """Generate a 6-digit OTP code."""
    return f'{secrets.randbelow(1000000):06d}'


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_signup(request):
    """
    Register subscriber: phone + email + country → OTP sent.
    Requires reseller context (serial or reseller_slug).
    """
    from accounts.models import Country as CountryModel
    raw_phone = request.data.get('phone', '')
    email = request.data.get('email', '').strip().lower()
    country_code = request.data.get('country', 'NG').upper()

    try:
        country = CountryModel.objects.get(code=country_code, is_active=True)
    except CountryModel.DoesNotExist:
        return Response({'error': 'Selected country is not supported.'}, status=400)

    phone, country = _normalize_phone(raw_phone, country)
    if not phone:
        return Response({'error': f'Invalid phone number for {country_code}.'}, status=400)

    if not email:
        return Response({'error': 'Email is required for payment receipts.'}, status=400)

    ip = _get_client_ip(request)
    allowed, limit_error = _check_otp_limits(phone, ip)
    if not allowed:
        return Response({'error': limit_error}, status=429)

    reseller = _resolve_reseller(request)
    if not reseller:
        return Response({'error': 'Could not determine WiFi network.'}, status=400)

    # Check if subscriber already exists for this reseller
    if Subscriber.objects.filter(reseller=reseller, phone=phone).exists():
        return Response({'error': 'An account with this phone number already exists. Please log in.'}, status=400)

    # Check free subscriber limit
    if not reseller.payment_verified:
        active_count = Subscription.objects.filter(
            reseller=reseller, status='active'
        ).count()
        limit = reseller.get_free_subscriber_limit()
        if active_count >= limit:
            return Response({
                'error': 'This network is currently full. Please try again later.'
            }, status=400)

    # Generate OTP and store in cache
    otp = _generate_otp()
    cache_key = f'otp_{phone}_{reseller.id}'
    cache.set(cache_key, {
        'otp': otp,
        'phone': phone,
        'email': email,
        'reseller_id': reseller.id,
        'country_code': country.code,
        'attempts': 0,
    }, timeout=600)

    # Send OTP — convert to international format for SMS
    from notifications.sms import get_sms_service
    sms = get_sms_service()
    sms.send_otp(country.to_international(phone), otp)
    _record_otp_sent(phone, ip)
    logger.info(f"OTP sent to {phone} ({country.code})")

    return Response({
        'message': 'Verification code sent to your phone.',
        'phone': phone,
        'reseller_id': reseller.id,
        'country': country.code,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_verify_otp(request):
    """Verify the 6-digit OTP code."""
    raw_phone = request.data.get('phone', '')
    code = request.data.get('code', '').strip()
    reseller_id = request.data.get('reseller_id', '')
    country_code = request.data.get('country', 'NG').upper()

    phone, country = _normalize_phone(raw_phone)
    if not phone:
        return Response({'error': 'Invalid phone number.'}, status=400)

    if not code or len(code) != 6:
        return Response({'error': 'Please enter the 6-digit verification code.'}, status=400)

    cache_key = f'otp_{phone}_{reseller_id}'
    otp_data = cache.get(cache_key)

    if not otp_data:
        return Response({'error': 'Verification code expired. Please request a new one.'}, status=400)

    if otp_data['attempts'] >= 5:
        cache.delete(cache_key)
        return Response({'error': 'Too many attempts. Please request a new code.'}, status=400)

    if otp_data['otp'] != code:
        otp_data['attempts'] += 1
        cache.set(cache_key, otp_data, timeout=600)
        return Response({'error': 'Invalid verification code.'}, status=400)

    # OTP verified — create a verification token for the set-pin step
    verify_token = secrets.token_hex(32)
    cache.set(f'verified_{verify_token}', {
        'phone': phone,
        'email': otp_data['email'],
        'reseller_id': otp_data['reseller_id'],
    }, timeout=900)  # 15 minutes

    cache.delete(cache_key)

    return Response({
        'message': 'Phone verified!',
        'verify_token': verify_token,
    })


def _get_paystack_secret_key():
    """Return Paystack secret key from settings or PlatformSettings."""
    from django.conf import settings
    key = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
    if not key:
        from operator_panel.models import PlatformSettings
        key = PlatformSettings.load().paystack_secret_key
    return key


def _get_paystack_public_key():
    """Return Paystack public key from settings or PlatformSettings."""
    from django.conf import settings
    key = getattr(settings, 'PAYSTACK_PUBLIC_KEY', '')
    if not key:
        from operator_panel.models import PlatformSettings
        key = PlatformSettings.load().paystack_public_key
    return key


def _verify_paystack_payment(reference, expected_amount_kobo):
    """
    Verify a Paystack transaction. Returns (data_dict, None) on success
    or (None, error_string) on failure.
    """
    secret_key = _get_paystack_secret_key()
    try:
        resp = http_requests.get(
            f'https://api.paystack.co/transaction/verify/{reference}',
            headers={'Authorization': f'Bearer {secret_key}'},
            timeout=15,
        )
        body = resp.json()
    except Exception as exc:
        logger.error(f'Paystack verify request error: {exc}')
        return None, 'Payment gateway unavailable. Please contact support.'

    if not body.get('status') or body.get('data', {}).get('status') != 'success':
        return None, 'Payment was not successful. Please try again.'

    paid_amount = body['data'].get('amount', 0)
    if paid_amount < expected_amount_kobo:
        logger.warning(
            f'Paystack amount mismatch: expected {expected_amount_kobo}, got {paid_amount}'
        )
        return None, 'Payment amount does not match. Please contact support.'

    return body['data'], None


def _create_subscription(subscriber, plan, reseller, paystack_data=None):
    """
    Create a Subscription + optional Payment record, assign RADIUS group.
    Returns the new Subscription.
    """
    from datetime import timedelta
    from radius.utils import create_default_trial_plan

    now = timezone.now()
    hours = float(plan.duration_hours or 0)
    days = int(plan.duration_days or 0)
    expiry = now + timedelta(days=days, hours=hours) if (days or hours) else now + timedelta(days=36500)

    # Expire any existing active subscription
    Subscription.objects.filter(subscriber=subscriber, status='active').update(status='expired')

    sub = Subscription.objects.create(
        subscriber=subscriber,
        plan=plan,
        reseller=reseller,
        start_date=now,
        expiry_date=expiry,
        status='active',
    )
    assign_subscriber_to_plan(subscriber, plan)

    # Create payment record
    if paystack_data:
        commission_pct = reseller.get_commission_pct()
        fee_bearer = reseller.get_fee_bearer()
        amount_ngn = plan.price_ngn
        platform_share = (amount_ngn * commission_pct / 100).quantize(amount_ngn)
        reseller_share = amount_ngn - platform_share
        Payment.objects.create(
            subscriber=subscriber,
            plan=plan,
            reseller=reseller,
            amount_ngn=amount_ngn,
            paystack_reference=paystack_data.get('reference', ''),
            paystack_status='success',
            payment_method=paystack_data.get('channel', 'card'),
            commission_pct_applied=commission_pct,
            fee_bearer_applied=fee_bearer,
            platform_amount_ngn=platform_share,
            reseller_amount_ngn=reseller_share,
            gateway_fee_ngn=0,
        )
    elif plan.is_free:
        Payment.objects.create(
            subscriber=subscriber,
            plan=plan,
            reseller=reseller,
            amount_ngn=0,
            paystack_reference=f'free_{secrets.token_hex(16)}',
            paystack_status='success',
            payment_method='free',
        )

    return sub


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_set_pin(request):
    """
    Set WiFi PIN after OTP verification. Creates the subscriber account and
    activates the chosen plan (free or already-paid).

    Body:
        verify_token      — from /api/portal/verify/
        pin               — 4-digit WiFi PIN
        pin_confirm       — confirmation
        plan_id           — chosen ServicePlan id (optional; defaults to trial)
        paystack_reference — required for paid plans
    """
    verify_token = request.data.get('verify_token', '')
    pin = request.data.get('pin', '')
    pin_confirm = request.data.get('pin_confirm', '')
    plan_id = request.data.get('plan_id')
    paystack_reference = request.data.get('paystack_reference', '').strip()

    if not verify_token:
        return Response({'error': 'Verification required.'}, status=400)

    verified_data = cache.get(f'verified_{verify_token}')
    if not verified_data:
        return Response({'error': 'Verification expired. Please start over.'}, status=400)

    if not pin or len(pin) < 4 or len(pin) > 6 or not pin.isdigit():
        return Response({'error': 'PIN must be 4-6 digits.'}, status=400)

    if pin != pin_confirm:
        return Response({'error': 'PINs do not match.'}, status=400)

    try:
        reseller = Reseller.objects.get(id=verified_data['reseller_id'])
    except Reseller.DoesNotExist:
        return Response({'error': 'Network not found.'}, status=400)

    # --- Resolve plan ---
    from radius.utils import create_default_trial_plan
    from accounts.models import Country as CountryModel

    chosen_plan = None
    paystack_data = None

    if plan_id:
        try:
            chosen_plan = ServicePlan.objects.get(id=plan_id, reseller=reseller, is_active=True)
        except ServicePlan.DoesNotExist:
            return Response({'error': 'Selected plan not found.'}, status=400)

        if not chosen_plan.is_free:
            # Require a verified Paystack payment
            if not paystack_reference:
                return Response({'error': 'Payment required for this plan.'}, status=400)

            pending = cache.get(f'pending_payment_{verify_token}')
            if not pending or pending.get('reference') != paystack_reference:
                return Response({'error': 'Invalid payment reference.'}, status=400)

            paystack_data, pmt_error = _verify_paystack_payment(
                paystack_reference, pending['amount_kobo']
            )
            if not paystack_data:
                return Response({'error': pmt_error}, status=400)

    if chosen_plan is None:
        # Fall back to trial plan
        chosen_plan = ServicePlan.objects.filter(
            reseller=reseller, is_trial=True, is_active=True
        ).first()
        if not chosen_plan:
            chosen_plan = create_default_trial_plan(reseller)

    # --- Create subscriber ---
    country = None
    country_code = verified_data.get('country_code', 'NG')
    try:
        country = CountryModel.objects.get(code=country_code)
    except CountryModel.DoesNotExist:
        pass

    subscriber, created = Subscriber.objects.get_or_create(
        reseller=reseller,
        phone=verified_data['phone'],
        defaults={'email': verified_data['email'], 'verified': True, 'country': country},
    )

    if not created:
        return Response({'error': 'Account already exists. Please log in.'}, status=400)

    subscriber.set_pin(pin)
    subscriber.generate_auth_token()
    subscriber.save()

    # Activate chosen plan
    if chosen_plan:
        _create_subscription(subscriber, chosen_plan, reseller, paystack_data)

    cache.delete(f'verified_{verify_token}')
    if paystack_reference:
        cache.delete(f'pending_payment_{verify_token}')

    return Response({
        'message': 'Account created!',
        'phone': subscriber.phone,
        'auth_token': subscriber.auth_token,
        'reseller_slug': reseller.slug,
    }, status=201)


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_initiate_payment(request):
    """
    Initialize a Paystack transaction for plan purchase during signup.

    Body:
        verify_token  — from /api/portal/verify/
        plan_id       — paid ServicePlan id

    Returns:
        reference, access_code, public_key, amount_kobo, email
    """
    import uuid
    from django.conf import settings

    verify_token = request.data.get('verify_token', '')
    plan_id = request.data.get('plan_id')

    if not verify_token or not plan_id:
        return Response({'error': 'verify_token and plan_id are required.'}, status=400)

    verified_data = cache.get(f'verified_{verify_token}')
    if not verified_data:
        return Response({'error': 'Session expired. Please start over.'}, status=400)

    try:
        reseller = Reseller.objects.get(id=verified_data['reseller_id'])
    except Reseller.DoesNotExist:
        return Response({'error': 'Network not found.'}, status=400)

    try:
        plan = ServicePlan.objects.get(id=plan_id, reseller=reseller, is_active=True)
    except ServicePlan.DoesNotExist:
        return Response({'error': 'Plan not found.'}, status=404)

    if plan.is_free:
        return Response({'error': 'This plan is free — no payment needed.'}, status=400)

    if not reseller.payment_verified or not reseller.paystack_subaccount_code:
        return Response({'error': 'This network does not accept online payments yet.'}, status=400)

    secret_key = _get_paystack_secret_key()
    public_key = _get_paystack_public_key()
    if not secret_key or not public_key:
        return Response({'error': 'Payment gateway not configured.'}, status=503)

    reference = f'sw_{uuid.uuid4().hex[:20]}'
    amount_kobo = int(plan.price_ngn * 100)
    commission_pct = reseller.get_commission_pct()
    fee_bearer = reseller.get_fee_bearer()
    platform_share_kobo = int(amount_kobo * commission_pct / 100)

    payload = {
        'email': verified_data['email'],
        'amount': amount_kobo,
        'reference': reference,
        'subaccount': reseller.paystack_subaccount_code,
        'bearer': fee_bearer,
        'transaction_charge': platform_share_kobo,
        'metadata': {
            'custom_fields': [
                {'display_name': 'Phone', 'variable_name': 'phone', 'value': verified_data['phone']},
                {'display_name': 'Plan', 'variable_name': 'plan', 'value': plan.name},
                {'display_name': 'Network', 'variable_name': 'network', 'value': reseller.name},
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
        logger.error(f'Paystack init error: {exc}')
        return Response({'error': 'Payment gateway unavailable. Please try again.'}, status=502)

    if not body.get('status'):
        logger.error(f'Paystack init failed: {body}')
        return Response({'error': body.get('message', 'Could not initialize payment.')}, status=400)

    # Cache pending payment so set-pin can verify it later
    cache.set(f'pending_payment_{verify_token}', {
        'reference': reference,
        'plan_id': int(plan_id),
        'amount_kobo': amount_kobo,
    }, timeout=1800)

    return Response({
        'reference': reference,
        'access_code': body['data']['access_code'],
        'public_key': public_key,
        'amount_kobo': amount_kobo,
        'email': verified_data['email'],
        'plan_name': plan.name,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_login_api(request):
    """Login with phone + PIN → auth_token + session."""
    phone = request.data.get('phone', '')
    pin = request.data.get('pin', '')
    serial = request.data.get('serial', '')
    reseller_slug = request.data.get('reseller_slug', '')

    phone, _ = _normalize_phone(phone)
    if not phone:
        return Response({'error': 'Invalid phone number.'}, status=400)

    if not pin:
        return Response({'error': 'WiFi PIN is required.'}, status=400)

    # Find subscriber — try by serial first, then by slug, then global search
    subscriber = None
    reseller = _resolve_reseller(request)

    if reseller:
        try:
            subscriber = Subscriber.objects.get(reseller=reseller, phone=phone)
        except Subscriber.DoesNotExist:
            return Response({'error': 'Account not found. Please sign up first.'}, status=400)
    else:
        # Self-service login (/account) — search across all resellers
        subscribers = Subscriber.objects.filter(phone=phone)
        if subscribers.count() == 1:
            subscriber = subscribers.first()
        elif subscribers.count() > 1:
            return Response({'error': 'Multiple accounts found. Please use the WiFi portal to log in.'}, status=400)
        else:
            return Response({'error': 'Account not found.'}, status=400)

    if not subscriber.check_pin(pin):
        return Response({'error': 'Incorrect PIN.'}, status=400)

    # Regenerate auth token on login
    subscriber.generate_auth_token()
    subscriber.save()

    # Update RADIUS credentials
    current_sub = Subscription.objects.filter(
        subscriber=subscriber, status='active'
    ).select_related('plan').first()

    if current_sub:
        assign_subscriber_to_plan(subscriber, current_sub.plan)
    else:
        # No active subscription — check if last plan was a free trial, auto-renew it
        from datetime import timedelta
        last_sub = Subscription.objects.filter(
            subscriber=subscriber
        ).select_related('plan').order_by('-expiry_date').first()

        if last_sub and last_sub.plan.is_trial and last_sub.plan.is_free:
            plan = last_sub.plan
            now = timezone.now()
            hours = float(plan.duration_hours or 0)
            days = int(plan.duration_days or 0)
            expiry = now + timedelta(days=days, hours=hours)
            current_sub = Subscription.objects.create(
                subscriber=subscriber,
                plan=plan,
                reseller=subscriber.reseller,
                start_date=now,
                expiry_date=expiry,
                status='active',
            )
            assign_subscriber_to_plan(subscriber, plan)
            logger.info(f"Auto-renewed trial for {subscriber.phone}")
        else:
            # Expired paid plan — still write radcheck so RADIUS accepts
            update_radcheck_password(subscriber)

    return Response({
        'message': 'Login successful.',
        'auth_token': subscriber.auth_token,
        'phone': subscriber.phone,
        'reseller_slug': subscriber.reseller.slug,
        'has_active_plan': current_sub is not None,
    })


@api_view(['GET'])
@permission_classes([AllowAny])
def portal_plans(request):
    """Available plans for a reseller (by slug or serial)."""
    reseller_slug = request.GET.get('reseller', '')
    serial = request.GET.get('r', '')

    reseller = None
    if reseller_slug:
        try:
            reseller = Reseller.objects.get(slug=reseller_slug)
        except Reseller.DoesNotExist:
            return Response({'error': 'Reseller not found.'}, status=404)
    elif serial:
        from routers.models import Router
        try:
            router = Router.objects.select_related('reseller').get(serial_number=serial.upper())
            reseller = router.reseller
        except Router.DoesNotExist:
            return Response({'error': 'Network not found.'}, status=404)

    if not reseller:
        return Response({'error': 'Reseller slug or serial required.'}, status=400)

    plans = ServicePlan.objects.filter(reseller=reseller, is_active=True)

    # If reseller has no bank, only show free plans
    if not reseller.payment_verified:
        plans = plans.filter(price_ngn=0)

    plan_data = [{
        'id': p.id,
        'name': p.name,
        'speed_display': p.speed_display,
        'duration_display': p.duration_display,
        'data_cap_display': p.data_cap_display,
        'max_devices': p.max_devices,
        'price_ngn': str(p.price_ngn),
        'is_free': p.is_free,
    } for p in plans]

    return Response({'plans': plan_data})


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def portal_account(request):
    """
    Subscriber account page data.
    GET: account details, plan status, usage.
    Authenticated via auth_token in header or session.
    """
    auth_token = request.headers.get('X-Auth-Token', '') or request.GET.get('token', '')
    if not auth_token:
        return Response({'error': 'Authentication required.'}, status=401)

    try:
        subscriber = Subscriber.objects.select_related('reseller').get(auth_token=auth_token)
    except Subscriber.DoesNotExist:
        return Response({'error': 'Invalid session. Please log in again.'}, status=401)

    # Current subscription
    current_sub = Subscription.objects.filter(
        subscriber=subscriber, status='active'
    ).select_related('plan').first()

    sub_data = None
    if current_sub:
        sub_data = {
            'plan_name': current_sub.plan.name,
            'speed_display': current_sub.plan.speed_display,
            'data_cap_display': current_sub.plan.data_cap_display,
            'max_devices': current_sub.plan.max_devices,
            'start_date': current_sub.start_date.isoformat(),
            'expiry_date': current_sub.expiry_date.isoformat(),
            'days_remaining': max(0, (current_sub.expiry_date - timezone.now()).days),
            'price_ngn': str(current_sub.plan.price_ngn),
        }

    # Recent payments
    recent_payments = Payment.objects.filter(
        subscriber=subscriber
    ).order_by('-created_at')[:10]

    payments_data = [{
        'date': p.created_at.isoformat(),
        'amount': str(p.amount_ngn),
        'status': p.paystack_status,
        'method': p.payment_method,
        'plan_name': p.plan.name if p.plan else '',
    } for p in recent_payments]

    return Response({
        'phone': subscriber.phone,
        'email': subscriber.email,
        'joined': subscriber.created_at.isoformat(),
        'reseller': {
            'name': subscriber.reseller.name,
            'slug': subscriber.reseller.slug,
        },
        'subscription': sub_data,
        'payments': payments_data,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_change_pin(request):
    """Change WiFi PIN (requires current PIN)."""
    auth_token = request.headers.get('X-Auth-Token', '')
    current_pin = request.data.get('current_pin', '')
    new_pin = request.data.get('new_pin', '')
    new_pin_confirm = request.data.get('new_pin_confirm', '')

    if not auth_token:
        return Response({'error': 'Authentication required.'}, status=401)

    try:
        subscriber = Subscriber.objects.get(auth_token=auth_token)
    except Subscriber.DoesNotExist:
        return Response({'error': 'Invalid session.'}, status=401)

    if not subscriber.check_pin(current_pin):
        return Response({'error': 'Current PIN is incorrect.'}, status=400)

    if not new_pin or len(new_pin) < 4 or len(new_pin) > 6 or not new_pin.isdigit():
        return Response({'error': 'New PIN must be 4-6 digits.'}, status=400)

    if new_pin != new_pin_confirm:
        return Response({'error': 'PINs do not match.'}, status=400)

    subscriber.set_pin(new_pin)
    subscriber.generate_auth_token()
    subscriber.save()

    # Update RADIUS auth token
    update_radcheck_password(subscriber)

    return Response({
        'message': 'PIN updated successfully.',
        'auth_token': subscriber.auth_token,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_reset_pin_request(request):
    """Request PIN reset — sends OTP to phone."""
    raw_phone = request.data.get('phone', '')
    ip = _get_client_ip(request)

    phone, country = _normalize_phone(raw_phone)
    if not phone:
        return Response({'error': 'Invalid phone number.'}, status=400)

    allowed, limit_error = _check_otp_limits(phone, ip)
    if not allowed:
        return Response({'error': limit_error}, status=429)

    subscribers = Subscriber.objects.filter(phone=phone)
    if not subscribers.exists():
        return Response({'error': 'Account not found.'}, status=400)

    otp = _generate_otp()
    cache.set(f'pin_reset_{phone}', {
        'otp': otp,
        'phone': phone,
        'attempts': 0,
    }, timeout=600)

    from notifications.sms import get_sms_service
    sms = get_sms_service()
    intl = country.to_international(phone) if country else phone
    sms.send_pin_reset_otp(intl, otp)
    _record_otp_sent(phone, ip)
    logger.info(f"PIN reset OTP sent to {phone}")

    return Response({
        'message': 'Reset code sent to your phone.',
        'phone': phone,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_reset_pin_confirm(request):
    """Verify OTP and set new PIN."""
    phone = request.data.get('phone', '')
    code = request.data.get('code', '')
    new_pin = request.data.get('new_pin', '')
    new_pin_confirm = request.data.get('new_pin_confirm', '')

    phone, _ = _normalize_phone(phone)
    if not phone:
        return Response({'error': 'Invalid phone number.'}, status=400)

    cache_key = f'pin_reset_{phone}'
    reset_data = cache.get(cache_key)

    if not reset_data:
        return Response({'error': 'Reset code expired. Please request a new one.'}, status=400)

    if reset_data['attempts'] >= 5:
        cache.delete(cache_key)
        return Response({'error': 'Too many attempts.'}, status=400)

    if reset_data['otp'] != code:
        reset_data['attempts'] += 1
        cache.set(cache_key, reset_data, timeout=600)
        return Response({'error': 'Invalid code.'}, status=400)

    if not new_pin or len(new_pin) < 4 or len(new_pin) > 6 or not new_pin.isdigit():
        return Response({'error': 'PIN must be 4-6 digits.'}, status=400)

    if new_pin != new_pin_confirm:
        return Response({'error': 'PINs do not match.'}, status=400)

    # Reset PIN for all subscriber accounts with this phone
    subscribers = Subscriber.objects.filter(phone=phone)
    for sub in subscribers:
        sub.set_pin(new_pin)
        sub.generate_auth_token()
        sub.save()

    cache.delete(cache_key)

    return Response({'message': 'PIN has been reset. You can now log in.'})


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_change_plan(request):
    """Switch to a different plan. Requires auth_token."""
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
        # Free plan — activate immediately
        from datetime import timedelta
        now = timezone.now()

        if plan.duration_days > 0:
            expiry = now + timedelta(days=plan.duration_days)
        elif plan.duration_hours > 0:
            expiry = now + timedelta(hours=float(plan.duration_hours))
        else:
            expiry = now + timedelta(days=365)

        # Expire current subscription
        Subscription.objects.filter(
            subscriber=subscriber, status='active'
        ).update(status='expired')

        # Create new subscription
        sub = Subscription.objects.create(
            subscriber=subscriber,
            plan=plan,
            reseller=subscriber.reseller,
            start_date=now,
            expiry_date=expiry,
            status='active',
        )

        # Update RADIUS
        assign_subscriber_to_plan(subscriber, plan)

        # Create a free payment record
        Payment.objects.create(
            subscriber=subscriber,
            plan=plan,
            reseller=subscriber.reseller,
            amount_ngn=0,
            paystack_reference=f'free_{secrets.token_hex(16)}',
            paystack_status='success',
            payment_method='free',
        )

        return Response({
            'message': f'Plan activated: {plan.name}',
            'subscription': {
                'plan_name': plan.name,
                'expiry_date': expiry.isoformat(),
            },
        })
    else:
        # Paid plan — return payment initialization data
        return Response({
            'requires_payment': True,
            'plan': {
                'id': plan.id,
                'name': plan.name,
                'price_ngn': str(plan.price_ngn),
            },
        })


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_disconnect(request):
    """Force disconnect current session."""
    auth_token = request.headers.get('X-Auth-Token', '')

    if not auth_token:
        return Response({'error': 'Authentication required.'}, status=401)

    try:
        subscriber = Subscriber.objects.get(auth_token=auth_token)
    except Subscriber.DoesNotExist:
        return Response({'error': 'Invalid session.'}, status=401)

    # Regenerate token to invalidate current session
    subscriber.generate_auth_token()
    subscriber.save()

    # Update RADIUS
    update_radcheck_password(subscriber)

    return Response({'message': 'Session disconnected.'})
