"""
Operator-side notification dispatcher.

Reads PlatformSettings (channel + toggles + recipient phones) and sends
an event to all configured recipients via SMS, WhatsApp, or both.

Event templates live in ``TEMPLATES`` below — keep them short (SMS-friendly).
Placeholders use ``{{var}}`` syntax (same as reseller templates).

Usage:
    from operator_panel.services.notify_operator import notify
    notify('new_order', {'reference': 'ORD123', 'amount': '25000', 'customer': 'John'})

Fire-and-forget: errors are logged, never raised.
"""
import logging
import re

from django.utils import timezone

logger = logging.getLogger(__name__)


# Event → toggle-field on PlatformSettings
TOGGLE_MAP = {
    'new_reseller':           'notify_on_new_reseller',
    'reseller_activated':     'notify_on_reseller_activated',
    'new_order':              'notify_on_new_order',
    'router_offline':         'notify_on_router_offline',
    'router_recovered':       'notify_on_router_recovered',
    'payment_failure':        'notify_on_payment_failure',
    'payment_failure_spike':  'notify_on_payment_failure_spike',
    'daily_summary':          'notify_daily_summary',
}


TEMPLATES = {
    'new_reseller':
        'SabiWiFi: New partner signup — {{name}} ({{phone}}). '
        'Review in admin.',
    'reseller_activated':
        'SabiWiFi: Partner "{{name}}" activated. '
        'Paystack subaccount: {{subaccount}}.',
    'new_order':
        'SabiWiFi shop: New order {{reference}} — ₦{{amount}} from {{customer}} ({{phone}}). '
        'Mark as processing in admin.',
    'router_offline':
        'SabiWiFi: Router {{serial}} ({{partner}}) went offline. Last seen {{last_seen}}.',
    'router_recovered':
        'SabiWiFi: Router {{serial}} ({{partner}}) is back online.',
    'payment_failure':
        'SabiWiFi: Payment failed — {{reference}} ₦{{amount}} ({{partner}}). Reason: {{reason}}.',
    'payment_failure_spike':
        'SabiWiFi ALERT: {{count}} payment failures in last 10 min. '
        'Check Paystack status.',
    'daily_summary':
        'SabiWiFi daily: ₦{{revenue}} revenue, {{orders}} orders, {{new_subs}} new subs, '
        '{{offline}} routers offline.',
}


def _render(body, context):
    def replacer(match):
        key = match.group(1).strip()
        return str(context.get(key, match.group(0)))
    return re.sub(r'\{\{(\w+)\}\}', replacer, body or '')


def notify(event_type, context=None):
    """
    Dispatch an operator notification.
    Returns a summary dict: {'sent': n, 'failed': n, 'skipped': bool, 'reason': str}.
    """
    from operator_panel.models import PlatformSettings
    from notifications.models import NotificationLog
    from notifications.sms import get_sms_service
    from operator_panel.services import operator_wa

    ctx = context or {}
    summary = {'sent': 0, 'failed': 0, 'skipped': False, 'reason': ''}

    toggle = TOGGLE_MAP.get(event_type)
    if not toggle:
        summary['skipped'] = True
        summary['reason'] = f'unknown event: {event_type}'
        logger.warning(f'notify_operator: unknown event_type {event_type}')
        return summary

    ps = PlatformSettings.load()

    if not getattr(ps, toggle, False):
        summary['skipped'] = True
        summary['reason'] = f'toggle off: {toggle}'
        return summary

    phones = ps.notification_phones or []
    if not phones:
        summary['skipped'] = True
        summary['reason'] = 'no recipients configured'
        return summary

    body = _render(TEMPLATES.get(event_type, '{{event}}: {{message}}'), ctx)
    channel = ps.operator_notification_channel  # sms / whatsapp / both

    sms = get_sms_service()

    for phone in phones:
        # WhatsApp attempt
        wa_ok = False
        if channel in ('whatsapp', 'both') and ps.operator_wa_connected:
            log = NotificationLog.objects.create(
                reseller=None, recipient_phone=phone, channel='whatsapp',
                event_type=f'operator.{event_type}', body=body, status='sending',
            )
            wa_ok = operator_wa.send(phone, body)
            if wa_ok:
                # sidecar webhook will flip status to sent on confirmation;
                # mark optimistically so it's not stuck at 'sending' forever
                log.status = 'queued'
                log.save(update_fields=['status'])
                summary['sent'] += 1
            else:
                log.status = 'failed'
                log.error_detail = 'WA send returned false'
                log.sent_at = timezone.now()
                log.save(update_fields=['status', 'error_detail', 'sent_at'])

        # SMS attempt — always if channel is sms, or fallback if WA failed and channel='both'
        send_sms_now = (channel == 'sms') or (channel == 'both' and not wa_ok)
        if send_sms_now:
            log = NotificationLog.objects.create(
                reseller=None, recipient_phone=phone, channel='sms',
                event_type=f'operator.{event_type}', body=body, status='sending',
            )
            ok = sms.send_sms(phone, body)
            log.status = 'sent' if ok else 'failed'
            log.sent_at = timezone.now()
            log.save(update_fields=['status', 'sent_at'])
            if ok:
                summary['sent'] += 1
            else:
                summary['failed'] += 1

    logger.info(f'notify_operator[{event_type}] {summary}')
    return summary
