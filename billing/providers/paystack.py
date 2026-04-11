"""Paystack payment provider implementation."""
import hashlib
import hmac
import logging
import requests
from django.conf import settings
from billing.providers.base import PaymentProvider

logger = logging.getLogger(__name__)

PAYSTACK_BASE_URL = 'https://api.paystack.co'


def get_paystack_keys():
    """Return (secret_key, public_key), falling back to PlatformSettings."""
    secret = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
    public = getattr(settings, 'PAYSTACK_PUBLIC_KEY', '')
    if not secret:
        from operator_panel.models import PlatformSettings
        ps = PlatformSettings.load()
        secret = ps.paystack_secret_key
        public = ps.paystack_public_key
    return secret, public


class PaystackProvider(PaymentProvider):
    """Paystack integration with automatic split payments."""

    def __init__(self):
        self.secret_key = settings.PAYSTACK_SECRET_KEY
        self.public_key = settings.PAYSTACK_PUBLIC_KEY
        self.headers = {
            'Authorization': f'Bearer {self.secret_key}',
            'Content-Type': 'application/json',
        }

    def initialize_payment(self, amount, email, subaccount_code, reference, metadata=None):
        """Initialize a Paystack transaction with split."""
        payload = {
            'amount': int(amount * 100),  # Paystack uses kobo
            'email': email,
            'reference': reference,
            'subaccount': subaccount_code,
            'callback_url': f'https://{settings.PLATFORM_DOMAIN}/api/billing/callback/',
        }
        if metadata:
            payload['metadata'] = metadata

        response = requests.post(
            f'{PAYSTACK_BASE_URL}/transaction/initialize',
            json=payload,
            headers=self.headers,
            timeout=30,
        )
        data = response.json()
        if data.get('status'):
            return data['data']
        logger.error(f"Paystack init failed: {data}")
        return None

    def verify_payment(self, reference):
        """Verify payment status by reference."""
        response = requests.get(
            f'{PAYSTACK_BASE_URL}/transaction/verify/{reference}',
            headers=self.headers,
            timeout=30,
        )
        data = response.json()
        if data.get('status'):
            return data['data']
        return None

    def create_subaccount(self, business_name, bank_code, account_number, percentage_charge):
        """Create a Paystack subaccount for a reseller."""
        payload = {
            'business_name': business_name,
            'bank_code': bank_code,
            'account_number': account_number,
            'percentage_charge': float(percentage_charge),
        }
        response = requests.post(
            f'{PAYSTACK_BASE_URL}/subaccount',
            json=payload,
            headers=self.headers,
            timeout=30,
        )
        data = response.json()
        if data.get('status'):
            return data['data']
        logger.error(f"Paystack subaccount creation failed: {data}")
        return None

    def update_subaccount(self, subaccount_code, bank_code, account_number):
        """Update subaccount bank details."""
        payload = {
            'bank_code': bank_code,
            'account_number': account_number,
        }
        response = requests.put(
            f'{PAYSTACK_BASE_URL}/subaccount/{subaccount_code}',
            json=payload,
            headers=self.headers,
            timeout=30,
        )
        data = response.json()
        if data.get('status'):
            return data['data']
        logger.error(f"Paystack subaccount update failed: {data}")
        return None

    def list_banks(self):
        """List Nigerian banks."""
        response = requests.get(
            f'{PAYSTACK_BASE_URL}/bank?country=nigeria',
            headers=self.headers,
            timeout=30,
        )
        data = response.json()
        if data.get('status'):
            return data['data']
        return []

    def resolve_account(self, account_number, bank_code):
        """Resolve account number to account name."""
        response = requests.get(
            f'{PAYSTACK_BASE_URL}/bank/resolve',
            params={'account_number': account_number, 'bank_code': bank_code},
            headers=self.headers,
            timeout=30,
        )
        data = response.json()
        if data.get('status'):
            return data['data']
        return None

    def verify_webhook_signature(self, payload, signature):
        """Verify Paystack webhook HMAC SHA-512 signature."""
        computed = hmac.new(
            self.secret_key.encode('utf-8'),
            payload,
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(computed, signature)
