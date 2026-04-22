"""End-to-end RBAC tests covering the capability matrix, view gates,
field-tech queryset scoping, and staff login provisioning.

Hits a real Postgres DB per CLAUDE.md ("integration tests must hit a real
database, not mocks") — no provider stubs.
"""
from decimal import Decimal

from django.contrib.auth.models import AnonymousUser, User
from django.test import Client, TestCase

from accounts.models import Reseller
from accounts.permissions import (
    can,
    effective_role, effective_reseller,
    ROLE_OWNER, ROLE_MANAGER, ROLE_DISPATCHER, ROLE_OFFICE_CARE, ROLE_FIELD_TECH,
    ROLE_CAPS,
    scope_queryset,
)
from conversations.models import Conversation
from staff.models import StaffMember
from tickets.models import Ticket
from tickets.services import create_ticket


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _reseller(slug='acme'):
    user = User.objects.create_user(username=f'{slug}-owner@x', password='ownerpw',
                                    email=f'{slug}-owner@x')
    return Reseller.objects.create(
        user=user, slug=slug, name=slug.title(),
        phone='+2348000000000',
        paystack_subaccount_code='ACCT_t',
        payment_verified=True,
    )


def _staff_with_login(reseller, *, role, name, email, password='staffpw'):
    """Build a StaffMember with a backing Django User."""
    sm = StaffMember.objects.create(
        reseller=reseller, name=name, role=role, active=True,
        phone='+2348011110001', whatsapp='+2348011110001',
        email=email, can_log_in=True,
    )
    user = User.objects.create_user(username=email, email=email, password=password)
    sm.user = user
    sm.save(update_fields=['user'])
    return sm, user


# ---------------------------------------------------------------------------
# Capability matrix truth table
# ---------------------------------------------------------------------------

class CapabilityMatrixTest(TestCase):
    """For each role × representative capabilities, confirm `can()` matches
    the user-confirmed plan matrix."""

    def setUp(self):
        self.reseller = _reseller('cap')
        # owner is reseller.user
        self.owner = self.reseller.user
        self.manager_sm, self.manager_user = _staff_with_login(
            self.reseller, role=ROLE_MANAGER, name='M', email='m@cap.x',
        )
        self.disp_sm, self.disp_user = _staff_with_login(
            self.reseller, role=ROLE_DISPATCHER, name='D', email='d@cap.x',
        )
        self.office_sm, self.office_user = _staff_with_login(
            self.reseller, role=ROLE_OFFICE_CARE, name='O', email='o@cap.x',
        )
        self.tech_sm, self.tech_user = _staff_with_login(
            self.reseller, role=ROLE_FIELD_TECH, name='T', email='t@cap.x',
        )

    def test_owner_can_everything(self):
        for cap in ('overview', 'plans', 'subscribers', 'all', 'ai_config',
                    'tickets', 'inbox_field', 'broadcasts', 'reports',
                    'assign_ticket'):
            self.assertTrue(can(self.owner, cap), f'owner should have {cap}')

    def test_manager_no_billing_no_ai_config(self):
        self.assertTrue(can(self.manager_user, 'plans'))
        self.assertTrue(can(self.manager_user, 'inbox_customer'))
        self.assertTrue(can(self.manager_user, 'inbox_field'))
        self.assertTrue(can(self.manager_user, 'assign_ticket'))
        self.assertTrue(can(self.manager_user, 'staff_crud'))
        # Owner-only
        self.assertFalse(can(self.manager_user, 'all'))
        self.assertFalse(can(self.manager_user, 'ai_config'))

    def test_dispatcher_can_assign_no_plans(self):
        self.assertTrue(can(self.disp_user, 'tickets'))
        self.assertTrue(can(self.disp_user, 'assign_ticket'))
        self.assertTrue(can(self.disp_user, 'inbox_field'))
        self.assertTrue(can(self.disp_user, 'staff_read'))
        # No
        self.assertFalse(can(self.disp_user, 'plans'))
        self.assertFalse(can(self.disp_user, 'staff_crud'))
        self.assertFalse(can(self.disp_user, 'broadcasts'))
        self.assertFalse(can(self.disp_user, 'ai_config'))

    def test_office_care_read_only(self):
        self.assertTrue(can(self.office_user, 'inbox_customer'))
        self.assertTrue(can(self.office_user, 'leads'))
        self.assertTrue(can(self.office_user, 'tickets_read'))
        self.assertTrue(can(self.office_user, 'reports_read'))
        # No write
        self.assertFalse(can(self.office_user, 'tickets'))
        self.assertFalse(can(self.office_user, 'assign_ticket'))
        self.assertFalse(can(self.office_user, 'inbox_field'))
        self.assertFalse(can(self.office_user, 'staff_read'))

    def test_field_tech_minimal_scope(self):
        self.assertTrue(can(self.tech_user, 'tickets_own'))
        self.assertTrue(can(self.tech_user, 'inbox_field_own'))
        self.assertTrue(can(self.tech_user, 'profile_own'))
        # No
        for denied in ('overview', 'plans', 'subscribers', 'leads', 'tickets',
                       'inbox_customer', 'inbox_field', 'staff_read',
                       'assign_ticket', 'reports', 'ai_config', 'all'):
            self.assertFalse(can(self.tech_user, denied),
                             f'field_tech should NOT have {denied}')

    def test_anonymous_user_no_caps(self):
        anon = AnonymousUser()
        for cap in ROLE_CAPS[ROLE_OWNER] | {'tickets', 'plans'}:
            self.assertFalse(can(anon, cap))

    def test_inactive_staff_has_no_role(self):
        self.tech_sm.active = False
        self.tech_sm.save(update_fields=['active'])
        self.assertEqual(effective_role(self.tech_user), '')
        self.assertFalse(can(self.tech_user, 'tickets_own'))

    def test_can_log_in_false_blocks_role_resolution(self):
        self.tech_sm.can_log_in = False
        self.tech_sm.save(update_fields=['can_log_in'])
        self.assertEqual(effective_role(self.tech_user), '')


