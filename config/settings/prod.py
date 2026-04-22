"""
Production settings for SabiWiFi.
"""
from decouple import config  # noqa: F401

from .base import *

DEBUG = False

# Security
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
# Trust the X-Forwarded-Proto header set by nginx (and by the WA sidecar
# when it POSTs over the loopback). Without this, every loopback POST gets
# 301-redirected to https://, breaking sidecar → Django webhooks.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
X_FRAME_OPTIONS = 'DENY'

# In production we want real SMTP — default to Django's SMTP backend unless
# overridden via EMAIL_BACKEND in .env. (base.py defaults to console for dev.)
EMAIL_BACKEND = config(
    'EMAIL_BACKEND', default='django.core.mail.backends.smtp.EmailBackend',
)
