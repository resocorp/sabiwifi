"""Operator overview dashboard — aggregate metrics across all resellers."""
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.utils import timezone
from django.db.models import Sum, Count, Q
from accounts.models import Reseller, Subscriber
from plans.models import Subscription
from billing.models import Payment
from routers.models import Router


@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_staff, login_url='/login/')
def operator_overview(request):
    """Platform-wide metrics dashboard for the operator."""
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Resellers
    total_resellers = Reseller.objects.count()
    active_resellers = Reseller.objects.filter(status='active').count()

    # Subscribers
    total_subscribers = Subscriber.objects.count()
    active_subscriptions = Subscription.objects.filter(status='active').count()

    # Routers
    total_routers = Router.objects.exclude(status='available').count()
    online_routers = Router.objects.filter(status='online').count()
    offline_routers = Router.objects.filter(status='offline').count()

    # Revenue
    monthly_revenue = Payment.objects.filter(
        paystack_status='success', created_at__gte=month_start,
    ).aggregate(
        total=Sum('amount_ngn'),
        platform=Sum('platform_amount_ngn'),
    )

    today_revenue = Payment.objects.filter(
        paystack_status='success', created_at__gte=today_start,
    ).aggregate(total=Sum('amount_ngn'))['total'] or 0

    # Top resellers by revenue this month
    top_resellers = Reseller.objects.filter(
        payments__paystack_status='success',
        payments__created_at__gte=month_start,
    ).annotate(
        month_revenue=Sum('payments__amount_ngn')
    ).order_by('-month_revenue')[:10]

    # Routers phoning home (last_seen within 5 min, regardless of status)
    five_min_ago = now - timezone.timedelta(minutes=5)
    phoning_home_routers = Router.objects.filter(
        last_seen__gte=five_min_ago
    ).select_related('reseller').order_by('-last_seen')

    # Unclaimed routers that have phoned home at least once
    unclaimed_with_contact = Router.objects.filter(
        status='available', last_seen__isnull=False
    ).select_related('reseller').order_by('-last_seen')[:10]

    # Recent alerts placeholder
    alerts = []
    offline_routers_list = Router.objects.filter(status='offline').select_related('reseller')[:5]
    for r in offline_routers_list:
        alerts.append({
            'type': 'warning',
            'message': f'Router {r.serial_number} ({r.reseller.name if r.reseller else "unassigned"}) is offline',
            'time': r.last_seen,
        })

    context = {
        'total_resellers': total_resellers,
        'active_resellers': active_resellers,
        'total_subscribers': total_subscribers,
        'active_subscriptions': active_subscriptions,
        'total_routers': total_routers,
        'online_routers': online_routers,
        'offline_routers': offline_routers,
        'monthly_revenue': monthly_revenue['total'] or 0,
        'monthly_platform_revenue': monthly_revenue['platform'] or 0,
        'today_revenue': today_revenue,
        'top_resellers': top_resellers,
        'alerts': alerts,
        'phoning_home_routers': phoning_home_routers,
        'unclaimed_with_contact': unclaimed_with_contact,
    }
    return render(request, 'operator/overview.html', context)
