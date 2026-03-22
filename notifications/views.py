"""
Notification API views:
  - wa_webhook        receives events back from the Node WA service
  - wa_session_status poll for QR / connection status
  - wa_connect        start a session
  - wa_disconnect     log out
  - wa_send_test      send a test message from the dashboard
  - notification_config_save   save reseller notification preferences
  - admin_contacts_*  CRUD for admin contacts
  - broadcast_*       create, list, progress, cancel broadcasts
  - subscriber_prefs_save  subscriber saves their own prefs
"""
import logging

from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

import requests as http_requests
from django.conf import settings

from notifications.models import (
    WhatsappSession, ResellerNotificationConfig, ResellerAdminContact,
    NotificationTemplate, SubscriberNotificationPrefs, Broadcast, NotificationLog,
)
from notifications.notify import seed_default_templates, send_whatsapp

logger = logging.getLogger(__name__)

WA_SERVICE_URL = getattr(settings, 'WA_SERVICE_URL', 'http://127.0.0.1:3001')
WA_TIMEOUT = 5


def _wa_post(path, payload=None):
    resp = http_requests.post(
        f'{WA_SERVICE_URL}{path}', json=payload or {}, timeout=WA_TIMEOUT
    )
    return resp


def _wa_get(path):
    resp = http_requests.get(f'{WA_SERVICE_URL}{path}', timeout=WA_TIMEOUT)
    return resp


# ---------------------------------------------------------------------------
# Webhook from Node service → Django
# ---------------------------------------------------------------------------

@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def wa_webhook(request):
    """
    Receives events from the Node WA service.
    Verifies optional X-WA-API-Key header.
    """
    api_key = getattr(settings, 'WA_API_KEY', '')
    if api_key and request.headers.get('X-WA-API-Key') != api_key:
        return Response({'error': 'Forbidden'}, status=403)

    event = request.data.get('event')
    slug = request.data.get('slug', '')

    if event == 'connected':
        phone = request.data.get('phone', '')
        WhatsappSession.objects.update_or_create(
            reseller__slug=slug,
            defaults={},
        )
        try:
            from accounts.models import Reseller
            reseller = Reseller.objects.get(slug=slug)
            sess, _ = WhatsappSession.objects.get_or_create(reseller=reseller)
            sess.status = 'connected'
            sess.wa_phone = phone
            sess.connected_at = timezone.now()
            sess.disconnected_at = None
            sess.save()
            # Seed default templates on first connect
            seed_default_templates(reseller)
            logger.info(f'WA connected: {slug} as {phone}')
        except Exception as exc:
            logger.error(f'wa_webhook connected error: {exc}')

    elif event == 'disconnected':
        try:
            from accounts.models import Reseller
            reseller = Reseller.objects.get(slug=slug)
            sess, _ = WhatsappSession.objects.get_or_create(reseller=reseller)
            sess.status = 'disconnected'
            sess.wa_phone = ''
            sess.disconnected_at = timezone.now()
            sess.save()
            logger.info(f'WA disconnected: {slug}')
        except Exception as exc:
            logger.error(f'wa_webhook disconnected error: {exc}')

    elif event in ('sent', 'failed'):
        log_id = request.data.get('log_id')
        if log_id:
            try:
                log = NotificationLog.objects.get(pk=log_id)
                if event == 'sent':
                    log.status = 'sent'
                    log.sent_at = timezone.now()
                else:
                    log.status = 'failed'
                    log.error_detail = request.data.get('error', '')
                log.save(update_fields=['status', 'sent_at', 'error_detail'])
                # Update broadcast counters
                if log.broadcast_id:
                    field = 'sent_count' if event == 'sent' else 'failed_count'
                    Broadcast.objects.filter(pk=log.broadcast_id).update(
                        **{field: getattr(
                            Broadcast.objects.get(pk=log.broadcast_id), field
                        ) + 1}
                    )
            except NotificationLog.DoesNotExist:
                pass
            except Exception as exc:
                logger.error(f'wa_webhook log update error: {exc}')

    return Response({'ok': True})


