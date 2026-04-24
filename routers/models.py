from django.db import models
from django.contrib.auth.hashers import make_password
from simple_history.models import HistoricalRecords


class Router(models.Model):
    """
    A router managed by the platform.
    Supports MikroTik (RouterOS) and OpenWrt devices.
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

    DEVICE_TYPE_CHOICES = [
        ('mikrotik', 'MikroTik'),
        ('openwrt', 'OpenWrt'),
    ]

    SERVICE_MODE_CHOICES = [
        ('hotspot', 'Hotspot Only'),
        ('pppoe', 'PPPoE Only'),
        ('both', 'Hotspot + PPPoE'),
    ]

    serial_number = models.CharField(max_length=50, unique=True)
    device_type = models.CharField(
        max_length=10, choices=DEVICE_TYPE_CHOICES, default='mikrotik',
        help_text='MikroTik uses serial number, OpenWrt uses MAC address as identifier.'
    )
    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='routers'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='available')
    service_mode = models.CharField(
        max_length=10, choices=SERVICE_MODE_CHOICES, default='hotspot',
        help_text='Hotspot (captive portal), PPPoE, or both.'
    )
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

    # WiFi GUI state (applied at next reprovision)
    wifi_enabled = models.BooleanField(default=True,
        help_text="If False, WiFi radios are disabled at provisioning.")
    wifi_ssid = models.CharField(max_length=32, blank=True, default='',
        help_text="Custom SSID. Empty = use reseller branding fallback.")
    wifi_password = models.CharField(max_length=63, blank=True, default='',
        help_text="WPA2/WPA3 password. Empty = open network.")

    # Capabilities reported by heartbeat (None = not yet detected)
    board_name = models.CharField(max_length=64, blank=True, default='',
        help_text="RouterBOARD model name from /system resource (e.g. 'hAP ac²').")
    has_wifi = models.BooleanField(null=True, blank=True,
        help_text="True if /interface wifi exists on this device.")
    has_5ghz = models.BooleanField(null=True, blank=True,
        help_text="True if device has a 5GHz radio.")
    ether_port_count = models.PositiveSmallIntegerField(null=True, blank=True,
        help_text="Number of ethernet ports detected.")
    ros_version = models.CharField(max_length=32, blank=True, default='',
        help_text="RouterOS version reported by /system resource.")
    detected_wan = models.CharField(max_length=16, blank=True, default='',
        help_text="Which ether port currently holds the WAN DHCP lease.")

    # Health
    last_seen = models.DateTimeField(null=True, blank=True,
        help_text="Last heartbeat received (internet-alive signal).")
    wg_last_handshake = models.DateTimeField(null=True, blank=True,
        help_text="Last successful WireGuard handshake (tunnel-alive signal).")
    offline_since = models.DateTimeField(null=True, blank=True,
        help_text="When this router last went offline. Cleared when it comes back online.")
    needs_reprovision = models.BooleanField(default=False,
        help_text="When True, the next MikroTik heartbeat returns the provision script.")
    last_reprovision_at = models.DateTimeField(null=True, blank=True,
        help_text="Timestamp of last successful provision delivery.")
    provision_count = models.PositiveIntegerField(default=0)
    offline_strikes = models.PositiveSmallIntegerField(default=0,
        help_text="Consecutive unhealthy observations. Router only flips to 'offline' "
                  "after this reaches the strike threshold (hysteresis).")

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

    @classmethod
    def router_name_map(cls, reseller, nas_ips):
        """Resolve a set of RADIUS `nasipaddress` values to router display
        names, scoped to one reseller.

        Used by the dashboard reports (session, traffic) to label rows by
        router instead of by tunnel IP. Returns a dict keyed on the string
        IP that appears in `radacct.nasipaddress`.

        Orphan NAS IPs (router deleted but radacct rows linger) map to
        'Unknown router' so the report never renders an empty cell.

        When two routers on the same reseller share a `location_name`
        (allowed — resellers legitimately run "Hall A" on two floors), the
        helper appends the last 4 chars of each router's serial so the
        reseller can still tell them apart in the report.
        """
        if not nas_ips:
            return {}
        rows = list(cls.objects.filter(
            reseller=reseller, wg_tunnel_ip__in=nas_ips,
        ).values('wg_tunnel_ip', 'location_name', 'serial_number'))

        # Count name collisions across the lookup set so we only suffix when
        # a name is genuinely ambiguous. A router with no `location_name`
        # always shows its serial, so those are never ambiguous.
        name_counts = {}
        for r in rows:
            label = r['location_name'] or r['serial_number']
            name_counts[label] = name_counts.get(label, 0) + 1

        out = {}
        for r in rows:
            ip = r['wg_tunnel_ip']
            name = r['location_name']
            serial = r['serial_number'] or ''
            if not name:
                out[ip] = serial or 'Unknown router'
                continue
            if name_counts.get(name, 0) > 1 and serial:
                out[ip] = f'{name} · {serial[-4:]}'
            else:
                out[ip] = name

        # Any NAS IP the caller asked about that didn't resolve is an orphan.
        for ip in nas_ips:
            if ip and ip not in out:
                out[ip] = 'Unknown router'
        return out

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
    notified_at = models.DateTimeField(null=True, blank=True,
        help_text="When this event was dispatched via SMS/WA (or suppressed as a blip). "
                  "NULL means pending evaluation by the next check_routers run.")

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['router', '-created_at']),
            models.Index(fields=['notified_at']),
        ]

    def __str__(self):
        return f'{self.router.serial_number} {self.event} @ {self.created_at:%Y-%m-%d %H:%M}'
