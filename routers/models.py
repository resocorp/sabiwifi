from django.db import models
from django.contrib.auth.hashers import make_password
from simple_history.models import HistoricalRecords


class Router(models.Model):
    """
    A MikroTik router managed by the platform.
    Reseller is nullable — 'available' routers have no reseller yet.
    """
    STATUS_CHOICES = [
        ('available', 'Available'),
        ('pending_provision', 'Pending Provision'),
        ('provisioned', 'Provisioned'),
        ('online', 'Online'),
        ('offline', 'Offline'),
        ('failed', 'Failed'),
    ]

    serial_number = models.CharField(max_length=50, unique=True)
    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='routers'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='available')
    location_name = models.CharField(max_length=200, blank=True, default='')

    # WireGuard
    wg_public_key = models.CharField(max_length=255, blank=True, default='')
    wg_private_key = models.CharField(max_length=255, blank=True, default='')
    wg_tunnel_ip = models.GenericIPAddressField(null=True, blank=True)

    # RADIUS / CoA
    nas_secret = models.CharField(max_length=255, blank=True, default='')

    # RouterOS API
    api_username = models.CharField(max_length=100, blank=True, default='sabiwifi')
    api_password = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Encrypted at rest"
    )

    # Health
    last_seen = models.DateTimeField(null=True, blank=True)
    offline_since = models.DateTimeField(null=True, blank=True,
        help_text="When this router last went offline. Cleared when it comes back online.")
    provision_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        label = self.location_name or self.serial_number
        if self.reseller:
            return f'{label} ({self.reseller.name})'
        return f'{label} (unassigned)'

    @property
    def is_online(self):
        return self.status == 'online'

    @property
    def is_assigned(self):
        return self.reseller is not None

    @property
    def offline_duration_display(self):
        """Human-readable string of how long the router has been offline."""
        if not self.offline_since:
            return ''
        from django.utils import timezone
        delta = timezone.now() - self.offline_since
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return 'just now'
        if total_seconds < 3600:
            m = total_seconds // 60
            return f'{m} min'
        if total_seconds < 86400:
            h = total_seconds // 3600
            m = (total_seconds % 3600) // 60
            return f'{h}h {m}m' if m else f'{h}h'
        d = total_seconds // 86400
        h = (total_seconds % 86400) // 3600
        return f'{d}d {h}h' if h else f'{d}d'


class RouterHealthLog(models.Model):
    """
    Records each online/offline status transition for a router.
    Used for uptime history, trend analysis, and reseller alerts.
    """
    EVENT_CHOICES = [
        ('online', 'Came Online'),
        ('offline', 'Went Offline'),
    ]

    router = models.ForeignKey(
        Router, on_delete=models.CASCADE, related_name='health_logs'
    )
    event = models.CharField(max_length=10, choices=EVENT_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['router', '-created_at']),
        ]

    def __str__(self):
        return f'{self.router.serial_number} {self.event} @ {self.created_at:%Y-%m-%d %H:%M}'
