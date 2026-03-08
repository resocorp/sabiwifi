from django.db import models
from simple_history.models import HistoricalRecords


class Payment(models.Model):
    """
    A payment record for a subscriber purchasing a plan.
    Snapshots commission/fee config at time of transaction for audit trail.
    """
    PAYMENT_METHOD_CHOICES = [
        ('card', 'Card'),
        ('bank_transfer', 'Bank Transfer'),
        ('ussd', 'USSD'),
        ('free', 'Free Plan'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]

    subscriber = models.ForeignKey(
        'accounts.Subscriber', on_delete=models.CASCADE, related_name='payments'
    )
    plan = models.ForeignKey(
        'plans.ServicePlan', on_delete=models.SET_NULL, null=True, related_name='payments'
    )
    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='payments'
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
        help_text="Reseller's share of this payment"
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
        return f'₦{self.amount_ngn} - {self.subscriber.phone} ({self.paystack_status})'
