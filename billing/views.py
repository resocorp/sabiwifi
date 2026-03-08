"""Billing API views — Paystack payment initialization, webhooks, callbacks."""
import logging
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger(__name__)


@api_view(['POST'])
def payment_initialize(request):
    """Create a Paystack payment with split to reseller subaccount."""
    # Phase 2 implementation
    return Response({'detail': 'Payment initialization — coming in Phase 2.'}, status=501)


@api_view(['POST'])
@permission_classes([AllowAny])
def payment_webhook(request):
    """Paystack webhook handler (charge.success). Signature verified."""
    # Phase 2 implementation
    return Response({'status': 'ok'})


@api_view(['GET'])
@permission_classes([AllowAny])
def payment_callback(request):
    """Paystack redirect after payment completion."""
    # Phase 2 implementation
    return Response({'detail': 'Payment callback — coming in Phase 2.'}, status=501)


@api_view(['GET'])
def current_subscription(request):
    """Get current subscription details for the authenticated subscriber."""
    # Phase 2 implementation
    return Response({'detail': 'Subscription endpoint — coming in Phase 2.'}, status=501)
