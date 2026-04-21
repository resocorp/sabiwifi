"""
Partner list with health scoring.

Health score heuristic (green/yellow/red):
- green:  ≥1 successful payment in last 14d AND ≥1 online router
- yellow: activity in last 30d but not in last 14d OR router offline
- red:    no payments in 30d OR suspended
"""
from datetime import timedelta

from django.db.models import Sum, Count, Q, Max
from django.utils import timezone

from accounts.models import Reseller
from plans.models import Subscription
from billing.models import Payment
from routers.models import Router


def partner_rows(filter_key=None):
    now = timezone.now()
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    fourteen_ago = now - timedelta(days=14)
    thirty_ago = now - timedelta(days=30)

    # Aggregate MRR + active subs + last payment + router counts in one pass
    qs = Reseller.objects.annotate(
        mrr=Sum(
            'payments__amount_ngn',
            filter=Q(payments__paystack_status='success', payments__created_at__gte=month),
        ),
        active_subs=Count(
            'subscriptions',
            filter=Q(subscriptions__status='active'),
            distinct=True,
        ),
        last_payment=Max(
            'payments__created_at',
            filter=Q(payments__paystack_status='success'),
        ),
        routers_online=Count(
            'routers',
            filter=Q(routers__status='online'),
            distinct=True,
        ),
        routers_total=Count(
            'routers',
            filter=~Q(routers__status='available'),
            distinct=True,
        ),
    ).order_by('-mrr', 'name')

    rows = []
    for r in qs:
        # Health score
        if r.status == 'suspended':
            health = 'red'
        elif not r.last_payment or r.last_payment < thirty_ago:
            health = 'red'
        elif r.last_payment >= fourteen_ago and (r.routers_online or 0) > 0:
            health = 'green'
        else:
            health = 'yellow'

        row = {
            'id': r.id,
            'name': r.name,
            'slug': r.slug,
            'status': r.status,
            'mrr': r.mrr or 0,
            'active_subs': r.active_subs or 0,
            'routers_online': r.routers_online or 0,
            'routers_total': r.routers_total or 0,
            'last_payment': r.last_payment,
            'health': health,
            'days_since_payment': (
                (now - r.last_payment).days if r.last_payment else None
            ),
        }
        rows.append(row)

    if filter_key == 'dormant':
        rows = [r for r in rows if r['last_payment'] is None or r['days_since_payment'] >= 30]
    elif filter_key == 'at_risk':
        rows = [r for r in rows if r['health'] in ('red', 'yellow')]

    return rows
