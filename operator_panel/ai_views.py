"""Operator-only AI oversight: cross-tenant spend, error rate, kill switch.

Phase 2 oversight surface. All endpoints require staff. The pause/resume
endpoints flip `ResellerAIConfig.ai_paused_at` — the same flag the circuit
breaker uses — so a manual pause is indistinguishable from a breaker trip
except via `ai_pause_reason`.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Avg, Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Reseller
from ai.models import AIAgentRun, ResellerAIConfig
from ai.safety import pause_ai, resume_ai


staff_required = user_passes_test(lambda u: u.is_staff, login_url='/login/')


def _window_start(request, default_days=7):
    try:
        days = max(1, min(90, int(request.GET.get('days', default_days))))
    except (TypeError, ValueError):
        days = default_days
    return timezone.now() - timedelta(days=days), days


@login_required(login_url='/login/')
@staff_required
def operator_ai_overview(request):
    """Per-reseller AI roll-up: spend, runs, error rate, pause state."""
    since, days = _window_start(request)

    configs = (ResellerAIConfig.objects
               .select_related('reseller')
               .order_by('reseller__name'))

    rows = []
    for cfg in configs:
        agg = (AIAgentRun.objects
               .filter(reseller=cfg.reseller, started_at__gte=since)
               .aggregate(
                   total=Count('id'),
                   failed=Count('id', filter=Q(status=AIAgentRun.STATUS_FAILED)),
                   cost=Sum('cost_ngn'),
                   avg_latency=Avg('latency_ms'),
               ))
        total = agg['total'] or 0
        failed = agg['failed'] or 0
        err_pct = (failed / total * 100) if total else 0
        rows.append({
            'config': cfg,
            'reseller': cfg.reseller,
            'total_runs': total,
            'failed_runs': failed,
            'error_pct': round(err_pct, 1),
            'cost_ngn': agg['cost'] or Decimal('0'),
            'avg_latency_ms': int(agg['avg_latency'] or 0),
            'paused': bool(cfg.ai_paused_at),
            'pause_reason': cfg.ai_pause_reason,
        })

    total_cost = sum((r['cost_ngn'] for r in rows), Decimal('0'))
    total_runs = sum(r['total_runs'] for r in rows)
    total_failed = sum(r['failed_runs'] for r in rows)

    return render(request, 'operator/ai_overview.html', {
        'rows': rows,
        'window_days': days,
        'total_cost_ngn': total_cost,
        'total_runs': total_runs,
        'total_failed': total_failed,
        'total_error_pct': round((total_failed / total_runs * 100) if total_runs else 0, 1),
    })


@login_required(login_url='/login/')
@staff_required
def operator_ai_runs(request):
    """Audit trail browser — filterable list of AIAgentRun rows."""
    since, days = _window_start(request)
    qs = (AIAgentRun.objects
          .select_related('reseller', 'conversation', 'ticket')
          .filter(started_at__gte=since))

    reseller_slug = request.GET.get('reseller')
    if reseller_slug:
        qs = qs.filter(reseller__slug=reseller_slug)
    role = request.GET.get('role')
    if role:
        qs = qs.filter(agent_role=role)
    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    runs = list(qs.order_by('-started_at')[:200])
    return render(request, 'operator/ai_runs.html', {
        'runs': runs,
        'resellers': Reseller.objects.order_by('name'),
        'window_days': days,
        'filters': {
            'reseller': reseller_slug or '',
            'role': role or '',
            'status': status or '',
        },
    })


@login_required(login_url='/login/')
@staff_required
def operator_ai_run_detail(request, run_id):
    run = get_object_or_404(
        AIAgentRun.objects.select_related('reseller', 'conversation', 'ticket', 'message'),
        pk=run_id,
    )
    return render(request, 'operator/ai_run_detail.html', {'run': run})


@require_POST
@login_required(login_url='/login/')
@staff_required
def operator_ai_pause(request, reseller_pk):
    cfg = get_object_or_404(ResellerAIConfig, reseller_id=reseller_pk)
    reason = (request.POST.get('reason') or f'operator_manual:{request.user.username}')[:255]
    pause_ai(cfg, reason)
    return JsonResponse({'ok': True, 'paused_at': cfg.ai_paused_at.isoformat(),
                         'reason': cfg.ai_pause_reason})


@require_POST
@login_required(login_url='/login/')
@staff_required
def operator_ai_resume(request, reseller_pk):
    cfg = get_object_or_404(ResellerAIConfig, reseller_id=reseller_pk)
    resume_ai(cfg)
    return JsonResponse({'ok': True, 'paused': False})
