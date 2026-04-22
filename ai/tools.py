"""Tool registry for AI agents.

Each tool is a callable that takes a bound `context` (reseller, conversation,
run) + tool arguments, does its work, and returns a JSON-serialisable dict.

Tools are grouped by agent role in AGENT_TOOLS. The same function can appear
under multiple roles (e.g. send_reply is shared across sales/support/field).

Design rules:
- Every tool validates inputs; failures return `{'error': '...'}` not raise.
- Every tool is safe to retry — side effects are guarded by idempotency keys
  where relevant (Lead.phone unique-per-reseller, Payment reference, etc.).
- No tool bypasses reseller isolation: the `context.reseller` is always the
  subject, arguments cannot widen scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from django.utils import timezone

from accounts.models import Reseller
from conversations.models import Conversation, Message
from conversations.services import record_outbound_message
from leads.models import Lead
from plans.models import ServicePlan
from staff.models import StaffMember
from tickets.models import Ticket
from tickets.services import create_ticket


@dataclass
class ToolContext:
    reseller: Reseller
    conversation: Conversation | None = None
    lead: Lead | None = None
    run_id: int | None = None


ToolFn = Callable[[ToolContext, dict], dict]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_lookup_plans(ctx: ToolContext, args: dict) -> dict:
    qs = ServicePlan.objects.filter(reseller=ctx.reseller, is_active=True).order_by('price_ngn')
    return {
        'plans': [{
            'id': p.pk,
            'name': p.name,
            'price_ngn': str(p.price_ngn),
            'duration_days': p.duration_days,
            'download_mbps': p.download_mbps,
            'upload_mbps': p.upload_mbps,
            'data_cap_gb': p.data_cap_gb,
        } for p in qs[:30]],
    }


def tool_suggest_plan(ctx: ToolContext, args: dict) -> dict:
    """Narrow the catalogue by declared tier or max budget so the model has
    fewer options to pick from."""
    max_price = args.get('max_price_ngn')
    min_speed = args.get('min_download_mbps') or 0
    qs = ServicePlan.objects.filter(reseller=ctx.reseller, is_active=True)
    if max_price:
        try:
            qs = qs.filter(price_ngn__lte=Decimal(str(max_price)))
        except Exception:
            pass
    if min_speed:
        qs = qs.filter(download_mbps__gte=int(min_speed))
    qs = qs.order_by('price_ngn')
    return tool_lookup_plans(ctx, {}) if not qs.exists() else {
        'plans': [{'id': p.pk, 'name': p.name, 'price_ngn': str(p.price_ngn),
                   'download_mbps': p.download_mbps,
                   'duration_days': p.duration_days} for p in qs[:5]],
    }


def tool_create_lead(ctx: ToolContext, args: dict) -> dict:
    phone = (args.get('phone') or '').strip()
    if not phone:
        return {'error': 'phone required'}
    name = (args.get('name') or '').strip()
    intent = args.get('intent') or Lead.INTENT_UNKNOWN
    address = (args.get('address') or '').strip()
    lead, created = Lead.objects.get_or_create(
        reseller=ctx.reseller, phone=phone,
        defaults={'name': name, 'intent': intent, 'address': address,
                  'source': 'ai_sales'},
    )
    if not created:
        # Backfill empties from the new info so we don't lose detail
        dirty = []
        for field, value in (('name', name), ('address', address)):
            if value and not getattr(lead, field):
                setattr(lead, field, value); dirty.append(field)
        if intent and intent != Lead.INTENT_UNKNOWN and lead.intent == Lead.INTENT_UNKNOWN:
            lead.intent = intent; dirty.append('intent')
        if dirty:
            dirty.append('updated_at'); lead.save(update_fields=dirty)
    ctx.lead = lead
    return {'lead_id': lead.pk, 'created': created}


def tool_create_payment_link(ctx: ToolContext, args: dict) -> dict:
    amount = args.get('amount_ngn')
    lead_id = args.get('lead_id') or (ctx.lead.pk if ctx.lead else None)
    if not (amount and lead_id):
        return {'error': 'amount_ngn and lead_id required'}
    try:
        amount_dec = Decimal(str(amount))
    except Exception:
        return {'error': 'amount_ngn must be a number'}
    try:
        lead = Lead.objects.get(pk=lead_id, reseller=ctx.reseller)
    except Lead.DoesNotExist:
        return {'error': 'lead not found'}

    from billing.services import create_lead_payment_link
    try:
        payment, url = create_lead_payment_link(
            lead=lead, amount=amount_dec,
            description=args.get('description', ''),
        )
    except ValueError as exc:
        return {'error': str(exc)}
    lead.status = Lead.STATUS_QUOTED
    lead.quoted_amount_ngn = amount_dec
    lead.save(update_fields=['status', 'quoted_amount_ngn', 'updated_at'])
    return {'payment_id': payment.pk, 'url': url, 'amount_ngn': str(amount_dec)}


def tool_send_reply(ctx: ToolContext, args: dict) -> dict:
    """Record an outbound message on the conversation AND trigger the wire
    send. Agents call this via draft-mode hand-off in the runner; direct
    calls here fire immediately — only invoked when `auto_send_replies` has
    been authorised upstream."""
    if not ctx.conversation:
        return {'error': 'no conversation context'}
    body = (args.get('body') or '').strip()
    if not body:
        return {'error': 'body required'}
    source = args.get('source', Message.SOURCE_AI_SALES)

    msg = record_outbound_message(
        conversation=ctx.conversation, body=body, source=source,
        agent_run_id=ctx.run_id,
    )
    _deliver_outbound(ctx.conversation, body)
    return {'message_id': msg.pk}


def _deliver_outbound(conversation: Conversation, body: str) -> None:
    if conversation.channel == Conversation.CHANNEL_WHATSAPP:
        from notifications.notify import send_whatsapp
        send_whatsapp(conversation.reseller.slug, conversation.external_thread_id, body)
    elif conversation.channel == Conversation.CHANNEL_SMS:
        from notifications.sms import SMSService
        try:
            SMSService().send_sms(conversation.external_thread_id, body)
        except Exception:
            pass


def tool_open_ticket(ctx: ToolContext, args: dict) -> dict:
    subject = (args.get('subject') or '').strip()
    body = (args.get('body') or '').strip()
    ttype = args.get('type', Ticket.TYPE_SUPPORT)
    if not subject:
        return {'error': 'subject required'}
    t = create_ticket(
        reseller=ctx.reseller, type=ttype, subject=subject, body=body,
        conversation=ctx.conversation,
        lead=ctx.lead,
        priority=args.get('priority', Ticket.PRIORITY_NORMAL),
    )
    return {'ticket_id': t.pk, 'sla_due_at': t.sla_due_at.isoformat() if t.sla_due_at else None}


def tool_escalate_to_human(ctx: ToolContext, args: dict) -> dict:
    if not ctx.conversation:
        return {'error': 'no conversation context'}
    reason = (args.get('reason') or 'ai_escalated').strip()
    ctx.conversation.state = Conversation.STATE_PENDING_HUMAN
    ctx.conversation.ai_enabled = False
    ctx.conversation.save(update_fields=['state', 'ai_enabled', 'updated_at'])
    return {'ok': True, 'reason': reason}


def tool_schedule_followup(ctx: ToolContext, args: dict) -> dict:
    """Raise a sales-followup ticket so a human (or the field-supervisor AI)
    picks it up at the requested time. Deferred send is handled by the
    ticket's SLA timer — lighter than scheduling arbitrary RQ jobs."""
    when = args.get('when_iso')
    note = (args.get('note') or '').strip()
    t = create_ticket(
        reseller=ctx.reseller, type=Ticket.TYPE_SALES_FOLLOWUP,
        subject=(note or 'Sales follow-up')[:120],
        body=f'When: {when or "asap"}\n\n{note}',
        conversation=ctx.conversation, lead=ctx.lead,
    )
    return {'ticket_id': t.pk, 'scheduled_for': when}


