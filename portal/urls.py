"""Captive portal server-rendered pages."""
from django.urls import path
from portal import page_views

urlpatterns = [
    path('', page_views.portal_login, name='portal-login'),
    path('connected/', page_views.portal_connected, name='portal-connected'),
]
