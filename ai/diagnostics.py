"""L1 diagnostic helpers for the Support agent.

Pure-Python functions — they take models in, return facts out. No side
effects, no LLM calls, no message sending. The Support agent calls these
via thin tool wrappers in `ai/tools.py` to gather facts before deciding
on a `diagnosed_cause` and `suggested_action`.

The big subscriber→router lookup goes through Radacct because there's no
direct FK. If the subscriber has no live RADIUS session, we can't tell
which router they connect through; the cause defaults to
`device_side_unknown` and the AI must ask the customer for hardware clues.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from radius.models import Radacct
from routers.models import Router
from tickets.models import Ticket


# ---------------------------------------------------------------------------
# Live router lookup — joins active radacct → nasipaddress → Router.wg_tunnel_ip
# ---------------------------------------------------------------------------

def lookup_subscriber_router(subscriber) -> Optional[Router]:
    """Return the Router this subscriber is currently connected through, or
    None if no active session can be found.

    Strategy:
      1. Look up the most recent active session in Radacct (acctstoptime IS NULL)
         keyed by subscriber.phone (the RADIUS username convention).
      2. The session's nasipaddress is the router's WireGuard tunnel IP.
      3. Filter Router by reseller (multi-tenant safety) AND wg_tunnel_ip.
    """
    sub_phone = (subscriber.phone or '').strip()
    if not sub_phone:
        return None
    row = (Radacct.objects.filter(username=sub_phone, acctstoptime__isnull=True)
           .order_by('-acctstarttime').first())
    if row is None or not row.nasipaddress or row.nasipaddress == '0.0.0.0':
        # Fall back to last-known session — even closed, the nasipaddress
        # tells us the most recent router. Helps when the subscriber's
        # session just dropped.
        row = (Radacct.objects.filter(username=sub_phone)
               .order_by('-acctstarttime').first())
        if row is None or not row.nasipaddress or row.nasipaddress == '0.0.0.0':
            return None
    return Router.objects.filter(
        reseller=subscriber.reseller,
        wg_tunnel_ip=row.nasipaddress,
    ).first()


def is_router_currently_offline(router: Router) -> bool:
    """True if router.status is 'offline' or last_seen is stale."""
    if router is None:
        return False
    if router.status == 'offline':
        return True
    # Stale heartbeat (>10 min) is treated as offline even if the status
    # field hasn't been refreshed by the periodic check yet.
    if router.last_seen is None:
        return True
    age = (timezone.now() - router.last_seen).total_seconds()
    return age > 600


def infer_customer_type(subscriber, router) -> str:
    """Return 'pppoe', 'hotspot', or 'unknown'.

    Heuristics in order of confidence:
      1. Router.service_mode is 'pppoe' or 'hotspot' (definitive when the
         router is single-mode).
      2. If service_mode is 'both', try the active radacct row's
         nasporttype / servicetype if present (FreeRADIUS may populate these).
      3. Otherwise return 'unknown' so the agent asks the customer.
    """
    if router is None:
        return 'unknown'
    mode = (router.service_mode or '').lower()
    if mode in ('pppoe', 'hotspot'):
        return mode

    # service_mode='both' or unset — try the live session's port type.
    sub_phone = (subscriber.phone or '').strip()
    row = (Radacct.objects.filter(username=sub_phone, acctstoptime__isnull=True)
           .order_by('-acctstarttime').first())
    if row is not None:
        port = (getattr(row, 'nasporttype', '') or '').lower()
        if 'pppo' in port or 'virtual' in port:
            return 'pppoe'
        if 'wireless' in port or 'ether' in port:
            return 'hotspot'
    return 'unknown'


# ---------------------------------------------------------------------------
# Cause categorisation
# ---------------------------------------------------------------------------

@dataclass
class DiagnosticFacts:
    """Bag of read-only facts collected by the Support agent before
    categorisation. Each field is independently optional — the categoriser
    handles partial info."""
    subscriber_id: Optional[int] = None
    subscription_active: Optional[bool] = None
    subscription_expired: Optional[bool] = None
    last_payment_status: str = ''            # success / failed / pending / ''
    data_cap_exhausted: Optional[bool] = None
    router_id: Optional[int] = None
    router_offline: Optional[bool] = None
    reseller_wide_outage: Optional[bool] = None
    has_live_session: Optional[bool] = None
    customer_type: str = ''                  # pppoe / hotspot / unknown
    customer_clue: str = ''                  # 'pon_blink', 'no_lights', etc.

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def categorise_cause(facts: DiagnosticFacts) -> tuple[str, str]:
    """Return (cause, action) tuple matching `Ticket.CAUSE_*` and
    `Ticket.ACTION_*`. Apply checks in priority order: outage > billing >
    data cap > hardware clue > unknown."""

    # 1. General outage — the upstream router is down (or the whole reseller).
    if facts.router_offline or facts.reseller_wide_outage:
        return Ticket.CAUSE_GENERAL_OUTAGE, Ticket.ACTION_NO_ACTION

    # 2. Subscription / payment problems.
    if facts.subscription_expired:
        if facts.last_payment_status == 'failed':
            return Ticket.CAUSE_PAYMENT_FAILED, Ticket.ACTION_CUSTOMER_ACTION
        return Ticket.CAUSE_EXPIRED_SUBSCRIPTION, Ticket.ACTION_CUSTOMER_ACTION

    # 3. Data cap burned even though the subscription is still "active".
    # Prepaid hotspot plans very commonly fail this way.
    if facts.data_cap_exhausted:
        return Ticket.CAUSE_DATA_CAP_EXHAUSTED, Ticket.ACTION_CUSTOMER_ACTION

    # 4. Hardware clue from the customer (e.g. PON light blinking on a fiber CPE).
    clue = (facts.customer_clue or '').lower()
    if 'pon' in clue and ('blink' in clue or 'red' in clue or 'lost' in clue):
        return Ticket.CAUSE_PON_SIGNAL_LOST, Ticket.ACTION_DISPATCH

    # 5. Active session but customer says no internet — likely device-side.
    if facts.has_live_session:
        return Ticket.CAUSE_DEVICE_SIDE_UNKNOWN, Ticket.ACTION_CUSTOMER_ACTION

    # 6. No session, no expiry, no outage — unknown; dispatch a tech.
    if facts.has_live_session is False and not facts.router_offline:
        return Ticket.CAUSE_DEVICE_SIDE_UNKNOWN, Ticket.ACTION_DISPATCH

    return Ticket.CAUSE_OTHER, Ticket.ACTION_DISPATCH


# ---------------------------------------------------------------------------
# Reseller-wide outage + data-cap helpers
# ---------------------------------------------------------------------------

def check_reseller_wide_outage(reseller) -> dict:
    """Is a meaningful fraction of this reseller's router fleet offline right
    now? Used to tell the customer "we're aware, whole service is down" rather
    than treating it as a one-off.

    Thresholds: 2+ routers offline OR 30%+ of the fleet.
    """
    all_routers = list(Router.objects.filter(reseller=reseller).only(
        'id', 'status', 'last_seen',
    ))
    total = len(all_routers)
    if total == 0:
        return {'offline_count': 0, 'total_routers': 0, 'is_general_outage': False}
    offline = sum(1 for r in all_routers if is_router_currently_offline(r))
    is_general = offline >= 2 or (total > 0 and offline / total >= 0.3)
    return {
        'offline_count': offline,
        'total_routers': total,
        'is_general_outage': bool(is_general),
    }


def check_data_cap_remaining(subscriber) -> dict:
    """Roll up DailyUsageSnapshot rows over the active subscription's
    billing window and compare against ServicePlan.data_cap_gb.

    Returns a dict with used_gb, cap_gb, percent_used, exhausted. Treats null
    cap as unlimited (exhausted=False). Falls back to the latest snapshot's
    `quota_exceeded` flag if the plan has daily quotas instead of a monthly cap.
    """
    from plans.models import DailyUsageSnapshot, Subscription
    result = {
        'used_gb': 0.0,
        'cap_gb': None,
        'percent_used': 0.0,
        'exhausted': False,
    }
    sub = (Subscription.objects.filter(subscriber=subscriber, status='active')
           .order_by('-start_date').first())
    if sub is None:
        return result
    plan = sub.plan
    cap = plan.data_cap_gb
    start = sub.start_date.date() if sub.start_date else None
    end = sub.expiry_date.date() if sub.expiry_date else timezone.now().date()
    qs = DailyUsageSnapshot.objects.filter(subscriber=subscriber, date__lte=end)
    if start is not None:
        qs = qs.filter(date__gte=start)
    agg = qs.aggregate(
        total_down=Sum('download_bytes'), total_up=Sum('upload_bytes'),
    )
    total_bytes = (agg['total_down'] or 0) + (agg['total_up'] or 0)
    used_gb = total_bytes / (1024 ** 3)
    result['used_gb'] = round(used_gb, 3)

    if cap is None:
        # Unlimited plan — fall back to the latest snapshot's quota_exceeded
        # flag, which covers daily-quota plans.
        last = qs.order_by('-date').first()
        if last is not None and last.quota_exceeded:
            result['exhausted'] = True
        return result

    cap_float = float(cap)
    result['cap_gb'] = cap_float
    if cap_float > 0:
        pct = (used_gb / cap_float) * 100
        result['percent_used'] = round(pct, 1)
        result['exhausted'] = used_gb >= cap_float
    return result