def tool_lookup_subscriber(ctx: ToolContext, args: dict) -> dict:
    from accounts.models import Subscriber
    phone = (args.get('phone') or '').strip()
    if not phone:
        return {'error': 'phone required'}
    sub = Subscriber.objects.filter(reseller=ctx.reseller, phone=phone).first()
    if not sub:
        return {'found': False}
    return {
        'found': True,
        'subscriber_id': sub.pk,
        'name': '',
        'phone': sub.phone,
        'status': sub.status,
        'plan_id': sub.plan_id,
    }


def tool_check_subscription(ctx: ToolContext, args: dict) -> dict:
    from accounts.models import Subscriber
    from plans.models import Subscription
    sub_id = args.get('subscriber_id')
    if not sub_id:
        return {'error': 'subscriber_id required'}
    try:
        sub = Subscriber.objects.get(pk=sub_id, reseller=ctx.reseller)
    except Subscriber.DoesNotExist:
        return {'error': 'not found'}
    subs = Subscription.objects.filter(subscriber=sub).order_by('-expiry_date')
    active = subs.filter(status='active').first()
    return {
        'has_active_subscription': bool(active),
        'plan_name': active.plan.name if active else '',
        'expiry_date': active.expiry_date.isoformat() if active and active.expiry_date else '',
        'latest_status': (subs.first().status if subs.exists() else ''),
    }


def tool_check_router_status(ctx: ToolContext, args: dict) -> dict:
    from routers.models import Router
    rid = args.get('router_id')
    qs = Router.objects.filter(reseller=ctx.reseller)
    if rid:
        qs = qs.filter(pk=rid)
    out = []
    for r in qs[:10]:
        out.append({
            'id': r.pk, 'name': getattr(r, 'name', '') or '',
            'last_seen': r.last_seen.isoformat() if r.last_seen else None,
            'online': bool(r.last_seen and (timezone.now() - r.last_seen).total_seconds() < 600),
        })
    return {'routers': out}