# ---------------------------------------------------------------------------
# WA session management (reseller-authenticated)
# ---------------------------------------------------------------------------

@api_view(['GET'])
def wa_status(request):
    """Poll for QR code and connection status."""
    reseller = request.user.reseller
    try:
        resp = _wa_get(f'/sessions/{reseller.slug}/status')
        node_data = resp.json() if resp.status_code == 200 else {}
    except Exception:
        node_data = {}

    # Sync DB state if needed
    sess, _ = WhatsappSession.objects.get_or_create(reseller=reseller)
    node_status = node_data.get('status', 'disconnected')
    if node_status != sess.status:
        # Node is authoritative while the service is running
        sess.status = node_status
        if node_status == 'connected':
            sess.wa_phone = node_data.get('phone', '')
        sess.save(update_fields=['status', 'wa_phone'])

    return Response({
        'status': sess.status,
        'phone': sess.wa_phone,
        'qr': node_data.get('qr'),  # base64 data URL or null
    })


@api_view(['POST'])
def wa_connect(request):
    """Start a WhatsApp session (generates QR)."""
    reseller = request.user.reseller
    try:
        resp = _wa_post(f'/sessions/{reseller.slug}/connect')
        if resp.status_code == 200:
            sess, _ = WhatsappSession.objects.get_or_create(reseller=reseller)
            sess.status = 'connecting'
            sess.save(update_fields=['status'])
            return Response({'ok': True, 'status': 'connecting'})
        return Response({'error': 'WA service error'}, status=502)
    except Exception as exc:
        return Response({'error': f'WA service unreachable: {exc}'}, status=503)


@api_view(['POST'])
def wa_disconnect(request):
    """Disconnect and log out of WhatsApp."""
    reseller = request.user.reseller
    try:
        _wa_post(f'/sessions/{reseller.slug}/disconnect')
    except Exception:
        pass  # Update DB regardless
    sess, _ = WhatsappSession.objects.get_or_create(reseller=reseller)
    sess.status = 'disconnected'
    sess.wa_phone = ''
    sess.disconnected_at = timezone.now()
    sess.save()
    return Response({'ok': True})


@api_view(['POST'])
def wa_send_test(request):
    """Send a test message to verify the connection."""
    reseller = request.user.reseller
    phone = request.data.get('phone', reseller.phone)
    if not phone:
        return Response({'error': 'Phone number required.'}, status=400)
    ok = send_whatsapp(reseller.slug, phone, '✅ SabiWiFi WhatsApp connection test successful!')
    if ok:
        return Response({'ok': True, 'message': f'Test message queued to {phone}.'})
    return Response({'error': 'Send failed. Is WhatsApp connected?'}, status=503)


# ---------------------------------------------------------------------------
# Notification config
# ---------------------------------------------------------------------------

@api_view(['GET'])
def notification_config(request):
    """Return current notification config + templates for the reseller."""
    reseller = request.user.reseller
    config, _ = ResellerNotificationConfig.objects.get_or_create(reseller=reseller)
    seed_default_templates(reseller)

    templates = {
        t.event_type: {'body': t.body, 'is_enabled': t.is_enabled}
        for t in NotificationTemplate.objects.filter(reseller=reseller)
    }
    contacts = list(ResellerAdminContact.objects.filter(reseller=reseller).values(
        'id', 'name', 'phone', 'channel',
        'recv_router_alerts', 'recv_new_subscriber', 'recv_payment_summary',
    ))
    sess, _ = WhatsappSession.objects.get_or_create(reseller=reseller)

    return Response({
        'config': {
            'send_plan_expiry_3d': config.send_plan_expiry_3d,
            'send_plan_expiry_1d': config.send_plan_expiry_1d,
            'send_plan_expired': config.send_plan_expired,
            'send_welcome': config.send_welcome,
            'subscriber_channel': config.subscriber_channel,
            'recv_new_subscriber': config.recv_new_subscriber,
            'recv_payment': config.recv_payment,
            'recv_router_offline': config.recv_router_offline,
            'recv_router_recovered': config.recv_router_recovered,
            'reseller_channel': config.reseller_channel,
        },
        'templates': templates,
        'contacts': contacts,
        'wa': {'status': sess.status, 'phone': sess.wa_phone},
    })


