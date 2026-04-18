"""
Management command: check_routers

Runs every 2 minutes via systemd timer. Determines router health by two
independent signals:

  1. Heartbeat freshness (`last_seen`) — the router has internet and is
     reaching the platform's public HTTPS endpoint.
  2. WireGuard handshake freshness (`wg_last_handshake`) — the tunnel
     itself is alive.

A router can have internet without a tunnel (the failure mode that left
our earlier device unrecoverable). When we see a heartbeat but no
handshake for `HANDSHAKE_STALE_SECONDS`, we flag `needs_reprovision` so
the next heartbeat response pushes the provision script back to the
device.

Status transitions:
  online    — heartbeat fresh AND (handshake fresh OR tunnel not yet
              expected, e.g. router is still pending_provision)
  offline   — heartbeat stale (no internet) OR handshake stale while
              heartbeat is also stale (device is gone)
  pending_provision — unchanged by this command; cleared only when the
              heartbeat view actually delivers a provision script.
"""
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from routers.models import Router, RouterHealthLog
from routers.wg_utils import get_handshakes

logger = logging.getLogger(__name__)

# If no heartbeat within this window the device is considered offline.
HEARTBEAT_STALE_SECONDS = 600          # 10 minutes

# If heartbeat is fresh but no handshake within this window, the tunnel
# is broken even though the device is otherwise reachable — push a
# re-provision.
HANDSHAKE_STALE_SECONDS = 300          # 5 minutes


class Command(BaseCommand):
    help = 'Check router health by heartbeat and WireGuard handshake.'

    def handle(self, *args, **options):
        now = timezone.now()

        routers = list(Router.objects.filter(
            reseller__isnull=False,
            status__in=['online', 'offline', 'provisioned', 'pending_provision'],
            wg_tunnel_ip__isnull=False,
        ))

        handshakes = get_handshakes()

        online_count = 0
        offline_count = 0
        reprovision_flagged = 0
        log_entries = []

        for router in routers:
            old_status = router.status
            update_fields = ['status', 'last_seen', 'offline_since',
                             'wg_last_handshake', 'needs_reprovision']

            # Handshake signal
            hs = handshakes.get(router.wg_public_key) if router.wg_public_key else None
            router.wg_last_handshake = hs
            handshake_fresh = (
                hs is not None
                and (now - hs).total_seconds() <= HANDSHAKE_STALE_SECONDS
            )

            # Heartbeat signal
            heartbeat_fresh = (
                router.last_seen is not None
                and (now - router.last_seen).total_seconds() <= HEARTBEAT_STALE_SECONDS
            )

            # Re-provision trigger: device is reaching us over the public
            # internet but its WG tunnel hasn't handshaked recently. Only
            # set the flag if we haven't just freshly delivered a provision
            # (the heartbeat view clears it on delivery).
            if (
                heartbeat_fresh
                and not handshake_fresh
                and old_status not in ('pending_provision',)
            ):
                if not router.needs_reprovision:
                    router.needs_reprovision = True
                    reprovision_flagged += 1
                    logger.info(
                        f'Router {router.serial_number}: flagged needs_reprovision '
                        f'(heartbeat OK but no WG handshake)'
                    )
            elif handshake_fresh and router.needs_reprovision:
                # Tunnel recovered — clear the flag
                router.needs_reprovision = False

            # Status calculation
            if old_status == 'pending_provision':
                # Do not overwrite — waiting for provision delivery
                new_status = 'pending_provision'
            elif heartbeat_fresh and handshake_fresh:
                new_status = 'online'
            elif heartbeat_fresh and not handshake_fresh:
                # Internet reachable but tunnel down. We still report
                # offline so operators see it, but needs_reprovision is
                # set above to recover automatically.
                new_status = 'offline'
            else:
                new_status = 'offline'

            router.status = new_status
            if new_status == 'online':
                router.offline_since = None
                online_count += 1
            else:
                if not router.offline_since and old_status == 'online':
                    router.offline_since = now
                if new_status == 'offline':
                    offline_count += 1

            router.save(update_fields=update_fields)

            if router.status != old_status and router.status in ('online', 'offline'):
                log_entries.append(RouterHealthLog(router=router, event=router.status))
                reason = (
                    f'hb={"fresh" if heartbeat_fresh else "stale"} '
                    f'hs={"fresh" if handshake_fresh else "stale"}'
                )
                logger.info(
                    f'Router {router.serial_number}: {old_status} → {router.status} ({reason})'
                )

        if log_entries:
            RouterHealthLog.objects.bulk_create(log_entries)

        for entry in log_entries:
            r = entry.router
            try:
                from notifications.notify import notify_reseller, notify_admin_contacts
                ctx = {
                    'location': r.location_name or r.serial_number,
                    'serial': r.serial_number,
                    'offline_duration': r.offline_duration_display,
                }
                event = 'router_offline' if entry.event == 'offline' else 'router_recovered'
                if r.reseller:
                    notify_reseller(r.reseller, event, ctx)
                    notify_admin_contacts(r.reseller, event, ctx)
            except Exception as exc:
                logger.warning(f'Router notify failed for {r.serial_number}: {exc}')

        msg = (
            f'Router check: {online_count} online, {offline_count} offline, '
            f'{len(log_entries)} status change(s), '
            f'{reprovision_flagged} flagged for re-provision.'
        )
        self.stdout.write(msg)
        logger.info(msg)