def tool_check_live_session(ctx: ToolContext, args: dict) -> dict:
    """Query radacct for the subscriber's most recent session. Radacct is an
    unmanaged model — we touch it read-only only."""
    from accounts.models import Subscriber
    from radius.models import Radacct
    sub_id = args.get('subscriber_id')
    try:
        sub = Subscriber.objects.get(pk=sub_id, reseller=ctx.reseller)
    except Subscriber.DoesNotExist:
        return {'error': 'subscriber not found'}
    row = (Radacct.objects.filter(username=sub.phone)
           .order_by('-acctstarttime').first())
    if not row:
        return {'active': False, 'reason': 'no radacct row'}
    active = row.acctstoptime is None
    return {
        'active': active,
        'framed_ip': row.framedipaddress or '',
        'started_at': row.acctstarttime.isoformat() if row.acctstarttime else '',
        'ended_at': row.acctstoptime.isoformat() if row.acctstoptime else '',
    }


def tool_check_recent_payments(ctx: ToolContext, args: dict) -> dict:
    from billing.models import Payment
    sub_id = args.get('subscriber_id')
    days = int(args.get('days') or 30)
    qs = Payment.objects.filter(reseller=ctx.reseller)
    if sub_id:
        qs = qs.filter(subscriber_id=sub_id)
    qs = qs.filter(created_at__gte=timezone.now() - timezone.timedelta(days=days))
    out = []
    for p in qs.order_by('-created_at')[:10]:
        out.append({
            'id': p.pk, 'amount_ngn': str(p.amount_ngn),
            'status': p.paystack_status,
            'reference': p.paystack_reference,
            'created_at': p.created_at.isoformat(),
        })
    return {'payments': out}


def tool_list_available_techs(ctx: ToolContext, args: dict) -> dict:
    area = (args.get('area') or '').strip().lower()
    qs = StaffMember.objects.filter(reseller=ctx.reseller, active=True,
                                    role=StaffMember.ROLE_FIELD_TECH)
    results = []
    for s in qs.order_by('current_load')[:20]:
        covers = s.coverage_areas or []
        if area and covers and not any(area in (c or '').lower() for c in covers):
            continue
        results.append({
            'id': s.pk, 'name': s.name, 'phone': s.phone,
            'whatsapp': s.whatsapp or s.phone,
            'coverage_areas': covers, 'current_load': s.current_load,
        })
    return {'techs': results[:10]}


def tool_assign_ticket(ctx: ToolContext, args: dict) -> dict:
    from tickets.services import assign_ticket
    ticket_id = args.get('ticket_id')
    staff_id = args.get('staff_id')
    if not (ticket_id and staff_id):
        return {'error': 'ticket_id and staff_id required'}
    try:
        ticket = Ticket.objects.get(pk=ticket_id, reseller=ctx.reseller)
        staff = StaffMember.objects.get(pk=staff_id, reseller=ctx.reseller)
    except (Ticket.DoesNotExist, StaffMember.DoesNotExist):
        return {'error': 'ticket or staff not found'}
    assign_ticket(ticket, staff, actor='ai_field',
                  note=args.get('note', '') or '')
    return {'ok': True, 'ticket_id': ticket.pk, 'assigned_to': staff.name}


def tool_send_dispatch_wa(ctx: ToolContext, args: dict) -> dict:
    """Message a field tech on WhatsApp with a dispatch brief AND record the
    outbound message as a tech-kind Conversation so the inbox + future inbound
    routing can correlate the thread to the staff member.

    Stamps `Ticket.dispatch_sent_at` and `last_field_ping_at` so the periodic
    sweep knows when to start pinging.
    """
    staff_id = args.get('staff_id')
    ticket_id = args.get('ticket_id')
    body = (args.get('body') or '').strip()
    if not (staff_id and body):
        return {'error': 'staff_id and body required'}
    try:
        staff = StaffMember.objects.get(pk=staff_id, reseller=ctx.reseller)
    except StaffMember.DoesNotExist:
        return {'error': 'staff not found'}

    target = staff.whatsapp or staff.phone
    if not target:
        return {'error': 'staff has no whatsapp/phone'}

    # Record (or look up) the tech-kind Conversation for this staff member
    conv, _ = Conversation.objects.get_or_create(
        reseller=ctx.reseller,
        channel=Conversation.CHANNEL_WHATSAPP,
        external_thread_id=target,
        defaults={
            'kind': Conversation.KIND_TECH,
            'assigned_staff': staff,
            'contact_phone': target,
            'contact_display_name': staff.name,
            'state': Conversation.STATE_OPEN,
        },
    )
    # Defensive: existing convos may have been customer-kind by accident.
    needs_save = []
    if conv.kind != Conversation.KIND_TECH:
        conv.kind = Conversation.KIND_TECH
        needs_save.append('kind')
    if conv.assigned_staff_id != staff.pk:
        conv.assigned_staff = staff
        needs_save.append('assigned_staff')
    if needs_save:
        needs_save.append('updated_at')
        conv.save(update_fields=needs_save)

    record_outbound_message(
        conversation=conv, body=body, source=Message.SOURCE_AI_FIELD,
        agent_run_id=ctx.run_id,
    )
    from notifications.notify import send_whatsapp
    ok = send_whatsapp(ctx.reseller.slug, target, body)

    # Update the ticket's dispatch tracking so the periodic sweep starts
    # counting from now.
    if ticket_id:
        try:
            t = Ticket.objects.get(pk=ticket_id, reseller=ctx.reseller)
        except Ticket.DoesNotExist:
            t = None
        if t is not None:
            from django.utils import timezone
            now = timezone.now()
            t.dispatch_sent_at = t.dispatch_sent_at or now
            t.last_field_ping_at = now
            t.save(update_fields=['dispatch_sent_at', 'last_field_ping_at',
                                  'updated_at'])
    return {'ok': bool(ok), 'to': target, 'conversation_id': conv.pk}


