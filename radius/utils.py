"""
RADIUS utility functions:
- Sync service plans to FreeRADIUS group attributes
- CoA sender (Change of Authorization) via pyrad
- Helper functions for radacct queries
"""
import logging
import subprocess

from radius.models import Radgroupreply, Radgroupcheck, Radusergroup, Radcheck, Nas

logger = logging.getLogger(__name__)


def upsert_nas_and_reload(nasname, shortname, secret, description=''):
    """
    Create or update a NAS entry in the FreeRADIUS nas table, then reload
    FreeRADIUS so it picks up the change (it reads SQL clients at startup only).
    """
    Nas.objects.update_or_create(
        nasname=nasname,
        defaults={
            'shortname': shortname,
            'type': 'other',
            'secret': secret,
            'description': description,
        }
    )
    _reload_freeradius()


def _reload_freeradius():
    """Restart FreeRADIUS so it re-reads clients from SQL.

    FreeRADIUS 3 only loads SQL clients at startup — HUP/reload
    does not re-read the NAS table, so a full restart is required.
    """
    try:
        subprocess.run(
            ['systemctl', 'restart', 'freeradius'],
            capture_output=True, timeout=10,
        )
        logger.info('FreeRADIUS restarted after NAS change')
    except Exception as e:
        logger.warning(f'Failed to restart FreeRADIUS: {e}')


def sync_plan_to_radius(plan):
    """
    Write RADIUS group reply/check attributes for a ServicePlan.
    Called via post_save signal on ServicePlan.
    Group name format: {reseller_slug}-{plan_slug}
    """
    group_name = plan.radius_group_name

    # Clear existing attributes for this group
    Radgroupreply.objects.filter(groupname=group_name).delete()
    Radgroupcheck.objects.filter(groupname=group_name).delete()

    if not plan.is_active:
        logger.info(f"Plan {group_name} is inactive — RADIUS attributes cleared.")
        return

    # --- Group Reply Attributes ---

    # Mikrotik-Rate-Limit: upload/download speed (MikroTik routers)
    Radgroupreply.objects.create(
        groupname=group_name,
        attribute='Mikrotik-Rate-Limit',
        op=':=',
        value=plan.mikrotik_rate_limit,
    )

    # WISPr-Bandwidth-Max-Down/Up: speed limits in bps (OpenWrt/uspot)
    Radgroupreply.objects.create(
        groupname=group_name,
        attribute='WISPr-Bandwidth-Max-Down',
        op=':=',
        value=str(plan.download_mbps * 1_000_000),
    )
    Radgroupreply.objects.create(
        groupname=group_name,
        attribute='WISPr-Bandwidth-Max-Up',
        op=':=',
        value=str(plan.upload_mbps * 1_000_000),
    )

    # Session-Timeout (if time-limited plan)
    timeout = plan.session_timeout_seconds
    if timeout > 0:
        Radgroupreply.objects.create(
            groupname=group_name,
            attribute='Session-Timeout',
            op=':=',
            value=str(timeout),
        )

    # Idle-Timeout: disconnect after 10 minutes of no traffic
    Radgroupreply.objects.create(
        groupname=group_name,
        attribute='Idle-Timeout',
        op=':=',
        value='600',
    )

    # Acct-Interim-Interval (5 minutes for usage tracking)
    Radgroupreply.objects.create(
        groupname=group_name,
        attribute='Acct-Interim-Interval',
        op=':=',
        value='300',
    )

    # Mikrotik-Total-Limit (data cap in bytes, if set)
    if plan.data_cap_gb is not None:
        total_bytes = int(float(plan.data_cap_gb) * 1024 * 1024 * 1024)
        Radgroupreply.objects.create(
            groupname=group_name,
            attribute='Mikrotik-Total-Limit',
            op=':=',
            value=str(total_bytes),
        )

    # Mikrotik-Recv-Limit (download-only cap in bytes)
    if plan.download_cap_gb is not None:
        recv_bytes = int(float(plan.download_cap_gb) * 1024 * 1024 * 1024)
        Radgroupreply.objects.create(
            groupname=group_name,
            attribute='Mikrotik-Recv-Limit',
            op=':=',
            value=str(recv_bytes),
        )

    # Mikrotik-Xmit-Limit (upload-only cap in bytes)
    if plan.upload_cap_gb is not None:
        xmit_bytes = int(float(plan.upload_cap_gb) * 1024 * 1024 * 1024)
        Radgroupreply.objects.create(
            groupname=group_name,
            attribute='Mikrotik-Xmit-Limit',
            op=':=',
            value=str(xmit_bytes),
        )

    # Mikrotik-Address-Pool (IP pool assignment)
    if plan.ip_pool_name:
        Radgroupreply.objects.create(
            groupname=group_name,
            attribute='Mikrotik-Address-Pool',
            op=':=',
            value=plan.ip_pool_name,
        )

    # --- Group Check Attributes ---

    # Simultaneous-Use: max devices
    Radgroupcheck.objects.create(
        groupname=group_name,
        attribute='Simultaneous-Use',
        op=':=',
        value=str(plan.max_devices),
    )

    logger.info(f"RADIUS attributes synced for plan {group_name}")


