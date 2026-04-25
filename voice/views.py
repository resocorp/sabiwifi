"""Voice API views.

Two endpoints:
  - POST /api/voice/webhook/         called by the AVR droplet on each
                                     call-lifecycle event (started / answered /
                                     completed / recording.ready / dtmf).
  - POST /api/ai/voice-turn/         called by the avr-llm-sabiwifi bridge
                                     container at every caller utterance;
                                     returns the text the droplet should TTS.

Both require a matching X-Voice-API-Key header (same value, set in .env).

Phase 2 MVP for /voice-turn/: direct LLM roundtrip, NO tool calls. The
existing CustomerAgent toolset is wired up in Phase 2.5 via a VoiceAgent
runner that reuses ai.agents.runner.AgentRunner with a trimmed tool schema.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from accounts.models import Reseller
from ai.models import AIAgentRun, AIPromptVersion, ResellerAIConfig
from conversations.models import Message
from voice import services as svc
from voice.models import RoutingRule, VoiceCall, VoiceTurn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _api_key_ok(request) -> bool:
    expected = getattr(settings, 'VOICE_API_KEY', '')
    if not expected:
        # Fail closed in production. An empty key in prod is an operator
        # mistake — we refuse rather than open the webhook to the world.
        # Dev (DEBUG=True) keeps the no-key convenience so local tests work.
        if not settings.DEBUG:
            logger.error('VOICE_API_KEY is empty but DEBUG=False — rejecting request')
            return False
        return True
    return request.headers.get('X-Voice-API-Key') == expected


# ---------------------------------------------------------------------------
# Droplet → Django: call lifecycle webhook
# ---------------------------------------------------------------------------

@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def voice_webhook(request):
    """Handles call.started / call.answered / call.completed / recording.ready /
    dtmf events posted by the AVR droplet."""
    if not _api_key_ok(request):
        return Response({'error': 'forbidden'}, status=403)

    event = request.data.get('event', '')
    call_id = request.data.get('call_id', '')
    if not event or not call_id:
        return Response({'error': 'event + call_id required'}, status=400)

    try:
        return _dispatch_event(request.data, event, call_id)
    except Exception as exc:  # never 500 on the droplet — log + acknowledge
        logger.exception('voice_webhook crashed event=%s call=%s', event, call_id)
        return Response({'error': f'server: {type(exc).__name__}'}, status=500)


def _dispatch_event(data: dict, event: str, call_id: str) -> Response:
    if event == 'call.started':
        to_e164 = data.get('to', '') or data.get('to_e164', '')
        from_e164 = data.get('from', '') or data.get('from_e164', '')
        direction = data.get('direction', VoiceCall.DIRECTION_IN)

        # For inbound calls the droplet resolves DID → reseller via our
        # RoutingRule table before ringing. It passes the rule back here so
        # we don't re-lookup. Outbound calls carry reseller_slug directly.
        reseller_slug = data.get('reseller_slug') or ''
        reseller = None
        if reseller_slug:
            reseller = Reseller.objects.filter(slug=reseller_slug).first()
        if reseller is None and direction == VoiceCall.DIRECTION_IN and to_e164:
            reseller, _rule = svc.resolve_tenant_by_did(to_e164)
        if reseller is None:
            logger.warning('voice.call.started: no tenant resolved call=%s to=%s',
                           call_id, to_e164)
            return Response({'error': 'no tenant'}, status=404)

        call = svc.record_call_started(
            call_id=call_id, reseller=reseller,
            direction=direction, from_e164=from_e164, to_e164=to_e164,
            metadata=data.get('metadata') or {},
        )
        return Response({'ok': True, 'call_pk': call.pk,
                         'reseller_slug': reseller.slug})

    if event == 'call.answered':
        call = svc.mark_call_answered(call_id)
        return Response({'ok': bool(call)})

    if event in ('call.completed', 'call.failed'):
        status_map = {
            'call.completed': VoiceCall.STATUS_COMPLETED,
            'call.failed':    VoiceCall.STATUS_FAILED,
        }
        call = svc.mark_call_completed(
            call_id=call_id,
            duration_seconds=data.get('duration_seconds'),
            recording_url=data.get('recording_url', ''),
            failure_reason=data.get('failure_reason', ''),
            status=status_map[event],
        )
        return Response({'ok': bool(call)})

    if event == 'recording.ready':
        try:
            call = VoiceCall.objects.get(call_id=call_id)
        except VoiceCall.DoesNotExist:
            return Response({'error': 'unknown call'}, status=404)
        call.recording_url = (data.get('recording_url') or '')[:200]
        call.save(update_fields=['recording_url'])
        return Response({'ok': True})

    if event == 'dtmf':
        # Stored only in metadata for now; Phase 3 consumes it for payment flow.
        try:
            call = VoiceCall.objects.get(call_id=call_id)
        except VoiceCall.DoesNotExist:
            return Response({'error': 'unknown call'}, status=404)
        digits = str(data.get('digits', ''))[:20]
        md = dict(call.metadata or {})
        md.setdefault('dtmf', []).append({
            'digits': digits,
            'at': timezone.now().isoformat(),
        })
        call.metadata = md
        call.save(update_fields=['metadata'])
        return Response({'ok': True})

    logger.info('voice_webhook unhandled event=%s call=%s', event, call_id)
    return Response({'ok': True, 'unhandled': event})


# ---------------------------------------------------------------------------
# Bridge container → Django: per-turn AI reply
# ---------------------------------------------------------------------------

VOICE_SYSTEM_PROMPT_DEFAULT = """You are the voice assistant for {reseller_name}, a Nigerian WiFi provider.