@api_view(['POST'])
def notification_config_save(request):
    """Save notification config and templates."""
    reseller = request.user.reseller
    config, _ = ResellerNotificationConfig.objects.get_or_create(reseller=reseller)

    bool_fields = [
        'send_plan_expiry_3d', 'send_plan_expiry_1d', 'send_plan_expired',
        'send_welcome', 'recv_new_subscriber', 'recv_payment',
        'recv_router_offline', 'recv_router_recovered',
    ]
    for field in bool_fields:
        if field in request.data:
            setattr(config, field, bool(request.data[field]))

    for ch_field in ('subscriber_channel', 'reseller_channel'):
        val = request.data.get(ch_field)
        if val in ('whatsapp', 'sms', 'both'):
            setattr(config, ch_field, val)

    config.save()

    # Save templates
    for event_type, tmpl_data in request.data.get('templates', {}).items():
        NotificationTemplate.objects.update_or_create(
            reseller=reseller, event_type=event_type,
            defaults={
                'body': tmpl_data.get('body', ''),
                'is_enabled': tmpl_data.get('is_enabled', True),
            }
        )

    return Response({'ok': True})


# ---------------------------------------------------------------------------
# Admin contacts
# ---------------------------------------------------------------------------

@api_view(['POST'])
def admin_contact_add(request):
    reseller = request.user.reseller
    name = request.data.get('name', '').strip()
    phone = request.data.get('phone', '').strip()
    if not name or not phone:
        return Response({'error': 'Name and phone required.'}, status=400)
    contact = ResellerAdminContact.objects.create(
        reseller=reseller,
        name=name,
        phone=phone,
        channel=request.data.get('channel', 'sms'),
        recv_router_alerts=request.data.get('recv_router_alerts', True),
        recv_new_subscriber=request.data.get('recv_new_subscriber', False),
        recv_payment_summary=request.data.get('recv_payment_summary', False),
    )
    return Response({'id': contact.pk, 'name': contact.name, 'phone': contact.phone})


@api_view(['DELETE'])
def admin_contact_delete(request, pk):
    reseller = request.user.reseller
    ResellerAdminContact.objects.filter(pk=pk, reseller=reseller).delete()
    return Response({'ok': True})


# ---------------------------------------------------------------------------
# Broadcasts
# ---------------------------------------------------------------------------

@api_view(['POST'])
def broadcast_create(request):
    """Create and queue a broadcast."""
    reseller = request.user.reseller
    body = request.data.get('body', '').strip()
    btype = request.data.get('type', 'alert')
    channel = request.data.get('channel', 'sms')

    if not body:
        return Response({'error': 'Message body required.'}, status=400)
    if btype not in ('alert', 'promo', 'marketing'):
        return Response({'error': 'Invalid type.'}, status=400)

    # Count eligible recipients
    from accounts.models import Subscriber
    from notifications.models import SubscriberNotificationPrefs

    all_subs = Subscriber.objects.filter(reseller=reseller)
    pref_field = {
        'alert': 'receive_alerts',
        'promo': 'receive_promos',
        'marketing': 'receive_marketing',
    }[btype]

    # Subscribers with prefs opted in + those without prefs (default for alerts=True, rest=False)
    opted_in_ids = set(
        SubscriberNotificationPrefs.objects.filter(
            subscriber__reseller=reseller,
            **{pref_field: True}
        ).values_list('subscriber_id', flat=True)
    )
    no_prefs_ids = set(
        all_subs.exclude(
            notification_prefs__isnull=False
        ).values_list('id', flat=True)
    )
    # For alerts, no-prefs subscribers are included (default True)
    # For promos/marketing, no-prefs are excluded (default False / opt-in)
    if btype == 'alert':
        recipient_ids = opted_in_ids | no_prefs_ids
    else:
        recipient_ids = opted_in_ids

    broadcast = Broadcast.objects.create(
        reseller=reseller,
        type=btype,
        body=body,
        channel=channel,
        status='queued',
        total_recipients=len(recipient_ids),
    )
    return Response({
        'id': broadcast.pk,
        'total_recipients': broadcast.total_recipients,
        'status': broadcast.status,
    })


