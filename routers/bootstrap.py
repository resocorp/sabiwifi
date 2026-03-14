"""
Generate bootstrap .rsc scripts for MikroTik routers.

The bootstrap script is the minimal config flashed via Netinstall or USB.
It contains only: identity, DHCP client, and a phone-home scheduler
that fetches the full provision script once the router is claimed.
"""

BOOTSTRAP_TEMPLATE = """# ============================================
# SabiWiFi Bootstrap Script
# Router: {serial_number}
# ============================================
# Flash this via Netinstall or USB.
# The router will phone home every 60s until
# it receives its full provisioning config.
# ============================================

# --- 1. Identity (so the router knows its serial) ---
/system/identity/set name="{serial_number}"

# --- 2. Internet on ether1 via DHCP ---
/ip/dhcp-client/add interface=ether1 disabled=no add-default-route=yes

# --- 3. Phone-home scheduler ---
/system/script/add name=sabiwifi-phonehome source="\\
  :do {{\\
    /tool/fetch url=\\"https://{platform_domain}/api/routers/provision/{serial_number}/\\" \\
      dst-path=provision.rsc mode=https check-certificate=no;\\
    :if ([:len [/file find name=provision.rsc]] > 0) do={{\\
      /import provision.rsc;\\
      /file remove provision.rsc;\\
      /system/scheduler/remove sabiwifi-phonehome;\\
      /system/script/remove sabiwifi-phonehome;\\
      :log info \\"SabiWiFi: provisioning complete\\";\\
    }}\\
  }} on-error={{ :log warning \\"SabiWiFi: provision fetch failed, will retry\\" }}\\
"
/system/scheduler/add name=sabiwifi-phonehome interval=1m \\
  on-event="/system/script/run sabiwifi-phonehome"

:log info "SabiWiFi bootstrap installed — waiting for provisioning"
"""


def generate_bootstrap_rsc(serial_number, platform_domain):
    """
    Generate a bootstrap .rsc script for a specific router.

    Args:
        serial_number: The router's unique serial number.
        platform_domain: The platform domain (e.g., sabiwifi.ng).

    Returns:
        The .rsc script content as a string.
    """
    return BOOTSTRAP_TEMPLATE.format(
        serial_number=serial_number,
        platform_domain=platform_domain,
    )
