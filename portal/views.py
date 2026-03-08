"""Portal API views — subscriber signup, OTP, login, plans, account."""
import logging
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_signup(request):
    """Register subscriber (phone + email → OTP sent)."""
    # Phase 2 implementation
    return Response({'detail': 'Portal signup — coming in Phase 2.'}, status=501)


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_verify_otp(request):
    """Verify OTP code."""
    # Phase 2 implementation
    return Response({'detail': 'OTP verification — coming in Phase 2.'}, status=501)


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_set_pin(request):
    """Set WiFi PIN after OTP verification."""
    # Phase 2 implementation
    return Response({'detail': 'Set PIN — coming in Phase 2.'}, status=501)


@api_view(['POST'])
@permission_classes([AllowAny])
def portal_login_api(request):
    """Login with phone + PIN → auth_token."""
    # Phase 2 implementation
    return Response({'detail': 'Portal login — coming in Phase 2.'}, status=501)


@api_view(['GET'])
@permission_classes([AllowAny])
def portal_plans(request):
    """Available plans for a reseller (by slug)."""
    reseller_slug = request.GET.get('reseller', '')
    if not reseller_slug:
        return Response({'error': 'Reseller slug required.'}, status=400)

    from accounts.models import Reseller
    from plans.models import ServicePlan

    try:
        reseller = Reseller.objects.get(slug=reseller_slug)
    except Reseller.DoesNotExist:
        return Response({'error': 'Reseller not found.'}, status=404)

    plans = ServicePlan.objects.filter(reseller=reseller, is_active=True)
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


@api_view(['GET'])
@permission_classes([AllowAny])
def portal_account(request):
    """Account details, plan, usage, devices."""
    # Phase 2 implementation
    return Response({'detail': 'Account details — coming in Phase 2.'}, status=501)
