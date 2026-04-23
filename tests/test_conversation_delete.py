"""Owner-only hard-delete of archived conversations.

POST /api/conversations/<pk>/delete/ — see conversations.views.conversation_delete.
Gated by the `conversations_delete` capability, further restricted to
state=resolved threads.

Cascade expectations:
  - Messages (FK CASCADE) are purged with the conversation.
  - Tickets (FK SET_NULL) survive; their conversation_id is nulled.
  - AIAgentRun (FK SET_NULL) survives; audit/cost trail preserved.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase

from accounts.models import Reseller
from accounts.permissions import (
    ROLE_MANAGER, ROLE_DISPATCHER, ROLE_OFFICE_CARE, ROLE_FIELD_TECH,
)
from conversations.models import Conversation, Message
from staff.models import StaffMember
from tickets.models import Ticket


def _reseller(slug):
    user = User.objects.create_user(
        username=f'{slug}-owner@x', password='ownerpw',
        email=f'{slug}-owner@x',
    )
    return Reseller.objects.create(
        user=user, slug=slug, name=slug.title(),
        email=f'biz-{slug}@example.com',
        phone=f'+23480000{abs(hash(slug)) % 10**5:05d}',
        paystack_subaccount_code='ACCT_t',
        payment_verified=True,
    )


def _staff_with_login(reseller, *, role, email, password='staffpw'):
    sm = StaffMember.objects.create(
        reseller=reseller, name=email, role=role, active=True,
        phone='+2348011110001', whatsapp='+2348011110001',
        email=email, can_log_in=True,
    )
    user = User.objects.create_user(username=email, email=email, password=password)
    sm.user = user
    sm.save(update_fields=['user'])
    return sm, user


def _resolved_convo(reseller, jid='2348099999999@s.whatsapp.net'):
    c = Conversation.objects.create(
        reseller=reseller, channel=Conversation.CHANNEL_WHATSAPP,
        external_thread_id=jid, contact_phone=jid.split('@')[0],
        state=Conversation.STATE_RESOLVED,
    )
    Message.objects.create(
        conversation=c, direction=Message.DIRECTION_IN,
        body='hello', source=Message.SOURCE_CUSTOMER,
    )
    Message.objects.create(
        conversation=c, direction=Message.DIRECTION_OUT,
        body='world', source=Message.SOURCE_HUMAN,
    )
    return c


class ConversationDeleteAuthTest(TestCase):
    def setUp(self):
        self.reseller = _reseller('delx')
        self.convo = _resolved_convo(self.reseller)
        self.url = f'/api/conversations/{self.convo.pk}/delete/'
        self.client = Client()

    def test_owner_deletes_resolved_conversation(self):
        self.client.force_login(self.reseller.user)
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Conversation.objects.filter(pk=self.convo.pk).exists())
        self.assertEqual(
            Message.objects.filter(conversation_id=self.convo.pk).count(), 0,
            'messages should cascade-delete with the conversation',
        )

    def test_non_owner_roles_are_forbidden(self):
        for role in (ROLE_MANAGER, ROLE_DISPATCHER, ROLE_OFFICE_CARE, ROLE_FIELD_TECH):
            _, user = _staff_with_login(
                self.reseller, role=role, email=f'{role}@delx.x',
            )
            self.client.force_login(user)
            resp = self.client.post(self.url)
            self.assertEqual(
                resp.status_code, 403,
                f'{role} should be forbidden but got {resp.status_code}',
            )
            self.assertTrue(
                Conversation.objects.filter(pk=self.convo.pk).exists(),
                f'{role} must not be able to delete',
            )
            self.client.logout()


class ConversationDeleteStateGuardTest(TestCase):
    def test_non_resolved_states_return_400(self):
        reseller = _reseller('gard')
        self.client = Client()
        self.client.force_login(reseller.user)
        for state in (
            Conversation.STATE_OPEN,
            Conversation.STATE_PENDING_HUMAN,
            Conversation.STATE_AI_DRAFTED,
        ):
            c = Conversation.objects.create(
                reseller=reseller, channel=Conversation.CHANNEL_WHATSAPP,
                external_thread_id=f'jid-{state}@s', state=state,
            )
            resp = self.client.post(f'/api/conversations/{c.pk}/delete/')
            self.assertEqual(
                resp.status_code, 400,
                f'state={state} should return 400 but got {resp.status_code}',
            )
            self.assertTrue(Conversation.objects.filter(pk=c.pk).exists())


class ConversationDeleteCascadeTest(TestCase):
    def test_linked_ticket_survives_with_null_conversation(self):
        reseller = _reseller('casc')
        convo = _resolved_convo(reseller)
        ticket = Ticket.objects.create(
            reseller=reseller, conversation=convo,
            type=Ticket.TYPE_SUPPORT, status=Ticket.STATUS_RESOLVED,
            subject='no internet',
        )
        client = Client()
        client.force_login(reseller.user)
        resp = client.post(f'/api/conversations/{convo.pk}/delete/')
        self.assertEqual(resp.status_code, 200)
        ticket.refresh_from_db()
        self.assertIsNone(
            ticket.conversation_id,
            'ticket FK must be SET_NULL on conversation delete',
        )
        # ticket itself persists
        self.assertTrue(Ticket.objects.filter(pk=ticket.pk).exists())


class ConversationDeleteTenancyTest(TestCase):
    def test_cross_reseller_delete_returns_404(self):
        r_a = _reseller('tenA')
        r_b = _reseller('tenB')
        convo_b = _resolved_convo(r_b, jid='2348000000002@s.whatsapp.net')
        client = Client()
        client.force_login(r_a.user)  # logged in as A, trying to delete B's convo
        resp = client.post(f'/api/conversations/{convo_b.pk}/delete/')
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Conversation.objects.filter(pk=convo_b.pk).exists())
