"""RQ jobs entry points for AI agents.

Inbound-message path:
  record_inbound_message() schedules `route_inbound_message(message_id)` on
  the `ai` queue after the DB commit. Routing decision:
    - Conversation.kind == 'tech' OR sender phone matches StaffMember.whatsapp
        → FieldInboundAgent (interpret tech reply, confirm-before-action)
    - else                                                  → CustomerAgent
      (one agent handles the full customer journey from enquiry through
       support — state persisted on Conversation.diagnostic_state)

Ticket-side jobs:
  enqueue_propose_assignment(ticket_id) → FieldSupervisorAgent.propose_assignment
  enqueue_dispatch_brief(ticket_id)     → FieldSupervisorAgent.dispatch_brief
  notify_customer_ticket_milestone(ticket_id, status)
                                        → posts a chat message into the
                                          customer's WA thread (or templated
                                          fallback if no conversation).
"""
import logging

import django_rq

from ai.agents.customer import CustomerAgent
from ai.agents.field import FieldSupervisorAgent
from ai.agents.field_inbound import FieldInboundAgent
from ai.models import ResellerAIConfig
from conversations.models import Conversation, Message
from staff.models import StaffMember
from tickets.models import Ticket

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inbound router
# ---------------------------------------------------------------------------

def enqueue_inbound_router(message_id: int) -> None:
    django_rq.get_queue('ai').enqueue(route_inbound_message, message_id)


