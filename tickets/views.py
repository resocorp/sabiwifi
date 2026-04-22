"""
Ticket API — reseller dashboard.
"""
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import (
    can, effective_reseller, scope_queryset,
)
from tickets.models import Ticket, TicketEvent
from tickets.services import (
    add_comment, assign_ticket, change_status, create_ticket, escalate,
)


def _serialise_ticket(ticket):
    return {
        'id': ticket.pk,
        'type': ticket.type,
        'status': ticket.status,
        'priority': ticket.priority,
        'subject': ticket.subject,
        'body': ticket.body,
        'lead_id': ticket.lead_id,
        'subscriber_id': ticket.subscriber_id,
        'conversation_id': ticket.conversation_id,
        'installation_order_id': ticket.installation_order_id,
        'assigned_staff_id': ticket.assigned_staff_id,
        'assigned_staff_name': ticket.assigned_staff.name if ticket.assigned_staff_id else None,
        'sla_due_at': ticket.sla_due_at.isoformat() if ticket.sla_due_at else None,
        'sla_breached': ticket.is_breached(),
        'first_response_at': ticket.first_response_at.isoformat() if ticket.first_response_at else None,
        'resolved_at': ticket.resolved_at.isoformat() if ticket.resolved_at else None,
        'closed_at': ticket.closed_at.isoformat() if ticket.closed_at else None,
        'ai_handled': ticket.ai_handled,
        'ai_confidence': ticket.ai_confidence,
        'escalation_reason': ticket.escalation_reason,
        'resolution_note': ticket.resolution_note,
        'diagnosed_cause': ticket.diagnosed_cause,
        'suggested_action': ticket.suggested_action,
        'time_to_resolution_seconds': ticket.time_to_resolution_seconds(),
        'time_to_first_response_seconds': ticket.time_to_first_response_seconds(),
        'created_at': ticket.created_at.isoformat(),
        'updated_at': ticket.updated_at.isoformat(),
    }


def _serialise_event(event):
    return {
        'id': event.pk,
        'kind': event.kind,
        'actor': event.actor,
        'from_status': event.from_status,
        'to_status': event.to_status,
        'note': event.note,
        'metadata': event.metadata,
        'created_at': event.created_at.isoformat(),
    }


def _require(request, *caps):
    """Raise PermissionDenied unless the user has any of the named caps."""
    if not any(can(request.user, c) for c in caps):
        raise PermissionDenied()


def _tickets_for_user(request):
    """Reseller- and (for field tech) ownership-scoped queryset."""
    return scope_queryset(Ticket.objects.all(), request.user)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ticket_list(request):
    _require(request, 'tickets', 'tickets_read', 'tickets_own')
    qs = _tickets_for_user(request).select_related('assigned_staff')
    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)
    ttype = request.GET.get('type')
    if ttype:
        qs = qs.filter(type=ttype)
    assigned = request.GET.get('assigned_staff_id')
    if assigned:
        qs = qs.filter(assigned_staff_id=assigned)
    breached = request.GET.get('breached')
    if breached == '1':
        from django.utils import timezone
        qs = qs.filter(sla_due_at__lt=timezone.now()).exclude(
            status__in=Ticket.TERMINAL_STATUSES,
        )
    q = request.GET.get('q')
    if q:
        qs = qs.filter(Q(subject__icontains=q) | Q(body__icontains=q))
    return Response([_serialise_ticket(t) for t in qs[:300]])


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ticket_create(request):
    _require(request, 'tickets')
    reseller = effective_reseller(request.user)
    data = request.data
    if not data.get('subject'):
        return Response({'error': 'subject required'}, status=400)

    lead = subscriber = None
    if data.get('lead_id'):
        from leads.models import Lead
        lead = get_object_or_404(Lead, pk=data['lead_id'], reseller=reseller)
    if data.get('subscriber_id'):
        from accounts.models import Subscriber
        subscriber = get_object_or_404(Subscriber, pk=data['subscriber_id'], reseller=reseller)
    if lead and subscriber:
        return Response({'error': 'pick one of lead_id / subscriber_id'}, status=400)

    assigned_staff = None
    if data.get('assigned_staff_id'):
        from staff.models import StaffMember
        assigned_staff = get_object_or_404(
            StaffMember,
            pk=data['assigned_staff_id'],
            reseller__in=[reseller, None],
        )

    ticket = create_ticket(
        reseller=reseller,
        type=data.get('type', Ticket.TYPE_SUPPORT),
        subject=data['subject'],
        body=data.get('body', ''),
        lead=lead,
        subscriber=subscriber,
        assigned_staff=assigned_staff,
        priority=data.get('priority', Ticket.PRIORITY_NORMAL),
        actor=f'human:{request.user.pk}',
    )
    return Response(_serialise_ticket(ticket))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ticket_detail(request, pk):
    _require(request, 'tickets', 'tickets_read', 'tickets_own')
    ticket = get_object_or_404(_tickets_for_user(request), pk=pk)
    events = [_serialise_event(e) for e in ticket.events.all()]
    return Response({'ticket': _serialise_ticket(ticket), 'events': events})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ticket_assign(request, pk):
    _require(request, 'assign_ticket')
    reseller = effective_reseller(request.user)
    ticket = get_object_or_404(_tickets_for_user(request), pk=pk)
    staff_id = request.data.get('staff_id')
    if not staff_id:
        return Response({'error': 'staff_id required'}, status=400)
    from staff.models import StaffMember
    staff = get_object_or_404(
        StaffMember, pk=staff_id, reseller__in=[reseller, None],
    )
    assign_ticket(ticket, staff, actor=f'human:{request.user.pk}',
                  note=request.data.get('note', ''))
    return Response(_serialise_ticket(ticket))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ticket_status(request, pk):
    _require(request, 'tickets', 'tickets_own')
    ticket = get_object_or_404(_tickets_for_user(request), pk=pk)
    new_status = request.data.get('status')
    valid = dict(Ticket.STATUS_CHOICES)
    if new_status not in valid:
        return Response({'error': 'invalid status'}, status=400)
    change_status(ticket, new_status,
                  actor=f'human:{request.user.pk}',
                  note=request.data.get('note', ''))
    return Response(_serialise_ticket(ticket))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ticket_comment(request, pk):
    _require(request, 'tickets', 'tickets_own')
    ticket = get_object_or_404(_tickets_for_user(request), pk=pk)
    note = request.data.get('note', '').strip()
    if not note:
        return Response({'error': 'note required'}, status=400)
    add_comment(ticket, actor=f'human:{request.user.pk}', note=note)
    return Response({'ok': True})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ticket_escalate(request, pk):
    _require(request, 'tickets')
    ticket = get_object_or_404(_tickets_for_user(request), pk=pk)
    reason = request.data.get('reason', '').strip()
    if not reason:
        return Response({'error': 'reason required'}, status=400)
    escalate(ticket, reason, actor=f'human:{request.user.pk}')
    return Response(_serialise_ticket(ticket))
