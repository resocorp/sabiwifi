"""
Management command: reset_daily_quotas

At midnight, restores subscribers who were on daily fallback plans back to
their original plan's RADIUS group.

Run via cron daily at 00:00:
    python manage.py reset_daily_quotas
"""
import logging

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from plans.models import Subscription, DailyUsageSnapshot
from radius.utils import assign_subscriber_to_plan, disconnect_subscriber_sessions

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Reset daily quotas — restore subscribers from daily fallback to original plan.'

    def handle(self, *args, **options):
        yesterday = (timezone.now() - timezone.timedelta(days=1)).date()

        # Find subscribers who exceeded daily quota yesterday
        exceeded = DailyUsageSnapshot.objects.filter(
            date=yesterday,
            quota_exceeded=True,
        ).select_related('subscriber')

        restored = 0
        for snapshot in exceeded:
            subscriber = snapshot.subscriber
            try:
                # Find their active/limited subscription
                sub = Subscription.objects.filter(
                    subscriber=subscriber,
                    status__in=['active', 'limited'],
                ).select_related('plan').first()

                if sub:
                    # Restore to original plan RADIUS group
                    assign_subscriber_to_plan(subscriber, sub.plan)
                    disconnect_subscriber_sessions(subscriber)
                    if sub.status == 'limited':
                        sub.status = 'active'
                        sub.save(update_fields=['status', 'updated_at'])
                    restored += 1
                    logger.info(f'Daily reset: restored {subscriber.phone} to {sub.plan.name}')
            except Exception as exc:
                logger.error(f'Error resetting daily quota for {subscriber.phone}: {exc}')

        msg = f'Daily quota reset: {restored} subscriber(s) restored.'
        self.stdout.write(msg)
        if restored:
            logger.info(msg)
