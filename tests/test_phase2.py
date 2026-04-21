"""
End-to-end tests for SabiWiFi Phase 2.
Tests cover: subscriber signup flow (OTP), login, PIN management,
plan selection/activation, billing webhook, plan expiry, and the
full subscriber lifecycle.
"""
import json
from decimal import Decimal
from datetime import timedelta
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Reseller, Subscriber
from plans.models import ServicePlan, Subscription
from billing.models import Payment
from routers.models import Router
from radius.models import Radcheck, Radusergroup, Radgroupreply
from radius.utils import assign_subscriber_to_plan


def _create_reseller(name='Test WiFi', email='reseller@x.com', phone='+2348011111111', payment_verified=False):
    """Helper: create a reseller with a Django User."""
    from django.contrib.auth.models import User
    user = User.objects.create_user(username=email, email=email, password='pass1234')
    reseller = Reseller.objects.create(
        user=user, name=name, owner_name='Test Owner', email=email, phone=phone,
        status='active', payment_verified=payment_verified,
        branding={'template': 'modern', 'portal_title': name, 'primary_color': '#0052CC'},
    )
    return reseller


def _create_plan(reseller, name='Free Trial', price=0, duration_days=0, duration_hours=0.5,
                 download=2, upload=2, data_cap=0.1, max_devices=1, is_trial=True):
    """Helper: create a service plan."""
    from django.utils.text import slugify
    return ServicePlan.objects.create(
        reseller=reseller, name=name, slug=slugify(name),
        download_mbps=download, upload_mbps=upload,
        duration_days=duration_days, duration_hours=duration_hours,
        data_cap_gb=Decimal(str(data_cap)) if data_cap else None,
        max_devices=max_devices, price_ngn=Decimal(str(price)),
        is_trial=is_trial, is_active=True,
    )


# ──────────────────────────────────────────────
#  SUBSCRIBER SIGNUP FLOW TESTS
# ──────────────────────────────────────────────

class SubscriberSignupTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.reseller = _create_reseller()
        _create_plan(self.reseller)
        cache.clear()

    def test_signup_sends_otp(self):
        resp = self.client.post(reverse('api-portal-signup'), {
            'phone': '+2348099999999',
            'email': 'sub@example.com',
            'reseller_slug': self.reseller.slug,
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('message', resp.data)
        self.assertIn('reseller_id', resp.data)

    def test_signup_normalizes_phone(self):
        resp = self.client.post(reverse('api-portal-signup'), {
            'phone': '08099999999',
            'email': 'sub@example.com',
            'reseller_slug': self.reseller.slug,
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['phone'], '08099999999')

    def test_signup_rejects_invalid_phone(self):
        resp = self.client.post(reverse('api-portal-signup'), {
            'phone': '12345',
            'email': 'sub@example.com',
            'reseller_slug': self.reseller.slug,
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_signup_rejects_missing_email(self):
        resp = self.client.post(reverse('api-portal-signup'), {
            'phone': '+2348099999999',
            'email': '',
            'reseller_slug': self.reseller.slug,
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_signup_rejects_without_reseller(self):
        resp = self.client.post(reverse('api-portal-signup'), {
            'phone': '+2348099999999',
            'email': 'sub@example.com',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_signup_rejects_duplicate_phone(self):
        Subscriber.objects.create(
            reseller=self.reseller, phone='08099999999', email='existing@x.com', verified=True,
        )
        resp = self.client.post(reverse('api-portal-signup'), {
            'phone': '+2348099999999',
            'email': 'new@example.com',
            'reseller_slug': self.reseller.slug,
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('already exists', resp.data['error'])

    def test_signup_enforces_free_subscriber_limit(self):
        from operator_panel.models import PlatformSettings
        PlatformSettings.load()  # ensure default exists

        # Create 5 active subscriptions
        plan = ServicePlan.objects.filter(reseller=self.reseller).first()
        for i in range(5):
            sub = Subscriber.objects.create(
                reseller=self.reseller, phone=f'+234801000000{i}', verified=True
            )
            Subscription.objects.create(
                subscriber=sub, plan=plan, reseller=self.reseller,
                start_date=timezone.now(), expiry_date=timezone.now() + timedelta(days=30),
                status='active',
            )

        resp = self.client.post(reverse('api-portal-signup'), {
            'phone': '+2348099999999',
            'email': 'sub@example.com',
            'reseller_slug': self.reseller.slug,
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('full', resp.data['error'])


class OTPVerificationTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.reseller = _create_reseller()
        _create_plan(self.reseller)
        cache.clear()

        # Do signup — OTP stored in cache (no otp_debug in response)
        self.client.post(reverse('api-portal-signup'), {
            'phone': '+2348099999999',
            'email': 'sub@example.com',
            'reseller_slug': self.reseller.slug,
        }, format='json')
        self.otp = cache.get(f'otp_08099999999_{self.reseller.id}')['otp']

    def test_verify_correct_otp(self):
        resp = self.client.post(reverse('api-portal-verify'), {
            'phone': '+2348099999999',
            'code': self.otp,
            'reseller_id': self.reseller.id,
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('verify_token', resp.data)

    def test_verify_wrong_otp(self):
        resp = self.client.post(reverse('api-portal-verify'), {
            'phone': '+2348099999999',
            'code': '000000',
            'reseller_id': self.reseller.id,
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Invalid', resp.data['error'])

    def test_verify_expired_otp(self):
        cache.clear()
        resp = self.client.post(reverse('api-portal-verify'), {
            'phone': '+2348099999999',
            'code': self.otp,
            'reseller_id': self.reseller.id,
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('expired', resp.data['error'])

    def test_verify_max_attempts(self):
        for _ in range(5):
            self.client.post(reverse('api-portal-verify'), {
                'phone': '+2348099999999',
                'code': '000000',
                'reseller_id': self.reseller.id,
            }, format='json')
        resp = self.client.post(reverse('api-portal-verify'), {
            'phone': '+2348099999999',
            'code': self.otp,
            'reseller_id': self.reseller.id,
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Too many', resp.data['error'])


class SetPINTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.reseller = _create_reseller()
        _create_plan(self.reseller)
        cache.clear()

        # Signup + verify OTP — read OTP from cache (no otp_debug in response)
        self.client.post(reverse('api-portal-signup'), {
            'phone': '+2348099999999',
            'email': 'sub@example.com',
            'reseller_slug': self.reseller.slug,
        }, format='json')
        otp = cache.get(f'otp_08099999999_{self.reseller.id}')['otp']

        resp = self.client.post(reverse('api-portal-verify'), {
            'phone': '+2348099999999',
            'code': otp,
            'reseller_id': self.reseller.id,
        }, format='json')
        self.verify_token = resp.data['verify_token']

    def test_set_pin_creates_subscriber(self):
        resp = self.client.post(reverse('api-portal-set-pin'), {
            'verify_token': self.verify_token,
            'pin': '12345',
            'pin_confirm': '12345',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertIn('auth_token', resp.data)
        sub = Subscriber.objects.get(phone='08099999999', reseller=self.reseller)
        self.assertTrue(sub.verified)
        self.assertTrue(sub.check_pin('12345'))
        self.assertEqual(len(sub.auth_token), 64)

    def test_set_pin_rejects_mismatch(self):
        resp = self.client.post(reverse('api-portal-set-pin'), {
            'verify_token': self.verify_token,
            'pin': '12345',
            'pin_confirm': '56789',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_set_pin_rejects_short_pin(self):
        resp = self.client.post(reverse('api-portal-set-pin'), {
            'verify_token': self.verify_token,
            'pin': '12',
            'pin_confirm': '12',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_set_pin_rejects_non_numeric(self):
        resp = self.client.post(reverse('api-portal-set-pin'), {
            'verify_token': self.verify_token,
            'pin': 'abcd',
            'pin_confirm': 'abcd',
        }, format='json')
        self.assertEqual(resp.status_code, 400)


# ──────────────────────────────────────────────
#  SUBSCRIBER LOGIN TESTS
# ──────────────────────────────────────────────

class SubscriberLoginTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.reseller = _create_reseller()
        self.sub = Subscriber.objects.create(
            reseller=self.reseller, phone='08099999999',
            email='sub@x.com', verified=True,
        )
        self.sub.set_pin('12345')
        self.sub.generate_auth_token()
        self.sub.save()

    def test_login_with_reseller_slug(self):
        resp = self.client.post(reverse('api-portal-login'), {
            'phone': '+2348099999999',
            'pin': '12345',
            'reseller_slug': self.reseller.slug,
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('auth_token', resp.data)

    def test_login_without_context_global_search(self):
        resp = self.client.post(reverse('api-portal-login'), {
            'phone': '+2348099999999',
            'pin': '12345',
        }, format='json')
        self.assertEqual(resp.status_code, 200)

    def test_login_wrong_pin(self):
        resp = self.client.post(reverse('api-portal-login'), {
            'phone': '+2348099999999',
            'pin': '99999',
            'reseller_slug': self.reseller.slug,
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Incorrect PIN', resp.data['error'])

    def test_login_nonexistent_phone(self):
        resp = self.client.post(reverse('api-portal-login'), {
            'phone': '+2348000000000',
            'pin': '12345',
            'reseller_slug': self.reseller.slug,
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_login_regenerates_auth_token(self):
        old_token = self.sub.auth_token
        self.client.post(reverse('api-portal-login'), {
            'phone': '+2348099999999',
            'pin': '12345',
            'reseller_slug': self.reseller.slug,
        }, format='json')
        self.sub.refresh_from_db()
        self.assertNotEqual(self.sub.auth_token, old_token)


# ──────────────────────────────────────────────
#  PIN MANAGEMENT TESTS
# ──────────────────────────────────────────────

class ChangePINTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.reseller = _create_reseller()
        self.sub = Subscriber.objects.create(
            reseller=self.reseller, phone='+2348099999999',
            email='sub@x.com', verified=True,
        )
        self.sub.set_pin('12345')
        self.sub.generate_auth_token()
        self.sub.save()
        self.token = self.sub.auth_token

    def test_change_pin_success(self):
        resp = self.client.post(reverse('api-portal-change-pin'), {
            'current_pin': '12345',
            'new_pin': '56789',
            'new_pin_confirm': '56789',
        }, format='json', HTTP_X_AUTH_TOKEN=self.token)
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertTrue(self.sub.check_pin('56789'))
        self.assertFalse(self.sub.check_pin('12345'))

    def test_change_pin_wrong_current(self):
        resp = self.client.post(reverse('api-portal-change-pin'), {
            'current_pin': '0000',
            'new_pin': '56789',
            'new_pin_confirm': '56789',
        }, format='json', HTTP_X_AUTH_TOKEN=self.token)
        self.assertEqual(resp.status_code, 400)

    def test_change_pin_updates_radius(self):
        self.client.post(reverse('api-portal-change-pin'), {
            'current_pin': '12345',
            'new_pin': '56789',
            'new_pin_confirm': '56789',
        }, format='json', HTTP_X_AUTH_TOKEN=self.token)
        self.sub.refresh_from_db()
        radcheck = Radcheck.objects.get(username=self.sub.phone, attribute='Cleartext-Password')
        self.assertEqual(radcheck.value, self.sub.auth_token)


class ResetPINTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.reseller = _create_reseller()
        self.sub = Subscriber.objects.create(
            reseller=self.reseller, phone='08099999999',
            email='sub@x.com', verified=True,
        )
        self.sub.set_pin('12345')
        self.sub.save()
        cache.clear()

    def test_reset_pin_full_flow(self):
        # Request reset OTP — stored in cache, not returned in response
        resp = self.client.post(reverse('api-portal-reset-pin'), {
            'phone': '+2348099999999',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        otp = cache.get('pin_reset_08099999999')['otp']

        # Confirm with OTP + new PIN
        resp = self.client.post(reverse('api-portal-reset-pin-confirm'), {
            'phone': '+2348099999999',
            'code': otp,
            'new_pin': '98765',
            'new_pin_confirm': '98765',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertTrue(self.sub.check_pin('98765'))
        self.assertFalse(self.sub.check_pin('12345'))


# ──────────────────────────────────────────────
#  FREE PLAN ACTIVATION TESTS
# ──────────────────────────────────────────────

class FreePlanActivationTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.reseller = _create_reseller()
        self.plan = _create_plan(self.reseller, name='Free Trial', price=0, duration_hours=0.5)
        self.sub = Subscriber.objects.create(
            reseller=self.reseller, phone='+2348099999999',
            email='sub@x.com', verified=True,
        )
        self.sub.set_pin('12345')
        self.sub.generate_auth_token()
        self.sub.save()

    def test_activate_free_plan(self):
        resp = self.client.post(reverse('api-portal-change-plan'), {
            'plan_id': self.plan.id,
        }, format='json', HTTP_X_AUTH_TOKEN=self.sub.auth_token)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Plan activated', resp.data['message'])

        # Subscription created
        sub = Subscription.objects.get(subscriber=self.sub, status='active')
        self.assertEqual(sub.plan, self.plan)

        # RADIUS updated
        self.assertTrue(Radusergroup.objects.filter(
            username=self.sub.phone,
            groupname=self.plan.radius_group_name,
        ).exists())

        # Payment record created
        self.assertTrue(Payment.objects.filter(
            subscriber=self.sub, plan=self.plan, payment_method='free',
        ).exists())

    def test_activate_expires_previous_plan(self):
        # Activate first plan
        self.client.post(reverse('api-portal-change-plan'), {
            'plan_id': self.plan.id,
        }, format='json', HTTP_X_AUTH_TOKEN=self.sub.auth_token)

        # Create and activate second plan
        plan2 = _create_plan(self.reseller, name='Free Week', price=0, duration_days=7, is_trial=False)
        self.sub.refresh_from_db()
        resp = self.client.post(reverse('api-portal-change-plan'), {
            'plan_id': plan2.id,
        }, format='json', HTTP_X_AUTH_TOKEN=self.sub.auth_token)
        self.assertEqual(resp.status_code, 200)

        # First subscription should be expired
        subs = Subscription.objects.filter(subscriber=self.sub).order_by('-start_date')
        self.assertEqual(subs[0].status, 'active')
        self.assertEqual(subs[0].plan, plan2)
        self.assertEqual(subs[1].status, 'expired')

    def test_paid_plan_returns_requires_payment(self):
        reseller = _create_reseller(
            name='Paid WiFi', email='paid@x.com', phone='+2348022222222',
            payment_verified=True,
        )
        paid_plan = _create_plan(reseller, name='Monthly', price=2000, duration_days=30, is_trial=False)
        sub = Subscriber.objects.create(
            reseller=reseller, phone='+2348088888888', email='s@x.com', verified=True,
        )
        sub.set_pin('12345')
        sub.generate_auth_token()
        sub.save()

        resp = self.client.post(reverse('api-portal-change-plan'), {
            'plan_id': paid_plan.id,
        }, format='json', HTTP_X_AUTH_TOKEN=sub.auth_token)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['requires_payment'])


# ──────────────────────────────────────────────
#  BILLING WEBHOOK TEST
# ──────────────────────────────────────────────

@override_settings(PAYSTACK_SECRET_KEY='')
class PaymentWebhookTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.reseller = _create_reseller(payment_verified=True)
        self.reseller.paystack_subaccount_code = 'ACCT_test123'
        self.reseller.save()

        self.plan = _create_plan(
            self.reseller, name='Monthly Basic', price=2000,
            duration_days=30, is_trial=False,
        )
        self.sub = Subscriber.objects.create(
            reseller=self.reseller, phone='+2348099999999',
            email='sub@x.com', verified=True,
        )
        self.sub.generate_auth_token()
        self.sub.save()

        # Create a pending payment
        self.payment = Payment.objects.create(
            subscriber=self.sub,
            plan=self.plan,
            reseller=self.reseller,
            amount_ngn=Decimal('2000'),
            paystack_reference='sw_testref123',
            paystack_status='pending',
            commission_pct_applied=Decimal('15'),
            platform_amount_ngn=Decimal('300'),
            reseller_amount_ngn=Decimal('1700'),
        )

    def test_webhook_activates_plan(self):
        payload = json.dumps({
            'event': 'charge.success',
            'data': {
                'reference': 'sw_testref123',
                'channel': 'card',
                'fees': 13000,
            }
        })
        resp = self.client.post(
            reverse('api-billing-webhook'),
            data=payload,
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.paystack_status, 'success')
        self.assertEqual(self.payment.payment_method, 'card')

        # Subscription created
        sub = Subscription.objects.get(subscriber=self.sub, status='active')
        self.assertEqual(sub.plan, self.plan)
        self.assertGreater(sub.expiry_date, timezone.now())

        # RADIUS updated
        self.assertTrue(Radusergroup.objects.filter(
            username=self.sub.phone,
            groupname=self.plan.radius_group_name,
        ).exists())

    def test_webhook_idempotent(self):
        payload = json.dumps({
            'event': 'charge.success',
            'data': {'reference': 'sw_testref123', 'channel': 'card'}
        })
        # First call
        self.client.post(reverse('api-billing-webhook'), data=payload, content_type='application/json')
        # Second call — should be idempotent
        resp = self.client.post(reverse('api-billing-webhook'), data=payload, content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'already_processed')

        # Only one subscription should exist
        self.assertEqual(Subscription.objects.filter(subscriber=self.sub, status='active').count(), 1)

    def test_webhook_ignores_non_charge_events(self):
        payload = json.dumps({'event': 'transfer.success', 'data': {}})
        resp = self.client.post(reverse('api-billing-webhook'), data=payload, content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'ignored')

    def test_webhook_unknown_reference(self):
        payload = json.dumps({
            'event': 'charge.success',
            'data': {'reference': 'unknown_ref'}
        })
        resp = self.client.post(reverse('api-billing-webhook'), data=payload, content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'not_applicable')


# ──────────────────────────────────────────────
#  SUBSCRIBER ACCOUNT API TESTS
# ──────────────────────────────────────────────

class SubscriberAccountTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.reseller = _create_reseller()
        self.plan = _create_plan(self.reseller)
        self.sub = Subscriber.objects.create(
            reseller=self.reseller, phone='+2348099999999',
            email='sub@x.com', verified=True,
        )
        self.sub.set_pin('12345')
        self.sub.generate_auth_token()
        self.sub.save()

    def test_account_requires_auth(self):
        resp = self.client.get(reverse('api-portal-account'))
        self.assertEqual(resp.status_code, 401)

    def test_account_returns_data(self):
        resp = self.client.get(
            reverse('api-portal-account'),
            HTTP_X_AUTH_TOKEN=self.sub.auth_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['phone'], '+2348099999999')
        self.assertEqual(resp.data['reseller']['slug'], self.reseller.slug)
        self.assertIsNone(resp.data['subscription'])

    def test_account_with_active_subscription(self):
        Subscription.objects.create(
            subscriber=self.sub, plan=self.plan, reseller=self.reseller,
            start_date=timezone.now(),
            expiry_date=timezone.now() + timedelta(days=30),
            status='active',
        )
        resp = self.client.get(
            reverse('api-portal-account'),
            HTTP_X_AUTH_TOKEN=self.sub.auth_token,
        )
        self.assertIsNotNone(resp.data['subscription'])
        self.assertEqual(resp.data['subscription']['plan_name'], 'Free Trial')


class DisconnectSessionTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.reseller = _create_reseller()
        self.sub = Subscriber.objects.create(
            reseller=self.reseller, phone='+2348099999999',
            email='sub@x.com', verified=True,
        )
        self.sub.generate_auth_token()
        self.sub.save()
        self.old_token = self.sub.auth_token

    def test_disconnect_invalidates_token(self):
        resp = self.client.post(
            reverse('api-portal-disconnect'),
            format='json',
            HTTP_X_AUTH_TOKEN=self.old_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertNotEqual(self.sub.auth_token, self.old_token)

        # Old token should not work
        resp = self.client.get(
            reverse('api-portal-account'),
            HTTP_X_AUTH_TOKEN=self.old_token,
        )
        self.assertEqual(resp.status_code, 401)


# ──────────────────────────────────────────────
#  PLAN EXPIRY MANAGEMENT COMMAND TEST
# ──────────────────────────────────────────────

class PlanExpiryTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()
        self.plan = _create_plan(self.reseller)
        self.sub = Subscriber.objects.create(
            reseller=self.reseller, phone='+2348099999999',
            email='sub@x.com', verified=True,
        )
        self.sub.generate_auth_token()
        self.sub.save()

    def test_expired_subscription_deactivated(self):
        # Create a subscription that expired 1 hour ago
        sub = Subscription.objects.create(
            subscriber=self.sub, plan=self.plan, reseller=self.reseller,
            start_date=timezone.now() - timedelta(days=31),
            expiry_date=timezone.now() - timedelta(hours=1),
            status='active',
        )
        assign_subscriber_to_plan(self.sub, self.plan)

        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('check_expiry', stdout=out)

        sub.refresh_from_db()
        self.assertEqual(sub.status, 'expired')
        self.assertIn('1 subscription(s) expired', out.getvalue())

        # RADIUS should be cleared
        self.assertFalse(Radusergroup.objects.filter(username=self.sub.phone).exists())

    def test_active_subscription_not_touched(self):
        sub = Subscription.objects.create(
            subscriber=self.sub, plan=self.plan, reseller=self.reseller,
            start_date=timezone.now(),
            expiry_date=timezone.now() + timedelta(days=30),
            status='active',
        )

        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('check_expiry', stdout=out)

        sub.refresh_from_db()
        self.assertEqual(sub.status, 'active')
        self.assertIn('0 subscription(s) expired', out.getvalue())


# ──────────────────────────────────────────────
#  PORTAL PLANS API — FREE-ONLY FILTER TEST
# ──────────────────────────────────────────────

class PortalPlansFilterTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.reseller = _create_reseller()
        _create_plan(self.reseller, name='Free Trial', price=0)
        _create_plan(self.reseller, name='Paid Monthly', price=2000, duration_days=30, is_trial=False)

    def test_unverified_reseller_shows_only_free_plans(self):
        resp = self.client.get(reverse('api-portal-plans'), {'reseller': self.reseller.slug})
        data = json.loads(resp.content)
        self.assertEqual(len(data['plans']), 1)
        self.assertEqual(data['plans'][0]['name'], 'Free Trial')

    def test_verified_reseller_shows_all_plans(self):
        self.reseller.payment_verified = True
        self.reseller.save()
        resp = self.client.get(reverse('api-portal-plans'), {'reseller': self.reseller.slug})
        data = json.loads(resp.content)
        self.assertEqual(len(data['plans']), 2)


# ──────────────────────────────────────────────
#  FULL SUBSCRIBER LIFECYCLE E2E TEST
# ──────────────────────────────────────────────

class FullSubscriberLifecycleTest(TestCase):
    """
    Full lifecycle: signup → OTP → set PIN → select free plan →
    plan activates → check account → plan expires → renew
    """
    def test_full_subscriber_lifecycle(self):
        cache.clear()
        client = APIClient()

        # Setup: reseller with router and plans
        reseller = _create_reseller()
        free_plan = _create_plan(reseller, name='Free Trial', price=0, duration_hours=0.5)
        weekly_plan = _create_plan(reseller, name='Free Week', price=0, duration_days=7, is_trial=False)

        # 1. Subscriber signs up
        resp = client.post(reverse('api-portal-signup'), {
            'phone': '+2348077777777',
            'email': 'lifecycle@example.com',
            'reseller_slug': reseller.slug,
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        otp = cache.get(f'otp_08077777777_{reseller.id}')['otp']

        # 2. Verify OTP
        resp = client.post(reverse('api-portal-verify'), {
            'phone': '+2348077777777',
            'code': otp,
            'reseller_id': reseller.id,
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        verify_token = resp.data['verify_token']

        # 3. Set PIN
        resp = client.post(reverse('api-portal-set-pin'), {
            'verify_token': verify_token,
            'pin': '43210',
            'pin_confirm': '43210',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        auth_token = resp.data['auth_token']

        # Verify subscriber created (phone stored as local format)
        sub = Subscriber.objects.get(phone='08077777777', reseller=reseller)
        self.assertTrue(sub.verified)

        # 4. Select free trial plan
        resp = client.post(reverse('api-portal-change-plan'), {
            'plan_id': free_plan.id,
        }, format='json', HTTP_X_AUTH_TOKEN=auth_token)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Plan activated', resp.data['message'])

        # 5. Check account — should have active subscription
        resp = client.get(reverse('api-portal-account'), HTTP_X_AUTH_TOKEN=auth_token)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.data['subscription'])
        self.assertEqual(resp.data['subscription']['plan_name'], 'Free Trial')

        # 6. Check RADIUS setup (username = subscriber.phone = local format)
        self.assertTrue(Radusergroup.objects.filter(
            username='08077777777',
            groupname=free_plan.radius_group_name,
        ).exists())
        self.assertTrue(Radcheck.objects.filter(
            username='08077777777',
            attribute='Cleartext-Password',
        ).exists())

        # 7. Subscriber logs in again (simulating return visit via /account)
        resp = client.post(reverse('api-portal-login'), {
            'phone': '+2348077777777',
            'pin': '43210',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['has_active_plan'])
        new_token = resp.data['auth_token']

        # 8. Switch to weekly plan
        resp = client.post(reverse('api-portal-change-plan'), {
            'plan_id': weekly_plan.id,
        }, format='json', HTTP_X_AUTH_TOKEN=new_token)
        self.assertEqual(resp.status_code, 200)

        # Old subscription expired, new active
        subs = Subscription.objects.filter(subscriber=sub).order_by('-start_date')
        self.assertEqual(subs[0].status, 'active')
        self.assertEqual(subs[0].plan, weekly_plan)
        self.assertEqual(subs[1].status, 'expired')

        # 9. RADIUS updated to new plan
        rug = Radusergroup.objects.get(username='08077777777')
        self.assertEqual(rug.groupname, weekly_plan.radius_group_name)

        # 10. Change PIN
        resp = client.post(reverse('api-portal-change-pin'), {
            'current_pin': '43210',
            'new_pin': '88888',
            'new_pin_confirm': '88888',
        }, format='json', HTTP_X_AUTH_TOKEN=new_token)
        self.assertEqual(resp.status_code, 200)

        # 11. Old PIN doesn't work, new PIN does
        sub.refresh_from_db()
        self.assertFalse(sub.check_pin('43210'))
        self.assertTrue(sub.check_pin('88888'))

        # 12. Simulate expiry
        active_sub = Subscription.objects.get(subscriber=sub, status='active')
        active_sub.expiry_date = timezone.now() - timedelta(hours=1)
        active_sub.save()

        from django.core.management import call_command
        from io import StringIO
        call_command('check_expiry', stdout=StringIO())

        active_sub.refresh_from_db()
        self.assertEqual(active_sub.status, 'expired')

        # RADIUS cleared
        self.assertFalse(Radusergroup.objects.filter(username='+2348077777777').exists())