def tool_close_ticket(ctx: ToolContext, args: dict) -> dict:
    from tickets.services import change_status
    ticket_id = args.get('ticket_id')
    note = (args.get('note') or '').strip()
    try:
        ticket = Ticket.objects.get(pk=ticket_id, reseller=ctx.reseller)
    except Ticket.DoesNotExist:
        return {'error': 'ticket not found'}
    change_status(ticket, Ticket.STATUS_RESOLVED, actor='ai_field', note=note)
    return {'ok': True, 'ticket_id': ticket.pk}


# ---------------------------------------------------------------------------
# Diagnostics — Support agent reads telemetry through these
# ---------------------------------------------------------------------------

def tool_get_subscriber_router(ctx: ToolContext, args: dict) -> dict:
    """Identify which Router this subscriber currently connects through."""
    from accounts.models import Subscriber
    from ai.diagnostics import lookup_subscriber_router
    sub_id = args.get('subscriber_id')
    try:
        sub = Subscriber.objects.get(pk=sub_id, reseller=ctx.reseller)
    except Subscriber.DoesNotExist:
        return {'error': 'subscriber not found'}
    router = lookup_subscriber_router(sub)
    if router is None:
        return {'found': False}
    return {
        'found': True,
        'router_id': router.pk,
        'name': getattr(router, 'name', '') or '',
        'service_mode': router.service_mode,
        'status': router.status,
        'last_seen': router.last_seen.isoformat() if router.last_seen else None,
    }


def tool_check_general_outage(ctx: ToolContext, args: dict) -> dict:
    """Is this reseller's router currently down? (general outage)"""
    from ai.diagnostics import is_router_currently_offline
    rid = args.get('router_id')
    try:
        router = Router.objects.get(pk=rid, reseller=ctx.reseller)
    except Router.DoesNotExist:
        return {'error': 'router not found'}
    return {
        'offline': is_router_currently_offline(router),
        'status': router.status,
        'offline_since': router.offline_since.isoformat() if router.offline_since else None,
    }


def tool_infer_customer_type(ctx: ToolContext, args: dict) -> dict:
    """Determine PPPoE vs hotspot vs unknown for the customer."""
    from accounts.models import Subscriber
    from ai.diagnostics import infer_customer_type
    sub_id = args.get('subscriber_id')
    rid = args.get('router_id')
    try:
        sub = Subscriber.objects.get(pk=sub_id, reseller=ctx.reseller)
    except Subscriber.DoesNotExist:
        return {'error': 'subscriber not found'}
    router = None
    if rid:
        try:
            router = Router.objects.get(pk=rid, reseller=ctx.reseller)
        except Router.DoesNotExist:
            router = None
    return {'customer_type': infer_customer_type(sub, router)}


def tool_categorise_diagnosis(ctx: ToolContext, args: dict) -> dict:
    """Aggregate read-only facts into a (cause, action) pair the Support
    agent can hand straight to `open_ticket`. Args may include any subset of
    the DiagnosticFacts fields (subscriber_id, router_id, customer_clue, etc.).
    """
    from accounts.models import Subscriber
    from ai.diagnostics import (
        DiagnosticFacts, categorise_cause, infer_customer_type,
        is_router_currently_offline,
    )
    from plans.models import Subscription
    from billing.models import Payment
    from radius.models import Radacct

    facts = DiagnosticFacts(
        subscriber_id=args.get('subscriber_id'),
        router_id=args.get('router_id'),
        customer_clue=(args.get('customer_clue') or '').strip(),
    )

    if facts.subscriber_id:
        try:
            sub = Subscriber.objects.get(pk=facts.subscriber_id, reseller=ctx.reseller)
        except Subscriber.DoesNotExist:
            sub = None
        if sub is not None:
            active = Subscription.objects.filter(subscriber=sub, status='active').first()
            facts.subscription_active = bool(active)
            latest = (Subscription.objects.filter(subscriber=sub)
                      .order_by('-expiry_date').first())
            if latest is not None:
                facts.subscription_expired = (latest.status == 'expired')
            last_pay = (Payment.objects.filter(reseller=ctx.reseller, subscriber=sub)
                        .order_by('-created_at').first())
            if last_pay is not None:
                facts.last_payment_status = last_pay.paystack_status or ''
            row = (Radacct.objects.filter(username=sub.phone, acctstoptime__isnull=True)
                   .order_by('-acctstarttime').first())
            facts.has_live_session = row is not None
            facts.customer_type = infer_customer_type(
                sub,
                Router.objects.filter(pk=facts.router_id, reseller=ctx.reseller).first()
                if facts.router_id else None,
            )

    if facts.router_id:
        router = Router.objects.filter(pk=facts.router_id, reseller=ctx.reseller).first()
        if router is not None:
            facts.router_offline = is_router_currently_offline(router)

    cause, action = categorise_cause(facts)
    return {'cause': cause, 'action': action, 'facts': facts.as_dict()}


