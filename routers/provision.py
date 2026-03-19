"""
Generate provision.rsc scripts for MikroTik routers.
Each variable is templated from the Router model + PlatformSettings.

Note: Uses RouterOS v7 command syntax. All commands use :do/on-error
for idempotency so re-provisioning (factory reset) doesn't fail on
duplicate resources.
"""
from django.conf import settings


PROVISION_TEMPLATE = """\
# ============================================
# SabiWiFi Provision Script
# Router: {serial_number}
# Generated: {timestamp}
# ============================================

# --- 0. Remove bootstrap scheduler/script (if present) ---
:do {{ /system scheduler remove [find name=sabiwifi-phonehome] }} on-error={{}}
:do {{ /system script remove [find name=sabiwifi-phonehome] }} on-error={{}}
:do {{ /system scheduler remove [find name=sabiwifi-setup] }} on-error={{}}
:do {{ /system script remove [find name=sabiwifi-setup] }} on-error={{}}

# --- 1. Identity ---
/system identity set name="{serial_number}"

# --- 2. RouterOS API user (for platform management via WG tunnel) ---
:do {{ /user add name="{api_username}" password="{api_password}" group=full }} on-error={{\\
  /user set [find name="{api_username}"] password="{api_password}"\\
}}
/ip service set api address=10.99.0.0/16 disabled=no
/ip service set api-ssl disabled=yes

# --- 3. Guest network bridge + DHCP ---
:do {{ /interface bridge add name=hotspot-br }} on-error={{}}
:do {{ /ip address add address=10.8.0.1/16 interface=hotspot-br }} on-error={{}}
:do {{ /ip pool add name=hotspot-pool ranges=10.8.0.2-10.8.255.254 }} on-error={{}}
:do {{ /ip dhcp-server add name=hotspot-dhcp interface=hotspot-br address-pool=hotspot-pool lease-time=2d }} on-error={{}}
:do {{ /ip dhcp-server network add address=10.8.0.0/16 gateway=10.8.0.1 }} on-error={{}}

# --- 4. WireGuard tunnel to SabiWiFi server ---
:do {{ /interface wireguard add name=wg0 private-key="{wg_private_key}" }} on-error={{\\
  /interface wireguard set [find name=wg0] private-key="{wg_private_key}"\\
}}
# Remove any existing peers before adding
:do {{ /interface wireguard peers remove [find interface=wg0] }} on-error={{}}
/interface wireguard peers add interface=wg0 \\
  public-key="{server_wg_public_key}" \\
  endpoint-address={server_ip} \\
  endpoint-port=51820 \\
  allowed-address=10.99.0.0/16 \\
  persistent-keepalive=25
:do {{ /ip address add address={wg_tunnel_ip}/32 interface=wg0 }} on-error={{}}

# --- 5. Download hotspot redirect HTML files ---
:do {{ /tool fetch url="https://{platform_domain}/static/hotspot/login.html" dst-path=hotspot/login.html mode=https check-certificate=no }} on-error={{}}
:do {{ /tool fetch url="https://{platform_domain}/static/hotspot/alogin.html" dst-path=hotspot/alogin.html mode=https check-certificate=no }} on-error={{}}
:do {{ /tool fetch url="https://{platform_domain}/static/hotspot/flogin.html" dst-path=hotspot/flogin.html mode=https check-certificate=no }} on-error={{}}
:do {{ /tool fetch url="https://{platform_domain}/static/hotspot/rlogin.html" dst-path=hotspot/rlogin.html mode=https check-certificate=no }} on-error={{}}

# --- 6. Walled garden (allow access before hotspot auth) ---
:do {{ /ip hotspot walled-garden add dst-host={platform_domain} }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host="*.{platform_domain}" }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host=paystack.com }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host="*.paystack.com" }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host="*.paystack.co" }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host=standard.paystack.co }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host="*.cloudflare.com" }} on-error={{}}

# --- 7. RADIUS (auth via WireGuard tunnel to server) ---
:do {{ /radius remove [find comment="sabiwifi"] }} on-error={{}}
/radius add service=hotspot address=10.99.0.1 secret="{nas_secret}" \\
  authentication-port=1812 accounting-port=1813 timeout=3s comment="sabiwifi"

# --- 8. Hotspot server profile + server ---
/ip hotspot profile set default \\
  use-radius=yes \\
  interim-update=5m \\
  login-by=http-pap \\
  html-directory=hotspot \\
  dns-name=wifi.portal
/ip hotspot user-profile set default keepalive-timeout=2d
:do {{ /ip hotspot add name=sabiwifi interface=hotspot-br address-pool=hotspot-pool idle-timeout=5m }} on-error={{}}

# --- 9. Assign WiFi interface to hotspot bridge ---
:do {{ /interface bridge port add bridge=hotspot-br interface=wlan1 }} on-error={{}}

# --- 10. Firewall ---
:do {{ /ip firewall filter add chain=input protocol=udp dst-port=51820 action=accept comment="SabiWiFi WireGuard" }} on-error={{}}
:do {{ /ip firewall filter add chain=input connection-state=established,related action=accept comment="SabiWiFi established" }} on-error={{}}
:do {{ /ip firewall filter add chain=forward connection-state=established,related action=accept comment="SabiWiFi forward" }} on-error={{}}
:do {{ /ip firewall nat add chain=srcnat out-interface=ether1 action=masquerade comment="SabiWiFi NAT" }} on-error={{}}

:log info "SabiWiFi: provisioning complete for {serial_number}"
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
