"""SLA-breach sweep embedded in `sweep_field_pings`.

The ping sweep already existed; Phase 2c added a second pass that
auto-escalates any non-terminal ticket whose SLA has blown. Verifies:
  - escalation event is logged
  - priority bumps normal → high
  - escalation_reason is set so the ticket won't be re-escalated
"""
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Reseller
from ai.models import ResellerAIConfig
from tickets.models import Ticket, TicketEvent


def _mk_reseller(slug='sla'):
    user = User.objects.create_user(
        username=f'{slug}@x', password='x', email=f'user-{slug}@x',
    )
    r = Reseller.objects.create(
        user=user, slug=slug, name=slug.title(),
        email=f'biz-{slug}@example.com',
        phone='+2348000000000', paystack_subaccount_code='ACCT_test',
        payment_verified=True,
    )
    ResellerAIConfig.objects.create(
        reseller=r, text_provider=ResellerAIConfig.PROVIDER_ANTHROPIC,
        text_model='claude-sonnet-4-6',
        capabilities={'ai_enabled': True},
    )
    return r


class SLAAutoEscalationTest(TestCase):
    def test_overdue_open_ticket_is_escalated_once(self):
        r = _mk_reseller('sla1')
        t = Ticket.objects.create(
            reseller=r, type=Ticket.TYPE_SUPPORT,
            subject='Missed SLA', priority=Ticket.PRIORITY_NORMAL,
        )
        # Force sla_due_at into the past so the sweep picks it up.
        Ticket.objects.filter(pk=t.pk).update(
            sla_due_at=timezone.now() - timedelta(hours=2),
        )

        with patch('notifications.notify.send_whatsapp', return_value=True):
            out = StringIO()
            call_command('sweep_field_pings', stdout=out)

        t.refresh_from_db()
        self.assertEqual(t.priority, Ticket.PRIORITY_HIGH)
        self.assertEqual(t.escalation_reason, 'sla_breached')
        self.assertTrue(
            TicketEvent.objects.filter(
                ticket=t, kind=TicketEvent.KIND_ESCALATED,
            ).exists()
        )

        # Second sweep must NOT re-escalate (escalation_reason is set).
        with patch('notifications.notify.send_whatsapp', return_value=True):
            call_command('sweep_field_pings', stdout=StringIO())
        self.assertEqual(
            TicketEvent.objects.filter(
                ticket=t, kind=TicketEvent.KIND_ESCALATED,
            ).count(),
            1,
        )

    def test_resolved_ticket_past_sla_is_not_escalated(self):
        r = _mk_reseller('sla2')
        t = Ticket.objects.create(
            reseller=r, type=Ticket.TYPE_SUPPORT,
            subject='Already done', priority=Ticket.PRIORITY_NORMAL,
            status=Ticket.STATUS_RESOLVED,
        )
        Ticket.objects.filter(pk=t.pk).update(
            sla_due_at=timezone.now() - timedelta(hours=2),
            resolved_at=timezone.now() - timedelta(hours=1),
        )
        with patch('notifications.notify.send_whatsapp', return_value=True):
            call_command('sweep_field_pings', stdout=StringIO())
        self.assertFalse(
            TicketEvent.objects.filter(
                ticket=t, kind=TicketEvent.KIND_ESCALATED,
            ).exists()
        )

    def test_dry_run_does_not_mutate(self):
        r = _mk_reseller('sla3')
        t = Ticket.objects.create(
            reseller=r, type=Ticket.TYPE_SUPPORT, subject='x',
            priority=Ticket.PRIORITY_NORMAL,
        )
        Ticket.objects.filter(pk=t.pk).update(
            sla_due_at=timezone.now() - timedelta(hours=1),
        )
        with patch('notifications.notify.send_whatsapp', return_value=True):
            call_command('sweep_field_pings', '--dry-run', stdout=StringIO())
        t.refresh_from_db()
        self.assertEqual(t.priority, Ticket.PRIORITY_NORMAL)
        self.assertEqual(t.escalation_reason, '')
