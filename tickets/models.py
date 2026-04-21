"""
Tickets — unified work-item queue for sales follow-ups, installs, support,
billing disputes, and anything else a staff member (human or AI) needs to act
on.

A Ticket belongs to a Reseller and is linked to either a Lead (pre-conversion)
or a Subscriber (post-conversion), never both. SLA timers drive the KPI
dashboards (report-to-resolution time, payment-to-service time).

TicketEvent is an append-only audit trail used by the operator panel and the
AI replay UI.
"""
from django.db import models
from django.utils import timezone


# SLA policy (per ticket type, in minutes). These are defaults applied at
# creation time; a reseller-specific override could be layered in later.
SLA_MINUTES_BY_TYPE = {
    'sales_followup': 4 * 60,      # 4h — cold prospects cool fast
    'install': 24 * 60,            # 24h to first tech dispatch
    'support': 60,                 # 1h for inbound support
    'billing': 4 * 60,             # 4h for payment / wallet issues
    'other': 24 * 60,
}


class Ticket(models.Model):
    TYPE_SALES_FOLLOWUP = 'sales_followup'
    TYPE_INSTALL = 'install'
    TYPE_SUPPORT = 'support'
    TYPE_BILLING = 'billing'
    TYPE_OTHER = 'other'
    TYPE_CHOICES = [
        (TYPE_SALES_FOLLOWUP, 'Sales follow-up'),
        (TYPE_INSTALL, 'Installation'),
        (TYPE_SUPPORT, 'Customer support'),
        (TYPE_BILLING, 'Billing / payments'),
        (TYPE_OTHER, 'Other'),
    ]

    STATUS_OPEN = 'open'
    STATUS_ASSIGNED = 'assigned'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_AWAITING_CUSTOMER = 'awaiting_customer'
    STATUS_RESOLVED = 'resolved'
    STATUS_CLOSED = 'closed'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_ASSIGNED, 'Assigned'),
        (STATUS_IN_PROGRESS, 'In progress'),
        (STATUS_AWAITING_CUSTOMER, 'Awaiting customer'),
        (STATUS_RESOLVED, 'Resolved'),
        (STATUS_CLOSED, 'Closed'),
    ]
    TERMINAL_STATUSES = (STATUS_RESOLVED, STATUS_CLOSED)

    PRIORITY_LOW = 'low'
    PRIORITY_NORMAL = 'normal'
    PRIORITY_HIGH = 'high'
    PRIORITY_URGENT = 'urgent'
    PRIORITY_CHOICES = [
        (PRIORITY_LOW, 'Low'),
        (PRIORITY_NORMAL, 'Normal'),
        (PRIORITY_HIGH, 'High'),
        (PRIORITY_URGENT, 'Urgent'),
    ]

    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='tickets',
    )
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_SUPPORT)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL)

    subject = models.CharField(max_length=200)
    body = models.TextField(blank=True, default='')

    # Exactly one of {lead, subscriber} should be set in practice. Both
    # nullable so an ad-hoc operator ticket (no linked actor) is still valid.
    lead = models.ForeignKey(
        'leads.Lead', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='tickets',
    )
    subscriber = models.ForeignKey(
        'accounts.Subscriber', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='tickets',
    )
    # Optional conversation link — the WA/SMS thread this ticket was raised
    # from. Support AI will use this for context.
    conversation = models.ForeignKey(
        'conversations.Conversation', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='tickets',
    )
    # Optional install link — an install ticket points at its InstallationOrder
    # so status changes can cascade (ticket resolved → order completed).
    installation_order = models.ForeignKey(
        'leads.InstallationOrder', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='tickets',
    )

    assigned_staff = models.ForeignKey(
        'staff.StaffMember', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='assigned_tickets',
    )

    # SLA timers — all nullable so we can measure "time-to-first-response" and
    # "time-to-resolution" even if the ticket is still in flight.
    sla_due_at = models.DateTimeField(null=True, blank=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    # AI handling metadata. `ai_handled` is set once any AI agent has taken an
    # action on this ticket. `ai_confidence` is the last agent's self-reported
    # confidence (0-1), useful for filtering low-confidence cases for review.
    ai_handled = models.BooleanField(default=False)
    ai_confidence = models.FloatField(null=True, blank=True)
    escalation_reason = models.CharField(max_length=200, blank=True, default='')

    resolution_note = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['reseller', 'status']),
            models.Index(fields=['reseller', 'type']),
            models.Index(fields=['assigned_staff', 'status']),
            models.Index(fields=['sla_due_at']),
        ]

    def save(self, *args, **kwargs):
        if not self.pk and self.sla_due_at is None:
            minutes = SLA_MINUTES_BY_TYPE.get(self.type, 24 * 60)
            self.sla_due_at = timezone.now() + timezone.timedelta(minutes=minutes)
        super().save(*args, **kwargs)

    def is_breached(self):
        if self.status in self.TERMINAL_STATUSES:
            return False
        return bool(self.sla_due_at and timezone.now() > self.sla_due_at)

    def time_to_resolution_seconds(self):
        if not self.resolved_at:
            return None
        return int((self.resolved_at - self.created_at).total_seconds())

    def time_to_first_response_seconds(self):
        if not self.first_response_at:
            return None
        return int((self.first_response_at - self.created_at).total_seconds())

    def __str__(self):
        return f'#{self.pk} [{self.get_status_display()}] {self.subject}'


class TicketEvent(models.Model):
    """
    Append-only audit trail. Written by signals on Ticket changes, by staff
    actions (assign, comment), and by AI agents (tool-call records).
    """
    KIND_CREATED = 'created'
    KIND_STATUS_CHANGED = 'status_changed'
    KIND_ASSIGNED = 'assigned'
    KIND_COMMENT = 'comment'
    KIND_FIRST_RESPONSE = 'first_response'
    KIND_RESOLVED = 'resolved'
    KIND_CLOSED = 'closed'
    KIND_REOPENED = 'reopened'
    KIND_AI_ACTION = 'ai_action'
    KIND_ESCALATED = 'escalated'
    KIND_CHOICES = [
        (KIND_CREATED, 'Created'),
        (KIND_STATUS_CHANGED, 'Status changed'),
        (KIND_ASSIGNED, 'Assigned'),
        (KIND_COMMENT, 'Comment'),
        (KIND_FIRST_RESPONSE, 'First response'),
        (KIND_RESOLVED, 'Resolved'),
        (KIND_CLOSED, 'Closed'),
        (KIND_REOPENED, 'Reopened'),
        (KIND_AI_ACTION, 'AI action'),
        (KIND_ESCALATED, 'Escalated'),
    ]

    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, related_name='events',
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    # Freeform actor identity: 'human:<user_id>', 'ai_sales', 'ai_support',
    # 'ai_field', 'system'. Keeping it a string avoids a giant union of FKs.
    actor = models.CharField(max_length=60, blank=True, default='system')
    from_status = models.CharField(max_length=20, blank=True, default='')
    to_status = models.CharField(max_length=20, blank=True, default='')
    note = models.TextField(blank=True, default='')
    # Arbitrary per-event metadata (tool-call dumps, assignment target IDs).
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['ticket', 'created_at']),
            models.Index(fields=['kind']),
        ]

    def __str__(self):
        return f'{self.ticket_id} {self.kind} @ {self.created_at:%Y-%m-%d %H:%M}'
