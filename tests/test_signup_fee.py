"""
Tests for Account Creation Fee feature.
Covers: model fields, verify-otp fee response, initiate-signup-payment,
set-pin fee gate, payment record creation, dashboard settings save.
"""
import json
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.test import TestCase, Client, override_settings
from django.core.cache import cache
from django.contrib.auth.models import User
from rest_framework.test import APIClient


# All tests disable SECURE_SSL_REDIRECT which causes 301s in the test client
_no_ssl = override_settings(SECURE_SSL_REDIRECT=False)

from accounts.models import Reseller, Subscriber
from plans.models import ServicePlan, Subscription
from billing.models import Payment
from operator_panel.models import PlatformSettings


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def _make_reseller(name='Test WiFi', email='r@test.com', phone='08011111111',
                   verified=False, slug=None):
    user = User.objects.create_user(username=email, email=email, password='pass1234')
    r = Reseller.objects.create(
        user=user, name=name, owner_name='Owner', email=email, phone=phone,
        status='active', payment_verified=verified,
        paystack_subaccount_code='ACCT_test123' if verified else '',
        branding={'template': 'modern', 'primary_color': '#0052CC'},
    )
    return r


def _make_free_plan(reseller):
    return ServicePlan.objects.create(
        reseller=reseller, name='Free Trial', slug='free-trial',
        download_mbps=2, upload_mbps=2, duration_hours=Decimal('1'),
        price_ngn=Decimal('0'), is_trial=True,
    )


def _seed_cache_verified(phone, email, reseller_id):
    """Simulate a verified OTP session as portal_verify_otp would create."""
    import secrets
    token = secrets.token_hex(32)
    cache.set(f'verified_{token}', {
        'phone': phone,
        'email': email,
        'reseller_id': reseller_id,
    }, timeout=900)
    return token


# ─────────────────────────────────────────────
#  1. Model fields
# ─────────────────────────────────────────────

class SignupFeeModelFieldsTest(TestCase):
    def setUp(self):
        PlatformSettings.load()

    def test_reseller_signup_fee_defaults(self):
        r = _make_reseller()
        self.assertFalse(r.signup_fee_enabled)
        self.assertEqual(r.signup_fee_amount, Decimal('0'))

    def test_subscriber_signup_fee_paid_default(self):
        r = _make_reseller()
        sub = Subscriber.objects.create(reseller=r, phone='08011111112')
        self.assertFalse(sub.signup_fee_paid)

    def test_payment_type_default(self):
        r = _make_reseller(verified=True)
        sub = Subscriber.objects.create(reseller=r, phone='08011111113')
        p = Payment.objects.create(
            subscriber=sub, reseller=r,
            amount_ngn=Decimal('500'),
            paystack_reference='ref_test_001',
        )
        self.assertEqual(p.payment_type, 'plan')

    def test_payment_type_signup_fee(self):
        r = _make_reseller(verified=True)
        sub = Subscriber.objects.create(reseller=r, phone='08011111114')
        p = Payment.objects.create(
            subscriber=sub, reseller=r,
            plan=None,
            payment_type='signup_fee',
            amount_ngn=Decimal('500'),
            paystack_reference='ref_fee_001',
        )
        self.assertEqual(p.payment_type, 'signup_fee')


# ─────────────────────────────────────────────
#  2. verify-otp: signup_fee_required flag
# ─────────────────────────────────────────────

