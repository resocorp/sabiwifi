"""
Operator-only CRM & KPI views — cross-tenant insight into conversations,
leads, tickets, installs + the two north-star KPIs.

Two KPIs drive every design choice:
  1. payment_to_service_time   — paid → service delivered
  2. report_to_resolution_time — issue reported → confirmed resolved
"""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Avg, Count, F, Q
from django.db.models.functions import Extract
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from accounts.models import Reseller
from conversations.models import Conversation
from leads.models import InstallationOrder, Lead
from tickets.models import Ticket


staff_required = user_passes_test(lambda u: u.is_staff, login_url='/login/')


@login_required(login_url='/login/')
@staff_required
def operator_tickets(request):
    """Cross-tenant ticket board with SLA breach filtering."""
    qs = Ticket.objects.select_related('reseller', 'assigned_staff').order_by('-created_at')

    reseller_slug = request.GET.get('reseller')
    if reseller_slug:
        qs = qs.filter(reseller__slug=reseller_slug)
    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)
    ttype = request.GET.get('type')
    if ttype:
        qs = qs.filter(type=ttype)
    if request.GET.get('breached') == '1':
        qs = qs.filter(sla_due_at__lt=timezone.now()).exclude(
            status__in=Ticket.TERMINAL_STATUSES,
        )

    tickets = list(qs[:200])
    resellers = Reseller.objects.order_by('name')
    return render(request, 'operator/tickets.html', {
        'tickets': tickets,
        'resellers': resellers,
        'filters': {
            'reseller': reseller_slug or '',
            'status': status or '',
            'type': ttype or '',
            'breached': request.GET.get('breached') == '1',
        },
    })


@login_required(login_url='/login/')
@staff_required
def operator_conversations(request):
    """Cross-tenant inbox view — unanswered messages, by reseller."""
    qs = Conversation.objects.select_related('reseller').order_by('-last_message_at')
    reseller_slug = request.GET.get('reseller')
    if reseller_slug:
        qs = qs.filter(reseller__slug=reseller_slug)
    state = request.GET.get('state')
    if state:
        qs = qs.filter(state=state)
    convos = list(qs[:200])
    return render(request, 'operator/conversations.html', {
        'conversations': convos,
        'resellers': Reseller.objects.order_by('name'),
        'filters': {'reseller': reseller_slug or '', 'state': state or ''},
    })


@login_required(login_url='/login/')
@staff_required
def operator_conversation_detail(request, conversation_id):
    """Thread view + composer + Paystack quick-quote action."""
    from django.shortcuts import get_object_or_404
    convo = get_object_or_404(
        Conversation.objects.select_related('reseller', 'lead', 'subscriber'),
        pk=conversation_id,
    )
    messages = list(
        convo.messages.order_by('created_at').select_related('sent_by')
    )
    return render(request, 'operator/conversation_detail.html', {
        'conversation': convo,
        'messages_thread': messages,
        'lead': convo.lead,
        'subscriber': convo.subscriber,
    })


