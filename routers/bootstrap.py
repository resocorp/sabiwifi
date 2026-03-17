"""
Generate bootstrap .rsc scripts for MikroTik routers.

The bootstrap script is the minimal config flashed via Netinstall or USB.
It sets identity + DHCP client, then creates a scheduler that phones home
every 60s to fetch the full provision script once the router is claimed.

IMPORTANT: This script runs as a "default configuration" during first boot.
MikroTik default config scripts have a tight execution timeout, so we must
NOT do any /tool/fetch here. The scheduler handles fetching after boot.
"""

BOOTSTRAP_TEMPLATE = """\
# ============================================
# SabiWiFi Bootstrap Script
# Router: {serial_number}
# ============================================

# --- 1. Set router identity to serial number ---
/system identity set name="{serial_number}"

# --- 2. Get internet via DHCP on ether1 ---
:do {{ /ip dhcp-client add interface=ether1 disabled=no add-default-route=yes }} on-error={{}}

# --- 3. DNS fallback (in case DHCP doesn't provide DNS) ---
/ip dns set servers=8.8.8.8,1.1.1.1 allow-remote-requests=yes

# --- 4. Create phone-home script ---
/system script add name=sabiwifi-phonehome dont-require-permissions=yes source="\\
:local fetchOk false;\\r\\
:do {{\\r\\
  /tool fetch url=\\"https://{platform_domain}/api/routers/provision/{serial_number}/\\" dst-path=provision.rsc mode=https check-certificate=no;\\r\\
  :set fetchOk true;\\r\\
}} on-error={{\\r\\
  :log warning \\"SabiWiFi: fetch failed, will retry\\";\\r\\
}};\\r\\
:if (\\$fetchOk) do={{\\r\\
  :local fileContent [/file get [/file find name=provision.rsc] contents];\\r\\
  :if ([:len \\$fileContent] > 100) do={{\\r\\
    :log info \\"SabiWiFi: provision script received, importing...\\";\\r\\
    /import provision.rsc;\\r\\
    :delay 2s;\\r\\
    /file remove provision.rsc;\\r\\
    /system scheduler remove [find name=sabiwifi-phonehome];\\r\\
    /system script remove [find name=sabiwifi-phonehome];\\r\\
    :log info \\"SabiWiFi: provisioning complete\\";\\r\\
  }} else={{\\r\\
    :log info \\"SabiWiFi: not ready yet (router not claimed), will retry\\";\\r\\
    /file remove provision.rsc;\\r\\
  }};\\r\\
}};\\r\\
"

# --- 5. Schedule phone-home every 2 minutes (allow time for DHCP) ---
/system scheduler add name=sabiwifi-phonehome interval=2m start-time=startup on-event="/system script run sabiwifi-phonehome"

:log info "SabiWiFi: bootstrap installed, phone-home scheduled"
"""


def generate_bootstrap_rsc(serial_number, platform_domain):
    """
    Generate a bootstrap .rsc script for a specific router.

    Args:
        serial_number: The router's unique serial number.
        platform_domain: The platform domain (e.g., app.sabiwifi.com).

    Returns:
        The .rsc script content as a string.
    """
    return BOOTSTRAP_TEMPLATE.format(
        serial_number=serial_number,
        platform_domain=platform_domain,
    )
