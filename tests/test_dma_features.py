"""
Tests for DMA Radius Manager feature parity implementation:
- Phase 1: Voucher / Prepaid Card System
- Phase 2: Wallet + Refill Cards + Auto-Renewal
- Phase 3: Enhanced Plans (Burst, Caps, IP Pool, Fallback, PPPoE)
- Phase 4: Daily Quotas
- Phase 5: Online Users & Session Management
- Phase 6: Reports & Analytics
"""
import json
import secrets
from decimal import Decimal
from datetime import timedelta

from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Reseller, Subscriber
from plans.models import ServicePlan, Subscription, DailyUsageSnapshot
from billing.models import Payment, Wallet, WalletTransaction
from routers.models import Router
from vouchers.models import VoucherBatch, Voucher, RefillCardBatch, RefillCard
from vouchers.utils import generate_voucher_batch, generate_refill_batch
from vouchers.radius import activate_voucher
from billing.wallet import (
    get_or_create_wallet, credit_wallet, debit_wallet,
    purchase_plan_from_wallet, renew_plan_from_wallet, InsufficientBalance,
)
from radius.models import Radgroupreply, Radgroupcheck, Radusergroup, Radcheck
from radius.utils import sync_plan_to_radius, assign_subscriber_to_plan


def _create_reseller(username='testreseller'):
    user = User.objects.create_user(username=username, password='testpass123')
    return Reseller.objects.create(
        user=user, name='Test Reseller', slug=username,
        status='active', payment_verified=True,
    )


def _create_plan(reseller, **kwargs):
    defaults = dict(
        name='Test Plan', slug='test-plan',
        download_mbps=10, upload_mbps=5,
        duration_days=30, price_ngn=Decimal('500'),
        is_active=True,
    )
    defaults.update(kwargs)
    return ServicePlan.objects.create(reseller=reseller, **defaults)


def _create_subscriber(reseller, phone='08066137843'):
    return Subscriber.objects.create(
        reseller=reseller, phone=phone, verified=True,
        auth_token=secrets.token_hex(32),
    )


# ──────────────────────────────────────────────
#  PHASE 1: VOUCHER SYSTEM
# ──────────────────────────────────────────────

class VoucherBatchModelTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()
        self.plan = _create_plan(self.reseller)

    def test_create_batch(self):
        batch = VoucherBatch.objects.create(
            reseller=self.reseller, plan=self.plan,
            name='Test Batch', quantity=10, pin_length=8,
        )
        self.assertEqual(batch.quantity, 10)
        self.assertEqual(batch.status, 'active')

    def test_generate_voucher_batch(self):
        batch = VoucherBatch.objects.create(
            reseller=self.reseller, plan=self.plan,
            name='Gen Batch', quantity=50, pin_length=8, prefix='SW',
        )
        count = generate_voucher_batch(batch)
        self.assertEqual(count, 50)
        self.assertEqual(Voucher.objects.filter(batch=batch).count(), 50)

        # All PINs should start with prefix
        for v in Voucher.objects.filter(batch=batch):
            self.assertTrue(v.pin.startswith('SW'))
            self.assertEqual(len(v.pin), 10)  # 2 prefix + 8 digits

    def test_pin_uniqueness(self):
        """Two batches from same reseller should have unique PINs."""
        b1 = VoucherBatch.objects.create(
            reseller=self.reseller, plan=self.plan,
            name='Batch 1', quantity=20, pin_length=6,
        )
        b2 = VoucherBatch.objects.create(
            reseller=self.reseller, plan=self.plan,
            name='Batch 2', quantity=20, pin_length=6,
        )
        generate_voucher_batch(b1)
        generate_voucher_batch(b2)

        all_pins = list(Voucher.objects.filter(reseller=self.reseller).values_list('pin', flat=True))
        self.assertEqual(len(all_pins), len(set(all_pins)))

    def test_batch_counts(self):
        batch = VoucherBatch.objects.create(
            reseller=self.reseller, plan=self.plan,
            name='Count Batch', quantity=5, pin_length=8,
        )
        generate_voucher_batch(batch)

        self.assertEqual(batch.vouchers_available, 5)
        self.assertEqual(batch.vouchers_used, 0)

        # Activate one
        v = batch.vouchers.first()
        v.status = 'active'
        v.save()

        self.assertEqual(batch.vouchers_available, 4)
        self.assertEqual(batch.vouchers_used, 1)


class VoucherActivationTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()
        self.plan = _create_plan(self.reseller)
        self.batch = VoucherBatch.objects.create(
            reseller=self.reseller, plan=self.plan,
            name='Activation Test', quantity=5, pin_length=8,
            validity_type='from_activation', validity_days=7,
        )
        generate_voucher_batch(self.batch)

    def test_activate_voucher_creates_subscriber(self):
        voucher = self.batch.vouchers.first()
        subscriber, auth_token = activate_voucher(voucher)

        voucher.refresh_from_db()
        self.assertEqual(voucher.status, 'active')
        self.assertIsNotNone(voucher.activated_at)
        self.assertIsNotNone(voucher.expires_at)
        self.assertEqual(subscriber.phone, voucher.pin)
        self.assertTrue(subscriber.is_voucher_user)
        self.assertTrue(subscriber.verified)

    def test_activate_voucher_creates_subscription(self):
        voucher = self.batch.vouchers.first()
        subscriber, _ = activate_voucher(voucher)

        sub = Subscription.objects.get(subscriber=subscriber, status='active')
        self.assertEqual(sub.plan, self.plan)

    def test_activate_voucher_writes_radius(self):
        voucher = self.batch.vouchers.first()
        subscriber, _ = activate_voucher(voucher)

        # Check radusergroup
        rug = Radusergroup.objects.get(username=voucher.pin)
        self.assertEqual(rug.groupname, self.plan.radius_group_name)

        # Check radcheck
        rc = Radcheck.objects.get(username=voucher.pin, attribute='Cleartext-Password')
        self.assertEqual(rc.value, subscriber.auth_token)

    def test_activate_voucher_validity_days(self):
        voucher = self.batch.vouchers.first()
        subscriber, _ = activate_voucher(voucher)

        voucher.refresh_from_db()
        expected = voucher.activated_at + timedelta(days=7)
        self.assertAlmostEqual(
            voucher.expires_at.timestamp(), expected.timestamp(), delta=5
        )

    def test_reactivate_voucher_refreshes_token(self):
        voucher = self.batch.vouchers.first()
        _, token1 = activate_voucher(voucher)
        _, token2 = activate_voucher(voucher)

        # Should refresh auth token on re-login
        self.assertNotEqual(token1, token2)

    def test_activate_fixed_date_validity(self):
        future = timezone.now() + timedelta(days=90)
        batch = VoucherBatch.objects.create(
            reseller=self.reseller, plan=self.plan,
            name='Fixed Date', quantity=2, pin_length=8,
            validity_type='fixed_date', validity_fixed_date=future,
        )
        generate_voucher_batch(batch)
        voucher = batch.vouchers.first()
        activate_voucher(voucher)
        voucher.refresh_from_db()
        self.assertAlmostEqual(voucher.expires_at.timestamp(), future.timestamp(), delta=5)


class VoucherPortalLoginTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()
        self.plan = _create_plan(self.reseller)
        self.router = Router.objects.create(
            serial_number='TEST123', device_type='mikrotik',
            reseller=self.reseller, status='online',
        )
        self.batch = VoucherBatch.objects.create(
            reseller=self.reseller, plan=self.plan,
            name='Portal Test', quantity=3, pin_length=8, prefix='VT',
            validity_type='from_activation', validity_days=30,
        )
        generate_voucher_batch(self.batch)
        self.client = APIClient()

    def test_voucher_login_success(self):
        voucher = self.batch.vouchers.first()
        resp = self.client.post('/api/portal/voucher-login/', {
            'pin': voucher.pin,
            'serial': 'TEST123',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('auth_token', resp.data)
        self.assertEqual(resp.data['plan_name'], self.plan.name)

    def test_voucher_login_invalid_pin(self):
        resp = self.client.post('/api/portal/voucher-login/', {
            'pin': 'INVALID12345',
            'serial': 'TEST123',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Invalid', resp.data['error'])

    def test_voucher_login_disabled_batch(self):
        self.batch.status = 'disabled'
        self.batch.save()
        voucher = self.batch.vouchers.first()
        resp = self.client.post('/api/portal/voucher-login/', {
            'pin': voucher.pin,
            'serial': 'TEST123',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_voucher_login_expired_voucher(self):
        voucher = self.batch.vouchers.first()
        voucher.status = 'expired'
        voucher.save()
        resp = self.client.post('/api/portal/voucher-login/', {
            'pin': voucher.pin,
            'serial': 'TEST123',
        }, format='json')
        self.assertEqual(resp.status_code, 400)


class VoucherDashboardTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()
        self.plan = _create_plan(self.reseller)
        self.client = Client()
        self.client.login(username='testreseller', password='testpass123')

    def test_voucher_batches_page(self):
        resp = self.client.get(reverse('dashboard-voucher-batches'))
        self.assertEqual(resp.status_code, 200)

    def test_create_batch_page(self):
        resp = self.client.get(reverse('dashboard-voucher-batch-create'))
        self.assertEqual(resp.status_code, 200)

    def test_create_batch_post(self):
        resp = self.client.post(reverse('dashboard-voucher-batch-create'), {
            'name': 'New Batch',
            'plan': self.plan.pk,
            'quantity': 10,
            'pin_length': 8,
            'prefix': 'NB',
            'validity_type': 'from_activation',
            'validity_days': 30,
            'simultaneous_use': 1,
        })
        self.assertEqual(resp.status_code, 302)
        batch = VoucherBatch.objects.get(name='New Batch')
        self.assertEqual(batch.vouchers.count(), 10)

    def test_batch_detail_page(self):
        batch = VoucherBatch.objects.create(
            reseller=self.reseller, plan=self.plan,
            name='Detail Test', quantity=3, pin_length=8,
        )
        generate_voucher_batch(batch)
        resp = self.client.get(reverse('dashboard-voucher-batch-detail', args=[batch.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_csv_export(self):
        batch = VoucherBatch.objects.create(
            reseller=self.reseller, plan=self.plan,
            name='CSV Test', quantity=5, pin_length=8,
        )
        generate_voucher_batch(batch)
        resp = self.client.get(reverse('dashboard-voucher-batch-csv', args=[batch.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/csv')
        content = resp.content.decode()
        self.assertIn('pin', content)
        lines = content.strip().split('\n')
        self.assertEqual(len(lines), 6)  # header + 5 vouchers

    def test_toggle_batch(self):
        batch = VoucherBatch.objects.create(
            reseller=self.reseller, plan=self.plan,
            name='Toggle Test', quantity=3, pin_length=8,
        )
        generate_voucher_batch(batch)

        resp = self.client.post(reverse('dashboard-voucher-batch-toggle', args=[batch.pk]))
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'disabled')
        self.assertEqual(batch.vouchers.filter(status='disabled').count(), 3)

        # Re-enable
        resp = self.client.post(reverse('dashboard-voucher-batch-toggle', args=[batch.pk]))
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'active')
        self.assertEqual(batch.vouchers.filter(status='unused').count(), 3)


# ──────────────────────────────────────────────
#  PHASE 2: WALLET + REFILL + AUTO-RENEWAL
# ──────────────────────────────────────────────

class WalletTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()
        self.subscriber = _create_subscriber(self.reseller)
        self.plan = _create_plan(self.reseller)

    def test_get_or_create_wallet(self):
        wallet = get_or_create_wallet(self.subscriber)
        self.assertEqual(wallet.balance_ngn, Decimal('0'))
        # Second call returns same wallet
        wallet2 = get_or_create_wallet(self.subscriber)
        self.assertEqual(wallet.pk, wallet2.pk)

    def test_credit_wallet(self):
        txn = credit_wallet(self.subscriber, 500, 'paystack_topup', reference='PS-123')
        wallet = Wallet.objects.get(subscriber=self.subscriber)
        self.assertEqual(wallet.balance_ngn, Decimal('500'))
        self.assertEqual(txn.amount_ngn, Decimal('500'))
        self.assertEqual(txn.balance_after, Decimal('500'))

    def test_debit_wallet(self):
        credit_wallet(self.subscriber, 1000, 'paystack_topup')
        txn = debit_wallet(self.subscriber, 400, 'plan_purchase')
        wallet = Wallet.objects.get(subscriber=self.subscriber)
        self.assertEqual(wallet.balance_ngn, Decimal('600'))
        self.assertEqual(txn.amount_ngn, Decimal('-400'))
        self.assertEqual(txn.balance_after, Decimal('600'))

    def test_debit_insufficient_balance(self):
        credit_wallet(self.subscriber, 100, 'paystack_topup')
        with self.assertRaises(InsufficientBalance):
            debit_wallet(self.subscriber, 200, 'plan_purchase')

    def test_purchase_plan_from_wallet(self):
        credit_wallet(self.subscriber, 1000, 'paystack_topup')
        sub = purchase_plan_from_wallet(self.subscriber, self.plan)

        self.assertEqual(sub.status, 'active')
        self.assertEqual(sub.plan, self.plan)

        wallet = Wallet.objects.get(subscriber=self.subscriber)
        self.assertEqual(wallet.balance_ngn, Decimal('500'))  # 1000 - 500

        # Payment record should exist
        payment = Payment.objects.get(subscriber=self.subscriber, payment_method='wallet')
        self.assertEqual(payment.amount_ngn, Decimal('500'))
        self.assertEqual(payment.paystack_status, 'success')

        # RADIUS should be written
        rug = Radusergroup.objects.get(username=self.subscriber.phone)
        self.assertEqual(rug.groupname, self.plan.radius_group_name)

    def test_renew_plan_from_wallet(self):
        credit_wallet(self.subscriber, 500, 'paystack_topup')
        sub = renew_plan_from_wallet(self.subscriber, self.plan)
        self.assertIsNotNone(sub)

        wallet = Wallet.objects.get(subscriber=self.subscriber)
        self.assertEqual(wallet.balance_ngn, Decimal('0'))

        payment = Payment.objects.get(payment_type='auto_renewal')
        self.assertEqual(payment.amount_ngn, Decimal('500'))

    def test_renew_insufficient_balance(self):
        credit_wallet(self.subscriber, 100, 'paystack_topup')
        result = renew_plan_from_wallet(self.subscriber, self.plan)
        self.assertIsNone(result)

    def test_transaction_audit_trail(self):
        credit_wallet(self.subscriber, 500, 'paystack_topup', reference='PS-1')
        credit_wallet(self.subscriber, 300, 'refill_card', reference='RF-1')
        debit_wallet(self.subscriber, 200, 'plan_purchase', reference='plan-1')

        wallet = get_or_create_wallet(self.subscriber)
        txns = list(wallet.transactions.order_by('created_at'))
        self.assertEqual(len(txns), 3)
        self.assertEqual(txns[0].balance_after, Decimal('500'))
        self.assertEqual(txns[1].balance_after, Decimal('800'))
        self.assertEqual(txns[2].balance_after, Decimal('600'))


class RefillCardTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()
        self.subscriber = _create_subscriber(self.reseller)

    def test_generate_refill_batch(self):
        batch = RefillCardBatch.objects.create(
            reseller=self.reseller, name='₦500 cards',
            quantity=20, value_ngn=Decimal('500'), pin_length=10, prefix='RF',
        )
        count = generate_refill_batch(batch)
        self.assertEqual(count, 20)
        cards = RefillCard.objects.filter(batch=batch)
        self.assertEqual(cards.count(), 20)
        for c in cards:
            self.assertTrue(c.pin.startswith('RF'))
            self.assertEqual(c.value_ngn, Decimal('500'))
            self.assertEqual(c.status, 'unused')

    def test_redeem_refill_card_api(self):
        batch = RefillCardBatch.objects.create(
            reseller=self.reseller, name='Redeem Test',
            quantity=1, value_ngn=Decimal('1000'), pin_length=10,
        )
        generate_refill_batch(batch)
        card = batch.refill_cards.first()

        client = APIClient()
        resp = client.post('/api/portal/redeem-refill/', {
            'pin': card.pin,
        }, format='json', HTTP_X_AUTH_TOKEN=self.subscriber.auth_token)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['balance'], '1000.00')

        card.refresh_from_db()
        self.assertEqual(card.status, 'used')
        self.assertEqual(card.redeemed_by, self.subscriber)

    def test_redeem_used_card_fails(self):
        batch = RefillCardBatch.objects.create(
            reseller=self.reseller, name='Used Test',
            quantity=1, value_ngn=Decimal('500'), pin_length=10,
        )
        generate_refill_batch(batch)
        card = batch.refill_cards.first()
        card.status = 'used'
        card.save()

        client = APIClient()
        resp = client.post('/api/portal/redeem-refill/', {
            'pin': card.pin,
        }, format='json', HTTP_X_AUTH_TOKEN=self.subscriber.auth_token)
        self.assertEqual(resp.status_code, 400)


class WalletPortalTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()
        self.subscriber = _create_subscriber(self.reseller)
        self.plan = _create_plan(self.reseller)
        credit_wallet(self.subscriber, 2000, 'paystack_topup')
        self.client = APIClient()

    def test_wallet_info(self):
        resp = self.client.get('/api/portal/wallet/',
                               HTTP_X_AUTH_TOKEN=self.subscriber.auth_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['balance'], '2000.00')
        self.assertTrue(len(resp.data['transactions']) >= 1)

    def test_purchase_from_wallet(self):
        resp = self.client.post('/api/portal/wallet/purchase/', {
            'plan_id': self.plan.pk,
        }, format='json', HTTP_X_AUTH_TOKEN=self.subscriber.auth_token)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('activated', resp.data['message'])

        wallet = Wallet.objects.get(subscriber=self.subscriber)
        self.assertEqual(wallet.balance_ngn, Decimal('1500'))

    def test_purchase_insufficient_balance(self):
        expensive = _create_plan(self.reseller, name='Expensive', slug='expensive', price_ngn=5000)
        resp = self.client.post('/api/portal/wallet/purchase/', {
            'plan_id': expensive.pk,
        }, format='json', HTTP_X_AUTH_TOKEN=self.subscriber.auth_token)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Insufficient', resp.data['error'])


class AutoRenewalTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()
        self.subscriber = _create_subscriber(self.reseller)
        self.subscriber.auto_renew_enabled = True
        self.subscriber.save()
        self.plan = _create_plan(self.reseller, allow_auto_renew=True)
        credit_wallet(self.subscriber, 1000, 'paystack_topup')

    def test_expire_plans_auto_renews(self):
        """When a subscription expires and auto-renew is on with sufficient balance, it renews."""
        sub = Subscription.objects.create(
            subscriber=self.subscriber, plan=self.plan,
            reseller=self.reseller,
            start_date=timezone.now() - timedelta(days=31),
            expiry_date=timezone.now() - timedelta(minutes=1),
            status='active',
        )
        assign_subscriber_to_plan(self.subscriber, self.plan)

        from django.core.management import call_command
        call_command('expire_plans')

        sub.refresh_from_db()
        self.assertEqual(sub.status, 'expired')

        # New subscription should be created
        new_sub = Subscription.objects.filter(
            subscriber=self.subscriber, status='active'
        ).first()
        self.assertIsNotNone(new_sub)
        self.assertEqual(new_sub.plan, self.plan)

        # Wallet debited
        wallet = Wallet.objects.get(subscriber=self.subscriber)
        self.assertEqual(wallet.balance_ngn, Decimal('500'))


# ──────────────────────────────────────────────
#  PHASE 3: ENHANCED PLANS (BURST, CAPS, PPPoE)
# ──────────────────────────────────────────────

class BurstPlanTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()

    def test_burst_rate_limit_format(self):
        plan = _create_plan(
            self.reseller, slug='burst-plan',
            download_mbps=10, upload_mbps=5,
            burst_download_mbps=20, burst_upload_mbps=10,
            burst_threshold_download_mbps=8, burst_threshold_upload_mbps=4,
            burst_time_seconds=15, priority=3,
        )
        expected = '5120k/10240k 10240k/20480k 4096k/8192k 15/15 3'
        self.assertEqual(plan.mikrotik_rate_limit, expected)

    def test_no_burst_rate_limit_format(self):
        plan = _create_plan(self.reseller, slug='no-burst')
        self.assertEqual(plan.mikrotik_rate_limit, '5120k/10240k')

    def test_burst_defaults_threshold(self):
        """When threshold is 0, use base speed."""
        plan = _create_plan(
            self.reseller, slug='burst-default',
            download_mbps=10, upload_mbps=5,
            burst_download_mbps=20, burst_upload_mbps=10,
            burst_time_seconds=10,
        )
        # threshold defaults to base: 5120k/10240k
        expected = '5120k/10240k 10240k/20480k 5120k/10240k 10/10 8'
        self.assertEqual(plan.mikrotik_rate_limit, expected)


class SeparateCapsTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()

    def test_download_cap_radius_attribute(self):
        plan = _create_plan(
            self.reseller, slug='dl-cap',
            download_cap_gb=Decimal('5.00'),
        )
        sync_plan_to_radius(plan)

        attr = Radgroupreply.objects.get(
            groupname=plan.radius_group_name,
            attribute='Mikrotik-Recv-Limit',
        )
        self.assertEqual(int(attr.value), 5 * 1024 * 1024 * 1024)

    def test_upload_cap_radius_attribute(self):
        plan = _create_plan(
            self.reseller, slug='ul-cap',
            upload_cap_gb=Decimal('2.00'),
        )
        sync_plan_to_radius(plan)

        attr = Radgroupreply.objects.get(
            groupname=plan.radius_group_name,
            attribute='Mikrotik-Xmit-Limit',
        )
        self.assertEqual(int(attr.value), 2 * 1024 * 1024 * 1024)

    def test_ip_pool_radius_attribute(self):
        plan = _create_plan(
            self.reseller, slug='pool-plan',
            ip_pool_name='premium-pool',
        )
        sync_plan_to_radius(plan)

        attr = Radgroupreply.objects.get(
            groupname=plan.radius_group_name,
            attribute='Mikrotik-Address-Pool',
        )
        self.assertEqual(attr.value, 'premium-pool')

    def test_no_caps_no_attributes(self):
        plan = _create_plan(self.reseller, slug='nocap')
        sync_plan_to_radius(plan)

        self.assertFalse(Radgroupreply.objects.filter(
            groupname=plan.radius_group_name,
            attribute='Mikrotik-Recv-Limit',
        ).exists())
        self.assertFalse(Radgroupreply.objects.filter(
            groupname=plan.radius_group_name,
            attribute='Mikrotik-Xmit-Limit',
        ).exists())


class FallbackPlanTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()
        self.fallback = _create_plan(
            self.reseller, name='Fallback', slug='fallback',
            download_mbps=1, upload_mbps=1, price_ngn=0,
        )
        self.main_plan = _create_plan(
            self.reseller, slug='main-plan',
            fallback_plan=self.fallback,
        )

    def test_fallback_plan_field(self):
        self.assertEqual(self.main_plan.fallback_plan, self.fallback)

    def test_fallback_plan_deletion_sets_null(self):
        self.fallback.delete()
        self.main_plan.refresh_from_db()
        self.assertIsNone(self.main_plan.fallback_plan)


class PPPoERouterTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()

    def test_service_mode_default(self):
        router = Router.objects.create(
            serial_number='PPPoE-TEST', reseller=self.reseller,
        )
        self.assertEqual(router.service_mode, 'hotspot')

    def test_service_mode_choices(self):
        for mode in ('hotspot', 'pppoe', 'both'):
            router = Router.objects.create(
                serial_number=f'PPPoE-{mode}', reseller=self.reseller,
                service_mode=mode,
            )
            self.assertEqual(router.service_mode, mode)

    def test_provision_script_pppoe(self):
        from routers.provision import generate_provision_rsc
        router = Router.objects.create(
            serial_number='PROV-PPPOE', reseller=self.reseller,
            service_mode='both', wg_tunnel_ip='10.99.0.5',
            nas_secret='testsecret', api_username='admin', api_password='admin',
        )
        script = generate_provision_rsc(router, 'fake-wg-key')
        self.assertIn('service=hotspot,ppp', script)
        self.assertIn('pppoe-server server add', script)
        self.assertIn('pppoe-pool', script)

    def test_provision_script_hotspot_only(self):
        from routers.provision import generate_provision_rsc
        router = Router.objects.create(
            serial_number='PROV-HS', reseller=self.reseller,
            service_mode='hotspot', wg_tunnel_ip='10.99.0.6',
            nas_secret='testsecret', api_username='admin', api_password='admin',
        )
        script = generate_provision_rsc(router, 'fake-wg-key')
        self.assertIn('service=hotspot ', script)
        self.assertNotIn('pppoe-server', script)


# ──────────────────────────────────────────────
#  PHASE 4: DAILY QUOTAS
# ──────────────────────────────────────────────

class DailyQuotaModelTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()

    def test_daily_quota_fields(self):
        plan = _create_plan(
            self.reseller, slug='daily-plan',
            daily_download_mb=500, daily_upload_mb=100,
            daily_total_mb=600, daily_time_minutes=120,
        )
        self.assertEqual(plan.daily_download_mb, 500)
        self.assertEqual(plan.daily_upload_mb, 100)
        self.assertEqual(plan.daily_total_mb, 600)
        self.assertEqual(plan.daily_time_minutes, 120)

    def test_daily_usage_snapshot(self):
        sub = _create_subscriber(self.reseller)
        today = timezone.now().date()
        snap = DailyUsageSnapshot.objects.create(
            subscriber=sub, date=today,
            download_bytes=1024 * 1024 * 100,  # 100 MB
            upload_bytes=1024 * 1024 * 20,      # 20 MB
            online_seconds=3600,
            sessions=5,
        )
        self.assertFalse(snap.quota_exceeded)
        self.assertIn('MB', str(snap))


# ──────────────────────────────────────────────
#  PHASE 5: ONLINE USERS DASHBOARD
# ──────────────────────────────────────────────

class OnlineUsersDashboardTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()
        self.client = Client()
        self.client.login(username='testreseller', password='testpass123')

    def test_online_users_page_loads(self):
        resp = self.client.get(reverse('dashboard-online-users'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Online Users')

    def test_disconnect_user_requires_post(self):
        sub = _create_subscriber(self.reseller)
        resp = self.client.get(reverse('dashboard-disconnect-user', args=[sub.pk]))
        # GET redirects to online users
        self.assertEqual(resp.status_code, 302)


# ──────────────────────────────────────────────
#  PHASE 6: REPORTS
# ──────────────────────────────────────────────

class ReportsDashboardTest(TestCase):
    def setUp(self):
        self.reseller = _create_reseller()
        self.plan = _create_plan(self.reseller)
        self.subscriber = _create_subscriber(self.reseller)
        self.client = Client()
        self.client.login(username='testreseller', password='testpass123')

    def test_traffic_report_loads(self):
        resp = self.client.get(reverse('dashboard-traffic-report'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Traffic Report')

    def test_session_report_loads(self):
        resp = self.client.get(reverse('dashboard-session-report'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Session Report')

    def test_financial_report_loads(self):
        # Create a payment so the report has data
        Payment.objects.create(
            subscriber=self.subscriber, plan=self.plan,
            reseller=self.reseller, payment_type='plan',
            amount_ngn=Decimal('500'), paystack_status='success',
            paystack_reference='test-ref-1',
        )
        resp = self.client.get(reverse('dashboard-financial-report'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Financial Report')
        self.assertContains(resp, '500')

    def test_traffic_csv_export(self):
        resp = self.client.get(reverse('dashboard-report-csv', args=['traffic']))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/csv')

    def test_financial_csv_export(self):
        Payment.objects.create(
            subscriber=self.subscriber, plan=self.plan,
            reseller=self.reseller, payment_type='plan',
            amount_ngn=Decimal('500'), paystack_status='success',
            paystack_reference='csv-ref-1',
        )
        resp = self.client.get(reverse('dashboard-report-csv', args=['financial']))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/csv')

    def test_report_date_range(self):
        resp = self.client.get(reverse('dashboard-traffic-report') + '?days=7')
        self.assertEqual(resp.status_code, 200)

    def test_session_report_phone_filter(self):
        resp = self.client.get(
            reverse('dashboard-session-report') + '?phone=08066137843&days=7'
        )
        self.assertEqual(resp.status_code, 200)


# ──────────────────────────────────────────────
#  CROSS-CUTTING: RADIUS ATTRIBUTE SYNC
# ──────────────────────────────────────────────

class FullRadiusSyncTest(TestCase):
    """Test that all new RADIUS attributes are written correctly."""
    def setUp(self):
        self.reseller = _create_reseller()

    def test_full_plan_sync(self):
        plan = _create_plan(
            self.reseller, slug='full-sync',
            data_cap_gb=Decimal('10.00'),
            download_cap_gb=Decimal('8.00'),
            upload_cap_gb=Decimal('2.00'),
            ip_pool_name='vip-pool',
            burst_download_mbps=20, burst_upload_mbps=10,
            burst_time_seconds=15, priority=2,
        )
        sync_plan_to_radius(plan)

        group = plan.radius_group_name
        attrs = {a.attribute: a.value for a in Radgroupreply.objects.filter(groupname=group)}

        # Rate limit with burst
        self.assertIn('Mikrotik-Rate-Limit', attrs)
        self.assertIn('20480k', attrs['Mikrotik-Rate-Limit'])  # burst download

        # Total cap
        self.assertIn('Mikrotik-Total-Limit', attrs)
        self.assertEqual(int(attrs['Mikrotik-Total-Limit']), 10 * 1024**3)

        # Download cap
        self.assertIn('Mikrotik-Recv-Limit', attrs)
        self.assertEqual(int(attrs['Mikrotik-Recv-Limit']), 8 * 1024**3)

        # Upload cap
        self.assertIn('Mikrotik-Xmit-Limit', attrs)
        self.assertEqual(int(attrs['Mikrotik-Xmit-Limit']), 2 * 1024**3)

        # IP pool
        self.assertIn('Mikrotik-Address-Pool', attrs)
        self.assertEqual(attrs['Mikrotik-Address-Pool'], 'vip-pool')

        # Session timeout
        self.assertIn('Session-Timeout', attrs)

        # Interim interval
        self.assertIn('Acct-Interim-Interval', attrs)

        # Check attributes
        checks = {a.attribute: a.value for a in Radgroupcheck.objects.filter(groupname=group)}
        self.assertIn('Simultaneous-Use', checks)

    def test_inactive_plan_clears_attributes(self):
        plan = _create_plan(self.reseller, slug='inactive-sync', is_active=True)
        sync_plan_to_radius(plan)
        self.assertTrue(Radgroupreply.objects.filter(groupname=plan.radius_group_name).exists())

        plan.is_active = False
        plan.save()
        sync_plan_to_radius(plan)
        self.assertFalse(Radgroupreply.objects.filter(groupname=plan.radius_group_name).exists())
