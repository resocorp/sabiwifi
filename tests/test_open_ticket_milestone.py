"""PR A — regression: when the customer agent opens a ticket mid-conversation,
the 'opened' milestone must NOT auto-post (the agent's own send_reply is the
one customer-facing message). Other actors (human, system) still get the
milestone posted.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from accounts.models import Reseller
from tickets.models import Ticket
from tickets.services import create_ticket


def _mk_reseller(slug='milestone-test'):
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


class OpenTicketMilestoneGuardTests(TestCase):
    def _create(self, reseller, *, actor):
        # `captureOnCommitCallbacks(execute=True)` runs transaction.on_commit
        # hooks inline so we can observe (or patch out) milestone scheduling.
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with patch('ai.jobs.notify_customer_ticket_milestone') as mock_notify:
                ticket = create_ticket(
                    reseller=reseller,
                    type=Ticket.TYPE_SUPPORT,
                    subject='PON signal lost',
                    body='fibre down',
                    actor=actor,
                )
                return ticket, mock_notify, callbacks

    def test_ai_customer_actor_suppresses_opened_milestone(self):
        """When actor='ai_customer', no 'opened' milestone must fire — the
        agent's own send_reply in the same turn is the only customer-visible
        message. This is the root-cause fix for the NEDU duplicate-reply bug.
        """
        r = _mk_reseller('ai-actor')
        ticket, mock_notify, callbacks = self._create(r, actor='ai_customer')
        self.assertTrue(ticket.pk)
        # No on_commit callback registered for the milestone → no notify call.
        mock_notify.assert_not_called()

    def test_human_actor_still_fires_opened_milestone(self):
        """For any non-AI actor (operator, system, field tech), the 'opened'
        milestone still runs — they don't auto-reply to the customer, so the
        milestone is the only acknowledgement the customer gets.
        """
        r = _mk_reseller('human-actor')
        ticket, mock_notify, _ = self._create(r, actor='operator')
        mock_notify.assert_called_once_with(ticket.pk, 'opened')

    def test_default_actor_still_fires_opened_milestone(self):
        """Default actor (system) also fires the milestone — backwards
        compatible with callers that don't set actor explicitly.
        """
        r = _mk_reseller('default-actor')
        with self.captureOnCommitCallbacks(execute=True):
            with patch('ai.jobs.notify_customer_ticket_milestone') as mock_notify:
                ticket = create_ticket(
                    reseller=r,
                    type=Ticket.TYPE_SUPPORT,
                    subject='No actor passed',
                )
        # Callbacks execute on captureOnCommitCallbacks exit — assert afterward.
        mock_notify.assert_called_once_with(ticket.pk, 'opened')
