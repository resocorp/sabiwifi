"""
Tests for plan ↔ router filtering (D1-D4).

Covers:
  - ServicePlan.available_for_router classmethod (D2):
    * empty router set → available everywhere
    * specific routers → only those routers
  - portal_plans API filters by serial (D3)
  - Cross-reseller plan never returned for another reseller's router
"""
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse

from accounts.models import Reseller
from plans.models import ServicePlan
from routers.models import Router


def _reseller(idx=1):
    user = User.objects.create_user(
        username=f'u{idx}@x.com', email=f'u{idx}@x.com', password='pass1234',
    )
    return Reseller.objects.create(
        user=user, name=f'Reseller {idx}', owner_name='Owner',
        email=f'u{idx}@x.com', phone=f'+23480{idx:09d}', status='active',
        payment_verified=True,
        paystack_subaccount_code=f'ACCT_{idx}',
    )


def _router(reseller, serial):
    return Router.objects.create(
        serial_number=serial, reseller=reseller, status='online',
        wg_tunnel_ip=f'10.99.0.{Router.objects.count() + 10}',
        nas_secret='s', api_username='u', api_password='p',
    )


def _plan(reseller, name, slug):
    return ServicePlan.objects.create(
        reseller=reseller, name=name, slug=slug,
        download_mbps=5, upload_mbps=5,
        duration_days=1, price_ngn=Decimal('100'), max_devices=1,
    )


class AvailableForRouterTests(TestCase):
    def setUp(self):
        self.r = _reseller()
        self.router_a = _router(self.r, 'AAA')
        self.router_b = _router(self.r, 'BBB')

    def test_plan_with_no_routers_available_on_all(self):
        plan = _plan(self.r, 'Open', 'open')
        self.assertIn(plan, ServicePlan.available_for_router(self.router_a))
        self.assertIn(plan, ServicePlan.available_for_router(self.router_b))

    def test_plan_restricted_to_router_a_excluded_from_router_b(self):
        plan = _plan(self.r, 'A only', 'a-only')
        plan.routers.add(self.router_a)
        self.assertIn(plan, ServicePlan.available_for_router(self.router_a))
        self.assertNotIn(plan, ServicePlan.available_for_router(self.router_b))

    def test_inactive_plan_excluded(self):
        plan = _plan(self.r, 'Off', 'off')
        plan.is_active = False
        plan.save()
        self.assertNotIn(plan, ServicePlan.available_for_router(self.router_a))

    def test_other_resellers_plan_excluded(self):
        r2 = _reseller(idx=2)
        plan = _plan(r2, 'Other', 'other')
        self.assertNotIn(plan, ServicePlan.available_for_router(self.router_a))

    def test_no_duplicates_when_plan_has_multiple_routers(self):
        plan = _plan(self.r, 'Both', 'both')
        plan.routers.add(self.router_a, self.router_b)
        plans = list(ServicePlan.available_for_router(self.router_a))
        self.assertEqual(plans.count(plan), 1)


class PortalPlansFilterTests(TestCase):
    """portal_plans?serial=X must only return plans available on that router."""

    def setUp(self):
        self.r = _reseller()
        self.router_a = _router(self.r, 'AAA')
        self.router_b = _router(self.r, 'BBB')
        self.open_plan = _plan(self.r, 'Open', 'open')
        self.a_only = _plan(self.r, 'A only', 'a-only')
        self.a_only.routers.add(self.router_a)
        self.client = Client()

    def test_serial_a_returns_open_and_a_only(self):
        resp = self.client.get('/api/portal/plans/', {'r': 'AAA'})
        self.assertEqual(resp.status_code, 200)
        names = [p['name'] for p in resp.json().get('plans', [])]
        self.assertIn('Open', names)
        self.assertIn('A only', names)

    def test_serial_b_excludes_a_only(self):
        resp = self.client.get('/api/portal/plans/', {'r': 'BBB'})
        self.assertEqual(resp.status_code, 200)
        names = [p['name'] for p in resp.json().get('plans', [])]
        self.assertIn('Open', names)
        self.assertNotIn('A only', names)
