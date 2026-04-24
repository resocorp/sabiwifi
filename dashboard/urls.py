"""Reseller dashboard server-rendered pages."""
from django.urls import path
from dashboard import views
from dashboard import crm_views
from dashboard import ai_views

urlpatterns = [
    # CRM shells (data via /api/* endpoints)
    path('inbox/', crm_views.inbox, name='dashboard-inbox'),
    path('leads/', crm_views.leads, name='dashboard-leads'),
    path('tickets/', crm_views.tickets, name='dashboard-tickets'),
    path('staff/', crm_views.staff, name='dashboard-staff'),

    # AI configuration (Phase 2)
    path('ai/', ai_views.ai_config, name='dashboard-ai-config'),
    path('ai/save/', ai_views.ai_config_save, name='dashboard-ai-config-save'),
    path('ai/prompts/', ai_views.ai_prompt_save, name='dashboard-ai-prompt-save'),
    path('ai/pause/', ai_views.ai_pause, name='dashboard-ai-pause'),
    path('ai/resume/', ai_views.ai_resume, name='dashboard-ai-resume'),
    path('ai/test-whatsapp/', ai_views.ai_test_whatsapp, name='dashboard-ai-test-whatsapp'),

    path('', views.overview, name='dashboard-overview'),
    path('plans/', views.plans_list, name='dashboard-plans'),
    path('plans/create/', views.plan_create, name='dashboard-plan-create'),
    path('plans/<int:pk>/edit/', views.plan_edit, name='dashboard-plan-edit'),
    path('plans/<int:pk>/disable/', views.plan_disable, name='dashboard-plan-disable'),
    path('plans/<int:pk>/enable/', views.plan_enable, name='dashboard-plan-enable'),
    path('plans/<int:pk>/delete/', views.plan_delete, name='dashboard-plan-delete'),
    path('subscribers/', views.subscribers_list, name='dashboard-subscribers'),
    path('subscribers/add/', views.subscriber_create, name='dashboard-subscriber-create'),
    path('subscribers/<int:pk>/', views.subscriber_detail, name='dashboard-subscriber-detail'),
    path('payments/', views.payments_list, name='dashboard-payments'),
    path('routers/', views.routers_list, name='dashboard-routers'),
    path('routers/add/', views.router_add, name='dashboard-router-add'),
    path('routers/<int:pk>/edit/', views.router_edit, name='dashboard-router-edit'),
    path('routers/<int:pk>/remove/', views.router_remove, name='dashboard-router-remove'),
    path('settings/', views.settings_page, name='dashboard-settings'),
    path('broadcasts/', views.broadcasts_page, name='dashboard-broadcasts'),

    # Vouchers
    path('vouchers/', views.voucher_batches, name='dashboard-voucher-batches'),
    path('vouchers/create/', views.voucher_batch_create, name='dashboard-voucher-batch-create'),
    path('vouchers/<int:pk>/', views.voucher_batch_detail, name='dashboard-voucher-batch-detail'),
    path('vouchers/<int:pk>/csv/', views.voucher_batch_export_csv, name='dashboard-voucher-batch-csv'),
    path('vouchers/<int:pk>/toggle/', views.voucher_batch_toggle, name='dashboard-voucher-batch-toggle'),

    # Refill Cards
    path('refill-cards/', views.refill_batches, name='dashboard-refill-batches'),
    path('refill-cards/create/', views.refill_batch_create, name='dashboard-refill-batch-create'),
    path('refill-cards/<int:pk>/csv/', views.refill_batch_export_csv, name='dashboard-refill-batch-csv'),

    # Online Users
    path('online-users/', views.online_users, name='dashboard-online-users'),
    path('online-users/<int:pk>/disconnect/', views.disconnect_user, name='dashboard-disconnect-user'),

    # Reports
    path('reports/traffic/', views.traffic_report, name='dashboard-traffic-report'),
    path('reports/sessions/', views.session_report, name='dashboard-session-report'),
    path('reports/financial/', views.financial_report, name='dashboard-financial-report'),
    path('reports/export/<str:report_type>/', views.report_csv_export, name='dashboard-report-csv'),
]