@_no_ssl
class VerifyOTPSignupFeeResponseTest(TestCase):
    def setUp(self):
        PlatformSettings.load()
        cache.clear()
        self.client = APIClient()

    def _seed_otp(self, phone, reseller):
        otp = '123456'  # must be 6 digits to pass view validation
        cache.set(f'otp_{phone}_{reseller.id}', {
            'otp': otp, 'phone': phone,
            'email': 'user@test.com', 'reseller_id': reseller.id,
            'country_code': 'NG', 'attempts': 0,
        }, timeout=600)
        return otp

    def test_no_fee_returns_signup_fee_required_false(self):
        r = _make_reseller(verified=True)
        phone = '08033333333'
        otp = self._seed_otp(phone, r)

        resp = self.client.post('/api/portal/verify/', {
            'phone': phone, 'code': otp, 'reseller_id': r.id, 'country': 'NG',
        }, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['signup_fee_required'])
        self.assertEqual(resp.data['signup_fee_amount'], 0)

    def test_fee_enabled_returns_signup_fee_required_true(self):
        r = _make_reseller(verified=True)
        r.signup_fee_enabled = True
        r.signup_fee_amount = Decimal('500')
        r.save()
        phone = '08044444444'
        otp = self._seed_otp(phone, r)

        resp = self.client.post('/api/portal/verify/', {
            'phone': phone, 'code': otp, 'reseller_id': r.id, 'country': 'NG',
        }, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['signup_fee_required'])
        self.assertEqual(resp.data['signup_fee_amount'], 500.0)

    def test_fee_enabled_but_no_bank_returns_false(self):
        """Fee is enabled but reseller has no verified bank — fee not required."""
        r = _make_reseller(verified=False)
        r.signup_fee_enabled = True
        r.signup_fee_amount = Decimal('500')
        r.save()
        phone = '08055555555'
        otp = self._seed_otp(phone, r)

        resp = self.client.post('/api/portal/verify/', {
            'phone': phone, 'code': otp, 'reseller_id': r.id, 'country': 'NG',
        }, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['signup_fee_required'])

    def test_fee_enabled_zero_amount_returns_false(self):
        """Toggle on but amount = 0 — treated as disabled."""
        r = _make_reseller(verified=True)
        r.signup_fee_enabled = True
        r.signup_fee_amount = Decimal('0')
        r.save()
        phone = '08066666666'
        otp = self._seed_otp(phone, r)

        resp = self.client.post('/api/portal/verify/', {
            'phone': phone, 'code': otp, 'reseller_id': r.id, 'country': 'NG',
        }, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['signup_fee_required'])


# ─────────────────────────────────────────────
#  3. initiate-signup-payment endpoint
# ─────────────────────────────────────────────

@_no_ssl
class InitiateSignupPaymentTest(TestCase):
    def setUp(self):
        PlatformSettings.load()
        cache.clear()
        self.client = APIClient()

    def test_returns_400_when_no_fee_configured(self):
        r = _make_reseller(verified=True)
        token = _seed_cache_verified('08011111111', 'u@test.com', r.id)

        resp = self.client.post('/api/portal/initiate-signup-payment/', {
            'verify_token': token,
        }, format='json')

        self.assertEqual(resp.status_code, 400)
        self.assertIn('does not charge', resp.data['error'])

    def test_returns_400_when_no_bank_verified(self):
        r = _make_reseller(verified=False)
        r.signup_fee_enabled = True
        r.signup_fee_amount = Decimal('500')
        r.save()
        token = _seed_cache_verified('08011111111', 'u@test.com', r.id)

        resp = self.client.post('/api/portal/initiate-signup-payment/', {
            'verify_token': token,
        }, format='json')

        self.assertEqual(resp.status_code, 400)

    def test_returns_400_when_session_expired(self):
        resp = self.client.post('/api/portal/initiate-signup-payment/', {
            'verify_token': 'notavalidtoken',
        }, format='json')

        self.assertEqual(resp.status_code, 400)
        self.assertIn('expired', resp.data['error'])

    @patch('portal.views.http_requests.post')
    def test_returns_paystack_data_when_fee_configured(self, mock_post):
        mock_post.return_value = MagicMock(
            json=lambda: {
                'status': True,
                'data': {'access_code': 'acc_abc', 'authorization_url': 'https://pay.link'},
            }
        )
        r = _make_reseller(verified=True)
        r.signup_fee_enabled = True
        r.signup_fee_amount = Decimal('500')
        r.save()

        token = _seed_cache_verified('08011111111', 'u@test.com', r.id)

        resp = self.client.post('/api/portal/initiate-signup-payment/', {
            'verify_token': token,
        }, format='json')

        self.assertEqual(resp.status_code, 200)
        self.assertIn('reference', resp.data)
        self.assertIn('access_code', resp.data)
        self.assertEqual(resp.data['fee_amount'], 500.0)
        self.assertIn('amount_kobo', resp.data)
        self.assertEqual(resp.data['amount_kobo'], 50000)

        # Verify cache was set for later verification in set-pin
        pending = cache.get(f'pending_signup_fee_{token}')
        self.assertIsNotNone(pending)
        self.assertEqual(pending['amount_kobo'], 50000)


