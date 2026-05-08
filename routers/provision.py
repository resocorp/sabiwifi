"""
Generate provision.rsc scripts for MikroTik routers (RouterOS v7+ only).

Two topology modes:
  - AUTO    : legacy behaviour. WAN auto-discovered via DHCP lease, every
              other ether port + every WiFi radio dumped into one
              hotspot-br bridge. Used when Router.port_assignments is empty.
  - EXPLICIT: operator picked per-port roles in the dashboard. WAN is set
              from the assignment, hotspot-role ports go into hotspot-br,
              pppoe-role ports into a dedicated pppoe-br (PPPoE server
              binds there instead of the hotspot bridge).

The unchanged sections (WG tunnel, RADIUS, walled garden, hotspot profile,
WiFi block, watchdog, heartbeat, announce) are shared between both modes
via format-slot parameters. The legacy auto-mode output is verified
byte-identical against a golden fixture in tests/test_provisioning.py —
that's what protects already-deployed routers from drift.

Re-provisioning is safe — every command is wrapped in :do/on-error or
checked for existence first.
"""
from django.conf import settings

from routers import catalogue
from routers.vlans import HOTSPOT_VLAN, PPPOE_VLAN


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

# --- 0a. Require RouterOS v7+ ---
:local osMajor [:tonum [:pick [/system resource get version] 0 1]]
:if ($osMajor < 7) do={{
  :log error "SabiWiFi: RouterOS v7 required — refusing to provision on legacy firmware"
  :error "RouterOS v7 required"
}}

# --- 1. Identity ---
/system identity set name="{serial_number}"

# --- 2. RouterOS API + REST user (for platform management via WG tunnel) ---
:do {{ /user add name="{api_username}" password="{api_password}" group=full }} on-error={{ /user set [find name="{api_username}"] password="{api_password}" }}
/ip service set api address=10.99.0.0/16 disabled=no
/ip service set api-ssl disabled=yes
/ip service set www address=10.99.0.0/16 disabled=no
/ip service set www-ssl disabled=yes

{wan_discovery_block}
# WAN interface-list (used by NAT rule below — survives port changes)
:do {{ /interface list add name=WAN }} on-error={{}}
/interface list member remove [find list=WAN]
/interface list member add list=WAN interface=$wanIface

# --- 4. Guest network bridge + DHCP ---
:do {{ /interface bridge add name=hotspot-br }} on-error={{}}
:do {{ /ip address add address=10.8.0.1/16 interface=hotspot-br }} on-error={{}}
:do {{ /ip pool add name=hotspot-pool ranges=10.8.0.2-10.8.255.254 }} on-error={{}}
:do {{ /ip dhcp-server add name=hotspot-dhcp interface=hotspot-br address-pool=hotspot-pool lease-time=2d }} on-error={{}}
:do {{ /ip dhcp-server network add address=10.8.0.0/16 gateway=10.8.0.1 dns-server=10.8.0.1 }} on-error={{}}

{pppoe_bridge_setup_block}
{port_bridging_block}

# --- 5. DNS (router acts as recursive resolver for hotspot clients) ---
/ip dns set allow-remote-requests=yes servers=8.8.8.8,1.1.1.1

# --- 6. WireGuard tunnel to SabiWiFi server ---
# mtu=1380: RouterOS default 1420 silently blackholes WG traffic on many
# Nigerian carrier paths (PPPoE backhaul, CGNAT boxes, transparent
# middleboxes that strip ICMP "frag needed" so PMTU discovery never
# converges). Small packets like the initial WG handshake and 25s
# keepalives still fit, so the tunnel APPEARS to come up briefly, then
# the first sustained traffic dies and the handshake never renews
# within WG's 180s rekey window. 1380 is the standard conservative
# value for WG over consumer broadband — covers PPPoE + light IPSec
# wrapping + CGNAT.
:do {{ /interface wireguard add name=wg0 mtu=1380 private-key="{wg_private_key}" }} on-error={{ /interface wireguard set [find name=wg0] mtu=1380 private-key="{wg_private_key}" }}
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
# Only specific hosts needed by the portal page. Do NOT add broad wildcards
# matching captive-portal probes (gstatic.com generate_204) — those let the
# OS suppress the "Sign in to network" prompt and the splash never appears.
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

# Accept RADIUS Disconnect-Messages (RFC 5176) on port 3799 so the platform
# can kick sessions via the "Disconnect all devices" button.
/radius incoming set accept=yes port=3799

