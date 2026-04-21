"""
Tests for the universal MikroTik provisioning template.

Covers:
  - Dynamic WAN discovery (no hardcoded ether1)
  - Conditional WiFi block (only renders if device has WiFi at runtime)
  - PPPoE block conditional on Router.service_mode
  - RouterOS v7-only guard
  - Watchdog reboot (replaces unsupported scripted safe-mode)
  - Auto-reprovision triggers from API endpoints
  - Capability announce endpoint persists fields
"""
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse

from accounts.models import Reseller
from routers.models import Router
from routers.provision import generate_provision_rsc


def _reseller(email='prov@x.com'):
    user = User.objects.create_user(username=email, email=email, password='pass1234')
    return Reseller.objects.create(
        user=user, name='Provisioning Test', owner_name='Owner',
        email=email, phone='+2348011112222', status='active',
        branding={'template': 'modern', 'primary_color': '#0052CC'},
    )


def _router(reseller, **overrides):
    defaults = dict(
        serial_number='TESTSN001',
        reseller=reseller,
        status='offline',
        wg_tunnel_ip='10.99.0.42',
        nas_secret='testsecret123',
        api_username='sabiwifi',
        api_password='apipass',
    )
    defaults.update(overrides)
    return Router.objects.create(**defaults)


class ProvisionTemplateTests(TestCase):
    def setUp(self):
        self.reseller = _reseller()

    def test_no_hardcoded_ether1_for_wan(self):
        """WAN must be discovered via DHCP loop, not hardcoded to ether1."""
        router = _router(self.reseller)
        rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
        self.assertIn('/ip dhcp-client find status=bound', rsc)
        self.assertIn(':local wanIface', rsc)
        # The script should never assume ether1 = WAN at the top level.
        # (It may fall back to ether1 only when no DHCP lease is found.)
        self.assertIn('defaulting WAN to ether1', rsc)

    def test_bridges_all_non_wan_ether_ports(self):
        router = _router(self.reseller)
        rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
        self.assertIn(':foreach', rsc)
        self.assertIn('/interface ethernet find', rsc)
        self.assertIn('hotspot-br', rsc)

    def test_conditional_wifi_block(self):
        """WiFi config should be guarded by runtime presence check."""
        router = _router(self.reseller)
        rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
        self.assertIn('/interface wifi find', rsc)
        # Conditional length-check ensures wifi-less routers don't error.
        self.assertIn(':if ([:len [/interface wifi find]]', rsc)

    def test_routeros_v7_guard(self):
        router = _router(self.reseller)
        rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
        self.assertIn('RouterOS v7 required', rsc)
        self.assertIn(':if ($osMajor < 7)', rsc)

    def test_pppoe_block_omitted_for_hotspot_only(self):
        router = _router(self.reseller, service_mode='hotspot')
        rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
        self.assertNotIn('pppoe-server', rsc)

    def test_pppoe_block_for_pppoe_mode(self):
        router = _router(self.reseller, service_mode='pppoe')
        rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
        self.assertIn('pppoe-server', rsc)
        # MS-CHAPv2 must be enabled so Windows' built-in dialer authenticates
        # against the cleartext PIN stored in radcheck.
        self.assertIn('mschap2', rsc)

    def test_pppoe_block_for_both_mode(self):
        router = _router(self.reseller, service_mode='both')
        rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
        self.assertIn('pppoe-server', rsc)
        self.assertIn('hotspot', rsc)

    def test_radius_services_match_service_mode(self):
        for mode, expected in [('hotspot', 'hotspot'), ('pppoe', 'ppp'), ('both', 'hotspot,ppp')]:
            router = Router.objects.create(
                serial_number=f'RAD-{mode}', reseller=self.reseller,
                wg_tunnel_ip=f'10.99.1.{len(mode)}',
                nas_secret='s', api_username='a', api_password='p',
                service_mode=mode,
            )
            rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
            self.assertIn(f'service={expected}', rsc)

    def test_wifi_open_when_no_password(self):
        router = _router(self.reseller, wifi_password='')
        rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
        # No security profile added, no security attr applied to configurations
        self.assertNotIn('/interface/wifi/security add', rsc)
        self.assertNotIn('security=sabiwifi-sec', rsc)

    def test_wifi_security_block_when_password_set(self):
        router = _router(self.reseller, wifi_password='secret-pass-123')
        rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
        self.assertIn('sabiwifi-sec', rsc)
        self.assertIn('wpa2-psk', rsc)
        self.assertIn('secret-pass-123', rsc)

    def test_watchdog_reboot_replaces_safe_mode(self):
        """Scripted safe-mode is unsupported on RouterOS — we use a watchdog instead."""
        router = _router(self.reseller)
        rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
        self.assertIn('sabiwifi-watchdog', rsc)
        self.assertIn('/system reboot', rsc)

    def test_capability_announce_call(self):
        router = _router(self.reseller)
        rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
        self.assertIn('/api/routers/announce/', rsc)

    def test_reprovision_sentinel_present(self):
        router = _router(self.reseller)
        rsc = generate_provision_rsc(router, wg_private_key='x' * 44)
        self.assertIn('SABIWIFI-REPROVISION-v1', rsc)


