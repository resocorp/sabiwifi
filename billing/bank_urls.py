from django.urls import path
from billing import bank_views

urlpatterns = [
    path('', bank_views.list_banks, name='api-banks-list'),
    path('resolve/', bank_views.resolve_account, name='api-banks-resolve'),
]