# ---------------------------------------------------------------------------
# Renewal payment links — Support sends these without escalation
# ---------------------------------------------------------------------------

def tool_create_renewal_payment_link(ctx: ToolContext, args: dict) -> dict:
    from accounts.models import Subscriber
    from plans.models import ServicePlan
    from billing.services import create_renewal_payment_link
    sub_id = args.get('subscriber_id')
    try:
        sub = Subscriber.objects.get(pk=sub_id, reseller=ctx.reseller)
    except Subscriber.DoesNotExist:
        return {'error': 'subscriber not found'}
    plan = None
    if args.get('plan_id'):
        plan = ServicePlan.objects.filter(pk=args['plan_id'], reseller=ctx.reseller).first()
    try:
        payment, url = create_renewal_payment_link(
            subscriber=sub, plan=plan, amount=args.get('amount_ngn'),
            description=args.get('description', '') or '',
        )
    except ValueError as exc:
        return {'error': str(exc)}
    return {'payment_id': payment.pk, 'url': url,
            'amount_ngn': str(payment.amount_ngn)}


# ---------------------------------------------------------------------------
# Ticket lookup + status confirmation flow (used by FieldInboundAgent)
# ---------------------------------------------------------------------------

def tool_lookup_ticket_for_tech(ctx: ToolContext, args: dict) -> dict:
    """Find non-terminal tickets a tech might be referring to.

    Strategies (any of):
      - explicit ticket_id (looked up directly)
      - phone — find tickets where subscriber.phone or lead.phone matches
      - email — likewise on email
      - default — tickets currently assigned to this tech (caller passes
        `assigned_staff_id`)
    Returns up to 5 with a one-line summary.
    """
    from accounts.models import Subscriber
    from leads.models import Lead

    ticket_id = args.get('ticket_id')
    phone = (args.get('phone') or '').strip()
    email = (args.get('email') or '').strip()
    assigned_staff_id = args.get('assigned_staff_id')

    qs = Ticket.objects.filter(reseller=ctx.reseller).exclude(
        status__in=Ticket.TERMINAL_STATUSES,
    ).select_related('subscriber', 'lead', 'assigned_staff')

    if ticket_id:
        qs = qs.filter(pk=ticket_id)
    elif phone:
        sub_ids = list(Subscriber.objects.filter(
            reseller=ctx.reseller, phone__icontains=phone[-7:],
        ).values_list('pk', flat=True))
        lead_ids = list(Lead.objects.filter(
            reseller=ctx.reseller, phone__icontains=phone[-7:],
        ).values_list('pk', flat=True))
        from django.db.models import Q
        qs = qs.filter(Q(subscriber_id__in=sub_ids) | Q(lead_id__in=lead_ids))
    elif email:
        sub_ids = list(Subscriber.objects.filter(
            reseller=ctx.reseller, email__iexact=email,
        ).values_list('pk', flat=True))
        lead_ids = list(Lead.objects.filter(
            reseller=ctx.reseller, email__iexact=email,
        ).values_list('pk', flat=True))
        from django.db.models import Q
        qs = qs.filter(Q(subscriber_id__in=sub_ids) | Q(lead_id__in=lead_ids))
    elif assigned_staff_id:
        qs = qs.filter(assigned_staff_id=assigned_staff_id)
    else:
        return {'matches': [], 'reason': 'no lookup key supplied'}

    matches = []
    for t in qs.order_by('-dispatch_sent_at', '-created_at')[:5]:
        contact_name = ''
        contact_phone = ''
        if t.subscriber_id:
            contact_name = ''  # Subscriber model has no name field today
            contact_phone = t.subscriber.phone or ''
        elif t.lead_id:
            contact_name = t.lead.name or ''
            contact_phone = t.lead.phone or ''
        matches.append({
            'ticket_id': t.pk,
            'status': t.status,
            'subject': t.subject,
            'cause': t.diagnosed_cause,
            'contact_name': contact_name,
            'contact_phone': contact_phone,
            'dispatch_sent_at': t.dispatch_sent_at.isoformat() if t.dispatch_sent_at else None,
        })
    return {'matches': matches}


def tool_set_pending_close_action(ctx: ToolContext, args: dict) -> dict:
    """Stash a pending status-change confirmation on the ticket. Cleared
    when the tech replies YES/NO or the AI clears it."""
    from django.utils import timezone
    ticket_id = args.get('ticket_id')
    expected_status = args.get('expected_status')
    valid = dict(Ticket.STATUS_CHOICES)
    if expected_status not in valid:
        return {'error': 'invalid expected_status'}
    try:
        t = Ticket.objects.get(pk=ticket_id, reseller=ctx.reseller)
    except Ticket.DoesNotExist:
        return {'error': 'ticket not found'}
    t.pending_close_action = {
        'action': args.get('action', 'change_status'),
        'expected_status': expected_status,
        'asked_at': timezone.now().isoformat(),
        'by_run_id': ctx.run_id,
    }
    t.save(update_fields=['pending_close_action', 'updated_at'])
    return {'ok': True, 'ticket_id': t.pk}


