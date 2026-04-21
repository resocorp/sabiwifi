"""Operator panel URLs."""
from django.urls import path
from operator_panel import views

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
]
