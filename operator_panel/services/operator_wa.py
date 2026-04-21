"""
Operator WhatsApp session management.

Uses the same Baileys sidecar as reseller sessions, but keyed by the
reserved session id `__operator` (leading underscore can't collide with
slugify-generated reseller slugs).

Public helpers:
    get_status()            -> {'status','phone','qr'}
    connect()               -> start/refresh session (returns {'ok','status'})
    disconnect()            -> log out + clear DB flags
    send(phone, body)       -> queue a WhatsApp message (bool success)
    handle_webhook_connected(phone)    -> called by wa_webhook
    handle_webhook_disconnected()      -> called by wa_webhook
"""
import logging

import requests as http_requests
from django.conf import settings
from django.utils import timezone

from operator_panel.models import PlatformSettings

logger = logging.getLogger(__name__)

# Reserved session id — leading underscore is impossible from slugify()
OPERATOR_SESSION_KEY = '__operator'

WA_SERVICE_URL = getattr(settings, 'WA_SERVICE_URL', 'http://127.0.0.1:3001')
WA_TIMEOUT = 5


def _wa_post(path, payload=None):
    return http_requests.post(
        f'{WA_SERVICE_URL}{path}', json=payload or {}, timeout=WA_TIMEOUT,
    )


def _wa_get(path):
    return http_requests.get(f'{WA_SERVICE_URL}{path}', timeout=WA_TIMEOUT)


def get_status():
    """Fetch live status from Node sidecar and reconcile DB."""
    try:
        resp = _wa_get(f'/sessions/{OPERATOR_SESSION_KEY}/status')
        data = resp.json() if resp.status_code == 200 else {}
    except Exception as exc:
        logger.warning(f'Operator WA status unreachable: {exc}')
        data = {}

    ps = PlatformSettings.load()
    node_status = data.get('status', 'disconnected')
    connected = node_status == 'connected'
    if connected != ps.operator_wa_connected:
        ps.operator_wa_connected = connected
        if connected:
            ps.operator_wa_phone = data.get('phone', '')
        else:
            ps.operator_wa_phone = ''
        ps.save(update_fields=['operator_wa_connected', 'operator_wa_phone'])

    return {
        'status': node_status,
        'phone': ps.operator_wa_phone,
        'qr': data.get('qr'),
    }


def connect():
    """Start a session (generates QR)."""
    try:
        resp = _wa_post(f'/sessions/{OPERATOR_SESSION_KEY}/connect')
        if resp.status_code == 200:
            return {'ok': True, 'status': 'connecting'}
        return {'ok': False, 'error': 'WA service rejected request', 'code': resp.status_code}
    except Exception as exc:
        return {'ok': False, 'error': f'WA service unreachable: {exc}'}


def disconnect():
    """Disconnect session + clear DB flags regardless of sidecar availability."""
    try:
        _wa_post(f'/sessions/{OPERATOR_SESSION_KEY}/disconnect')
    except Exception:
        pass
    ps = PlatformSettings.load()
    ps.operator_wa_connected = False
    ps.operator_wa_phone = ''
    ps.save(update_fields=['operator_wa_connected', 'operator_wa_phone'])
    return {'ok': True}


def send(phone, body):
    """Queue a WhatsApp message on the operator session."""
    try:
        resp = http_requests.post(
            f'{WA_SERVICE_URL}/send',
            json={'slug': OPERATOR_SESSION_KEY, 'to': phone, 'message': body},
            timeout=WA_TIMEOUT,
        )
        if resp.status_code == 200:
            return True
        logger.warning(f'Operator WA send rejected: {resp.status_code} {resp.text[:200]}')
        return False
    except http_requests.exceptions.RequestException as exc:
        logger.warning(f'Operator WA service unreachable: {exc}')
        return False


def handle_webhook_connected(phone):
    ps = PlatformSettings.load()
    ps.operator_wa_connected = True
    ps.operator_wa_phone = phone or ''
    ps.save(update_fields=['operator_wa_connected', 'operator_wa_phone'])
    logger.info(f'Operator WA connected as {phone}')


def handle_webhook_disconnected():
    ps = PlatformSettings.load()
    ps.operator_wa_connected = False
    ps.operator_wa_phone = ''
    ps.save(update_fields=['operator_wa_connected', 'operator_wa_phone'])
    logger.info('Operator WA disconnected')
