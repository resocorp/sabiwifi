"""Fernet-based symmetric encryption for at-rest AI provider API keys.

The platform holds the single master key in settings.AI_FERNET_KEY (read from
.env). Rotating the key requires decrypting with the old key and re-encrypting
with the new one — handled out-of-band by a management command.
"""
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = getattr(settings, 'AI_FERNET_KEY', '') or ''
    if not key:
        raise ImproperlyConfigured(
            'AI_FERNET_KEY is not set. Generate one with '
            '`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` '
            'and add to .env.'
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ''
    return _fernet().encrypt(plaintext.encode('utf-8')).decode('ascii')


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ''
    try:
        return _fernet().decrypt(ciphertext.encode('ascii')).decode('utf-8')
    except InvalidToken:
        return ''
