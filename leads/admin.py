from django.contrib import admin

from leads.models import Lead, InstallationOrder, PricingPlan


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ('id', 'reseller', 'name', 'phone', 'status', 'intent', 'source', 'created_at')
    list_filter = ('status', 'intent', 'source', 'reseller')
    search_fields = ('name', 'phone', 'email')
    raw_id_fields = ('reseller', 'interested_plan', 'assigned_staff', 'converted_subscriber', 'chosen_plan')
    readonly_fields = ('created_at', 'updated_at', 'converted_at')


@admin.register(InstallationOrder)
class InstallationOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'reseller', 'lead', 'status', 'service_mode', 'assigned_tech', 'scheduled_for')
    list_filter = ('status', 'service_mode')
    search_fields = ('address', 'lead__name', 'lead__phone', 'pppoe_username')
    raw_id_fields = ('reseller', 'lead', 'payment', 'assigned_tech', 'router')
    readonly_fields = ('created_at', 'updated_at', 'completed_at')


@admin.register(PricingPlan)
class PricingPlanAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'category', 'upfront_label', 'upfront_price_ngn',
        'upfront_discount_price_ngn', 'monthly_price_ngn',
        'is_active', 'sort_order',
    )
    list_filter = ('category', 'is_active')
    list_editable = ('upfront_price_ngn', 'upfront_discount_price_ngn',
                     'monthly_price_ngn', 'is_active', 'sort_order')
    search_fields = ('name', 'tagline', 'best_for')
    fieldsets = (
        (None, {'fields': ('category', 'name', 'tagline', 'best_for', 'is_active', 'sort_order')}),
        ('Pricing', {'fields': (
            'upfront_label', 'upfront_price_ngn', 'upfront_discount_price_ngn',
            'monthly_price_ngn',
        )}),
        ('Features', {
            'fields': ('features',),
            'description': 'One feature per line. Renders as a checkmark list on the public card.',
        }),
        ('Audit', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )
    readonly_fields = ('created_at', 'updated_at')