# ─────────────────────────────────────────────
#  4. set-pin: fee gate
# ─────────────────────────────────────────────

@_no_ssl
class SetPinFeeGateTest(TestCase):
    def setUp(self):
        PlatformSettings.load()
        cache.clear()
        self.client = APIClient()
        self.reseller = _make_reseller(verified=True)
        self.reseller.signup_fee_enabled = True
        self.reseller.signup_fee_amount = Decimal('500')
        self.reseller.save()
        _make_free_plan(self.reseller)

    def test_set_pin_rejected_when_fee_required_but_not_provided(self):
        token = _seed_cache_verified('08077777777', 'u@test.com', self.reseller.id)

        resp = self.client.post('/api/portal/set-pin/', {
            'verify_token': token,
            'pin': '1234',
            'pin_confirm': '1234',
        }, format='json')

        self.assertEqual(resp.status_code, 400)
        self.assertIn('Account creation fee', resp.data['error'])

    def test_set_pin_rejected_with_wrong_reference(self):
        token = _seed_cache_verified('08077777778', 'u@test.com', self.reseller.id)
        # Cache a pending fee with one reference
        cache.set(f'pending_signup_fee_{token}', {
            'reference': 'swf_correct_ref',
            'amount_kobo': 50000,
        }, timeout=1800)

        resp = self.client.post('/api/portal/set-pin/', {
            'verify_token': token,
            'pin': '1234',
            'pin_confirm': '1234',
            'signup_fee_reference': 'swf_wrong_ref',
        }, format='json')

        self.assertEqual(resp.status_code, 400)
        self.assertIn('Invalid signup fee', resp.data['error'])

    @patch('portal.views._verify_paystack_payment')
    @patch('notifications.notify.notify_reseller')
    @patch('notifications.notify.notify_subscriber')
    @patch('notifications.notify.notify_admin_contacts')
    def test_set_pin_succeeds_when_fee_paid(
        self, mock_contacts, mock_sub_notify, mock_res_notify, mock_verify
    ):
        mock_verify.return_value = ({'reference': 'swf_ref_ok', 'channel': 'card'}, None)

        token = _seed_cache_verified('08077777779', 'u@test.com', self.reseller.id)
        cache.set(f'pending_signup_fee_{token}', {
            'reference': 'swf_ref_ok',
            'amount_kobo': 50000,
        }, timeout=1800)

        resp = self.client.post('/api/portal/set-pin/', {
            'verify_token': token,
            'pin': '1234',
            'pin_confirm': '1234',
            'signup_fee_reference': 'swf_ref_ok',
        }, format='json')

        self.assertEqual(resp.status_code, 201)

        # Subscriber created with signup_fee_paid=True
        sub = Subscriber.objects.get(reseller=self.reseller, phone='08077777779')
        self.assertTrue(sub.signup_fee_paid)

        # Payment record created with correct type
        fee_payment = Payment.objects.get(subscriber=sub, payment_type='signup_fee')
        self.assertEqual(fee_payment.amount_ngn, Decimal('500'))
        self.assertEqual(fee_payment.paystack_status, 'success')
        self.assertIsNone(fee_payment.plan)

        # Cache cleared
        self.assertIsNone(cache.get(f'pending_signup_fee_{token}'))

    @patch('portal.views._verify_paystack_payment')
    @patch('notifications.notify.notify_reseller')
    @patch('notifications.notify.notify_subscriber')
    @patch('notifications.notify.notify_admin_contacts')
    def test_set_pin_no_fee_flow_unaffected(
        self, mock_contacts, mock_sub_notify, mock_res_notify, mock_verify
    ):
        """When fee is disabled, set-pin should work without signup_fee_reference."""
        self.reseller.signup_fee_enabled = False
        self.reseller.save()

        token = _seed_cache_verified('08088888880', 'u@test.com', self.reseller.id)

        resp = self.client.post('/api/portal/set-pin/', {
            'verify_token': token,
            'pin': '1234',
            'pin_confirm': '1234',
        }, format='json')

        self.assertEqual(resp.status_code, 201)
        sub = Subscriber.objects.get(reseller=self.reseller, phone='08088888880')
        self.assertFalse(sub.signup_fee_paid)
        self.assertFalse(mock_verify.called)