def tool_consume_pending_close_action(ctx: ToolContext, args: dict) -> dict:
    """Read + clear the pending action. Returns the snapshot for the agent
    to reason over before calling change_status_ticket."""
    ticket_id = args.get('ticket_id')
    try:
        t = Ticket.objects.get(pk=ticket_id, reseller=ctx.reseller)
    except Ticket.DoesNotExist:
        return {'error': 'ticket not found'}
    snapshot = dict(t.pending_close_action or {})
    if snapshot:
        t.pending_close_action = {}
        t.save(update_fields=['pending_close_action', 'updated_at'])
    return {'ok': True, 'pending': snapshot}


def tool_change_status_ticket(ctx: ToolContext, args: dict) -> dict:
    """Wrap tickets.services.change_status with actor='ai_field'."""
    from tickets.services import change_status
    ticket_id = args.get('ticket_id')
    new_status = args.get('new_status')
    note = (args.get('note') or '').strip()
    valid = dict(Ticket.STATUS_CHOICES)
    if new_status not in valid:
        return {'error': 'invalid new_status'}
    try:
        t = Ticket.objects.get(pk=ticket_id, reseller=ctx.reseller)
    except Ticket.DoesNotExist:
        return {'error': 'ticket not found'}
    change_status(t, new_status, actor='ai_field', note=note)
    return {'ok': True, 'ticket_id': t.pk, 'status': new_status}


def tool_add_ticket_comment(ctx: ToolContext, args: dict) -> dict:
    from tickets.services import add_comment
    ticket_id = args.get('ticket_id')
    note = (args.get('note') or '').strip()
    if not note:
        return {'error': 'note required'}
    try:
        t = Ticket.objects.get(pk=ticket_id, reseller=ctx.reseller)
    except Ticket.DoesNotExist:
        return {'error': 'ticket not found'}
    add_comment(t, actor='ai_field', note=note,
                metadata=args.get('metadata') or {})
    return {'ok': True, 'ticket_id': t.pk}


# ---------------------------------------------------------------------------
# Per-agent tool sets + JSON-schemas for the LLM
# ---------------------------------------------------------------------------

