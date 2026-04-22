"""RQ jobs entry points for AI agents.

Inbound-message path:
  record_inbound_message() schedules `route_inbound_message(message_id)` on
  the `ai` queue after the DB commit. Routing decision:
    - Conversation.kind == 'tech' OR sender phone matches StaffMember.whatsapp
        → FieldInboundAgent (interpret tech reply, confirm-before-action)
    - else if conversation has a Subscriber                → SupportAgent
    - else                                                  → SalesAgent

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

from ai.agents.field import FieldSupervisorAgent
from ai.agents.field_inbound import FieldInboundAgent
from ai.agents.sales import SalesAgent
from ai.agents.support import SupportAgent
from ai.models import ResellerAIConfig
from conversations.models import Conversation, Message
from staff.models import StaffMember
from tickets.models import Ticket

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inbound router
# ---------------------------------------------------------------------------

def enqueue_sales_agent(message_id: int) -> None:
    """Back-compat shim. Prefer `enqueue_inbound_router` for new call sites."""
    django_rq.get_queue('ai').enqueue(route_inbound_message, message_id)


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
    try:
        msg = (Message.objects
               .select_related('conversation', 'conversation__reseller')
               .get(pk=message_id))
    except Message.DoesNotExist:
        return {'skipped': True, 'reason': 'message gone'}

    convo = msg.conversation
    if msg.direction != Message.DIRECTION_IN:
        return {'skipped': True, 'reason': 'not inbound'}
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

    if convo.subscriber_id:
        return run_support_agent(message_id)
    return run_sales_agent(message_id)


# ---------------------------------------------------------------------------
# Per-agent runners
# ---------------------------------------------------------------------------

def run_sales_agent(message_id: int) -> dict:
    msg = Message.objects.select_related(
        'conversation', 'conversation__reseller').get(pk=message_id)
    config = msg.conversation.reseller.ai_config
    if not config.is_agent_enabled('sales'):
        return {'skipped': True, 'reason': 'sales agent disabled'}
    r = SalesAgent(config).handle_inbound_message(
        conversation=msg.conversation, message=msg)
    return {'agent': 'sales', 'run_id': r.run_id, 'replied': r.replied,
            'drafted': r.drafted, 'skipped': r.skipped}


def run_support_agent(message_id: int) -> dict:
    msg = Message.objects.select_related(
        'conversation', 'conversation__reseller').get(pk=message_id)
    config = msg.conversation.reseller.ai_config
    if not config.is_agent_enabled('support'):
        return {'skipped': True, 'reason': 'support agent disabled'}
    r = SupportAgent(config).handle_inbound_message(
        conversation=msg.conversation, message=msg)
    return {'agent': 'support', 'run_id': r.run_id, 'replied': r.replied,
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
        "Tech support is now on it."
    ),
    'in_progress': (
        "Hi {name}, {tech} has accepted ticket #{ticket_id} and is working on it now."
    ),
    'resolved': (
        "Hi {name}, your issue (ticket #{ticket_id}) has been marked resolved. "
        "Reply STILL if it's not actually fixed."
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
