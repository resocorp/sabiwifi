"""
SabiWiFi URL Configuration.
"""
from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path, include

from voice import views as voice_views

urlpatterns = [
    path('admin/', admin.site.urls),

    # Reseller auth (server-rendered pages)
    path('', include('accounts.urls')),

    # Reseller dashboard (server-rendered)
    path('dashboard/', include('dashboard.urls')),

    # API — Reseller endpoints
    path('api/resellers/', include('accounts.api_urls')),

    # API — Plans
    path('api/resellers/plans/', include('plans.api_urls')),

    # API — Routers
    path('api/routers/', include('routers.api_urls')),

    # API — Billing
    path('api/billing/', include('billing.api_urls')),

    # API — Portal (subscriber-facing)
    path('api/portal/', include('portal.api_urls')),

    # API — Public recharge (sabiwifi.com widget)
    path('api/recharge/', include('portal.recharge_urls')),

    # API — Banks
    path('api/banks/', include('billing.bank_urls')),

    # Portal (captive portal + subscriber self-service)
    path('portal/', include('portal.urls')),
    path('account/', include('portal.account_urls')),

    # Operator
    path('operator/', include('operator_panel.urls')),

    # Notifications (WA webhook + reseller config + broadcasts)
    path('api/notifications/', include('notifications.urls')),

    # Voice (AVR webhook + reseller voice config)
    path('api/voice/', include('voice.urls')),
    # Bridge-container contract — kept under /api/ai/ since it's the LLM
    # bridge, not a tenant API. Header-auth'd same as /api/voice/webhook/.
    path('api/ai/voice-turn/', voice_views.voice_turn, name='voice-turn-bridge'),

    # Shop (public storefront)
    path('shop/', include('shop.urls')),
    path('api/shop/', include('shop.api_urls')),

    # CRM — Conversations, Leads, Tickets, Staff
    path('api/conversations/', include('conversations.urls')),
    path('api/leads/', include('leads.urls')),
    path('api/tickets/', include('tickets.urls')),
    path('api/staff/', include('staff.urls')),

    # Queue admin (RQ dashboard) — staff-only via django-rq's own decorator.
    path('django-rq/', include('django_rq.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