You are on a phone call with a customer. Follow these rules:
- SPEAK in short, natural, conversational sentences — 1-2 sentences per reply.
- Never read bullet points, URLs, or long menus aloud.
- Acknowledge before probing ("I hear you, let me check…").
- If the caller's problem needs technical action you can't do over the phone,
  offer to open a support ticket — a technician will follow up within 3 hours.
- If the caller is angry or escalating, offer to connect them to a human —
  "let me transfer you to our team now".
- Keep your tone calm, empathetic, Nigerian-English-friendly.
- Ask at most ONE question per turn.
- If you don't know something, say so honestly — do NOT invent prices, times,
  ticket numbers, or technician names.

Reseller-specific guidance from their owner:
{overrides}
"""


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def voice_turn(request):
    """Bridge-container endpoint: take the caller's latest transcript +
    conversation history, return the text to TTS back to the caller.

    Expected payload:
      {
        "tenant_slug": "acme",
        "call_id": "abc-123",
        "transcript": "my internet is down",
        "history": [ {role:"user"|"assistant", content:"..."} , ... ],
        "caller_phone": "+2348012345678",
      }
    """
    if not _api_key_ok(request):
        return Response({'error': 'forbidden'}, status=403)

    data = request.data or {}
    tenant_slug = data.get('tenant_slug', '')
    call_id = data.get('call_id', '')
    transcript = (data.get('transcript') or '').strip()
    history = data.get('history') or []
    caller_phone = data.get('caller_phone', '')

    if not (tenant_slug and call_id):
        return Response({'error': 'tenant_slug + call_id required'}, status=400)

    try:
        reseller = Reseller.objects.get(slug=tenant_slug)
    except Reseller.DoesNotExist:
        return Response({'error': 'unknown tenant'}, status=404)

    try:
        call = VoiceCall.objects.get(call_id=call_id)
    except VoiceCall.DoesNotExist:
        return Response({'error': 'unknown call'}, status=404)
    if call.reseller_id != reseller.pk:
        return Response({'error': 'tenant mismatch'}, status=403)

    try:
        config = reseller.ai_config
    except ResellerAIConfig.DoesNotExist:
        return Response({'error': 'no ai_config for tenant'}, status=409)
    if config.ai_paused_at or not config.cap('ai_enabled'):
        return Response({'error': 'ai paused or disabled'}, status=409)

    tenant = svc.tenant_for_reseller(reseller)
    if tenant is None or not tenant.voice_enabled:
        return Response({'error': 'voice not enabled for tenant'}, status=409)

    # Surface a Conversation for the inbox / operator view.
    convo = svc.ensure_voice_conversation(
        reseller=reseller, call=call, contact_phone=caller_phone,
    )

    # Record the caller's turn (inbound).
    if transcript:
        svc.append_turn(call=call, direction=VoiceTurn.DIRECTION_IN, text=transcript)
        svc.record_voice_message(
            conversation=convo, direction=Message.DIRECTION_IN,
            body=transcript, source=Message.SOURCE_CUSTOMER,
        )

    # Build the prompt + history and round-trip to the LLM.
    from ai.agents._helpers import _latest_override
    from ai.providers import get_provider

    # Voice uses its own prompt override slot; falls back to the text-customer
    # override when a reseller hasn't customised the voice prompt yet. Keeps
    # Phase 2 launch low-friction (platform default works) while still letting
    # a reseller tune voice tone independently via AIPromptVersion rows.
    override_body = (
        _latest_override(config, AIPromptVersion.ROLE_VOICE_CUSTOMER)
        or _latest_override(config, AIPromptVersion.ROLE_CUSTOMER)
    )
    system = VOICE_SYSTEM_PROMPT_DEFAULT.format(
        reseller_name=reseller.name,
        overrides=override_body or '(none)',
    )
    messages = list(history) if isinstance(history, list) else []
    if transcript:
        messages.append({'role': 'user', 'content': transcript})

    provider = get_provider(config)

    # AIAgentRun for audit — same shape as text agents, role=ROLE_CUSTOMER
    # so the dashboard aggregates voice + text runs into one cost view.
    run = AIAgentRun.objects.create(
        reseller=reseller,
        conversation=convo,
        agent_role=AIAgentRun.ROLE_VOICE_CUSTOMER,
        provider=config.text_provider,
        model=config.text_model or '',
        inputs={'voice': True, 'call_id': call_id,
                'transcript': transcript[:2000],
                'history_len': len(messages)},
    )

    start = time.monotonic()
    reply_text = ''
    status = AIAgentRun.STATUS_SUCCESS
    err = ''
    prompt_tokens = completion_tokens = 0
    cost = Decimal('0')

    try:
        resp = provider.chat(
            system=system, messages=messages,
            tools=[],  # Phase 2 MVP: no tools; Phase 2.5 wires the trimmed set
            max_tokens=200,  # voice replies stay short
        )
        reply_text = (resp.text or '').strip()
        prompt_tokens = resp.prompt_tokens
        completion_tokens = resp.completion_tokens
        cost = resp.cost_ngn or Decimal('0')
    except Exception as exc:
        status = AIAgentRun.STATUS_FAILED
        err = f'{type(exc).__name__}: {exc}'[:500]
        logger.exception('voice_turn LLM error call=%s', call_id)
        reply_text = (
            "I'm sorry, I'm having trouble right now. Let me transfer you to our team."
        )

    latency_ms = int((time.monotonic() - start) * 1000)
    AIAgentRun.objects.filter(pk=run.pk).update(
        status=status, error_message=err,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        cost_ngn=cost,
        outputs={'text': reply_text[:4000]},
        ended_at=timezone.now(),
        latency_ms=latency_ms,
    )

    # Record AI turn (outbound) + message for inbox.
    svc.append_turn(
        call=call, direction=VoiceTurn.DIRECTION_OUT, text=reply_text,
        agent_run_id=run.pk, latency_ms=latency_ms, cost_ngn=cost,
    )
    svc.record_voice_message(
        conversation=convo, direction=Message.DIRECTION_OUT,
        body=reply_text, source=Message.SOURCE_AI_SUPPORT,
        agent_run_id=run.pk,
    )

    return Response({
        'text': reply_text,
        'call_id': call_id,
        'run_id': run.pk,
        'latency_ms': latency_ms,
        'status': status,
    })
