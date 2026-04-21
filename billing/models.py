from django.db import models
from simple_history.models import HistoricalRecords


class Payment(models.Model):
    """
    A payment record for a subscriber purchasing a plan or paying a signup fee.
    Snapshots commission/fee config at time of transaction for audit trail.
    """
    PAYMENT_TYPE_CHOICES = [
        ('plan', 'Plan Purchase'),
        ('signup_fee', 'Account Creation Fee'),
        ('wallet_topup', 'Wallet Top-up'),
        ('auto_renewal', 'Auto Renewal'),
        ('lead_install', 'Lead install payment'),
    ]

    PAYMENT_METHOD_CHOICES = [
        ('card', 'Card'),
        ('bank_transfer', 'Bank Transfer'),
        ('ussd', 'USSD'),
        ('free', 'Free Plan'),
        ('wallet', 'Wallet Balance'),
        ('refill_card', 'Refill Card'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]

    # Nullable — a lead-install payment is created before a Subscriber exists;
    # the webhook handler converts the lead and fills this in at activation.
    subscriber = models.ForeignKey(
        'accounts.Subscriber', null=True, blank=True,
        on_delete=models.CASCADE, related_name='payments',
    )
    plan = models.ForeignKey(
        'plans.ServicePlan', on_delete=models.SET_NULL, null=True, blank=True, related_name='payments'
    )
    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='payments'
    )
    # Set when this payment is an installation / first-month charge for a Lead
    # that has not yet been converted to a Subscriber.
    lead = models.ForeignKey(
        'leads.Lead', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='payments',
    )
    installation_order = models.ForeignKey(
        'leads.InstallationOrder', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='payments',
    )

    payment_type = models.CharField(
        max_length=20, choices=PAYMENT_TYPE_CHOICES, default='plan'
    )

    # Amount
    amount_ngn = models.DecimalField(max_digits=10, decimal_places=2)

    # Paystack fields
    paystack_reference = models.CharField(max_length=200, unique=True, blank=True, default='')
    paystack_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default='card')

    # Split details (snapshot at time of payment)
    commission_pct_applied = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Commission % applied at time of this transaction"
    )
    fee_bearer_applied = models.CharField(max_length=20, default='subaccount')
    platform_amount_ngn = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Platform's share of this payment"
    )
    reseller_amount_ngn = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Partner's share of this payment"
    )
    gateway_fee_ngn = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Paystack transaction fee"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        who = (self.subscriber and self.subscriber.phone) \
            or (self.lead and self.lead.phone) or '—'
        return f'₦{self.amount_ngn} - {who} ({self.paystack_status})'


class Wallet(models.Model):
    """Prepaid balance wallet for a subscriber."""
    subscriber = models.OneToOneField(
        'accounts.Subscriber', on_delete=models.CASCADE, related_name='wallet'
    )
    balance_ngn = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.subscriber.phone} — ₦{self.balance_ngn}'


class WalletTransaction(models.Model):
    """Audit trail for every wallet balance mutation."""
    TRANSACTION_TYPE_CHOICES = [
        ('refill_card', 'Refill Card'),
        ('paystack_topup', 'Paystack Top-up'),
        ('plan_purchase', 'Plan Purchase'),
        ('plan_renewal', 'Plan Auto-Renewal'),
        ('reseller_credit', 'Reseller Credit'),
        ('reseller_debit', 'Reseller Debit'),
        ('refund', 'Refund'),
    ]

    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='transactions')
    amount_ngn = models.DecimalField(max_digits=10, decimal_places=2)  # positive=credit, negative=debit
    balance_after = models.DecimalField(max_digits=10, decimal_places=2)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPE_CHOICES)
    reference = models.CharField(max_length=200, blank=True, default='')
    description = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=100, default='system')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        sign = '+' if self.amount_ngn >= 0 else ''
        return f'{sign}₦{self.amount_ngn} ({self.transaction_type})'