_SCHEMAS = {
    'lookup_plans': {
        'name': 'lookup_plans',
        'description': "Return this reseller's active service plans catalogue.",
        'parameters': {'type': 'object', 'properties': {}, 'required': []},
    },
    'suggest_plan': {
        'name': 'suggest_plan',
        'description': 'Filter plans by budget or minimum speed and return up to 5 suggestions.',
        'parameters': {'type': 'object', 'properties': {
            'max_price_ngn': {'type': 'number'},
            'min_download_mbps': {'type': 'integer'},
        }, 'required': []},
    },
    'create_lead': {
        'name': 'create_lead',
        'description': 'Create or update a Lead for this reseller keyed on phone.',
        'parameters': {'type': 'object', 'properties': {
            'phone': {'type': 'string'},
            'name': {'type': 'string'},
            'address': {'type': 'string'},
            'intent': {'type': 'string',
                       'enum': ['unknown', 'home', 'cluster', 'technical', 'other']},
        }, 'required': ['phone']},
    },
    'create_payment_link': {
        'name': 'create_payment_link',
        'description': 'Generate a Paystack payment link for the lead.',
        'parameters': {'type': 'object', 'properties': {
            'lead_id': {'type': 'integer'},
            'amount_ngn': {'type': 'number'},
            'description': {'type': 'string'},
        }, 'required': ['amount_ngn']},
    },
    'send_reply': {
        'name': 'send_reply',
        'description': 'Send a message to the customer on this conversation.',
        'parameters': {'type': 'object', 'properties': {
            'body': {'type': 'string'},
        }, 'required': ['body']},
    },
    'open_ticket': {
        'name': 'open_ticket',
        'description': 'Open a ticket for human follow-up.',
        'parameters': {'type': 'object', 'properties': {
            'subject': {'type': 'string'},
            'body': {'type': 'string'},
            'type': {'type': 'string',
                     'enum': ['sales_followup', 'install', 'support', 'billing', 'other']},
            'priority': {'type': 'string',
                         'enum': ['low', 'normal', 'high', 'urgent']},
        }, 'required': ['subject']},
    },
    'escalate_to_human': {
        'name': 'escalate_to_human',
        'description': 'Disable AI on this conversation and flag for human attention.',
        'parameters': {'type': 'object', 'properties': {
            'reason': {'type': 'string'},
        }, 'required': ['reason']},
    },
    'schedule_followup': {
        'name': 'schedule_followup',
        'description': 'Schedule a sales follow-up ticket for later action.',
        'parameters': {'type': 'object', 'properties': {
            'when_iso': {'type': 'string'},
            'note': {'type': 'string'},
        }, 'required': []},
    },
    'list_available_techs': {
        'name': 'list_available_techs',
        'description': 'Return active field-tech staff ordered by current load, optionally filtered by coverage area.',
        'parameters': {'type': 'object', 'properties': {
            'area': {'type': 'string'},
        }, 'required': []},
    },
    'lookup_subscriber': {
        'name': 'lookup_subscriber',
        'description': 'Find a subscriber on this reseller by phone number.',
        'parameters': {'type': 'object', 'properties': {
            'phone': {'type': 'string'},
        }, 'required': ['phone']},
    },
    'check_subscription': {
        'name': 'check_subscription',
        'description': 'Return active-subscription status + expiry for a subscriber.',
        'parameters': {'type': 'object', 'properties': {
            'subscriber_id': {'type': 'integer'},
        }, 'required': ['subscriber_id']},
    },
    'check_router_status': {
        'name': 'check_router_status',
        'description': "Return online / offline state for one or all of the reseller's routers.",
        'parameters': {'type': 'object', 'properties': {
            'router_id': {'type': 'integer'},
        }, 'required': []},
    },
    'check_live_session': {
        'name': 'check_live_session',
        'description': 'Return whether the subscriber has an active RADIUS session right now.',
        'parameters': {'type': 'object', 'properties': {
            'subscriber_id': {'type': 'integer'},
        }, 'required': ['subscriber_id']},
    },
    'check_recent_payments': {
        'name': 'check_recent_payments',
        'description': 'Return this subscriber\'s recent payments (status + amount).',
        'parameters': {'type': 'object', 'properties': {
            'subscriber_id': {'type': 'integer'},
            'days': {'type': 'integer'},
        }, 'required': []},
    },
    'assign_ticket': {
        'name': 'assign_ticket',
        'description': 'Assign a ticket to a staff member.',
        'parameters': {'type': 'object', 'properties': {
            'ticket_id': {'type': 'integer'},
            'staff_id': {'type': 'integer'},
            'note': {'type': 'string'},
        }, 'required': ['ticket_id', 'staff_id']},
    },
    'send_dispatch_wa': {
        'name': 'send_dispatch_wa',
        'description': 'Send a WhatsApp dispatch brief directly to a field tech AND record it as a tech-kind conversation. Pass ticket_id so dispatch tracking is updated.',
        'parameters': {'type': 'object', 'properties': {
            'staff_id': {'type': 'integer'},
            'ticket_id': {'type': 'integer'},
            'body': {'type': 'string'},
        }, 'required': ['staff_id', 'body']},
    },
    'close_ticket': {
        'name': 'close_ticket',
        'description': 'Mark a ticket as resolved with a short note.',
        'parameters': {'type': 'object', 'properties': {
            'ticket_id': {'type': 'integer'},
            'note': {'type': 'string'},
        }, 'required': ['ticket_id']},
    },
    'get_subscriber_router': {
        'name': 'get_subscriber_router',
        'description': "Identify which Router this subscriber currently connects through (via the active RADIUS session).",
        'parameters': {'type': 'object', 'properties': {
            'subscriber_id': {'type': 'integer'},
        }, 'required': ['subscriber_id']},
    },
    'check_general_outage': {
        'name': 'check_general_outage',
        'description': 'Is this router currently offline? (general outage detection)',
        'parameters': {'type': 'object', 'properties': {
            'router_id': {'type': 'integer'},
        }, 'required': ['router_id']},
    },
    'infer_customer_type': {
        'name': 'infer_customer_type',
        'description': "Determine whether the customer is on PPPoE, hotspot, or unknown.",
        'parameters': {'type': 'object', 'properties': {
            'subscriber_id': {'type': 'integer'},
            'router_id': {'type': 'integer'},
        }, 'required': ['subscriber_id']},
    },
    'categorise_diagnosis': {
        'name': 'categorise_diagnosis',
        'description': "Aggregate read-only diagnostic facts into a (cause, action) pair. Pass any hardware clue from the customer (e.g. 'PON light blinking red') as customer_clue.",
        'parameters': {'type': 'object', 'properties': {
            'subscriber_id': {'type': 'integer'},
            'router_id': {'type': 'integer'},
            'customer_clue': {'type': 'string'},
        }, 'required': []},
    },
    'create_renewal_payment_link': {
        'name': 'create_renewal_payment_link',
        'description': "Generate a Paystack renewal payment link for an existing subscriber. Subject to the same auto_quote_below_ngn cap as new payment links.",
        'parameters': {'type': 'object', 'properties': {
            'subscriber_id': {'type': 'integer'},
            'plan_id': {'type': 'integer'},
            'amount_ngn': {'type': 'number'},
            'description': {'type': 'string'},
        }, 'required': ['subscriber_id']},
    },
    'lookup_ticket_for_tech': {
        'name': 'lookup_ticket_for_tech',
        'description': "Find non-terminal tickets a tech might be referring to. Use ticket_id, phone, email, or assigned_staff_id.",
        'parameters': {'type': 'object', 'properties': {
            'ticket_id': {'type': 'integer'},
            'phone': {'type': 'string'},
            'email': {'type': 'string'},
            'assigned_staff_id': {'type': 'integer'},
        }, 'required': []},
    },
    'set_pending_close_action': {
        'name': 'set_pending_close_action',
        'description': "Stash a pending status-change confirmation on the ticket. The next inbound from the tech (YES/NO) consumes it.",
        'parameters': {'type': 'object', 'properties': {
            'ticket_id': {'type': 'integer'},
            'action': {'type': 'string'},
            'expected_status': {'type': 'string',
                                'enum': ['in_progress', 'resolved', 'awaiting_customer', 'closed']},
        }, 'required': ['ticket_id', 'expected_status']},
    },
    'consume_pending_close_action': {
        'name': 'consume_pending_close_action',
        'description': "Read + clear any pending close-action snapshot stored on this ticket.",
        'parameters': {'type': 'object', 'properties': {
            'ticket_id': {'type': 'integer'},
        }, 'required': ['ticket_id']},
    },
    'change_status_ticket': {
        'name': 'change_status_ticket',
        'description': "Change a ticket's status. Use ONLY after the tech has confirmed via YES.",
        'parameters': {'type': 'object', 'properties': {
            'ticket_id': {'type': 'integer'},
            'new_status': {'type': 'string',
                           'enum': ['in_progress', 'resolved',
                                    'awaiting_customer', 'closed']},
            'note': {'type': 'string'},
        }, 'required': ['ticket_id', 'new_status']},
    },
    'add_ticket_comment': {
        'name': 'add_ticket_comment',
        'description': 'Add a free-text comment to a ticket (audit trail only — does not message the customer).',
        'parameters': {'type': 'object', 'properties': {
            'ticket_id': {'type': 'integer'},
            'note': {'type': 'string'},
            'metadata': {'type': 'object'},
        }, 'required': ['ticket_id', 'note']},
    },
}


