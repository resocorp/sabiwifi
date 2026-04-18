"""
Management command: reconcile_wg

Rewrites /etc/wireguard/wg0.conf from the Router DB table and pushes the
resulting peer set into the running kernel. Removes any peer that isn't
backed by a Router row (orphans accumulated from prior `wg-quick save`).

Run on deploy, on boot (systemd), and periodically if desired.
"""
from django.core.management.base import BaseCommand
from routers.wg_utils import reconcile_wg, WireGuardError


class Command(BaseCommand):
    help = 'Regenerate /etc/wireguard/wg0.conf from the Router table.'

    def handle(self, *args, **options):
        try:
            n = reconcile_wg()
        except WireGuardError as e:
            self.stderr.write(self.style.ERROR(f'Reconcile failed: {e}'))
            return
        self.stdout.write(self.style.SUCCESS(f'Reconciled {n} peer(s).'))