# ---------------------------------------------------------------------------
# View gate: each role hits each URL → expect appropriate code
# ---------------------------------------------------------------------------

class ViewGateTest(TestCase):
    """Drive a Django test client through the dashboard URLs as each role
    and assert 200 (allowed), 302 (redirect to login for anon), or 403
    (PermissionDenied) per the matrix."""

    def setUp(self):
        self.reseller = _reseller('gate')
        self.owner = self.reseller.user
        self.manager_sm, self.manager_user = _staff_with_login(
            self.reseller, role=ROLE_MANAGER, name='M', email='m@gate.x',
        )
        self.tech_sm, self.tech_user = _staff_with_login(
            self.reseller, role=ROLE_FIELD_TECH, name='T', email='t@gate.x',
        )

    def _get(self, user, url):
        c = Client()
        c.force_login(user)
        return c.get(url)

    def test_owner_can_hit_inbox(self):
        self.assertEqual(self._get(self.owner, '/dashboard/inbox/').status_code, 200)

    def test_manager_can_hit_inbox(self):
        self.assertEqual(self._get(self.manager_user, '/dashboard/inbox/').status_code, 200)

    def test_field_tech_can_hit_inbox(self):
        # field tech has inbox_field_own which gates the inbox shell
        self.assertEqual(self._get(self.tech_user, '/dashboard/inbox/').status_code, 200)

    def test_field_tech_blocked_from_plans(self):
        self.assertEqual(self._get(self.tech_user, '/dashboard/plans/').status_code, 403)

    def test_field_tech_blocked_from_payments(self):
        self.assertEqual(self._get(self.tech_user, '/dashboard/payments/').status_code, 403)

    def test_manager_blocked_from_ai_config(self):
        self.assertEqual(self._get(self.manager_user, '/dashboard/ai/').status_code, 403)

    def test_owner_can_open_ai_config(self):
        self.assertEqual(self._get(self.owner, '/dashboard/ai/').status_code, 200)

    def test_anonymous_redirected_to_login(self):
        c = Client()
        r = c.get('/dashboard/inbox/')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r['Location'])


# ---------------------------------------------------------------------------
# Field-tech queryset scoping
# ---------------------------------------------------------------------------

