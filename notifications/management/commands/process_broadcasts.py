"""
Management command: process_broadcasts

Runs every minute via systemd timer.
Picks up to one 'queued' or 'sending' broadcast per reseller and sends
one batch of messages, then updates progress. Actual rate limiting is
enforced inside the Node WA service (4s between sends per session).
For SMS, we send up to 30 per run to avoid blocking the process too long.
"""
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications.models import Broadcast, SubscriberNotificationPrefs
from notifications.notify import notify_subscriber, _dispatch

logger = logging.getLogger(__name__)

SMS_BATCH_PER_RUN = 30  # max SMS sends per broadcast per run


class Command(BaseCommand):
    help = 'Process queued broadcast campaigns.'

    def handle(self, *args, **options):
        now = timezone.now()

        # Pick broadcasts in queued/sending state, one per reseller
        active = Broadcast.objects.filter(
            status__in=['queued', 'sending']
        ).select_related('reseller').order_by('created_at')

        processed = 0

        for broadcast in active:
            reseller = broadcast.reseller

            # Move to sending state
            if broadcast.status == 'queued':
                broadcast.status = 'sending'
                broadcast.started_at = now
                broadcast.save(update_fields=['status', 'started_at'])

            pref_field = {
                'alert': 'receive_alerts',
                'promo': 'receive_promos',
                'marketing': 'receive_marketing',
            }[broadcast.type]

            # Find subscribers who haven't been sent this broadcast yet
            already_sent_phones = set(
                broadcast.logs.values_list('recipient_phone', flat=True)
            )

            from accounts.models import Subscriber
            all_subs = Subscriber.objects.filter(
                reseller=reseller
            ).select_related('notification_prefs')

            batch_count = 0

            for sub in all_subs:
                if sub.phone in already_sent_phones:
                    continue

                # Check opt-in prefs
                try:
                    prefs = sub.notification_prefs
                    opted_in = getattr(prefs, pref_field, False)
                except SubscriberNotificationPrefs.DoesNotExist:
                    # No prefs: alerts default in, promos/marketing default out
                    opted_in = broadcast.type == 'alert'

                if not opted_in:
                    # Count as skipped — still "sent" from broadcast perspective
                    broadcast.sent_count += 1
                    continue

                # Determine channel
                channel = broadcast.channel
                from notifications.notify import _log, _mark_sent, _mark_failed
                from notifications.sms import get_sms_service

                body = broadcast.body
                # Render simple name substitution
                import re
                body = re.sub(r'\{\{name\}\}', sub.phone, body)

                if channel in ('whatsapp', 'both'):
                    from notifications.notify import send_whatsapp, _wa_connected
                    if _wa_connected(reseller):
                        log = _log(reseller, sub.phone, 'whatsapp', 'broadcast', body,
                                   subscriber=sub, broadcast=broadcast)
                        send_whatsapp(reseller.slug, sub.phone, body)
                        # WA updates log via webhook; don't mark sent here

                if channel in ('sms', 'both'):
                    sms_svc = get_sms_service()
                    log = _log(reseller, sub.phone, 'sms', 'broadcast', body,
                               subscriber=sub, broadcast=broadcast)
                    ok = sms_svc.send_sms(sub.phone, body)
                    _mark_sent(log) if ok else _mark_failed(log, 'SMS failed')
                    broadcast.sent_count += (1 if ok else 0)
                    broadcast.failed_count += (0 if ok else 1)
                    batch_count += 1

                if batch_count >= SMS_BATCH_PER_RUN:
                    break

            # Check if broadcast is complete
            total_done = broadcast.sent_count + broadcast.failed_count
            if total_done >= broadcast.total_recipients:
                broadcast.status = 'sent'
                broadcast.completed_at = now

            broadcast.save(update_fields=[
                'status', 'sent_count', 'failed_count', 'completed_at'
            ])
            processed += 1

        msg = f'Broadcasts processed: {processed} active campaigns.'
        self.stdout.write(msg)
        logger.info(msg)