TOOL_FNS: dict[str, ToolFn] = {
    'lookup_plans': tool_lookup_plans,
    'suggest_plan': tool_suggest_plan,
    'create_lead': tool_create_lead,
    'create_payment_link': tool_create_payment_link,
    'send_reply': tool_send_reply,
    'open_ticket': tool_open_ticket,
    'escalate_to_human': tool_escalate_to_human,
    'schedule_followup': tool_schedule_followup,
    'list_available_techs': tool_list_available_techs,
    'lookup_subscriber': tool_lookup_subscriber,
    'check_subscription': tool_check_subscription,
    'check_router_status': tool_check_router_status,
    'check_live_session': tool_check_live_session,
    'check_recent_payments': tool_check_recent_payments,
    'assign_ticket': tool_assign_ticket,
    'send_dispatch_wa': tool_send_dispatch_wa,
    'close_ticket': tool_close_ticket,
    # Phase 2b — diagnostics + ticket lifecycle
    'get_subscriber_router': tool_get_subscriber_router,
    'check_general_outage': tool_check_general_outage,
    'infer_customer_type': tool_infer_customer_type,
    'categorise_diagnosis': tool_categorise_diagnosis,
    'create_renewal_payment_link': tool_create_renewal_payment_link,
    'lookup_ticket_for_tech': tool_lookup_ticket_for_tech,
    'set_pending_close_action': tool_set_pending_close_action,
    'consume_pending_close_action': tool_consume_pending_close_action,
    'change_status_ticket': tool_change_status_ticket,
    'add_ticket_comment': tool_add_ticket_comment,
}


AGENT_TOOLS = {
    'sales': ['lookup_plans', 'suggest_plan', 'create_lead',
              'create_payment_link', 'schedule_followup',
              'send_reply', 'escalate_to_human'],
    'support': ['lookup_subscriber', 'check_subscription',
                'check_router_status', 'check_live_session',
                'check_recent_payments',
                # Phase 2b: full diagnostic chain + renewal links
                'get_subscriber_router', 'check_general_outage',
                'infer_customer_type', 'categorise_diagnosis',
                'create_renewal_payment_link',
                'send_reply', 'open_ticket', 'escalate_to_human'],
    # Field-supervisor (propose-only — assign_ticket dropped):
    'field':  ['list_available_techs',
               'send_dispatch_wa', 'add_ticket_comment',
               'send_reply', 'escalate_to_human'],
    # Field-inbound: tech replies on WA. Confirm-before-action pattern.
    'field_inbound': ['lookup_ticket_for_tech',
                      'set_pending_close_action',
                      'consume_pending_close_action',
                      'change_status_ticket', 'add_ticket_comment',
                      'send_reply', 'escalate_to_human'],
}


def tool_schemas_for(role: str) -> list[dict]:
    names = AGENT_TOOLS.get(role, [])
    return [_SCHEMAS[n] for n in names if n in _SCHEMAS]


def run_tool(name: str, ctx: ToolContext, args: dict) -> dict:
    fn = TOOL_FNS.get(name)
    if not fn:
        return {'error': f'unknown tool: {name}'}
    try:
        return fn(ctx, args or {}) or {}
    except Exception as exc:  # defensive — never crash the agent loop
        return {'error': f'{type(exc).__name__}: {exc}'}
