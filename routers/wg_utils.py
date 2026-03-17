"""
WireGuard utility functions for managing server-side peers.

Peers are added both at runtime (via `wg set`) AND persisted to
/etc/wireguard/wg0.conf so they survive server reboots.

Deployment requirement: /etc/sudoers.d/sabiwifi-wg containing:
    www-data ALL=(root) NOPASSWD: /usr/bin/wg, /usr/bin/wg-quick
"""
import logging
import subprocess
import fcntl

logger = logging.getLogger(__name__)

WG_CONF_PATH = '/etc/wireguard/wg0.conf'


class WireGuardError(Exception):
    """Raised when a WireGuard operation fails."""
    pass


def generate_keypair():
    """
    Generate a real Curve25519 WireGuard keypair.
    Returns (private_key, public_key) as base64 strings.
    """
    try:
        gen_result = subprocess.run(
            ['sudo', 'wg', 'genkey'],
            capture_output=True, text=True, check=True, timeout=10,
        )
        private_key = gen_result.stdout.strip()

        pub_result = subprocess.run(
            ['sudo', 'wg', 'pubkey'],
            input=private_key,
            capture_output=True, text=True, check=True, timeout=10,
        )
        public_key = pub_result.stdout.strip()

        logger.info("Generated new WireGuard keypair")
        return private_key, public_key

    except subprocess.CalledProcessError as e:
        raise WireGuardError(f"Failed to generate WireGuard keypair: {e.stderr}") from e
    except subprocess.TimeoutExpired as e:
        raise WireGuardError("WireGuard key generation timed out") from e


def _save_peer_to_conf(public_key, allowed_ip):
    """Append a [Peer] block to wg0.conf for persistence across reboots."""
    peer_block = (
        f"\n[Peer]\n"
        f"PublicKey = {public_key}\n"
        f"AllowedIPs = {allowed_ip}/32\n"
        f"PersistentKeepalive = 25\n"
    )
    try:
        with open(WG_CONF_PATH, 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(peer_block)
            fcntl.flock(f, fcntl.LOCK_UN)
        logger.info(f"Saved peer {public_key[:8]}... to {WG_CONF_PATH}")
    except PermissionError:
        # Try via sudo tee
        try:
            subprocess.run(
                ['sudo', 'tee', '-a', WG_CONF_PATH],
                input=peer_block, capture_output=True, text=True,
                check=True, timeout=10,
            )
            logger.info(f"Saved peer {public_key[:8]}... to {WG_CONF_PATH} (via sudo)")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(f"Failed to persist peer to conf: {e}")
    except OSError as e:
        logger.error(f"Failed to write peer to {WG_CONF_PATH}: {e}")


def _remove_peer_from_conf(public_key):
    """Remove a [Peer] block from wg0.conf by public key."""
    try:
        with open(WG_CONF_PATH, 'r') as f:
            content = f.read()
    except (PermissionError, OSError):
        try:
            result = subprocess.run(
                ['sudo', 'cat', WG_CONF_PATH],
                capture_output=True, text=True, check=True, timeout=10,
            )
            content = result.stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            logger.error(f"Cannot read {WG_CONF_PATH}")
            return

    # Parse and rebuild without the matching peer
    lines = content.split('\n')
    new_lines = []
    skip = False
    for line in lines:
        if line.strip() == '[Peer]':
            # Look ahead to check if this peer matches
            skip = False
            # Buffer the [Peer] line, decide after seeing PublicKey
            new_lines.append(line)
            continue
        if line.strip().startswith('PublicKey') and public_key in line:
            # Remove this peer block — drop the [Peer] line we just added
            # and skip until next section
            new_lines.pop()  # remove the [Peer] line
            skip = True
            continue
        if skip:
            if line.strip() == '' or line.strip().startswith('['):
                skip = False
                if line.strip().startswith('['):
                    new_lines.append(line)
                continue
            continue
        new_lines.append(line)

    new_content = '\n'.join(new_lines).rstrip('\n') + '\n'

    try:
        with open(WG_CONF_PATH, 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(new_content)
            fcntl.flock(f, fcntl.LOCK_UN)
        logger.info(f"Removed peer {public_key[:8]}... from {WG_CONF_PATH}")
    except PermissionError:
        try:
            subprocess.run(
                ['sudo', 'tee', WG_CONF_PATH],
                input=new_content, capture_output=True, text=True,
                check=True, timeout=10,
            )
            logger.info(f"Removed peer {public_key[:8]}... from {WG_CONF_PATH} (via sudo)")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(f"Failed to update conf after peer removal: {e}")


def add_peer(public_key, allowed_ip, interface='wg0'):
    """
    Add a WireGuard peer to the server interface (runtime + config file).
    Sets allowed-ips to {allowed_ip}/32 with persistent-keepalive of 25s.
    """
    try:
        subprocess.run(
            [
                'sudo', 'wg', 'set', interface,
                'peer', public_key,
                'allowed-ips', f'{allowed_ip}/32',
                'persistent-keepalive', '25',
            ],
            capture_output=True, text=True, check=True, timeout=10,
        )
        logger.info(f"Added WireGuard peer {public_key[:8]}... with allowed-ips {allowed_ip}/32")

    except subprocess.CalledProcessError as e:
        raise WireGuardError(
            f"Failed to add WireGuard peer {public_key[:8]}...: {e.stderr}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise WireGuardError("WireGuard add_peer timed out") from e

    # Persist to config file so peer survives reboot
    _save_peer_to_conf(public_key, allowed_ip)


def remove_peer(public_key, interface='wg0'):
    """
    Remove a WireGuard peer from the server interface (runtime + config file).
    """
    if not public_key:
        return

    try:
        subprocess.run(
            ['sudo', 'wg', 'set', interface, 'peer', public_key, 'remove'],
            capture_output=True, text=True, check=True, timeout=10,
        )
        logger.info(f"Removed WireGuard peer {public_key[:8]}...")

    except subprocess.CalledProcessError as e:
        raise WireGuardError(
            f"Failed to remove WireGuard peer {public_key[:8]}...: {e.stderr}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise WireGuardError("WireGuard remove_peer timed out") from e

    # Remove from config file too
    _remove_peer_from_conf(public_key)


def list_peers(interface='wg0'):
    """
    List all WireGuard peers on the server interface.
    Returns a list of dicts with keys: public_key, endpoint, allowed_ips,
    latest_handshake, transfer_rx, transfer_tx, persistent_keepalive.
    """
    try:
        result = subprocess.run(
            ['sudo', 'wg', 'show', interface, 'dump'],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except subprocess.CalledProcessError as e:
        raise WireGuardError(f"Failed to list WireGuard peers: {e.stderr}") from e
    except subprocess.TimeoutExpired as e:
        raise WireGuardError("WireGuard list_peers timed out") from e

    peers = []
    lines = result.stdout.strip().split('\n')

    # First line is the interface itself, skip it
    for line in lines[1:]:
        parts = line.split('\t')
        if len(parts) >= 7:
            peers.append({
                'public_key': parts[0],
                'preshared_key': parts[1],
                'endpoint': parts[2],
                'allowed_ips': parts[3],
                'latest_handshake': parts[4],
                'transfer_rx': parts[5],
                'transfer_tx': parts[6],
                'persistent_keepalive': parts[7] if len(parts) > 7 else 'off',
            })

    return peers
