"""Subscriber self-service pages (accessible from anywhere)."""
from django.urls import path
from portal import page_views

urlpatterns = [
    path('', page_views.portal_account_page, name='account-login'),
]