# --- 9. Hotspot server profile + server (RouterOS v7 syntax) ---
# login-by=http-pap: the portal runs on a different origin from the MikroTik's
# 10.8.0.1, so any POST back to /login is cross-site. Modern browsers default
# cookies to SameSite=Lax and drop them on cross-site POSTs, which breaks
# http-chap (MikroTik stores chap-id/challenge keyed by the session cookie).
# PAP is stateless and self-contained in the POST.
/ip hotspot profile set default use-radius=yes radius-interim-update=5m login-by=http-pap html-directory=hotspot dns-name=wifi.portal
/ip/hotspot/user/profile/set default keepalive-timeout=2d
:do {{ /ip hotspot add name=sabiwifi interface=hotspot-br address-pool=hotspot-pool idle-timeout=5m }} on-error={{}}
/ip hotspot set sabiwifi disabled=no

# --- 9b. Download hotspot redirect HTML (AFTER hotspot add so hotspot/ dir exists) ---
:do {{ /tool fetch url="https://{platform_domain}/api/routers/hotspot-html/{serial_number}/login.html" dst-path=hotspot/login.html mode=https check-certificate=no }} on-error={{}}
:do {{ /tool fetch url="https://{platform_domain}/static/hotspot/alogin.html" dst-path=hotspot/alogin.html mode=https check-certificate=no }} on-error={{}}
:do {{ /tool fetch url="https://{platform_domain}/static/hotspot/flogin.html" dst-path=hotspot/flogin.html mode=https check-certificate=no }} on-error={{}}
:do {{ /tool fetch url="https://{platform_domain}/static/hotspot/rlogin.html" dst-path=hotspot/rlogin.html mode=https check-certificate=no }} on-error={{}}

# --- 10. WiFi (only if hardware exists; loops over actual radios found) ---
# RouterOS v7 /interface/wifi system. Devices without WiFi (hEX, RB750)
# have an empty /interface/wifi list — the whole block is skipped.
:if ([:len [/interface wifi find]] > 0) do={{
  # Drop any prior SabiWiFi profiles (clean re-provision)
  :do {{ /interface/wifi/security remove [find name=sabiwifi-sec] }} on-error={{}}
  :do {{ /interface/wifi/configuration remove [find name=sabiwifi-2g] }} on-error={{}}
  :do {{ /interface/wifi/configuration remove [find name=sabiwifi-5g] }} on-error={{}}
  :do {{ /interface/wifi/channel remove [find name=sabiwifi-ch-2g] }} on-error={{}}
  :do {{ /interface/wifi/channel remove [find name=sabiwifi-ch-5g] }} on-error={{}}

  # Channels (one per band — the radio picks the right one based on its band)
  /interface/wifi/channel add name=sabiwifi-ch-2g band=2ghz-ax width=20/40mhz
  :do {{ /interface/wifi/channel add name=sabiwifi-ch-5g band=5ghz-ax width=20/40/80mhz }} on-error={{}}

  # Optional WPA2/WPA3 security profile (only if a password is set; otherwise open AP)
{wifi_security_block}
  # Per-band configuration.
  # country= is REQUIRED: without it, RouterOS v7 wifi regulatory blocks every
  # channel and the radio stays stuck at running=false with no SSID on air.
  # The enum is case-sensitive title-case ("Nigeria", not "nigeria"/"NG").
  /interface/wifi/configuration add name=sabiwifi-2g mode=ap country=Nigeria ssid="{wifi_ssid}" channel=sabiwifi-ch-2g{wifi_security_attr}
  :do {{ /interface/wifi/configuration add name=sabiwifi-5g mode=ap country=Nigeria ssid="{wifi_ssid_5g}" channel=sabiwifi-ch-5g{wifi_security_attr} }} on-error={{}}

  # Apply to every wifi radio: pick 5g config if the radio supports 5GHz, else 2g.
  # Then add to hotspot bridge (idempotent).
  :foreach w in=[/interface wifi find] do={{
    :local wname [/interface wifi get $w name]
    # Hardware capability lives on /interface/wifi/radio — /interface/wifi's
    # channel.band just reflects the last applied config, so it can't be
    # trusted to tell us which bands this radio actually supports.
    #
    # /interface/wifi/radio get bands returns an ARRAY, not a string.
    # [:find $array "..."] returns "nothing", and `nothing != -1` is true
    # in RouterOS — so the old single-expression form ALWAYS chose 5g
    # even on 2.4GHz-only boards, leaving the radio unable to transmit.
    # Iterate the array explicitly instead.
    :local is5g false
    :do {{
      :foreach b in=[/interface/wifi/radio get [find interface=$wname] bands] do={{
        :if ([:find $b "5ghz"] != -1) do={{ :set is5g true }}
      }}
    }} on-error={{}}
    :local cfg "sabiwifi-2g"
    :if ($is5g) do={{ :set cfg "sabiwifi-5g" }}
    :do {{ /interface/wifi set $w configuration=$cfg disabled={wifi_disabled} }} on-error={{}}
    :if ([:len [/interface bridge port find interface=$wname]] = 0) do={{
      :do {{ /interface/bridge/port add bridge=hotspot-br interface=$wname }} on-error={{}}
    }}
  }}
}} else={{
  :log info "SabiWiFi: no WiFi hardware on this device — skipping WiFi block"
}}

