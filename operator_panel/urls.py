"""Operator panel URLs."""
from django.urls import path
from operator_panel import views
from operator_panel import crm_views
from operator_panel import ai_views

urlpatterns = [
    path('overview/', views.operator_overview, name='operator-overview'),
    path('network/', views.operator_network, name='operator-network'),
    path('business/', views.operator_business, name='operator-business'),
    path('partners/', views.operator_partners, name='operator-partners'),
    path('partners/<int:reseller_pk>/subscribers/add/', views.staff_subscriber_create, name='operator-staff-subscriber-create'),
    path('settings/', views.operator_settings, name='operator-settings'),

    # Operator WA + settings API
    path('api/wa/status/', views.operator_wa_status, name='operator-wa-status'),
    path('api/wa/connect/', views.operator_wa_connect, name='operator-wa-connect'),
    path('api/wa/disconnect/', views.operator_wa_disconnect, name='operator-wa-disconnect'),
    path('api/wa/test/', views.operator_wa_test, name='operator-wa-test'),
    path('api/settings/save/', views.operator_settings_save, name='operator-settings-save'),

    # Cross-tenant CRM & KPI (Phase 1 operator oversight)
    path('tickets/', crm_views.operator_tickets, name='operator-tickets'),
    path('conversations/', crm_views.operator_conversations, name='operator-conversations'),
    path('conversations/<int:conversation_id>/', crm_views.operator_conversation_detail, name='operator-conversation-detail'),
    path('leads/<int:lead_id>/', crm_views.operator_lead_detail, name='operator-lead-detail'),
    path('leads/<int:lead_id>/send-quote/', crm_views.operator_send_quote, name='operator-lead-send-quote'),
    path('kpis/', crm_views.operator_kpis, name='operator-kpis'),
    path('api/kpis/', crm_views.operator_kpis_api, name='operator-kpis-api'),
    path('api/queue-health/', crm_views.operator_queue_health, name='operator-queue-health'),

    # Phase 2 AI oversight
    path('ai/', ai_views.operator_ai_overview, name='operator-ai-overview'),
    path('ai/runs/', ai_views.operator_ai_runs, name='operator-ai-runs'),
    path('ai/runs/<int:run_id>/', ai_views.operator_ai_run_detail, name='operator-ai-run-detail'),
    path('ai/<int:reseller_pk>/pause/', ai_views.operator_ai_pause, name='operator-ai-pause'),
    path('ai/<int:reseller_pk>/resume/', ai_views.operator_ai_resume, name='operator-ai-resume'),
]
