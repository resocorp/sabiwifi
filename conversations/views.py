"""
Conversation API views.

Covers the inbound-WhatsApp webhook (sidecar → Django) and dashboard-facing
endpoints the reseller inbox uses for listing threads, viewing messages,
and posting human replies.
"""
import logging
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from accounts.models import Reseller
from accounts.permissions import scope_queryset, effective_reseller, effective_role, ROLE_FIELD_TECH
from conversations.models import Conversation, Message
from conversations.services import record_inbound_message, record_outbound_message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inbound webhook (WA sidecar → Django)
# ---------------------------------------------------------------------------

@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def inbound_wa(request):
    """
    Receive inbound WhatsApp messages forwarded from the Node sidecar.

    Payload:
      { event: 'message_received', slug, from_jid, from_phone, body,
        attachments, external_message_id, timestamp }
    """
    api_key = getattr(settings, 'WA_API_KEY', '')
    if api_key and request.headers.get('X-WA-API-Key') != api_key:
        return Response({'error': 'Forbidden'}, status=403)

    if request.data.get('event') != 'message_received':
        return Response({'error': 'Unsupported event'}, status=400)

    slug = request.data.get('slug', '')
    reseller = Reseller.objects.filter(slug=slug).first()
    if not reseller:
        logger.warning(f'inbound_wa: unknown reseller slug {slug!r}')
        return Response({'error': 'Unknown reseller'}, status=404)

    from_jid = request.data.get('from_jid', '')
    from_phone = request.data.get('from_phone', '')
    display_name = (request.data.get('display_name') or '').strip()
    body = request.data.get('body', '') or ''
    attachments = request.data.get('attachments', []) or []
    external_id = request.data.get('external_message_id', '') or ''
    ts_epoch = request.data.get('timestamp')

    if not from_jid:
        return Response({'error': 'from_jid required'}, status=400)

    ts = None
    if ts_epoch:
        try:
            ts = datetime.fromtimestamp(int(ts_epoch), tz=dt_timezone.utc)
        except (TypeError, ValueError):
            ts = None

    msg = record_inbound_message(
        reseller=reseller,
        channel=Conversation.CHANNEL_WHATSAPP,
        external_thread_id=from_jid,
        body=body,
        attachments=attachments,
        external_message_id=external_id,
        sender_phone=from_phone,
        display_name=display_name,
        timestamp=ts,
    )

    if msg is None:
        return Response({'ok': True, 'deduped': True})
    return Response({'ok': True, 'message_id': msg.pk, 'conversation_id': msg.conversation_id})


# ---------------------------------------------------------------------------
# Dashboard API (reseller-authenticated)
# ---------------------------------------------------------------------------

def _conversation_for_reseller(pk, reseller):
    return get_object_or_404(Conversation, pk=pk, reseller=reseller)


def _convos_for_user(request):
    """Returns the Conversation queryset visible to the authenticated user.
    Field techs see only tech-kind conversations assigned to them."""
    return scope_queryset(Conversation.objects.all(), request.user)


