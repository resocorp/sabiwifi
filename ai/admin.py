from django.contrib import admin

from ai.models import AIAgentRun, AIPromptVersion, ResellerAIConfig


@admin.register(ResellerAIConfig)
class ResellerAIConfigAdmin(admin.ModelAdmin):
    list_display = ('reseller', 'text_provider', 'text_model', 'ai_paused_at', 'updated_at')
    search_fields = ('reseller__slug', 'reseller__name', 'text_model')
    list_filter = ('text_provider', 'ai_paused_at')
    readonly_fields = ('text_api_key_encrypted', 'asr_api_key_encrypted',
                       'created_at', 'updated_at')


@admin.register(AIAgentRun)
class AIAgentRunAdmin(admin.ModelAdmin):
    list_display = ('reseller', 'agent_role', 'status', 'provider', 'model',
                    'prompt_tokens', 'completion_tokens', 'cost_ngn', 'started_at')
    list_filter = ('agent_role', 'status', 'provider')
    search_fields = ('reseller__slug', 'error_message')
    readonly_fields = ('inputs', 'outputs', 'tool_calls',
                       'started_at', 'ended_at', 'latency_ms')
    date_hierarchy = 'started_at'


@admin.register(AIPromptVersion)
class AIPromptVersionAdmin(admin.ModelAdmin):
    list_display = ('config', 'agent_role', 'edited_by', 'created_at')
    list_filter = ('agent_role',)
    search_fields = ('config__reseller__slug', 'body', 'note')
    date_hierarchy = 'created_at'
