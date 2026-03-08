"""Operator panel URLs."""
from django.urls import path
from operator_panel import views

urlpatterns = [
    path('overview/', views.operator_overview, name='operator-overview'),
]
