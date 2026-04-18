"""
WireGuard utility functions for managing server-side peers.

The Router DB table is the source of truth for peers. Runtime operations
(add_peer, remove_peer) call `wg set` for immediate effect and then call
`reconcile_wg()` to regenerate /etc/wireguard/wg0.conf from the DB so the
on-disk config never drifts.

Do NOT use `wg-quick save` — it snapshots whatever is currently in the
kernel (including orphans and manually-added peers) and we've observed
it accumulating dead entries over time.

Deployment requirement: /etc/sudoers.d/sabiwifi-wg containing:
    www-data ALL=(root) NOPASSWD: /usr/bin/wg, /usr/bin/wg-quick, /usr/bin/tee, /bin/mv
"""
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone as dt_timezone

logger = logging.getLogger(__name__)

WG_CONF_PATH = '/etc/wireguard/wg0.conf'
WG_INTERFACE = 'wg0'


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


def _read_interface_section():
    """
    Read the [Interface] block from the existing wg0.conf via `sudo cat`
    (the file is root:root 600 because it holds the server's WG private
    key). Returns the block text or None if unreadable.
    """
    try:
        result = subprocess.run(
            ['sudo', '/bin/cat', WG_CONF_PATH],
            capture_output=True, text=True, check=True, timeout=10,
        )
        content = result.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.error(f"Cannot read {WG_CONF_PATH}: {e}")
        return None

    # [Interface] runs from the header until the first [Peer] (or EOF)
    m = re.search(r'\[Interface\].*?(?=\n\[Peer\]|\Z)', content, re.DOTALL)
    if not m:
        return None
    return m.group(0).rstrip() + '\n'


def reconcile_wg(interface=WG_INTERFACE):
    """
    Regenerate /etc/wireguard/wg0.conf from the Router DB and push the
    resulting peer set into the running kernel via `wg syncconf`.

    Idempotent: any peer not in the DB is pruned from both the file and
    the runtime. This is how orphans introduced by earlier `wg-quick save`
    calls are cleaned up.
    """
    # Lazy import to avoid a circular import at module load time
    from django.apps import apps
    Router = apps.get_model('routers', 'Router')

    iface_block = _read_interface_section()
    if iface_block is None:
        raise WireGuardError(
            f"Cannot reconcile: {WG_CONF_PATH} missing or has no [Interface] section"
        )

    peer_blocks = []
    for r in Router.objects.exclude(wg_public_key='').exclude(wg_tunnel_ip__isnull=True):
        peer_blocks.append(
            f"[Peer]\n"
            f"# {r.serial_number}\n"
            f"PublicKey = {r.wg_public_key}\n"
            f"AllowedIPs = {r.wg_tunnel_ip}/32\n"
            f"PersistentKeepalive = 25\n"
        )

    new_config = iface_block + '\n' + '\n'.join(peer_blocks)

    # Atomic write via tempfile + sudo mv (www-data can't write /etc/wireguard directly)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix='wg0.', suffix='.conf', dir='/tmp')
        with os.fdopen(fd, 'w') as f:
            f.write(new_config)
        os.chmod(tmp_path, 0o600)

        subprocess.run(
            ['sudo', '/bin/mv', tmp_path, WG_CONF_PATH],
            capture_output=True, text=True, check=True, timeout=10,
        )
        tmp_path = None  # moved successfully

        # Apply the new peer set to the running interface without tearing
        # down the tunnel. `wg syncconf` accepts only a "stripped" config
        # (no [Interface] ListenPort/PrivateKey), so we pipe through
        # `wg-quick strip`.
        strip = subprocess.run(
            ['sudo', 'wg-quick', 'strip', interface],
            capture_output=True, text=True, check=True, timeout=10,
        )
        subprocess.run(
            ['sudo', 'wg', 'syncconf', interface, '/dev/stdin'],
            input=strip.stdout,
            capture_output=True, text=True, check=True, timeout=10,
        )

        logger.info(f"Reconciled WireGuard: {len(peer_blocks)} peer(s) written to {WG_CONF_PATH}")
        return len(peer_blocks)

    except subprocess.CalledProcessError as e:
        raise WireGuardError(f"reconcile_wg failed: {e.stderr}") from e
    except subprocess.TimeoutExpired as e:
        raise WireGuardError("reconcile_wg timed out") from e
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def add_peer(public_key, allowed_ip, interface=WG_INTERFACE):
    """
    Add a WireGuard peer (runtime + persist via reconcile).
    Assumes the caller has already saved the corresponding Router row,
    so reconcile_wg() sees it when regenerating the config file.
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

    try:
        reconcile_wg(interface)
    except WireGuardError as e:
        logger.error(f"add_peer succeeded but reconcile failed: {e}")


def remove_peer(public_key, interface=WG_INTERFACE):
    """
    Remove a WireGuard peer (runtime + persist via reconcile).
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

    try:
        reconcile_wg(interface)
    except WireGuardError as e:
        logger.error(f"remove_peer succeeded but reconcile failed: {e}")


def list_peers(interface=WG_INTERFACE):
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


def get_handshakes(interface=WG_INTERFACE):
    """
    Return a dict mapping public_key -> last-handshake datetime (tz-aware UTC)
    or None for peers that have never handshaked. Used by check_routers to
    detect a stale tunnel even when the device still has internet.
    """
    try:
        peers = list_peers(interface)
    except WireGuardError as e:
        logger.error(f"get_handshakes failed: {e}")
        return {}

    out = {}
    for p in peers:
        ts = p.get('latest_handshake', '0')
        try:
            epoch = int(ts)
        except (TypeError, ValueError):
            epoch = 0
        out[p['public_key']] = (
            datetime.fromtimestamp(epoch, tz=dt_timezone.utc) if epoch > 0 else None
        )
    return out
