"""
Generate provision.rsc scripts for MikroTik routers.
Each variable is templated from the Router model + PlatformSettings.

Note: Uses RouterOS v7 command syntax (v7.13+ /interface/wifi system).
All commands use :do/on-error for idempotency so re-provisioning
(factory reset) doesn't fail on duplicate resources.
"""
from django.conf import settings


PROVISION_TEMPLATE = """\
# SABIWIFI-REPROVISION-v1
# ============================================
# SabiWiFi Provision Script
# Router: {serial_number}
# Generated: {timestamp}
# ============================================

# --- 0. Remove bootstrap artifacts (NOT phone-home — it self-removes after /import returns) ---
:do {{ /system scheduler remove [find name=sabiwifi-setup] }} on-error={{}}
:do {{ /system script remove [find name=sabiwifi-setup] }} on-error={{}}

# --- 1. Identity ---
/system identity set name="{serial_number}"

# --- 2. RouterOS API + REST user (for platform management via WG tunnel) ---
:do {{ /user add name="{api_username}" password="{api_password}" group=full }} on-error={{ /user set [find name="{api_username}"] password="{api_password}" }}
/ip service set api address=10.99.0.0/16 disabled=no
/ip service set api-ssl disabled=yes
/ip service set www address=10.99.0.0/16 disabled=no
/ip service set www-ssl disabled=yes

# --- 3. Guest network bridge + DHCP ---
:do {{ /interface bridge add name=hotspot-br }} on-error={{}}
:do {{ /ip address add address=10.8.0.1/16 interface=hotspot-br }} on-error={{}}
:do {{ /ip pool add name=hotspot-pool ranges=10.8.0.2-10.8.255.254 }} on-error={{}}
:do {{ /ip dhcp-server add name=hotspot-dhcp interface=hotspot-br address-pool=hotspot-pool lease-time=2d }} on-error={{}}
:do {{ /ip dhcp-server network add address=10.8.0.0/16 gateway=10.8.0.1 dns-server=10.8.0.1 }} on-error={{}}

# --- 4. DNS (router acts as recursive resolver for hotspot clients) ---
/ip dns set allow-remote-requests=yes servers=8.8.8.8,1.1.1.1

# --- 5. WireGuard tunnel to SabiWiFi server ---
:do {{ /interface wireguard add name=wg0 private-key="{wg_private_key}" }} on-error={{ /interface wireguard set [find name=wg0] private-key="{wg_private_key}" }}
# Idempotent peer: update if exists, add if not (avoids tearing down active handshake)
:if ([:len [/interface wireguard peers find interface=wg0 public-key="{server_wg_public_key}"]] > 0) do={{
  /interface wireguard peers set [find interface=wg0 public-key="{server_wg_public_key}"] endpoint-address={server_ip} endpoint-port=51820 allowed-address=10.99.0.0/16 persistent-keepalive=25
}} else={{
  :do {{ /interface wireguard peers remove [find interface=wg0] }} on-error={{}}
  /interface wireguard peers add interface=wg0 public-key="{server_wg_public_key}" endpoint-address={server_ip} endpoint-port=51820 allowed-address=10.99.0.0/16 persistent-keepalive=25
}}
# Idempotent IP + route
:if ([:len [/ip address find interface=wg0 address="{wg_tunnel_ip}/32"]] = 0) do={{
  :do {{ /ip address remove [find interface=wg0] }} on-error={{}}
  /ip address add address={wg_tunnel_ip}/32 interface=wg0
}}
:if ([:len [/ip route find comment="SabiWiFi WG"]] = 0) do={{
  /ip route add dst-address=10.99.0.0/16 gateway=wg0 comment="SabiWiFi WG"
}}

# --- 7. Walled garden (allow access before hotspot auth) ---
# Only specific hosts needed by the portal page. Do NOT add broad wildcards like
# *.gstatic.com, *.googleapis.com, or www.google.com — those match Android/Chrome
# captive-portal probes (connectivitycheck.gstatic.com/generate_204, www.google.com/
# generate_204), so the probe succeeds and the OS suppresses the "Sign in to
# network" prompt. Splash never appears. Allow only fonts/CDNs the portal loads.
# Purge any pre-existing broad entries left by earlier provisions.
:do {{ /ip hotspot walled-garden remove [find dst-host="*.gstatic.com"] }} on-error={{}}
:do {{ /ip hotspot walled-garden remove [find dst-host="*.googleapis.com"] }} on-error={{}}
:do {{ /ip hotspot walled-garden remove [find dst-host="www.google.com"] }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host={platform_domain} }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host="*.{platform_domain}" }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host=paystack.com }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host="*.paystack.com" }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host="*.paystack.co" }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host=standard.paystack.co }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host="*.cloudflare.com" }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host=fonts.googleapis.com }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host=fonts.gstatic.com }} on-error={{}}
:do {{ /ip hotspot walled-garden add dst-host=cdn.tailwindcss.com }} on-error={{}}

# --- 8. RADIUS (auth via WireGuard tunnel to server) ---
:do {{ /radius remove [find comment="sabiwifi"] }} on-error={{}}
/radius add service={radius_services} address=10.99.0.1 secret="{nas_secret}" authentication-port=1812 accounting-port=1813 timeout=3s comment="sabiwifi"

# --- 9. Hotspot server profile + server (RouterOS v7 syntax) ---
# login-by=http-chap: browser computes md5(chap-id || password || chap-challenge)
# before submitting — password never crosses the captive-portal LAN in plaintext.
# Our per-router login.html (served by Django) forwards chap-id/challenge to the
# portal; the portal's chap.js hashes and submits. HTTP-PAP is intentionally NOT
# enabled so a stale/malicious JS can't downgrade to plaintext.
/ip hotspot profile set default use-radius=yes radius-interim-update=5m login-by=http-chap html-directory=hotspot dns-name=wifi.portal
/ip/hotspot/user/profile/set default keepalive-timeout=2d
:do {{ /ip hotspot add name=sabiwifi interface=hotspot-br address-pool=hotspot-pool idle-timeout=5m }} on-error={{}}
/ip hotspot set sabiwifi disabled=no

# --- 9b. Download hotspot redirect HTML (AFTER hotspot add so hotspot/ dir exists) ---
# login.html is per-router: Django bakes the serial into the redirect URL so
# we don't depend on any RouterOS template variable for the identifier.
:do {{ /tool fetch url="https://{platform_domain}/api/routers/hotspot-html/{serial_number}/login.html" dst-path=hotspot/login.html mode=https check-certificate=no }} on-error={{}}
:do {{ /tool fetch url="https://{platform_domain}/static/hotspot/alogin.html" dst-path=hotspot/alogin.html mode=https check-certificate=no }} on-error={{}}
:do {{ /tool fetch url="https://{platform_domain}/static/hotspot/flogin.html" dst-path=hotspot/flogin.html mode=https check-certificate=no }} on-error={{}}
:do {{ /tool fetch url="https://{platform_domain}/static/hotspot/rlogin.html" dst-path=hotspot/rlogin.html mode=https check-certificate=no }} on-error={{}}

# --- 10. WiFi (RouterOS v7 /interface/wifi system) ---
# 2.4GHz channel: WiFi 6 (ax), auto channel selection, 20/40MHz width
:do {{ /interface/wifi/channel remove [find name=sabiwifi-ch-2g] }} on-error={{}}
/interface/wifi/channel add name=sabiwifi-ch-2g band=2ghz-ax width=20/40mhz

# 2.4GHz configuration: open AP for hotspot (captive portal handles auth)
:do {{ /interface/wifi/configuration remove [find name=sabiwifi-2g] }} on-error={{}}
/interface/wifi/configuration add name=sabiwifi-2g mode=ap ssid="{wifi_ssid}" country=Nigeria channel=sabiwifi-ch-2g

# Apply to wifi1 (primary 2.4GHz radio) and enable
/interface/wifi set [find default-name=wifi1] configuration=sabiwifi-2g disabled=no

# Add wifi1 to hotspot bridge
:do {{ /interface/bridge/port remove [find interface=wifi1] }} on-error={{}}
/interface/bridge/port add bridge=hotspot-br interface=wifi1

# 5GHz radio (wifi2) if present - silently skips if hardware not available
:do {{ /interface/wifi/channel remove [find name=sabiwifi-ch-5g] }} on-error={{}}
:do {{ /interface/wifi/channel add name=sabiwifi-ch-5g band=5ghz-ax width=20/40/80mhz }} on-error={{}}
:do {{ /interface/wifi/configuration remove [find name=sabiwifi-5g] }} on-error={{}}
:do {{ /interface/wifi/configuration add name=sabiwifi-5g mode=ap ssid="{wifi_ssid_5g}" country=Nigeria channel=sabiwifi-ch-5g }} on-error={{}}
:do {{ /interface/wifi set [find default-name=wifi2] configuration=sabiwifi-5g disabled=no }} on-error={{}}
:do {{ /interface/bridge/port remove [find interface=wifi2] }} on-error={{}}
:do {{ /interface/bridge/port add bridge=hotspot-br interface=wifi2 }} on-error={{}}

{pppoe_block}
# --- 11. Firewall ---
:do {{ /ip firewall filter add chain=input protocol=udp dst-port=51820 action=accept comment="SabiWiFi WireGuard" }} on-error={{}}
:do {{ /ip firewall filter add chain=input in-interface=wg0 action=accept comment="SabiWiFi WG management" }} on-error={{}}
:do {{ /ip firewall filter add chain=input connection-state=established,related action=accept comment="SabiWiFi established" }} on-error={{}}
:do {{ /ip firewall filter add chain=forward connection-state=established,related action=accept comment="SabiWiFi forward" }} on-error={{}}
:do {{ /ip firewall nat add chain=srcnat out-interface=ether1 action=masquerade comment="SabiWiFi NAT" }} on-error={{}}

# --- 12. Bidirectional heartbeat + self-healing WG tunnel ---
# The heartbeat fetches a response body. If the server needs to push a
# re-provision (e.g. WG handshake never happened), the body begins with
# the sentinel "# SABIWIFI-REPROVISION-v1" — in which case the script
# /import-s the body. Plain "# ok" responses are discarded. This removes
# the one-shot provision limitation that left earlier devices unrecoverable.
:do {{ /system scheduler remove [find name=sabiwifi-heartbeat] }} on-error={{}}
:do {{ /system script remove [find name=sabiwifi-heartbeat] }} on-error={{}}
/system script add name=sabiwifi-heartbeat dont-require-permissions=yes source=":do {{ /tool fetch url=\\"https://{platform_domain}/api/routers/heartbeat/{serial_number}/\\" dst-path=sabiwifi-hb.rsc mode=https check-certificate=no }} on-error={{}}\\r\\n:local body \\"\\"\\r\\n:do {{ :set body [/file get [/file find name=sabiwifi-hb.rsc] contents] }} on-error={{}}\\r\\n:if ([:pick \\$body 0 25] = \\"# SABIWIFI-REPROVISION-v1\\") do={{ :log info \\"SabiWiFi: server requested re-provision\\"; /import sabiwifi-hb.rsc }}\\r\\n:do {{ /file remove sabiwifi-hb.rsc }} on-error={{}}\\r\\n:if ([/ping 10.99.0.1 count=2 interval=1] = 0) do={{ :log warning \\"SabiWiFi: WG tunnel down, resetting\\"; /interface/wireguard/disable wg0; :delay 2s; /interface/wireguard/enable wg0 }}"
/system scheduler add name=sabiwifi-heartbeat interval=2m start-time=startup on-event="/system script run sabiwifi-heartbeat"

:log info "SabiWiFi: provisioning complete for {serial_number}"
"""


