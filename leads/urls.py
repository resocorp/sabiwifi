from django.urls import path

from leads import views, public_views

app_name = 'leads'

urlpatterns = [
    # Public, unauthenticated TikTok funnel intake
    path('intake/', public_views.lead_intake, name='intake'),

    path('', views.lead_list, name='list'),
    path('create/', views.lead_create, name='create'),
    path('<int:pk>/', views.lead_detail, name='detail'),
    path('<int:pk>/update/', views.lead_update, name='update'),
    path('<int:pk>/quote/', views.lead_send_quote, name='send-quote'),
    path('<int:pk>/convert/', views.lead_convert, name='convert'),
    path('<int:pk>/lose/', views.lead_mark_lost, name='lose'),

    path('installations/', views.installation_list, name='installation-list'),
    path('installations/<int:pk>/', views.installation_detail, name='installation-detail'),
    path('installations/<int:pk>/update/', views.installation_update, name='installation-update'),
]
