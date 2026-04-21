"""
Notification models:
  - WhatsappSession              per-reseller Baileys session state
  - ResellerNotificationConfig   what to send and to whom
  - ResellerAdminContact         extra ops contacts
  - NotificationTemplate         editable message templates per reseller
  - SubscriberNotificationPrefs  subscriber opt-in preferences
  - Broadcast                    bulk campaign
  - NotificationLog              every message attempted (unified audit log)
"""
from django.db import models


class WhatsappSession(models.Model):
    STATUS_CHOICES = [
        ('disconnected', 'Disconnected'),
        ('connecting', 'Connecting'),
        ('connected', 'Connected'),
    ]
    reseller = models.OneToOneField(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='whatsapp_session'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='disconnected')
    wa_phone = models.CharField(max_length=30, blank=True, default='')
    connected_at = models.DateTimeField(null=True, blank=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.reseller.slug} WA ({self.status})'


class ResellerNotificationConfig(models.Model):
    CHANNEL_CHOICES = [
        ('whatsapp', 'WhatsApp only'),
        ('sms', 'SMS only'),
        ('both', 'WhatsApp + SMS'),
    ]
    reseller = models.OneToOneField(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='notification_config'
    )
    # Automated messages sent to subscribers
    send_plan_expiry_3d = models.BooleanField(default=True)
    send_plan_expiry_1d = models.BooleanField(default=True)
    send_plan_expired = models.BooleanField(default=True)
    send_welcome = models.BooleanField(default=True)
    subscriber_channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default='sms')
    # Operational alerts the reseller receives
    recv_new_subscriber = models.BooleanField(default=True)
    recv_payment = models.BooleanField(default=True)
    recv_router_offline = models.BooleanField(default=True)
    recv_router_recovered = models.BooleanField(default=False)
    reseller_channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default='sms')

    def __str__(self):
        return f'{self.reseller.slug} notification config'


class ResellerAdminContact(models.Model):
    CHANNEL_CHOICES = [
        ('whatsapp', 'WhatsApp only'),
        ('sms', 'SMS only'),
        ('both', 'WhatsApp + SMS'),
    ]
    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='admin_contacts'
    )
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20)
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default='sms')
    recv_router_alerts = models.BooleanField(default=True)
    recv_new_subscriber = models.BooleanField(default=False)
    recv_payment_summary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.phone}) — {self.reseller.slug}'


class NotificationTemplate(models.Model):
    EVENT_CHOICES = [
        ('plan_expiry_3d', 'Plan expiring in 3 days'),
        ('plan_expiry_1d', 'Plan expiring tomorrow'),
        ('plan_expired', 'Plan expired'),
        ('welcome', 'Welcome message'),
        ('router_offline', 'Router went offline'),
        ('router_recovered', 'Router back online'),
        ('new_subscriber', 'New subscriber alert (to partner)'),
        ('payment_received', 'Payment received (to partner)'),
    ]
    EVENT_VARS = {
        'plan_expiry_3d':   ['name', 'plan', 'expiry_date', 'link'],
        'plan_expiry_1d':   ['name', 'plan', 'expiry_date', 'link'],
        'plan_expired':     ['name', 'plan', 'link'],
        'welcome':          ['name', 'plan', 'link'],
        'router_offline':   ['location', 'serial', 'offline_duration'],
        'router_recovered': ['location', 'serial'],
        'new_subscriber':   ['name', 'phone', 'plan'],
        'payment_received': ['name', 'phone', 'plan', 'amount'],
    }
    DEFAULT_BODIES = {
        'plan_expiry_3d':
            'Hi {{name}}, your {{plan}} plan expires in 3 days ({{expiry_date}}). Top up now: {{link}}',
        'plan_expiry_1d':
            'Hi {{name}}, your {{plan}} plan expires tomorrow. Top up now: {{link}}',
        'plan_expired':
            'Hi {{name}}, your {{plan}} plan has expired. Renew to get back online: {{link}}',
        'welcome':
            'Welcome {{name}}! You are now on the {{plan}} plan. Manage your account: {{link}}',
        'router_offline':
            '\u26a0\ufe0f Router {{location}} ({{serial}}) has gone offline.',
        'router_recovered':
            '\u2705 Router {{location}} ({{serial}}) is back online.',
        'new_subscriber':
            'New subscriber: {{name}} ({{phone}}) just signed up on the {{plan}} plan.',
        'payment_received':
            'Payment received: \u20a6{{amount}} from {{name}} ({{phone}}) for {{plan}} plan.',
    }

    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='notification_templates'
    )
    event_type = models.CharField(max_length=30, choices=EVENT_CHOICES)
    body = models.TextField()
    is_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['reseller', 'event_type']

    def __str__(self):
        return f'{self.reseller.slug} — {self.event_type}'


class SubscriberNotificationPrefs(models.Model):
    CHANNEL_CHOICES = [
        ('sms', 'SMS'),
        ('whatsapp', 'WhatsApp'),
    ]
    subscriber = models.OneToOneField(
        'accounts.Subscriber', on_delete=models.CASCADE, related_name='notification_prefs'
    )
    transactional_enabled = models.BooleanField(default=True)
    transactional_channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default='sms')
    # Broadcast opt-in — False by default (opt-in model)
    receive_alerts = models.BooleanField(default=True)
    receive_promos = models.BooleanField(default=False)
    receive_marketing = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.subscriber.phone} prefs'


class Broadcast(models.Model):
    TYPE_CHOICES = [
        ('alert', 'Alert'),
        ('promo', 'Promo'),
        ('marketing', 'Marketing'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('queued', 'Queued'),
        ('sending', 'Sending'),
        ('sent', 'Sent'),
        ('cancelled', 'Cancelled'),
    ]
    CHANNEL_CHOICES = [
        ('whatsapp', 'WhatsApp'),
        ('sms', 'SMS'),
        ('both', 'WhatsApp + SMS'),
    ]
    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='broadcasts'
    )
    type = models.CharField(max_length=15, choices=TYPE_CHOICES)
    body = models.TextField()
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default='sms')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='draft')
    total_recipients = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.reseller.slug} {self.type} broadcast ({self.status})'

    @property
    def progress_pct(self):
        if not self.total_recipients:
            return 0
        return round((self.sent_count + self.failed_count) / self.total_recipients * 100)


class NotificationLog(models.Model):
    CHANNEL_CHOICES = [
        ('whatsapp', 'WhatsApp'),
        ('sms', 'SMS'),
    ]
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('sending', 'Sending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ]
    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='notification_logs',
        null=True, blank=True,
    )
    subscriber = models.ForeignKey(
        'accounts.Subscriber', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='notification_logs'
    )
    recipient_phone = models.CharField(max_length=20)
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    event_type = models.CharField(max_length=30)
    broadcast = models.ForeignKey(
        Broadcast, on_delete=models.SET_NULL, null=True, blank=True, related_name='logs'
    )
    body = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='queued')
    error_detail = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['reseller', '-created_at']),
            models.Index(fields=['broadcast', 'status']),
        ]

    def __str__(self):
        return f'{self.recipient_phone} {self.channel} {self.event_type} ({self.status})'
