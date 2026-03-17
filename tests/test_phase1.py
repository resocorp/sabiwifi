"""
End-to-end tests for SabiWiFi Phase 1.
Tests cover: models, reseller auth, router provisioning, plans CRUD,
RADIUS sync, dashboard views, admin, and the full onboarding flow.
"""
import json
from decimal import Decimal
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.authtoken.models import Token

from accounts.models import Reseller, Subscriber
from plans.models import ServicePlan, Subscription
from billing.models import Payment
from routers.models import Router
from operator_panel.models import PlatformSettings
from radius.models import (
    Radcheck, Radreply, Radusergroup, Radgroupcheck, Radgroupreply, Nas
)
from radius.utils import (
    sync_plan_to_radius, assign_subscriber_to_plan,
    remove_subscriber_from_radius, create_default_trial_plan,
)


# ──────────────────────────────────────────────
#  MODEL TESTS
# ──────────────────────────────────────────────

class PlatformSettingsTest(TestCase):
    def test_singleton_load(self):
        s = PlatformSettings.load()
        self.assertEqual(s.pk, 1)
        self.assertEqual(s.default_commission_pct, Decimal('15.00'))

    def test_singleton_cannot_have_two(self):
        PlatformSettings.load()
        s2 = PlatformSettings(platform_name='Duplicate')
        s2.save()
        self.assertEqual(PlatformSettings.objects.count(), 1)

    def test_cannot_delete(self):
        s = PlatformSettings.load()
        s.delete()
        self.assertTrue(PlatformSettings.objects.filter(pk=1).exists())


class ResellerModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='test@example.com', email='test@example.com', password='pass1234'
        )
        self.reseller = Reseller.objects.create(
            user=self.user, name='Test WiFi', owner_name='Tester',
            email='test@example.com', phone='+2348011111111',
        )

    def test_slug_auto_generated(self):
        self.assertEqual(self.reseller.slug, 'test-wifi')

    def test_unique_slug_collision(self):
        user2 = User.objects.create_user(username='t2@x.com', email='t2@x.com', password='p')
        r2 = Reseller.objects.create(
            user=user2, name='Test WiFi', owner_name='Other',
            email='t2@x.com', phone='+2348022222222',
        )
        self.assertNotEqual(r2.slug, self.reseller.slug)
        self.assertTrue(r2.slug.startswith('test-wifi'))

    def test_commission_fallback(self):
        PlatformSettings.load()
        self.assertEqual(self.reseller.get_commission_pct(), Decimal('15.00'))
        self.reseller.commission_pct = Decimal('12.00')
        self.reseller.save()
        self.assertEqual(self.reseller.get_commission_pct(), Decimal('12.00'))

    def test_fee_bearer_fallback(self):
        PlatformSettings.load()
        self.assertEqual(self.reseller.get_fee_bearer(), 'subaccount')
        self.reseller.fee_bearer = 'account'
        self.reseller.save()
        self.assertEqual(self.reseller.get_fee_bearer(), 'account')

    def test_free_subscriber_limit_fallback(self):
        PlatformSettings.load()
        self.assertEqual(self.reseller.get_free_subscriber_limit(), 5)


class SubscriberModelTest(TestCase):
    def setUp(self):
        user = User.objects.create_user(username='r@x.com', email='r@x.com', password='p')
        self.reseller = Reseller.objects.create(
            user=user, name='R', owner_name='R', email='r@x.com', phone='+2348011111111'
        )
        self.sub = Subscriber.objects.create(
            reseller=self.reseller, phone='+2348099999999', email='s@x.com'
        )

    def test_pin_set_and_check(self):
        self.sub.set_pin('1234')
        self.sub.save()
        self.assertTrue(self.sub.check_pin('1234'))
        self.assertFalse(self.sub.check_pin('0000'))

    def test_auth_token_generation(self):
        token = self.sub.generate_auth_token()
        self.assertEqual(len(token), 64)
        self.assertEqual(self.sub.auth_token, token)