{pppoe_block}
# --- 11. Firewall ---
:do {{ /ip firewall filter add chain=input protocol=udp dst-port=51820 action=accept comment="SabiWiFi WireGuard" }} on-error={{}}
:do {{ /ip firewall filter add chain=input in-interface=wg0 action=accept comment="SabiWiFi WG management" }} on-error={{}}
:do {{ /ip firewall filter add chain=input connection-state=established,related action=accept comment="SabiWiFi established" }} on-error={{}}
:do {{ /ip firewall filter add chain=forward connection-state=established,related action=accept comment="SabiWiFi forward" }} on-error={{}}
# NAT uses the WAN interface-list so the rule survives WAN port changes
:do {{ /ip firewall nat remove [find comment="SabiWiFi NAT"] }} on-error={{}}
/ip firewall nat add chain=srcnat out-interface-list=WAN action=masquerade comment="SabiWiFi NAT"

# --- 12. Bidirectional heartbeat + self-healing WG tunnel ---
# The heartbeat fetches a response body. If the server needs to push a
# re-provision, the body begins with "# SABIWIFI-REPROVISION-v1" — in
# which case the script /import-s the body. Plain "# ok" responses are
# discarded. Also pings 10.99.0.1 and resets WG if the tunnel is dead.
#
# IMPORTANT: this whole provision script is /import-ed FROM the running
# sabiwifi-heartbeat script. Removing that script mid-import interrupts
# execution ("script error: interrupted"), so Sections 12/13 never run
# and the heartbeat loop dies. Always update in-place via /system script
# set, never remove the running script.
:local hbSrc ":do {{ /tool fetch url=\\"https://{platform_domain}/api/routers/heartbeat/{serial_number}/\\" dst-path=sabiwifi-hb.rsc mode=https check-certificate=no }} on-error={{}}\\r\\n:local body \\"\\"\\r\\n:do {{ :set body [/file get [/file find name=sabiwifi-hb.rsc] contents] }} on-error={{}}\\r\\n:if ([:pick \\$body 0 25] = \\"# SABIWIFI-REPROVISION-v1\\") do={{ :log info \\"SabiWiFi: server requested re-provision\\"; /import sabiwifi-hb.rsc }}\\r\\n:do {{ /file remove sabiwifi-hb.rsc }} on-error={{}}\\r\\n:if ([/ping 10.99.0.1 count=2 interval=1] = 0) do={{ :log warning \\"SabiWiFi: WG tunnel down, resetting\\"; /interface/wireguard/disable wg0; :delay 2s; /interface/wireguard/enable wg0 }}"
:if ([:len [/system script find name=sabiwifi-heartbeat]] = 0) do={{
  /system script add name=sabiwifi-heartbeat dont-require-permissions=yes source=$hbSrc
}} else={{
  /system script set [find name=sabiwifi-heartbeat] source=$hbSrc
}}
:if ([:len [/system scheduler find name=sabiwifi-heartbeat]] = 0) do={{
  /system scheduler add name=sabiwifi-heartbeat interval=2m start-time=startup on-event="/system script run sabiwifi-heartbeat"
}} else={{
  /system scheduler set [find name=sabiwifi-heartbeat] interval=2m start-time=startup on-event="/system script run sabiwifi-heartbeat"
}}

