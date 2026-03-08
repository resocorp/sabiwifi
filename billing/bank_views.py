"""Bank verification API — list banks, resolve account number."""
import logging
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger(__name__)


@api_view(['GET'])
def list_banks(request):
    """List Nigerian banks (cached from Paystack)."""
    # Phase 2 implementation — will call Paystack List Banks API
    return Response({'detail': 'Bank list — coming in Phase 2.'}, status=501)


@api_view(['POST'])
def resolve_account(request):
    """Resolve account number → account name via Paystack."""
    # Phase 2 implementation — will call Paystack Resolve Account API
    return Response({'detail': 'Bank resolve — coming in Phase 2.'}, status=501)
