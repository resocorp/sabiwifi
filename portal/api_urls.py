"""Portal API endpoints (subscriber-facing)."""
from django.urls import path
from portal import views

urlpatterns = [
    path('signup/', views.portal_signup, name='api-portal-signup'),
    path('verify/', views.portal_verify_otp, name='api-portal-verify'),
    path('set-pin/', views.portal_set_pin, name='api-portal-set-pin'),
    path('login/', views.portal_login_api, name='api-portal-login'),
    path('plans/', views.portal_plans, name='api-portal-plans'),
    path('account/', views.portal_account, name='api-portal-account'),
]
