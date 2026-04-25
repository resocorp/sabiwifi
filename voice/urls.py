"""Voice URL config.

Mounted under /api/voice/  by config/urls.py. The /api/ai/voice-turn/
path lives in config/urls.py directly (it's the bridge-container contract —
not a tenant-facing API, so it's kept out of /api/voice/ to avoid confusion).
"""
from django.urls import path

from voice import views

urlpatterns = [
    path('webhook/', views.voice_webhook, name='voice-webhook'),
]
