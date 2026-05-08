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


class InstallationCompletionTest(TestCase):
    """Lock down the field-tech "mark complete" flow: it must create a
    Subscription, sync RADIUS, send the handoff to the subscriber, and resolve
    the install ticket."""

    def setUp(self):
        from plans.models import ServicePlan
        self.reseller, self.user = _make_reseller(slug='complete')
        self.token = Token.objects.create(user=self.user)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.plan = ServicePlan.objects.create(
            reseller=self.reseller, name='Home 10Mbps', slug='home-10',
            download_mbps=10, upload_mbps=5, duration_days=30,
            price_ngn=Decimal('15000'),
        )

    def _seed_paid_lead(self):
        """Run a lead through the paystack webhook so we have a converted
        Subscriber + pending InstallationOrder + open install ticket."""
        lead = Lead.objects.create(
            reseller=self.reseller, phone='2348022223333', name='Tunde',
            intent=Lead.INTENT_HOME, address='12 Lekki Road',
            interested_plan=self.plan,
        )
        payment = Payment.objects.create(
            reseller=self.reseller, lead=lead,
            amount_ngn=Decimal('150000'),
            paystack_reference='pay_complete_1',
            payment_type='lead_install',
            paystack_status='pending',
        )
        from billing.views import _activate_payment
        _activate_payment(payment, paystack_data={'channel': 'card'})
        lead.refresh_from_db()
        order = InstallationOrder.objects.get(lead=lead)
        return lead, order

    def test_completing_install_creates_subscription_and_resolves_ticket(self):
        from leads.services import complete_installation
        from plans.models import Subscription
        from radius.models import Radusergroup, Radcheck

        lead, order = self._seed_paid_lead()
        ticket = Ticket.objects.get(installation_order=order,
                                    type=Ticket.TYPE_INSTALL)
        self.assertNotIn(ticket.status, Ticket.TERMINAL_STATUSES)

        with patch('notifications.notify.send_whatsapp', return_value=True) as wa, \
                patch('notifications.sms.get_sms_service') as sms:
            subscriber, subscription = complete_installation(
                order, actor='tester',
            )

        order.refresh_from_db()
        lead.refresh_from_db()
        ticket.refresh_from_db()

        self.assertEqual(order.status, InstallationOrder.STATUS_COMPLETED)
        self.assertIsNotNone(order.completed_at)
        self.assertEqual(lead.status, Lead.STATUS_INSTALLED)
        self.assertEqual(subscription.status, 'active')
        self.assertEqual(
            Subscription.objects.filter(subscriber=subscriber, status='active').count(),
            1,
        )
        # RADIUS group assignment
        self.assertEqual(
            Radusergroup.objects.filter(
                username=subscriber.phone, groupname=self.plan.radius_group_name,
            ).count(),
            1,
        )
        # PPPoE password landed in radcheck (SOURCE_STAFF subscribers don't
        # get the auth_token written by assign_subscriber_to_plan)
        self.assertEqual(
            Radcheck.objects.filter(
                username=subscriber.phone, attribute='Cleartext-Password',
                value=order.pppoe_password,
            ).count(),
            1,
        )
        # Install ticket resolved
        self.assertEqual(ticket.status, Ticket.STATUS_RESOLVED)
        # WA handoff fired (SMS fallback not used because WA returned True)
        self.assertEqual(wa.call_count, 1)
        sms.assert_not_called()

    def test_completing_install_is_idempotent(self):
        from leads.services import complete_installation

        _, order = self._seed_paid_lead()
        with patch('notifications.notify.send_whatsapp', return_value=True), \
                patch('notifications.sms.get_sms_service'):
            complete_installation(order)
            subscriber, second = complete_installation(order)
        self.assertIsNone(second)  # second call short-circuits
        # Only one active Subscription
        from plans.models import Subscription
        self.assertEqual(
            Subscription.objects.filter(subscriber=subscriber, status='active').count(),
            1,
        )

    def test_completing_install_without_plan_raises(self):
        from leads.services import (
            complete_installation, InstallationCompletionError,
        )
        lead, order = self._seed_paid_lead()
        # Strip the plan attachment to simulate the gap case
        lead.interested_plan = None
        lead.save(update_fields=['interested_plan', 'updated_at'])
        with self.assertRaises(InstallationCompletionError):
            complete_installation(order)

    def test_dashboard_view_drives_completion(self):
        """The /api/leads/installations/<pk>/update/ endpoint with status=completed
        must run the full provisioning chain."""
        from plans.models import Subscription

        _, order = self._seed_paid_lead()

        with patch('notifications.notify.send_whatsapp', return_value=True), \
                patch('notifications.sms.get_sms_service'):
            resp = self.api.post(
                f'/api/leads/installations/{order.pk}/update/',
                {'status': InstallationOrder.STATUS_COMPLETED}, format='json',
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        order.refresh_from_db()
        self.assertEqual(order.status, InstallationOrder.STATUS_COMPLETED)
        self.assertEqual(
            Subscription.objects.filter(
                subscriber=order.lead.converted_subscriber, status='active',
            ).count(),
            1,
        )


class OmniInboxCrossLinkTest(TestCase):
    """Lead and Conversation that share a phone must reference each other in
    the dashboard APIs so the operator triages one human, not two records."""

    def setUp(self):
        self.reseller, self.user = _make_reseller(slug='omni')
        self.token = Token.objects.create(user=self.user)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    @override_settings(WA_API_KEY='test-wa-key')
    def test_inbound_wa_auto_links_existing_lead(self):
        lead = Lead.objects.create(
            reseller=self.reseller, phone='2348011112222',
            name='Adaeze', intent=Lead.INTENT_HOME,
        )
        payload = {
            'event': 'message_received',
            'slug': self.reseller.slug,
            'from_jid': '2348011112222@s.whatsapp.net',
            'from_phone': '2348011112222',
            'body': 'Hello, is the install today?',
            'external_message_id': 'WA-OMNI-1',
            'timestamp': int(timezone.now().timestamp()),
        }
        resp = self.client.post(
            '/api/conversations/inbound-wa/', payload,
            content_type='application/json',
            HTTP_X_WA_API_KEY='test-wa-key',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        convo = Conversation.objects.get(reseller=self.reseller)
        self.assertEqual(convo.lead_id, lead.pk)

    def test_lead_detail_includes_recent_conversations(self):
        lead = Lead.objects.create(
            reseller=self.reseller, phone='2348033334444', name='Bayo',
            intent=Lead.INTENT_SME,
        )
        # Lead.save() normalises the phone via Country lookup; use the
        # post-save value so the join key matches what the API sees.
        Conversation.objects.create(
            reseller=self.reseller, channel='whatsapp',
            external_thread_id=f'wa:{lead.phone}',
            contact_phone=lead.phone,
            last_message_at=timezone.now(),
        )
        resp = self.api.get(f'/api/leads/{lead.pk}/')
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(len(body['conversations']), 1)
        self.assertEqual(body['conversations'][0]['channel'], 'whatsapp')

    def test_conversation_detail_includes_linked_lead_summary(self):
        lead = Lead.objects.create(
            reseller=self.reseller, phone='2348055556666', name='Chika',
            intent=Lead.INTENT_HOME, quoted_amount_ngn=Decimal('150000'),
        )
        convo = Conversation.objects.create(
            reseller=self.reseller, channel='whatsapp',
            external_thread_id='wa:2348055556666',
            contact_phone='2348055556666', lead=lead,
            last_message_at=timezone.now(),
        )
        resp = self.api.get(f'/api/conversations/{convo.pk}/')
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertIsNotNone(body['lead'])
        self.assertEqual(body['lead']['id'], lead.pk)
        self.assertEqual(body['lead']['name'], 'Chika')
        self.assertEqual(body['lead']['quoted_amount_ngn'], '150000.00')


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
