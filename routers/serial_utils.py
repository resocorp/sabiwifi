"""Serial number validation for MikroTik routers."""
import re

# MikroTik routerboard serials are alphanumeric, typically 10-14 characters.
# Examples: 94DB07634317, HM20B2HFCY2, HFG30A9B72C, D4CA6D123456
# Must be at least 8 chars, all uppercase alphanumeric, no special characters.
MIKROTIK_SERIAL_RE = re.compile(r'^[A-Z0-9]{8,16}$')


def is_valid_mikrotik_serial(serial: str) -> bool:
    """Check if a serial looks like a valid MikroTik routerboard serial."""
    return bool(MIKROTIK_SERIAL_RE.match(serial.strip().upper()))
