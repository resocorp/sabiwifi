"""Shop page URLs."""
from django.urls import path
from shop import views

urlpatterns = [
    path('', views.shop_home, name='shop-home'),
    path('cart/', views.shop_cart, name='shop-cart'),
    path('checkout/', views.shop_checkout, name='shop-checkout'),
    path('order/<str:ref>/', views.shop_order, name='shop-order'),
    path('product/<slug:slug>/', views.shop_product, name='shop-product'),
]
