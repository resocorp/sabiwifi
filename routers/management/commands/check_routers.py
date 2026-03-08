"""
Management command: check_routers
Runs every 5 minutes via cron. Pings routers via WireGuard tunnel,
updates status to online/offline, tracks last_seen.
"""
import logging
import subprocess
from django.core.management.base import BaseCommand
from django.utils import timezone
from routers.models import Router

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Check router health by pinging WireGuard tunnel IPs.'

    def handle(self, *args, **options):
        routers = Router.objects.filter(
            reseller__isnull=False,
            status__in=['online', 'offline', 'provisioned'],
            wg_tunnel_ip__isnull=False,
        )

        online_count = 0
        offline_count = 0
        changed = []

        for router in routers:
            reachable = self._ping(router.wg_tunnel_ip)
            old_status = router.status

            if reachable:
                router.status = 'online'
                router.last_seen = timezone.now()
                online_count += 1
            else:
                if old_status == 'online':
                    router.status = 'offline'
                offline_count += 1

            if router.status != old_status:
                changed.append((router.serial_number, old_status, router.status))

            router.save(update_fields=['status', 'last_seen'])

        for serial, old, new in changed:
            logger.info(f"Router {serial}: {old} → {new}")

        self.stdout.write(
            f"Router check: {online_count} online, {offline_count} offline, "
            f"{len(changed)} changed."
        )

    def _ping(self, ip):
        """Ping an IP address. Returns True if reachable."""
        try:
            result = subprocess.run(
                ['ping', '-n', '1', '-w', '2000', str(ip)],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, Exception):
            return False
