"""
Core notification dispatch utility.

Public functions:
  notify_subscriber(subscriber, event_type, context)
      Send a transactional message to a subscriber.
  notify_reseller(reseller, event_type, context)
      Send an operational alert to the reseller themselves.
  notify_admin_contacts(reseller, event_type, context)
      Send to the reseller's admin contacts for the given event type.
  send_whatsapp(reseller_slug, phone, body) -> bool
      Low-level: POST to the Node WA service. Returns True on success.

All functions are fire-and-forget — errors are logged, never raised.
"""
import logging
import re

import requests
from django.conf import settings
from django.utils import timezone

from notifications.sms import get_sms_service

logger = logging.getLogger(__name__)

# Node WhatsApp service base URL (internal)
WA_SERVICE_URL = getattr(settings, 'WA_SERVICE_URL', 'http://127.0.0.1:3001')
WA_TIMEOUT = 5  # seconds — fire and forget


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

def _render(body, context):
    """Replace {{var}} placeholders with context values."""
    def replacer(match):
        key = match.group(1).strip()
        return str(context.get(key, match.group(0)))
    return re.sub(r'\{\{(\w+)\}\}', replacer, body)


def _get_template(reseller, event_type, channel='sms'):
    """
    Return (body, subject) for this event + channel. Tries the channel-specific
    reseller override first, falls back to any channel row the reseller has for
    this event, then the built-in default. Returns (None, '') if the matching
    template is explicitly disabled.
    """
    from notifications.models import NotificationTemplate
    qs = NotificationTemplate.objects.filter(reseller=reseller, event_type=event_type)
    tmpl = qs.filter(channel=channel).first() or qs.first()
    if tmpl is not None:
        if not tmpl.is_enabled:
            return None, ''
        return tmpl.body, tmpl.subject
    return NotificationTemplate.DEFAULT_BODIES.get(event_type), ''


def _ensure_config(reseller):
    """Get or create ResellerNotificationConfig for a reseller."""
    from notifications.models import ResellerNotificationConfig
    config, _ = ResellerNotificationConfig.objects.get_or_create(reseller=reseller)
    return config


def _ensure_prefs(subscriber):
    """Get or create SubscriberNotificationPrefs for a subscriber."""
    from notifications.models import SubscriberNotificationPrefs
    prefs, _ = SubscriberNotificationPrefs.objects.get_or_create(subscriber=subscriber)
    return prefs


# ---------------------------------------------------------------------------
# WhatsApp low-level send
# ---------------------------------------------------------------------------

def send_whatsapp(reseller_slug, phone, body):
    """
    POST to the Node WA service to queue a message for this reseller's session.
    Returns True if the service accepted the request.
    """
    try:
        resp = requests.post(
            f'{WA_SERVICE_URL}/send',
            json={'slug': reseller_slug, 'to': phone, 'message': body},
            timeout=WA_TIMEOUT,
        )
        if resp.status_code == 200:
            return True
        logger.warning(f'WA service rejected send to {phone}: {resp.status_code} {resp.text[:200]}')
        return False
    except requests.exceptions.RequestException as exc:
        logger.warning(f'WA service unreachable for {reseller_slug}: {exc}')
        return False


def _wa_connected(reseller):
    """True if the reseller has an active WhatsApp session."""
    try:
        return reseller.whatsapp_session.status == 'connected'
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Log helper
# ---------------------------------------------------------------------------

def _log(reseller, phone, channel, event_type, body,
         subscriber=None, broadcast=None, status='sending'):
    from notifications.models import NotificationLog
    return NotificationLog.objects.create(
        reseller=reseller,
        subscriber=subscriber,
        recipient_phone=phone,
        channel=channel,
        event_type=event_type,
        body=body,
        status=status,
        broadcast=broadcast,
    )


def _mark_sent(log_entry):
    log_entry.status = 'sent'
    log_entry.sent_at = timezone.now()
    log_entry.save(update_fields=['status', 'sent_at'])


def _mark_failed(log_entry, error=''):
    log_entry.status = 'failed'
    log_entry.error_detail = error
    log_entry.save(update_fields=['status', 'error_detail'])


# ---------------------------------------------------------------------------
# Channel dispatch
# ---------------------------------------------------------------------------

def _dispatch(reseller, phone, body, channel, event_type,
              subscriber=None, broadcast=None, email=None, subject=''):
    """
    Send via the requested channel(s), write a NotificationLog entry per send.
    channel: 'sms' | 'whatsapp' | 'both' | 'email'
    """
    sms_svc = get_sms_service()
    if channel == 'both':
        channels = ['whatsapp', 'sms']
    else:
        channels = [channel]

    for ch in channels:
        recipient = email if ch == 'email' else phone
        log = _log(reseller, recipient, ch, event_type, body,
                   subscriber=subscriber, broadcast=broadcast)
        if ch == 'whatsapp':
            if _wa_connected(reseller):
                ok = send_whatsapp(reseller.slug, phone, body)
                _mark_sent(log) if ok else _mark_failed(log, 'WA service error')
            else:
                _mark_failed(log, 'WhatsApp not connected')
        elif ch == 'email':
            if not email:
                _mark_failed(log, 'No email on recipient')
                continue
            from notifications.email import send_email
            ok = send_email(email, subject or event_type, body)
            _mark_sent(log) if ok else _mark_failed(log, 'Email send failed')
        elif ch == 'voice':
            # Voice is dispatched by the voice droplet asynchronously.
            # send_voice_call returns True on "accepted" — actual delivery
            # outcome arrives via /api/voice/webhook/ (call.completed) and
            # updates VoiceCall.status there, not here.
            try:
                from voice.services import send_voice_call
            except Exception as exc:
                _mark_failed(log, f'voice app unavailable: {exc}')
                continue
            ok = send_voice_call(
                tenant_slug=reseller.slug, to_phone=phone,
                body=body, purpose='tts',
            )
            _mark_sent(log) if ok else _mark_failed(log, 'Voice droplet error')
        else:  # sms
            ok = sms_svc.send_sms(phone, body)
            _mark_sent(log) if ok else _mark_failed(log, 'SMS send failed')


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Mapping from event_type to the config flag that guards it
_SUBSCRIBER_GUARDS = {
    'plan_expiry_3d': 'send_plan_expiry_3d',
    'plan_expiry_1d': 'send_plan_expiry_1d',
    'plan_expired':   'send_plan_expired',
    'welcome':        'send_welcome',
}

