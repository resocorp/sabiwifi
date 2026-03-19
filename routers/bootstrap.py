"""
Generate bootstrap .rsc scripts for MikroTik routers.

Two-stage bootstrap approach:
  Stage 1 (bootstrap.rsc) — Flat single-line commands that work everywhere:
    Netinstall, terminal paste, /import. Sets up DHCP + DNS, then creates a
    tiny setup script that fetches Stage 2 from the server.

  Stage 2 (phonehome-setup.rsc) — Fetched from server and /import-ed.
    Can use full RouterOS syntax (multi-line blocks, local variables).
    Reads hardware serial, sets identity, creates the real phone-home script.
"""

# ── Legacy per-serial bootstrap (kept for backward compatibility) ────────────

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

# ── Stage 1: Generic bootstrap (all single-line commands) ────────────────────

GENERIC_BOOTSTRAP_TEMPLATE = """\
# ============================================
# SabiWiFi Generic Bootstrap
# Flash to ANY MikroTik via Netinstall/USB
# or paste into terminal. Every line is a
# single self-contained command.
# ============================================

# --- 1. Get internet via DHCP on ether1 ---
:do {{ /ip/dhcp-client/add interface=ether1 disabled=no add-default-route=yes }} on-error={{}}

# --- 2. DNS fallback ---
/ip/dns/set servers=8.8.8.8,1.1.1.1 allow-remote-requests=yes

# --- 3. Setup script: fetches phone-home installer from server (skips if already done) ---
/system/script/add name=sabiwifi-setup dont-require-permissions=yes source=":if ([:len [/system/script/find name=sabiwifi-phonehome]] > 0) do={{ :log info \\"SabiWiFi: already configured\\" }} else={{ /tool/fetch url=\\"https://{platform_domain}/api/routers/phonehome-setup/\\" dst-path=phonehome-setup.rsc mode=https check-certificate=no; :delay 2s; /import phonehome-setup.rsc; /file/remove phonehome-setup.rsc }}"

# --- 4. Run setup every 2 minutes until it succeeds ---
/system/scheduler/add name=sabiwifi-setup interval=2m start-time=startup on-event="/system/script/run sabiwifi-setup"

:log info "SabiWiFi: bootstrap installed, setup scheduled"
"""

# ── Stage 2: Phone-home setup (fetched and /import-ed as a file) ─────────────
#
# Escaping rules for this template (NOT a raw string):
#   Python .format():  {{ → {   }} → }   {platform_domain} → replaced
#   Top-level lines:   Plain $var — no escaping needed (executed by /import)
#   Inside source="...":  \\$ → \$ in output → RouterOS reads as $
#                          \\" → \" in output → RouterOS reads as literal "
#
# Two escaping contexts in one template:
#   - Lines 1-4, 6: top-level /import context → plain $ and "
#   - Line 5: source="..." string → needs \$ and \" for RouterOS

PHONEHOME_SETUP_TEMPLATE = """\
# ============================================
# SabiWiFi Phone-Home Setup (Stage 2)
# Fetched from server and /import-ed.
# ============================================

# --- 1. Read hardware serial ---
:local mySerial
:do {{
  :set mySerial [/system/routerboard/get serial-number]
}} on-error={{
  :set mySerial [/interface/ethernet/get [find default-name=ether1] mac-address]
  :set mySerial ([:pick $mySerial 0 2].[:pick $mySerial 3 5].[:pick $mySerial 6 8].[:pick $mySerial 9 11].[:pick $mySerial 12 14].[:pick $mySerial 15 17])
}}

# --- 2. Set identity ---
/system/identity/set name=$mySerial

# --- 3. Remove any previous phone-home artifacts (idempotent) ---
:do {{ /system/scheduler/remove [find name=sabiwifi-phonehome] }} on-error={{}}
:do {{ /system/script/remove [find name=sabiwifi-phonehome] }} on-error={{}}

# --- 4. Create phone-home script (single-line source) ---
/system/script/add name=sabiwifi-phonehome dont-require-permissions=yes source=":local s [/system/routerboard/get serial-number]; :do {{ /tool/fetch url=(\\"https://{platform_domain}/api/routers/provision/\\" . \\$s . \\"/\\") dst-path=provision.rsc mode=https check-certificate=no }} on-error={{ :log warning \\"SabiWiFi: fetch failed\\"; :error \\"fetch failed\\" }}; :local c [/file/get [/file/find name=provision.rsc] contents]; :if ([:len \\$c] > 100) do={{ /import provision.rsc; :delay 2s; /file/remove provision.rsc; /system/scheduler/remove [find name=sabiwifi-phonehome]; /system/script/remove [find name=sabiwifi-phonehome]; :log info \\"SabiWiFi: provisioning complete\\" }} else={{ :log info \\"SabiWiFi: not ready yet, will retry\\"; /file/remove provision.rsc }}"

# --- 5. Schedule phone-home every 2 minutes ---
/system/scheduler/add name=sabiwifi-phonehome interval=2m start-time=startup on-event="/system/script/run sabiwifi-phonehome"

:log info ("SabiWiFi: phone-home installed for " . $mySerial)

# Note: Stage 1 (sabiwifi-setup) is NOT removed here because this script
# is /import-ed BY Stage 1. Removing the calling script causes "interrupted".
# Instead, Stage 1 checks if sabiwifi-phonehome exists and skips silently.
"""


def generate_bootstrap_rsc(serial_number, platform_domain):
    """
    Generate a bootstrap .rsc script for a specific router (legacy, per-serial).
    """
    return BOOTSTRAP_TEMPLATE.format(
        serial_number=serial_number,
        platform_domain=platform_domain,
    )


def generate_generic_bootstrap_rsc(platform_domain):
    """
    Generate a generic bootstrap .rsc (Stage 1).
    All single-line commands — works in Netinstall, terminal paste, and /import.
    """
    return GENERIC_BOOTSTRAP_TEMPLATE.format(
        platform_domain=platform_domain,
    )


def generate_phonehome_setup_rsc(platform_domain):
    """
    Generate the phone-home setup .rsc (Stage 2).
    This file is fetched by the Stage 1 setup script and /import-ed.
    It reads the hardware serial, sets identity, and creates the phone-home
    script + scheduler.
    """
    return PHONEHOME_SETUP_TEMPLATE.format(
        platform_domain=platform_domain,
    )
