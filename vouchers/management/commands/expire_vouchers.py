"""
Management command: expire_vouchers

Finds activated vouchers whose expires_at has passed, marks them expired,
and expires the associated subscription + removes RADIUS access.

Run via systemd timer every 5 minutes:
    python manage.py expire_vouchers
"""
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from vouchers.models import Voucher
from plans.models import Subscription
from radius.utils import disconnect_subscriber_sessions, remove_subscriber_from_radius

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Expire vouchers past their expiry date and remove RADIUS access.'

    def handle(self, *args, **options):
        now = timezone.now()

        expired_vouchers = list(
            Voucher.objects.filter(
                status='active',
                expires_at__lte=now,
            ).select_related('subscriber', 'plan', 'reseller')
        )

        if not expired_vouchers:
            self.stdout.write('No vouchers to expire.')
            return

        ok = 0
        errors = 0

        for voucher in expired_vouchers:
            try:
                voucher.status = 'expired'
                voucher.save(update_fields=['status'])

                subscriber = voucher.subscriber
                if subscriber:
                    # Expire associated subscription
                    Subscription.objects.filter(
                        subscriber=subscriber, status='active'
                    ).update(status='expired')

                    disconnect_subscriber_sessions(subscriber)
                    remove_subscriber_from_radius(subscriber)

                    try:
                        from notifications.notify import notify_subscriber
                        notify_subscriber(subscriber, 'plan_expired', {
                            'name': subscriber.phone,
                            'plan': voucher.plan.name,
                        })
                    except Exception as exc:
                        logger.warning(f'Voucher expiry notify failed for {voucher.pin}: {exc}')

                logger.info(f'Expired voucher {voucher.pin} (reseller: {voucher.reseller.name})')
                ok += 1
            except Exception as exc:
                logger.error(f'Error expiring voucher {voucher.pk}: {exc}')
                errors += 1

        msg = f'Expired {ok} voucher(s).'
        if errors:
            msg += f' {errors} error(s) — check logs.'
        self.stdout.write(msg)
        logger.info(msg)