def _serialise_conversation_row(c):
    last_body = ''
    last = c.messages.order_by('-created_at').first()
    if last:
        last_body = last.body or ('[attachment]' if last.attachments else '')
    contact = c.display_contact
    # For tech-kind conversations, show the staff name as the contact label
    if c.kind == Conversation.KIND_TECH and c.assigned_staff_id:
        contact = c.assigned_staff.name
    return {
        'id': c.pk,
        'channel': c.channel,
        'kind': c.kind,
        'contact': contact,
        'contact_phone': c.contact_phone,
        'state': c.state,
        'unread_count': c.unread_count,
        'last_message_at': c.last_message_at.isoformat() if c.last_message_at else None,
        'last_body': last_body[:140],
        'subscriber_id': c.subscriber_id,
        'lead_id': c.lead_id,
        'staff_id': c.assigned_staff_id,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def conversation_list(request):
    qs = _convos_for_user(request).prefetch_related('messages')
    channel = request.GET.get('channel')
    if channel:
        qs = qs.filter(channel=channel)
    state = request.GET.get('state')
    if state:
        qs = qs.filter(state=state)
    kind = request.GET.get('kind')
    if kind:
        qs = qs.filter(kind=kind)
    qs = qs[:100]
    return Response([_serialise_conversation_row(c) for c in qs])


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def conversation_detail(request, pk):
    reseller = effective_reseller(request.user)
    convo = get_object_or_404(_convos_for_user(request), pk=pk)

    messages = list(convo.messages.order_by('created_at').values(
        'id', 'direction', 'body', 'attachments', 'source',
        'delivery_status', 'delivery_error', 'is_draft', 'agent_run_id',
        'created_at', 'sent_at',
    ))
    for m in messages:
        m['created_at'] = m['created_at'].isoformat()
        if m['sent_at']:
            m['sent_at'] = m['sent_at'].isoformat()

    # Clear unread on open
    if convo.unread_count:
        convo.unread_count = 0
        convo.save(update_fields=['unread_count'])

    contact = convo.display_contact
    if convo.kind == Conversation.KIND_TECH and convo.assigned_staff_id:
        contact = convo.assigned_staff.name
    return Response({
        'conversation': {
            'id': convo.pk,
            'channel': convo.channel,
            'kind': convo.kind,
            'contact': contact,
            'contact_phone': convo.contact_phone,
            'state': convo.state,
            'ai_enabled': convo.ai_enabled,
            'subscriber_id': convo.subscriber_id,
            'lead_id': convo.lead_id,
            'staff_id': convo.assigned_staff_id,
        },
        'messages': messages,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def conversation_reply(request, pk):
    """Human reply from the inbox. Records + triggers the wire send."""
    reseller = effective_reseller(request.user)
    convo = get_object_or_404(_convos_for_user(request), pk=pk)

    body = (request.data.get('body') or '').strip()
    if not body:
        return Response({'error': 'Body required'}, status=400)

    msg = record_outbound_message(
        conversation=convo, body=body, source=Message.SOURCE_HUMAN,
    )

    # Fire-and-forget wire send. For now only WhatsApp is wired; SMS/email
    # land when those channels start producing inbound too.
    if convo.channel == Conversation.CHANNEL_WHATSAPP:
        from notifications.notify import send_whatsapp
        phone = convo.contact_phone or convo.external_thread_id.split('@')[0]
        ok = send_whatsapp(reseller.slug, phone, body)
        msg.delivery_status = (
            Message.DELIVERY_QUEUED if ok else Message.DELIVERY_FAILED
        )
        msg.save(update_fields=['delivery_status'])

    return Response({'ok': True, 'message_id': msg.pk})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def conversation_resolve(request, pk):
    reseller = effective_reseller(request.user)
    convo = get_object_or_404(_convos_for_user(request), pk=pk)
    convo.state = Conversation.STATE_RESOLVED
    convo.save(update_fields=['state'])
    return Response({'ok': True})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def conversation_toggle_ai(request, pk):
    reseller = effective_reseller(request.user)
    convo = get_object_or_404(_convos_for_user(request), pk=pk)
    convo.ai_enabled = not convo.ai_enabled
    convo.save(update_fields=['ai_enabled'])
    return Response({'ok': True, 'ai_enabled': convo.ai_enabled})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def conversation_send_draft(request, pk, message_id):
    """Human approves an AI draft. Optional `body` override lets the agent
    pre-fill a reply that the human edits before sending."""
    reseller = effective_reseller(request.user)
    convo = get_object_or_404(_convos_for_user(request), pk=pk)
    try:
        msg = convo.messages.get(pk=message_id, is_draft=True)
    except Message.DoesNotExist:
        return Response({'error': 'draft not found'}, status=404)

    new_body = (request.data.get('body') or '').strip()
    if new_body and new_body != msg.body:
        msg.body = new_body
    msg.is_draft = False

    # Deliver on the wire
    if convo.channel == Conversation.CHANNEL_WHATSAPP:
        from notifications.notify import send_whatsapp
        phone = convo.contact_phone or convo.external_thread_id.split('@')[0]
        ok = send_whatsapp(reseller.slug, phone, msg.body)
        msg.delivery_status = (
            Message.DELIVERY_QUEUED if ok else Message.DELIVERY_FAILED
        )
    else:
        msg.delivery_status = Message.DELIVERY_QUEUED
    msg.save(update_fields=['body', 'is_draft', 'delivery_status'])

    convo.state = Conversation.STATE_OPEN
    convo.save(update_fields=['state', 'updated_at'])
    return Response({'ok': True, 'message_id': msg.pk})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def conversation_discard_draft(request, pk, message_id):
    reseller = effective_reseller(request.user)
    convo = get_object_or_404(_convos_for_user(request), pk=pk)
    convo.messages.filter(pk=message_id, is_draft=True).delete()
    if not convo.messages.filter(is_draft=True).exists():
        convo.state = Conversation.STATE_OPEN
        convo.save(update_fields=['state', 'updated_at'])
    return Response({'ok': True})
