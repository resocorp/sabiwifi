"""
RADIUS / network statistics.

All functions read from the unmanaged radacct / radpostauth tables and
join to Router / Reseller via nasipaddress = Router.wg_tunnel_ip.
"""
from datetime import timedelta

from django.db.models import Count, Sum, Q
from django.utils import timezone

from radius.models import Radacct, Radpostauth
from routers.models import Router, RouterHealthLog


def active_sessions_qs():
    """Radacct rows where the session is still open."""
    return Radacct.objects.filter(acctstoptime__isnull=True)


def active_session_count():
    return active_sessions_qs().count()


def auth_stats_24h():
    """Count ACCEPT vs REJECT from radpostauth for last 24 h."""
    since = timezone.now() - timedelta(hours=24)
    rows = Radpostauth.objects.filter(authdate__gte=since).values('reply').annotate(n=Count('id'))
    out = {'accept': 0, 'reject': 0, 'total': 0}
    for r in rows:
        reply = (r['reply'] or '').lower()
        if 'accept' in reply:
            out['accept'] += r['n']
        elif 'reject' in reply:
            out['reject'] += r['n']
        out['total'] += r['n']
    out['success_rate'] = round(100 * out['accept'] / out['total'], 1) if out['total'] else None
    return out


def data_today_bytes():
    """Bytes in + out for sessions that started today."""
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    agg = Radacct.objects.filter(acctstarttime__gte=today_start).aggregate(
        inp=Sum('acctinputoctets'), out=Sum('acctoutputoctets'),
    )
    return (agg['inp'] or 0) + (agg['out'] or 0)


def peak_concurrent_24h():
    """
    Approximate peak-concurrent over last 24 h by counting sessions that
    overlapped each hourly bucket. Uses Python reduction over at most 24 rows.
    """
    now = timezone.now()
    since = now - timedelta(hours=24)
    peak = 0
    for h in range(24):
        bucket = since + timedelta(hours=h)
        n = Radacct.objects.filter(
            acctstarttime__lte=bucket,
        ).filter(
            Q(acctstoptime__gte=bucket) | Q(acctstoptime__isnull=True),
        ).count()
        if n > peak:
            peak = n
    return peak


def per_partner_breakdown():
    """
    For each router that currently has active sessions OR saw traffic today,
    join nasipaddress → Router.wg_tunnel_ip → Reseller and report counts.
    """
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    by_ip = Radacct.objects.filter(acctstarttime__gte=today_start).values('nasipaddress').annotate(
        sessions=Count('radacctid'),
        inp=Sum('acctinputoctets'),
        out=Sum('acctoutputoctets'),
    )
    # Map ip → router/reseller via wg_tunnel_ip
    ips = [row['nasipaddress'] for row in by_ip if row['nasipaddress']]
    router_map = {
        r.wg_tunnel_ip: r
        for r in Router.objects.filter(wg_tunnel_ip__in=ips).select_related('reseller')
    }
    rows = []
    for row in by_ip:
        ip = row['nasipaddress']
        router = router_map.get(ip)
        partner = router.reseller.name if router and router.reseller else 'unassigned'
        serial = router.serial_number if router else '—'
        rows.append({
            'partner': partner,
            'serial': serial,
            'nasipaddress': ip,
            'sessions_today': row['sessions'] or 0,
            'bytes_today': (row['inp'] or 0) + (row['out'] or 0),
        })
    rows.sort(key=lambda r: r['bytes_today'], reverse=True)
    return rows


def router_uptime_pct(router, since, until=None):
    """
    Percentage of time a single router was in 'online' state between
    `since` and `until` (default now), computed by replaying
    RouterHealthLog transitions.
    """
    until = until or timezone.now()
    total_window = (until - since).total_seconds()
    if total_window <= 0:
        return 0.0

    logs = list(
        RouterHealthLog.objects.filter(
            router=router, created_at__gte=since, created_at__lte=until,
        )
        .order_by('created_at')
        .values_list('event', 'created_at')
    )
    prior = RouterHealthLog.objects.filter(
        router=router, created_at__lt=since,
    ).order_by('-created_at').values_list('event', flat=True).first()
    state = prior if prior in ('online', 'offline') else (
        'online' if router.status == 'online' else 'offline'
    )

    online_sec = 0.0
    cursor = since
    for event, ts in logs:
        delta = (ts - cursor).total_seconds()
        if state == 'online':
            online_sec += delta
        cursor = ts
        state = event
    tail = (until - cursor).total_seconds()
    if state == 'online':
        online_sec += tail

    return round(100 * online_sec / total_window, 1)


def router_uptime_7d():
    """
    Percentage of time routers were in 'online' state over the last 7 days,
    computed from RouterHealthLog transitions. Returns list of dicts.
    """
    now = timezone.now()
    since = now - timedelta(days=7)

    rows = []
    for router in Router.objects.filter(reseller__isnull=False).select_related('reseller'):
        rows.append({
            'serial': router.serial_number,
            'partner': router.reseller.name if router.reseller else 'unassigned',
            'status': router.status,
            'uptime_pct': router_uptime_pct(router, since, now),
        })
    rows.sort(key=lambda r: r['uptime_pct'])
    return rows


def all_stats():
    return {
        'active_sessions': active_session_count(),
        'auth': auth_stats_24h(),
        'data_today_bytes': data_today_bytes(),
        'peak_concurrent_24h': peak_concurrent_24h(),
        'per_partner': per_partner_breakdown(),
        'uptime_7d': router_uptime_7d(),
    }
