"""
Generate idempotent provision scripts for OpenWrt routers.

The provision script is delivered inline via the heartbeat response when a
reseller claims an OpenWrt device. It can safely run multiple times — all
anonymous UCI sections are deleted before creation, and named sections use
'set' (upsert semantics).

Configures:
  - WireGuard tunnel back to SabiWiFi server
  - uspot captive portal in UAM mode
  - RADIUS pointing to FreeRADIUS via WG tunnel
  - Firewall zones, rules, ipsets, and redirect
  - Guest WiFi SSIDs on a captive bridge
  - radcli config for uspot's radius-client
  - Heartbeat cron job
"""
from django.conf import settings


OPENWRT_PROVISION_TEMPLATE = """\
#!/bin/sh
# ============================================
# SabiWiFi OpenWrt Provision Script
# Device: {device_mac}
# Generated: {timestamp}
# ============================================
# This script is IDEMPOTENT — safe to run multiple times.
# All anonymous sections are cleaned before creation.

logger -t sabiwifi "Starting provisioning for {device_mac}"

# ============================================
# PHASE 1: Clean up any previous provision state
# ============================================

# Delete ALL anonymous firewall rules/zones/ipsets/redirects that we created.
# We identify ours by name prefix. Work backwards to avoid index shifting.
for TYPE in rule redirect ipset zone; do
    IDX=$(uci show firewall 2>/dev/null | grep "firewall\\.@$TYPE\\[" | grep -c '=')
    while [ "$IDX" -gt 0 ]; do
        IDX=$((IDX - 1))
        NAME=$(uci -q get "firewall.@$TYPE[$IDX].name" 2>/dev/null)
        case "$NAME" in
            Allow-WG|Allow-DHCP-NTP-captive|Restrict-input-captive|Allow-captive-CPD-WEB-UAM)
                uci -q delete "firewall.@$TYPE[$IDX]" ;;
            Forward-auth-captive|Allow-DNS-captive|Allow-captive-DAE|Allow-Whitelist)
                uci -q delete "firewall.@$TYPE[$IDX]" ;;
            Redirect-unauth-captive-CPD)
                uci -q delete "firewall.@$TYPE[$IDX]" ;;
            uspot|wlist)
                uci -q delete "firewall.@$TYPE[$IDX]" ;;
            captive)
                uci -q delete "firewall.@$TYPE[$IDX]" ;;
        esac
    done
done

# Delete named firewall zone 'wg' if it exists
uci -q delete firewall.wg 2>/dev/null

# Delete old uspot config
uci -q delete uspot.captive 2>/dev/null

# Delete old uhttpd instances we created
uci -q delete uhttpd.uspot 2>/dev/null
uci -q delete uhttpd.uam3990 2>/dev/null

# Delete old network interfaces we created
uci -q delete network.captive 2>/dev/null
uci -q delete network.wg0 2>/dev/null

# Delete old WG peers (all anonymous wireguard_wg0 sections)
while uci -q delete network.@wireguard_wg0[-1]; do :; done

# Delete old DHCP config for captive
uci -q delete dhcp.captive 2>/dev/null

# Delete old WiFi interfaces we create (guest, guest5)
uci -q delete wireless.guest 2>/dev/null
uci -q delete wireless.guest5 2>/dev/null

# Delete first-boot WiFi interfaces on lan (wifinet0, wifinet1)
uci -q delete wireless.wifinet0 2>/dev/null
uci -q delete wireless.wifinet1 2>/dev/null

logger -t sabiwifi "Cleanup complete, applying new config"

# ============================================
# PHASE 2: WireGuard tunnel
# ============================================

uci set network.wg0=interface
uci set network.wg0.proto='wireguard'
uci set network.wg0.private_key="$(cat /etc/sabiwifi/wg_private.key)"
uci add_list network.wg0.addresses='{wg_tunnel_ip}/32'

uci add network wireguard_wg0
uci set network.@wireguard_wg0[-1].public_key='{server_wg_public_key}'
uci set network.@wireguard_wg0[-1].endpoint_host='{server_ip}'
uci set network.@wireguard_wg0[-1].endpoint_port='51820'
uci set network.@wireguard_wg0[-1].allowed_ips='10.99.0.0/16'
uci set network.@wireguard_wg0[-1].persistent_keepalive='25'
uci set network.@wireguard_wg0[-1].route_allowed_ips='1'

# Firewall: named WG zone (idempotent via 'set')
uci set firewall.wg=zone
uci set firewall.wg.name='wg'
uci set firewall.wg.network='wg0'
uci set firewall.wg.input='ACCEPT'
uci set firewall.wg.output='ACCEPT'
uci set firewall.wg.forward='ACCEPT'

# Allow WG UDP on WAN
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-WG'
uci set firewall.@rule[-1].src='wan'
uci set firewall.@rule[-1].dest_port='51820'
uci set firewall.@rule[-1].proto='udp'
uci set firewall.@rule[-1].target='ACCEPT'

# ============================================
# PHASE 3: Guest network bridge + DHCP
# ============================================

uci set network.captive=interface
uci set network.captive.proto='static'
uci set network.captive.ipaddr='10.8.0.1'
uci set network.captive.netmask='255.255.0.0'
uci set network.captive.type='bridge'
uci set network.captive.device='br-captive'

uci set dhcp.captive=dhcp
uci set dhcp.captive.interface='captive'
uci set dhcp.captive.start='2'
uci set dhcp.captive.limit='1000'
uci set dhcp.captive.leasetime='2h'

# ============================================
# PHASE 4: WiFi on captive bridge
# ============================================

# 2.4GHz guest WiFi
uci set wireless.guest=wifi-iface
uci set wireless.guest.device='radio0'
uci set wireless.guest.network='captive'
uci set wireless.guest.mode='ap'
uci set wireless.guest.ssid='{wifi_ssid}'
uci set wireless.guest.encryption='none'
uci set wireless.radio0.disabled='0'

# 5GHz guest WiFi (if radio1 exists)
if uci -q get wireless.radio1 > /dev/null 2>&1; then
    uci set wireless.guest5=wifi-iface
    uci set wireless.guest5.device='radio1'
    uci set wireless.guest5.network='captive'
    uci set wireless.guest5.mode='ap'
    uci set wireless.guest5.ssid='{wifi_ssid_5g}'
    uci set wireless.guest5.encryption='none'
    uci set wireless.radio1.disabled='0'
fi

# ============================================
# PHASE 5: uspot captive portal (UAM mode)
# ============================================

uci set uspot.captive=uspot
uci set uspot.captive.auth_mode='uam'
uci set uspot.captive.interface='captive'
uci set uspot.captive.setname='uspot'
uci set uspot.captive.uam_server='https://{platform_domain}/portal/'
uci set uspot.captive.uam_port='3990'
uci set uspot.captive.challenge="$(head -c 16 /dev/urandom | md5sum | cut -d' ' -f1)"
uci set uspot.captive.nasid='{device_mac}'
uci set uspot.captive.nasmac='{device_mac_colon}'

# RADIUS config (through WireGuard tunnel)
uci set uspot.captive.auth_server='10.99.0.1'
uci set uspot.captive.auth_port='1812'
uci set uspot.captive.auth_secret='{nas_secret}'
uci set uspot.captive.acct_server='10.99.0.1'
uci set uspot.captive.acct_port='1813'
uci set uspot.captive.acct_secret='{nas_secret}'
uci set uspot.captive.acct_interval='300'
uci set uspot.captive.das_port='3799'

# ============================================
# PHASE 6: uhttpd instances for uspot
# ============================================

# Move default uhttpd to LAN-only (free port 80 on captive IP)
uci -q delete uhttpd.main.listen_http
uci add_list uhttpd.main.listen_http='192.168.1.1:80'
uci -q delete uhttpd.main.listen_https

# uspot CPD instance on captive IP port 80
uci set uhttpd.uspot=uhttpd
uci add_list uhttpd.uspot.listen_http='10.8.0.1:80'
uci set uhttpd.uspot.redirect_https='0'
uci set uhttpd.uspot.max_requests='5'
uci set uhttpd.uspot.no_dirlists='1'
uci set uhttpd.uspot.home='/www-uspot'
uci add_list uhttpd.uspot.ucode_prefix='/hotspot=/usr/share/uspot/handler.uc'
uci add_list uhttpd.uspot.ucode_prefix='/cpd=/usr/share/uspot/handler-cpd.uc'
uci set uhttpd.uspot.error_page='/cpd'

# UAM handler instance on captive IP port 3990
uci set uhttpd.uam3990=uhttpd
uci add_list uhttpd.uam3990.listen_http='10.8.0.1:3990'
uci set uhttpd.uam3990.redirect_https='0'
uci set uhttpd.uam3990.max_requests='5'
uci set uhttpd.uam3990.no_dirlists='1'
uci set uhttpd.uam3990.home='/www-uspot'
uci add_list uhttpd.uam3990.ucode_prefix='/logon=/usr/share/uspot/handler-uam.uc'
uci add_list uhttpd.uam3990.ucode_prefix='/logoff=/usr/share/uspot/handler-uam.uc'
uci add_list uhttpd.uam3990.ucode_prefix='/logout=/usr/share/uspot/handler-uam.uc'

# ============================================
# PHASE 7: Firewall
# ============================================

# nftables ipset for authenticated client MACs
uci add firewall ipset
uci set firewall.@ipset[-1].name='uspot'
uci add_list firewall.@ipset[-1].match='src_mac'

# Captive zone
uci add firewall zone
uci set firewall.@zone[-1].name='captive'
uci add_list firewall.@zone[-1].network='captive'
uci set firewall.@zone[-1].input='REJECT'
uci set firewall.@zone[-1].output='ACCEPT'
uci set firewall.@zone[-1].forward='REJECT'

# CPD hijacking: DNAT port 80 for unauthenticated clients
uci add firewall redirect
uci set firewall.@redirect[-1].name='Redirect-unauth-captive-CPD'
uci set firewall.@redirect[-1].src='captive'
uci set firewall.@redirect[-1].src_dport='80'
uci set firewall.@redirect[-1].proto='tcp'
uci set firewall.@redirect[-1].target='DNAT'
uci set firewall.@redirect[-1].reflection='0'
uci set firewall.@redirect[-1].ipset='!uspot'

# Allow DHCP + NTP
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-DHCP-NTP-captive'
uci set firewall.@rule[-1].src='captive'
uci set firewall.@rule[-1].proto='udp'
uci set firewall.@rule[-1].dest_port='67 123'
uci set firewall.@rule[-1].target='ACCEPT'

# Block captive clients from LAN services
uci add firewall rule
uci set firewall.@rule[-1].name='Restrict-input-captive'
uci set firewall.@rule[-1].src='captive'
uci set firewall.@rule[-1].dest_ip='!captive'
uci set firewall.@rule[-1].target='DROP'

# Allow CPD/web/UAM ports
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-captive-CPD-WEB-UAM'
uci set firewall.@rule[-1].src='captive'
uci set firewall.@rule[-1].dest_port='80 443 3990'
uci set firewall.@rule[-1].proto='tcp'
uci set firewall.@rule[-1].target='ACCEPT'

# Allow DNS from captive clients
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-DNS-captive'
uci set firewall.@rule[-1].src='captive'
uci add_list firewall.@rule[-1].proto='udp'
uci add_list firewall.@rule[-1].proto='tcp'
uci set firewall.@rule[-1].dest_port='53'
uci set firewall.@rule[-1].target='ACCEPT'

# Allow forwarding to WAN for authenticated clients only
uci add firewall rule
uci set firewall.@rule[-1].name='Forward-auth-captive'
uci set firewall.@rule[-1].src='captive'
uci set firewall.@rule[-1].dest='wan'
uci set firewall.@rule[-1].proto='any'
uci set firewall.@rule[-1].target='ACCEPT'
uci set firewall.@rule[-1].ipset='uspot'

# Allow RADIUS DAE from server (CoA disconnect)
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-captive-DAE'
uci set firewall.@rule[-1].src='wan'
uci set firewall.@rule[-1].proto='udp'
uci set firewall.@rule[-1].family='ipv4'
uci set firewall.@rule[-1].src_ip='10.99.0.1'
uci set firewall.@rule[-1].dest_port='3799'
uci set firewall.@rule[-1].target='ACCEPT'

# Whitelist: allow captive clients to reach SabiWiFi + Paystack before auth
uci add firewall ipset
uci set firewall.@ipset[-1].name='wlist'
uci add_list firewall.@ipset[-1].match='dest_ip'
uci add_list firewall.@ipset[-1].entry='{server_ip}'
uci add_list firewall.@ipset[-1].entry='104.18.28.7'
uci add_list firewall.@ipset[-1].entry='104.18.29.7'
uci add_list firewall.@ipset[-1].entry='104.18.8.49'
uci add_list firewall.@ipset[-1].entry='104.18.9.49'
uci add_list firewall.@ipset[-1].entry='142.250.140.95'
uci add_list firewall.@ipset[-1].entry='142.250.151.94'

uci add firewall rule
uci set firewall.@rule[-1].name='Allow-Whitelist'
uci set firewall.@rule[-1].src='captive'
uci set firewall.@rule[-1].dest='wan'
uci set firewall.@rule[-1].proto='any'
uci set firewall.@rule[-1].ipset='wlist'
uci set firewall.@rule[-1].target='ACCEPT'

# ============================================
# PHASE 8: DNS for captive clients
# ============================================

# Ensure dnsmasq listens on all interfaces (needed for captive bridge)
uci set dhcp.@dnsmasq[0].localservice='0'

# Clear any duplicate DNS servers then set them
uci -q delete dhcp.@dnsmasq[0].server
uci add_list dhcp.@dnsmasq[0].server='8.8.8.8'
uci add_list dhcp.@dnsmasq[0].server='1.1.1.1'

# Point captive clients to router as DNS
uci -q delete dhcp.captive.dhcp_option
uci add_list dhcp.captive.dhcp_option='6,10.8.0.1'

# ============================================
# PHASE 9: radcli config
# ============================================

echo '10.99.0.1 {nas_secret}' > /etc/radcli/servers
chmod 600 /etc/radcli/servers
sed -i 's/^authserver.*/authserver 10.99.0.1/' /etc/radcli/radiusclient.conf
sed -i 's/^acctserver.*/acctserver 10.99.0.1/' /etc/radcli/radiusclient.conf

# ============================================
# PHASE 10: Commit and restart services
# ============================================

uci commit network
uci commit wireless
uci commit dhcp
uci commit firewall
uci commit uspot
uci commit uhttpd

logger -t sabiwifi "UCI committed, restarting services..."

/etc/init.d/network restart
sleep 5
wifi reload
sleep 3

# Wait for br-captive to get its IP before starting uhttpd
for i in 1 2 3 4 5 6 7 8 9 10; do
    ip addr show br-captive 2>/dev/null | grep -q "10.8.0.1" && break
    sleep 2
done

/etc/init.d/firewall restart
/etc/init.d/uhttpd restart
/etc/init.d/uspot restart

# ============================================
# PHASE 11: Verify critical services
# ============================================

# Check br-captive exists and has the right IP
if ip addr show br-captive 2>/dev/null | grep -q "10.8.0.1"; then
    logger -t sabiwifi "OK: br-captive has 10.8.0.1"
else
    logger -t sabiwifi "ERROR: br-captive missing or wrong IP"
fi

# Check WireGuard tunnel can reach server
if ping -c 2 -W 5 10.99.0.1 > /dev/null 2>&1; then
    logger -t sabiwifi "OK: WireGuard tunnel to 10.99.0.1 is up"
else
    logger -t sabiwifi "WARNING: WireGuard tunnel to 10.99.0.1 not reachable yet"
fi

# Check uhttpd is listening on captive IP
if netstat -tlnp 2>/dev/null | grep -q "10.8.0.1:80"; then
    logger -t sabiwifi "OK: uhttpd listening on 10.8.0.1:80"
else
    logger -t sabiwifi "WARNING: uhttpd not listening on 10.8.0.1:80"
fi

# ============================================
# PHASE 12: Mark provisioned
# ============================================

mkdir -p /etc/sabiwifi
echo "{device_mac}" > /etc/sabiwifi/provisioned
date >> /etc/sabiwifi/provisioned

logger -t sabiwifi "Provisioning complete for {device_mac}"
"""


