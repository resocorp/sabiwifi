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
