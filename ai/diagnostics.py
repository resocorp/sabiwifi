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
    router_id: Optional[int] = None
    router_offline: Optional[bool] = None
    has_live_session: Optional[bool] = None
    customer_type: str = ''                  # pppoe / hotspot / unknown
    customer_clue: str = ''                  # 'pon_blink', 'no_lights', etc.

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def categorise_cause(facts: DiagnosticFacts) -> tuple[str, str]:
    """Return (cause, action) tuple matching `Ticket.CAUSE_*` and
    `Ticket.ACTION_*`. Apply checks in priority order: outage > billing >
    hardware clue > unknown."""

    # 1. General outage — the upstream router is down.
    if facts.router_offline:
        return Ticket.CAUSE_GENERAL_OUTAGE, Ticket.ACTION_NO_ACTION

    # 2. Subscription / payment problems.
    if facts.subscription_expired:
        if facts.last_payment_status == 'failed':
            return Ticket.CAUSE_PAYMENT_FAILED, Ticket.ACTION_CUSTOMER_ACTION
        return Ticket.CAUSE_EXPIRED_SUBSCRIPTION, Ticket.ACTION_CUSTOMER_ACTION

    # 3. Hardware clue from the customer (e.g. PON light blinking on a fiber CPE).
    clue = (facts.customer_clue or '').lower()
    if 'pon' in clue and ('blink' in clue or 'red' in clue or 'lost' in clue):
        return Ticket.CAUSE_PON_SIGNAL_LOST, Ticket.ACTION_DISPATCH

    # 4. Active session but customer says no internet — likely device-side.
    if facts.has_live_session:
        return Ticket.CAUSE_DEVICE_SIDE_UNKNOWN, Ticket.ACTION_CUSTOMER_ACTION

    # 5. No session, no expiry, no outage — unknown; dispatch a tech.
    if facts.has_live_session is False and not facts.router_offline:
        return Ticket.CAUSE_DEVICE_SIDE_UNKNOWN, Ticket.ACTION_DISPATCH

    return Ticket.CAUSE_OTHER, Ticket.ACTION_DISPATCH
