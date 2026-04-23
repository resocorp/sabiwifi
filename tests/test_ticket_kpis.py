"""KPI computation for the tickets page.

Synthetic tickets spanning the SLA / reopen / AI-handled / per-cause axes so a
single run of compute_kpis reveals regressions in any one column.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.models import Reseller
from tickets.models import Ticket, TicketEvent
from tickets.services import compute_kpis


def _mk_reseller(slug='kp'):
    user = User.objects.create_user(
        username=f'{slug}@x', password='x', email=f'user-{slug}@x',
    )
    return Reseller.objects.create(
        user=user, slug=slug, name=slug.title(),
        email=f'biz-{slug}@example.com',
        phone=f'+23480{abs(hash(slug)) % 10**9:09d}',
        paystack_subaccount_code='ACCT_test',
        payment_verified=True,
    )


def _mk_ticket(r, *, subject='t', status='open', priority='normal',
               created_at=None, resolved_at=None, sla_due_at=None,
               first_response_at=None, ai_handled=False,
               diagnosed_cause=''):
    now = timezone.now()
    t = Ticket.objects.create(
        reseller=r, type=Ticket.TYPE_SUPPORT, subject=subject,
        status=status, priority=priority, ai_handled=ai_handled,
        diagnosed_cause=diagnosed_cause,
    )
    # Override auto-fields post-save to simulate historical rows.
    updates = {}
    if created_at is not None:
        updates['created_at'] = created_at
    if resolved_at is not None:
        updates['resolved_at'] = resolved_at
    if sla_due_at is not None:
        updates['sla_due_at'] = sla_due_at
    if first_response_at is not None:
        updates['first_response_at'] = first_response_at
    if updates:
        Ticket.objects.filter(pk=t.pk).update(**updates)
        t.refresh_from_db()
    return t


class ComputeKpisBasicTest(TestCase):
    def test_empty_window_returns_nulls_and_zero_open(self):
        r = _mk_reseller('k0')
        out = compute_kpis(reseller=r, days=30)
        self.assertEqual(out['open_count'], 0)
        self.assertEqual(out['total_in_window'], 0)
        self.assertIsNone(out['mttr_seconds'])
        self.assertIsNone(out['sla_breach_rate'])
        self.assertEqual(out['top_causes'], [])
        self.assertEqual(len(out['tickets_per_day']), 30)

    def test_mttr_is_median_of_resolution_durations(self):
        r = _mk_reseller('k1')
        now = timezone.now()
        # Three resolved tickets: 10min, 30min, 60min → median 30min
        for mins in (10, 30, 60):
            _mk_ticket(
                r, status=Ticket.STATUS_RESOLVED,
                created_at=now - timedelta(minutes=mins + 1),
                resolved_at=now - timedelta(minutes=1),
            )
        out = compute_kpis(reseller=r, days=30)
        self.assertEqual(out['resolved_in_window'], 3)
        # 30 minutes +/- 60s jitter from "+1 minute" margin in the fixture
        self.assertIsNotNone(out['mttr_seconds'])
        self.assertAlmostEqual(out['mttr_seconds'], 30 * 60, delta=120)

    def test_sla_breach_rate_counts_both_late_resolved_and_still_open_past_due(self):
        r = _mk_reseller('k2')
        now = timezone.now()
        # On-time
        _mk_ticket(
            r, status=Ticket.STATUS_RESOLVED,
            created_at=now - timedelta(hours=2),
            sla_due_at=now + timedelta(hours=1),
            resolved_at=now - timedelta(minutes=10),
        )
        # Late-resolved
        _mk_ticket(
            r, status=Ticket.STATUS_RESOLVED,
            created_at=now - timedelta(hours=3),
            sla_due_at=now - timedelta(hours=1),
            resolved_at=now - timedelta(minutes=5),
        )
        # Still-open, past due
        _mk_ticket(
            r, status=Ticket.STATUS_OPEN,
            created_at=now - timedelta(hours=4),
            sla_due_at=now - timedelta(minutes=30),
        )
        out = compute_kpis(reseller=r, days=30)
        # 2 of 3 in the window are breached
        self.assertAlmostEqual(out['sla_breach_rate'], 2 / 3, places=2)

    def test_reopen_rate_counts_tickets_with_any_reopen_event(self):
        r = _mk_reseller('k3')
        now = timezone.now()
        t1 = _mk_ticket(r, status=Ticket.STATUS_CLOSED,
                        created_at=now - timedelta(days=1))
        t2 = _mk_ticket(r, status=Ticket.STATUS_RESOLVED,
                        created_at=now - timedelta(days=1))
        _mk_ticket(r, status=Ticket.STATUS_OPEN,
                   created_at=now - timedelta(days=1))
        # Only t1 and t2 have reopen events; t1 has two reopens (shouldn't double-count)
        TicketEvent.objects.create(
            ticket=t1, kind=TicketEvent.KIND_REOPENED, actor='human:1',
        )
        TicketEvent.objects.create(
            ticket=t1, kind=TicketEvent.KIND_REOPENED, actor='human:2',
        )
        TicketEvent.objects.create(
            ticket=t2, kind=TicketEvent.KIND_REOPENED, actor='customer_reopened',
        )
        out = compute_kpis(reseller=r, days=30)
        # 2 distinct tickets with reopens / 3 total in window
        self.assertAlmostEqual(out['reopen_rate'], 2 / 3, places=2)

    def test_ai_handled_share(self):
        r = _mk_reseller('k4')
        now = timezone.now()
        _mk_ticket(r, created_at=now - timedelta(days=1), ai_handled=True)
        _mk_ticket(r, created_at=now - timedelta(days=1), ai_handled=True)
        _mk_ticket(r, created_at=now - timedelta(days=1), ai_handled=False)
        _mk_ticket(r, created_at=now - timedelta(days=1), ai_handled=False)
        out = compute_kpis(reseller=r, days=30)
        self.assertAlmostEqual(out['ai_handled_share'], 0.5, places=2)

    def test_top_causes_sorted_by_count(self):
        r = _mk_reseller('k5')
        now = timezone.now()
        for _ in range(3):
            _mk_ticket(r, created_at=now - timedelta(days=1),
                       diagnosed_cause=Ticket.CAUSE_GENERAL_OUTAGE)
        for _ in range(5):
            _mk_ticket(r, created_at=now - timedelta(days=1),
                       diagnosed_cause=Ticket.CAUSE_DATA_CAP_EXHAUSTED)
        _mk_ticket(r, created_at=now - timedelta(days=1))  # no cause → excluded
        out = compute_kpis(reseller=r, days=30)
        self.assertEqual(out['top_causes'][0]['cause'],
                         Ticket.CAUSE_DATA_CAP_EXHAUSTED)
        self.assertEqual(out['top_causes'][0]['count'], 5)
        self.assertEqual(out['top_causes'][1]['cause'],
                         Ticket.CAUSE_GENERAL_OUTAGE)

    def test_tenancy_filter_only_counts_requested_reseller(self):
        r1 = _mk_reseller('k6a')
        r2 = _mk_reseller('k6b')
        now = timezone.now()
        _mk_ticket(r1, status=Ticket.STATUS_OPEN,
                   created_at=now - timedelta(days=1))
        _mk_ticket(r2, status=Ticket.STATUS_OPEN,
                   created_at=now - timedelta(days=1))
        out = compute_kpis(reseller=r1, days=30)
        self.assertEqual(out['open_count'], 1)

    def test_tickets_per_day_has_one_entry_per_day(self):
        r = _mk_reseller('k7')
        out = compute_kpis(reseller=r, days=14)
        self.assertEqual(len(out['tickets_per_day']), 14)
        dates = [row['date'] for row in out['tickets_per_day']]
        self.assertEqual(dates, sorted(dates))  # chronological
