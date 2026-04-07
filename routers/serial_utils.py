"""Serial number / MAC address validation for routers."""
import re

# MikroTik routerboard serials are alphanumeric, typically 10-14 characters.
# Examples: 94DB07634317, HM20B2HFCY2, HFG30A9B72C, D4CA6D123456
# Must be at least 8 chars, all uppercase alphanumeric, no special characters.
MIKROTIK_SERIAL_RE = re.compile(r'^[A-Z0-9]{8,16}$')

# OpenWrt devices use MAC address as identifier.
# Stored as 12 hex chars uppercase (no colons/hyphens).
# Examples: AABBCCDDEEFF, 00112233AABB
MAC_RE = re.compile(r'^[A-F0-9]{12}$')


def is_valid_mikrotik_serial(serial: str) -> bool:
    """Check if a serial looks like a valid MikroTik routerboard serial."""
    return bool(MIKROTIK_SERIAL_RE.match(serial.strip().upper()))


def normalize_mac(mac: str) -> str:
    """Normalize a MAC address to 12 uppercase hex chars (no separators)."""
    return re.sub(r'[:\-.]', '', mac.strip()).upper()


def is_valid_mac(mac: str) -> bool:
    """Check if a string is a valid MAC address (after normalization)."""
    return bool(MAC_RE.match(normalize_mac(mac)))
