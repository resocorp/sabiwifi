"""
Management command: check_usage

Checks cumulative online time limits and daily quotas against radacct data.
When limits are exceeded, either switches subscriber to fallback plan or
disconnects them.

Run via systemd timer every 5 minutes:
    python manage.py check_usage
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Sum, Q
from django.utils import timezone

from plans.models import Subscription, DailyUsageSnapshot
from radius.models import Radacct
from radius.utils import (
    assign_subscriber_to_plan,
    disconnect_subscriber_sessions,
    remove_subscriber_from_radius,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Check cumulative time limits and daily quotas, enforce caps via CoA.'

    def handle(self, *args, **options):
        now = timezone.now()
        today = now.date()

        ok = 0
        errors = 0

        # --- 1. Cumulative online time limits ---
        time_limited = Subscription.objects.filter(
            status='active',
            plan__online_time_limit_minutes__gt=0,
        ).select_related('subscriber', 'plan', 'plan__fallback_plan')

        for sub in time_limited:
            try:
                total_seconds = Radacct.objects.filter(
                    username=sub.subscriber.phone,
                    acctstarttime__gte=sub.start_date,
                ).aggregate(total=Sum('acctsessiontime'))['total'] or 0

                limit_seconds = sub.plan.online_time_limit_minutes * 60
                if total_seconds >= limit_seconds:
                    if sub.plan.fallback_plan:
                        assign_subscriber_to_plan(sub.subscriber, sub.plan.fallback_plan)
                        disconnect_subscriber_sessions(sub.subscriber)
                        sub.status = 'limited'
                        sub.save(update_fields=['status', 'updated_at'])
                        logger.info(f'Time limit: {sub.subscriber.phone} → fallback plan')
                    else:
                        sub.status = 'expired'
                        sub.save(update_fields=['status', 'updated_at'])
                        disconnect_subscriber_sessions(sub.subscriber)
                        remove_subscriber_from_radius(sub.subscriber)
                        logger.info(f'Time limit: {sub.subscriber.phone} expired (no fallback)')
                    ok += 1
            except Exception as exc:
                logger.error(f'Error checking time for {sub.subscriber.phone}: {exc}')
                errors += 1

        # --- 2. Daily quotas ---
        today_midnight = timezone.make_aware(
            timezone.datetime.combine(today, timezone.datetime.min.time())
        )

        daily_limited = Subscription.objects.filter(
            status='active',
        ).filter(
            Q(plan__daily_download_mb__gt=0) |
            Q(plan__daily_upload_mb__gt=0) |
            Q(plan__daily_total_mb__gt=0) |
            Q(plan__daily_time_minutes__gt=0)
        ).select_related('subscriber', 'plan', 'plan__daily_fallback_plan')

        for sub in daily_limited:
            try:
                # Check if already exceeded today
                snapshot, created = DailyUsageSnapshot.objects.get_or_create(
                    subscriber=sub.subscriber, date=today,
                    defaults={'download_bytes': 0, 'upload_bytes': 0, 'online_seconds': 0},
                )
                if snapshot.quota_exceeded:
                    continue

                # Query today's usage from radacct
                usage = Radacct.objects.filter(
                    username=sub.subscriber.phone,
                    acctstarttime__gte=today_midnight,
                ).aggregate(
                    dl=Sum('acctoutputoctets'),  # output = download to subscriber
                    ul=Sum('acctinputoctets'),    # input = upload from subscriber
                    time=Sum('acctsessiontime'),
                )
                dl_bytes = usage['dl'] or 0
                ul_bytes = usage['ul'] or 0
                time_seconds = usage['time'] or 0

                # Update snapshot
                snapshot.download_bytes = dl_bytes
                snapshot.upload_bytes = ul_bytes
                snapshot.online_seconds = time_seconds
                snapshot.save(update_fields=['download_bytes', 'upload_bytes', 'online_seconds'])

                # Check quotas
                plan = sub.plan
                exceeded = False
                if plan.daily_download_mb > 0 and dl_bytes >= plan.daily_download_mb * 1024 * 1024:
                    exceeded = True
                if plan.daily_upload_mb > 0 and ul_bytes >= plan.daily_upload_mb * 1024 * 1024:
                    exceeded = True
                if plan.daily_total_mb > 0 and (dl_bytes + ul_bytes) >= plan.daily_total_mb * 1024 * 1024:
                    exceeded = True
                if plan.daily_time_minutes > 0 and time_seconds >= plan.daily_time_minutes * 60:
                    exceeded = True

                if exceeded:
                    snapshot.quota_exceeded = True
                    snapshot.save(update_fields=['quota_exceeded'])

                    if plan.daily_fallback_plan:
                        assign_subscriber_to_plan(sub.subscriber, plan.daily_fallback_plan)
                        disconnect_subscriber_sessions(sub.subscriber)
                        logger.info(f'Daily quota: {sub.subscriber.phone} → daily fallback')
                    else:
                        disconnect_subscriber_sessions(sub.subscriber)
                        remove_subscriber_from_radius(sub.subscriber)
                        logger.info(f'Daily quota: {sub.subscriber.phone} disconnected')

                    try:
                        from notifications.notify import notify_subscriber
                        notify_subscriber(sub.subscriber, 'daily_quota_exceeded', {
                            'name': sub.subscriber.phone,
                        })
                    except Exception:
                        pass

                    ok += 1
            except Exception as exc:
                logger.error(f'Error checking daily quota for {sub.subscriber.phone}: {exc}')
                errors += 1

        msg = f'Usage check complete. {ok} action(s) taken.'
        if errors:
            msg += f' {errors} error(s).'
        self.stdout.write(msg)
        if ok or errors:
            logger.info(msg)
