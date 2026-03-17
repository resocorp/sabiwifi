"""Django admin registrations for all SabiWiFi models."""
import csv
import io
import logging

from django.conf import settings
from django.contrib import admin
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils.html import format_html
from simple_history.admin import SimpleHistoryAdmin
from operator_panel.models import PlatformSettings
from accounts.models import Reseller, Subscriber
from plans.models import ServicePlan, Subscription
from billing.models import Payment
from routers.models import Router
from routers.bootstrap import generate_bootstrap_rsc, generate_generic_bootstrap_rsc

logger = logging.getLogger(__name__)


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
    list_display = ['serial_number', 'reseller', 'status_display', 'location_name', 'last_seen_display', 'bootstrap_link', 'created_at']
    list_filter = ['status', 'reseller', 'created_at']
    search_fields = ['serial_number', 'reseller__name', 'location_name']
    readonly_fields = [
        'wg_public_key', 'wg_tunnel_ip', 'nas_secret',
        'api_username', 'api_password', 'provision_count',
        'created_at', 'updated_at',
        'bootstrap_download_button',
    ]
    change_list_template = 'admin/routers/router_changelist.html'

    actions = ['mark_available', 'mark_offline', 'import_serials_action', 'download_bootstrap_bulk']

    fieldsets = (
        (None, {
            'fields': ('serial_number', 'reseller', 'status', 'location_name', 'bootstrap_download_button'),
        }),
        ('Health', {
            'fields': ('last_seen', 'provision_count'),
        }),
        ('WireGuard', {
            'fields': ('wg_public_key', 'wg_tunnel_ip'),
            'classes': ('collapse',),
        }),
        ('RADIUS / API', {
            'fields': ('nas_secret', 'api_username', 'api_password'),
            'classes': ('collapse',),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    @admin.display(description='Status', ordering='status')
    def status_display(self, obj):
        """Show status with phone-home indicator for unclaimed routers."""
        from django.utils import timezone
        status_colors = {
            'available': '#6b7280',
            'pending_provision': '#f59e0b',
            'provisioned': '#3b82f6',
            'online': '#10b981',
            'offline': '#ef4444',
            'failed': '#dc2626',
        }
        color = status_colors.get(obj.status, '#6b7280')
        label = obj.get_status_display()

        # Check if router is phoning home (last_seen within 5 minutes)
        if obj.last_seen and obj.status == 'available':
            age = (timezone.now() - obj.last_seen).total_seconds()
            if age < 300:  # 5 minutes
                return format_html(
                    '<span style="color: {}">●</span> {} '
                    '<span style="background: #d1fae5; color: #065f46; padding: 1px 6px; '
                    'border-radius: 8px; font-size: 10px; font-weight: 600;">📡 Phoning Home</span>',
                    color, label,
                )
            else:
                return format_html(
                    '<span style="color: {}">●</span> {} '
                    '<span style="background: #fef3c7; color: #92400e; padding: 1px 6px; '
                    'border-radius: 8px; font-size: 10px;">Last seen {}m ago</span>',
                    color, label, int(age // 60),
                )

        return format_html('<span style="color: {}">●</span> {}', color, label)

    @admin.display(description='Last Seen', ordering='last_seen')
    def last_seen_display(self, obj):
        """Show last_seen with relative time."""
        if not obj.last_seen:
            return '-'
        from django.utils import timezone
        from django.utils.timesince import timesince
        age = (timezone.now() - obj.last_seen).total_seconds()
        time_str = timesince(obj.last_seen) + ' ago'
        if age < 300:
            return format_html('<span style="color: #10b981; font-weight: 600;">{}</span>', time_str)
        return time_str

    @admin.display(description='Bootstrap')
    def bootstrap_link(self, obj):
        """Show a download link in the list view."""
        url = reverse('admin:routers_router_download_bootstrap', args=[obj.pk])
        return format_html('<a href="{}">Download .rsc</a>', url)

    @admin.display(description='Bootstrap Script')
    def bootstrap_download_button(self, obj):
        """Show a download button on the detail/change page."""
        if not obj.pk:
            return '-'
        url = reverse('admin:routers_router_download_bootstrap', args=[obj.pk])
        return format_html(
            '<a href="{}" class="button" style="padding: 5px 15px;">Download Bootstrap .rsc</a>',
            url,
        )

    @admin.action(description='Download bootstrap .rsc for selected routers')
    def download_bootstrap_bulk(self, request, queryset):
        """Download bootstrap for a single selected router (first in selection)."""
        router = queryset.first()
        if router:
            return HttpResponseRedirect(
                reverse('admin:routers_router_download_bootstrap', args=[router.pk])
            )

    @admin.action(description='Mark as available (unassign)')
    def mark_available(self, request, queryset):
        from routers.wg_utils import remove_peer, WireGuardError
        # Remove WireGuard peers before unassigning
        for router in queryset.filter(wg_public_key__gt=''):
            try:
                remove_peer(router.wg_public_key)
            except WireGuardError as e:
                self.message_user(
                    request,
                    f"Warning: failed to remove WG peer for {router.serial_number}: {e}",
                    level='warning',
                )
        queryset.update(
            status='available',
            reseller=None,
            wg_public_key='',
            wg_tunnel_ip=None,
            nas_secret='',
            api_password='',
        )

    @admin.action(description='Mark as offline')
    def mark_offline(self, request, queryset):
        queryset.update(status='offline')

    @admin.action(description='Import serials from file')
    def import_serials_action(self, request, queryset):
        """Redirect to the serial import page."""
        return HttpResponseRedirect(
            reverse('admin:routers_router_import_serials')
        )

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['generic_bootstrap_url'] = reverse(
            'admin:routers_router_download_generic_bootstrap'
        )
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'import-serials/',
                self.admin_site.admin_view(self.import_serials_view),
                name='routers_router_import_serials',
            ),
            path(
                'download-generic-bootstrap/',
                self.admin_site.admin_view(self.download_generic_bootstrap_view),
                name='routers_router_download_generic_bootstrap',
            ),
            path(
                '<int:pk>/download-bootstrap/',
                self.admin_site.admin_view(self.download_bootstrap_view),
                name='routers_router_download_bootstrap',
            ),
        ]
        return custom_urls + urls

    def download_generic_bootstrap_view(self, request):
        """Download the generic bootstrap .rsc (no serial needed)."""
        platform_domain = getattr(settings, 'PLATFORM_DOMAIN', 'localhost')
        rsc_content = generate_generic_bootstrap_rsc(platform_domain)

        response = HttpResponse(rsc_content, content_type='text/plain')
        response['Content-Disposition'] = 'attachment; filename="bootstrap-generic.rsc"'
        return response

    def download_bootstrap_view(self, request, pk):
        """Admin view to download the bootstrap .rsc for a single router."""
        try:
            router = Router.objects.get(pk=pk)
        except Router.DoesNotExist:
            self.message_user(request, 'Router not found.', level='error')
            return HttpResponseRedirect(reverse('admin:routers_router_changelist'))

        platform_domain = getattr(settings, 'PLATFORM_DOMAIN', 'localhost')
        rsc_content = generate_bootstrap_rsc(router.serial_number, platform_domain)

        response = HttpResponse(rsc_content, content_type='text/plain')
        response['Content-Disposition'] = f'attachment; filename="bootstrap-{router.serial_number}.rsc"'
        return response

    def import_serials_view(self, request):
        """Admin view for bulk-importing router serial numbers from a file."""
        if request.method == 'POST' and request.FILES.get('serial_file'):
            serial_file = request.FILES['serial_file']
            content = serial_file.read().decode('utf-8')

            created = 0
            skipped = 0
            errors = []

            if serial_file.name.endswith('.csv'):
                reader = csv.reader(io.StringIO(content))
                lines = [row[0] for row in reader if row]
            else:
                lines = content.splitlines()

            for line in lines:
                serial = line.strip().upper()
                if not serial or serial.startswith('#'):
                    continue
                try:
                    _, was_created = Router.objects.get_or_create(
                        serial_number=serial,
                        defaults={'status': 'available'},
                    )
                    if was_created:
                        created += 1
                    else:
                        skipped += 1
                except Exception as e:
                    errors.append(f"{serial}: {e}")

            msg = f"Import complete: {created} created, {skipped} already existed."
            if errors:
                msg += f" {len(errors)} errors: {'; '.join(errors[:5])}"
            self.message_user(request, msg)
            logger.info(f"Admin serial import: {created} created, {skipped} skipped, {len(errors)} errors")
            return HttpResponseRedirect(reverse('admin:routers_router_changelist'))

        return render(request, 'admin/routers/import_serials.html', {
            'title': 'Import Router Serials',
            'opts': self.model._meta,
        })


# Admin site customization
admin.site.site_header = 'SabiWiFi Admin'
admin.site.site_title = 'SabiWiFi'
admin.site.index_title = 'Platform Administration'
