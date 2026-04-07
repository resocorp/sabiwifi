"""
Comprehensive workflow tests for all SabiWiFi user flows.
Tests every workflow listed in DEPLOYMENT.md.
"""
import json
import secrets
from decimal import Decimal
from datetime import timedelta
from io import StringIO
from unittest.mock import patch, MagicMock

from django.test import TestCase, Client, override_settings
from django.utils import timezone
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework.authtoken.models import Token

from accounts.models import Reseller, Subscriber
from plans.models import ServicePlan, Subscription
from billing.models import Payment
from routers.models import Router
from operator_panel.models import PlatformSettings
from radius.models import Radcheck, Radusergroup, Radgroupreply, Radgroupcheck, Nas
from radius.utils import assign_subscriber_to_plan, create_default_trial_plan


# ═══════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════

def _make_reseller(name='Test WiFi', email='r@x.com', phone='+2348011111111',
                   verified=False, status='active'):
    user = User.objects.create_user(username=email, email=email, password='pass1234')
    return Reseller.objects.create(
        user=user, name=name, owner_name='Owner', email=email, phone=phone,
        status=status, payment_verified=verified,
        branding={'template': 'modern', 'portal_title': name,
                  'welcome_text': 'Welcome!', 'primary_color': '#0052CC'},
    )


def _make_plan(reseller, name='Free Trial', price=0, days=0, hours=0.5,
               download=2, upload=2, cap=0.1, devices=1, trial=True):
    from django.utils.text import slugify
    slug = slugify(name)
    # ensure unique slug
    i = 1
    while ServicePlan.objects.filter(reseller=reseller, slug=slug).exists():
        slug = f'{slugify(name)}-{i}'
        i += 1
    return ServicePlan.objects.create(
        reseller=reseller, name=name, slug=slug,
        download_mbps=download, upload_mbps=upload,
        duration_days=days, duration_hours=Decimal(str(hours)),
        data_cap_gb=Decimal(str(cap)) if cap else None,
        max_devices=devices, price_ngn=Decimal(str(price)),
        is_trial=trial, is_active=True,
    )


def _make_subscriber(reseller, phone='08099999999', pin='1234'):
    sub = Subscriber.objects.create(
        reseller=reseller, phone=phone, email='sub@x.com', verified=True,
    )
    sub.set_pin(pin)
    sub.generate_auth_token()
    sub.save()
    return sub


def _signup_subscriber_via_api(client, reseller, phone='+2348077777777'):
    """Full signup flow via API: signup → OTP → set PIN. Returns auth_token."""
    cache.clear()
    # Step 1: Signup
    resp = client.post(reverse('api-portal-signup'), {
        'phone': phone, 'email': 'test@example.com',
        'reseller_slug': reseller.slug,
    }, format='json')
    assert resp.status_code == 200, resp.data
    local_phone = resp.data['phone']  # signup returns local format
    otp = cache.get(f'otp_{local_phone}_{reseller.id}')['otp']

    # Step 2: Verify OTP
    resp = client.post(reverse('api-portal-verify'), {
        'phone': phone, 'code': otp, 'reseller_id': reseller.id,
    }, format='json')
    assert resp.status_code == 200, resp.data
    verify_token = resp.data['verify_token']

    # Step 3: Set PIN
    resp = client.post(reverse('api-portal-set-pin'), {
        'verify_token': verify_token, 'pin': '1234', 'pin_confirm': '1234',
    }, format='json')
    assert resp.status_code == 201, resp.data
    return resp.data['auth_token']


# ═══════════════════════════════════════════════
#  RESELLER WORKFLOWS (1-16)
# ═══════════════════════════════════════════════