class ServicePlanModelTest(TestCase):
    def setUp(self):
        user = User.objects.create_user(username='r@x.com', email='r@x.com', password='p')
        self.reseller = Reseller.objects.create(
            user=user, name='R1', owner_name='R', email='r@x.com', phone='+2348011111111'
        )

    def test_radius_group_name(self):
        plan = ServicePlan.objects.create(
            reseller=self.reseller, name='Monthly Basic', slug='monthly-basic',
            download_mbps=5, upload_mbps=5, duration_days=30, price_ngn=2000, max_devices=2,
        )
        self.assertEqual(plan.radius_group_name, 'r1-monthly-basic')

    def test_duration_display_days(self):
        plan = ServicePlan(duration_days=30, duration_hours=0)
        self.assertEqual(plan.duration_display, '30 days')

    def test_duration_display_minutes(self):
        plan = ServicePlan(duration_days=0, duration_hours=Decimal('0.50'))
        self.assertEqual(plan.duration_display, '30 min')

    def test_data_cap_display_unlimited(self):
        plan = ServicePlan(data_cap_gb=None)
        self.assertEqual(plan.data_cap_display, 'Unlimited')

    def test_data_cap_display_gb(self):
        plan = ServicePlan(data_cap_gb=Decimal('30'))
        self.assertEqual(plan.data_cap_display, '30 GB')

    def test_data_cap_display_mb(self):
        plan = ServicePlan(data_cap_gb=Decimal('0.1'))
        self.assertEqual(plan.data_cap_display, '100 MB')

    def test_mikrotik_rate_limit(self):
        plan = ServicePlan(download_mbps=10, upload_mbps=5)
        self.assertEqual(plan.mikrotik_rate_limit, '5120k/10240k')

    def test_is_free(self):
        plan = ServicePlan(price_ngn=0)
        self.assertTrue(plan.is_free)
        plan.price_ngn = Decimal('2000')
        self.assertFalse(plan.is_free)


class RouterModelTest(TestCase):
    def test_unassigned_router(self):
        r = Router.objects.create(serial_number='ABC123', status='available')
        self.assertFalse(r.is_assigned)
        self.assertFalse(r.is_online)

    def test_online_router(self):
        user = User.objects.create_user(username='r@x.com', email='r@x.com', password='p')
        reseller = Reseller.objects.create(
            user=user, name='R', owner_name='R', email='r@x.com', phone='+2348011111111'
        )
        r = Router.objects.create(
            serial_number='XYZ789', status='online', reseller=reseller
        )
        self.assertTrue(r.is_assigned)
        self.assertTrue(r.is_online)


# ──────────────────────────────────────────────
#  RADIUS SYNC TESTS
# ──────────────────────────────────────────────

class RadiusSyncTest(TestCase):
    def setUp(self):
        user = User.objects.create_user(username='r@x.com', email='r@x.com', password='p')
        self.reseller = Reseller.objects.create(
            user=user, name='R1', owner_name='R', email='r@x.com', phone='+2348011111111'
        )

    def test_sync_plan_creates_radius_attributes(self):
        plan = ServicePlan.objects.create(
            reseller=self.reseller, name='Test Plan', slug='test-plan',
            download_mbps=10, upload_mbps=5, duration_days=30,
            data_cap_gb=Decimal('50'), max_devices=2, price_ngn=3000,
        )
        group = plan.radius_group_name
        # Signal fires on save, so attributes should exist
        self.assertTrue(Radgroupreply.objects.filter(groupname=group, attribute='Mikrotik-Rate-Limit').exists())
        self.assertTrue(Radgroupreply.objects.filter(groupname=group, attribute='Session-Timeout').exists())
        self.assertTrue(Radgroupreply.objects.filter(groupname=group, attribute='Acct-Interim-Interval').exists())
        self.assertTrue(Radgroupreply.objects.filter(groupname=group, attribute='Mikrotik-Total-Limit').exists())
        self.assertTrue(Radgroupcheck.objects.filter(groupname=group, attribute='Simultaneous-Use').exists())

        # Verify values
        rate = Radgroupreply.objects.get(groupname=group, attribute='Mikrotik-Rate-Limit')
        self.assertEqual(rate.value, '5120k/10240k')
        sim = Radgroupcheck.objects.get(groupname=group, attribute='Simultaneous-Use')
        self.assertEqual(sim.value, '2')

    def test_sync_plan_unlimited_data_no_total_limit(self):
        plan = ServicePlan.objects.create(
            reseller=self.reseller, name='Unlimited', slug='unlimited',
            download_mbps=10, upload_mbps=10, duration_days=30,
            data_cap_gb=None, max_devices=3, price_ngn=5000,
        )
        group = plan.radius_group_name
        self.assertFalse(Radgroupreply.objects.filter(groupname=group, attribute='Mikrotik-Total-Limit').exists())

    def test_disable_plan_clears_attributes(self):
        plan = ServicePlan.objects.create(
            reseller=self.reseller, name='Temp', slug='temp',
            download_mbps=5, upload_mbps=5, duration_days=7, max_devices=1, price_ngn=500,
        )
        group = plan.radius_group_name
        self.assertTrue(Radgroupreply.objects.filter(groupname=group).exists())
        plan.is_active = False
        plan.save()
        self.assertFalse(Radgroupreply.objects.filter(groupname=group).exists())

    def test_assign_subscriber_to_plan(self):
        plan = ServicePlan.objects.create(
            reseller=self.reseller, name='Plan1', slug='plan1',
            download_mbps=5, upload_mbps=5, duration_days=30, max_devices=1, price_ngn=1000,
        )
        sub = Subscriber.objects.create(
            reseller=self.reseller, phone='+2348099999999', verified=True
        )
        sub.generate_auth_token()
        sub.save()

        assign_subscriber_to_plan(sub, plan)
        self.assertTrue(Radusergroup.objects.filter(username=sub.phone, groupname=plan.radius_group_name).exists())
        self.assertTrue(Radcheck.objects.filter(username=sub.phone, attribute='Cleartext-Password').exists())

    def test_remove_subscriber_from_radius(self):
        sub = Subscriber.objects.create(
            reseller=self.reseller, phone='+2348088888888', verified=True
        )
        sub.generate_auth_token()
        sub.save()
        plan = ServicePlan.objects.create(
            reseller=self.reseller, name='P', slug='p',
            download_mbps=5, upload_mbps=5, duration_days=30, max_devices=1, price_ngn=0,
        )
        assign_subscriber_to_plan(sub, plan)
        self.assertTrue(Radusergroup.objects.filter(username=sub.phone).exists())
        remove_subscriber_from_radius(sub)
        self.assertFalse(Radusergroup.objects.filter(username=sub.phone).exists())
        self.assertFalse(Radcheck.objects.filter(username=sub.phone).exists())

    def test_create_default_trial_plan(self):
        plan = create_default_trial_plan(self.reseller)
        self.assertIsNotNone(plan)
        self.assertTrue(plan.is_trial)
        self.assertTrue(plan.is_system_created)
        self.assertEqual(plan.price_ngn, 0)
        # Should not create duplicate
        plan2 = create_default_trial_plan(self.reseller)
        self.assertIsNone(plan2)


