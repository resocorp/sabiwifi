"""
Development settings for SabiWiFi.
"""
from .base import *

DEBUG = True

# Allow all hosts in development
ALLOWED_HOSTS = ['*']

# CORS — allow all in development
CORS_ALLOW_ALL_ORIGINS = True

# Disable throttling in development
REST_FRAMEWORK['DEFAULT_THROTTLE_CLASSES'] = []
