"""
Lead + InstallationOrder models.

A Lead is a prospect who has contacted a reseller but not yet become a
Subscriber. Leads are scoped per-reseller and track the sales funnel from
first inbound message through to payment. Once a payment lands, the lead is
converted to a Subscriber and an InstallationOrder is created for the field
team to act on.

InstallationOrder is the field-side work item: an install scheduled at a
specific address, assigned to a tech, tied to a service mode (pppoe/hotspot)
and optionally to a router that ships with the install.
"""
from django.db import models

from accounts.models import Country


def _normalise_phone(raw):
    if not raw:
        return ''
    for country in Country.objects.filter(is_active=True).order_by('sort_order'):
        for candidate in (raw, '+' + raw if not raw.startswith('+') else raw):
            local = country.normalize_to_local(candidate)
            if local:
                return local
    return raw


class Lead(models.Model):
    SOURCE_WA = 'whatsapp'
    SOURCE_SMS = 'sms'
    SOURCE_EMAIL = 'email'
    SOURCE_VOICE = 'voice'
    SOURCE_WEB = 'web'
    SOURCE_REFERRAL = 'referral'
    SOURCE_MANUAL = 'manual'
    SOURCE_CHOICES = [
        (SOURCE_WA, 'WhatsApp'),
        (SOURCE_SMS, 'SMS'),
        (SOURCE_EMAIL, 'Email'),
        (SOURCE_VOICE, 'Phone call'),
        (SOURCE_WEB, 'Web form'),
        (SOURCE_REFERRAL, 'Referral'),
        (SOURCE_MANUAL, 'Manually added'),
    ]

    INTENT_UNKNOWN = 'unknown'
    INTENT_HOME = 'home'
    INTENT_SME = 'sme'
    INTENT_CLUSTER = 'cluster'
    INTENT_TECHNICAL = 'technical'
    INTENT_OTHER = 'other'
    INTENT_CHOICES = [
        (INTENT_UNKNOWN, 'Unknown'),
        (INTENT_HOME, 'Home / home-office'),
        (INTENT_SME, 'SME (hotel, small factory, business)'),
        (INTENT_CLUSTER, 'Building hotspot (hostel, lodge, 50+ residents)'),
        (INTENT_TECHNICAL, 'Technical question (existing customer)'),
        (INTENT_OTHER, 'Other'),
    ]

    STATUS_NEW = 'new'
    STATUS_QUALIFIED = 'qualified'
    STATUS_QUOTED = 'quoted'
    STATUS_PAID = 'paid'
    STATUS_INSTALLED = 'installed'
    STATUS_LOST = 'lost'
    STATUS_CHOICES = [
        (STATUS_NEW, 'New'),
        (STATUS_QUALIFIED, 'Qualified'),
        (STATUS_QUOTED, 'Quoted'),
        (STATUS_PAID, 'Paid'),
        (STATUS_INSTALLED, 'Installed'),
        (STATUS_LOST, 'Lost'),
    ]

    # Nullable: platform-direct leads (e.g. from sabiwifi.com TikTok funnel)
    # have no partner attribution at intake. Ops assigns a partner before
    # quoting/converting (Paystack split needs a verified subaccount).
    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='leads',
        null=True, blank=True,
    )
    phone = models.CharField(max_length=20)
    name = models.CharField(max_length=200, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    address = models.TextField(blank=True, default='')

    # Optional GPS pin from public form. Ops verifies coverage manually.
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
    )

    # Marketing attribution — populated from public landing form.
    utm_source = models.CharField(max_length=128, blank=True, default='')
    utm_medium = models.CharField(max_length=128, blank=True, default='')
    utm_campaign = models.CharField(max_length=128, blank=True, default='')
    utm_term = models.CharField(max_length=128, blank=True, default='')
    utm_content = models.CharField(max_length=128, blank=True, default='')
    landing_url = models.URLField(blank=True, default='')
    click_id = models.CharField(
        max_length=128, blank=True, default='',
        help_text='TikTok ttclid / Meta fbclid / Google gclid',
    )

    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    intent = models.CharField(max_length=20, choices=INTENT_CHOICES, default=INTENT_UNKNOWN)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW)

    # Optional plan preference, set once a quote is attached.
    interested_plan = models.ForeignKey(
        'plans.ServicePlan', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='leads',
    )
    quoted_amount_ngn = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )

    assigned_staff = models.ForeignKey(
        'staff.StaffMember', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='assigned_leads',
    )

    # Set when the lead becomes a paying subscriber.
    converted_subscriber = models.ForeignKey(
        'accounts.Subscriber', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='origin_lead',
    )
    converted_at = models.DateTimeField(null=True, blank=True)
    lost_reason = models.CharField(max_length=200, blank=True, default='')

    notes = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['reseller', 'status']),
            models.Index(fields=['reseller', 'phone']),
        ]

    def save(self, *args, **kwargs):
        self.phone = _normalise_phone(self.phone) or self.phone
        super().save(*args, **kwargs)

    def __str__(self):
        name = self.name or self.phone
        return f'{name} ({self.reseller.slug}, {self.get_status_display()})'


class InstallationOrder(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_SCHEDULED = 'scheduled'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_COMPLETED = 'completed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending scheduling'),
        (STATUS_SCHEDULED, 'Scheduled'),
        (STATUS_IN_PROGRESS, 'In progress'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    SERVICE_MODE_PPPOE = 'pppoe'
    SERVICE_MODE_HOTSPOT = 'hotspot'
    SERVICE_MODE_CHOICES = [
        (SERVICE_MODE_PPPOE, 'PPPoE'),
        (SERVICE_MODE_HOTSPOT, 'Captive portal / hotspot'),
    ]

    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='installation_orders',
    )
    lead = models.ForeignKey(
        Lead, on_delete=models.CASCADE, related_name='installation_orders',
    )
    # Nullable — the order may exist before payment (quote stage) and link
    # to the Payment once it clears. Nullable the other way too because
    # InstallationOrder is created before Payment in some flows.
    payment = models.ForeignKey(
        'billing.Payment', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='installation_orders',
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    service_mode = models.CharField(
        max_length=10, choices=SERVICE_MODE_CHOICES, default=SERVICE_MODE_PPPOE,
    )

    address = models.TextField(blank=True, default='')
    scheduled_for = models.DateTimeField(null=True, blank=True)
    assigned_tech = models.ForeignKey(
        'staff.StaffMember', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='assigned_installations',
    )

    # Router shipped / linked to this install, once provisioned.
    router = models.ForeignKey(
        'routers.Router', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='installation_orders',
    )

    # PPPoE creds issued at conversion time (hotspot installs leave these blank).
    pppoe_username = models.CharField(max_length=100, blank=True, default='')
    pppoe_password = models.CharField(max_length=100, blank=True, default='')

    notes = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['reseller', 'status']),
        ]

    def __str__(self):
        return f'Install for {self.lead} — {self.get_status_display()}'
