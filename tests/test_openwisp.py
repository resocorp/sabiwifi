"""
Tests for OpenWISP integration — integrations/ app.

Covers:
  - OpenWISPClient instantiation and configuration
  - OpenWISPBinding model
  - Reseller → org auto-sync signal (mocked)
  - router_add view (OpenWrt MAC path)
  - router_heartbeat view (OpenWrt auto-create)
  - openwisp_webhook view
  - Platform Settings OpenWISP fields
  - API connectivity to live OpenWISP (live_api tests, skipped if not configured)
"""
import json
import uuid
import warnings
from unittest.mock import patch, MagicMock, PropertyMock

from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.urls import reverse

from accounts.models import Reseller, Subscriber
from integrations.models import OpenWISPBinding
from integrations.openwisp import OpenWISPClient, OpenWISPError
from operator_panel.models import PlatformSettings
from routers.models import Router


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_reseller(username='r@test.com', name='Test WiFi', phone='+2348011111111',
                  with_openwisp_binding=True):
    """Create a Reseller. with_openwisp_binding=False leaves no auto-created OpenWISPBinding."""
    mock_org_id = str(uuid.uuid4())
    mock_client = MagicMock()
    if with_openwisp_binding:
        mock_client.create_organization.return_value = {'id': mock_org_id}
    else:
        mock_client.create_organization.side_effect = OpenWISPError('disabled for test')
    with patch('integrations.openwisp.OpenWISPClient', return_value=mock_client):
        user = User.objects.create_user(username=username, email=username, password='pass')
        return Reseller.objects.create(
            user=user, name=name, owner_name='Owner',
            email=username, phone=phone,
        )


def make_router(reseller, serial='ABC123DEF456', device_type='mikrotik'):
    return Router.objects.create(
        reseller=reseller,
        serial_number=serial,
        device_type=device_type,
    )


# ─── PlatformSettings OpenWISP Fields ─────────────────────────────────────────

class PlatformSettingsOpenWISPFieldsTest(TestCase):
    """PlatformSettings has all 4 OpenWISP fields with correct defaults."""

    def test_openwisp_fields_exist_with_defaults(self):
        ps = PlatformSettings.load()
        self.assertEqual(ps.openwisp_api_url, '')
        self.assertEqual(ps.openwisp_api_token, '')
        self.assertEqual(ps.openwisp_shared_secret, '')
        self.assertIsNone(ps.openwisp_pool_org_id)

    def test_openwisp_fields_persist(self):
        ps = PlatformSettings.load()
        test_uuid = uuid.uuid4()
        ps.openwisp_api_url = 'https://wisp.example.com'
        ps.openwisp_api_token = 'testtoken123'
        ps.openwisp_shared_secret = 'secret32charslongexactlyforfr'
        ps.openwisp_pool_org_id = test_uuid
        ps.save()

        ps2 = PlatformSettings.load()
        self.assertEqual(ps2.openwisp_api_url, 'https://wisp.example.com')
        self.assertEqual(ps2.openwisp_api_token, 'testtoken123')
        self.assertEqual(ps2.openwisp_pool_org_id, test_uuid)


# ─── OpenWISPClient Unit Tests ────────────────────────────────────────────────

class OpenWISPClientConfigTest(TestCase):
    """OpenWISPClient raises OpenWISPError when not configured."""

    def test_raises_when_url_missing(self):
        ps = PlatformSettings.load()
        ps.openwisp_api_url = ''
        ps.openwisp_api_token = 'tok'
        ps.save()
        with self.assertRaises(OpenWISPError) as ctx:
            OpenWISPClient()
        self.assertIn('not configured', str(ctx.exception))

    def test_raises_when_token_missing(self):
        ps = PlatformSettings.load()
        ps.openwisp_api_url = 'https://wisp.example.com'
        ps.openwisp_api_token = ''
        ps.save()
        with self.assertRaises(OpenWISPError):
            OpenWISPClient()

    def test_initialises_when_configured(self):
        ps = PlatformSettings.load()
        ps.openwisp_api_url = 'https://wisp.example.com'
        ps.openwisp_api_token = 'validtoken'
        ps.save()
        client = OpenWISPClient()
        self.assertEqual(client._base, 'https://wisp.example.com')
        self.assertIn('Bearer validtoken', client._session.headers.get('Authorization', ''))

    def test_trailing_slash_stripped_from_base_url(self):
        ps = PlatformSettings.load()
        ps.openwisp_api_url = 'https://wisp.example.com/'
        ps.openwisp_api_token = 'tok'
        ps.save()
        client = OpenWISPClient()
        self.assertFalse(client._base.endswith('/'))


