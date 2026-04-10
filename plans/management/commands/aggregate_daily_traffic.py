"""
Management command: aggregate_daily_traffic

Summarizes yesterday's radacct data into DailyUsageSnapshot records.
Used for reports and as a performance optimization so report queries
don't need to scan the full radacct table.

Run via cron daily at 02:00:
    python manage.py aggregate_daily_traffic
"""
import logging

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Sum
from django.utils import timezone

from plans.models import DailyUsageSnapshot
from accounts.models import Subscriber

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Aggregate yesterday\'s radacct data into DailyUsageSnapshot.'

    def handle(self, *args, **options):
        yesterday = (timezone.now() - timezone.timedelta(days=1)).date()
        yesterday_start = timezone.make_aware(
            timezone.datetime.combine(yesterday, timezone.datetime.min.time())
        )
        yesterday_end = timezone.make_aware(
            timezone.datetime.combine(yesterday + timezone.timedelta(days=1), timezone.datetime.min.time())
        )

        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT ra.username,
                       SUM(ra.acctoutputoctets) as dl,
                       SUM(ra.acctinputoctets) as ul,
                       SUM(ra.acctsessiontime) as t,
                       COUNT(*) as sessions
                FROM radacct ra
                WHERE ra.acctstarttime >= %s AND ra.acctstarttime < %s
                GROUP BY ra.username
            """, [yesterday_start, yesterday_end])
            rows = cursor.fetchall()

        if not rows:
            self.stdout.write(f'No radacct data for {yesterday}.')
            return

        # Build a username -> subscriber_id map
        usernames = [r[0] for r in rows]
        sub_map = dict(
            Subscriber.objects.filter(phone__in=usernames)
            .values_list('phone', 'id')
        )

        created = 0
        updated = 0
        for username, dl, ul, t, sessions in rows:
            sub_id = sub_map.get(username)
            if not sub_id:
                continue

            snapshot, was_created = DailyUsageSnapshot.objects.update_or_create(
                subscriber_id=sub_id,
                date=yesterday,
                defaults={
                    'download_bytes': dl or 0,
                    'upload_bytes': ul or 0,
                    'online_seconds': t or 0,
                    'sessions': sessions or 0,
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1

        msg = f'Aggregated {yesterday}: {created} created, {updated} updated.'
        self.stdout.write(msg)
        logger.info(msg)
