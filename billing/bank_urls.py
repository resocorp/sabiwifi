from django.urls import path
from billing import bank_views

urlpatterns = [
    path('', bank_views.list_banks, name='api-banks-list'),
    path('resolve/', bank_views.resolve_account, name='api-banks-resolve'),
    path('setup/', bank_views.bank_setup, name='api-banks-setup'),
    path('change/', bank_views.bank_change, name='api-banks-change'),
]