def assign_subscriber_to_plan(subscriber, plan):
    """
    Assign a subscriber to a RADIUS group (plan).
    Removes any existing group assignment first.
    """
    username = subscriber.phone
    group_name = plan.radius_group_name

    # Remove existing group assignments for this user
    Radusergroup.objects.filter(username=username).delete()

    # Assign to new group
    Radusergroup.objects.create(
        username=username,
        groupname=group_name,
        priority=1,
    )

    # Ensure radcheck has the auth token for this user
    Radcheck.objects.filter(username=username, attribute='Cleartext-Password').delete()
    if subscriber.auth_token:
        Radcheck.objects.create(
            username=username,
            attribute='Cleartext-Password',
            op=':=',
            value=subscriber.auth_token,
        )

    logger.info(f"Subscriber {username} assigned to RADIUS group {group_name}")


def update_radcheck_password(subscriber):
    """Write/update only the Cleartext-Password in radcheck (no group change)."""
    username = subscriber.phone
    Radcheck.objects.filter(username=username, attribute='Cleartext-Password').delete()
    if subscriber.auth_token:
        Radcheck.objects.create(
            username=username,
            attribute='Cleartext-Password',
            op=':=',
            value=subscriber.auth_token,
        )
    logger.info(f"Updated radcheck password for {username}")


def disconnect_subscriber_sessions(subscriber):
    """
    Disconnect all open sessions for a subscriber.

    - MikroTik: RADIUS Disconnect-Message (DM, RFC 5176) on port 3799
    - OpenWrt: SSH + ubus call to uspot das_disconnect

    Called at login time so Simultaneous-Use check doesn't reject
    a new connection because of a stale session on another device.
    Also called from dashboard "Disconnect" button.
    Errors are logged but never raised — a failure must not block login.
    """
    from radius.models import Radacct
    from routers.models import Router

    handled_ips = set()

    # 1. Disconnect via radacct open sessions (works for both router types)
    open_sessions = list(
        Radacct.objects.filter(
            username=subscriber.phone,
            acctstoptime__isnull=True,
        ).values('nasipaddress', 'acctsessionid')
    )

    for session in open_sessions:
        nas_ip = str(session['nasipaddress'])
        handled_ips.add(nas_ip)
        try:
            router = Router.objects.get(wg_tunnel_ip=nas_ip)
        except Router.DoesNotExist:
            logger.warning(f'CoA DM: no router found for NAS IP {nas_ip}')
            continue

        if router.device_type == 'openwrt':
            _openwrt_disconnect(
                username=subscriber.phone,
                nas_ip=nas_ip,
            )
        else:
            _coa_disconnect(
                username=subscriber.phone,
                session_id=session.get('acctsessionid', ''),
                nas_ip=nas_ip,
                secret=router.nas_secret,
            )

    # 2. For OpenWrt routers: also try direct disconnect via SSH even without
    #    a radacct entry, since uspot tracks sessions in memory, not always
    #    reflected in radacct (e.g. accounting hasn't started yet).
    if subscriber.reseller_id:
        openwrt_routers = Router.objects.filter(
            reseller_id=subscriber.reseller_id,
            device_type='openwrt',
            status='online',
        ).exclude(wg_tunnel_ip__in=handled_ips)
        for router in openwrt_routers:
            if router.wg_tunnel_ip:
                _openwrt_disconnect(
                    username=subscriber.phone,
                    nas_ip=str(router.wg_tunnel_ip),
                )


