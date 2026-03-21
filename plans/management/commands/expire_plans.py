"""
Management command: expire_plans

Finds all active subscriptions whose expiry_date has passed, marks them
expired, removes them from RADIUS, and sends CoA Disconnect-Messages to
evict any still-open router sessions.

Run via systemd timer every 5 minutes:
    python manage.py expire_plans
"""
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from plans.models import Subscription
from radius.utils import disconnect_subscriber_sessions, remove_subscriber_from_radius

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Expire overdue subscriptions and remove RADIUS access.'

    def handle(self, *args, **options):
        now = timezone.now()

        expired = list(
            Subscription.objects.filter(
                status='active',
                expiry_date__lte=now,
            ).select_related('subscriber', 'plan')
        )

        if not expired:
            self.stdout.write('No subscriptions to expire.')
            return

        ok = 0
        errors = 0

        for sub in expired:
            subscriber = sub.subscriber
            try:
                # Mark expired first so concurrent login attempts see no active plan
                sub.status = 'expired'
                sub.save(update_fields=['status', 'updated_at'])

                # Kick any open router sessions via CoA Disconnect-Message
                disconnect_subscriber_sessions(subscriber)

                # Remove from RADIUS so new auth attempts are rejected
                remove_subscriber_from_radius(subscriber)

                logger.info(
                    f'Expired: {subscriber.phone} plan={sub.plan.name} '
                    f'expired_at={sub.expiry_date.isoformat()}'
                )
                ok += 1
            except Exception as exc:
                logger.error(f'Error expiring sub {sub.pk} for {subscriber.phone}: {exc}')
                errors += 1

        msg = f'Expired {ok} subscription(s).'
        if errors:
            msg += f' {errors} error(s) — check logs.'
        self.stdout.write(msg)
        logger.info(msg)
