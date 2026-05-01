"""Public recharge API endpoints (sabiwifi.com widget)."""
from django.urls import path
from portal import recharge as views

urlpatterns = [
    path('lookup/', views.recharge_lookup, name='api-recharge-lookup'),
    path('send-otp/', views.recharge_send_otp, name='api-recharge-send-otp'),
    path('verify/', views.recharge_verify, name='api-recharge-verify'),
    path('initiate-payment/', views.recharge_initiate_payment, name='api-recharge-initiate-payment'),
    path('complete/', views.recharge_complete, name='api-recharge-complete'),
]
