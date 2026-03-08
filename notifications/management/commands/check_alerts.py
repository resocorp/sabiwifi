"""
Management command: check_alerts
Runs every 5 minutes via cron. Checks for conditions that need operator notification:
- Router offline > 1 hour
- Payment failure spike (>3 failures/hour for same reseller)
- Reseller hit free subscriber limit
"""
import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Count, Q
from operator_panel.models import PlatformSettings
from routers.models import Router
from billing.models import Payment
from accounts.models import Reseller
from plans.models import Subscription
from notifications.sms import send_operator_alert

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Check for alert conditions and notify operator via SMS.'

    def handle(self, *args, **options):
        ps = PlatformSettings.load()
        alerts_sent = 0

        if ps.notify_on_router_offline:
            alerts_sent += self._check_offline_routers()

        if ps.notify_on_payment_failure:
            alerts_sent += self._check_payment_failures()

        alerts_sent += self._check_free_limits()

        self.stdout.write(f"Alert check complete: {alerts_sent} alert(s) sent.")

    def _check_offline_routers(self):
        """Alert if any router has been offline for >1 hour."""
        one_hour_ago = timezone.now() - timedelta(hours=1)
        offline_routers = Router.objects.filter(
            status='offline',
            reseller__isnull=False,
        ).filter(
            Q(last_seen__lte=one_hour_ago) | Q(last_seen__isnull=True)
        ).select_related('reseller')

        count = 0
        for router in offline_routers:
            cache_key = f'alert_offline_{router.serial_number}'
            from django.core.cache import cache
            if not cache.get(cache_key):
                reseller_name = router.reseller.name if router.reseller else 'unassigned'
                send_operator_alert(
                    f"Router offline: {router.serial_number} ({reseller_name}) "
                    f"- last seen {router.last_seen or 'never'}"
                )
                cache.set(cache_key, True, timeout=3600)
                count += 1
        return count

    def _check_payment_failures(self):
        """Alert if >3 payment failures in the last hour for any reseller."""
        one_hour_ago = timezone.now() - timedelta(hours=1)
        failure_counts = Payment.objects.filter(
            paystack_status='failed',
            created_at__gte=one_hour_ago,
        ).values('reseller__name', 'reseller_id').annotate(
            fail_count=Count('id')
        ).filter(fail_count__gt=3)

        count = 0
        for entry in failure_counts:
            cache_key = f'alert_payfail_{entry["reseller_id"]}'
            from django.core.cache import cache
            if not cache.get(cache_key):
                send_operator_alert(
                    f"Payment failure spike: {entry['reseller__name']} "
                    f"({entry['fail_count']} failures in last hour)"
                )
                cache.set(cache_key, True, timeout=3600)
                count += 1
        return count

    def _check_free_limits(self):
        """Alert when a reseller hits their free subscriber limit."""
        resellers = Reseller.objects.filter(payment_verified=False, status='active')
        count = 0

        for reseller in resellers:
            limit = reseller.get_free_subscriber_limit()
            active = Subscription.objects.filter(
                reseller=reseller, status='active'
            ).count()

            if active >= limit:
                cache_key = f'alert_freelimit_{reseller.id}'
                from django.core.cache import cache
                if not cache.get(cache_key):
                    send_operator_alert(
                        f"Free limit reached: {reseller.name} ({active}/{limit} subscribers). "
                        f"Bank not verified."
                    )
                    cache.set(cache_key, True, timeout=86400)
                    count += 1
        return count