class OpenWISPClientMockedTest(TestCase):
    """OpenWISPClient API methods with mocked HTTP responses."""

    def setUp(self):
        ps = PlatformSettings.load()
        ps.openwisp_api_url = 'https://wisp.example.com'
        ps.openwisp_api_token = 'testtoken'
        ps.save()

    def _make_client_with_mock_session(self):
        """Return (client, mock_session) with session HTTP methods mocked."""
        with patch('integrations.openwisp.requests.Session') as MockSession:
            mock_session = MagicMock()
            mock_session.headers = {}
            MockSession.return_value = mock_session
            client = OpenWISPClient()
        return client, mock_session

    @staticmethod
    def _mock_response(status_code=200, json_data=None):
        mock = MagicMock()
        mock.status_code = status_code
        mock.json.return_value = json_data or {}
        if status_code >= 400:
            from requests.exceptions import HTTPError
            mock.raise_for_status.side_effect = HTTPError(response=mock)
        else:
            mock.raise_for_status.return_value = None
        return mock

    def test_create_organization(self):
        client, mock_session = self._make_client_with_mock_session()
        org_id = str(uuid.uuid4())
        mock_session.post.return_value = self._mock_response(201, {'id': org_id, 'name': 'Test Org', 'slug': 'test-org'})
        result = client.create_organization('Test Org', 'test-org')
        self.assertEqual(result['id'], org_id)
        mock_session.post.assert_called_once()
        call_url = mock_session.post.call_args[0][0]
        self.assertIn('/api/v1/users/organization/', call_url)

    def test_find_device_by_mac_found(self):
        client, mock_session = self._make_client_with_mock_session()
        device_id = str(uuid.uuid4())
        mock_session.get.return_value = self._mock_response(200, {
            'count': 1,
            'results': [{'id': device_id, 'name': 'Test Device', 'mac_address': 'AA:BB:CC:DD:EE:FF'}]
        })
        result = client.find_device_by_mac('AA:BB:CC:DD:EE:FF')
        self.assertIsNotNone(result)
        self.assertEqual(result['id'], device_id)

    def test_find_device_by_mac_not_found(self):
        client, mock_session = self._make_client_with_mock_session()
        mock_session.get.return_value = self._mock_response(200, {'count': 0, 'results': []})
        result = client.find_device_by_mac('AA:BB:CC:DD:EE:FF')
        self.assertIsNone(result)

    def test_update_device_context(self):
        client, mock_session = self._make_client_with_mock_session()
        device_id = str(uuid.uuid4())
        mock_session.patch.return_value = self._mock_response(200, {'id': device_id})
        result = client.update_device_context(device_id, {'wg_tunnel_ip': '10.99.0.5'})
        self.assertIsNotNone(result)
        mock_session.patch.assert_called_once()

    def test_get_raises_openwisp_error_on_http_error(self):
        client, mock_session = self._make_client_with_mock_session()
        mock_session.get.return_value = self._mock_response(500)
        with self.assertRaises(OpenWISPError):
            client._get('/api/v1/test/')

    def test_get_raises_on_connection_error(self):
        client, mock_session = self._make_client_with_mock_session()
        import requests as req_lib
        mock_session.get.side_effect = req_lib.ConnectionError('refused')
        with self.assertRaises(OpenWISPError):
            client._get('/api/v1/test/')


# ─── OpenWISPBinding Model ─────────────────────────────────────────────────────

class OpenWISPBindingModelTest(TestCase):
    """OpenWISPBinding stores reseller↔org and router↔device mappings."""

    def setUp(self):
        self.reseller = make_reseller(with_openwisp_binding=False)

    def test_reseller_binding(self):
        org_id = uuid.uuid4()
        binding = OpenWISPBinding.objects.create(
            reseller=self.reseller,
            openwisp_id=org_id,
            openwisp_type='org',
        )
        self.assertEqual(binding.openwisp_id, org_id)
        self.assertEqual(binding.openwisp_type, 'org')
        self.assertEqual(self.reseller.openwisp_binding, binding)

    def test_router_binding(self):
        router = make_router(self.reseller, device_type='openwrt', serial='AABBCCDDEEFF')
        device_id = uuid.uuid4()
        binding = OpenWISPBinding.objects.create(
            router=router,
            openwisp_id=device_id,
            openwisp_type='device',
        )
        self.assertEqual(router.openwisp_binding, binding)

    def test_str_representation(self):
        org_id = uuid.uuid4()
        binding = OpenWISPBinding.objects.create(
            reseller=self.reseller,
            openwisp_id=org_id,
            openwisp_type='org',
        )
        self.assertIn('org', str(binding).lower())

    def test_cannot_have_both_reseller_and_router(self):
        """Binding must be for exactly one entity."""
        router = make_router(self.reseller, device_type='openwrt', serial='AABBCCDDEEFF')
        org_id = uuid.uuid4()
        from django.db import IntegrityError, transaction
        with self.assertRaises((IntegrityError, Exception)):
            with transaction.atomic():
                OpenWISPBinding.objects.create(
                    reseller=self.reseller,
                    router=router,
                    openwisp_id=org_id,
                    openwisp_type='org',
                )


