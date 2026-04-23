from django.urls import path

from conversations import views

app_name = 'conversations'

urlpatterns = [
    # Inbound webhook from the Node WA sidecar
    path('inbound-wa/', views.inbound_wa, name='inbound-wa'),

    # Reseller dashboard API
    path('', views.conversation_list, name='list'),
    path('<int:pk>/', views.conversation_detail, name='detail'),
    path('<int:pk>/reply/', views.conversation_reply, name='reply'),
    path('<int:pk>/resolve/', views.conversation_resolve, name='resolve'),
    path('<int:pk>/reopen/', views.conversation_reopen, name='reopen'),
    path('<int:pk>/delete/', views.conversation_delete, name='delete'),
    path('<int:pk>/toggle-ai/', views.conversation_toggle_ai, name='toggle-ai'),
    path('<int:pk>/drafts/<int:message_id>/send/',
         views.conversation_send_draft, name='send-draft'),
    path('<int:pk>/drafts/<int:message_id>/discard/',
         views.conversation_discard_draft, name='discard-draft'),
]
