"""Serial number validation for MikroTik routers."""
import re

# MikroTik routerboard serials are typically 12-13 character hex strings
# e.g., 94DB07634317, HFG30A9B72C
MIKROTIK_SERIAL_RE = re.compile(r'^[A-F0-9]{12,13}$')


def is_valid_mikrotik_serial(serial: str) -> bool:
    """Check if a serial looks like a valid MikroTik routerboard serial."""
    return bool(MIKROTIK_SERIAL_RE.match(serial.strip().upper()))
