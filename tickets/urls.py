from django.urls import path

from tickets import views

app_name = 'tickets'

urlpatterns = [
    path('', views.ticket_list, name='list'),
    path('create/', views.ticket_create, name='create'),
    path('<int:pk>/', views.ticket_detail, name='detail'),
    path('<int:pk>/assign/', views.ticket_assign, name='assign'),
    path('<int:pk>/status/', views.ticket_status, name='status'),
    path('<int:pk>/comment/', views.ticket_comment, name='comment'),
    path('<int:pk>/escalate/', views.ticket_escalate, name='escalate'),
]