@api_view(['GET'])
def broadcast_list(request):
    reseller = request.user.reseller
    broadcasts = Broadcast.objects.filter(reseller=reseller)[:20]
    return Response([{
        'id': b.pk,
        'type': b.type,
        'body': b.body[:80] + ('...' if len(b.body) > 80 else ''),
        'channel': b.channel,
        'status': b.status,
        'total_recipients': b.total_recipients,
        'sent_count': b.sent_count,
        'failed_count': b.failed_count,
        'progress_pct': b.progress_pct,
        'created_at': b.created_at.strftime('%d %b %Y %H:%M'),
    } for b in broadcasts])


@api_view(['GET'])
def broadcast_progress(request, pk):
    reseller = request.user.reseller
    try:
        b = Broadcast.objects.get(pk=pk, reseller=reseller)
    except Broadcast.DoesNotExist:
        return Response({'error': 'Not found.'}, status=404)
    return Response({
        'status': b.status,
        'total_recipients': b.total_recipients,
        'sent_count': b.sent_count,
        'failed_count': b.failed_count,
        'progress_pct': b.progress_pct,
    })


@api_view(['POST'])
def broadcast_cancel(request, pk):
    reseller = request.user.reseller
    Broadcast.objects.filter(pk=pk, reseller=reseller, status='queued').update(status='cancelled')
    return Response({'ok': True})


# ---------------------------------------------------------------------------
# Subscriber notification prefs (portal-facing, token-authenticated)
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def subscriber_notification_prefs(request):
    token = request.data.get('token') or request.GET.get('token', '')
    if not token:
        return Response({'error': 'Token required.'}, status=400)

    from accounts.models import Subscriber
    try:
        subscriber = Subscriber.objects.get(auth_token=token)
    except Subscriber.DoesNotExist:
        return Response({'error': 'Invalid token.'}, status=401)

    prefs, _ = SubscriberNotificationPrefs.objects.get_or_create(subscriber=subscriber)

    if request.method == 'GET':
        return Response({
            'transactional_enabled': prefs.transactional_enabled,
            'transactional_channel': prefs.transactional_channel,
            'receive_alerts': prefs.receive_alerts,
            'receive_promos': prefs.receive_promos,
            'receive_marketing': prefs.receive_marketing,
        })

    # POST — save preferences
    for field in ('transactional_enabled', 'receive_alerts', 'receive_promos', 'receive_marketing'):
        if field in request.data:
            setattr(prefs, field, bool(request.data[field]))
    if request.data.get('transactional_channel') in ('sms', 'whatsapp'):
        prefs.transactional_channel = request.data['transactional_channel']
    prefs.save()
    return Response({'ok': True})


# ---------------------------------------------------------------------------
# Notification log (dashboard)
# ---------------------------------------------------------------------------

@api_view(['GET'])
def notification_log(request):
    reseller = request.user.reseller
    logs = NotificationLog.objects.filter(reseller=reseller).select_related('subscriber')[:50]
    return Response([{
        'phone': log.recipient_phone,
        'channel': log.channel,
        'event_type': log.event_type,
        'status': log.status,
        'body': log.body[:80] + ('...' if len(log.body) > 80 else ''),
        'created_at': log.created_at.strftime('%d %b %Y %H:%M'),
    } for log in logs])