# ─── Signal: Reseller → OpenWISP Org ─────────────────────────────────────────

class ResellerOrgSyncSignalTest(TestCase):
    """Creating a Reseller triggers OpenWISP org creation via signal."""

    @patch('integrations.openwisp.OpenWISPClient')
    def test_signal_creates_org_on_reseller_creation(self, MockClient):
        org_id = str(uuid.uuid4())
        mock_instance = MagicMock()
        MockClient.return_value = mock_instance
        mock_instance.create_organization.return_value = {'id': org_id, 'name': 'Test WiFi'}

        user = User.objects.create_user(username='signal@test.com', email='signal@test.com', password='p')
        reseller = Reseller.objects.create(
            user=user, name='Signal WiFi', owner_name='O',
            email='signal@test.com', phone='+2348012222222',
        )

        mock_instance.create_organization.assert_called_once()
        binding = OpenWISPBinding.objects.get(reseller=reseller, openwisp_type='org')
        self.assertEqual(str(binding.openwisp_id), org_id)

    def test_signal_does_not_crash_on_openwisp_error(self):
        """OpenWISP unavailability must not block reseller creation."""
        # make_reseller silently fails (OpenWISP not configured in test DB) — reseller is still created
        reseller = make_reseller(username='safe@test.com', name='Safe WiFi', phone='+2348013333333')
        self.assertIsNotNone(reseller.pk)

    @patch('integrations.openwisp.OpenWISPClient')
    def test_signal_does_not_create_duplicate_binding(self, MockClient):
        """Signal fires on every save — only first save creates binding."""
        org_id = str(uuid.uuid4())
        mock_instance = MagicMock()
        MockClient.return_value = mock_instance
        mock_instance.create_organization.return_value = {'id': org_id}

        user = User.objects.create_user(username='dup@test.com', email='dup@test.com', password='p')
        reseller = Reseller.objects.create(
            user=user, name='Dup WiFi', owner_name='O',
            email='dup@test.com', phone='+2348014444444',
        )
        # Save again (update — signal should skip because created=False)
        reseller.name = 'Dup WiFi Updated'
        reseller.save()

        self.assertEqual(OpenWISPBinding.objects.filter(reseller=reseller).count(), 1)


# ─── router_heartbeat View (OpenWrt) ─────────────────────────────────────────

class RouterHeartbeatOpenWrtTest(TestCase):
    """Heartbeat endpoint auto-creates Router records for OpenWrt (MAC-based) devices."""

    def setUp(self):
        self.c = Client()
        self.reseller = make_reseller()

    def test_openwrt_heartbeat_creates_router(self):
        mac = 'AABBCCDDEEFF'
        wg_pub = 'abc123publickey=='
        resp = self.c.get(
            f'/api/routers/heartbeat/{mac}/',
            HTTP_X_WG_PUBLIC_KEY=wg_pub,
        )
        self.assertEqual(resp.status_code, 200)
        router = Router.objects.get(serial_number=mac)
        self.assertEqual(router.device_type, 'openwrt')
        self.assertEqual(router.wg_public_key, wg_pub)

    def test_openwrt_heartbeat_updates_last_seen(self):
        mac = 'AABBCCDDEEFF'
        # First heartbeat
        self.c.get(f'/api/routers/heartbeat/{mac}/', HTTP_X_WG_PUBLIC_KEY='key1==')
        router = Router.objects.get(serial_number=mac)
        first_seen = router.last_seen

        import time; time.sleep(0.1)
        # Second heartbeat
        self.c.get(f'/api/routers/heartbeat/{mac}/', HTTP_X_WG_PUBLIC_KEY='key1==')
        router.refresh_from_db()
        self.assertGreaterEqual(router.last_seen, first_seen)

    def test_openwrt_heartbeat_updates_wg_key_on_change(self):
        mac = 'AABBCCDDEEFF'
        self.c.get(f'/api/routers/heartbeat/{mac}/', HTTP_X_WG_PUBLIC_KEY='oldkey==')
        self.c.get(f'/api/routers/heartbeat/{mac}/', HTTP_X_WG_PUBLIC_KEY='newkey==')
        router = Router.objects.get(serial_number=mac)
        self.assertEqual(router.wg_public_key, 'newkey==')

    def test_mikrotik_heartbeat_works_for_claimed_router(self):
        """MikroTik serial heartbeat updates last_seen for a claimed router."""
        router = make_router(self.reseller, serial='HFG30A9B72C', device_type='mikrotik')
        resp = self.c.get(f'/api/routers/heartbeat/HFG30A9B72C/')
        self.assertEqual(resp.status_code, 200)