@login_required(login_url='/login/')
@staff_required
def operator_send_quote(request, lead_id):
    """
    Generate a Paystack link for `lead` and post it as an outbound WhatsApp
    message in the lead's conversation thread (creating the conversation if
    absent). One-click action used from the operator inbox.

    Body: {amount_ngn, description?, conversation_id?}
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    from django.shortcuts import get_object_or_404
    import json
    from decimal import Decimal, InvalidOperation

    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    lead = get_object_or_404(Lead, pk=lead_id)
    if not lead.reseller:
        return JsonResponse({
            'error': 'This lead has no partner assigned. Assign a partner before quoting (the Paystack split needs a verified subaccount).',
        }, status=400)

    # Auto-fill from chosen_plan if amount not supplied + lead picked a plan
    raw_amount = data.get('amount_ngn') or lead.quoted_amount_ngn
    if not raw_amount and lead.chosen_plan_id:
        raw_amount = lead.chosen_plan.effective_upfront_price_ngn
    if not raw_amount:
        return JsonResponse({'error': 'amount_ngn required'}, status=400)
    try:
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, TypeError):
        return JsonResponse({'error': 'Invalid amount'}, status=400)
    if amount <= 0:
        return JsonResponse({'error': 'Amount must be positive'}, status=400)

    intent_label = dict(Lead.INTENT_CHOICES).get(lead.intent, 'Service')
    description = (data.get('description') or f'Installation fee for {intent_label}').strip()

    # Generate Paystack link via existing service
    from billing.services import create_lead_payment_link
    try:
        payment, hosted_url = create_lead_payment_link(
            lead=lead, amount=amount, description=description,
        )
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    # Resolve / create conversation for this lead (WhatsApp by default).
    conversation = None
    convo_id = data.get('conversation_id')
    if convo_id:
        conversation = Conversation.objects.filter(
            pk=convo_id, lead=lead,
        ).first()
    if conversation is None:
        conversation = lead.conversations.order_by('-last_message_at').first()
    if conversation is None:
        # No prior thread — start a WA conversation keyed by phone.
        conversation = Conversation.objects.create(
            reseller=lead.reseller,
            lead=lead,
            channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id=lead.phone,
            contact_phone=lead.phone,
            contact_display_name=lead.name or '',
            state=Conversation.STATE_OPEN,
        )

    # Compose the WhatsApp message
    name = lead.name.split()[0] if lead.name else 'there'
    message_body = (
        f'Hi {name}, here is your payment link for {description}:\n'
        f'₦{amount:,} — pay here: {hosted_url}\n\n'
        f'Once we receive payment, we will schedule your installation.'
    )

    # Record outbound + send via WA sidecar
    from conversations.services import record_outbound_message
    from conversations.models import Message
    from notifications.notify import send_whatsapp

    msg = record_outbound_message(
        conversation=conversation,
        body=message_body,
        source=Message.SOURCE_HUMAN,
    )
    sent_ok = False
    try:
        sent_ok = bool(send_whatsapp(lead.reseller.slug, lead.phone, message_body))
    except Exception:
        sent_ok = False
    msg.delivery_status = Message.DELIVERY_QUEUED if sent_ok else Message.DELIVERY_FAILED
    msg.save(update_fields=['delivery_status'])

    # Update lead status
    if lead.status in (Lead.STATUS_NEW, Lead.STATUS_QUALIFIED):
        lead.status = Lead.STATUS_QUOTED
    lead.quoted_amount_ngn = amount
    lead.save(update_fields=['status', 'quoted_amount_ngn', 'updated_at'])

    return JsonResponse({
        'success': True,
        'payment_id': payment.pk,
        'hosted_url': hosted_url,
        'message_id': msg.pk,
        'conversation_id': conversation.pk,
        'sent_ok': sent_ok,
    })


@login_required(login_url='/login/')
@staff_required
def operator_lead_detail(request, lead_id):
    """Lead detail page — used when a lead has no conversation yet (fresh
    intake from the public form). Surfaces the same actions: send a quote,
    open WhatsApp, view linked conversations."""
    from django.shortcuts import get_object_or_404
    lead = get_object_or_404(
        Lead.objects.select_related('reseller', 'assigned_staff'),
        pk=lead_id,
    )
    convos = list(lead.conversations.all().order_by('-last_message_at'))
    return render(request, 'operator/lead_detail.html', {
        'lead': lead,
        'conversations': convos,
        'resellers': Reseller.objects.order_by('name'),
    })


@login_required(login_url='/login/')
@staff_required
def operator_kpis(request):
    """HTML shell page; data is fetched from /operator/api/kpis/."""
    return render(request, 'operator/kpis.html', {
        'resellers': Reseller.objects.order_by('name'),
    })


@login_required(login_url='/login/')
@staff_required
def operator_kpis_api(request):
    """JSON endpoint driving the KPI charts.

    Returns median + p90 for both north-star KPIs, per reseller and overall,
    plus queue-health breakdown and a per-type SLA breach count.
    """
    window_days = int(request.GET.get('days', 30))
    since = timezone.now() - timezone.timedelta(days=window_days)
    reseller_slug = request.GET.get('reseller')

    reseller_filter = Q()
    if reseller_slug:
        reseller_filter = Q(reseller__slug=reseller_slug)

    # ── report_to_resolution_time (tickets with resolved_at)
    resolved = Ticket.objects.filter(
        reseller_filter,
        resolved_at__isnull=False,
        created_at__gte=since,
    ).annotate(
        resolution_seconds=Extract(
            F('resolved_at') - F('created_at'), 'epoch',
        ),
    )
    resolution = _percentiles(resolved.values_list('resolution_seconds', flat=True))
    by_type = []
    for row in resolved.values('type').annotate(
        n=Count('id'), avg=Avg('resolution_seconds'),
    ).order_by('type'):
        by_type.append({
            'type': row['type'],
            'count': row['n'],
            'avg_seconds': int(row['avg'] or 0),
        })

    # ── payment_to_service_time (InstallationOrder, payment.created_at → completed_at)
    completed_installs = InstallationOrder.objects.filter(
        reseller_filter,
        completed_at__isnull=False,
        payment__created_at__gte=since,
    ).annotate(
        paid_to_service_seconds=Extract(
            F('completed_at') - F('payment__created_at'), 'epoch',
        ),
    )
    service_time = _percentiles(
        completed_installs.values_list('paid_to_service_seconds', flat=True),
    )

    # ── SLA breaches currently open
    breached_open = Ticket.objects.filter(
        reseller_filter,
        sla_due_at__lt=timezone.now(),
    ).exclude(status__in=Ticket.TERMINAL_STATUSES).count()

    # ── Lead funnel snapshot
    funnel = dict(
        Lead.objects.filter(reseller_filter, created_at__gte=since)
        .values_list('status').annotate(n=Count('id'))
    )

    # ── Richer ticket KPIs (shares the reseller-dashboard computation so the
    # operator and per-reseller views stay consistent). Pass reseller=None to
    # aggregate platform-wide when no slug filter is applied.
    from tickets.services import compute_kpis
    scoped_reseller = None
    if reseller_slug:
        scoped_reseller = Reseller.objects.filter(slug=reseller_slug).first()
    ticket_kpis = compute_kpis(reseller=scoped_reseller, days=window_days)

    return JsonResponse({
        'window_days': window_days,
        'reseller': reseller_slug or 'all',
        'report_to_resolution': resolution,
        'resolution_by_type': by_type,
        'payment_to_service': service_time,
        'breached_open_tickets': breached_open,
        'lead_funnel': funnel,
        'ticket_kpis': ticket_kpis,
    })


def _percentiles(values):
    """Return {'n', 'median_seconds', 'p90_seconds', 'avg_seconds'} from a raw list."""
    data = sorted(int(v) for v in values if v is not None)
    n = len(data)
    if n == 0:
        return {'n': 0, 'median_seconds': None, 'p90_seconds': None, 'avg_seconds': None}

    def pct(p):
        idx = max(0, min(n - 1, int(round((p / 100.0) * (n - 1)))))
        return data[idx]

    return {
        'n': n,
        'median_seconds': pct(50),
        'p90_seconds': pct(90),
        'avg_seconds': int(sum(data) / n),
    }


@login_required(login_url='/login/')
@staff_required
def operator_queue_health(request):
    """Queue depth + failure rate snapshot for the RQ dashboard card."""
    import django_rq
    data = {'queues': []}
    for name in ('ai', 'default', 'low'):
        try:
            q = django_rq.get_queue(name)
            registry_failed = q.failed_job_registry
            data['queues'].append({
                'name': name,
                'depth': q.count,
                'failed': registry_failed.count,
                'scheduled': q.scheduled_job_registry.count,
            })
        except Exception as exc:
            data['queues'].append({'name': name, 'error': str(exc)})
    return JsonResponse(data)
