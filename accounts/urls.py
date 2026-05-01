"""Server-rendered reseller auth pages (signup, login, logout)."""
from django.urls import path
from django.contrib.auth import views as auth_views
from accounts import page_views

urlpatterns = [
    path('', page_views.landing_page, name='landing'),
    path('partners/', page_views.partners_page, name='partners'),
    path('recharge/', page_views.recharge_page, name='recharge'),
    path('signup/', page_views.signup_page, name='signup'),
    path('login/', page_views.login_page, name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
]
