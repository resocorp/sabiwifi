"""Voice services — call lifecycle, AVR droplet RPCs, tenant resolution.

All webhook/turn endpoints funnel through these helpers so the DB writes stay
consistent. Nothing here raises on external errors — the voice droplet is a
remote system and a failed round-trip must never bring the web worker down.
"""
from __future__ import annotations

import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone

from accounts.models import Reseller
from conversations.models import Conversation, Message
from voice.models import RoutingRule, VoiceCall, VoiceTenant, VoiceTurn

logger = logging.getLogger(__name__)


VOICE_SERVICE_URL = getattr(settings, 'VOICE_SERVICE_URL', 'http://127.0.0.1:8088')
VOICE_TIMEOUT = 5  # seconds — fire and forget, webhook reports completion


# ---------------------------------------------------------------------------
# Tenant / routing resolution
# ---------------------------------------------------------------------------

def resolve_tenant_by_did(did_e164: str) -> tuple[Reseller | None, RoutingRule | None]:
    """Look up the reseller + routing rule bound to a DID. Returns (None, None)
    if the DID isn't provisioned to any tenant."""
    try:
        rule = (RoutingRule.objects
                .select_related('reseller')
                .get(did_e164=did_e164, is_active=True))
    except RoutingRule.DoesNotExist:
        return None, None
    return rule.reseller, rule


def tenant_for_reseller(reseller: Reseller) -> VoiceTenant | None:
    try:
        return reseller.voice_tenant
    except VoiceTenant.DoesNotExist:
        return None


# ---------------------------------------------------------------------------
# Call lifecycle — called from the voice webhook
# ---------------------------------------------------------------------------

def record_call_started(*, call_id: str, reseller: Reseller,
                        direction: str, from_e164: str, to_e164: str,
                        metadata: dict | None = None) -> VoiceCall:
    """Idempotent: if call_id already exists, return the existing row."""
    call, created = VoiceCall.objects.get_or_create(
        call_id=call_id,
        defaults={
            'reseller': reseller,
            'direction': direction,
            'from_e164': from_e164,
            'to_e164': to_e164,
            'metadata': metadata or {},
            'status': VoiceCall.STATUS_RINGING,
        },
    )
    if not created:
        logger.info('voice.call_started duplicate call_id=%s — reusing row', call_id)
    return call


def mark_call_answered(call_id: str) -> VoiceCall | None:
    try:
        call = VoiceCall.objects.get(call_id=call_id)
    except VoiceCall.DoesNotExist:
        logger.warning('voice.answered for unknown call_id=%s', call_id)
        return None
    if call.answered_at is None:
        call.answered_at = timezone.now()
        call.status = VoiceCall.STATUS_IN_PROGRESS
        call.save(update_fields=['answered_at', 'status'])
    return call


def mark_call_completed(*, call_id: str,
                        duration_seconds: int | None = None,
                        recording_url: str = '',
                        failure_reason: str = '',
                        status: str = VoiceCall.STATUS_COMPLETED) -> VoiceCall | None:
    try:
        call = VoiceCall.objects.get(call_id=call_id)
    except VoiceCall.DoesNotExist:
        logger.warning('voice.completed for unknown call_id=%s', call_id)
        return None
    now = timezone.now()
    if call.ended_at is not None:
        return call
    call.ended_at = now
    call.status = status
    call.failure_reason = failure_reason[:255]
    if duration_seconds is not None:
        call.duration_seconds = duration_seconds
    elif call.answered_at:
        call.duration_seconds = int((now - call.answered_at).total_seconds())
    if recording_url:
        call.recording_url = recording_url[:200]
    call.save(update_fields=[
        'ended_at', 'status', 'failure_reason', 'duration_seconds',
        'recording_url',
    ])
    return call


# ---------------------------------------------------------------------------
# Per-turn logging — called from /api/ai/voice-turn/
# ---------------------------------------------------------------------------

