"""
Tests for manually-created subscriber accounts (reseller dashboard + operator panel).

Covers:
  - Reseller subscriber_create view (C3): creates Subscriber, hashed PIN,
    raw PIN in radcheck, optional plan activation
  - Operator staff_subscriber_create view (C4): same but with source='staff'
  - PIN length validation (5-8 digits)
  - Phone normalisation
  - Duplicate phone rejection
  - PPPoE password contract: manually-created subscribers keep raw PIN in
    radcheck even after assign_subscriber_to_plan runs
"""
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse

from accounts.models import Reseller, Subscriber, Country
from plans.models import ServicePlan, Subscription
from radius.models import Radcheck, Radusergroup
from radius.utils import assign_subscriber_to_plan, set_subscriber_radius_password


def _country():
    obj, _ = Country.objects.get_or_create(
        code='NG',
        defaults=dict(
            name='Nigeria', dial_code='+234',
            local_prefix='0', local_phone_length=11,
            flag_emoji='🇳🇬', is_active=True,
        ),
    )
    return obj


def _reseller(email='r@x.com', staff=False):
    user = User.objects.create_user(username=email, email=email, password='pass1234', is_staff=staff)
    return Reseller.objects.create(
        user=user, name='Test Reseller', owner_name='Owner',
        email=email, phone=f'+23480{Reseller.objects.count():09d}',
        status='active',
    )


def _plan(reseller, **overrides):
    defaults = dict(
        reseller=reseller, name='1 Day', slug='1-day',
        download_mbps=5, upload_mbps=5,
        duration_days=1, price_ngn=100, max_devices=1,
    )
    defaults.update(overrides)
    return ServicePlan.objects.create(**defaults)


class ResellerSubscriberCreateTests(TestCase):
    def setUp(self):
        self.country = _country()
        self.reseller = _reseller()
        self.client = Client()
        self.client.force_login(self.reseller.user)
        self.url = reverse('dashboard-subscriber-create')

    def test_creates_subscriber_with_hashed_pin_and_raw_radcheck(self):
        resp = self.client.post(self.url, {
            'country': 'NG',
            'phone': '08031234567',
            'email': 'cust@x.com',
            'pin': '12345',
        })
        self.assertEqual(resp.status_code, 302)

        sub = Subscriber.objects.get(reseller=self.reseller, phone='08031234567')
        self.assertEqual(sub.source, Subscriber.SOURCE_RESELLER)
        self.assertTrue(sub.verified)
        self.assertNotEqual(sub.pin_hash, '12345')  # hashed, not raw
        self.assertTrue(sub.check_pin('12345'))

        rc = Radcheck.objects.get(username='08031234567', attribute='Cleartext-Password')
        self.assertEqual(rc.value, '12345')

    def test_assigns_plan_and_creates_subscription(self):
        plan = _plan(self.reseller)
        resp = self.client.post(self.url, {
            'country': 'NG',
            'phone': '08031234567',
            'pin': '54321',
            'plan_id': str(plan.pk),
        })
        self.assertEqual(resp.status_code, 302)

        sub = Subscriber.objects.get(phone='08031234567')
        self.assertTrue(Subscription.objects.filter(
            subscriber=sub, plan=plan, status='active'
        ).exists())
        self.assertTrue(Radusergroup.objects.filter(
            username='08031234567', groupname=plan.radius_group_name,
        ).exists())

    def test_pin_too_short_rejected(self):
        resp = self.client.post(self.url, {
            'country': 'NG', 'phone': '08031234567', 'pin': '1234',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Subscriber.objects.filter(phone='08031234567').exists())

    def test_pin_too_long_rejected(self):
        resp = self.client.post(self.url, {
            'country': 'NG', 'phone': '08031234567', 'pin': '123456789',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Subscriber.objects.filter(phone='08031234567').exists())

    def test_invalid_phone_rejected(self):
        resp = self.client.post(self.url, {
            'country': 'NG', 'phone': '12345', 'pin': '12345',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Subscriber.objects.exists())

    def test_duplicate_phone_rejected(self):
        Subscriber.objects.create(
            reseller=self.reseller, country=self.country,
            phone='08031234567', verified=True,
        )
        resp = self.client.post(self.url, {
            'country': 'NG', 'phone': '08031234567', 'pin': '12345',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Subscriber.objects.filter(phone='08031234567').count(), 1)

    def test_phone_accepts_international_format(self):
        resp = self.client.post(self.url, {
            'country': 'NG', 'phone': '+2348031234567', 'pin': '12345',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Subscriber.objects.filter(phone='08031234567').exists())


class OperatorStaffSubscriberCreateTests(TestCase):
    def setUp(self):
        self.country = _country()
        self.reseller = _reseller()
        self.staff = User.objects.create_user(
            username='staff@x.com', email='staff@x.com', password='pass1234', is_staff=True,
        )
        self.client = Client()
        self.client.force_login(self.staff)
        self.url = reverse('operator-staff-subscriber-create', args=[self.reseller.pk])

    def test_staff_creates_subscriber_with_source_staff(self):
        resp = self.client.post(self.url, {
            'country': 'NG', 'phone': '08039999999', 'pin': '12345',
        })
        self.assertEqual(resp.status_code, 302)
        sub = Subscriber.objects.get(phone='08039999999')
        self.assertEqual(sub.source, Subscriber.SOURCE_STAFF)
        self.assertEqual(sub.reseller, self.reseller)

    def test_non_staff_user_forbidden(self):
        non_staff = _reseller(email='other@x.com')
        self.client.force_login(non_staff.user)
        resp = self.client.post(self.url, {
            'country': 'NG', 'phone': '08039999999', 'pin': '12345',
        })
        # user_passes_test bounces non-staff to /login/, not the partner detail
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)
        self.assertFalse(Subscriber.objects.filter(phone='08039999999').exists())


class ManuallyCreatedRadcheckContractTests(TestCase):
    """assign_subscriber_to_plan must NOT clobber the raw PIN in radcheck for
    reseller/staff-source subscribers — that would break PPPoE MS-CHAPv2."""

    def setUp(self):
        self.country = _country()
        self.reseller = _reseller()
        self.plan = _plan(self.reseller)

    def test_assign_preserves_raw_pin_for_reseller_source(self):
        sub = Subscriber.objects.create(
            reseller=self.reseller, country=self.country,
            phone='08030000001', verified=True,
            source=Subscriber.SOURCE_RESELLER,
        )
        sub.set_pin('99999')
        sub.generate_auth_token()
        sub.save()
        set_subscriber_radius_password(sub, '99999')

        assign_subscriber_to_plan(sub, self.plan)

        rc = Radcheck.objects.get(username='08030000001', attribute='Cleartext-Password')
        self.assertEqual(rc.value, '99999')  # raw PIN, not auth_token

    def test_assign_writes_auth_token_for_portal_source(self):
        sub = Subscriber.objects.create(
            reseller=self.reseller, country=self.country,
            phone='08030000002', verified=True,
            source=Subscriber.SOURCE_PORTAL,
        )
        sub.generate_auth_token()
        sub.save()

        assign_subscriber_to_plan(sub, self.plan)

        rc = Radcheck.objects.get(username='08030000002', attribute='Cleartext-Password')
        self.assertEqual(rc.value, sub.auth_token)
