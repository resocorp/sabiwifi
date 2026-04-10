from django.urls import path
from routers import views

urlpatterns = [
    path('add/', views.router_add, name='api-router-add'),
    path('provision/<str:serial>/', views.router_provision, name='api-router-provision'),
    path('heartbeat/<str:serial>/', views.router_heartbeat, name='api-router-heartbeat'),
    path('phonehome-setup/', views.router_phonehome_setup, name='api-router-phonehome-setup'),
    path('bootstrap/', views.router_bootstrap_generic, name='api-router-bootstrap-generic'),
    path('bootstrap/<str:serial>/', views.router_bootstrap, name='api-router-bootstrap'),
    # OpenWrt phone-home + provision (deprecated — openwisp-config replaces this)
    path('openwrt-provision/<str:mac>/', views.openwrt_provision, name='api-openwrt-provision'),
    # OpenWISP → SabiWiFi webhook (device online/offline events)
    path('openwisp-webhook/', views.openwisp_webhook, name='api-openwisp-webhook'),
    path('', views.router_list, name='api-router-list'),
    path('<int:pk>/status/', views.router_status, name='api-router-status'),
    path('<int:pk>/stats/', views.router_stats, name='api-router-stats'),
    path('<int:pk>/ssid/', views.router_ssid, name='api-router-ssid'),
    path('<int:pk>/wifi/', views.router_wifi, name='api-router-wifi'),
    path('<int:pk>/health-log/', views.router_health_log, name='api-router-health-log'),
    path('<int:pk>/service-mode/', views.router_service_mode, name='api-router-service-mode'),
]
