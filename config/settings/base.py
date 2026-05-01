"""
Base settings for SabiWiFi project.
Shared across all environments (dev, prod).
"""
import os
from pathlib import Path
from decouple import config, Csv

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = config('SECRET_KEY', default='django-insecure-change-me-in-production')

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third party
    'rest_framework',
    'rest_framework.authtoken',
    'corsheaders',
    'simple_history',
    # SabiWiFi apps
    'operator_panel',
    'radius',
    'accounts',
    'plans',
    'billing',
    'routers',
    'portal',
    'dashboard',
    'notifications',
    'shop',
    'integrations',
    'vouchers',
    'conversations',
    'staff',
    'leads',
    'tickets',
    'ai',
    'voice',
    'django_rq',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'accounts.middleware.EffectiveResellerMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'simple_history.middleware.HistoryRequestMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database — PostgreSQL (shared with FreeRADIUS via rlm_sql)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME', default='sabiwifi'),
        'USER': config('DB_USER', default='sabiwifi'),
        'PASSWORD': config('DB_PASSWORD', default=''),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Lagos'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files (reseller logos, bg images)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Django REST Framework
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        'accounts.authentication.ResellerTokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '60/minute',
        'user': '120/minute',
    },
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 25,
}

# CORS
CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:8000',
    cast=Csv()
)

# Paystack
PAYSTACK_SECRET_KEY = config('PAYSTACK_SECRET_KEY', default='')
PAYSTACK_PUBLIC_KEY = config('PAYSTACK_PUBLIC_KEY', default='')

# Termii SMS
TERMII_API_KEY = config('TERMII_API_KEY', default='')
TERMII_SENDER_ID = config('TERMII_SENDER_ID', default='SabiWiFi')

# WhatsApp sidecar (Node/Baileys)
WA_SERVICE_URL = config('WA_SERVICE_URL', default='http://127.0.0.1:3001')
WA_API_KEY = config('WA_API_KEY', default='')
OPENWISP_WEBHOOK_SECRET = config('OPENWISP_WEBHOOK_SECRET', default='')

# Voice droplet (Asterisk + AVR). Lives on a separate host, reached over
# HTTPS. Shared-secret header auth mirrors the WA sidecar pattern.
VOICE_SERVICE_URL = config('VOICE_SERVICE_URL', default='http://127.0.0.1:8088')
VOICE_API_KEY = config('VOICE_API_KEY', default='')

# TikTok Pixel — paste your Pixel ID from TikTok Business Center → Events →
# Web Events Manager. Empty string disables the pixel.
TIKTOK_PIXEL_ID = config('TIKTOK_PIXEL_ID', default='')

# Server infrastructure
SERVER_IP = config('SERVER_IP', default='127.0.0.1')
SERVER_WG_PUBLIC_KEY = config('SERVER_WG_PUBLIC_KEY', default='')

# Platform
PLATFORM_DOMAIN = config('PLATFORM_DOMAIN', default='app.sabiwifi.com')

# Email — SMTP (Mailgun / Resend / any SMTP provider). Credentials come from
# .env so local dev can point at the console backend.
EMAIL_BACKEND = config(
    'EMAIL_BACKEND', default='django.core.mail.backends.console.EmailBackend',
)
EMAIL_HOST = config('EMAIL_HOST', default='')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='noreply@sabiwifi.com')

# OpenWrt firmware image path (built by manage.py build_openwrt_firmware)
OPENWRT_FIRMWARE_PATH = '/opt/openwrt-imagebuilder/bin/firmware-latest.bin'

# Feature flag: while MikroTik is the focus of v1.0, OpenWrt runtime
# entry points are kept disabled by default. When False:
#   - /api/routers/openwrt-provision/<mac>/ returns 404
#   - The heartbeat endpoint rejects OpenWrt devices (MAC-format serials)
#     with "# not found"
# Flip to True in prod.py (or via the OPENWRT_ENABLED env var) once OpenWrt
# support is ready to ship.
OPENWRT_ENABLED = config('OPENWRT_ENABLED', default=False, cast=bool)

# Login URL for @login_required redirects
TEST_RUNNER = 'tests.test_runner.SabiWiFiTestRunner'

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'

# Cache — Redis (shared across all Gunicorn workers)
CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': config('REDIS_URL', default='redis://127.0.0.1:6379/1'),
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            'SOCKET_CONNECT_TIMEOUT': 5,
            'SOCKET_TIMEOUT': 5,
            'IGNORE_EXCEPTIONS': False,
        },
        'KEY_PREFIX': 'sabiwifi',
    }
}

# Store sessions in Redis too (survives worker restarts)
SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_CACHE_ALIAS = 'default'

# ---------------------------------------------------------------------------
# Durable job queue (RQ over Redis)
# ---------------------------------------------------------------------------
# Uses a separate DB index from the cache so `FLUSHDB` on the cache never
# wipes enqueued work. Three queues:
#   - 'default'      — generic background work (outbound WA/SMS/email sends)
#   - 'ai'           — per-conversation AI agent runs (phase 2+)
#   - 'low'          — non-urgent / catch-up (reports, exports)
RQ_REDIS_URL = config('RQ_REDIS_URL', default='redis://127.0.0.1:6379/2')
RQ_QUEUES = {
    'default': {'URL': RQ_REDIS_URL, 'DEFAULT_TIMEOUT': 300},
    'ai':      {'URL': RQ_REDIS_URL, 'DEFAULT_TIMEOUT': 120},
    'low':     {'URL': RQ_REDIS_URL, 'DEFAULT_TIMEOUT': 900},
}

# AI supervisors (Phase 2) ----------------------------------------------------
# Symmetric key for encrypting reseller-provided AI provider API keys at rest.
# Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# and set in .env as AI_FERNET_KEY. Empty in dev is fine — ai.crypto refuses
# to encrypt/decrypt until the key is set.
AI_FERNET_KEY = config('AI_FERNET_KEY', default='')

# Approximate conversion for operator-facing cost roll-up. Provider rate cards
# are quoted in USD/MToken; multiply by this to show NGN spend per reseller.
# Not used for billing — purely an operator dashboard estimate.
AI_USD_TO_NGN_RATE = config('AI_USD_TO_NGN_RATE', default='1500')