def generate_openwrt_provision(router, platform_domain=None):
    """
    Generate an idempotent provision shell script for an OpenWrt router.
    """
    from django.utils import timezone

    if platform_domain is None:
        platform_domain = settings.PLATFORM_DOMAIN

    wifi_ssid = 'SabiWiFi'
    wifi_ssid_5g = 'SabiWiFi-5G'
    if router.reseller and router.reseller.branding:
        custom_ssid = router.reseller.branding.get('ssid', '')
        if custom_ssid:
            wifi_ssid = custom_ssid
            wifi_ssid_5g = f'{custom_ssid}-5G'

    # Format MAC with colons for nasmac
    mac_raw = router.serial_number.upper().replace(':', '').replace('-', '')
    device_mac_colon = ':'.join(mac_raw[i:i+2] for i in range(0, 12, 2))

    return OPENWRT_PROVISION_TEMPLATE.format(
        device_mac=router.serial_number,
        device_mac_colon=device_mac_colon,
        timestamp=timezone.now().isoformat(),
        wg_tunnel_ip=router.wg_tunnel_ip,
        server_wg_public_key=settings.SERVER_WG_PUBLIC_KEY,
        server_ip=settings.SERVER_IP,
        platform_domain=platform_domain,
        nas_secret=router.nas_secret,
        wifi_ssid=wifi_ssid,
        wifi_ssid_5g=wifi_ssid_5g,
    )
