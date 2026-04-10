"""
Management command to build a custom SabiWiFi OpenWrt firmware image
for Xiaomi AC2100 using the OpenWrt Image Builder.

Usage:
    python manage.py build_openwrt_firmware
    python manage.py build_openwrt_firmware --download-only
    python manage.py build_openwrt_firmware --rebuild

The built image is saved to OPENWRT_FIRMWARE_PATH (default:
/opt/openwrt-imagebuilder/bin/firmware-latest.bin).

Architecture:
  - First-boot: WiFi on lan (temporary), WG key gen, heartbeat cron
  - Heartbeat: every 2 min sends MAC + WG pubkey to server
  - Provision: server returns inline shell script via heartbeat when
    reseller claims device — configures WG tunnel, uspot captive portal,
    RADIUS, firewall, and moves WiFi to captive bridge
"""
import os
import shutil
import subprocess

from django.conf import settings
from django.core.management.base import BaseCommand


IMAGEBUILDER_DIR = '/opt/openwrt-imagebuilder'
IMAGEBUILDER_URL = (
    'https://downloads.openwrt.org/releases/24.10.0/targets/ramips/mt7621/'
    'openwrt-imagebuilder-24.10.0-ramips-mt7621.Linux-x86_64.tar.zst'
)
# IMPORTANT: Xiaomi Mi Router AC2100 (black cylinder).
# Device reports as xiaomi,mi-router-ac2100 in OpenWrt (verified by sysupgrade check).
PROFILE = 'xiaomi_mi-router-ac2100'
OUTPUT_DIR = os.path.join(IMAGEBUILDER_DIR, 'bin')

# Packages to include in the image
INCLUDE_PACKAGES = [
    'wireguard-tools',
    'kmod-wireguard',
    'luci',                  # Web GUI — kept for dev/testing, strip for production
    'uspot',
    'uspot-www',             # HTML templates + CSS for captive portal pages
    'uspotfilter',           # nftables firewall interface for uspot
    'ratelimit',             # Per-client bandwidth limiting (HTB shaper)
    'curl',
    'ca-certificates',       # Required for HTTPS (heartbeat)
]

# Packages to REMOVE — minimal for dev builds (only strip PPP which is unused)
EXCLUDE_PACKAGES = [
    'ppp', 'ppp-mod-pppoe',
]

# Build dependencies (Ubuntu/Debian)
BUILD_DEPS = [
    'build-essential', 'file', 'libncurses-dev', 'zlib1g-dev',
    'gawk', 'git', 'gettext', 'libssl-dev', 'xsltproc', 'rsync',
    'wget', 'unzip', 'python3', 'zstd',
]


