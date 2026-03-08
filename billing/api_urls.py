from django.urls import path
from billing import views

urlpatterns = [
    path('initialize/', views.payment_initialize, name='api-billing-initialize'),
    path('webhook/', views.payment_webhook, name='api-billing-webhook'),
    path('callback/', views.payment_callback, name='api-billing-callback'),
    path('subscription/', views.current_subscription, name='api-billing-subscription'),
]
