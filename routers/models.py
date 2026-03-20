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
