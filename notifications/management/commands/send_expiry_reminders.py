"""
Management command: send_expiry_reminders

Runs every 15 minutes via systemd timer. Sends reminders to subscribers
whose plans expire within 24 hours (1d warning) or within 3-4 days (3d warning).

Respects per-reseller ResellerNotificationConfig and subscriber prefs.
Uses notify_subscriber() which handles channel selection (WA / SMS).
De-duplicates with Redis cache to avoid repeat sends.
"""
import logging
from datetime import timedelta

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from plans.models import Subscription

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Send plan expiry reminders (3-day and 1-day) to subscribers.'

    def handle(self, *args, **options):
        from notifications.notify import notify_subscriber

        now = timezone.now()
        sent_1d = sent_3d = skipped = 0

        # 1-day window: expires in 0–24 hours
        window_1d_end = now + timedelta(hours=24)
        # 3-day window: expires in 48–96 hours (catches "3 days away" with some buffer)
        window_3d_start = now + timedelta(hours=48)
        window_3d_end = now + timedelta(hours=96)

        # --- 1-day reminders ---
        subs_1d = Subscription.objects.filter(
            status='active',
            expiry_date__gte=now,
            expiry_date__lte=window_1d_end,
        ).select_related('subscriber', 'plan', 'subscriber__reseller')

        for sub in subs_1d:
            cache_key = f'expiry_1d_{sub.id}'
            if cache.get(cache_key):
                skipped += 1
                continue
            context = {
                'name': sub.subscriber.phone,
                'plan': sub.plan.name,
                'expiry_date': sub.expiry_date.strftime('%d %b %Y'),
                'link': f'https://app.sabiwifi.com/portal/account/?r={sub.reseller.slug}',
            }
            notify_subscriber(sub.subscriber, 'plan_expiry_1d', context)
            cache.set(cache_key, True, timeout=86400)
            sent_1d += 1

        # --- 3-day reminders ---
        subs_3d = Subscription.objects.filter(
            status='active',
            expiry_date__gte=window_3d_start,
            expiry_date__lte=window_3d_end,
        ).select_related('subscriber', 'plan', 'subscriber__reseller')

        for sub in subs_3d:
            cache_key = f'expiry_3d_{sub.id}'
            if cache.get(cache_key):
                skipped += 1
                continue
            context = {
                'name': sub.subscriber.phone,
                'plan': sub.plan.name,
                'expiry_date': sub.expiry_date.strftime('%d %b %Y'),
                'link': f'https://app.sabiwifi.com/portal/account/?r={sub.reseller.slug}',
            }
            notify_subscriber(sub.subscriber, 'plan_expiry_3d', context)
            cache.set(cache_key, True, timeout=86400)
            sent_3d += 1

        msg = f'Expiry reminders: {sent_1d} 1-day, {sent_3d} 3-day sent. {skipped} skipped (already sent).'
        self.stdout.write(msg)
        logger.info(msg)