# --- 13. Watchdog: if WG never handshakes within 5 min of provision, reboot. ---
# Replaces RouterOS CLI safe-mode (which is interactive-only). A bad provision
# that wedges the management tunnel triggers a reboot, which restarts the
# heartbeat and lets the server re-push a corrected script.
:local wdEvent ":local lh [/interface/wireguard/peers get [find interface=wg0] last-handshake]; :if ([:typeof \\$lh] = \\"nothing\\" || \\$lh > 10m) do={{ :log error \\"SabiWiFi watchdog: WG dead, rebooting\\"; /system reboot }}"
:if ([:len [/system scheduler find name=sabiwifi-watchdog]] = 0) do={{
  /system scheduler add name=sabiwifi-watchdog start-time=startup interval=5m on-event=$wdEvent
}} else={{
  /system scheduler set [find name=sabiwifi-watchdog] start-time=startup interval=5m on-event=$wdEvent
}}

# --- 14. Announce hardware capabilities to platform (one-shot, drives the GUI) ---
:local board [/system resource get board-name]
:local ros [/system resource get version]
:local wif "no"
:local fg "no"
:if ([:len [/interface wifi find]] > 0) do={{
  :set wif "yes"
  # Read the hardware-supported bands from /interface/wifi/radio (channel.band
  # would just reflect whatever config we just applied, not real capability).
  :foreach ra in=[/interface/wifi/radio find] do={{
    :do {{
      :foreach b in=[/interface/wifi/radio get $ra bands] do={{
        :if ([:find $b "5ghz"] != -1) do={{ :set fg "yes" }}
      }}
    }} on-error={{}}
  }}
}}
:local ec [:len [/interface ethernet find]]
:do {{ /tool fetch url=("https://{platform_domain}/api/routers/announce/{serial_number}/?board=" . $board . "&ros=" . $ros . "&wifi=" . $wif . "&fg=" . $fg . "&ether=" . $ec . "&wan=" . $wanIface) mode=https check-certificate=no keep-result=no }} on-error={{}}

:log info "SabiWiFi: provisioning complete for {serial_number}"
"""


# ---------------------------------------------------------------------------
# Port topology — chooses AUTO vs EXPLICIT block contents
# ---------------------------------------------------------------------------

# Verbatim copy of the legacy WAN-discovery section. Substituting this into
# the {wan_discovery_block} slot reproduces the pre-topology RSC byte-for-byte.
AUTO_WAN_DISCOVERY_BLOCK = """\
# --- 3. WAN auto-discovery ---
# Customer can plug internet into ANY ether port. Bootstrap already enabled
# DHCP on every ether port; whichever got a lease becomes WAN. The rest
# get bridged to the guest network.
:local wanIface ""
:foreach c in=[/ip dhcp-client find status=bound] do={
  :if ($wanIface = "") do={ :set wanIface [/ip dhcp-client get $c interface] }
}
:if ($wanIface = "") do={
  # No DHCP lease found — fall back to ether1 so re-provision doesn't dead-end
  :set wanIface "ether1"
  :log warning "SabiWiFi: no DHCP lease detected, defaulting WAN to ether1"
}
:log info ("SabiWiFi: WAN detected on " . $wanIface)
"""

# Verbatim copy of the legacy port-bridging section.
AUTO_PORT_BRIDGING_BLOCK = """\
# --- 4a. Bridge every non-WAN ether port to the guest network ---
:foreach e in=[/interface ethernet find] do={
  :local ename [/interface ethernet get $e name]
  :if ($ename != $wanIface) do={
    # Drop DHCP on this port (it's a LAN port now)
    :foreach c in=[/ip dhcp-client find interface=$ename] do={
      :do { /ip dhcp-client remove $c } on-error={}
    }
    # Drop any IP we accidentally have on it
    :foreach a in=[/ip address find interface=$ename] do={
      :do { /ip address remove $a } on-error={}
    }
    # Add to bridge (idempotent)
    :if ([:len [/interface bridge port find interface=$ename]] = 0) do={
      :do { /interface bridge port add bridge=hotspot-br interface=$ename } on-error={}
    }
  }
}"""


def _explicit_wan_block(wan_iface):
    """Operator picked the WAN port — set it literally and verify it exists."""
    return f"""\
# --- 3. WAN explicit assignment ---
# Operator chose this port as the management/WAN port via the dashboard.
:local wanIface "{wan_iface}"
:if ([:len [/interface ethernet find name=$wanIface]] = 0) do={{
  :log warning ("SabiWiFi: configured WAN port " . $wanIface . " not found, falling back to ether1")
  :set wanIface "ether1"
}}
:log info ("SabiWiFi: WAN set to " . $wanIface)
"""


def _explicit_pppoe_bridge_setup(pppoe_ports):
    """Bridge for the PPPoE-role ports. Empty when no PPPoE ports."""
    if not pppoe_ports:
        return ''
    return """\
