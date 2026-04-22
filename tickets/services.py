"""
Ticket lifecycle helpers. Views and AI agents should call through these
functions rather than poking Ticket fields directly — they ensure SLA timers,
audit events, and staff load counters stay in sync.
"""
from django.db import transaction
from django.utils import timezone

from tickets.models import Ticket, TicketEvent


def _log(ticket, kind, actor='system', note='', from_status='', to_status='', metadata=None):
    TicketEvent.objects.create(
        ticket=ticket,
        kind=kind,
        actor=actor or 'system',
        note=note or '',
        from_status=from_status or '',
        to_status=to_status or '',
        metadata=metadata or {},
    )


@transaction.atomic
def create_ticket(*, reseller, type, subject, body='', lead=None, subscriber=None,
                  conversation=None, installation_order=None, priority=Ticket.PRIORITY_NORMAL,
                  assigned_staff=None, actor='system',
                  diagnosed_cause='', suggested_action=''):
    ticket = Ticket.objects.create(
        reseller=reseller,
        type=type,
        subject=subject,
        body=body,
        lead=lead,
        subscriber=subscriber,
        conversation=conversation,
        installation_order=installation_order,
        priority=priority,
        assigned_staff=assigned_staff,
        status=Ticket.STATUS_ASSIGNED if assigned_staff else Ticket.STATUS_OPEN,
        diagnosed_cause=diagnosed_cause or '',
        suggested_action=suggested_action or '',
    )
    _log(ticket, TicketEvent.KIND_CREATED, actor=actor, to_status=ticket.status)
    if assigned_staff:
        _bump_staff_load(assigned_staff, +1)
        _log(
            ticket, TicketEvent.KIND_ASSIGNED, actor=actor,
            metadata={'staff_id': assigned_staff.pk, 'staff_name': assigned_staff.name},
        )

    # Field-supervisor AI: PROPOSE a tech (writes a TicketEvent) for unassigned
    # support/install tickets. Does NOT auto-assign — a human clicks Assign on
    # the Tickets page. Wrapped so a broken ai/ config never blocks the call.
    if not assigned_staff and type in (Ticket.TYPE_SUPPORT, Ticket.TYPE_INSTALL):
        try:
            if hasattr(reseller, 'ai_config'):
                from ai.jobs import enqueue_propose_assignment
                transaction.on_commit(lambda tid=ticket.pk: enqueue_propose_assignment(tid))
        except Exception:
            pass

    # Customer milestone: ticket opened. Goes back into the customer's chat
    # (or falls back to a templated transactional ping).
    try:
        from ai.jobs import notify_customer_ticket_milestone
        transaction.on_commit(
            lambda tid=ticket.pk: notify_customer_ticket_milestone(tid, 'opened'),
        )
    except Exception:
        pass

    return ticket


@transaction.atomic
def assign_ticket(ticket, staff_member, actor='system', note=''):
    previous = ticket.assigned_staff
    if previous and previous.pk == staff_member.pk:
        return ticket
    ticket.assigned_staff = staff_member
    if ticket.status == Ticket.STATUS_OPEN:
        ticket.status = Ticket.STATUS_ASSIGNED
    ticket.save(update_fields=['assigned_staff', 'status', 'updated_at'])
    if previous:
        _bump_staff_load(previous, -1)
    _bump_staff_load(staff_member, +1)
    _log(
        ticket, TicketEvent.KIND_ASSIGNED, actor=actor, note=note,
        metadata={'staff_id': staff_member.pk, 'staff_name': staff_member.name,
                  'previous_staff_id': previous.pk if previous else None},
    )

    # Hand off to the field-supervisor AI to send the dispatch brief over WA.
    try:
        if hasattr(ticket.reseller, 'ai_config'):
            from ai.jobs import enqueue_dispatch_brief
            transaction.on_commit(lambda tid=ticket.pk: enqueue_dispatch_brief(tid))
    except Exception:
        pass
    return ticket


