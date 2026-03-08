"""DRF API endpoints for reseller auth and dashboard data."""
from django.urls import path
from accounts import views

urlpatterns = [
    path('signup/', views.reseller_signup, name='api-reseller-signup'),
    path('login/', views.reseller_login, name='api-reseller-login'),
    path('dashboard/', views.reseller_dashboard_data, name='api-reseller-dashboard'),
]
