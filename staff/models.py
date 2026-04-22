"""
Staff directory — field technicians, office care agents, dispatchers.

Each reseller registers their own team. Platform-staff entries (reseller=null)
are used by SabiWiFi itself for cross-tenant dispatch or support during
reseller incidents.
"""
from django.conf import settings
from django.db import models


class StaffMember(models.Model):
    ROLE_FIELD_TECH = 'field_tech'
    ROLE_OFFICE_CARE = 'office_care'
    ROLE_DISPATCHER = 'dispatcher'
    ROLE_MANAGER = 'manager'
    ROLE_CHOICES = [
        (ROLE_FIELD_TECH, 'Field technician'),
        (ROLE_OFFICE_CARE, 'Office / customer care'),
        (ROLE_DISPATCHER, 'Dispatcher'),
        (ROLE_MANAGER, 'Manager'),
    ]

    reseller = models.ForeignKey(
        'accounts.Reseller', null=True, blank=True, on_delete=models.CASCADE,
        related_name='staff_members',
        help_text='Null = platform-level staff (SabiWiFi itself).',
    )
    name = models.CharField(max_length=200)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)

    phone = models.CharField(max_length=20)
    whatsapp = models.CharField(max_length=20, blank=True, default='')
    email = models.EmailField(blank=True, default='')

    # JSON list of area names (LGA / city / neighbourhood). Used by the field
    # supervisor agent to pick the best available tech for a ticket.
    coverage_areas = models.JSONField(default=list, blank=True)
    # JSON object: { "mon": ["08:00","18:00"], ... } — optional, free-form.
    shift_hours = models.JSONField(default=dict, blank=True)

    active = models.BooleanField(default=True)
    # Denormalised — maintained by tickets signals. Read-mostly; OK to be
    # slightly stale (operator dashboard recomputes on demand).
    current_load = models.IntegerField(default=0)

    notes = models.TextField(blank=True, default='')

    # Optional Django auth user for RBAC. Owner accounts use Reseller.user;
    # staff who can log in get their own User row linked here. Nullable so
    # legacy data-only StaffMember rows keep working.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='staff_member',
    )
    can_log_in = models.BooleanField(
        default=False,
        help_text='If true, staff member is provisioned with a login.',
    )

    hired_at = models.DateTimeField(null=True, blank=True)
    last_active_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['reseller_id', 'role', 'name']
        indexes = [
            models.Index(fields=['reseller', 'role', 'active']),
        ]

    def __str__(self):
        scope = self.reseller.slug if self.reseller_id else 'platform'
        return f'{self.name} ({self.get_role_display()}, {scope})'
