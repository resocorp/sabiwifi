"""Bank verification API — list banks, resolve account, setup subaccount."""
import logging
from django.core.cache import cache
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger(__name__)


@api_view(['GET'])
def list_banks(request):
    """List Nigerian banks (cached from Paystack for 24h)."""
    cached = cache.get('paystack_banks')
    if cached:
        return Response({'banks': cached})

    from billing.providers.paystack import PaystackProvider
    provider = PaystackProvider()
    banks = provider.list_banks()

    if banks:
        # Cache for 24 hours
        bank_list = [{'name': b['name'], 'code': b['code']} for b in banks]
        cache.set('paystack_banks', bank_list, timeout=86400)
        return Response({'banks': bank_list})

    return Response({'error': 'Could not load bank list.'}, status=502)


@api_view(['POST'])
def resolve_account(request):
    """Resolve account number → account name via Paystack."""
    account_number = request.data.get('account_number', '').strip()
    bank_code = request.data.get('bank_code', '').strip()

    if not account_number or len(account_number) != 10:
        return Response({'error': 'Please enter a valid 10-digit account number.'}, status=400)

    if not bank_code:
        return Response({'error': 'Please select a bank.'}, status=400)

    from billing.providers.paystack import PaystackProvider
    provider = PaystackProvider()
    result = provider.resolve_account(account_number, bank_code)

    if not result:
        return Response({'error': 'Could not verify this account. Please check the details.'}, status=400)

    return Response({
        'account_name': result.get('account_name', ''),
        'account_number': result.get('account_number', account_number),
    })


@api_view(['POST'])
def bank_setup(request):
    """
    Save verified bank details and create Paystack subaccount.
    Called after reseller confirms resolved account name.
    """
    reseller = request.user.reseller
    account_number = request.data.get('account_number', '').strip()
    bank_code = request.data.get('bank_code', '').strip()
    bank_name = request.data.get('bank_name', '').strip()
    account_name = request.data.get('account_name', '').strip()

    if not all([account_number, bank_code, bank_name, account_name]):
        return Response({'error': 'All bank details are required.'}, status=400)

    from billing.providers.paystack import PaystackProvider
    provider = PaystackProvider()

    # Create Paystack subaccount
    commission_pct = reseller.get_commission_pct()
    sub_data = provider.create_subaccount(
        business_name=reseller.name,
        bank_code=bank_code,
        account_number=account_number,
        percentage_charge=float(commission_pct),
    )

    if not sub_data:
        return Response({'error': 'Could not create payment account. Please try again.'}, status=502)

    # Save to reseller
    reseller.bank_code = bank_code
    reseller.bank_name = bank_name
    reseller.account_number = account_number
    reseller.account_name = account_name
    reseller.paystack_subaccount_code = sub_data.get('subaccount_code', '')
    reseller.payment_verified = True
    reseller.save()

    logger.info(f"Bank verified for reseller {reseller.name}: {bank_name} {account_number}")

    return Response({
        'message': 'Bank account verified! You can now create paid plans.',
        'bank_name': bank_name,
        'account_name': account_name,
        'account_number': account_number,
    })


@api_view(['PUT'])
def bank_change(request):
    """Change bank details — re-resolve + update Paystack subaccount."""
    reseller = request.user.reseller
    account_number = request.data.get('account_number', '').strip()
    bank_code = request.data.get('bank_code', '').strip()
    bank_name = request.data.get('bank_name', '').strip()
    account_name = request.data.get('account_name', '').strip()

    if not all([account_number, bank_code, bank_name, account_name]):
        return Response({'error': 'All bank details are required.'}, status=400)

    if not reseller.paystack_subaccount_code:
        return Response({'error': 'No existing bank account to update.'}, status=400)

    from billing.providers.paystack import PaystackProvider
    provider = PaystackProvider()

    result = provider.update_subaccount(
        reseller.paystack_subaccount_code,
        bank_code,
        account_number,
    )

    if not result:
        return Response({'error': 'Could not update bank details.'}, status=502)

    reseller.bank_code = bank_code
    reseller.bank_name = bank_name
    reseller.account_number = account_number
    reseller.account_name = account_name
    reseller.save()

    logger.info(f"Bank changed for reseller {reseller.name}: {bank_name} {account_number}")

    return Response({
        'message': 'Bank account updated.',
        'bank_name': bank_name,
        'account_name': account_name,
    })