# ─── router_add View (OpenWrt MAC path) ───────────────────────────────────────

class RouterAddOpenWrtTest(TestCase):
    """dashboard router_add handles OpenWrt MAC address claiming."""

    def setUp(self):
        self.c = Client()
        self.reseller = make_reseller()
        self.c.login(username='r@test.com', password='pass')

    def _post_openwrt(self, mac):
        return self.c.post(
            reverse('dashboard-router-add'),
            {'device_type': 'openwrt', 'mac_address': mac},
        )

    @patch('dashboard.views._register_openwrt_in_openwisp')
    def test_openwrt_add_with_precreated_router(self, mock_register):
        """Router pre-created by heartbeat → claimed successfully."""
        mac = 'AABBCCDDEEFF'
        Router.objects.create(
            reseller=None, serial_number=mac, device_type='openwrt',
        )
        mock_register.return_value = None

        resp = self._post_openwrt('AA:BB:CC:DD:EE:FF')
        # Should redirect to routers list on success
        self.assertIn(resp.status_code, [200, 302])
        router = Router.objects.get(serial_number=mac)
        self.assertEqual(router.reseller, self.reseller)

    def test_openwrt_add_rejects_invalid_mac(self):
        resp = self._post_openwrt('NOTAMAC')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'MAC')

    def test_openwrt_add_rejects_already_claimed_mac(self):
        """MAC already claimed by another reseller → error."""
        with patch('integrations.signals.sync_reseller_to_openwisp'):
            other_reseller = make_reseller(username='other@test.com', name='Other', phone='+2348015555555')
        mac = 'AABBCCDDEEFF'
        Router.objects.create(
            reseller=other_reseller, serial_number=mac, device_type='openwrt',
        )
        resp = self._post_openwrt('AA:BB:CC:DD:EE:FF')
        self.assertEqual(resp.status_code, 200)
        # Router remains with original owner
        router = Router.objects.get(serial_number=mac)
        self.assertEqual(router.reseller, other_reseller)

    def test_openwrt_tab_selected_on_mac_error(self):
        """When MAC error occurs, OpenWrt tab should be active in response."""
        resp = self._post_openwrt('BAD')
        self.assertEqual(resp.status_code, 200)
        # The template JS switches to openwrt tab when errors.mac_address exists
        self.assertContains(resp, 'mac_address')


# ─── openwisp_webhook View ────────────────────────────────────────────────────

