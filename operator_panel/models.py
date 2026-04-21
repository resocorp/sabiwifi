from django.db import models
from simple_history.models import HistoricalRecords


class PlatformSettings(models.Model):
    """
    Singleton model for platform-wide configuration.
    Only one row should exist — enforced by the save() method.
    """
    # Commission & Billing defaults
    default_commission_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=15.00,
        help_text="Default platform commission percentage (e.g. 15.00)"
    )
    default_fee_bearer = models.CharField(
        max_length=20, default='subaccount',
        choices=[('account', 'Platform'), ('subaccount', 'Partner')],
        help_text="Who bears the Paystack transaction fee by default"
    )
    default_free_subscriber_limit = models.PositiveIntegerField(
        default=5,
        help_text="Max active subscribers on free plans before bank verification required"
    )

    # Payment gateway keys
    paystack_secret_key = models.CharField(max_length=255, blank=True, default='')
    paystack_public_key = models.CharField(max_length=255, blank=True, default='')

    # SMS provider
    termii_api_key = models.CharField(max_length=255, blank=True, default='')
    termii_sender_id = models.CharField(max_length=20, default='SabiWiFi')

    # Platform identity
    platform_name = models.CharField(max_length=100, default='SabiWiFi')
    platform_domain = models.CharField(max_length=255, default='sabiwifi.ng')

    # Server infrastructure
    server_ip = models.GenericIPAddressField(default='127.0.0.1')
    server_wg_public_key = models.CharField(max_length=255, blank=True, default='')

    # OpenWISP integration
    openwisp_api_url = models.CharField(
        max_length=255, blank=True, default='',
        help_text='OpenWISP controller base URL, e.g. https://wisp.sabiwifi.com',
    )
    openwisp_api_token = models.CharField(
        max_length=255, blank=True, default='',
        help_text='OpenWISP REST API bearer token (from OpenWISP admin → API tokens)',
    )
    openwisp_shared_secret = models.CharField(
        max_length=128, blank=True, default='',
        help_text='Shared secret baked into firmware for device auto-registration with OpenWISP',
    )
    openwisp_pool_org_id = models.UUIDField(
        null=True, blank=True,
        help_text='UUID of the OpenWISP "pool" organization where unclaimed devices land after first boot',
    )

    # Operator notifications
    notification_phones = models.JSONField(
        default=list, blank=True,
        help_text='JSON list of phone numbers, e.g. ["+2348012345678"]'
    )
    operator_notification_channel = models.CharField(
        max_length=10, default='whatsapp',
        choices=[('sms', 'SMS'), ('whatsapp', 'WhatsApp'), ('both', 'Both')],
        help_text='Channel used to reach operator recipients'
    )
    notify_on_new_reseller = models.BooleanField(default=True)
    notify_on_reseller_activated = models.BooleanField(default=True)
    notify_on_new_order = models.BooleanField(default=True)
    notify_on_router_offline = models.BooleanField(default=True)
    notify_on_router_recovered = models.BooleanField(default=True)
    notify_on_payment_failure = models.BooleanField(default=True)
    notify_on_payment_failure_spike = models.BooleanField(default=True)
    notify_daily_summary = models.BooleanField(default=True)

    # Operator WhatsApp session (Baileys sidecar, session key='operator')
    operator_wa_connected = models.BooleanField(default=False)
    operator_wa_phone = models.CharField(max_length=20, blank=True, default='')

    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Platform Settings'
        verbose_name_plural = 'Platform Settings'

    def __str__(self):
        return self.platform_name

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass  # Prevent deletion of singleton

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
