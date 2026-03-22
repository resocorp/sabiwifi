"""Reseller dashboard server-rendered pages."""
from django.urls import path
from dashboard import views

urlpatterns = [
    path('', views.overview, name='dashboard-overview'),
    path('plans/', views.plans_list, name='dashboard-plans'),
    path('plans/create/', views.plan_create, name='dashboard-plan-create'),
    path('plans/<int:pk>/edit/', views.plan_edit, name='dashboard-plan-edit'),
    path('subscribers/', views.subscribers_list, name='dashboard-subscribers'),
    path('subscribers/<int:pk>/', views.subscriber_detail, name='dashboard-subscriber-detail'),
    path('payments/', views.payments_list, name='dashboard-payments'),
    path('routers/', views.routers_list, name='dashboard-routers'),
    path('routers/add/', views.router_add, name='dashboard-router-add'),
    path('settings/', views.settings_page, name='dashboard-settings'),
    path('broadcasts/', views.broadcasts_page, name='dashboard-broadcasts'),
]