class ReprovisionTriggerTests(TestCase):
    def setUp(self):
        self.reseller = _reseller(email='trig@x.com')
        self.router = _router(self.reseller)
        self.client = Client()
        self.client.force_login(self.reseller.user)

    def test_service_mode_change_triggers_reprovision(self):
        self.router.needs_reprovision = False
        self.router.save(update_fields=['needs_reprovision'])
        resp = self.client.post(
            reverse('api-router-service-mode', args=[self.router.pk]),
            data='{"service_mode":"both"}', content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.router.refresh_from_db()
        self.assertEqual(self.router.service_mode, 'both')
        self.assertTrue(self.router.needs_reprovision)

    def test_wifi_config_save_triggers_reprovision(self):
        self.router.needs_reprovision = False
        self.router.save(update_fields=['needs_reprovision'])
        resp = self.client.post(
            reverse('api-router-wifi-config', args=[self.router.pk]),
            data='{"ssid":"Cafe-WiFi","password":"longpass1","enabled":true}',
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.router.refresh_from_db()
        self.assertEqual(self.router.wifi_ssid, 'Cafe-WiFi')
        self.assertEqual(self.router.wifi_password, 'longpass1')
        self.assertTrue(self.router.wifi_enabled)
        self.assertTrue(self.router.needs_reprovision)

    def test_wifi_config_rejects_short_password(self):
        resp = self.client.post(
            reverse('api-router-wifi-config', args=[self.router.pk]),
            data='{"ssid":"X","password":"short","enabled":true}',
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_wifi_config_blocked_when_no_wifi_hardware(self):
        self.router.has_wifi = False
        self.router.save(update_fields=['has_wifi'])
        resp = self.client.post(
            reverse('api-router-wifi-config', args=[self.router.pk]),
            data='{"ssid":"X","password":"longpass1","enabled":true}',
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_redeploy_endpoint_flips_flag(self):
        self.router.needs_reprovision = False
        self.router.save(update_fields=['needs_reprovision'])
        resp = self.client.post(reverse('api-router-redeploy', args=[self.router.pk]))
        self.assertEqual(resp.status_code, 200)
        self.router.refresh_from_db()
        self.assertTrue(self.router.needs_reprovision)


class CapabilityAnnounceTests(TestCase):
    def setUp(self):
        self.reseller = _reseller(email='ann@x.com')
        self.router = _router(self.reseller, serial_number='ANNOUNCE1')

    def test_announce_persists_capabilities(self):
        client = Client()
        resp = client.get(
            reverse('api-router-announce', args=['ANNOUNCE1']),
            {'board': 'hAP ac2', 'ros': '7.15.3', 'wifi': 'yes',
             'fg': 'yes', 'ether': '5', 'wan': 'ether3'},
        )
        self.assertEqual(resp.status_code, 200)
        self.router.refresh_from_db()
        self.assertEqual(self.router.board_name, 'hAP ac2')
        self.assertEqual(self.router.ros_version, '7.15.3')
        self.assertTrue(self.router.has_wifi)
        self.assertTrue(self.router.has_5ghz)
        self.assertEqual(self.router.ether_port_count, 5)
        self.assertEqual(self.router.detected_wan, 'ether3')
        self.assertIsNotNone(self.router.last_reprovision_at)

    def test_announce_marks_no_wifi(self):
        client = Client()
        client.get(
            reverse('api-router-announce', args=['ANNOUNCE1']),
            {'board': 'hEX', 'ros': '7.15', 'wifi': 'no', 'fg': 'no',
             'ether': '5', 'wan': 'ether1'},
        )
        self.router.refresh_from_db()
        self.assertFalse(self.router.has_wifi)
        self.assertFalse(self.router.has_5ghz)

    def test_announce_unknown_serial_returns_ok(self):
        client = Client()
        resp = client.get(reverse('api-router-announce', args=['NOPE']))
        self.assertEqual(resp.status_code, 200)