# ──────────────────────────────────────────────
#  RESELLER AUTH API TESTS
# ──────────────────────────────────────────────

class ResellerSignupAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_signup_success(self):
        resp = self.client.post(reverse('api-reseller-signup'), {
            'business_name': 'Chidi WiFi',
            'owner_name': 'Chidi Okonkwo',
            'email': 'chidi@example.com',
            'phone': '+2348012345678',
            'password': 'securepass123',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertIn('token', resp.data)
        self.assertEqual(resp.data['reseller']['name'], 'Chidi WiFi')
        self.assertTrue(Reseller.objects.filter(email='chidi@example.com').exists())

    def test_signup_duplicate_email(self):
        self.client.post(reverse('api-reseller-signup'), {
            'business_name': 'A', 'owner_name': 'A', 'email': 'dup@x.com',
            'phone': '+2348011111111', 'password': 'securepass123',
        }, format='json')
        resp = self.client.post(reverse('api-reseller-signup'), {
            'business_name': 'B', 'owner_name': 'B', 'email': 'dup@x.com',
            'phone': '+2348022222222', 'password': 'securepass123',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_signup_duplicate_phone(self):
        self.client.post(reverse('api-reseller-signup'), {
            'business_name': 'A', 'owner_name': 'A', 'email': 'a@x.com',
            'phone': '+2348011111111', 'password': 'securepass123',
        }, format='json')
        resp = self.client.post(reverse('api-reseller-signup'), {
            'business_name': 'B', 'owner_name': 'B', 'email': 'b@x.com',
            'phone': '+2348011111111', 'password': 'securepass123',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_signup_invalid_phone(self):
        resp = self.client.post(reverse('api-reseller-signup'), {
            'business_name': 'A', 'owner_name': 'A', 'email': 'a@x.com',
            'phone': '+14155551234', 'password': 'securepass123',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_signup_normalizes_phone(self):
        resp = self.client.post(reverse('api-reseller-signup'), {
            'business_name': 'A', 'owner_name': 'A', 'email': 'a@x.com',
            'phone': '08012345678', 'password': 'securepass123',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(Reseller.objects.get(email='a@x.com').phone, '+2348012345678')

    def test_signup_creates_default_branding(self):
        self.client.post(reverse('api-reseller-signup'), {
            'business_name': 'Cool WiFi', 'owner_name': 'A', 'email': 'a@x.com',
            'phone': '+2348012345678', 'password': 'securepass123',
        }, format='json')
        r = Reseller.objects.get(email='a@x.com')
        self.assertEqual(r.branding['template'], 'modern')
        self.assertEqual(r.branding['primary_color'], '#0052CC')
        self.assertEqual(r.branding['portal_title'], 'Cool WiFi')


class ResellerLoginAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.post(reverse('api-reseller-signup'), {
            'business_name': 'Test', 'owner_name': 'T', 'email': 'test@x.com',
            'phone': '+2348011111111', 'password': 'securepass123',
        }, format='json')

    def test_login_success(self):
        resp = self.client.post(reverse('api-reseller-login'), {
            'email': 'test@x.com', 'password': 'securepass123',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('token', resp.data)

    def test_login_wrong_password(self):
        resp = self.client.post(reverse('api-reseller-login'), {
            'email': 'test@x.com', 'password': 'wrongpassword',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_login_nonexistent_email(self):
        resp = self.client.post(reverse('api-reseller-login'), {
            'email': 'noone@x.com', 'password': 'securepass123',
        }, format='json')
        self.assertEqual(resp.status_code, 400)


# ──────────────────────────────────────────────
#  ROUTER API TESTS
# ──────────────────────────────────────────────

class RouterAddAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        resp = self.client.post(reverse('api-reseller-signup'), {
            'business_name': 'R WiFi', 'owner_name': 'R', 'email': 'r@x.com',
            'phone': '+2348011111111', 'password': 'securepass123',
        }, format='json')
        self.token = resp.data['token']
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token}')
        # Operator registers a serial in admin
        self.router = Router.objects.create(serial_number='HFG30A9B72C', status='available')

    def test_add_router_success(self):
        resp = self.client.post(reverse('api-router-add'), {
            'serial_number': 'HFG30A9B72C',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.router.refresh_from_db()
        self.assertEqual(self.router.status, 'pending_provision')
        self.assertIsNotNone(self.router.reseller)
        self.assertTrue(self.router.wg_public_key)
        self.assertTrue(self.router.nas_secret)
        self.assertTrue(self.router.api_password)
        # NAS entry should exist
        self.assertTrue(Nas.objects.filter(shortname='HFG30A9B72C').exists())

    def test_add_unknown_serial(self):
        resp = self.client.post(reverse('api-router-add'), {
            'serial_number': 'UNKNOWN123',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_add_already_assigned_serial(self):
        # First claim
        self.client.post(reverse('api-router-add'), {'serial_number': 'HFG30A9B72C'}, format='json')
        # Second reseller tries
        client2 = APIClient()
        resp2 = client2.post(reverse('api-reseller-signup'), {
            'business_name': 'B', 'owner_name': 'B', 'email': 'b@x.com',
            'phone': '+2348022222222', 'password': 'securepass123',
        }, format='json')
        client2.credentials(HTTP_AUTHORIZATION=f'Token {resp2.data["token"]}')
        resp = client2.post(reverse('api-router-add'), {'serial_number': 'HFG30A9B72C'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_add_router_case_insensitive(self):
        resp = self.client.post(reverse('api-router-add'), {
            'serial_number': 'hfg30a9b72c',
        }, format='json')
        self.assertEqual(resp.status_code, 201)


class RouterProvisionAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        resp = self.client.post(reverse('api-reseller-signup'), {
            'business_name': 'R WiFi', 'owner_name': 'R', 'email': 'r@x.com',
            'phone': '+2348011111111', 'password': 'securepass123',
        }, format='json')
        self.token = resp.data['token']
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token}')
        Router.objects.create(serial_number='PROV123', status='available')
        self.client.post(reverse('api-router-add'), {'serial_number': 'PROV123'}, format='json')

    def test_provision_returns_rsc(self):
        # Provision endpoint is unauthenticated
        anon = Client()
        resp = anon.get(reverse('api-router-provision', args=['PROV123']))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('SabiWiFi Provision Script', content)
        self.assertIn('PROV123', content)
        self.assertIn('/interface wireguard add', content)
        self.assertIn('/ip hotspot add', content)
        self.assertIn('/radius add', content)
        # Router should be marked provisioned
        router = Router.objects.get(serial_number='PROV123')
        self.assertEqual(router.status, 'provisioned')
        self.assertEqual(router.provision_count, 1)

    def test_provision_unknown_serial(self):
        anon = Client()
        resp = anon.get(reverse('api-router-provision', args=['UNKNOWN']))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('# not found', resp.content.decode())

    def test_provision_unassigned_serial(self):
        Router.objects.create(serial_number='UNASSIGNED', status='available')
        anon = Client()
        resp = anon.get(reverse('api-router-provision', args=['UNASSIGNED']))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('# not ready', resp.content.decode())


# ──────────────────────────────────────────────
#  PLANS CRUD API TESTS
# ──────────────────────────────────────────────

class PlansCRUDAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        resp = self.client.post(reverse('api-reseller-signup'), {
            'business_name': 'Plan WiFi', 'owner_name': 'P', 'email': 'p@x.com',
            'phone': '+2348011111111', 'password': 'securepass123',
        }, format='json')
        self.token = resp.data['token']
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token}')

    def test_create_free_plan(self):
        resp = self.client.post(reverse('plan-list'), {
            'name': 'Free Trial',
            'download_mbps': 2, 'upload_mbps': 2,
            'duration_days': 0, 'duration_hours': '0.50',
            'max_devices': 1, 'price_ngn': '0',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['name'], 'Free Trial')
        self.assertTrue(resp.data['is_trial'])
        # RADIUS group should exist
        plan = ServicePlan.objects.get(pk=resp.data['id'])
        self.assertTrue(Radgroupreply.objects.filter(groupname=plan.radius_group_name).exists())

    def test_create_paid_plan_blocked_without_bank(self):
        resp = self.client.post(reverse('plan-list'), {
            'name': 'Monthly Basic',
            'download_mbps': 5, 'upload_mbps': 5,
            'duration_days': 30, 'max_devices': 2, 'price_ngn': '2000',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('price_ngn', resp.data)

    def test_create_paid_plan_with_bank(self):
        reseller = Reseller.objects.get(email='p@x.com')
        reseller.payment_verified = True
        reseller.save()
        resp = self.client.post(reverse('plan-list'), {
            'name': 'Monthly Basic',
            'download_mbps': 5, 'upload_mbps': 5,
            'duration_days': 30, 'max_devices': 2, 'price_ngn': '2000',
        }, format='json')
        self.assertEqual(resp.status_code, 201)

    def test_list_plans(self):
        self.client.post(reverse('plan-list'), {
            'name': 'Plan A', 'download_mbps': 2, 'upload_mbps': 2,
            'duration_days': 7, 'max_devices': 1, 'price_ngn': '0',
        }, format='json')
        self.client.post(reverse('plan-list'), {
            'name': 'Plan B', 'download_mbps': 5, 'upload_mbps': 5,
            'duration_days': 30, 'max_devices': 1, 'price_ngn': '0',
        }, format='json')
        resp = self.client.get(reverse('plan-list'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['results']), 2)

    def test_update_plan(self):
        resp = self.client.post(reverse('plan-list'), {
            'name': 'Old Name', 'download_mbps': 2, 'upload_mbps': 2,
            'duration_days': 7, 'max_devices': 1, 'price_ngn': '0',
        }, format='json')
        plan_id = resp.data['id']
        resp2 = self.client.patch(reverse('plan-detail', args=[plan_id]), {
            'name': 'New Name', 'download_mbps': 5,
        }, format='json')
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data['name'], 'New Name')
        self.assertEqual(resp2.data['download_mbps'], 5)

    def test_disable_plan(self):
        resp = self.client.post(reverse('plan-list'), {
            'name': 'To Disable', 'download_mbps': 2, 'upload_mbps': 2,
            'duration_days': 7, 'max_devices': 1, 'price_ngn': '0',
        }, format='json')
        plan_id = resp.data['id']
        resp2 = self.client.post(reverse('plan-disable', args=[plan_id]))
        self.assertEqual(resp2.status_code, 200)
        plan = ServicePlan.objects.get(pk=plan_id)
        self.assertFalse(plan.is_active)
        # RADIUS attributes should be cleared
        self.assertFalse(Radgroupreply.objects.filter(groupname=plan.radius_group_name).exists())

    def test_plan_isolation_between_resellers(self):
        # Second reseller
        client2 = APIClient()
        resp2 = client2.post(reverse('api-reseller-signup'), {
            'business_name': 'Other', 'owner_name': 'O', 'email': 'o@x.com',
            'phone': '+2348022222222', 'password': 'securepass123',
        }, format='json')
        client2.credentials(HTTP_AUTHORIZATION=f'Token {resp2.data["token"]}')
        # Create plan as first reseller
        resp = self.client.post(reverse('plan-list'), {
            'name': 'My Plan', 'download_mbps': 2, 'upload_mbps': 2,
            'duration_days': 7, 'max_devices': 1, 'price_ngn': '0',
        }, format='json')
        # Second reseller should not see it
        resp3 = client2.get(reverse('plan-list'))
        self.assertEqual(len(resp3.data['results']), 0)


# ──────────────────────────────────────────────
#  PORTAL PLANS API TEST
# ──────────────────────────────────────────────

class PortalPlansAPITest(TestCase):
    def setUp(self):
        self.api = APIClient()
        resp = self.api.post(reverse('api-reseller-signup'), {
            'business_name': 'Portal Test', 'owner_name': 'PT', 'email': 'pt@x.com',
            'phone': '+2348011111111', 'password': 'securepass123',
        }, format='json')
        self.api.credentials(HTTP_AUTHORIZATION=f'Token {resp.data["token"]}')
        self.api.post(reverse('plan-list'), {
            'name': 'Free', 'download_mbps': 2, 'upload_mbps': 2,
            'duration_days': 0, 'duration_hours': '0.50', 'max_devices': 1, 'price_ngn': '0',
        }, format='json')

    def test_portal_plans_returns_active_plans(self):
        client = Client()
        reseller = Reseller.objects.get(email='pt@x.com')
        resp = client.get(reverse('api-portal-plans'), {'reseller': reseller.slug})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(len(data['plans']), 1)
        self.assertEqual(data['plans'][0]['name'], 'Free')

    def test_portal_plans_missing_slug(self):
        client = Client()
        resp = client.get(reverse('api-portal-plans'))
        self.assertEqual(resp.status_code, 400)


# ──────────────────────────────────────────────
#  SERVER-RENDERED PAGE TESTS
# ──────────────────────────────────────────────

class LandingPageTest(TestCase):
    def test_landing_page_loads(self):
        resp = self.client.get(reverse('landing'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'SabiWiFi')
        self.assertContains(resp, 'Turn Your Internet')

    def test_landing_redirects_logged_in_reseller(self):
        user = User.objects.create_user(username='r@x.com', email='r@x.com', password='p')
        Reseller.objects.create(
            user=user, name='R', owner_name='R', email='r@x.com', phone='+2348011111111'
        )
        self.client.login(username='r@x.com', password='p')
        resp = self.client.get(reverse('landing'))
        self.assertEqual(resp.status_code, 302)


class SignupPageTest(TestCase):
    def test_signup_page_loads(self):
        resp = self.client.get(reverse('signup'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Create Your Account')

    def test_signup_post_creates_reseller_and_redirects(self):
        resp = self.client.post(reverse('signup'), {
            'business_name': 'My WiFi', 'owner_name': 'Me',
            'email': 'me@x.com', 'phone': '+2348011111111', 'password': 'securepass123',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Reseller.objects.filter(email='me@x.com').exists())

    def test_signup_post_shows_errors(self):
        resp = self.client.post(reverse('signup'), {
            'business_name': '', 'owner_name': '', 'email': 'bad',
            'phone': '123', 'password': 'short',
        })
        self.assertEqual(resp.status_code, 200)
        # Should re-render with errors


class LoginPageTest(TestCase):
    def setUp(self):
        user = User.objects.create_user(username='r@x.com', email='r@x.com', password='pass1234')
        Reseller.objects.create(
            user=user, name='R', owner_name='R', email='r@x.com', phone='+2348011111111'
        )

    def test_login_page_loads(self):
        resp = self.client.get(reverse('login'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Log In')

    def test_login_success_redirects(self):
        resp = self.client.post(reverse('login'), {
            'email': 'r@x.com', 'password': 'pass1234',
        })
        self.assertEqual(resp.status_code, 302)

    def test_login_failure_shows_error(self):
        resp = self.client.post(reverse('login'), {
            'email': 'r@x.com', 'password': 'wrong',
        })
        self.assertEqual(resp.status_code, 200)


# ──────────────────────────────────────────────
#  DASHBOARD PAGE TESTS
# ──────────────────────────────────────────────

class DashboardPagesTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='d@x.com', email='d@x.com', password='p')
        self.reseller = Reseller.objects.create(
            user=self.user, name='Dash WiFi', owner_name='D', email='d@x.com',
            phone='+2348011111111', status='active',
        )
        self.client.login(username='d@x.com', password='p')

    def test_overview_loads(self):
        resp = self.client.get(reverse('dashboard-overview'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Dash WiFi')

    def test_overview_shows_getting_started_when_no_router(self):
        resp = self.client.get(reverse('dashboard-overview'))
        self.assertContains(resp, 'Add Your Router')

    def test_plans_list_loads(self):
        resp = self.client.get(reverse('dashboard-plans'))
        self.assertEqual(resp.status_code, 200)

    def test_plan_create_loads(self):
        resp = self.client.get(reverse('dashboard-plan-create'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Create New Plan')

    def test_subscribers_list_loads(self):
        resp = self.client.get(reverse('dashboard-subscribers'))
        self.assertEqual(resp.status_code, 200)

    def test_payments_loads(self):
        resp = self.client.get(reverse('dashboard-payments'))
        self.assertEqual(resp.status_code, 200)

    def test_routers_loads(self):
        resp = self.client.get(reverse('dashboard-routers'))
        self.assertEqual(resp.status_code, 200)

    def test_router_add_loads(self):
        resp = self.client.get(reverse('dashboard-router-add'))
        self.assertEqual(resp.status_code, 200)

    def test_settings_loads(self):
        resp = self.client.get(reverse('dashboard-settings'))
        self.assertEqual(resp.status_code, 200)

    def test_settings_update_branding(self):
        resp = self.client.post(reverse('dashboard-settings'), {
            'section': 'branding',
            'portal_title': 'Updated Title',
            'welcome_text': 'Hello!',
            'primary_color': '#FF0000',
            'template': 'bold',
        })
        self.assertEqual(resp.status_code, 302)
        self.reseller.refresh_from_db()
        self.assertEqual(self.reseller.branding['portal_title'], 'Updated Title')
        self.assertEqual(self.reseller.branding['template'], 'bold')

    def test_settings_update_account(self):
        resp = self.client.post(reverse('dashboard-settings'), {
            'section': 'account',
            'business_name': 'New Name',
            'email': 'd@x.com',
            'phone': '+2348011111111',
            'location': 'Lagos, Lekki',
        })
        self.assertEqual(resp.status_code, 302)
        self.reseller.refresh_from_db()
        self.assertEqual(self.reseller.name, 'New Name')
        self.assertEqual(self.reseller.location, 'Lagos, Lekki')

    def test_dashboard_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse('dashboard-overview'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)

    def test_router_add_via_dashboard(self):
        Router.objects.create(serial_number='DASHTEST', status='available')
        resp = self.client.post(reverse('dashboard-router-add'), {
            'serial_number': 'DASHTEST',
        })
        self.assertEqual(resp.status_code, 302)
        router = Router.objects.get(serial_number='DASHTEST')
        self.assertEqual(router.reseller, self.reseller)
        self.assertEqual(router.status, 'pending_provision')

    def test_plan_create_via_dashboard(self):
        resp = self.client.post(reverse('dashboard-plan-create'), {
            'name': 'Daily Plan',
            'download_mbps': '3', 'upload_mbps': '3',
            'duration_days': '1', 'duration_hours': '0',
            'max_devices': '1', 'price_ngn': '0',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ServicePlan.objects.filter(reseller=self.reseller, name='Daily Plan').exists())


# ──────────────────────────────────────────────
#  OPERATOR / ADMIN TESTS
# ──────────────────────────────────────────────

class OperatorOverviewTest(TestCase):
    def test_requires_staff(self):
        resp = self.client.get(reverse('operator-overview'))
        self.assertEqual(resp.status_code, 302)

    def test_loads_for_staff(self):
        admin = User.objects.create_superuser('admin', 'admin@x.com', 'admin123')
        self.client.login(username='admin', password='admin123')
        resp = self.client.get(reverse('operator-overview'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Platform Overview')


class DjangoAdminTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin', 'admin@x.com', 'admin123')
        self.client.login(username='admin', password='admin123')

    def test_admin_loads(self):
        resp = self.client.get('/admin/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_reseller_list(self):
        resp = self.client.get('/admin/accounts/reseller/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_router_list(self):
        resp = self.client.get('/admin/routers/router/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_plan_list(self):
        resp = self.client.get('/admin/plans/serviceplan/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_payment_list(self):
        resp = self.client.get('/admin/billing/payment/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_platform_settings(self):
        resp = self.client.get('/admin/operator_panel/platformsettings/')
        self.assertEqual(resp.status_code, 200)


# ──────────────────────────────────────────────
#  FULL END-TO-END ONBOARDING FLOW
# ──────────────────────────────────────────────

class FullOnboardingFlowTest(TestCase):
    """
    Tests the complete Phase 1 flow:
    Reseller signs up → adds router → router phones home → trial plan auto-created
    → creates custom plan → views dashboard
    """
    def test_full_reseller_onboarding(self):
        # 1. Operator registers router serial in admin
        Router.objects.create(serial_number='E2ETEST123', status='available')

        # 2. Reseller signs up via the web page
        resp = self.client.post(reverse('signup'), {
            'business_name': 'E2E WiFi',
            'owner_name': 'End To End',
            'email': 'e2e@example.com',
            'phone': '+2348099887766',
            'password': 'strongpass456',
        })
        self.assertEqual(resp.status_code, 302)
        reseller = Reseller.objects.get(email='e2e@example.com')
        self.assertEqual(reseller.status, 'setup')
        self.assertFalse(reseller.payment_verified)

        # 3. Dashboard shows Getting Started
        resp = self.client.get(reverse('dashboard-overview'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Add Your Router')

        # 4. Reseller adds router via dashboard
        resp = self.client.post(reverse('dashboard-router-add'), {
            'serial_number': 'E2ETEST123',
        })
        self.assertEqual(resp.status_code, 302)
        router = Router.objects.get(serial_number='E2ETEST123')
        self.assertEqual(router.status, 'pending_provision')
        self.assertEqual(router.reseller, reseller)

        # 5. Dashboard shows pending router
        resp = self.client.get(reverse('dashboard-overview'))
        self.assertContains(resp, 'Waiting for router')
        self.assertContains(resp, 'E2ETEST123')

        # 6. Router phones home — provision endpoint returns .rsc
        anon = Client()
        resp = anon.get(reverse('api-router-provision', args=['E2ETEST123']))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('E2ETEST123', content)
        self.assertIn('sabiwifi', content)
        router.refresh_from_db()
        self.assertEqual(router.status, 'provisioned')

        # 7. Simulate router coming online (normally done by health check)
        router.status = 'online'
        router.save()

        # 8. Auto-create trial plan
        trial = create_default_trial_plan(reseller)
        self.assertIsNotNone(trial)
        self.assertEqual(trial.name, 'Free Trial')
        self.assertTrue(trial.is_system_created)

        # 9. Dashboard now shows router online and plans
        resp = self.client.get(reverse('dashboard-overview'))
        self.assertContains(resp, "You're live!")

        # 10. Reseller creates a custom free plan via dashboard
        resp = self.client.post(reverse('dashboard-plan-create'), {
            'name': 'Weekly Free',
            'download_mbps': '3', 'upload_mbps': '3',
            'duration_days': '7', 'duration_hours': '0',
            'max_devices': '1', 'price_ngn': '0',
        })
        self.assertEqual(resp.status_code, 302)

        # 11. Plans page shows both plans
        resp = self.client.get(reverse('dashboard-plans'))
        self.assertContains(resp, 'Free Trial')
        self.assertContains(resp, 'Weekly Free')

        # 12. Paid plan creation is blocked (no bank)
        api = APIClient()
        token, _ = Token.objects.get_or_create(user=reseller.user)
        api.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        resp = api.post(reverse('plan-list'), {
            'name': 'Monthly', 'download_mbps': 5, 'upload_mbps': 5,
            'duration_days': 30, 'max_devices': 2, 'price_ngn': '2000',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

        # 13. Bank banner shows on dashboard
        resp = self.client.get(reverse('dashboard-overview'))
        self.assertContains(resp, 'Add your bank account')

        # 14. After bank verification, paid plans work
        reseller.payment_verified = True
        reseller.bank_name = 'GTBank'
        reseller.account_number = '0123456789'
        reseller.save()
        resp = api.post(reverse('plan-list'), {
            'name': 'Monthly Basic', 'download_mbps': 5, 'upload_mbps': 5,
            'duration_days': 30, 'max_devices': 2, 'price_ngn': '2000',
        }, format='json')
        self.assertEqual(resp.status_code, 201)

        # 15. All plans visible on portal API
        resp = anon.get(reverse('api-portal-plans'), {'reseller': reseller.slug})
        data = json.loads(resp.content)
        self.assertEqual(len(data['plans']), 3)

        # 16. Routers page shows the router
        resp = self.client.get(reverse('dashboard-routers'))
        self.assertContains(resp, 'E2ETEST123')
        self.assertContains(resp, 'Online')

        # 17. Settings page loads with branding
        resp = self.client.get(reverse('dashboard-settings'))
        self.assertContains(resp, 'E2E WiFi')

        # 18. Operator overview shows the reseller
        admin_user = User.objects.create_superuser('op', 'op@x.com', 'op123')
        op_client = Client()
        op_client.login(username='op', password='op123')
        resp = op_client.get(reverse('operator-overview'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Platform Overview')
