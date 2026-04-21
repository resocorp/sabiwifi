"""
Conversation + Message models.

A Conversation is an ongoing thread between a Reseller and one external contact
(a Lead who hasn't paid yet, or a Subscriber) over a single channel. Messages
flow in both directions and are the durable audit trail for everything the
inbox and future AI agents produce.

Tenancy: every Conversation is scoped to a Reseller. Uniqueness is per
(reseller, channel, external_thread_id) so the same WA JID in two different
reseller tenants is two distinct conversations.
"""
from django.db import models


class Conversation(models.Model):
    CHANNEL_WHATSAPP = 'whatsapp'
    CHANNEL_SMS = 'sms'
    CHANNEL_EMAIL = 'email'
    CHANNEL_VOICE = 'voice'
    CHANNEL_WEB = 'web'
    CHANNEL_CHOICES = [
        (CHANNEL_WHATSAPP, 'WhatsApp'),
        (CHANNEL_SMS, 'SMS'),
        (CHANNEL_EMAIL, 'Email'),
        (CHANNEL_VOICE, 'Voice'),
        (CHANNEL_WEB, 'Web chat'),
    ]

    STATE_OPEN = 'open'
    STATE_PENDING_HUMAN = 'pending_human'
    STATE_AI_DRAFTED = 'ai_drafted'
    STATE_RESOLVED = 'resolved'
    STATE_CHOICES = [
        (STATE_OPEN, 'Open'),
        (STATE_PENDING_HUMAN, 'Pending human action'),
        (STATE_AI_DRAFTED, 'AI drafted reply awaiting approval'),
        (STATE_RESOLVED, 'Resolved'),
    ]

    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='conversations',
    )
    lead = models.ForeignKey(
        'leads.Lead', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='conversations',
    )
    subscriber = models.ForeignKey(
        'accounts.Subscriber', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='conversations',
    )

    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    # WA JID, phone number (for SMS/voice), email thread Message-ID, etc.
    external_thread_id = models.CharField(max_length=255, db_index=True)
    contact_phone = models.CharField(max_length=20, blank=True, default='')
    contact_display_name = models.CharField(max_length=200, blank=True, default='')

    state = models.CharField(max_length=20, choices=STATE_CHOICES, default=STATE_OPEN)
    # Per-conversation override of reseller AI toggle. False disables AI for
    # this thread even if the reseller has AI on.
    ai_enabled = models.BooleanField(default=True)

    unread_count = models.IntegerField(default=0)
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_outbound_at = models.DateTimeField(null=True, blank=True)

    assigned_staff = models.ForeignKey(
        'staff.StaffMember', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='assigned_conversations',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('reseller', 'channel', 'external_thread_id')]
        ordering = ['-last_message_at', '-created_at']
        indexes = [
            models.Index(fields=['reseller', 'state']),
            models.Index(fields=['reseller', 'channel']),
        ]

    def __str__(self):
        contact = self.contact_display_name or self.contact_phone or self.external_thread_id
        return f'{self.channel}:{contact} ({self.reseller.slug})'

    @property
    def display_contact(self):
        return self.contact_display_name or self.contact_phone or self.external_thread_id


class Message(models.Model):
    DIRECTION_IN = 'in'
    DIRECTION_OUT = 'out'
    DIRECTION_CHOICES = [
        (DIRECTION_IN, 'Inbound'),
        (DIRECTION_OUT, 'Outbound'),
    ]

    SOURCE_CUSTOMER = 'customer'
    SOURCE_HUMAN = 'human_staff'
    SOURCE_AI_SALES = 'ai_sales'
    SOURCE_AI_SUPPORT = 'ai_support'
    SOURCE_AI_FIELD = 'ai_field'
    SOURCE_SYSTEM = 'system'
    SOURCE_CHOICES = [
        (SOURCE_CUSTOMER, 'Customer'),
        (SOURCE_HUMAN, 'Human staff'),
        (SOURCE_AI_SALES, 'AI sales'),
        (SOURCE_AI_SUPPORT, 'AI support'),
        (SOURCE_AI_FIELD, 'AI field supervisor'),
        (SOURCE_SYSTEM, 'System'),
    ]

    DELIVERY_PENDING = 'pending'
    DELIVERY_QUEUED = 'queued'
    DELIVERY_SENT = 'sent'
    DELIVERY_DELIVERED = 'delivered'
    DELIVERY_FAILED = 'failed'
    DELIVERY_CHOICES = [
        (DELIVERY_PENDING, 'Pending'),
        (DELIVERY_QUEUED, 'Queued'),
        (DELIVERY_SENT, 'Sent'),
        (DELIVERY_DELIVERED, 'Delivered'),
        (DELIVERY_FAILED, 'Failed'),
    ]

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='messages',
    )
    direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES)
    body = models.TextField(blank=True, default='')
    attachments = models.JSONField(default=list, blank=True)

    # Provider-specific identifier used for dedupe + delivery-receipt correlation.
    external_message_id = models.CharField(
        max_length=255, blank=True, default='', db_index=True,
    )

    # Inbound only — the raw sender identifier if different from the
    # conversation's thread id (e.g. group messages, not yet supported).
    sender_phone = models.CharField(max_length=20, blank=True, default='')

    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_CUSTOMER)

    # Outbound-only fields
    delivery_status = models.CharField(
        max_length=20, choices=DELIVERY_CHOICES, default=DELIVERY_PENDING, blank=True,
    )
    delivery_error = models.TextField(blank=True, default='')
    sent_by = models.ForeignKey(
        'staff.StaffMember', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='messages_sent',
    )

    # For AI-generated messages, link back to the agent run that produced them.
    # Set up once the ai/ app is in place; kept nullable so foundation phase works
    # without the ai/ app installed.
    agent_run_id = models.BigIntegerField(null=True, blank=True)

    # When true the message is an AI draft awaiting human approval in the inbox.
    # Drafts are not delivered to the customer until a human clicks Send, which
    # clears this flag.
    is_draft = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['conversation', 'created_at']),
            models.Index(fields=['external_message_id']),
        ]

    def __str__(self):
        preview = (self.body[:60] + '…') if len(self.body) > 60 else self.body
        return f'{self.direction}:{self.source}:{preview}'