def _normalise_phone_for_match(raw: str) -> str:
    """Trim non-digits + return the last 10 digits — robust against +234 vs 0
    prefixes and various spacing conventions stored on StaffMember."""
    digits = ''.join(c for c in (raw or '') if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _maybe_promote_to_tech_conversation(conv: Conversation) -> StaffMember | None:
    """If the inbound conversation's external thread matches a StaffMember's
    whatsapp/phone, mark the conversation as kind='tech' + assigned_staff and
    return the StaffMember. Idempotent."""
    target = _normalise_phone_for_match(
        conv.contact_phone or conv.external_thread_id.split('@')[0],
    )
    if not target:
        return None
    candidates = StaffMember.objects.filter(reseller=conv.reseller, active=True)
    for sm in candidates:
        if _normalise_phone_for_match(sm.whatsapp or sm.phone) == target:
            updates = []
            if conv.kind != Conversation.KIND_TECH:
                conv.kind = Conversation.KIND_TECH
                updates.append('kind')
            if conv.assigned_staff_id != sm.pk:
                conv.assigned_staff = sm
                updates.append('assigned_staff')
            if updates:
                updates.append('updated_at')
                conv.save(update_fields=updates)
            return sm
    return None


def route_inbound_message(message_id: int) -> dict:
    # Outer guard: a crashing router is invisible to the customer (they
    # just don't get a reply). Catch, log with full trace, and return a
    # structured skipped dict so the failure is traceable in journalctl
    # and the job doesn't sit in failed_job_registry silently.
    try:
        return _route_inbound_message(message_id)
    except Exception as exc:
        logger.exception('route_inbound_message crashed for msg %s', message_id)
        return {'skipped': True,
                'reason': f'router-crashed: {type(exc).__name__}: {exc}'}


def _route_inbound_message(message_id: int) -> dict:
    try:
        msg = (Message.objects
               .select_related('conversation', 'conversation__reseller')
               .get(pk=message_id))
    except Message.DoesNotExist:
        return {'skipped': True, 'reason': 'message gone'}

    convo = msg.conversation
    if msg.direction != Message.DIRECTION_IN:
        return {'skipped': True, 'reason': 'not inbound'}

    # Voice messages are handled synchronously by voice.views.voice_turn (the
    # bridge container calls it per utterance and blocks for the reply).
    # They must NOT re-enter the text agent router — CustomerAgent's
    # send_reply tool would try to post the voice transcript back over
    # WhatsApp, which is wrong. Any Message created with channel=voice is
    # already fully handled; short-circuit here defensively.
    if convo.channel == Conversation.CHANNEL_VOICE:
        return {'skipped': True, 'reason': 'voice channel — handled by voice.views.voice_turn'}

    # Pre-router: if the conversation is awaiting a satisfaction YES/NO
    # reply, handle that BEFORE any agent runs. Fires regardless of
    # ai_enabled — closing or reopening a ticket is a deterministic
    # transactional operation, not an AI reply.
    reply_verdict = _handle_satisfaction_reply(msg, convo)
    if reply_verdict is not None:
        return reply_verdict

    if not convo.ai_enabled:
        return {'skipped': True, 'reason': 'conversation.ai_enabled=False'}

    try:
        config = convo.reseller.ai_config
    except ResellerAIConfig.DoesNotExist:
        return {'skipped': True, 'reason': 'no ai_config for reseller'}

    if config.ai_paused_at or not config.cap('ai_enabled'):
        return {'skipped': True, 'reason': 'ai paused or not enabled'}

    # Tech-side routing: either the conversation is already kind='tech', or
    # the sender's phone matches a StaffMember (in which case we promote it).
    if convo.kind == Conversation.KIND_TECH or _maybe_promote_to_tech_conversation(convo):
        return run_field_inbound_agent(message_id)

    # All non-tech inbound flows through the unified CustomerAgent, which
    # runs one state machine from enquiry through support.
    return run_customer_agent(message_id)


# ---------------------------------------------------------------------------
# Satisfaction YES/NO handling
# ---------------------------------------------------------------------------

_YES_TOKENS = {'yes', 'y', 'yep', 'yeah', 'ok', 'okay', 'working', 'fine',
               'good', 'thanks', 'resolved', 'fixed', 'done'}
_NO_TOKENS = {'no', 'n', 'nope', 'not', 'still', 'broken', 'bad', 'worse',
              'nothing', 'same'}
_YES_PHRASES = ('thank you', 'all good', 'all working', 'its working',
                "it's working", 'back online')
_NO_PHRASES = ('not working', "doesn't work", 'doesnt work', 'still bad',
               'still broken', 'not fixed', 'no signal', 'no internet')


def _classify_yes_no(body: str) -> str:
    """Return 'yes' | 'no' | '' (ambiguous) for a short customer reply.

    Matching strategy:
      1. Strip to a short lowercase alphanumeric string.
      2. If too long (>40 chars), reply is free-form — don't classify.
      3. Phrase check first (e.g. "not working"), then whole-token check
         (so 'n' in 'thanks' doesn't misfire).
    """
    text = (body or '').strip().lower()
    if not text:
        return ''
    text = ''.join(c if c.isalnum() or c == ' ' else ' ' for c in text)
    text = ' '.join(text.split())
    if not text or len(text) > 40:
        return ''
    # Phrase-level match runs first because "not working" should win over
    # matching 'not' and 'working' individually in different sets.
    if any(p in text for p in _NO_PHRASES):
        return 'no'
    if any(p in text for p in _YES_PHRASES):
        return 'yes'
    tokens = set(text.split())
    if tokens & _NO_TOKENS:
        return 'no'
    if tokens & _YES_TOKENS:
        return 'yes'
    return ''


def _handle_satisfaction_reply(msg: Message, convo: Conversation) -> dict | None:
    """If the conversation is awaiting a YES/NO satisfaction confirmation,
    parse the reply and act on it. Returns a result dict if handled, None
    otherwise (caller continues with normal routing).
    """
    state = convo.diagnostic_state or {}
    awaiting_tid = state.get('awaiting_confirmation_ticket_id')
    if not awaiting_tid:
        return None
    try:
        ticket = Ticket.objects.get(pk=awaiting_tid, reseller=convo.reseller)
    except Ticket.DoesNotExist:
        # Stale pointer — clear it so we don't loop.
        _clear_awaiting(convo)
        return None

    verdict = _classify_yes_no(msg.body)
    if verdict == 'yes':
        from tickets.services import change_status
        if ticket.status != Ticket.STATUS_CLOSED:
            change_status(ticket, Ticket.STATUS_CLOSED,
                          actor='customer_confirmed',
                          note='Customer confirmed fix via satisfaction ping.')
        _send_customer_reply(
            convo,
            f"Great — we've closed ticket #{ticket.pk}. Thanks for confirming!",
        )
        _clear_awaiting(convo)
        return {'handled': True, 'verdict': 'yes', 'ticket_id': ticket.pk}
    if verdict == 'no':
        from tickets.services import change_status
        if ticket.status != Ticket.STATUS_IN_PROGRESS:
            change_status(ticket, Ticket.STATUS_IN_PROGRESS,
                          actor='customer_reopened',
                          note='Customer says fix did not work — reopening.')
        _send_customer_reply(
            convo,
            f"Sorry to hear that. We've reopened ticket #{ticket.pk} and "
            f"will follow up — updates within 3 hours.",
        )
        # Reset state so the Support agent picks up at classify on the
        # inbound-router path below (this function returns handled, so we
        # don't re-run the agent here).
        state['step'] = 'classify'
        state['awaiting_confirmation_ticket_id'] = None
        from django.utils import timezone as _tz
        state['updated_at'] = _tz.now().isoformat()
        convo.diagnostic_state = state
        convo.save(update_fields=['diagnostic_state', 'updated_at'])
        return {'handled': True, 'verdict': 'no', 'ticket_id': ticket.pk}
    # Ambiguous — clear the awaiting flag (customer has moved on) and
    # fall through to normal routing so the Support agent can respond to
    # whatever the customer actually said.
    _clear_awaiting(convo)
    return None


def _clear_awaiting(convo: Conversation) -> None:
    state = dict(convo.diagnostic_state or {})
    state.pop('awaiting_confirmation_ticket_id', None)
    from django.utils import timezone as _tz
    state['updated_at'] = _tz.now().isoformat()
    convo.diagnostic_state = state
    convo.save(update_fields=['diagnostic_state', 'updated_at'])


def _send_customer_reply(convo: Conversation, body: str) -> None:
    from conversations.services import record_outbound_message
    record_outbound_message(
        conversation=convo, body=body, source=Message.SOURCE_AI_SUPPORT,
    )
    if convo.channel == Conversation.CHANNEL_WHATSAPP:
        from notifications.notify import send_whatsapp
        phone = (convo.contact_phone
                 or convo.external_thread_id.split('@')[0])
        try:
            send_whatsapp(convo.reseller.slug, phone, body)
        except Exception:
            logger.warning('satisfaction reply WA send failed for conv %s', convo.pk)


def send_satisfaction_ping(ticket_id: int) -> dict:
    """Enqueue-target: 15 min after a ticket is marked resolved, post a
    'Is it working? YES/NO' message to the customer and flag the
    conversation as awaiting_confirmation.

    Skips silently if:
      - The ticket was reopened / re-resolved in the interim (status no
        longer 'resolved').
      - The ticket has no linked customer conversation.
    """
    try:
        ticket = Ticket.objects.select_related(
            'reseller', 'conversation', 'subscriber', 'lead',
        ).get(pk=ticket_id)
    except Ticket.DoesNotExist:
        return {'skipped': True, 'reason': 'ticket gone'}
    if ticket.status != Ticket.STATUS_RESOLVED:
        return {'skipped': True, 'reason': f'status now {ticket.status}'}
    if not ticket.conversation_id:
        return {'skipped': True, 'reason': 'no conversation'}
    convo = ticket.conversation
    if convo.kind != Conversation.KIND_CUSTOMER:
        return {'skipped': True, 'reason': 'not a customer conversation'}

    # Send the YES/NO message via the milestone path so the same template
    # handling applies.
    result = notify_customer_ticket_milestone(
        ticket_id, 'resolved_pending_confirmation',
    )
    # Stamp the awaiting flag on the conversation.
    state = dict(convo.diagnostic_state or {})
    state['step'] = 'awaiting_confirmation'
    state['awaiting_confirmation_ticket_id'] = ticket.pk
    from django.utils import timezone as _tz
    state['updated_at'] = _tz.now().isoformat()
    convo.diagnostic_state = state
    convo.save(update_fields=['diagnostic_state', 'updated_at'])
    return {'sent': True, 'ticket_id': ticket.pk, 'milestone_result': result}


def sweep_stale_awaiting_confirmations(max_age_hours: int = 24) -> dict:
    """Auto-close tickets whose satisfaction ping went unanswered past the
    window. Called by a periodic task (sweep_field_pings extension).
    """
    from datetime import timedelta
    from django.utils import timezone as _tz
    cutoff_dt = _tz.now() - timedelta(hours=max_age_hours)
    cutoff_iso = cutoff_dt.isoformat()
    stale_convos = Conversation.objects.filter(
        diagnostic_state__awaiting_confirmation_ticket_id__isnull=False,
    ).exclude(diagnostic_state__awaiting_confirmation_ticket_id=None)
    closed = 0
    for convo in stale_convos:
        state = convo.diagnostic_state or {}
        updated = state.get('updated_at') or ''
        if updated > cutoff_iso:
            continue
        tid = state.get('awaiting_confirmation_ticket_id')
        try:
            ticket = Ticket.objects.get(pk=tid)
        except Ticket.DoesNotExist:
            _clear_awaiting(convo)
            continue
        if ticket.status == Ticket.STATUS_RESOLVED:
            from tickets.services import change_status
            change_status(ticket, Ticket.STATUS_CLOSED,
                          actor='system_timeout',
                          note='Auto-closed: no customer reply within 24h.')
            closed += 1
        _clear_awaiting(convo)
    return {'closed': closed}


# ---------------------------------------------------------------------------
# Per-agent runners
# ---------------------------------------------------------------------------

def run_customer_agent(message_id: int) -> dict:
    msg = Message.objects.select_related(
        'conversation', 'conversation__reseller').get(pk=message_id)
    config = msg.conversation.reseller.ai_config
    if not config.is_agent_enabled('customer'):
        return {'skipped': True, 'reason': 'customer agent disabled'}
    r = CustomerAgent(config).handle_inbound_message(
        conversation=msg.conversation, message=msg)
    return {'agent': 'customer', 'run_id': r.run_id, 'replied': r.replied,
            'drafted': r.drafted, 'skipped': r.skipped}


def run_field_inbound_agent(message_id: int) -> dict:
    msg = Message.objects.select_related(
        'conversation', 'conversation__reseller',
        'conversation__assigned_staff').get(pk=message_id)
    config = msg.conversation.reseller.ai_config
    if not config.is_agent_enabled('field'):
        return {'skipped': True, 'reason': 'field agent disabled'}
    r = FieldInboundAgent(config).handle_inbound_message(
        conversation=msg.conversation, message=msg)
    return {'agent': 'field_inbound', 'run_id': r.run_id,
            'replied': r.replied, 'drafted': r.drafted, 'skipped': r.skipped}


# ---------------------------------------------------------------------------
# Field-supervisor (ticket side)
# ---------------------------------------------------------------------------

def enqueue_propose_assignment(ticket_id: int) -> None:
    django_rq.get_queue('ai').enqueue(run_propose_assignment, ticket_id)


def run_propose_assignment(ticket_id: int) -> dict:
    try:
        ticket = Ticket.objects.select_related('reseller').get(pk=ticket_id)
    except Ticket.DoesNotExist:
        return {'skipped': True, 'reason': 'ticket gone'}
    try:
        config = ticket.reseller.ai_config
    except ResellerAIConfig.DoesNotExist:
        return {'skipped': True, 'reason': 'no ai_config'}
    if config.ai_paused_at or not config.cap('ai_enabled'):
        return {'skipped': True, 'reason': 'ai paused'}
    if not config.is_agent_enabled('field'):
        return {'skipped': True, 'reason': 'field agent disabled'}
    r = FieldSupervisorAgent(config).propose_assignment(ticket=ticket)
    return {'agent': 'field_propose', 'run_id': r.run_id, 'skipped': r.skipped}


def enqueue_dispatch_brief(ticket_id: int) -> None:
    django_rq.get_queue('ai').enqueue(run_dispatch_brief, ticket_id)


def run_dispatch_brief(ticket_id: int) -> dict:
    try:
        ticket = Ticket.objects.select_related(
            'reseller', 'assigned_staff').get(pk=ticket_id)
    except Ticket.DoesNotExist:
        return {'skipped': True, 'reason': 'ticket gone'}
    try:
        config = ticket.reseller.ai_config
    except ResellerAIConfig.DoesNotExist:
        return {'skipped': True, 'reason': 'no ai_config'}
    if config.ai_paused_at or not config.cap('ai_enabled'):
        return {'skipped': True, 'reason': 'ai paused'}
    if not config.is_agent_enabled('field'):
        return {'skipped': True, 'reason': 'field agent disabled'}
    r = FieldSupervisorAgent(config).dispatch_brief(ticket=ticket)
    return {'agent': 'field_dispatch', 'run_id': r.run_id, 'skipped': r.skipped}


# ---------------------------------------------------------------------------
# Customer milestone notifications (ticket lifecycle → customer chat)
# ---------------------------------------------------------------------------

_MILESTONE_BODIES = {
    'opened': (
        "Hi {name}, we've logged your issue (ticket #{ticket_id}). "
        "We're checking it now — updates will be sent within 3 hours."
    ),
    'in_progress': (
        "Hi {name}, {tech} is working on it now (ticket #{ticket_id})."
    ),
    'resolved': (
        "Hi {name}, your issue (ticket #{ticket_id}) has been marked resolved. "
        "We'll check back with you shortly to make sure everything is working."
    ),
    'resolved_pending_confirmation': (
        "Hi {name}, is your WiFi working now? Reply *YES* to close ticket "
        "#{ticket_id}, or *NO* if you're still having issues."
    ),
}

_MILESTONE_TO_EVENT = {
    'opened': 'ticket_opened',
    'in_progress': 'ticket_in_progress',
    'resolved': 'ticket_resolved',
}


def notify_customer_ticket_milestone(ticket_id: int, status: str) -> dict:
    """Push a milestone message into the customer's chat (or fall back to a
    templated transactional ping if the ticket has no linked conversation).

    Always sends — bypasses auto_send_replies and per-conversation ai_enabled
    because milestones are transactional system pings, not free-form AI.
    """
    try:
        ticket = Ticket.objects.select_related(
            'reseller', 'subscriber', 'lead', 'conversation', 'assigned_staff',
        ).get(pk=ticket_id)
    except Ticket.DoesNotExist:
        return {'skipped': True, 'reason': 'ticket gone'}

    name = ''
    if ticket.subscriber_id:
        name = ticket.subscriber.phone or ''
    elif ticket.lead_id:
        name = ticket.lead.name or ticket.lead.phone

    body = _MILESTONE_BODIES.get(status, '').format(
        name=name or 'there',
        ticket_id=ticket.pk,
        tech=ticket.assigned_staff.name if ticket.assigned_staff_id else 'a technician',
    )
    if not body:
        return {'skipped': True, 'reason': f'no template for status {status}'}

    # Path A — post into the customer's existing conversation (preferred).
    if ticket.conversation_id and ticket.conversation.kind == Conversation.KIND_CUSTOMER:
        from conversations.services import record_outbound_message
        msg = record_outbound_message(
            conversation=ticket.conversation,
            body=body,
            source=Message.SOURCE_AI_SUPPORT,
        )
        ok = False
        if ticket.conversation.channel == Conversation.CHANNEL_WHATSAPP:
            from notifications.notify import send_whatsapp
            phone = (ticket.conversation.contact_phone
                     or ticket.conversation.external_thread_id.split('@')[0])
            ok = bool(send_whatsapp(ticket.reseller.slug, phone, body))
        msg.delivery_status = (
            Message.DELIVERY_QUEUED if ok else Message.DELIVERY_FAILED
        )
        msg.save(update_fields=['delivery_status'])
        return {'sent': True, 'via': 'conversation', 'message_id': msg.pk, 'ok': ok}

    # Path B — fallback to templated transactional notification.
    if ticket.subscriber_id:
        from notifications.notify import notify_subscriber
        ev = _MILESTONE_TO_EVENT.get(status)
        if ev:
            notify_subscriber(ticket.subscriber, ev, context={
                'name': name or '',
                'ticket_id': ticket.pk,
                'tech': ticket.assigned_staff.name if ticket.assigned_staff_id else '',
            })
            return {'sent': True, 'via': 'templated'}
    return {'skipped': True, 'reason': 'no delivery path'}


# ---------------------------------------------------------------------------
# Legacy wrappers kept so older callers (cron / tests) still work
# ---------------------------------------------------------------------------

def enqueue_field_agent(ticket_id: int) -> None:
    """Legacy alias — now points at the propose-only flow."""
    enqueue_propose_assignment(ticket_id)


def run_field_agent(ticket_id: int) -> dict:
    return run_propose_assignment(ticket_id)