def generate_provision_rsc(router, wg_private_key):
    """
    Generate a provision.rsc script for a specific router.
    wg_private_key is passed in (not stored in DB) - generated at assignment time.
    """
    from django.utils import timezone

    # WiFi SSID: use reseller branding if set, otherwise default
    wifi_ssid = 'SabiWiFi-2G'
    wifi_ssid_5g = 'SabiWiFi-5G'
    if router.reseller and router.reseller.branding:
        custom_ssid = router.reseller.branding.get('ssid', '')
        if custom_ssid:
            wifi_ssid = custom_ssid
            wifi_ssid_5g = f'{custom_ssid}-5G'

    # RADIUS services based on router service_mode
    mode = getattr(router, 'service_mode', 'hotspot')
    if mode == 'pppoe':
        radius_services = 'ppp'
    elif mode == 'both':
        radius_services = 'hotspot,ppp'
    else:
        radius_services = 'hotspot'

    # PPPoE server block (only when PPPoE is enabled)
    pppoe_block = ''
    if mode in ('pppoe', 'both'):
        pppoe_service_name = wifi_ssid.replace(' ', '-') + '-PPPoE' if wifi_ssid else 'SabiWiFi-PPPoE'
        pppoe_block = f"""
# --- 10b. PPPoE Server ---
/ppp profile set default use-radius=yes only-one=yes change-tcp-mss=yes dns-server=8.8.8.8,1.1.1.1
:do {{{{ /ip pool add name=pppoe-pool ranges=10.9.0.2-10.9.255.254 }}}} on-error={{{{}}}}
/ppp profile set default local-address=10.9.0.1 remote-address=pppoe-pool
:do {{{{ /interface pppoe-server server add service-name="{pppoe_service_name}" interface=hotspot-br default-profile=default one-session-per-host=yes max-sessions=200 }}}} on-error={{{{}}}}
"""

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
        wifi_ssid=wifi_ssid,
        wifi_ssid_5g=wifi_ssid_5g,
        radius_services=radius_services,
        pppoe_block=pppoe_block,
    )