# --- 4b. PPPoE bridge (separate L2 broadcast domain from hotspot) ---
:do { /interface bridge add name=pppoe-br } on-error={}
"""


def _explicit_port_bridging_block(hotspot_ports, pppoe_ports):
    """
    Strip DHCP/IP from every non-WAN ether port, then explicitly assign each
    listed port to its bridge. Ports omitted from both lists stay unbridged
    (operator can still manage them via SSH).
    """
    lines = ["# --- 4a. Explicit per-port bridge assignment ---"]
    lines.append(":foreach e in=[/interface ethernet find] do={")
    lines.append("  :local ename [/interface ethernet get $e name]")
    lines.append("  :if ($ename != $wanIface) do={")
    lines.append("    :foreach c in=[/ip dhcp-client find interface=$ename] do={")
    lines.append("      :do { /ip dhcp-client remove $c } on-error={}")
    lines.append("    }")
    lines.append("    :foreach a in=[/ip address find interface=$ename] do={")
    lines.append("      :do { /ip address remove $a } on-error={}")
    lines.append("    }")
    lines.append("    # Strip any prior bridge membership so role re-assignment is clean")
    lines.append("    :foreach bp in=[/interface bridge port find interface=$ename] do={")
    lines.append("      :do { /interface bridge port remove $bp } on-error={}")
    lines.append("    }")
    lines.append("  }")
    lines.append("}")
    for p in hotspot_ports:
        lines.append(
            f':if ([:len [/interface bridge port find interface={p}]] = 0) do={{ '
            f':do {{ /interface bridge port add bridge=hotspot-br interface={p} }} on-error={{}} }}'
        )
    for p in pppoe_ports:
        lines.append(
            f':if ([:len [/interface bridge port find interface={p}]] = 0) do={{ '
            f':do {{ /interface bridge port add bridge=pppoe-br interface={p} }} on-error={{}} }}'
        )
    return '\n'.join(lines)


def build_port_topology(router):
    """
    Resolve a router's per-port topology.

    Returns a dict with:
      mode:              'auto' | 'explicit'
      wan_iface:         port name (only meaningful in explicit mode)
      hotspot_ports:     [str] (ether ports tagged hotspot)
      pppoe_ports:       [str] (ether ports tagged pppoe)
      wifi_to_role:      {wifi_name: role}  (radios → role)
      pppoe_bridge:      'hotspot-br' | 'pppoe-br' — where the PPPoE server binds

    A router with empty `port_assignments` falls into 'auto' mode and the
    legacy DHCP-discovery RSC sections are emitted unchanged.
    """
    assignments = getattr(router, 'port_assignments', None) or {}
    if not assignments:
        return {
            'mode': 'auto',
            'wan_iface': None,
            'hotspot_ports': [],
            'pppoe_ports': [],
            'wifi_to_role': {},
            'pppoe_bridge': 'hotspot-br',
        }

    entry = catalogue.get_catalogue_entry(
        router.board_name,
        ether_port_count=router.ether_port_count,
        has_wifi=router.has_wifi,
        has_5ghz=router.has_5ghz,
    )
    valid_ports = set(catalogue.all_port_names(entry))
    radio_names = {r['name'] for r in entry['radios']}

    wan_iface = None
    hotspot_ports = []
    pppoe_ports = []
    wifi_to_role = {}
    for port, role in assignments.items():
        if port not in valid_ports or role not in catalogue.VALID_ROLES:
            continue
        if port in radio_names:
            wifi_to_role[port] = role
            continue
        if role == catalogue.ROLE_WAN:
            wan_iface = port
        elif role == catalogue.ROLE_HOTSPOT:
            hotspot_ports.append(port)
        elif role == catalogue.ROLE_PPPOE:
            pppoe_ports.append(port)
        # ROLE_LAN reserved — silently skipped in v1

    return {
        'mode': 'explicit',
        'wan_iface': wan_iface or 'ether1',
        'hotspot_ports': sorted(hotspot_ports),
        'pppoe_ports': sorted(pppoe_ports),
        'wifi_to_role': wifi_to_role,
        'pppoe_bridge': 'pppoe-br' if pppoe_ports else 'hotspot-br',
    }


def generate_provision_rsc(router, wg_private_key):
    """
    Generate a provision.rsc script for a specific router.
    wg_private_key is passed in (not stored in DB) - generated at assignment time.
    """
    from django.utils import timezone

    topology = build_port_topology(router)

    if topology['mode'] == 'auto':
        wan_discovery_block = AUTO_WAN_DISCOVERY_BLOCK
        port_bridging_block = AUTO_PORT_BRIDGING_BLOCK
        pppoe_bridge_setup_block = ''
    else:
        wan_discovery_block = _explicit_wan_block(topology['wan_iface'])
        port_bridging_block = _explicit_port_bridging_block(
            topology['hotspot_ports'], topology['pppoe_ports'],
        )
        pppoe_bridge_setup_block = _explicit_pppoe_bridge_setup(topology['pppoe_ports'])

    # WiFi SSID resolution: explicit Router.wifi_ssid > reseller branding > default
    wifi_ssid = (getattr(router, 'wifi_ssid', '') or '').strip() or 'SabiWiFi'
    if wifi_ssid == 'SabiWiFi' and router.reseller and router.reseller.branding:
        custom_ssid = (router.reseller.branding.get('ssid', '') or '').strip()
        if custom_ssid:
            wifi_ssid = custom_ssid
    wifi_ssid_5g = f'{wifi_ssid}-5G'

    # WiFi enabled flag → maps to RouterOS disabled=yes/no
    wifi_disabled = 'no' if getattr(router, 'wifi_enabled', True) else 'yes'

    # Optional WPA2/WPA3 security profile
    wifi_password = (getattr(router, 'wifi_password', '') or '').strip()
    if wifi_password:
        wifi_security_block = (
            '  /interface/wifi/security add name=sabiwifi-sec '
            'authentication-types=wpa2-psk,wpa3-psk passphrase="{pw}"\n'
        ).format(pw=wifi_password)
        wifi_security_attr = ' security=sabiwifi-sec'
    else:
        wifi_security_block = '  # (open network — no password set)\n'
        wifi_security_attr = ''

    # RADIUS services based on router service_mode
    mode = getattr(router, 'service_mode', 'hotspot')
    if mode == 'pppoe':
        radius_services = 'ppp'
    elif mode == 'both':
        radius_services = 'hotspot,ppp'
    else:
        radius_services = 'hotspot'

    # PPPoE server block — reuses subscriber phone+PIN via RADIUS
    # (radcheck.Cleartext-Password is populated by accounts; MS-CHAPv2 works).
    pppoe_block = ''
    if mode in ('pppoe', 'both'):
        pppoe_service_name = wifi_ssid.replace(' ', '-') + '-PPPoE'
        pppoe_iface = topology['pppoe_bridge']
        pppoe_block = f"""
