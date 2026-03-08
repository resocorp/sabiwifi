"""Django admin registrations for all SabiWiFi models."""
from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin
from operator_panel.models import PlatformSettings
from accounts.models import Reseller, Subscriber
from plans.models import ServicePlan, Subscription
from billing.models import Payment
from routers.models import Router


# --- PlatformSettings (Singleton) ---

@admin.register(PlatformSettings)
class PlatformSettingsAdmin(SimpleHistoryAdmin):
    list_display = ['platform_name', 'platform_domain', 'default_commission_pct', 'default_free_subscriber_limit']
    fieldsets = (
        ('Platform Identity', {
            'fields': ('platform_name', 'platform_domain'),
        }),
        ('Commission & Billing', {
            'fields': ('default_commission_pct', 'default_fee_bearer', 'default_free_subscriber_limit'),
        }),
        ('Payment Gateway', {
            'fields': ('paystack_secret_key', 'paystack_public_key'),
            'classes': ('collapse',),
        }),
        ('SMS Provider', {
            'fields': ('termii_api_key', 'termii_sender_id'),
            'classes': ('collapse',),
        }),
        ('Infrastructure', {
            'fields': ('server_ip', 'server_wg_public_key'),
            'classes': ('collapse',),
        }),
        ('Notifications', {
            'fields': (
                'notification_phones',
                'notify_on_new_reseller', 'notify_on_router_offline',
                'notify_on_payment_failure', 'notify_daily_summary',
            ),
        }),
    )

    def has_add_permission(self, request):
        return not PlatformSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


# --- Reseller ---

@admin.register(Reseller)
class ResellerAdmin(SimpleHistoryAdmin):
    list_display = ['name', 'owner_name', 'email', 'phone', 'status', 'payment_verified', 'created_at']
    list_filter = ['status', 'payment_verified', 'created_at']
    search_fields = ['name', 'owner_name', 'email', 'phone', 'slug']
    readonly_fields = ['slug', 'created_at', 'updated_at']
    list_editable = ['status']

    fieldsets = (
        (None, {
            'fields': ('user', 'name', 'slug', 'owner_name', 'email', 'phone', 'location', 'status'),
        }),
        ('Branding', {
            'fields': ('branding',),
            'classes': ('collapse',),
        }),
        ('Payment / KYC', {
            'fields': (
                'bank_code', 'bank_name', 'account_number', 'account_name',
                'paystack_subaccount_code', 'payment_verified',
            ),
        }),
        ('Overrides', {
            'fields': ('commission_pct', 'fee_bearer', 'free_subscriber_limit'),
            'classes': ('collapse',),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    actions = ['suspend_resellers', 'activate_resellers']

    @admin.action(description='Suspend selected resellers')
    def suspend_resellers(self, request, queryset):
        queryset.update(status='suspended')

    @admin.action(description='Activate selected resellers')
    def activate_resellers(self, request, queryset):
        queryset.update(status='active')


# --- Subscriber ---

@admin.register(Subscriber)
class SubscriberAdmin(SimpleHistoryAdmin):
    list_display = ['phone', 'reseller', 'email', 'verified', 'created_at']
    list_filter = ['verified', 'reseller', 'created_at']
    search_fields = ['phone', 'email', 'reseller__name']
    readonly_fields = ['created_at', 'updated_at', 'pin_hash', 'auth_token']


# --- ServicePlan ---

@admin.register(ServicePlan)
class ServicePlanAdmin(SimpleHistoryAdmin):
    list_display = [
        'name', 'reseller', 'download_mbps', 'upload_mbps',
        'price_ngn', 'is_active', 'is_trial', 'is_system_created',
    ]
    list_filter = ['is_active', 'is_trial', 'is_system_created', 'reseller']
    search_fields = ['name', 'reseller__name']
    readonly_fields = ['slug', 'created_at', 'updated_at']


# --- Subscription ---

@admin.register(Subscription)
class SubscriptionAdmin(SimpleHistoryAdmin):
    list_display = ['subscriber', 'plan', 'reseller', 'status', 'start_date', 'expiry_date']
    list_filter = ['status', 'reseller', 'start_date']
    search_fields = ['subscriber__phone', 'plan__name', 'reseller__name']
    readonly_fields = ['created_at', 'updated_at']


# --- Payment ---

@admin.register(Payment)
class PaymentAdmin(SimpleHistoryAdmin):
    list_display = [
        'subscriber', 'reseller', 'amount_ngn', 'paystack_status',
        'payment_method', 'reseller_amount_ngn', 'created_at',
    ]
    list_filter = ['paystack_status', 'payment_method', 'reseller', 'created_at']
    search_fields = ['subscriber__phone', 'paystack_reference', 'reseller__name']
    readonly_fields = [
        'paystack_reference', 'commission_pct_applied', 'fee_bearer_applied',
        'platform_amount_ngn', 'reseller_amount_ngn', 'gateway_fee_ngn',
        'created_at', 'updated_at',
    ]


# --- Router ---

@admin.register(Router)
class RouterAdmin(SimpleHistoryAdmin):
    list_display = ['serial_number', 'reseller', 'status', 'location_name', 'last_seen', 'created_at']
    list_filter = ['status', 'reseller', 'created_at']
    search_fields = ['serial_number', 'reseller__name', 'location_name']
    readonly_fields = [
        'wg_public_key', 'wg_tunnel_ip', 'nas_secret',
        'api_username', 'api_password', 'provision_count',
        'created_at', 'updated_at',
    ]
    list_editable = ['status']

    actions = ['mark_available', 'mark_offline']

    @admin.action(description='Mark as available (unassign)')
    def mark_available(self, request, queryset):
        queryset.update(status='available', reseller=None)

    @admin.action(description='Mark as offline')
    def mark_offline(self, request, queryset):
        queryset.update(status='offline')


# Admin site customization
admin.site.site_header = 'SabiWiFi Admin'
admin.site.site_title = 'SabiWiFi'
admin.site.index_title = 'Platform Administration'
