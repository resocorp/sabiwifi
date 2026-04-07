import secrets
import re
from django.db import models
from django.contrib.auth.models import User
from django.contrib.auth.hashers import make_password, check_password
from django.utils.text import slugify
from simple_history.models import HistoricalRecords


class Country(models.Model):
    """
    Supported countries for subscriber registration.
    Defines how phone numbers are stored and sent via SMS.
    """
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=2, unique=True, help_text='ISO 3166-1 alpha-2, e.g. NG')
    dial_code = models.CharField(max_length=10, help_text='International prefix, e.g. +234')
    local_prefix = models.CharField(max_length=5, default='0', help_text='Leading digit(s) in local format, e.g. 0')
    local_phone_length = models.IntegerField(default=11, help_text='Expected length of local-format number, e.g. 11 for Nigeria')
    flag_emoji = models.CharField(max_length=10, blank=True, default='')
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=100)

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name_plural = 'Countries'

    def __str__(self):
        return f'{self.flag_emoji} {self.name} ({self.dial_code})'

    def normalize_to_local(self, raw_phone):
        """
        Convert any input format to local storage format (e.g. 0803XXXXXXX for Nigeria).
        Returns None if phone is invalid for this country.
        """
        phone = re.sub(r'[\s\-()]', '', raw_phone.strip())
        intl_prefix = self.dial_code.lstrip('+')  # e.g. '234'

        # Already local format: 0803...
        if phone.startswith(self.local_prefix):
            if len(phone) == self.local_phone_length:
                return phone

        # International with +: +2348033...
        elif phone.startswith('+' + intl_prefix):
            local = self.local_prefix + phone[1 + len(intl_prefix):]
            if len(local) == self.local_phone_length:
                return local

        # International without +: 2348033...
        elif phone.startswith(intl_prefix):
            local = self.local_prefix + phone[len(intl_prefix):]
            if len(local) == self.local_phone_length:
                return local

        return None

    def to_international(self, local_phone):
        """Convert local format (0803...) to international (+234803...)."""
        if local_phone.startswith(self.local_prefix):
            return self.dial_code + local_phone[len(self.local_prefix):]
        return local_phone


class Reseller(models.Model):
    """
    A WiFi reseller — someone who sells internet access to subscribers.
    Linked 1:1 to a Django User for authentication.
    """
    STATUS_CHOICES = [
        ('setup', 'Setup'),
        ('pending_router', 'Pending Router'),
        ('active', 'Active'),
        ('suspended', 'Suspended'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='reseller')
    name = models.CharField(max_length=200, help_text="Business name")
    slug = models.SlugField(max_length=200, unique=True)
    owner_name = models.CharField(max_length=200)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, unique=True)
    location = models.CharField(max_length=500, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='setup')

    # Portal branding (JSON)
    branding = models.JSONField(default=dict, blank=True, help_text=(
        'Portal branding config: portal_title, welcome_text, bg_image, '
        'logo, primary_color, template (modern/bold/minimal)'
    ))

    # Payment / KYC
    bank_code = models.CharField(max_length=20, blank=True, default='')
    bank_name = models.CharField(max_length=200, blank=True, default='')
    account_number = models.CharField(max_length=20, blank=True, default='')
    account_name = models.CharField(max_length=200, blank=True, default='')
    paystack_subaccount_code = models.CharField(max_length=100, blank=True, default='')
    payment_verified = models.BooleanField(default=False)

    # Configurable overrides (nullable = fall back to PlatformSettings defaults)
    commission_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Override platform default commission. Null = use platform default."
    )
    fee_bearer = models.CharField(
        max_length=20, blank=True, default='',
        choices=[('', 'Platform Default'), ('account', 'Platform'), ('subaccount', 'Reseller')],
        help_text="Override who bears the Paystack fee. Empty = use platform default."
    )
    free_subscriber_limit = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Override default free subscriber limit. Null = use platform default."
    )

    # Account creation fee
    signup_fee_enabled = models.BooleanField(default=False)
    signup_fee_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Future multi-gateway
    flutterwave_subaccount_id = models.CharField(max_length=100, blank=True, default='')
    monnify_subaccount_code = models.CharField(max_length=100, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
            # Ensure unique slug
            original_slug = self.slug
            counter = 1
            while Reseller.objects.filter(slug=self.slug).exclude(pk=self.pk).exists():
                self.slug = f'{original_slug}-{counter}'
                counter += 1
        super().save(*args, **kwargs)

    def get_commission_pct(self):
        """Return effective commission percentage, falling back to platform default."""
        if self.commission_pct is not None:
            return self.commission_pct
        from operator_panel.models import PlatformSettings
        return PlatformSettings.load().default_commission_pct

    def get_fee_bearer(self):
        """Return effective fee bearer, falling back to platform default."""
        if self.fee_bearer:
            return self.fee_bearer
        from operator_panel.models import PlatformSettings
        return PlatformSettings.load().default_fee_bearer

    def get_free_subscriber_limit(self):
        """Return effective free subscriber limit, falling back to platform default."""
        if self.free_subscriber_limit is not None:
            return self.free_subscriber_limit
        from operator_panel.models import PlatformSettings
        return PlatformSettings.load().default_free_subscriber_limit


class Subscriber(models.Model):
    """
    A WiFi subscriber — someone who connects to a reseller's hotspot.
    Authenticated via phone + PIN. No Django User — lightweight model.
    """
    reseller = models.ForeignKey(Reseller, on_delete=models.CASCADE, related_name='subscribers')
    country = models.ForeignKey(
        Country, on_delete=models.PROTECT, null=True, blank=True,
        help_text='Country determines phone format and SMS routing.'
    )
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True, default='')
    pin_hash = models.CharField(max_length=255, blank=True, default='')
    auth_token = models.CharField(max_length=64, blank=True, default='')
    verified = models.BooleanField(default=False)
    signup_fee_paid = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        unique_together = ['reseller', 'phone']
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.phone} ({self.reseller.name})'

    def set_pin(self, raw_pin):
        """Hash and store the PIN."""
        self.pin_hash = make_password(raw_pin)

    def check_pin(self, raw_pin):
        """Check a raw PIN against the stored hash."""
        return check_password(raw_pin, self.pin_hash)

    def generate_auth_token(self):
        """Generate a new 64-char random auth token."""
        self.auth_token = secrets.token_hex(32)
        return self.auth_token
