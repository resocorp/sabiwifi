"""
Enhanced overview stats: revenue trend, MRR delta, funnel, live sessions.
"""
from datetime import timedelta

from django.db.models import Sum, Count, Q
from django.utils import timezone

from accounts.models import Subscriber
from billing.models import Payment
from radius.models import Radacct


def revenue_sparkline_30d():
    """Return list of 30 dicts {date, revenue} for the last 30 days (inclusive today)."""
    now = timezone.now()
    start = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)

    # Fetch per-day sums in one query
    from django.db.models.functions import TruncDate
    rows = (
        Payment.objects
        .filter(paystack_status='success', created_at__gte=start)
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(total=Sum('amount_ngn'))
    )
    by_day = {r['day']: float(r['total'] or 0) for r in rows}

    out = []
    max_val = 0.0
    for i in range(30):
        d = (start + timedelta(days=i)).date()
        v = by_day.get(d, 0.0)
        out.append({'date': d, 'revenue': v})
        if v > max_val:
            max_val = v
    return {'series': out, 'max': max_val}


def mrr_delta():
    """
    Revenue this month vs last month (calendar months), with % delta.
    """
    now = timezone.now()
    this_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Compute last month's start by going 1 day before this_start
    last_end = this_start - timedelta(seconds=1)
    last_start = last_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    this_rev = Payment.objects.filter(
        paystack_status='success', created_at__gte=this_start,
    ).aggregate(t=Sum('amount_ngn'))['t'] or 0

    last_rev = Payment.objects.filter(
        paystack_status='success',
        created_at__gte=last_start, created_at__lt=this_start,
    ).aggregate(t=Sum('amount_ngn'))['t'] or 0

    if last_rev:
        pct = round(100 * (float(this_rev) - float(last_rev)) / float(last_rev), 1)
    else:
        pct = None
    return {'this_month': this_rev, 'last_month': last_rev, 'pct_delta': pct}


def signup_funnel_7d():
    """
    Subscribers created in last 7 days → verified → PIN set → first paid.
    """
    since = timezone.now() - timedelta(days=7)

    otp_initiated = Subscriber.objects.filter(created_at__gte=since).count()
    verified = Subscriber.objects.filter(created_at__gte=since, verified=True).count()
    pin_set = Subscriber.objects.filter(
        created_at__gte=since, verified=True,
    ).exclude(pin_hash='').count()

    # First paid: subscribers created in window who have a successful payment
    paid_ids = set(
        Payment.objects.filter(
            paystack_status='success', created_at__gte=since,
        ).values_list('subscriber_id', flat=True)
    )
    window_ids = set(
        Subscriber.objects.filter(created_at__gte=since).values_list('id', flat=True)
    )
    first_paid = len(paid_ids & window_ids)

    return {
        'otp_initiated': otp_initiated,
        'verified': verified,
        'pin_set': pin_set,
        'first_paid': first_paid,
    }


def live_active_sessions():
    return Radacct.objects.filter(acctstoptime__isnull=True).count()


def commission_snapshot():
    """Platform commission (month-to-date) — payouts aren't tracked, they route to subaccounts directly."""
    now = timezone.now()
    this_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    commission_mtd = Payment.objects.filter(
        paystack_status='success', created_at__gte=this_start,
    ).aggregate(t=Sum('platform_amount_ngn'))['t'] or 0
    return {'commission_mtd': commission_mtd}


def all_stats():
    return {
        'sparkline': revenue_sparkline_30d(),
        'mrr': mrr_delta(),
        'funnel': signup_funnel_7d(),
        'live_sessions': live_active_sessions(),
        'commission': commission_snapshot(),
    }
