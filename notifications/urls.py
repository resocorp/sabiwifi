from django.urls import path
from notifications import views

urlpatterns = [
    # WA webhook (from Node service)
    path('wa-webhook/', views.wa_webhook, name='wa-webhook'),
    # WA session management
    path('wa/status/', views.wa_status, name='wa-status'),
    path('wa/connect/', views.wa_connect, name='wa-connect'),
    path('wa/disconnect/', views.wa_disconnect, name='wa-disconnect'),
    path('wa/test/', views.wa_send_test, name='wa-test'),
    # Notification config
    path('config/', views.notification_config, name='notification-config'),
    path('config/save/', views.notification_config_save, name='notification-config-save'),
    # Admin contacts
    path('contacts/add/', views.admin_contact_add, name='admin-contact-add'),
    path('contacts/<int:pk>/delete/', views.admin_contact_delete, name='admin-contact-delete'),
    # Broadcasts
    path('broadcast/create/', views.broadcast_create, name='broadcast-create'),
    path('broadcast/', views.broadcast_list, name='broadcast-list'),
    path('broadcast/<int:pk>/progress/', views.broadcast_progress, name='broadcast-progress'),
    path('broadcast/<int:pk>/cancel/', views.broadcast_cancel, name='broadcast-cancel'),
    # Subscriber prefs (portal-facing)
    path('subscriber-prefs/', views.subscriber_notification_prefs, name='subscriber-prefs'),
    # Log
    path('log/', views.notification_log, name='notification-log'),
]
