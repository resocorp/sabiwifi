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

Status transitions use hysteresis: a router must be seen as unhealthy on
OFFLINE_STRIKES_REQUIRED consecutive checks before status flips to
'offline'. Recovery flips immediately on the first healthy observation.
This suppresses flaps caused by a single late heartbeat.

Notifications are deferred: the SMS/WA for an offline event fires only
once the router has been offline for CONFIRM_SECONDS AND it has not
recovered in the meantime. Recoveries only notify if the corresponding
offline notification was sent. Brief outages therefore produce zero
messages (and zero user-visible log rows, since the offline row is
written at the strike threshold, not at first failure).
"""
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from routers.models import Router, RouterHealthLog
from routers.wg_utils import get_handshakes

logger = logging.getLogger(__name__)

# If no heartbeat within this window the device is considered unhealthy.
HEARTBEAT_STALE_SECONDS = 600          # 10 minutes

# If heartbeat is fresh but no handshake within this window, the tunnel
# is broken even though the device is otherwise reachable — push a
# re-provision.
HANDSHAKE_STALE_SECONDS = 300          # 5 minutes

# Consecutive unhealthy checks required to flip status to 'offline'.
# At 2-minute check cadence this requires ~2-4 min of sustained badness
# beyond the heartbeat window before the transition is recorded.
OFFLINE_STRIKES_REQUIRED = 2

# Wait this long after an offline log is written before sending the
# notification. If the router recovers in the window, both the offline
# and recovery events are marked notified-without-sending.
CONFIRM_SECONDS = 300                  # 5 minutes

# Safety guard: never retroactively send notifications for events older
# than this. Anything older is marked notified_at=now without sending
# (e.g., pre-existing rows on first deploy of the notified_at field, or
# a backlog left after an outage of the check_routers timer itself).
MAX_NOTIFY_AGE_SECONDS = 3600          # 1 hour


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
        new_logs = []

        for router in routers:
            old_status = router.status
            update_fields = ['status', 'last_seen', 'offline_since',
                             'wg_last_handshake', 'needs_reprovision',
                             'offline_strikes']

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

            is_healthy = heartbeat_fresh and handshake_fresh

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
            # Don't auto-clear the flag here. The heartbeat view clears it
            # on successful delivery. Auto-clearing would clobber manually
            # queued re-provisions (redeploy, service-mode change, wifi
            # config) that land between check_routers ticks. Re-provision
            # is idempotent, so delivering one when WG is already healthy
            # is harmless.

            # Status calculation with hysteresis
            if old_status == 'pending_provision':
                # Do not overwrite — waiting for provision delivery
                new_status = 'pending_provision'
            elif is_healthy:
                router.offline_strikes = 0
                new_status = 'online'
            else:
                router.offline_strikes = min(router.offline_strikes + 1, 255)
                if old_status == 'offline':
                    new_status = 'offline'
                elif router.offline_strikes >= OFFLINE_STRIKES_REQUIRED:
                    new_status = 'offline'
                else:
                    # Below strike threshold — keep prior status, no log row.
                    new_status = old_status

            router.status = new_status
            if new_status == 'online':
                router.offline_since = None
                online_count += 1
            else:
                if not router.offline_since and old_status == 'online' and new_status == 'offline':
                    router.offline_since = now
                if new_status == 'offline':
                    offline_count += 1

            router.save(update_fields=update_fields)

            if new_status != old_status and new_status in ('online', 'offline'):
                entry = RouterHealthLog(router=router, event=new_status)
                new_logs.append(entry)
                reason = (
                    f'hb={"fresh" if heartbeat_fresh else "stale"} '
                    f'hs={"fresh" if handshake_fresh else "stale"} '
                    f'strikes={router.offline_strikes}'
                )
                logger.info(
                    f'Router {router.serial_number}: {old_status} → {new_status} ({reason})'
                )

        if new_logs:
            RouterHealthLog.objects.bulk_create(new_logs)

        suppressed, sent = self._dispatch_pending_notifications(now)

        msg = (
            f'Router check: {online_count} online, {offline_count} offline, '
            f'{len(new_logs)} status change(s), '
            f'{reprovision_flagged} flagged for re-provision, '
            f'{sent} notification(s) sent, {suppressed} suppressed.'
        )
        self.stdout.write(msg)
        logger.info(msg)

    def _dispatch_pending_notifications(self, now):
        """
        Walk all RouterHealthLog rows with notified_at IS NULL and decide
        what to do with each. Returns (suppressed_count, sent_count).

        We walk chronologically. An offline row with a newer online row
        is a blip — we mark both notified-without-sending in one pass.
        An offline row whose recovery hasn't happened yet is announced
        only once it's at least CONFIRM_SECONDS old. Online rows that
        survive to their own iteration are "announced recoveries": they
        pair with an already-announced offline, so we notify.
        """
        from notifications.notify import notify_reseller, notify_admin_contacts

        pending = list(
            RouterHealthLog.objects
            .filter(notified_at__isnull=True)
            .select_related('router', 'router__reseller')
            .order_by('created_at', 'id')
        )
        # Fast lookup of rows already processed this run.
        handled_ids = set()

        suppressed = 0
        sent = 0

        for entry in pending:
            if entry.id in handled_ids:
                continue

            router = entry.router

            # Unassigned router: nothing to notify. Mark quietly.
            if router.reseller_id is None:
                entry.notified_at = now
                entry.save(update_fields=['notified_at'])
                handled_ids.add(entry.id)
                suppressed += 1
                continue

            # Stale event (older than MAX_NOTIFY_AGE_SECONDS): never
            # retroactively fire. Mark notified without sending.
            age = (now - entry.created_at).total_seconds()
            if age > MAX_NOTIFY_AGE_SECONDS:
                entry.notified_at = now
                entry.save(update_fields=['notified_at'])
                handled_ids.add(entry.id)
                suppressed += 1
                continue

            ctx = {
                'location': router.location_name or router.serial_number,
                'serial': router.serial_number,
                'offline_duration': router.offline_duration_display,
            }

            if entry.event == 'offline':
                recovery = RouterHealthLog.objects.filter(
                    router=router,
                    event='online',
                    created_at__gt=entry.created_at,
                ).order_by('created_at').first()

                if recovery is not None:
                    # Blip: offline recovered before we confirmed it.
                    # Suppress both halves.
                    entry.notified_at = now
                    entry.save(update_fields=['notified_at'])
                    if recovery.notified_at is None:
                        recovery.notified_at = now
                        recovery.save(update_fields=['notified_at'])
                    handled_ids.add(entry.id)
                    handled_ids.add(recovery.id)
                    suppressed += 2
                    logger.info(
                        f'Router {router.serial_number}: suppressed blip '
                        f'{entry.created_at:%H:%M} → {recovery.created_at:%H:%M}'
                    )
                    continue

                if age < CONFIRM_SECONDS:
                    # Not yet confirmed; re-evaluate next run.
                    continue
                if router.status != 'offline':
                    # Safety net: router recovered but no online log yet.
                    entry.notified_at = now
                    entry.save(update_fields=['notified_at'])
                    handled_ids.add(entry.id)
                    suppressed += 1
                    continue

                # Confirmed offline: notify.
                try:
                    if router.reseller:
                        notify_reseller(router.reseller, 'router_offline', ctx)
                        notify_admin_contacts(router.reseller, 'router_offline', ctx)
                    entry.notified_at = now
                    entry.save(update_fields=['notified_at'])
                    handled_ids.add(entry.id)
                    sent += 1
                except Exception as exc:
                    logger.warning(
                        f'Router notify (offline) failed for {router.serial_number}: {exc}'
                    )

            else:  # entry.event == 'online'
                # Any online row surviving to this branch was NOT paired
                # off as a blip above — its preceding offline was already
                # confirmed and announced, so this is a real recovery.
                try:
                    if router.reseller:
                        notify_reseller(router.reseller, 'router_recovered', ctx)
                        notify_admin_contacts(router.reseller, 'router_recovered', ctx)
                    try:
                        from operator_panel.services.notify_operator import notify as notify_operator
                        notify_operator('router_recovered', {
                            'serial': router.serial_number,
                            'partner': router.reseller.name if router.reseller else 'unassigned',
                        })
                    except Exception as exc:
                        logger.warning(
                            f'notify_operator router_recovered failed: {exc}'
                        )
                    entry.notified_at = now
                    entry.save(update_fields=['notified_at'])
                    handled_ids.add(entry.id)
                    sent += 1
                except Exception as exc:
                    logger.warning(
                        f'Router notify (recovered) failed for {router.serial_number}: {exc}'
                    )

        return suppressed, sent