# --- 10b. PPPoE Server ---
# Customers dial PPPoE with username = phone (E.164), password = SabiWiFi PIN.
# Authentication methods include MS-CHAPv2 for Windows' built-in dialer.
# RouterOS v7 moved the RADIUS toggle from /ppp profile to /ppp aaa.
# We use a dedicated 'sabiwifi-pppoe' profile so we don't mutate the
# built-in default profile (shared by L2TP/PPTP/etc).
/ppp aaa set use-radius=yes accounting=yes interim-update=5m
:do {{ /ip pool add name=pppoe-pool ranges=10.9.0.2-10.9.255.254 }} on-error={{}}
# Tear down the pppoe-server BEFORE the profile, otherwise the profile
# is still referenced and the remove silently fails — then the bare
# profile-add halts the whole script.
:do {{ /interface pppoe-server server remove [find interface={pppoe_iface}] }} on-error={{}}
:if ([:len [/ppp profile find name=sabiwifi-pppoe]] = 0) do={{
  /ppp profile add name=sabiwifi-pppoe only-one=yes change-tcp-mss=yes dns-server=8.8.8.8,1.1.1.1 local-address=10.9.0.1 remote-address=pppoe-pool
}} else={{
  /ppp profile set [find name=sabiwifi-pppoe] only-one=yes change-tcp-mss=yes dns-server=8.8.8.8,1.1.1.1 local-address=10.9.0.1 remote-address=pppoe-pool
}}
/interface pppoe-server server add service-name="{pppoe_service_name}" interface={pppoe_iface} default-profile=sabiwifi-pppoe one-session-per-host=yes max-sessions=200 authentication=pap,chap,mschap2 disabled=no
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
        wifi_disabled=wifi_disabled,
        wifi_security_block=wifi_security_block,
        wifi_security_attr=wifi_security_attr,
        radius_services=radius_services,
        pppoe_block=pppoe_block,
        wan_discovery_block=wan_discovery_block,
        port_bridging_block=port_bridging_block,
        pppoe_bridge_setup_block=pppoe_bridge_setup_block,
    )