def append_turn(*, call: VoiceCall, direction: str, text: str,
                agent_run_id: int | None = None,
                latency_ms: int | None = None,
                cost_ngn: Decimal | None = None) -> VoiceTurn:
    turn = VoiceTurn.objects.create(
        call=call,
        direction=direction,
        text=text[:8000],
        agent_run_id=agent_run_id,
        latency_ms=latency_ms,
        cost_ngn=cost_ngn or Decimal('0'),
    )
    # Keep the call-level transcript + cost roll-up in sync so the dashboard
    # and operator panel don't have to aggregate on every page load.
    prefix = 'Caller: ' if direction == VoiceTurn.DIRECTION_IN else 'Agent: '
    call.transcript = (call.transcript + f'\n{prefix}{text}').strip()[:60000]
    if cost_ngn:
        call.cost_ngn = (call.cost_ngn or Decimal('0')) + Decimal(str(cost_ngn))
    call.save(update_fields=['transcript', 'cost_ngn'])
    return turn


# ---------------------------------------------------------------------------
# Conversation helper — voice conversations are per-call, channel=VOICE
# ---------------------------------------------------------------------------

def ensure_voice_conversation(*, reseller: Reseller, call: VoiceCall,
                              contact_phone: str) -> Conversation:
    """Get or create the Conversation row for this voice call. Uses call_id
    as the external_thread_id so each call is a distinct thread (unlike WA,
    where an ongoing JID thread is one conversation across many messages).
    """
    convo, _ = Conversation.objects.get_or_create(
        reseller=reseller,
        channel=Conversation.CHANNEL_VOICE,
        external_thread_id=call.call_id,
        defaults={
            'contact_phone': contact_phone,
            'state': Conversation.STATE_OPEN,
            'kind': Conversation.KIND_CUSTOMER,
        },
    )
    # Attach to the VoiceCall lazily so dashboards can join from either side.
    if call.conversation_id != convo.pk:
        call.conversation = convo
        call.save(update_fields=['conversation'])
    return convo


def record_voice_message(*, conversation: Conversation, direction: str,
                         body: str, source: str = Message.SOURCE_CUSTOMER,
                         agent_run_id: int | None = None) -> Message:
    msg = Message.objects.create(
        conversation=conversation,
        direction=direction,
        body=body[:8000],
        source=source,
        agent_run_id=agent_run_id,
        delivery_status=(Message.DELIVERY_SENT if direction == Message.DIRECTION_OUT
                         else Message.DELIVERY_PENDING),
    )
    now = timezone.now()
    conversation.last_message_at = now
    if direction == Message.DIRECTION_IN:
        conversation.last_inbound_at = now
    else:
        conversation.last_outbound_at = now
    conversation.save(update_fields=[
        'last_message_at', 'last_inbound_at', 'last_outbound_at',
    ])
    return msg


# ---------------------------------------------------------------------------
# Outbound — Django → voice droplet
# ---------------------------------------------------------------------------

def send_voice_call(*, tenant_slug: str, to_phone: str, body: str,
                    purpose: str = 'tts', call_id_hint: str = '') -> bool:
    """Ask the voice droplet to place an outbound call and play `body`.

    purpose='tts'  → one-shot TTS then hang up (OTP, reminder).
    purpose='agent'→ bring up the AI agent for a conversational turn.

    Returns True if the droplet accepted the request. The call's actual
    outcome arrives via webhook.
    """
    api_key = getattr(settings, 'VOICE_API_KEY', '')
    try:
        resp = requests.post(
            f'{VOICE_SERVICE_URL}/call/outbound',
            json={
                'tenant_slug': tenant_slug,
                'to': to_phone,
                'body': body,
                'purpose': purpose,
                'call_id_hint': call_id_hint,
            },
            headers={'X-Voice-API-Key': api_key} if api_key else {},
            timeout=VOICE_TIMEOUT,
        )
        if resp.status_code in (200, 202):
            return True
        logger.warning('voice droplet rejected outbound %s: %s %s',
                       to_phone, resp.status_code, resp.text[:200])
        return False
    except requests.exceptions.RequestException as exc:
        logger.warning('voice droplet unreachable for %s: %s', to_phone, exc)
        return False
