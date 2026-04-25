from django.contrib import admin

from voice.models import RoutingRule, VoiceCall, VoiceTenant, VoiceTurn


@admin.register(VoiceTenant)
class VoiceTenantAdmin(admin.ModelAdmin):
    list_display = ('reseller', 'voice_enabled', 'sip_provider_name',
                    'concurrent_call_cap', 'recording_enabled', 'updated_at')
    list_filter = ('voice_enabled', 'recording_enabled', 'sip_provider_name')
    search_fields = ('reseller__slug', 'reseller__name', 'sip_provider_name',
                     'outbound_caller_id')
    readonly_fields = ('created_at', 'updated_at', 'sip_credentials_encrypted')


@admin.register(RoutingRule)
class RoutingRuleAdmin(admin.ModelAdmin):
    list_display = ('did_e164', 'reseller', 'agent_role', 'is_active',
                    'created_at')
    list_filter = ('agent_role', 'is_active')
    search_fields = ('did_e164', 'reseller__slug', 'reseller__name')


@admin.register(VoiceCall)
class VoiceCallAdmin(admin.ModelAdmin):
    list_display = ('call_id', 'direction', 'reseller', 'from_e164', 'to_e164',
                    'status', 'duration_seconds', 'cost_ngn', 'started_at')
    list_filter = ('status', 'direction', 'reseller')
    search_fields = ('call_id', 'from_e164', 'to_e164', 'reseller__slug')
    readonly_fields = ('started_at', 'answered_at', 'ended_at',
                       'duration_seconds', 'transcript', 'metadata')
    date_hierarchy = 'started_at'


@admin.register(VoiceTurn)
class VoiceTurnAdmin(admin.ModelAdmin):
    list_display = ('call', 'direction', 'latency_ms', 'cost_ngn',
                    'started_at')
    list_filter = ('direction',)
    readonly_fields = ('started_at',)
