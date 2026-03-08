from django.urls import path
from routers import views

urlpatterns = [
    path('add/', views.router_add, name='api-router-add'),
    path('provision/<str:serial>/', views.router_provision, name='api-router-provision'),
    path('', views.router_list, name='api-router-list'),
    path('<int:pk>/status/', views.router_status, name='api-router-status'),
    path('<int:pk>/ssid/', views.router_ssid, name='api-router-ssid'),
]
