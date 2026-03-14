"""
RouterOS API utility functions for managing MikroTik routers
over the WireGuard tunnel.

Uses the routeros_api library (already in requirements.txt).
All connections go through the WG tunnel IP on port 8728 (default API port).
"""
import logging

import routeros_api

logger = logging.getLogger(__name__)


class RouterOSError(Exception):
    """Raised when a RouterOS API operation fails."""
    pass


def get_connection(router):
    """
    Open a RouterOS API connection to a router via its WireGuard tunnel IP.

    Args:
        router: A Router model instance with wg_tunnel_ip, api_username, api_password.

    Returns:
        A routeros_api.RouterOsApiPool connection pool.

    Raises:
        RouterOSError: If the connection fails.
    """
    if not router.wg_tunnel_ip:
        raise RouterOSError(f"Router {router.serial_number} has no WireGuard tunnel IP")

    try:
        pool = routeros_api.RouterOsApiPool(
            host=str(router.wg_tunnel_ip),
            username=router.api_username,
            password=router.api_password,
            port=8728,
            plaintext_login=True,
        )
        return pool
    except Exception as e:
        raise RouterOSError(
            f"Failed to connect to router {router.serial_number} "
            f"at {router.wg_tunnel_ip}: {e}"
        ) from e


def set_ssid(router, ssid):
    """
    Set the WiFi SSID on wlan1 via RouterOS API.

    Args:
        router: A Router model instance.
        ssid: The new SSID string.

    Returns:
        True on success.

    Raises:
        RouterOSError: If the operation fails.
    """
    pool = get_connection(router)
    try:
        api = pool.get_api()
        wireless = api.get_resource('/interface/wireless')

        # Find wlan1 interface
        interfaces = wireless.get(name='wlan1')
        if not interfaces:
            raise RouterOSError(
                f"Router {router.serial_number}: wlan1 interface not found"
            )

        wlan_id = interfaces[0]['id']
        wireless.set(id=wlan_id, ssid=ssid)

        logger.info(
            f"SSID updated to '{ssid}' on router {router.serial_number}"
        )
        return True

    except RouterOSError:
        raise
    except Exception as e:
        raise RouterOSError(
            f"Failed to set SSID on router {router.serial_number}: {e}"
        ) from e
    finally:
        pool.disconnect()


def get_router_info(router):
    """
    Fetch system identity, uptime, and resource usage from a router.

    Args:
        router: A Router model instance.

    Returns:
        A dict with keys: identity, uptime, cpu_load, free_memory, total_memory,
        board_name, version.

    Raises:
        RouterOSError: If the operation fails.
    """
    pool = get_connection(router)
    try:
        api = pool.get_api()

        # Get identity
        identity_resource = api.get_resource('/system/identity')
        identity = identity_resource.get()

        # Get resource info
        resource = api.get_resource('/system/resource')
        res_info = resource.get()

        info = {}
        if identity:
            info['identity'] = identity[0].get('name', '')
        if res_info:
            r = res_info[0]
            info['uptime'] = r.get('uptime', '')
            info['cpu_load'] = r.get('cpu-load', '')
            info['free_memory'] = r.get('free-memory', '')
            info['total_memory'] = r.get('total-memory', '')
            info['board_name'] = r.get('board-name', '')
            info['version'] = r.get('version', '')

        logger.info(f"Retrieved info for router {router.serial_number}")
        return info

    except RouterOSError:
        raise
    except Exception as e:
        raise RouterOSError(
            f"Failed to get info from router {router.serial_number}: {e}"
        ) from e
    finally:
        pool.disconnect()


def check_api_reachable(router):
    """
    Quick connectivity test: connect to the router API and disconnect.

    Args:
        router: A Router model instance.

    Returns:
        True if reachable, False otherwise.
    """
    try:
        pool = get_connection(router)
        api = pool.get_api()
        # Run a simple command to verify the connection works
        identity = api.get_resource('/system/identity').get()
        pool.disconnect()
        return bool(identity)
    except Exception:
        return False