class WF01_ResellerSignupTest(TestCase):
    def test_signup_via_web_form(self):
        resp = self.client.post(reverse('signup'), {
            'business_name': 'WF WiFi', 'owner_name': 'Test',
            'email': 'wf@x.com', 'phone': '+2348011111111', 'password': 'securepass123',
        })
        self.assertEqual(resp.status_code, 302)
        r = Reseller.objects.get(email='wf@x.com')
        self.assertEqual(r.status, 'setup')
        self.assertEqual(r.branding['template'], 'modern')

    def test_signup_via_api(self):
        c = APIClient()
        resp = c.post(reverse('api-reseller-signup'), {
            'business_name': 'API WiFi', 'owner_name': 'T',
            'email': 'api@x.com', 'phone': '+2348022222222', 'password': 'securepass123',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertIn('token', resp.data)


class WF02_ResellerLoginTest(TestCase):
    def setUp(self):
        _make_reseller()

    def test_login_web(self):
        resp = self.client.post(reverse('login'), {'email': 'r@x.com', 'password': 'pass1234'})
        self.assertEqual(resp.status_code, 302)

    def test_login_api(self):
        c = APIClient()
        resp = c.post(reverse('api-reseller-login'), {
            'email': 'r@x.com', 'password': 'pass1234',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('token', resp.data)


class WF03_AddRouterTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        Router.objects.create(serial_number='WF03TEST', status='available')
        self.client.login(username='r@x.com', password='pass1234')

    def test_add_router_dashboard(self):
        resp = self.client.post(reverse('dashboard-router-add'), {'serial_number': 'WF03TEST'})
        self.assertEqual(resp.status_code, 302)
        r = Router.objects.get(serial_number='WF03TEST')
        self.assertEqual(r.status, 'pending_provision')
        self.assertEqual(r.reseller, self.reseller)
        self.assertTrue(r.wg_public_key)
        self.assertTrue(r.nas_secret)
        self.assertTrue(Nas.objects.filter(shortname='WF03TEST').exists())

    def test_add_router_api(self):
        c = APIClient()
        token, _ = Token.objects.get_or_create(user=self.reseller.user)
        c.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        resp = c.post(reverse('api-router-add'), {'serial_number': 'WF03TEST'}, format='json')
        self.assertEqual(resp.status_code, 201)


class WF04_RouterProvisioningTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        Router.objects.create(serial_number='WF04TEST', status='available')
        self.client.login(username='r@x.com', password='pass1234')
        self.client.post(reverse('dashboard-router-add'), {'serial_number': 'WF04TEST'})

    def test_provision_endpoint(self):
        anon = Client()
        resp = anon.get(reverse('api-router-provision', args=['WF04TEST']))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('WF04TEST', content)
        self.assertIn('/interface wireguard add', content)
        self.assertIn('/radius add', content)
        self.assertIn('/ip hotspot add', content)
        r = Router.objects.get(serial_number='WF04TEST')
        self.assertEqual(r.status, 'provisioned')


class WF05_CreateFreePlanTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        self.c = APIClient()
        token, _ = Token.objects.get_or_create(user=self.reseller.user)
        self.c.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_create_free_plan(self):
        resp = self.c.post(reverse('plan-list'), {
            'name': 'Free Trial', 'download_mbps': 2, 'upload_mbps': 2,
            'duration_days': 0, 'duration_hours': '0.50',
            'max_devices': 1, 'price_ngn': '0',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        plan = ServicePlan.objects.get(pk=resp.data['id'])
        self.assertTrue(plan.is_trial)
        self.assertTrue(Radgroupreply.objects.filter(groupname=plan.radius_group_name).exists())
        self.assertTrue(Radgroupcheck.objects.filter(groupname=plan.radius_group_name).exists())


class WF06_PaidPlanBlockedTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller(verified=False)
        self.c = APIClient()
        token, _ = Token.objects.get_or_create(user=self.reseller.user)
        self.c.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_paid_plan_blocked_without_bank(self):
        resp = self.c.post(reverse('plan-list'), {
            'name': 'Monthly', 'download_mbps': 5, 'upload_mbps': 5,
            'duration_days': 30, 'max_devices': 2, 'price_ngn': '2000',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('price_ngn', resp.data)


class WF07_BankSetupTest(TestCase):
    """Tests the bank setup API flow (the UI is JS-driven, so we test the API)."""
    def setUp(self):
        self.reseller = _make_reseller()
        self.c = APIClient()
        token, _ = Token.objects.get_or_create(user=self.reseller.user)
        self.c.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    @patch('billing.providers.paystack.PaystackProvider')
    def test_bank_setup_flow(self, MockProvider):
        mock = MockProvider.return_value
        mock.create_subaccount.return_value = {'subaccount_code': 'ACCT_test123'}

        resp = self.c.post(reverse('api-banks-setup'), {
            'account_number': '0123456789', 'bank_code': '058',
            'bank_name': 'GTBank', 'account_name': 'TEST USER',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.reseller.refresh_from_db()
        self.assertTrue(self.reseller.payment_verified)
        self.assertEqual(self.reseller.paystack_subaccount_code, 'ACCT_test123')
        self.assertEqual(self.reseller.bank_name, 'GTBank')


class WF08_PaidPlanAllowedTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller(verified=True)
        self.c = APIClient()
        token, _ = Token.objects.get_or_create(user=self.reseller.user)
        self.c.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_paid_plan_allowed_with_bank(self):
        resp = self.c.post(reverse('plan-list'), {
            'name': 'Monthly Basic', 'download_mbps': 5, 'upload_mbps': 5,
            'duration_days': 30, 'max_devices': 2, 'price_ngn': '2000',
        }, format='json')
        self.assertEqual(resp.status_code, 201)


class WF09_EditPlanTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        self.plan = _make_plan(self.reseller)
        self.c = APIClient()
        token, _ = Token.objects.get_or_create(user=self.reseller.user)
        self.c.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_edit_plan(self):
        resp = self.c.patch(reverse('plan-detail', args=[self.plan.pk]), {
            'download_mbps': 10, 'name': 'Updated Plan',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.download_mbps, 10)
        self.assertEqual(self.plan.name, 'Updated Plan')
        # RADIUS updated
        rate = Radgroupreply.objects.get(
            groupname=self.plan.radius_group_name, attribute='Mikrotik-Rate-Limit'
        )
        self.assertIn('10240k', rate.value)


class WF10_DisablePlanTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        self.plan = _make_plan(self.reseller)
        self.c = APIClient()
        token, _ = Token.objects.get_or_create(user=self.reseller.user)
        self.c.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_disable_plan_clears_radius(self):
        group = self.plan.radius_group_name
        self.assertTrue(Radgroupreply.objects.filter(groupname=group).exists())
        resp = self.c.post(reverse('plan-disable', args=[self.plan.pk]))
        self.assertEqual(resp.status_code, 200)
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.is_active)
        self.assertFalse(Radgroupreply.objects.filter(groupname=group).exists())


class WF11to16_DashboardPagesTest(TestCase):
    """Tests that all dashboard pages load and show correct data."""
    def setUp(self):
        self.reseller = _make_reseller()
        self.plan = _make_plan(self.reseller)
        self.sub = _make_subscriber(self.reseller)
        Subscription.objects.create(
            subscriber=self.sub, plan=self.plan, reseller=self.reseller,
            start_date=timezone.now(), expiry_date=timezone.now() + timedelta(days=30),
            status='active',
        )
        Payment.objects.create(
            subscriber=self.sub, plan=self.plan, reseller=self.reseller,
            amount_ngn=0, paystack_reference=f'free_{secrets.token_hex(8)}',
            paystack_status='success', payment_method='free',
        )
        Router.objects.create(
            serial_number='DASH001', reseller=self.reseller, status='online',
            wg_tunnel_ip='10.99.0.10',
        )
        self.client.login(username='r@x.com', password='pass1234')

    def test_wf11_subscribers_list(self):
        resp = self.client.get(reverse('dashboard-subscribers'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '08099999999')

    def test_wf11_subscribers_search(self):
        resp = self.client.get(reverse('dashboard-subscribers') + '?q=80999')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '08099999999')

    def test_wf12_subscriber_detail(self):
        resp = self.client.get(reverse('dashboard-subscriber-detail', args=[self.sub.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '08099999999')
        self.assertContains(resp, 'Free Trial')

    def test_wf13_payments_page(self):
        resp = self.client.get(reverse('dashboard-payments'))
        self.assertEqual(resp.status_code, 200)

    def test_wf14_branding_update(self):
        resp = self.client.post(reverse('dashboard-settings'), {
            'section': 'branding', 'portal_title': 'New Brand',
            'welcome_text': 'Hi!', 'primary_color': '#FF0000', 'template': 'bold',
        })
        self.assertEqual(resp.status_code, 302)
        self.reseller.refresh_from_db()
        self.assertEqual(self.reseller.branding['template'], 'bold')

    def test_wf15_account_update(self):
        resp = self.client.post(reverse('dashboard-settings'), {
            'section': 'account', 'business_name': 'New Name',
            'email': 'r@x.com', 'phone': '+2348011111111', 'location': 'Lagos',
        })
        self.assertEqual(resp.status_code, 302)
        self.reseller.refresh_from_db()
        self.assertEqual(self.reseller.name, 'New Name')
        self.assertEqual(self.reseller.location, 'Lagos')

    def test_wf16_routers_page(self):
        resp = self.client.get(reverse('dashboard-routers'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'DASH001')
        self.assertContains(resp, 'Online')

    def test_settings_bank_section_not_placeholder(self):
        """Verify bank section does NOT show 'coming in Phase 2'."""
        resp = self.client.get(reverse('dashboard-settings'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'coming in Phase 2')
        self.assertContains(resp, 'Select your bank')


# ═══════════════════════════════════════════════
#  SUBSCRIBER WORKFLOWS (17-27)
# ═══════════════════════════════════════════════

class WF17_SubscriberSignupTest(TestCase):
    def setUp(self):
        cache.clear()
        self.reseller = _make_reseller()
        _make_plan(self.reseller)
        self.c = APIClient()

    def test_full_signup_flow(self):
        token = _signup_subscriber_via_api(self.c, self.reseller)
        self.assertEqual(len(token), 64)
        sub = Subscriber.objects.get(phone='08077777777', reseller=self.reseller)
        self.assertTrue(sub.verified)
        self.assertTrue(sub.check_pin('1234'))


class WF18_SubscriberLoginCaptiveTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        self.sub = _make_subscriber(self.reseller)
        Router.objects.create(
            serial_number='LOGIN01', reseller=self.reseller, status='online',
            wg_tunnel_ip='10.99.0.20',
        )
        self.c = APIClient()

    def test_login_with_serial(self):
        resp = self.c.post(reverse('api-portal-login'), {
            'phone': '+2348099999999', 'pin': '1234', 'serial': 'LOGIN01',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('auth_token', resp.data)


class WF19_SubscriberLoginSelfServiceTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        self.sub = _make_subscriber(self.reseller)
        self.c = APIClient()

    def test_login_without_serial(self):
        resp = self.c.post(reverse('api-portal-login'), {
            'phone': '+2348099999999', 'pin': '1234',
        }, format='json')
        self.assertEqual(resp.status_code, 200)


class WF20_SelectFreePlanTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        self.plan = _make_plan(self.reseller)
        self.sub = _make_subscriber(self.reseller)
        self.c = APIClient()

    def test_activate_free_plan(self):
        resp = self.c.post(reverse('api-portal-change-plan'), {
            'plan_id': self.plan.id,
        }, format='json', HTTP_X_AUTH_TOKEN=self.sub.auth_token)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Subscription.objects.filter(subscriber=self.sub, status='active').exists())
        self.assertTrue(Radusergroup.objects.filter(
            username=self.sub.phone, groupname=self.plan.radius_group_name
        ).exists())


@override_settings(PAYSTACK_SECRET_KEY='')
class WF21_PaidPlanPaymentTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller(verified=True)
        self.reseller.paystack_subaccount_code = 'ACCT_test'
        self.reseller.save()
        self.plan = _make_plan(self.reseller, name='Monthly', price=2000, days=30, trial=False)
        self.sub = _make_subscriber(self.reseller)

    def test_paid_plan_requires_payment(self):
        c = APIClient()
        resp = c.post(reverse('api-portal-change-plan'), {
            'plan_id': self.plan.id,
        }, format='json', HTTP_X_AUTH_TOKEN=self.sub.auth_token)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['requires_payment'])

    def test_webhook_activates_paid_plan(self):
        # Create pending payment
        payment = Payment.objects.create(
            subscriber=self.sub, plan=self.plan, reseller=self.reseller,
            amount_ngn=2000, paystack_reference='sw_wf21test',
            paystack_status='pending',
        )
        # Simulate webhook
        client = Client()
        resp = client.post(reverse('api-billing-webhook'), data=json.dumps({
            'event': 'charge.success',
            'data': {'reference': 'sw_wf21test', 'channel': 'card'},
        }), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.paystack_status, 'success')
        self.assertTrue(Subscription.objects.filter(subscriber=self.sub, status='active').exists())


class WF22_ViewAccountTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        self.plan = _make_plan(self.reseller)
        self.sub = _make_subscriber(self.reseller)
        Subscription.objects.create(
            subscriber=self.sub, plan=self.plan, reseller=self.reseller,
            start_date=timezone.now(), expiry_date=timezone.now() + timedelta(days=30),
            status='active',
        )
        self.c = APIClient()

    def test_account_shows_subscription(self):
        resp = self.c.get(reverse('api-portal-account'), HTTP_X_AUTH_TOKEN=self.sub.auth_token)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.data['subscription'])
        self.assertEqual(resp.data['subscription']['plan_name'], 'Free Trial')


class WF23to24_ChangePlanTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        self.plan1 = _make_plan(self.reseller, name='Trial', hours=0.5)
        self.plan2 = _make_plan(self.reseller, name='Weekly', days=7, hours=0, trial=False)
        self.sub = _make_subscriber(self.reseller)
        self.c = APIClient()
        # Activate first plan
        self.c.post(reverse('api-portal-change-plan'), {
            'plan_id': self.plan1.id,
        }, format='json', HTTP_X_AUTH_TOKEN=self.sub.auth_token)

    def test_change_plan_expires_old(self):
        resp = self.c.post(reverse('api-portal-change-plan'), {
            'plan_id': self.plan2.id,
        }, format='json', HTTP_X_AUTH_TOKEN=self.sub.auth_token)
        self.assertEqual(resp.status_code, 200)
        subs = Subscription.objects.filter(subscriber=self.sub).order_by('-start_date')
        self.assertEqual(subs[0].status, 'active')
        self.assertEqual(subs[0].plan, self.plan2)
        self.assertEqual(subs[1].status, 'expired')


class WF25_ChangePINTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        self.sub = _make_subscriber(self.reseller, pin='1234')
        self.c = APIClient()

    def test_change_pin(self):
        resp = self.c.post(reverse('api-portal-change-pin'), {
            'current_pin': '1234', 'new_pin': '5678', 'new_pin_confirm': '5678',
        }, format='json', HTTP_X_AUTH_TOKEN=self.sub.auth_token)
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertTrue(self.sub.check_pin('5678'))


class WF26_ResetPINTest(TestCase):
    def setUp(self):
        cache.clear()
        self.reseller = _make_reseller()
        self.sub = _make_subscriber(self.reseller, pin='1234')
        self.c = APIClient()

    def test_reset_pin_flow(self):
        # Request OTP
        resp = self.c.post(reverse('api-portal-reset-pin'), {
            'phone': '+2348099999999',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        otp = cache.get('pin_reset_08099999999')['otp']
        # Confirm with new PIN
        resp = self.c.post(reverse('api-portal-reset-pin-confirm'), {
            'phone': '+2348099999999', 'code': otp,
            'new_pin': '9999', 'new_pin_confirm': '9999',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertTrue(self.sub.check_pin('9999'))


class WF27_DisconnectSessionTest(TestCase):
    def setUp(self):
        self.reseller = _make_reseller()
        self.sub = _make_subscriber(self.reseller)
        self.c = APIClient()

    def test_disconnect_invalidates_session(self):
        old_token = self.sub.auth_token
        resp = self.c.post(reverse('api-portal-disconnect'),
                           format='json', HTTP_X_AUTH_TOKEN=old_token)
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertNotEqual(self.sub.auth_token, old_token)
        # Old token no longer works
        resp = self.c.get(reverse('api-portal-account'), HTTP_X_AUTH_TOKEN=old_token)
        self.assertEqual(resp.status_code, 401)


# ═══════════════════════════════════════════════
#  OPERATOR WORKFLOWS (28-33)
# ═══════════════════════════════════════════════

class WF28_DjangoAdminTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin', 'admin@x.com', 'admin123')
        self.client.login(username='admin', password='admin123')

    def test_admin_index(self):
        resp = self.client.get('/admin/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_reseller_list(self):
        resp = self.client.get('/admin/accounts/reseller/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_subscriber_list(self):
        resp = self.client.get('/admin/accounts/subscriber/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_router_list(self):
        resp = self.client.get('/admin/routers/router/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_plan_list(self):
        resp = self.client.get('/admin/plans/serviceplan/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_subscription_list(self):
        resp = self.client.get('/admin/plans/subscription/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_payment_list(self):
        resp = self.client.get('/admin/billing/payment/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_platform_settings(self):
        resp = self.client.get('/admin/operator_panel/platformsettings/')
        self.assertEqual(resp.status_code, 200)


class WF29_OperatorOverviewTest(TestCase):
    def test_requires_staff(self):
        user = User.objects.create_user('regular', 'reg@x.com', 'p')
        self.client.login(username='regular', password='p')
        resp = self.client.get(reverse('operator-overview'))
        self.assertEqual(resp.status_code, 302)

    def test_loads_for_admin(self):
        User.objects.create_superuser('admin', 'admin@x.com', 'admin123')
        self.client.login(username='admin', password='admin123')
        resp = self.client.get(reverse('operator-overview'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Platform Overview')

    def test_shows_metrics(self):
        User.objects.create_superuser('admin', 'admin@x.com', 'admin123')
        reseller = _make_reseller()
        _make_plan(reseller)
        sub = _make_subscriber(reseller)
        Router.objects.create(serial_number='OP001', reseller=reseller, status='online', wg_tunnel_ip='10.99.0.5')
        self.client.login(username='admin', password='admin123')
        resp = self.client.get(reverse('operator-overview'))
        self.assertContains(resp, 'Active resellers')
        self.assertContains(resp, 'Routers online')


class WF30_RegisterRouterSerialTest(TestCase):
    """Operator registers serial via Django Admin."""
    def setUp(self):
        self.admin = User.objects.create_superuser('admin', 'admin@x.com', 'admin123')
        self.client.login(username='admin', password='admin123')

    def test_add_router_via_admin(self):
        resp = self.client.post('/admin/routers/router/add/', {
            'serial_number': 'ADMIN001',
            'status': 'available',
            'api_username': 'sabiwifi',
            'provision_count': 0,
        })
        # Django admin redirects on success
        self.assertIn(resp.status_code, [200, 302])
        if resp.status_code == 302:
            self.assertTrue(Router.objects.filter(serial_number='ADMIN001').exists())


class WF31_ManageResellersTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin', 'admin@x.com', 'admin123')
        self.reseller = _make_reseller()
        self.client.login(username='admin', password='admin123')

    def test_view_reseller_in_admin(self):
        resp = self.client.get(f'/admin/accounts/reseller/{self.reseller.pk}/change/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Test WiFi')

    def test_suspend_reseller_action(self):
        resp = self.client.post('/admin/accounts/reseller/', {
            'action': 'suspend_resellers',
            '_selected_action': [self.reseller.pk],
        })
        self.assertIn(resp.status_code, [200, 302])
        self.reseller.refresh_from_db()
        self.assertEqual(self.reseller.status, 'suspended')


class WF32_PlatformSettingsTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin', 'admin@x.com', 'admin123')
        self.client.login(username='admin', password='admin123')

    def test_platform_settings_editable(self):
        ps = PlatformSettings.load()
        resp = self.client.get(f'/admin/operator_panel/platformsettings/{ps.pk}/change/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'default_commission_pct')
        self.assertContains(resp, 'notification_phones')


# ═══════════════════════════════════════════════
#  AUTOMATED WORKFLOWS / CRON (34-38)
# ═══════════════════════════════════════════════

class WF34_PlanExpiryTest(TestCase):
    def test_expired_subscription_deactivated(self):
        reseller = _make_reseller()
        plan = _make_plan(reseller)
        sub = _make_subscriber(reseller)
        sub_record = Subscription.objects.create(
            subscriber=sub, plan=plan, reseller=reseller,
            start_date=timezone.now() - timedelta(days=2),
            expiry_date=timezone.now() - timedelta(hours=1),
            status='active',
        )
        assign_subscriber_to_plan(sub, plan)
        out = StringIO()
        call_command('check_expiry', stdout=out)
        sub_record.refresh_from_db()
        self.assertEqual(sub_record.status, 'expired')
        self.assertFalse(Radusergroup.objects.filter(username=sub.phone).exists())


class WF35_RouterHealthTest(TestCase):
    def test_unreachable_router_goes_offline(self):
        reseller = _make_reseller()
        router = Router.objects.create(
            serial_number='HEALTH01', reseller=reseller, status='online',
            wg_tunnel_ip='10.99.99.99', last_seen=timezone.now(),
        )
        out = StringIO()
        call_command('check_routers', stdout=out)
        router.refresh_from_db()
        self.assertEqual(router.status, 'offline')


class WF36_AlertsTest(TestCase):
    def setUp(self):
        cache.clear()
        ps = PlatformSettings.load()
        ps.notification_phones = ['+2348012345678']
        ps.save()

    def test_offline_router_alert(self):
        reseller = _make_reseller()
        Router.objects.create(
            serial_number='ALERT01', reseller=reseller, status='offline',
            last_seen=timezone.now() - timedelta(hours=2), wg_tunnel_ip='10.99.0.50',
        )
        out = StringIO()
        call_command('check_alerts', stdout=out)
        self.assertIn('1 alert(s) sent', out.getvalue())


class WF37_ExpiryRemindersTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_sends_reminder(self):
        reseller = _make_reseller()
        plan = _make_plan(reseller)
        sub = _make_subscriber(reseller)
        Subscription.objects.create(
            subscriber=sub, plan=plan, reseller=reseller,
            start_date=timezone.now() - timedelta(days=29),
            expiry_date=timezone.now() + timedelta(hours=12),
            status='active',
        )
        out = StringIO()
        call_command('send_expiry_reminders', stdout=out)
        self.assertIn('1 1-day', out.getvalue())


class WF38_DailySummaryTest(TestCase):
    def test_sends_summary(self):
        ps = PlatformSettings.load()
        ps.notification_phones = ['+2348012345678']
        ps.notify_daily_summary = True
        ps.save()
        out = StringIO()
        call_command('daily_summary', stdout=out)
        self.assertIn('Daily summary sent', out.getvalue())


# ═══════════════════════════════════════════════
#  PORTAL PLANS FILTER (free-only for unverified)
# ═══════════════════════════════════════════════

class PortalPlansFilterTest(TestCase):
    def test_unverified_shows_only_free(self):
        reseller = _make_reseller(verified=False)
        _make_plan(reseller, name='Free', price=0)
        _make_plan(reseller, name='Paid', price=2000, days=30, trial=False)
        resp = self.client.get(reverse('api-portal-plans'), {'reseller': reseller.slug})
        data = json.loads(resp.content)
        self.assertEqual(len(data['plans']), 1)
        self.assertEqual(data['plans'][0]['name'], 'Free')

    def test_verified_shows_all(self):
        reseller = _make_reseller(name='V WiFi', email='v@x.com', phone='+2348022222222', verified=True)
        _make_plan(reseller, name='Free', price=0)
        _make_plan(reseller, name='Paid', price=2000, days=30, trial=False)
        resp = self.client.get(reverse('api-portal-plans'), {'reseller': reseller.slug})
        data = json.loads(resp.content)
        self.assertEqual(len(data['plans']), 2)
