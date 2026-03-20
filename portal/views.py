"""Portal API views — subscriber signup, OTP, login, plans, account, PIN management."""
import secrets
import logging
import re
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
from radius.utils import assign_subscriber_to_plan

logger = logging.getLogger(__name__)


class OTPRateThrottle(AnonRateThrottle):
    rate = '3/hour'

    def get_cache_key(self, request, view):
        phone = request.data.get('phone', '')
        if phone:
            return f'otp_throttle_{phone}'
        return self.get_ident(request)


def _normalize_phone(phone):
    """Normalize Nigerian phone number to +234XXXXXXXXXX format."""
    phone = re.sub(r'\s+', '', phone)
    if phone.startswith('0'):
        phone = '+234' + phone[1:]
    elif phone.startswith('234'):
        phone = '+' + phone
    elif not phone.startswith('+234'):
        return None
    if not re.match(r'^\+234\d{10}$', phone):
        return None
    return phone


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
    Register subscriber: phone + email → OTP sent.
    Requires reseller context (serial or reseller_slug).
    """
    phone = request.data.get('phone', '')
    email = request.data.get('email', '').strip().lower()

    phone = _normalize_phone(phone)
    if not phone:
        return Response({'error': 'Invalid Nigerian phone number.'}, status=400)

    if not email:
        return Response({'error': 'Email is required for payment receipts.'}, status=400)

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
        'attempts': 0,
    }, timeout=600)  # 10 minutes

    # Send OTP via Termii SMS
    from notifications.sms import get_sms_service
    sms = get_sms_service()
    sms.send_otp(phone, otp)
    logger.info(f"OTP sent to {phone}")

    return Response({
        'message': 'Verification code sent to your phone.',
        'phone': phone,
        'reseller_id': reseller.id,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_verify_otp(request):
    """Verify the 6-digit OTP code."""
    phone = request.data.get('phone', '')
    code = request.data.get('code', '').strip()
    reseller_id = request.data.get('reseller_id', '')

    phone = _normalize_phone(phone)
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


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_set_pin(request):
    """Set WiFi PIN after OTP verification. Creates the subscriber account."""
    verify_token = request.data.get('verify_token', '')
    pin = request.data.get('pin', '')
    pin_confirm = request.data.get('pin_confirm', '')

    if not verify_token:
        return Response({'error': 'Verification required.'}, status=400)

    verified_data = cache.get(f'verified_{verify_token}')
    if not verified_data:
        return Response({'error': 'Verification expired. Please start over.'}, status=400)

    if not pin or len(pin) < 4 or len(pin) > 6:
        return Response({'error': 'PIN must be 4-6 digits.'}, status=400)

    if not pin.isdigit():
        return Response({'error': 'PIN must contain only numbers.'}, status=400)

    if pin != pin_confirm:
        return Response({'error': 'PINs do not match.'}, status=400)

    try:
        reseller = Reseller.objects.get(id=verified_data['reseller_id'])
    except Reseller.DoesNotExist:
        return Response({'error': 'Network not found.'}, status=400)

    # Create subscriber
    subscriber, created = Subscriber.objects.get_or_create(
        reseller=reseller,
        phone=verified_data['phone'],
        defaults={'email': verified_data['email'], 'verified': True}
    )

    if not created:
        return Response({'error': 'Account already exists. Please log in.'}, status=400)

    subscriber.set_pin(pin)
    subscriber.generate_auth_token()
    subscriber.save()

    # Auto-assign trial plan and create RADIUS credentials
    from datetime import timedelta
    from radius.utils import assign_subscriber_to_plan, create_default_trial_plan
    trial_plan = ServicePlan.objects.filter(
        reseller=reseller, is_trial=True, is_active=True
    ).first()
    if not trial_plan:
        trial_plan = create_default_trial_plan(reseller)

    if trial_plan:
        now = timezone.now()
        hours = float(trial_plan.duration_hours or 0)
        days = int(trial_plan.duration_days or 0)
        expiry = now + timedelta(days=days, hours=hours)
        Subscription.objects.create(
            subscriber=subscriber,
            plan=trial_plan,
            reseller=reseller,
            start_date=now,
            expiry_date=expiry,
            status='active',
        )
        assign_subscriber_to_plan(subscriber, trial_plan)

    cache.delete(f'verified_{verify_token}')

    return Response({
        'message': 'Account created!',
        'phone': subscriber.phone,
        'auth_token': subscriber.auth_token,
        'reseller_slug': reseller.slug,
    }, status=201)


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_login_api(request):
    """Login with phone + PIN → auth_token + session."""
    phone = request.data.get('phone', '')
    pin = request.data.get('pin', '')
    serial = request.data.get('serial', '')
    reseller_slug = request.data.get('reseller_slug', '')

    phone = _normalize_phone(phone)
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
    from radius.models import Radcheck
    Radcheck.objects.filter(username=subscriber.phone, attribute='Cleartext-Password').delete()
    Radcheck.objects.create(
        username=subscriber.phone,
        attribute='Cleartext-Password',
        op=':=',
        value=subscriber.auth_token,
    )

    return Response({
        'message': 'PIN updated successfully.',
        'auth_token': subscriber.auth_token,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_reset_pin_request(request):
    """Request PIN reset — sends OTP to phone."""
    phone = request.data.get('phone', '')
    phone = _normalize_phone(phone)
    if not phone:
        return Response({'error': 'Invalid phone number.'}, status=400)

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
    sms.send_pin_reset_otp(phone, otp)
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

    phone = _normalize_phone(phone)
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
    from radius.models import Radcheck
    Radcheck.objects.filter(username=subscriber.phone, attribute='Cleartext-Password').update(
        value=subscriber.auth_token
    )

    return Response({'message': 'Session disconnected.'})
