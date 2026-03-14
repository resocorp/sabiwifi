"""
Generate provision.rsc scripts for MikroTik routers.
Each variable is templated from the Router model + PlatformSettings.
"""
from django.conf import settings


PROVISION_TEMPLATE = """# ============================================
# SabiWiFi Provision Script
# Router: {serial_number}
# Generated: {timestamp}
# ============================================

# --- 0. Remove bootstrap scheduler (if present) ---
:do {{ /system/scheduler/remove sabiwifi-phonehome }} on-error={{}}
:do {{ /system/script/remove sabiwifi-phonehome }} on-error={{}}

# --- 1. Identity (used by hotspot redirect) ---
/system/identity/set name="{serial_number}"

# --- 2. RouterOS API user (for platform management) ---
/user/add name="{api_username}" password="{api_password}" group=full
/ip/service/set api address=10.99.0.0/16 disabled=no
/ip/service/set api-ssl disabled=yes

# --- 3. Guest network bridge ---
/interface/bridge/add name=hotspot-br
/ip/address/add address=10.8.0.1/16 interface=hotspot-br
/ip/pool/add name=hotspot-pool ranges=10.8.0.2-10.8.255.254
/ip/dhcp-server/add name=hotspot-dhcp interface=hotspot-br address-pool=hotspot-pool lease-time=2d
/ip/dhcp-server/network/add address=10.8.0.0/16 gateway=10.8.0.1

# --- 4. WireGuard tunnel ---
/interface/wireguard/add name=wg0 private-key="{wg_private_key}"
/interface/wireguard/peers/add interface=wg0 public-key="{server_wg_public_key}" endpoint-address={server_ip} endpoint-port=51820 allowed-address=10.99.0.0/16 persistent-keepalive=25
/ip/address/add address={wg_tunnel_ip}/32 interface=wg0

# --- 5. Download hotspot redirect files ---
/tool/fetch url="https://{platform_domain}/static/hotspot/login.html" dst-path=login.html
/tool/fetch url="https://{platform_domain}/static/hotspot/alogin.html" dst-path=alogin.html
/tool/fetch url="https://{platform_domain}/static/hotspot/flogin.html" dst-path=flogin.html
/tool/fetch url="https://{platform_domain}/static/hotspot/rlogin.html" dst-path=rlogin.html

# --- 6. Walled garden ---
/ip/hotspot/walled-garden/add dst-host={platform_domain}
/ip/hotspot/walled-garden/add dst-host=*.{platform_domain}
/ip/hotspot/walled-garden/add dst-host=paystack.com
/ip/hotspot/walled-garden/add dst-host=*.paystack.com
/ip/hotspot/walled-garden/add dst-host=*.paystack.co
/ip/hotspot/walled-garden/add dst-host=standard.paystack.co
/ip/hotspot/walled-garden/add dst-host=*.cloudflare.com

# --- 7. RADIUS (via WireGuard tunnel) ---
/radius/add service=hotspot address=10.99.0.1 secret="{nas_secret}" authentication-port=1812 accounting-port=1813 timeout=3s

# --- 8. Hotspot server ---
/ip/hotspot/profile/set default use-radius=yes interim-update=5m login-by=http-pap html-directory=. dns-name=portal.{platform_domain}
/ip/hotspot/user-profile/set default keepalive-timeout=2d
/ip/hotspot/add name=sabiwifi interface=hotspot-br address-pool=hotspot-pool idle-timeout=5m

# --- 9. Assign WiFi interface to hotspot bridge ---
/interface/bridge/port/add bridge=hotspot-br interface=wlan1

# --- 10. Firewall (basic: allow WG, allow hotspot, block rest) ---
/ip/firewall/filter/add chain=input protocol=udp dst-port=51820 action=accept comment="Allow WireGuard"
/ip/firewall/filter/add chain=input connection-state=established,related action=accept
/ip/firewall/filter/add chain=forward connection-state=established,related action=accept
/ip/firewall/nat/add chain=srcnat out-interface=ether1 action=masquerade comment="NAT for subscribers"

:log info "SabiWiFi provisioning complete"
"""


def generate_provision_rsc(router, wg_private_key):
    """
    Generate a provision.rsc script for a specific router.
    wg_private_key is passed in (not stored in DB) — generated at assignment time.
    """
    from django.utils import timezone

    return PROVISION_TEMPLATE.format(
        serial_number=router.serial_number,
        timestamp=timezone.now().isoformat(),
        api_username=router.api_username,
        api_password=router.api_password,
        wg_private_key=wg_private_key,
        server_wg_public_key=settings.SERVER_WG_PUBLIC_KEY,
        server_ip=settings.SERVER_IP,
        wg_tunnel_ip=router.wg_tunnel_ip,
        platform_domain=settings.PLATFORM_DOMAIN,
        nas_secret=router.nas_secret,
    )