_RESELLER_GUARDS = {
    'new_subscriber':   'recv_new_subscriber',
    'payment_received': 'recv_payment',
    'router_offline':   'recv_router_offline',
    'router_recovered': 'recv_router_recovered',
}


def notify_subscriber(subscriber, event_type, context=None):
    """
    Send a transactional notification to a subscriber.
    Respects reseller config (is this event enabled?) and subscriber prefs
    (has the subscriber opted into transactional messages?).
    """
    if context is None:
        context = {}
    try:
        reseller = subscriber.reseller
        config = _ensure_config(reseller)
        prefs = _ensure_prefs(subscriber)

        if not prefs.transactional_enabled:
            return

        guard = _SUBSCRIBER_GUARDS.get(event_type)
        if guard and not getattr(config, guard, True):
            return

        channel = prefs.transactional_channel
        # If subscriber prefers WA but reseller config says sms, use sms
        if channel == 'whatsapp' and config.subscriber_channel == 'sms':
            channel = 'sms'
        elif config.subscriber_channel == 'both':
            channel = 'both'
        else:
            channel = config.subscriber_channel

        # Use the template for the primary channel (for 'both' we pick sms).
        template_channel = 'sms' if channel == 'both' else channel
        body_template, subject_template = _get_template(reseller, event_type, template_channel)
        if not body_template:
            return

        # Build full context — portal link uses reseller slug + subscriber serial
        link = f'https://app.sabiwifi.com/portal/account/?r={subscriber.reseller.slug}'
        full_context = {'name': subscriber.phone, 'link': link}
        full_context.update(context)

        body = _render(body_template, full_context)
        subject = _render(subject_template, full_context) if subject_template else ''

        _dispatch(reseller, subscriber.phone, body, channel, event_type,
                  subscriber=subscriber,
                  email=getattr(subscriber, 'email', '') or None,
                  subject=subject)

    except Exception as exc:
        logger.error(f'notify_subscriber error ({event_type} → {subscriber.phone}): {exc}')


def notify_reseller(reseller, event_type, context=None):
    """
    Send an operational alert to the reseller themselves.
    """
    if context is None:
        context = {}
    try:
        config = _ensure_config(reseller)

        guard = _RESELLER_GUARDS.get(event_type)
        if guard and not getattr(config, guard, True):
            return

        channel = config.reseller_channel
        template_channel = 'sms' if channel == 'both' else channel
        body_template, subject_template = _get_template(reseller, event_type, template_channel)
        if not body_template:
            return

        body = _render(body_template, context)
        subject = _render(subject_template, context) if subject_template else ''
        _dispatch(reseller, reseller.phone, body, channel, event_type,
                  email=getattr(reseller, 'email', '') or None, subject=subject)

    except Exception as exc:
        logger.error(f'notify_reseller error ({event_type} → {reseller.slug}): {exc}')


def notify_admin_contacts(reseller, event_type, context=None):
    """
    Send an alert to all admin contacts who have this event type enabled.
    """
    if context is None:
        context = {}
    try:
        contacts = reseller.admin_contacts.all()
        if not contacts.exists():
            return

        # Pick a single template per contact — use the contact's own channel.
        for contact in contacts:
            # Check per-contact guards
            if event_type in ('router_offline', 'router_recovered') and not contact.recv_router_alerts:
                continue
            if event_type == 'new_subscriber' and not contact.recv_new_subscriber:
                continue
            if event_type == 'payment_received' and not contact.recv_payment_summary:
                continue
            template_channel = 'sms' if contact.channel == 'both' else contact.channel
            body_template, subject_template = _get_template(
                reseller, event_type, template_channel,
            )
            if not body_template:
                continue
            body = _render(body_template, context)
            subject = _render(subject_template, context) if subject_template else ''
            _dispatch(reseller, contact.phone, body, contact.channel, event_type,
                      subject=subject)

    except Exception as exc:
        logger.error(f'notify_admin_contacts error ({event_type} → {reseller.slug}): {exc}')


def seed_default_templates(reseller):
    """
    Create default NotificationTemplate rows for a reseller.
    Called when a reseller first connects WhatsApp or saves notification settings.
    Only creates rows that don't already exist.
    """
    from notifications.models import NotificationTemplate
    for event_type, body in NotificationTemplate.DEFAULT_BODIES.items():
        NotificationTemplate.objects.get_or_create(
            reseller=reseller,
            event_type=event_type,
            defaults={'body': body, 'is_enabled': True},
        )
