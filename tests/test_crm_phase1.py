"""
End-to-end verification for the CRM foundation (Conversations, Leads,
Installations, Tickets, Staff) — the AI-less Phase 1 of the supervisor plan.

Covers:
  * Inbound WhatsApp webhook → Conversation + Message persist
  * Lead create → send quote → simulated Paystack success → lead converted
    to Subscriber, InstallationOrder created, install Ticket raised
  * Ticket lifecycle: create → assign → status change → SLA bookkeeping
  * Staff load counter updates on assign / resolve
  * Operator KPI endpoint returns the expected shape
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Reseller, Subscriber
from billing.models import Payment
from conversations.models import Conversation, Message
from leads.models import InstallationOrder, Lead
from staff.models import StaffMember
from tickets.models import Ticket, TicketEvent
from tickets.services import (
    assign_ticket, change_status, create_ticket, record_ai_action,
)


def _make_reseller(slug='acme'):
    user = User.objects.create_user(username=f'{slug}-owner', password='x')
    reseller = Reseller.objects.create(
        user=user, slug=slug, name=f'{slug.title()} Networks',
        phone='+2348000000001',
        paystack_subaccount_code='ACCT_test',
        payment_verified=True,
    )
    return reseller, user


@override_settings(WA_API_KEY='test-wa-key')
class InboundWhatsAppTest(TestCase):
    def setUp(self):
        self.reseller, _ = _make_reseller()

    def test_inbound_creates_conversation_and_message(self):
        payload = {
            'event': 'message_received',
            'slug': self.reseller.slug,
            'from_jid': '2348011112222@s.whatsapp.net',
            'from_phone': '2348011112222',
            'body': 'Hello, I need WiFi for my home',
            'external_message_id': 'WA-MSG-1',
            'timestamp': int(timezone.now().timestamp()),
        }
        resp = self.client.post(
            '/api/conversations/inbound-wa/', payload,
            content_type='application/json',
            HTTP_X_WA_API_KEY='test-wa-key',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 1)

    def test_inbound_deduped_by_external_id(self):
        payload = {
            'event': 'message_received',
            'slug': self.reseller.slug,
            'from_jid': '2348011112222@s.whatsapp.net',
            'from_phone': '2348011112222',
            'body': 'hi',
            'external_message_id': 'SAME',
            'timestamp': 0,
        }
        for _ in range(3):
            self.client.post(
                '/api/conversations/inbound-wa/', payload,
                content_type='application/json',
                HTTP_X_WA_API_KEY='test-wa-key',
            )
        self.assertEqual(Message.objects.count(), 1)

    def test_inbound_rejected_without_key(self):
        resp = self.client.post(
            '/api/conversations/inbound-wa/', {}, content_type='application/json',
        )
        self.assertEqual(resp.status_code, 403)


class LeadConversionTest(TestCase):
    def setUp(self):
        self.reseller, self.user = _make_reseller()
        self.token = Token.objects.create(user=self.user)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_create_lead_via_api(self):
        resp = self.api.post('/api/leads/create/', {
            'phone': '2348099887766', 'name': 'Adaeze', 'intent': 'home',
            'source': 'manual',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Lead.objects.count(), 1)

    def test_paystack_webhook_converts_lead(self):
        lead = Lead.objects.create(
            reseller=self.reseller, phone='2348099887766',
            name='Adaeze', intent=Lead.INTENT_HOME,
        )
        payment = Payment.objects.create(
            reseller=self.reseller, lead=lead,
            amount_ngn=Decimal('140000'),
            paystack_reference='pay_test',
            payment_type='lead_install',
            paystack_status='pending',
        )

        from billing.views import _activate_payment
        _activate_payment(payment, paystack_data={'channel': 'card'})

        payment.refresh_from_db()
        lead.refresh_from_db()
        self.assertEqual(payment.paystack_status, 'success')
        self.assertIsNotNone(payment.subscriber_id)
        self.assertEqual(lead.status, Lead.STATUS_PAID)
        self.assertEqual(InstallationOrder.objects.count(), 1)
        # An install ticket should have been auto-raised
        install_tickets = Ticket.objects.filter(
            reseller=self.reseller, type=Ticket.TYPE_INSTALL,
        )
        self.assertEqual(install_tickets.count(), 1)

    def test_lead_conversion_is_idempotent(self):
        from leads.services import convert_lead_to_subscriber
        lead = Lead.objects.create(
            reseller=self.reseller, phone='2348099887711', intent='home',
        )
        s1, o1 = convert_lead_to_subscriber(lead)
        s2, o2 = convert_lead_to_subscriber(lead)
        self.assertEqual(s1.pk, s2.pk)
        self.assertEqual(o1.pk, o2.pk)


class TicketLifecycleTest(TestCase):
    def setUp(self):
        self.reseller, _ = _make_reseller()
        self.staff = StaffMember.objects.create(
            reseller=self.reseller, name='Tech One', phone='+2348000001111',
            role=StaffMember.ROLE_FIELD_TECH,
        )

    def test_create_assign_resolve_updates_load(self):
        ticket = create_ticket(
            reseller=self.reseller, type=Ticket.TYPE_SUPPORT,
            subject='Router down',
        )
        self.assertEqual(ticket.status, Ticket.STATUS_OPEN)
        self.assertIsNotNone(ticket.sla_due_at)

        assign_ticket(ticket, self.staff, actor='human:1')
        ticket.refresh_from_db()
        self.staff.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_ASSIGNED)
        self.assertEqual(self.staff.current_load, 1)

        change_status(ticket, Ticket.STATUS_RESOLVED, actor='human:1', note='Fixed')
        ticket.refresh_from_db()
        self.staff.refresh_from_db()
        self.assertEqual(ticket.status, Ticket.STATUS_RESOLVED)
        self.assertIsNotNone(ticket.resolved_at)
        self.assertEqual(self.staff.current_load, 0)

        # TicketEvent audit trail
        kinds = list(ticket.events.values_list('kind', flat=True))
        self.assertIn(TicketEvent.KIND_CREATED, kinds)
        self.assertIn(TicketEvent.KIND_ASSIGNED, kinds)
        self.assertIn(TicketEvent.KIND_RESOLVED, kinds)

    def test_sla_breach_detection(self):
        ticket = create_ticket(
            reseller=self.reseller, type=Ticket.TYPE_SUPPORT,
            subject='Support',
        )
        ticket.sla_due_at = timezone.now() - timezone.timedelta(hours=1)
        ticket.save(update_fields=['sla_due_at'])
        self.assertTrue(ticket.is_breached())

    def test_record_ai_action_flips_handled_flag(self):
        ticket = create_ticket(
            reseller=self.reseller, type=Ticket.TYPE_SUPPORT, subject='x',
        )
        record_ai_action(ticket, agent_role='support', tool='check_router',
                         inputs={'a': 1}, outputs={'ok': True}, confidence=0.82)
        ticket.refresh_from_db()
        self.assertTrue(ticket.ai_handled)
        self.assertAlmostEqual(ticket.ai_confidence, 0.82, places=4)


class OperatorKPITest(TestCase):
    def setUp(self):
        self.reseller, _ = _make_reseller()
        self.staff_user = User.objects.create_user(
            username='opstaff', password='x', is_staff=True,
        )
        self.client = Client()
        self.client.force_login(self.staff_user)

    def test_kpi_endpoint_returns_shape(self):
        # Generate one resolved ticket
        t = create_ticket(reseller=self.reseller, type=Ticket.TYPE_SUPPORT,
                          subject='x')
        change_status(t, Ticket.STATUS_RESOLVED, actor='system')

        resp = self.client.get('/operator/api/kpis/?days=30')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        for key in ('report_to_resolution', 'payment_to_service',
                    'breached_open_tickets', 'lead_funnel',
                    'resolution_by_type'):
            self.assertIn(key, data)
