"""
OpenWISP REST API client for SabiWiFi integration.

Handles:
  - Organization management (one org per reseller)
  - Device lookup, claiming, and context variable updates
  - Monitoring metrics (CPU, memory, traffic, WiFi clients)
  - Firmware upgrade management

Errors are raised as OpenWISPError. Callers should catch and log — never
let OpenWISP unavailability crash a user-facing request.
"""
import logging
import requests
from operator_panel.models import PlatformSettings

logger = logging.getLogger(__name__)


class OpenWISPError(Exception):
    pass


class OpenWISPClient:
    """Thin wrapper around the OpenWISP v1 REST API."""

    def __init__(self):
        ps = PlatformSettings.load()
        base = (ps.openwisp_api_url or '').rstrip('/')
        token = ps.openwisp_api_token or ''
        if not base or not token:
            raise OpenWISPError(
                'OpenWISP not configured. Set openwisp_api_url and '
                'openwisp_api_token in Platform Settings.'
            )
        self._base = base
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json',
        })

    # ── Internal helpers ──────────────────────────────────────────────────

    def _get(self, path, **kwargs):
        try:
            r = self._session.get(f'{self._base}{path}', timeout=10, **kwargs)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            raise OpenWISPError(f'GET {path} failed: {exc}') from exc

    def _post(self, path, json=None, **kwargs):
        try:
            r = self._session.post(f'{self._base}{path}', json=json, timeout=10, **kwargs)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            raise OpenWISPError(f'POST {path} failed: {exc}') from exc

    def _patch(self, path, json=None, **kwargs):
        try:
            r = self._session.patch(f'{self._base}{path}', json=json, timeout=10, **kwargs)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            raise OpenWISPError(f'PATCH {path} failed: {exc}') from exc

    # ── Organizations ─────────────────────────────────────────────────────

    def create_organization(self, name, slug):
        """Create a new OpenWISP organization. Returns org dict with 'id'."""
        return self._post('/api/v1/users/organization/', json={
            'name': name,
            'slug': slug,
            'is_active': True,
        })

    def list_organizations(self):
        return self._get('/api/v1/users/organization/')

    # ── Devices ───────────────────────────────────────────────────────────

    def find_device_by_mac(self, mac):
        """
        Search OpenWISP for a device by MAC address.
        mac: 12-char hex string (no separators), e.g. 'A4B1C2D3E4F5'
        Returns device dict or None.
        """
        mac_clean = mac.upper().replace(':', '').replace('-', '')
        mac_colons = ':'.join(mac_clean[i:i+2] for i in range(0, 12, 2))

        try:
            # Try exact MAC address field first
            data = self._get('/api/v1/controller/device/', params={'mac_address': mac_colons})
            results = data.get('results', [])
            if results:
                return results[0]

            # Fallback: text search (device name often contains MAC)
            data = self._get('/api/v1/controller/device/', params={'search': mac_clean})
            for d in data.get('results', []):
                d_mac = d.get('mac_address', '').upper().replace(':', '')
                if d_mac == mac_clean:
                    return d
        except OpenWISPError:
            pass
        return None

    def get_device(self, device_id):
        return self._get(f'/api/v1/controller/device/{device_id}/')

    def update_device(self, device_id, data):
        """PATCH device fields (organization, name, context, etc.)."""
        return self._patch(f'/api/v1/controller/device/{device_id}/', json=data)

    def move_device_to_org(self, device_id, org_id):
        """Assign device to a reseller's OpenWISP organization."""
        return self._patch(f'/api/v1/controller/device/{device_id}/', json={
            'organization': str(org_id),
        })

    def update_device_context(self, device_id, context):
        """
        Set per-device context variables injected into NetJSON config templates.

        Typical context for SabiWiFi devices:
          wg_tunnel_ip  — allocated WireGuard tunnel IP (e.g. '10.99.0.5')
          nas_secret    — RADIUS NAS shared secret
          ssid          — reseller's guest WiFi SSID
          ssid_5g       — 5 GHz SSID variant
        """
        return self._patch(f'/api/v1/controller/device/{device_id}/', json={
            'context': context,
        })

    # ── Monitoring ────────────────────────────────────────────────────────

    def get_device_monitoring(self, device_id):
        """
        Get monitoring status and latest metric values for a device.
        Returns OpenWISP monitoring dict; key fields:
          status            — 'ok' | 'problem' | 'unknown'
          monitoring_status — same
          related.metrics   — list of {field, value, ...}
        """
        return self._get(f'/api/v1/monitoring/device/{device_id}/')

    def parse_metrics(self, monitoring_data):
        """
        Convert OpenWISP monitoring response to the dict format the SabiWiFi
        dashboard JS expects:
          cpu_load, memory_pct, uptime, connected_devices, wan_rx, wan_tx, wifi
        """
        metrics = {}
        for m in monitoring_data.get('related', {}).get('metrics', []):
            key = m.get('field_name') or m.get('name', '')
            metrics[key] = m.get('value')

        def fmt_bytes(val):
            if val is None:
                return '0 B'
            val = float(val)
            for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
                if val < 1024:
                    return f'{val:.1f} {unit}'
                val /= 1024
            return f'{val:.1f} PB'

        uptime_seconds = metrics.get('uptime') or 0
        uptime_str = ''
        if uptime_seconds:
            days = int(uptime_seconds) // 86400
            hours = (int(uptime_seconds) % 86400) // 3600
            uptime_str = f'{days}d {hours}h' if days else f'{hours}h'

        return {
            'cpu_load': round(float(metrics.get('cpu', 0) or 0)),
            'memory_pct': round(float(metrics.get('memory_usage', 0) or 0)),
            'uptime': uptime_str or '--',
            'connected_devices': int(metrics.get('clients', 0) or 0),
            'wan_rx': fmt_bytes(metrics.get('rx_bytes')),
            'wan_tx': fmt_bytes(metrics.get('tx_bytes')),
            'wifi': [],  # populated separately if needed
        }

    # ── Firmware ──────────────────────────────────────────────────────────

    def list_firmware_builds(self, org_id=None):
        params = {}
        if org_id:
            params['build__category__organization'] = str(org_id)
        return self._get('/api/v1/firmware-upgrader/build/', params=params)

    def get_device_firmware(self, device_id):
        return self._get('/api/v1/firmware-upgrader/devicefirmware/',
                         params={'device': str(device_id)})

    def set_device_firmware(self, device_id, build_id):
        """Trigger OTA firmware upgrade for a device."""
        return self._post('/api/v1/firmware-upgrader/devicefirmware/', json={
            'device': str(device_id),
            'build': str(build_id),
        })