# ─────────────────────────────────────────────
#  5. Dashboard settings: signup_fee section
# ─────────────────────────────────────────────

@_no_ssl
class SignupFeeSettingsTest(TestCase):
    def setUp(self):
        PlatformSettings.load()
        self.reseller = _make_reseller(verified=True, email='owner@res.com', phone='08099999990')
        self.client = Client()
        self.client.login(username='owner@res.com', password='pass1234')

    def test_can_enable_fee(self):
        resp = self.client.post('/dashboard/settings/', {
            'section': 'signup_fee',
            'signup_fee_enabled': '1',
            'signup_fee_amount': '750',
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.reseller.refresh_from_db()
        self.assertTrue(self.reseller.signup_fee_enabled)
        self.assertEqual(self.reseller.signup_fee_amount, Decimal('750'))

    def test_can_disable_fee(self):
        self.reseller.signup_fee_enabled = True
        self.reseller.signup_fee_amount = Decimal('500')
        self.reseller.save()

        resp = self.client.post('/dashboard/settings/', {
            'section': 'signup_fee',
            'signup_fee_amount': '500',
            # signup_fee_enabled not sent (checkbox unchecked)
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.reseller.refresh_from_db()
        self.assertFalse(self.reseller.signup_fee_enabled)

    def test_cannot_enable_fee_without_verified_bank(self):
        self.reseller.payment_verified = False
        self.reseller.save()

        resp = self.client.post('/dashboard/settings/', {
            'section': 'signup_fee',
            'signup_fee_enabled': '1',
            'signup_fee_amount': '500',
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.reseller.refresh_from_db()
        self.assertFalse(self.reseller.signup_fee_enabled)

    def test_invalid_amount_saved_as_zero(self):
        resp = self.client.post('/dashboard/settings/', {
            'section': 'signup_fee',
            'signup_fee_enabled': '1',
            'signup_fee_amount': 'not-a-number',
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.reseller.refresh_from_db()
        self.assertEqual(self.reseller.signup_fee_amount, Decimal('0'))

    def test_negative_amount_clamped_to_zero(self):
        resp = self.client.post('/dashboard/settings/', {
            'section': 'signup_fee',
            'signup_fee_enabled': '1',
            'signup_fee_amount': '-200',
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.reseller.refresh_from_db()
        self.assertEqual(self.reseller.signup_fee_amount, Decimal('0'))


# ─────────────────────────────────────────────
#  6. Payment type: plan payments unaffected
# ─────────────────────────────────────────────

class PaymentTypeDefaultTest(TestCase):
    def setUp(self):
        PlatformSettings.load()

    def test_existing_plan_payments_default_to_plan_type(self):
        r = _make_reseller(verified=True)
        sub = Subscriber.objects.create(reseller=r, phone='08011112222')
        plan = _make_free_plan(r)
        p = Payment.objects.create(
            subscriber=sub, reseller=r, plan=plan,
            amount_ngn=Decimal('0'),
            paystack_reference='ref_plan_001',
            paystack_status='success',
            payment_method='free',
        )
        self.assertEqual(p.payment_type, 'plan')
