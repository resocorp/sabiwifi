"""
Management command: expire_plans

Finds all active subscriptions whose expiry_date has passed, marks them
expired, removes them from RADIUS, and sends CoA Disconnect-Messages to
evict any still-open router sessions.

Run via systemd timer every 5 minutes:
    python manage.py expire_plans
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from plans.models import Subscription
from radius.models import Radacct
from radius.utils import disconnect_subscriber_sessions, remove_subscriber_from_radius

logger = logging.getLogger(__name__)

# A radacct row whose last interim-update is older than this is considered
# stale (the device vanished without sending Acct-Stop). Interim updates are
# configured at 5 min on the router, so 30 min gives ample slack.
STALE_SESSION_AGE = timedelta(minutes=30)


class Command(BaseCommand):
    help = 'Expire overdue subscriptions and remove RADIUS access.'

    def handle(self, *args, **options):
        now = timezone.now()
        self._close_stale_radacct(now)

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
                # Check auto-renewal before expiring
                if (sub.plan.allow_auto_renew and subscriber.auto_renew_enabled
                        and not sub.plan.is_free):
                    try:
                        from billing.wallet import renew_plan_from_wallet
                        new_sub = renew_plan_from_wallet(subscriber, sub.plan)
                        if new_sub:
                            sub.status = 'expired'
                            sub.save(update_fields=['status', 'updated_at'])
                            try:
                                from notifications.notify import notify_subscriber
                                notify_subscriber(subscriber, 'auto_renewal_success', {
                                    'name': subscriber.phone,
                                    'plan': sub.plan.name,
                                })
                            except Exception:
                                pass
                            logger.info(f'Auto-renewed: {subscriber.phone} plan={sub.plan.name}')
                            ok += 1
                            continue
                        else:
                            # Insufficient balance — notify and proceed with expiry
                            try:
                                from notifications.notify import notify_subscriber
                                notify_subscriber(subscriber, 'auto_renewal_failed', {
                                    'name': subscriber.phone,
                                    'plan': sub.plan.name,
                                    'link': f'https://app.sabiwifi.com/portal/account/?r={subscriber.reseller.slug}',
                                })
                            except Exception:
                                pass
                    except Exception as renew_exc:
                        logger.warning(f'Auto-renewal failed for {subscriber.phone}: {renew_exc}')

                # Mark expired first so concurrent login attempts see no active plan
                sub.status = 'expired'
                sub.save(update_fields=['status', 'updated_at'])

                # Kick any open router sessions via CoA Disconnect-Message
                disconnect_subscriber_sessions(subscriber)

                # Remove from RADIUS so new auth attempts are rejected
                remove_subscriber_from_radius(subscriber)

                # Notify subscriber their plan has expired
                try:
                    from notifications.notify import notify_subscriber
                    notify_subscriber(subscriber, 'plan_expired', {
                        'name': subscriber.phone,
                        'plan': sub.plan.name,
                        'link': f'https://app.sabiwifi.com/portal/account/?r={subscriber.reseller.slug}',
                    })
                except Exception as _exc:
                    logger.warning(f'Expiry notify failed for {subscriber.phone}: {_exc}')

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

    def _close_stale_radacct(self, now):
        """
        Mark radacct rows with no recent interim-update as stopped.

        A device that loses connection without an Acct-Stop leaves a row
        marked open forever. That poisons Simultaneous-Use checks and makes
        "Disconnect all devices" send DMs at ghost sessions (NAKs in the log).
        """
        cutoff = now - STALE_SESSION_AGE
        stale = Radacct.objects.filter(
            acctstoptime__isnull=True,
        ).filter(
            Q(acctupdatetime__lt=cutoff) | (
                Q(acctupdatetime__isnull=True) & Q(acctstarttime__lt=cutoff)
            )
        )
        count = stale.update(
            acctstoptime=now,
            acctterminatecause='Session-Timeout',
        )
        if count:
            logger.info(f'Closed {count} stale radacct session(s) older than {STALE_SESSION_AGE}.')
