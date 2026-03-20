"""
RouterOS REST API utility functions for managing MikroTik routers
over the WireGuard tunnel.

Uses the RouterOS v7 REST API (HTTP on port 80) instead of the legacy
binary API (port 8728).  All connections go through the WG tunnel IP.

RouterOS REST API reference:
  GET  /rest/<path>         - list / get resource
  PUT  /rest/<path>/<id>    - full update
  PATCH /rest/<path>/<id>   - partial update
  POST /rest/<path>         - create
  DELETE /rest/<path>/<id>  - delete
"""
import logging

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

# Timeout for REST API calls (seconds)
API_TIMEOUT = 10


class RouterOSError(Exception):
    """Raised when a RouterOS API operation fails."""
    pass


# ---------------------------------------------------------------------------
# Low-level REST helpers
# ---------------------------------------------------------------------------

def _auth(router):
    return HTTPBasicAuth(router.api_username, router.api_password)


def _url(router, path):
    return f'http://{router.wg_tunnel_ip}/rest{path}'


def _rest_get(router, path):
    """GET request to RouterOS REST API. Returns parsed JSON."""
    try:
        resp = requests.get(_url(router, path), auth=_auth(router), timeout=API_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        raise RouterOSError(f'Timeout connecting to router {router.serial_number}')
    except requests.exceptions.ConnectionError:
        raise RouterOSError(f'Cannot reach router {router.serial_number} at {router.wg_tunnel_ip}')
    except requests.exceptions.HTTPError as e:
        raise RouterOSError(f'REST API error on {router.serial_number}: {e}')


def _rest_patch(router, path, data):
    """PATCH request (partial update) to RouterOS REST API."""
    try:
        resp = requests.patch(
            _url(router, path), json=data,
            auth=_auth(router), timeout=API_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except requests.exceptions.Timeout:
        raise RouterOSError(f'Timeout connecting to router {router.serial_number}')
    except requests.exceptions.ConnectionError:
        raise RouterOSError(f'Cannot reach router {router.serial_number}')
    except requests.exceptions.HTTPError as e:
        raise RouterOSError(f'REST API error on {router.serial_number}: {e}')


def _rest_post(router, path, data):
    """POST request (create) to RouterOS REST API.
    Falls back to PUT if POST returns 400 (some RouterOS resources only accept PUT).
    """
    try:
        resp = requests.post(
            _url(router, path), json=data,
            auth=_auth(router), timeout=API_TIMEOUT,
        )
        if resp.status_code == 400:
            # RouterOS uses PUT for some resource creation
            resp = requests.put(
                _url(router, path), json=data,
                auth=_auth(router), timeout=API_TIMEOUT,
            )
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except requests.exceptions.Timeout:
        raise RouterOSError(f'Timeout connecting to router {router.serial_number}')
    except requests.exceptions.ConnectionError:
        raise RouterOSError(f'Cannot reach router {router.serial_number}')
    except requests.exceptions.HTTPError as e:
        raise RouterOSError(f'REST API error on {router.serial_number}: {e}')


def _rest_put(router, path, data):
    """PUT request (full update) to RouterOS REST API."""
    try:
        resp = requests.put(
            _url(router, path), json=data,
            auth=_auth(router), timeout=API_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except requests.exceptions.Timeout:
        raise RouterOSError(f'Timeout connecting to router {router.serial_number}')
    except requests.exceptions.ConnectionError:
        raise RouterOSError(f'Cannot reach router {router.serial_number}')
    except requests.exceptions.HTTPError as e:
        raise RouterOSError(f'REST API error on {router.serial_number}: {e}')


def _rest_delete(router, path):
    """DELETE request to RouterOS REST API."""
    try:
        resp = requests.delete(
            _url(router, path), auth=_auth(router), timeout=API_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except requests.exceptions.Timeout:
        raise RouterOSError(f'Timeout connecting to router {router.serial_number}')
    except requests.exceptions.ConnectionError:
        raise RouterOSError(f'Cannot reach router {router.serial_number}')
    except requests.exceptions.HTTPError as e:
        raise RouterOSError(f'REST API error on {router.serial_number}: {e}')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_bytes(nbytes):
    """Human-readable byte count."""
    nbytes = int(nbytes)
    if nbytes < 1024:
        return f'{nbytes} B'
    elif nbytes < 1024 ** 2:
        return f'{nbytes / 1024:.1f} KB'
    elif nbytes < 1024 ** 3:
        return f'{nbytes / 1024 ** 2:.1f} MB'
    else:
        return f'{nbytes / 1024 ** 3:.2f} GB'


# ---------------------------------------------------------------------------
# High-level functions
# ---------------------------------------------------------------------------

def check_api_reachable(router):
    """Quick connectivity test via REST API."""
    try:
        result = _rest_get(router, '/system/identity')
        return bool(result)
    except RouterOSError:
        return False


def get_router_info(router):
    """
    Basic system info.
    Returns dict: identity, uptime, cpu_load, free_memory, total_memory,
    board_name, version.
    """
    identity = _rest_get(router, '/system/identity')
    res = _rest_get(router, '/system/resource')

    return {
        'identity': identity.get('name', ''),
        'uptime': res.get('uptime', ''),
        'cpu_load': res.get('cpu-load', ''),
        'free_memory': res.get('free-memory', ''),
        'total_memory': res.get('total-memory', ''),
        'board_name': res.get('board-name', ''),
        'version': res.get('version', ''),
    }


def get_router_stats(router):
    """
    Comprehensive router stats for the dashboard.
    Returns a dict suitable for JSON serialization.
    """
    stats = {}

    # --- System resources ---
    try:
        res = _rest_get(router, '/system/resource')
        stats['cpu_load'] = int(res.get('cpu-load', 0))
        stats['uptime'] = res.get('uptime', '')
        stats['version'] = res.get('version', '')
        stats['board'] = res.get('board-name', '')
        total_mem = int(res.get('total-memory', 1))
        free_mem = int(res.get('free-memory', 0))
        stats['memory_pct'] = round((total_mem - free_mem) / total_mem * 100)
        stats['memory_total_mb'] = round(total_mem / 1024 / 1024)
    except (RouterOSError, ValueError, ZeroDivisionError):
        stats.setdefault('cpu_load', 0)
        stats.setdefault('uptime', 'unknown')

    # --- WAN (ether1) traffic ---
    try:
        ifaces = _rest_get(router, '/interface?name=ether1')
        if isinstance(ifaces, list) and ifaces:
            eth = ifaces[0]
            stats['wan_rx'] = _format_bytes(eth.get('rx-byte', 0))
            stats['wan_tx'] = _format_bytes(eth.get('tx-byte', 0))
            stats['wan_rx_bytes'] = int(eth.get('rx-byte', 0))
            stats['wan_tx_bytes'] = int(eth.get('tx-byte', 0))
        else:
            stats['wan_rx'] = '0 B'
            stats['wan_tx'] = '0 B'
    except RouterOSError:
        stats['wan_rx'] = 'N/A'
        stats['wan_tx'] = 'N/A'

    # --- Connected devices (hotspot active sessions) ---
    try:
        active = _rest_get(router, '/ip/hotspot/active')
        stats['hotspot_users'] = len(active) if isinstance(active, list) else 0
    except RouterOSError:
        stats['hotspot_users'] = 0

    # --- WiFi registration table (connected WiFi clients, Layer 2) ---
    try:
        regs = _rest_get(router, '/interface/wifi/registration-table')
        stats['connected_devices'] = len(regs) if isinstance(regs, list) else 0
    except RouterOSError:
        stats['connected_devices'] = 0

    # --- WiFi interfaces ---
    wifi_list = []
    try:
        wifi_ifaces = _rest_get(router, '/interface/wifi')
        if isinstance(wifi_ifaces, list):
            for wifi in wifi_ifaces:
                wifi_info = {
                    'name': wifi.get('name', ''),
                    'default_name': wifi.get('default-name', wifi.get('name', '')),
                    'enabled': wifi.get('disabled') != 'true',
                    'running': wifi.get('running') == 'true',
                    'mac': wifi.get('mac-address', ''),
                    'ssid': '',
                    'band': '',
                    'has_password': False,
                }
                # Get SSID - try inline properties first (v7.13+ REST returns these)
                inline_ssid = wifi.get('configuration.ssid', '')
                inline_band = wifi.get('channel.band', '')
                inline_sec = wifi.get('configuration.security', '')

                if inline_ssid:
                    wifi_info['ssid'] = inline_ssid
                    if '5g' in inline_band.lower():
                        wifi_info['band'] = '5 GHz'
                    elif '2g' in inline_band.lower():
                        wifi_info['band'] = '2.4 GHz'
                    if inline_sec:
                        wifi_info['has_password'] = True
                else:
                    # Fallback: read from linked configuration profile
                    cfg_ref = wifi.get('configuration', '')
                    if cfg_ref:
                        try:
                            cfgs = _rest_get(router, f'/interface/wifi/configuration?name={cfg_ref}')
                            if isinstance(cfgs, list) and cfgs:
                                cfg = cfgs[0]
                                wifi_info['ssid'] = cfg.get('ssid', '')
                                ch_ref = cfg.get('channel', '')
                                if '5g' in ch_ref.lower():
                                    wifi_info['band'] = '5 GHz'
                                else:
                                    wifi_info['band'] = '2.4 GHz'
                                sec_ref = cfg.get('security', '')
                                if sec_ref:
                                    wifi_info['has_password'] = True
                        except RouterOSError:
                            pass

                # Count clients on this specific interface
                try:
                    regs = _rest_get(
                        router,
                        f'/interface/wifi/registration-table?interface={wifi.get("name", "")}'
                    )
                    wifi_info['clients'] = len(regs) if isinstance(regs, list) else 0
                except RouterOSError:
                    wifi_info['clients'] = 0

                wifi_list.append(wifi_info)
    except RouterOSError:
        pass

    stats['wifi'] = wifi_list
    return stats


def set_wifi_ssid(router, ssid, interface_name='wifi1'):
    """
    Update the WiFi SSID on a specific interface.
    Finds the configuration profile linked to the interface and updates its SSID.
    """
    if not ssid or len(ssid) > 32:
        raise RouterOSError('SSID must be 1-32 characters')

    # Get the wifi interface
    wifi_ifaces = _rest_get(router, f'/interface/wifi?name={interface_name}')
    if not isinstance(wifi_ifaces, list) or not wifi_ifaces:
        raise RouterOSError(f'WiFi interface {interface_name} not found')

    wifi = wifi_ifaces[0]
    cfg_ref = wifi.get('configuration', '')
    if not cfg_ref:
        raise RouterOSError(f'No configuration profile on {interface_name}')

    # Find the configuration profile
    cfgs = _rest_get(router, f'/interface/wifi/configuration?name={cfg_ref}')
    if not isinstance(cfgs, list) or not cfgs:
        raise RouterOSError(f'Configuration profile {cfg_ref} not found')

    cfg_id = cfgs[0].get('.id')
    _rest_patch(router, f'/interface/wifi/configuration/{cfg_id}', {'ssid': ssid})

    logger.info(f'SSID updated to "{ssid}" on {interface_name} for router {router.serial_number}')
    return True


def set_wifi_password(router, password, interface_name='wifi1'):
    """
    Set or remove WPA password on a WiFi interface.
    - If password is non-empty: creates/updates security profile with WPA2+WPA3-PSK.
    - If password is empty: removes security from the configuration (open network).
    """
    # Get the wifi interface and its configuration
    wifi_ifaces = _rest_get(router, f'/interface/wifi?name={interface_name}')
    if not isinstance(wifi_ifaces, list) or not wifi_ifaces:
        raise RouterOSError(f'WiFi interface {interface_name} not found')

    wifi = wifi_ifaces[0]
    cfg_ref = wifi.get('configuration', '')
    if not cfg_ref:
        raise RouterOSError(f'No configuration profile on {interface_name}')

    cfgs = _rest_get(router, f'/interface/wifi/configuration?name={cfg_ref}')
    if not isinstance(cfgs, list) or not cfgs:
        raise RouterOSError(f'Configuration profile {cfg_ref} not found')

    cfg_id = cfgs[0].get('.id')
    sec_name = f'sabiwifi-sec-{interface_name}'

    if password:
        if len(password) < 8:
            raise RouterOSError('WiFi password must be at least 8 characters')
        if len(password) > 63:
            raise RouterOSError('WiFi password must be at most 63 characters')

        # Create or update security profile
        existing_sec = _rest_get(router, f'/interface/wifi/security?name={sec_name}')
        if isinstance(existing_sec, list) and existing_sec:
            sec_id = existing_sec[0].get('.id')
            _rest_patch(router, f'/interface/wifi/security/{sec_id}', {
                'authentication-types': 'wpa2-psk,wpa3-psk',
                'passphrase': password,
            })
        else:
            _rest_post(router, '/interface/wifi/security', {
                'name': sec_name,
                'authentication-types': 'wpa2-psk,wpa3-psk',
                'passphrase': password,
            })

        # Link security to configuration
        _rest_patch(router, f'/interface/wifi/configuration/{cfg_id}', {
            'security': sec_name,
        })
        logger.info(f'WiFi password set on {interface_name} for router {router.serial_number}')
    else:
        # Remove security (make open network)
        _rest_patch(router, f'/interface/wifi/configuration/{cfg_id}', {
            'security': '',
        })
        # Clean up security profile
        try:
            existing_sec = _rest_get(router, f'/interface/wifi/security?name={sec_name}')
            if isinstance(existing_sec, list) and existing_sec:
                sec_id = existing_sec[0].get('.id')
                _rest_delete(router, f'/interface/wifi/security/{sec_id}')
        except RouterOSError:
            pass  # OK if it doesn't exist
        logger.info(f'WiFi password removed on {interface_name} for router {router.serial_number}')

    return True


# Legacy aliases for backward compatibility
def set_ssid(router, ssid):
    """Legacy wrapper - updates SSID on wifi1."""
    return set_wifi_ssid(router, ssid, 'wifi1')


def get_connection(router):
    """Legacy compatibility - no longer needed with REST API."""
    raise RouterOSError(
        'Binary API connections are deprecated. Use REST API functions instead.'
    )