@transaction.atomic
def change_status(ticket, new_status, actor='system', note=''):
    if new_status == ticket.status:
        return ticket
    old_status = ticket.status
    now = timezone.now()

    ticket.status = new_status
    updates = ['status', 'updated_at']

    if new_status == Ticket.STATUS_RESOLVED and not ticket.resolved_at:
        ticket.resolved_at = now
        updates.append('resolved_at')
        if note:
            ticket.resolution_note = note
            updates.append('resolution_note')
    if new_status == Ticket.STATUS_CLOSED and not ticket.closed_at:
        ticket.closed_at = now
        updates.append('closed_at')
        if not ticket.resolved_at:
            ticket.resolved_at = now
            updates.append('resolved_at')

    ticket.save(update_fields=updates)

    if new_status in Ticket.TERMINAL_STATUSES and ticket.assigned_staff:
        _bump_staff_load(ticket.assigned_staff, -1)

    kind = {
        Ticket.STATUS_RESOLVED: TicketEvent.KIND_RESOLVED,
        Ticket.STATUS_CLOSED: TicketEvent.KIND_CLOSED,
    }.get(new_status, TicketEvent.KIND_STATUS_CHANGED)
    if old_status in Ticket.TERMINAL_STATUSES and new_status not in Ticket.TERMINAL_STATUSES:
        kind = TicketEvent.KIND_REOPENED

    _log(ticket, kind, actor=actor, note=note, from_status=old_status, to_status=new_status)

    # Customer-facing milestone for in_progress / resolved transitions.
    if new_status in (Ticket.STATUS_IN_PROGRESS, Ticket.STATUS_RESOLVED):
        try:
            from ai.jobs import notify_customer_ticket_milestone
            transaction.on_commit(
                lambda tid=ticket.pk, st=new_status:
                notify_customer_ticket_milestone(tid, st),
            )
        except Exception:
            pass
    return ticket


@transaction.atomic
def record_first_response(ticket, actor='human_staff'):
    if ticket.first_response_at:
        return ticket
    ticket.first_response_at = timezone.now()
    ticket.save(update_fields=['first_response_at', 'updated_at'])
    _log(ticket, TicketEvent.KIND_FIRST_RESPONSE, actor=actor)
    return ticket


@transaction.atomic
def add_comment(ticket, *, actor, note, metadata=None):
    _log(ticket, TicketEvent.KIND_COMMENT, actor=actor, note=note, metadata=metadata)
    if not ticket.first_response_at and actor != 'system':
        record_first_response(ticket, actor=actor)
    return ticket


@transaction.atomic
def record_ai_action(ticket, *, agent_role, tool, inputs=None, outputs=None, confidence=None):
    """
    Record an AI tool-call on this ticket. Flips ai_handled and stores the
    latest confidence so ops can filter low-confidence cases.
    """
    fields = []
    if not ticket.ai_handled:
        ticket.ai_handled = True
        fields.append('ai_handled')
    if confidence is not None:
        ticket.ai_confidence = confidence
        fields.append('ai_confidence')
    if fields:
        fields.append('updated_at')
        ticket.save(update_fields=fields)
    _log(
        ticket, TicketEvent.KIND_AI_ACTION, actor=f'ai_{agent_role}',
        metadata={'tool': tool, 'inputs': inputs or {}, 'outputs': outputs or {},
                  'confidence': confidence},
    )
    return ticket


@transaction.atomic
def escalate(ticket, reason, actor='system'):
    ticket.escalation_reason = reason[:200]
    ticket.priority = Ticket.PRIORITY_HIGH if ticket.priority == Ticket.PRIORITY_NORMAL else ticket.priority
    ticket.save(update_fields=['escalation_reason', 'priority', 'updated_at'])
    _log(ticket, TicketEvent.KIND_ESCALATED, actor=actor, note=reason)
    return ticket


def _bump_staff_load(staff_member, delta):
    # Kept separate so we can swap in a F-expression update under heavy write
    # contention without touching the call sites.
    staff_member.current_load = max(0, (staff_member.current_load or 0) + delta)
    staff_member.save(update_fields=['current_load'])
