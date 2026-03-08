"""Abstract base class for payment providers."""
from abc import ABC, abstractmethod


class PaymentProvider(ABC):
    """Interface that all payment gateway integrations must implement."""

    @abstractmethod
    def initialize_payment(self, amount, email, subaccount_code, reference, metadata=None):
        """Initialize a payment transaction. Returns authorization URL."""
        pass

    @abstractmethod
    def verify_payment(self, reference):
        """Verify a payment by reference. Returns payment details."""
        pass

    @abstractmethod
    def create_subaccount(self, business_name, bank_code, account_number, percentage_charge):
        """Create a split subaccount. Returns subaccount code."""
        pass

    @abstractmethod
    def update_subaccount(self, subaccount_code, bank_code, account_number):
        """Update a subaccount's bank details."""
        pass

    @abstractmethod
    def list_banks(self):
        """List available banks. Returns list of bank dicts."""
        pass

    @abstractmethod
    def resolve_account(self, account_number, bank_code):
        """Resolve account number to account name. Returns account name."""
        pass

    @abstractmethod
    def verify_webhook_signature(self, payload, signature):
        """Verify webhook signature. Returns True if valid."""
        pass
