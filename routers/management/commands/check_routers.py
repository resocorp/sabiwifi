"""
Management command: check_routers

Runs every 2 minutes via systemd timer. Determines router health by two signals:
  1. ICMP ping to WireGuard tunnel IP (hard connectivity check)
  2. Heartbeat staleness — if last_seen is older than HEARTBEAT_STALE_SECONDS
     and the router was previously online, treat it as offline.

When status changes:
  - online/provisioned → offline: set offline_since, write RouterHealthLog
  - offline/provisioned → online: clear offline_since, write RouterHealthLog

Routers in 'available' or 'pending_provision' are skipped.
"""
import logging
import subprocess

from django.core.management.base import BaseCommand
from django.utils import timezone

from routers.models import Router, RouterHealthLog

logger = logging.getLogger(__name__)

# If last_seen is older than this, treat the router as offline (missed heartbeats)
HEARTBEAT_STALE_SECONDS = 600  # 10 minutes


class Command(BaseCommand):
    help = 'Check router health and update online/offline status.'

    def handle(self, *args, **options):
        now = timezone.now()

        routers = list(Router.objects.filter(
            reseller__isnull=False,
            status__in=['online', 'offline', 'provisioned'],
            wg_tunnel_ip__isnull=False,
        ))

        online_count = 0
        offline_count = 0
        log_entries = []

        for router in routers:
            old_status = router.status
            reachable = self._ping(str(router.wg_tunnel_ip))

            # A router that has stopped sending heartbeats is unhealthy even
            # if the WireGuard tunnel is still up (e.g. hotspot process crashed).
            heartbeat_stale = (
                router.last_seen is not None
                and (now - router.last_seen).total_seconds() > HEARTBEAT_STALE_SECONDS
                and old_status == 'online'
            )

            if reachable and not heartbeat_stale:
                router.status = 'online'
                router.last_seen = now
                router.offline_since = None
                online_count += 1
            else:
                router.status = 'offline'
                if not router.offline_since:
                    router.offline_since = now
                offline_count += 1

            router.save(update_fields=['status', 'last_seen', 'offline_since'])

            if router.status != old_status:
                log_entries.append(RouterHealthLog(router=router, event=router.status))
                reason = 'heartbeat stale' if heartbeat_stale else ('ping OK' if reachable else 'no ping')
                logger.info(
                    f'Router {router.serial_number}: {old_status} → {router.status} ({reason})'
                )

        if log_entries:
            RouterHealthLog.objects.bulk_create(log_entries)

        # Send notifications for status changes
        for router, old, new in [(e.router, None, e.event) for e in log_entries]:
            try:
                from notifications.notify import notify_reseller, notify_admin_contacts
                ctx = {
                    'location': router.location_name or router.serial_number,
                    'serial': router.serial_number,
                    'offline_duration': router.offline_duration_display,
                }
                event = 'router_offline' if new == 'offline' else 'router_recovered'
                if router.reseller:
                    notify_reseller(router.reseller, event, ctx)
                    notify_admin_contacts(router.reseller, event, ctx)
            except Exception as exc:
                logger.warning(f'Router notify failed for {router.serial_number}: {exc}')

        msg = (
            f'Router check: {online_count} online, {offline_count} offline, '
            f'{len(log_entries)} status change(s).'
        )
        self.stdout.write(msg)
        logger.info(msg)

    def _ping(self, ip):
        """Single ICMP ping. Returns True if reachable within 2 seconds."""
        try:
            result = subprocess.run(
                ['ping', '-c', '1', '-W', '2', ip],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False
