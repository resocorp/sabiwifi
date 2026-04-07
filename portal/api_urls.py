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
    path('change-pin/', views.portal_change_pin, name='api-portal-change-pin'),
    path('reset-pin/', views.portal_reset_pin_request, name='api-portal-reset-pin'),
    path('reset-pin/confirm/', views.portal_reset_pin_confirm, name='api-portal-reset-pin-confirm'),
    path('account/change-plan/', views.portal_change_plan, name='api-portal-change-plan'),
    path('disconnect/', views.portal_disconnect, name='api-portal-disconnect'),
    path('initiate-payment/', views.portal_initiate_payment, name='api-portal-initiate-payment'),
    path('initiate-signup-payment/', views.portal_initiate_signup_payment, name='api-portal-initiate-signup-payment'),
]
