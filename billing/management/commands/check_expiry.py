"""
Management command: check_expiry
Runs every 15 minutes via cron. Finds expired subscriptions, marks them expired,
and removes RADIUS group assignment.
"""
import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from plans.models import Subscription
from radius.utils import remove_subscriber_from_radius

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Check for expired subscriptions and deactivate them.'

    def handle(self, *args, **options):
        now = timezone.now()

        expired_subs = Subscription.objects.filter(
            status='active',
            expiry_date__lte=now,
        ).select_related('subscriber', 'plan')

        count = 0
        for sub in expired_subs:
            sub.status = 'expired'
            sub.save()

            # Check if subscriber has any other active subscription
            other_active = Subscription.objects.filter(
                subscriber=sub.subscriber,
                status='active',
            ).exclude(pk=sub.pk).exists()

            if not other_active:
                remove_subscriber_from_radius(sub.subscriber)

            count += 1
            logger.info(f"Expired: {sub.subscriber.phone} plan {sub.plan.name}")

        self.stdout.write(f"Checked expiry: {count} subscription(s) expired.")