class FieldTechScopingTest(TestCase):
    def setUp(self):
        self.reseller = _reseller('scope')
        self.owner = self.reseller.user

        self.tech_a_sm, self.tech_a_user = _staff_with_login(
            self.reseller, role=ROLE_FIELD_TECH, name='Bola', email='bola@scope.x',
        )
        self.tech_b_sm, self.tech_b_user = _staff_with_login(
            self.reseller, role=ROLE_FIELD_TECH, name='Tunde', email='tunde@scope.x',
        )

        # Two tickets — one assigned to A, one to B
        self.t_a = create_ticket(
            reseller=self.reseller, type=Ticket.TYPE_SUPPORT,
            subject='A only', body='', assigned_staff=self.tech_a_sm,
        )
        self.t_b = create_ticket(
            reseller=self.reseller, type=Ticket.TYPE_SUPPORT,
            subject='B only', body='', assigned_staff=self.tech_b_sm,
        )

    def test_owner_sees_all_tickets(self):
        qs = scope_queryset(Ticket.objects.all(), self.owner)
        self.assertEqual(qs.count(), 2)

    def test_field_tech_sees_only_own_tickets(self):
        qs = scope_queryset(Ticket.objects.all(), self.tech_a_user)
        ids = list(qs.values_list('pk', flat=True))
        self.assertEqual(ids, [self.t_a.pk])

    def test_field_tech_conversation_scoping(self):
        """Tech sees only tech-kind conversations assigned to them."""
        # Customer convo — must NOT show to tech
        Conversation.objects.create(
            reseller=self.reseller, channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='2348011112222@s.whatsapp.net',
            kind=Conversation.KIND_CUSTOMER,
        )
        # Tech convo for B — must NOT show to A
        Conversation.objects.create(
            reseller=self.reseller, channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='234-bola@s.whatsapp.net',
            kind=Conversation.KIND_TECH,
            assigned_staff=self.tech_b_sm,
        )
        # Tech convo for A — should show
        own = Conversation.objects.create(
            reseller=self.reseller, channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='234-tunde@s.whatsapp.net',
            kind=Conversation.KIND_TECH,
            assigned_staff=self.tech_a_sm,
        )

        qs = scope_queryset(Conversation.objects.all(), self.tech_a_user)
        self.assertEqual([c.pk for c in qs], [own.pk])


# ---------------------------------------------------------------------------
# Staff login provisioning
# ---------------------------------------------------------------------------

class StaffLoginProvisioningTest(TestCase):
    def setUp(self):
        self.reseller = _reseller('prov')
        self.owner = self.reseller.user

    def test_owner_creates_staff_with_login(self):
        c = Client()
        c.force_login(self.owner)
        r = c.post('/api/staff/create/', {
            'name': 'Bola', 'phone': '+2348011110001', 'role': 'field_tech',
            'email': 'bola@prov.x', 'can_log_in': True, 'password': 'tempPwd123',
        }, content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)

        sm = StaffMember.objects.get(reseller=self.reseller, email='bola@prov.x')
        self.assertTrue(sm.can_log_in)
        self.assertIsNotNone(sm.user_id)
        self.assertTrue(sm.user.is_active)

        # New user can authenticate
        c2 = Client()
        ok = c2.login(username='bola@prov.x', password='tempPwd123')
        self.assertTrue(ok)

    def test_can_log_in_false_does_not_create_user(self):
        c = Client()
        c.force_login(self.owner)
        r = c.post('/api/staff/create/', {
            'name': 'Ada', 'phone': '+2348011110002', 'role': 'office_care',
            'email': 'ada@prov.x', 'can_log_in': False,
        }, content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)

        sm = StaffMember.objects.get(reseller=self.reseller, email='ada@prov.x')
        self.assertFalse(sm.can_log_in)
        self.assertIsNone(sm.user_id)

    def test_disabling_can_log_in_deactivates_user(self):
        # Provision first
        c = Client()
        c.force_login(self.owner)
        c.post('/api/staff/create/', {
            'name': 'Tunde', 'phone': '+2348011110003', 'role': 'dispatcher',
            'email': 'tunde@prov.x', 'can_log_in': True, 'password': 'pw1234',
        }, content_type='application/json')
        sm = StaffMember.objects.get(reseller=self.reseller, email='tunde@prov.x')
        self.assertTrue(sm.user.is_active)

        # Disable
        r = c.post(f'/api/staff/{sm.pk}/', {'can_log_in': False},
                   content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        sm.refresh_from_db()
        self.assertFalse(sm.can_log_in)
        self.assertIsNotNone(sm.user_id)  # user is preserved
        sm.user.refresh_from_db()
        self.assertFalse(sm.user.is_active)
        # Cannot log in
        c2 = Client()
        self.assertFalse(c2.login(username='tunde@prov.x', password='pw1234'))

    def test_login_page_accepts_active_staff(self):
        c = Client()
        c.force_login(self.owner)
        c.post('/api/staff/create/', {
            'name': 'Dee', 'phone': '+2348011110004', 'role': 'manager',
            'email': 'dee@prov.x', 'can_log_in': True, 'password': 'managerpw',
        }, content_type='application/json')
        c.logout()

        login_ok = Client().post('/login/', {
            'email': 'dee@prov.x', 'password': 'managerpw',
        }, follow=False)
        self.assertEqual(login_ok.status_code, 302)
        self.assertNotIn('/login/', login_ok['Location'])
