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

    # Data caps
    data_cap_gb = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Total data cap in GB. Null = unlimited."
    )
    download_cap_gb = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Download-only cap in GB. Null = unlimited."
    )
    upload_cap_gb = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Upload-only cap in GB. Null = unlimited."
    )

    # Burst (MikroTik)
    burst_download_mbps = models.PositiveIntegerField(
        default=0, help_text="Burst download speed in Mbps (0=disabled)"
    )
    burst_upload_mbps = models.PositiveIntegerField(
        default=0, help_text="Burst upload speed in Mbps (0=disabled)"
    )
    burst_threshold_download_mbps = models.PositiveIntegerField(
        default=0, help_text="Burst threshold download (0=use base speed)"
    )
    burst_threshold_upload_mbps = models.PositiveIntegerField(
        default=0, help_text="Burst threshold upload (0=use base speed)"
    )
    burst_time_seconds = models.PositiveIntegerField(
        default=0, help_text="Burst duration in seconds"
    )
    priority = models.PositiveIntegerField(
        default=8, help_text="Queue priority 1(highest) to 8(lowest)"
    )

    # Cumulative online time limit
    online_time_limit_minutes = models.PositiveIntegerField(
        default=0, help_text="Total online minutes allowed across sessions (0=unlimited)"
    )

    # IP Pool
    ip_pool_name = models.CharField(
        max_length=64, blank=True, default='',
        help_text="MikroTik IP pool name to assign to this plan's subscribers"
    )

    # Fallback plan (throttle instead of disconnect when cap reached)
    fallback_plan = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='fallback_for',
        help_text="Plan activated when data/time limit reached (instead of disconnect)"
    )

    # Daily quotas
    daily_download_mb = models.PositiveIntegerField(
        default=0, help_text="Daily download quota in MB (0=unlimited)"
    )
    daily_upload_mb = models.PositiveIntegerField(
        default=0, help_text="Daily upload quota in MB (0=unlimited)"
    )
    daily_total_mb = models.PositiveIntegerField(
        default=0, help_text="Daily total traffic quota in MB (0=unlimited)"
    )
    daily_time_minutes = models.PositiveIntegerField(
        default=0, help_text="Daily online time quota in minutes (0=unlimited)"
    )
    daily_fallback_plan = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='daily_fallback_for',
        help_text="Plan activated when daily quota exceeded"
    )

    # Devices
    max_devices = models.PositiveIntegerField(default=1, help_text="Max simultaneous devices")

    # Price
    price_ngn = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Price in Naira. 0 = free plan."
    )

    # Flags
    # Restrict to specific routers. Empty = available on all the reseller's routers.
    routers = models.ManyToManyField(
        'routers.Router', blank=True, related_name='plans',
        help_text='Restrict this plan to specific routers. Leave empty to make it available on all of your routers.',
    )

    is_trial = models.BooleanField(default=False, help_text="Is this a trial/free plan?")
    is_system_created = models.BooleanField(
        default=False,
        help_text="Auto-created by the system (e.g. default Free Trial). Prevents duplicate auto-creation."
    )
    is_active = models.BooleanField(default=True, help_text="Whether this plan is available to subscribers")
    allow_auto_renew = models.BooleanField(default=True, help_text="Allow wallet-based auto-renewal on expiry")

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
        """
        MikroTik rate limit string.
        Simple: upload/download (e.g. '2048k/10240k')
        Burst:  rx/tx rx-burst/tx-burst rx-thresh/tx-thresh bt/bt priority
        """
        up_k = self.upload_mbps * 1024
        down_k = self.download_mbps * 1024

        if self.burst_download_mbps and self.burst_upload_mbps:
            b_up_k = self.burst_upload_mbps * 1024
            b_down_k = self.burst_download_mbps * 1024
            t_up_k = (self.burst_threshold_upload_mbps or self.upload_mbps) * 1024
            t_down_k = (self.burst_threshold_download_mbps or self.download_mbps) * 1024
            bt = self.burst_time_seconds or 10
            return (
                f'{up_k}k/{down_k}k '
                f'{b_up_k}k/{b_down_k}k '
                f'{t_up_k}k/{t_down_k}k '
                f'{bt}/{bt} '
                f'{self.priority}'
            )

        return f'{up_k}k/{down_k}k'

    @classmethod
    def available_for_router(cls, router):
        """Plans the router's reseller offers AND that include this router (or have no router restriction)."""
        return cls.objects.filter(
            reseller=router.reseller, is_active=True,
        ).filter(
            models.Q(routers__isnull=True) | models.Q(routers=router)
        ).distinct()

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
        ('limited', 'Limited'),  # On fallback plan due to cap/quota
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


class DailyUsageSnapshot(models.Model):
    """
    Daily traffic/time usage per subscriber.
    Used for daily quota enforcement and traffic reports.
    Populated by check_usage command and nightly aggregation.
    """
    subscriber = models.ForeignKey(
        'accounts.Subscriber', on_delete=models.CASCADE, related_name='daily_usage'
    )
    date = models.DateField()
    download_bytes = models.BigIntegerField(default=0)
    upload_bytes = models.BigIntegerField(default=0)
    online_seconds = models.IntegerField(default=0)
    sessions = models.IntegerField(default=0)
    quota_exceeded = models.BooleanField(default=False)

    class Meta:
        unique_together = ['subscriber', 'date']
        ordering = ['-date']
        indexes = [
            models.Index(fields=['date']),
        ]

    def __str__(self):
        total_mb = (self.download_bytes + self.upload_bytes) / (1024 * 1024)
        return f'{self.subscriber.phone} {self.date} — {total_mb:.1f} MB'
