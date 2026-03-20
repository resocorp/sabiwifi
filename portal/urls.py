"""Captive portal server-rendered pages."""
from django.urls import path
from portal import page_views

urlpatterns = [
    path('', page_views.portal_login, name='portal-login'),
    path('signup/', page_views.portal_signup, name='portal-signup'),
    path('connected/', page_views.portal_connected, name='portal-connected'),
    path('account/', page_views.portal_account_page, name='portal-account'),
]
