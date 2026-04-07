"""Shop API URLs."""
from django.urls import path
from shop import views

urlpatterns = [
    path('shipping-rate/', views.api_shipping_rate, name='api-shop-shipping-rate'),
    path('initiate-payment/', views.api_initiate_payment, name='api-shop-initiate-payment'),
    path('order/<str:ref>/', views.api_order_status, name='api-shop-order-status'),
    path('webhook/', views.api_shop_webhook, name='api-shop-webhook'),
]
