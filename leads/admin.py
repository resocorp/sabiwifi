from django.contrib import admin

from leads.models import Lead, InstallationOrder


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ('id', 'reseller', 'name', 'phone', 'status', 'intent', 'source', 'created_at')
    list_filter = ('status', 'intent', 'source', 'reseller')
    search_fields = ('name', 'phone', 'email')
    raw_id_fields = ('reseller', 'interested_plan', 'assigned_staff', 'converted_subscriber')
    readonly_fields = ('created_at', 'updated_at', 'converted_at')


@admin.register(InstallationOrder)
class InstallationOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'reseller', 'lead', 'status', 'service_mode', 'assigned_tech', 'scheduled_for')
    list_filter = ('status', 'service_mode')
    search_fields = ('address', 'lead__name', 'lead__phone', 'pppoe_username')
    raw_id_fields = ('reseller', 'lead', 'payment', 'assigned_tech', 'router')
    readonly_fields = ('created_at', 'updated_at', 'completed_at')