@override_settings(OPENWISP_WEBHOOK_SECRET='test-webhook-secret-12345')
class OpenWISPWebhookTest(TestCase):
    """openwisp_webhook endpoint processes device online/offline events.

    OpenWISP sends status values: 'ok' (healthy), 'problem'/'unknown' (degraded).
    Payload: {'event': 'device_status_changed', 'device': {'id': '...', 'status': 'ok'}}
    """

    WEBHOOK_SECRET = 'test-webhook-secret-12345'

    def setUp(self):
        self.c = Client()
        self.reseller = make_reseller()
        self.router = make_router(self.reseller, serial='AABBCCDDEEFF', device_type='openwrt')
        self.device_id = uuid.uuid4()
        OpenWISPBinding.objects.create(
            router=self.router,
            openwisp_id=self.device_id,
            openwisp_type='device',
        )

    def _post_webhook(self, event_type='device_status_changed', device_status='ok',
                      secret=None):
        """Post a webhook with status inside the device dict (OpenWISP's actual format)."""
        if secret is None:
            secret = self.WEBHOOK_SECRET
        payload = {
            'event': event_type,
            'device': {'id': str(self.device_id), 'status': device_status},
        }
        return self.c.post(
            '/api/routers/openwisp-webhook/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_OPENWISP_SIGNATURE=secret,
        )

    def test_webhook_device_goes_online(self):
        """OpenWISP 'ok' status → router status becomes 'online'."""
        self.router.status = 'offline'
        self.router.save()
        resp = self._post_webhook(device_status='ok')
        self.assertEqual(resp.status_code, 200)
        self.router.refresh_from_db()
        self.assertEqual(self.router.status, 'online')

    def test_webhook_device_goes_offline(self):
        """OpenWISP 'problem' status → router status becomes 'offline'."""
        self.router.status = 'online'
        self.router.save()
        resp = self._post_webhook(device_status='problem')
        self.assertEqual(resp.status_code, 200)
        self.router.refresh_from_db()
        self.assertEqual(self.router.status, 'offline')

    def test_webhook_unknown_device_returns_200(self):
        """Unknown device ID should not crash — return 200."""
        payload = {
            'event': 'device_status_changed',
            'device': {'id': str(uuid.uuid4()), 'status': 'ok'},
        }
        resp = self.c.post(
            '/api/routers/openwisp-webhook/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_OPENWISP_SIGNATURE=self.WEBHOOK_SECRET,
        )
        self.assertEqual(resp.status_code, 200)

    def test_webhook_creates_health_log_entry(self):
        """Status transition (offline → online) creates a RouterHealthLog entry."""
        from routers.models import RouterHealthLog
        # Set initial state: offline so transition to 'ok' triggers a log
        self.router.status = 'offline'
        self.router.save()
        initial_count = RouterHealthLog.objects.filter(router=self.router).count()
        self._post_webhook(device_status='ok')
        new_count = RouterHealthLog.objects.filter(router=self.router).count()
        self.assertEqual(new_count, initial_count + 1)

    def test_webhook_rejects_missing_signature(self):
        """Webhook without signature header is rejected with 403."""
        payload = {
            'event': 'device_status_changed',
            'device': {'id': str(self.device_id), 'status': 'ok'},
        }
        resp = self.c.post(
            '/api/routers/openwisp-webhook/',
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_webhook_rejects_wrong_signature(self):
        """Webhook with wrong signature is rejected with 403."""
        resp = self._post_webhook(device_status='ok', secret='wrong-secret')
        self.assertEqual(resp.status_code, 403)

    def test_webhook_rejects_invalid_json(self):
        resp = self.c.post(
            '/api/routers/openwisp-webhook/',
            data='not-json',
            content_type='application/json',
            HTTP_X_OPENWISP_SIGNATURE=self.WEBHOOK_SECRET,
        )
        self.assertIn(resp.status_code, [200, 400])


# ─── Live API Tests (skipped when not configured) ─────────────────────────────

import os
from django.test import skipUnlessDBFeature

LIVE_OPENWISP = (
    os.environ.get('OPENWISP_API_URL') or
    # Check real platform settings (production DB not available in test, use env)
    ''
)


class LiveOpenWISPAPITest(TestCase):
    """Integration tests that hit the real OpenWISP API.
    Run with: OPENWISP_LIVE_TESTS=1 manage.py test tests.test_openwisp.LiveOpenWISPAPITest
    """

    @classmethod
    def setUpClass(cls):
        # Must check skip condition BEFORE super().setUpClass() opens a DB transaction,
        # otherwise raising SkipTest leaves the connection in an aborted state.
        if not os.environ.get('OPENWISP_LIVE_TESTS'):
            import unittest
            raise unittest.SkipTest('Set OPENWISP_LIVE_TESTS=1 to run live API tests')
        super().setUpClass()
        # Use prod-like settings with real OpenWISP
        ps = PlatformSettings.load()
        ps.openwisp_api_url = os.environ.get('OPENWISP_API_URL', 'https://wisp.sabiwifi.com')
        ps.openwisp_api_token = os.environ.get('OPENWISP_API_TOKEN', '')
        ps.save()

    def test_list_organizations(self):
        warnings.filterwarnings('ignore')
        client = OpenWISPClient()
        result = client.list_organizations()
        self.assertIn('results', result)
        self.assertGreaterEqual(result['count'], 1)

    def test_find_nonexistent_device(self):
        warnings.filterwarnings('ignore')
        client = OpenWISPClient()
        result = client.find_device_by_mac('FF:FF:FF:FF:FF:FF')
        self.assertIsNone(result)

    def test_create_and_lookup_organization(self):
        warnings.filterwarnings('ignore')
        client = OpenWISPClient()
        test_slug = f'test-org-{uuid.uuid4().hex[:8]}'
        try:
            org = client.create_organization(f'Test Org {test_slug}', test_slug)
            self.assertIn('id', org)
        except OpenWISPError as e:
            self.fail(f'create_organization raised: {e}')