class Command(BaseCommand):
    help = 'Build custom SabiWiFi OpenWrt firmware image for Xiaomi AC2100'

    def add_arguments(self, parser):
        parser.add_argument(
            '--download-only', action='store_true',
            help='Only download the Image Builder, do not build.',
        )
        parser.add_argument(
            '--rebuild', action='store_true',
            help='Force rebuild even if firmware already exists.',
        )

    def handle(self, *args, **options):
        firmware_path = getattr(
            settings, 'OPENWRT_FIRMWARE_PATH',
            os.path.join(OUTPUT_DIR, 'firmware-latest.bin'),
        )

        if os.path.exists(firmware_path) and not options['rebuild']:
            self.stdout.write(self.style.WARNING(
                f'Firmware already exists at {firmware_path}. Use --rebuild to force.'
            ))
            return

        self._ensure_deps()
        self._download_imagebuilder()

        if options['download_only']:
            self.stdout.write(self.style.SUCCESS('Image Builder downloaded. Use --rebuild to build.'))
            return

        self._create_overlay_files()
        self._build_image()
        self._copy_output(firmware_path)

        self.stdout.write(self.style.SUCCESS(f'Firmware built successfully: {firmware_path}'))

    def _ensure_deps(self):
        """Check that build dependencies are installed."""
        self.stdout.write('Checking build dependencies...')
        try:
            result = subprocess.run(
                ['dpkg', '-l'] + BUILD_DEPS,
                capture_output=True, text=True,
            )
            missing = [p for p in BUILD_DEPS if p not in result.stdout]
            if missing:
                self.stdout.write(f'Installing missing deps: {", ".join(missing)}')
                subprocess.run(
                    ['sudo', 'apt-get', 'install', '-y'] + missing,
                    check=True,
                )
        except subprocess.CalledProcessError as e:
            self.stderr.write(f'Failed to install deps: {e}')
            raise

    def _download_imagebuilder(self):
        """Download and extract the OpenWrt Image Builder if not present."""
        makefile = os.path.join(IMAGEBUILDER_DIR, 'Makefile')
        if os.path.exists(makefile):
            self.stdout.write('Image Builder already downloaded.')
            return

        self.stdout.write('Downloading Image Builder...')
        os.makedirs('/opt', exist_ok=True)
        archive = '/tmp/openwrt-imagebuilder.tar.zst'

        subprocess.run(['wget', '-q', '-O', archive, IMAGEBUILDER_URL], check=True)
        self.stdout.write('Extracting...')
        subprocess.run(['tar', '--zstd', '-xf', archive, '-C', '/opt'], check=True)
        os.remove(archive)

        for entry in os.listdir('/opt'):
            if entry.startswith('openwrt-imagebuilder-') and os.path.isdir(f'/opt/{entry}'):
                if entry != 'openwrt-imagebuilder':
                    if os.path.exists(IMAGEBUILDER_DIR):
                        shutil.rmtree(IMAGEBUILDER_DIR)
                    os.rename(f'/opt/{entry}', IMAGEBUILDER_DIR)
                break

        self.stdout.write('Image Builder ready.')

    def _create_overlay_files(self):
        """Create the custom files overlay that gets baked into the image."""
        from operator_panel.models import PlatformSettings
        ps = PlatformSettings.load()
        platform_domain = ps.platform_domain or getattr(settings, 'PLATFORM_DOMAIN', 'sabiwifi.ng')

        files_dir = os.path.join(IMAGEBUILDER_DIR, 'files')
        if os.path.exists(files_dir):
            shutil.rmtree(files_dir)

        for d in ['etc/sabiwifi', 'etc/uci-defaults', 'etc/crontabs', 'etc/radcli',
                  'etc/dropbear', 'usr/bin']:
            os.makedirs(os.path.join(files_dir, d), exist_ok=True)

        # ── Bake server SSH key so we always have remote access ──
        authkeys_path = os.path.join(files_dir, 'etc/dropbear/authorized_keys')
        with open(authkeys_path, 'w') as f:
            f.write('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMx63y8nfNhOvk4cRDl0uX/SmJHQKRjmU1xHrA7V8oum sabiwifi-server\n')
        os.chmod(authkeys_path, 0o600)

        # ── Bake root password into /etc/shadow (survives even if first-boot fails) ──
        import crypt
        pw_hash = crypt.crypt('sabiwifi', crypt.mksalt(crypt.METHOD_MD5))
        shadow_path = os.path.join(files_dir, 'etc/shadow')
        with open(shadow_path, 'w') as f:
            f.write(f"root:{pw_hash}:0:0:99999:7:::\n"
                    "daemon:*:0:0:99999:7:::\n"
                    "ftp:*:0:0:99999:7:::\n"
                    "network:*:0:0:99999:7:::\n"
                    "nobody:*:0:0:99999:7:::\n")
        os.chmod(shadow_path, 0o600)

        # ── Bake radcli dictionary (required by uspot radius-client binary) ──
        radcli_dict = os.path.join(files_dir, 'etc/radcli/dictionary')
        with open(radcli_dict, 'w') as f:
            f.write("""\
#
# Radcli dictionary for uspot/SabiWiFi
#
ATTRIBUTE	User-Name		1	string
ATTRIBUTE	User-Password		2	string
ATTRIBUTE	CHAP-Password		3	string
ATTRIBUTE	NAS-IP-Address		4	ipv4addr
ATTRIBUTE	NAS-Port-Id		5	integer
ATTRIBUTE	Service-Type		6	integer
ATTRIBUTE	Framed-Protocol		7	integer
ATTRIBUTE	Framed-IP-Address	8	ipv4addr
ATTRIBUTE	Framed-IP-Netmask	9	ipv4addr
ATTRIBUTE	Framed-Routing		10	integer
ATTRIBUTE	Filter-Id		11	string
ATTRIBUTE	Framed-MTU		12	integer
ATTRIBUTE	Framed-Compression	13	integer
ATTRIBUTE	Login-IP-Host		14	ipv4addr
ATTRIBUTE	Login-Service		15	integer
ATTRIBUTE	Login-TCP-Port		16	integer
ATTRIBUTE	Reply-Message		18	string
ATTRIBUTE	Callback-Number		19	string
ATTRIBUTE	Callback-Id		20	string
ATTRIBUTE	Framed-Route		22	string
ATTRIBUTE	Framed-IPX-Network	23	ipv4addr
ATTRIBUTE	State			24	string
ATTRIBUTE	Class			25	string
ATTRIBUTE	Vendor-Specific		26	string
ATTRIBUTE	Session-Timeout		27	integer
ATTRIBUTE	Idle-Timeout		28	integer
ATTRIBUTE	Termination-Action	29	integer
ATTRIBUTE	Called-Station-Id	30	string
ATTRIBUTE	Calling-Station-Id	31	string
ATTRIBUTE	NAS-Identifier		32	string
ATTRIBUTE	Proxy-State		33	string
ATTRIBUTE	Acct-Status-Type	40	integer
ATTRIBUTE	Acct-Delay-Time		41	integer
ATTRIBUTE	Acct-Input-Octets	42	integer
ATTRIBUTE	Acct-Output-Octets	43	integer
ATTRIBUTE	Acct-Session-Id		44	string
ATTRIBUTE	Acct-Authentic		45	integer
ATTRIBUTE	Acct-Session-Time	46	integer
ATTRIBUTE	Acct-Input-Packets	47	integer
ATTRIBUTE	Acct-Output-Packets	48	integer
ATTRIBUTE	Acct-Terminate-Cause	49	integer
ATTRIBUTE	Acct-Input-Gigawords	52	integer
ATTRIBUTE	Acct-Output-Gigawords	53	integer
ATTRIBUTE	Event-Timestamp		55	integer
ATTRIBUTE	CHAP-Challenge		60	string
ATTRIBUTE	NAS-Port-Type		61	integer
ATTRIBUTE	Port-Limit		62	integer
ATTRIBUTE	Chargeable-User-Identity	89	string
VENDOR		WISPr			14122
ATTRIBUTE	WISPr-Location-ID		1	string	WISPr
ATTRIBUTE	WISPr-Location-Name		2	string	WISPr
ATTRIBUTE	WISPr-Logoff-URL		3	string	WISPr
ATTRIBUTE	WISPr-Redirection-URL		4	string	WISPr
ATTRIBUTE	WISPr-Bandwidth-Min-Up		5	integer	WISPr
ATTRIBUTE	WISPr-Bandwidth-Min-Down	6	integer	WISPr
ATTRIBUTE	WISPr-Bandwidth-Max-Up		7	integer	WISPr
ATTRIBUTE	WISPr-Bandwidth-Max-Down	8	integer	WISPr
ATTRIBUTE	WISPr-Session-Terminate-Time	9	string	WISPr
VENDOR		ChilliSpot		14559
ATTRIBUTE	ChilliSpot-Max-Input-Octets	1	integer	ChilliSpot
ATTRIBUTE	ChilliSpot-Max-Output-Octets	2	integer	ChilliSpot
ATTRIBUTE	ChilliSpot-Max-Total-Octets	3	integer	ChilliSpot
ATTRIBUTE	ChilliSpot-Bandwidth-Max-Up	4	integer	ChilliSpot
ATTRIBUTE	ChilliSpot-Bandwidth-Max-Down	5	integer	ChilliSpot
ATTRIBUTE	ChilliSpot-Config		6	string	ChilliSpot
ATTRIBUTE	ChilliSpot-Lang			7	string	ChilliSpot
""")

        # ── Bake crontab into overlay (works even if first-boot fails) ────────
        crontab_path = os.path.join(files_dir, 'etc/crontabs/root')
        with open(crontab_path, 'w') as f:
            f.write("*/2 * * * * /usr/bin/sabiwifi-heartbeat\n")

        # ── First-boot script (uci-defaults — runs once, auto-deletes) ─────────
        firstboot = os.path.join(files_dir, 'etc/uci-defaults/99-sabiwifi')
        with open(firstboot, 'w') as f:
            f.write(f"""\
#!/bin/sh
# SabiWiFi first-boot setup for OpenWrt
# Runs once on first boot via uci-defaults, then auto-deletes.
# Root password, SSH key, and crontab are baked into overlay as fallback.

logger -t sabiwifi "First boot: configuring SabiWiFi OpenWrt device"

# ── DNS forwarders ──
uci add_list dhcp.@dnsmasq[0].server='8.8.8.8'
uci add_list dhcp.@dnsmasq[0].server='1.1.1.1'
uci commit dhcp

# ── WiFi: enable radios with temporary SSIDs on lan ──
# These will be moved to the captive bridge by the provision script.
uci set wireless.radio0.disabled='0'
uci set wireless.radio0.country='NG'
uci delete wireless.default_radio0 2>/dev/null
uci set wireless.wifinet0=wifi-iface
uci set wireless.wifinet0.device='radio0'
uci set wireless.wifinet0.mode='ap'
uci set wireless.wifinet0.network='lan'
uci set wireless.wifinet0.ssid='SabiWiFi-Setup'
uci set wireless.wifinet0.encryption='none'
uci set wireless.radio1.disabled='0'
uci set wireless.radio1.country='NG'
uci delete wireless.default_radio1 2>/dev/null
uci set wireless.wifinet1=wifi-iface
uci set wireless.wifinet1.device='radio1'
uci set wireless.wifinet1.mode='ap'
uci set wireless.wifinet1.network='lan'
uci set wireless.wifinet1.ssid='SabiWiFi-Setup-5G'
uci set wireless.wifinet1.encryption='none'
uci commit wireless

# ── Generate WireGuard keys ──
mkdir -p /etc/sabiwifi
wg genkey | tee /etc/sabiwifi/wg_private.key | wg pubkey > /etc/sabiwifi/wg_public.key 2>/dev/null
chmod 600 /etc/sabiwifi/wg_private.key 2>/dev/null

# ── Enable cron for heartbeat ──
/etc/init.d/cron enable
/etc/init.d/cron restart

logger -t sabiwifi "First boot complete. Waiting for provisioning via heartbeat."
""")
        os.chmod(firstboot, 0o755)

        # ── SabiWiFi heartbeat script ─────────────────────────────────────────
        heartbeat = os.path.join(files_dir, 'usr/bin/sabiwifi-heartbeat')
        with open(heartbeat, 'w') as f:
            f.write(f"""\
#!/bin/sh
# SabiWiFi heartbeat — runs every 2 min via cron.
# Sends MAC + WG pubkey + system stats to server. If server returns a
# provision script (starts with #!/bin/sh), executes it.

MAC=""
for IFACE in eth0 br-lan eth1; do
    if [ -f /sys/class/net/$IFACE/address ]; then
        MAC=$(cat /sys/class/net/$IFACE/address | tr -d ':' | tr 'a-f' 'A-F')
        [ -n "$MAC" ] && break
    fi
done
[ -z "$MAC" ] && exit 1

WG_PUB=""
[ -f /etc/sabiwifi/wg_public.key ] && WG_PUB=$(cat /etc/sabiwifi/wg_public.key)

# ── Collect system stats ──

# CPU: sample /proc/stat twice with 1s gap
read_cpu() {{ awk '/^cpu / {{print $2+$3+$4+$5+$6+$7+$8, $5}}' /proc/stat; }}
CPU1=$(read_cpu)
sleep 1
CPU2=$(read_cpu)
CPU_PCT=$(echo "$CPU1 $CPU2" | awk '{{
    td=$3-$1; id=$4-$2;
    if(td>0) printf "%d",100*(td-id)/td; else print "0"
}}')

# Memory
MEM_TOTAL=$(awk '/^MemTotal/ {{print $2}}' /proc/meminfo)
MEM_AVAIL=$(awk '/^MemAvailable/ {{print $2}}' /proc/meminfo)
[ -z "$MEM_AVAIL" ] && MEM_AVAIL=$(awk '/^MemFree/ {{print $2}}' /proc/meminfo)
if [ "$MEM_TOTAL" -gt 0 ] 2>/dev/null; then
    MEM_PCT=$(( (MEM_TOTAL - MEM_AVAIL) * 100 / MEM_TOTAL ))
else
    MEM_PCT=0
fi

# Uptime (seconds)
UPTIME=$(awk '{{printf "%d", $1}}' /proc/uptime)

# WiFi clients per interface
WIFI_CLIENTS=0
GUEST_CLIENTS=0
GUEST5_CLIENTS=0
for WDEV in $(iw dev 2>/dev/null | awk '/Interface/{{print $2}}'); do
    COUNT=$(iw dev "$WDEV" station dump 2>/dev/null | grep -c "^Station")
    WIFI_CLIENTS=$((WIFI_CLIENTS + COUNT))
    SSID=$(iwinfo "$WDEV" info 2>/dev/null | awk -F'"' '/ESSID/{{print $2}}')
    case "$SSID" in
        *-5G) GUEST5_CLIENTS=$COUNT ;;
        *)    GUEST_CLIENTS=$COUNT ;;
    esac
done

# DHCP leases
DHCP_LEASES=0
[ -f /tmp/dhcp.leases ] && DHCP_LEASES=$(wc -l < /tmp/dhcp.leases)

# WAN traffic
WAN_RX=$(cat /sys/class/net/eth0/statistics/rx_bytes 2>/dev/null || echo 0)
WAN_TX=$(cat /sys/class/net/eth0/statistics/tx_bytes 2>/dev/null || echo 0)

# ── Send heartbeat with stats ──
STATS="cpu=$CPU_PCT&mem=$MEM_PCT&uptime=$UPTIME&wifi_clients=$WIFI_CLIENTS"
STATS="$STATS&guest_clients=$GUEST_CLIENTS&guest5_clients=$GUEST5_CLIENTS"
STATS="$STATS&dhcp_leases=$DHCP_LEASES&wan_rx=$WAN_RX&wan_tx=$WAN_TX"

RESPONSE=$(curl -sf --max-time 10 \\
    -H "X-WG-Public-Key: $WG_PUB" \\
    "https://{platform_domain}/api/routers/heartbeat/$MAC/?$STATS")
[ $? -ne 0 ] && exit 1

# Server returns provision script inline when device needs provisioning
if echo "$RESPONSE" | head -1 | grep -q "^#!/bin/sh"; then
    logger -t sabiwifi "heartbeat: received provision script, executing..."
    echo "$RESPONSE" > /tmp/sabiwifi-provision.sh
    chmod +x /tmp/sabiwifi-provision.sh
    /bin/sh /tmp/sabiwifi-provision.sh >> /tmp/sabiwifi-provision.log 2>&1
    RESULT=$?
    logger -t sabiwifi "heartbeat: provision script finished (exit=$RESULT)"
    rm -f /tmp/sabiwifi-provision.sh
fi
""")
        os.chmod(heartbeat, 0o755)

        self.stdout.write('Overlay files created.')

    def _build_image(self):
        """Run the Image Builder."""
        self.stdout.write('Building firmware image (this may take a few minutes)...')

        packages = ' '.join(INCLUDE_PACKAGES + [f'-{p}' for p in EXCLUDE_PACKAGES])

        result = subprocess.run(
            ['make', 'image', f'PROFILE={PROFILE}',
             f'PACKAGES={packages}', 'FILES=files'],
            cwd=IMAGEBUILDER_DIR,
            capture_output=True, text=True,
            timeout=600,
        )

        if result.returncode != 0:
            self.stderr.write('Image Builder STDOUT:\n' + result.stdout[-2000:])
            self.stderr.write('Image Builder STDERR:\n' + result.stderr[-2000:])
            raise Exception(f'Image build failed with return code {result.returncode}')

        self.stdout.write('Image build complete.')

    def _copy_output(self, firmware_path):
        """Find the sysupgrade image and copy it to the output path."""
        bin_dir = os.path.join(IMAGEBUILDER_DIR, 'bin', 'targets', 'ramips', 'mt7621')

        if not os.path.exists(bin_dir):
            raise Exception(f'Build output directory not found: {bin_dir}')

        sysupgrade = None
        for f in sorted(os.listdir(bin_dir)):
            if 'sysupgrade' in f and f.endswith('.bin') and PROFILE in f:
                sysupgrade = os.path.join(bin_dir, f)
                break

        if not sysupgrade:
            raise Exception(f'No sysupgrade image found in {bin_dir}')

        os.makedirs(os.path.dirname(firmware_path), exist_ok=True)
        shutil.copy2(sysupgrade, firmware_path)
        size_mb = os.path.getsize(firmware_path) / (1024 * 1024)
        self.stdout.write(f'Firmware: {firmware_path} ({size_mb:.1f} MB)')