def _coa_disconnect(username, session_id, nas_ip, secret):
    """
    Build and send a raw RADIUS Disconnect-Message (code 40) over UDP.
    Uses only stdlib — no extra dependency beyond what is already installed.
    """
    import hashlib
    import socket
    import struct
    import os

    secret_bytes = secret.encode('utf-8') if isinstance(secret, str) else secret
    identifier = int.from_bytes(os.urandom(1), 'big')

    def _attr(type_code, value_bytes):
        return bytes([type_code, len(value_bytes) + 2]) + value_bytes

    attrs = _attr(1, username.encode('utf-8'))           # User-Name (type 1)
    if session_id:
        attrs += _attr(44, session_id.encode('utf-8'))   # Acct-Session-Id (type 44)

    length = 20 + len(attrs)   # 20-byte RADIUS header + attributes

    # Authenticator = MD5(Code || ID || Length || 0×16 || Attrs || Secret)
    pre_auth = (
        bytes([40, identifier]) + struct.pack('!H', length)
        + b'\x00' * 16 + attrs + secret_bytes
    )
    authenticator = hashlib.md5(pre_auth).digest()

    packet = bytes([40, identifier]) + struct.pack('!H', length) + authenticator + attrs

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(3)
        sock.sendto(packet, (nas_ip, 3799))
        resp, _ = sock.recvfrom(4096)
        code = resp[0] if resp else 0
        if code == 41:
            logger.info(f'CoA DM ACK: {username} session evicted from {nas_ip}')
        else:
            logger.warning(f'CoA DM NAK (code={code}) from {nas_ip} for {username}')
    except socket.timeout:
        logger.warning(f'CoA DM timeout from {nas_ip} for {username}')
    except OSError as exc:
        logger.warning(f'CoA DM error for {username} on {nas_ip}: {exc}')
    finally:
        sock.close()


def _openwrt_disconnect(username, nas_ip):
    """
    Disconnect a user from an OpenWrt/uspot router via SSH + ubus.
    uspot doesn't listen for RADIUS DAS packets; it uses ubus instead.
    """
    import json
    import shlex

    ubus_args = json.dumps({
        'uspot': 'captive',
        'request': {'User-Name': username},
    })
    cmd = [
        'ssh', '-o', 'StrictHostKeyChecking=no',
        '-o', 'ConnectTimeout=5',
        f'root@{nas_ip}',
        f'ubus call uspot das_disconnect {shlex.quote(ubus_args)}',
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
        if result.returncode == 0 and 'true' in result.stdout:
            logger.info(f'OpenWrt disconnect: {username} evicted from {nas_ip}')
        else:
            logger.warning(
                f'OpenWrt disconnect: {username} on {nas_ip} — '
                f'rc={result.returncode} out={result.stdout.strip()}'
            )
    except Exception as e:
        logger.warning(f'OpenWrt disconnect error for {username} on {nas_ip}: {e}')


def remove_subscriber_from_radius(subscriber):
    """Remove a subscriber from all RADIUS groups and auth."""
    username = subscriber.phone
    Radusergroup.objects.filter(username=username).delete()
    Radcheck.objects.filter(username=username).delete()
    logger.info(f"Subscriber {username} removed from RADIUS")


def create_default_trial_plan(reseller):
    """
    Auto-create a default Free Trial plan for a reseller.
    Called when their first router comes online.
    """
    from plans.models import ServicePlan

    # Check if reseller already has a system-created plan
    if ServicePlan.objects.filter(reseller=reseller, is_system_created=True).exists():
        return None

    plan = ServicePlan.objects.create(
        reseller=reseller,
        name='Free Trial',
        slug='free-trial',
        download_mbps=2,
        upload_mbps=2,
        duration_days=0,
        duration_hours=0.50,  # 30 minutes
        data_cap_gb=0.1,  # 100 MB
        max_devices=1,
        price_ngn=0,
        is_trial=True,
        is_system_created=True,
        is_active=True,
    )
    logger.info(f"Auto-created Free Trial plan for reseller {reseller.name}")
    return plan
