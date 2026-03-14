from django.db import models
from simple_history.models import HistoricalRecords


class ServicePlan(models.Model):
    """
    A WiFi service plan created by a reseller.
    Maps to a FreeRADIUS group with reply/check attributes.
    """
    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='plans'
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200)

    # Speed (Mbps)
    download_mbps = models.PositiveIntegerField(help_text="Download speed in Mbps")
    upload_mbps = models.PositiveIntegerField(help_text="Upload speed in Mbps")

    # Duration
    duration_days = models.PositiveIntegerField(default=0, help_text="Duration in days (0 if using hours)")
    duration_hours = models.DecimalField(
        max_digits=6, decimal_places=2, default=0,
        help_text="Duration in hours (e.g. 0.50 for 30 min). Used when duration_days=0."
    )

    # Data cap
    data_cap_gb = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Data cap in GB. Null = unlimited."
    )

    # Devices
    max_devices = models.PositiveIntegerField(default=1, help_text="Max simultaneous devices")

    # Price
    price_ngn = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Price in Naira. 0 = free plan."
    )

    # Flags
    is_trial = models.BooleanField(default=False, help_text="Is this a trial/free plan?")
    is_system_created = models.BooleanField(
        default=False,
        help_text="Auto-created by the system (e.g. default Free Trial). Prevents duplicate auto-creation."
    )
    is_active = models.BooleanField(default=True, help_text="Whether this plan is available to subscribers")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        unique_together = ['reseller', 'slug']
        ordering = ['price_ngn', 'name']

    def __str__(self):
        return f'{self.name} - {self.reseller.name}'

    @property
    def radius_group_name(self):
        """RADIUS group name: reseller_slug-plan_slug (globally unique)."""
        return f'{self.reseller.slug}-{self.slug}'

    @property
    def is_free(self):
        return self.price_ngn == 0

    @property
    def duration_display(self):
        """Human-readable duration string."""
        if self.duration_days > 0:
            return f'{self.duration_days} day{"s" if self.duration_days != 1 else ""}'
        if self.duration_hours > 0:
            hours = float(self.duration_hours)
            if hours < 1:
                minutes = int(hours * 60)
                return f'{minutes} min'
            return f'{hours:.0f} hour{"s" if hours != 1 else ""}'
        return 'Unlimited'

    @property
    def data_cap_display(self):
        """Human-readable data cap string."""
        if self.data_cap_gb is None:
            return 'Unlimited'
        gb = float(self.data_cap_gb)
        if gb < 1:
            mb = round(gb * 1000)
            return f'{mb} MB'
        return f'{gb:.0f} GB'

    @property
    def speed_display(self):
        """Human-readable speed string."""
        return f'{self.download_mbps} Mbps ↓ / {self.upload_mbps} Mbps ↑'

    @property
    def mikrotik_rate_limit(self):
        """MikroTik rate limit string: upload/download format."""
        up_k = self.upload_mbps * 1024
        down_k = self.download_mbps * 1024
        return f'{up_k}k/{down_k}k'

    @property
    def session_timeout_seconds(self):
        """Session timeout in seconds for RADIUS."""
        if self.duration_days > 0:
            return self.duration_days * 86400
        if self.duration_hours > 0:
            return int(float(self.duration_hours) * 3600)
        return 0


class Subscription(models.Model):
    """
    An active or historical subscription linking a subscriber to a plan.
    """
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
    ]

    subscriber = models.ForeignKey(
        'accounts.Subscriber', on_delete=models.CASCADE, related_name='subscriptions'
    )
    plan = models.ForeignKey(ServicePlan, on_delete=models.CASCADE, related_name='subscriptions')
    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='subscriptions'
    )
    start_date = models.DateTimeField()
    expiry_date = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return f'{self.subscriber.phone} → {self.plan.name} ({self.status})'
