"""
Management command: check_alerts
Runs every 5 minutes via cron. Checks for conditions that need operator notification:
- Router offline > 1 hour
- Payment failure spike (>=5 failures in last 10 min across platform)
- Reseller hit free subscriber limit
Dispatches via operator_panel.services.notify_operator, which honours
PlatformSettings channel (SMS / WhatsApp / both) and per-event toggles.
"""
import logging
from datetime import timedelta
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Count, Q
from routers.models import Router
from billing.models import Payment
from accounts.models import Reseller
from plans.models import Subscription
from operator_panel.services.notify_operator import notify as notify_operator
from notifications.sms import send_operator_alert

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Check for alert conditions and notify operator.'

    def handle(self, *args, **options):
        alerts_sent = 0
        alerts_sent += self._check_offline_routers()
        alerts_sent += self._check_payment_failure_spike()
        alerts_sent += self._check_free_limits()
        self.stdout.write(f'Alert check complete: {alerts_sent} alert(s) sent.')

    def _check_offline_routers(self):
        """Alert if any router has been offline for >1 hour. Throttle per router 1h."""
        one_hour_ago = timezone.now() - timedelta(hours=1)
        offline_routers = Router.objects.filter(
            status='offline', reseller__isnull=False,
        ).filter(
            Q(last_seen__lte=one_hour_ago) | Q(last_seen__isnull=True)
        ).select_related('reseller')

        count = 0
        for router in offline_routers:
            cache_key = f'alert_offline_{router.serial_number}'
            if cache.get(cache_key):
                continue
            notify_operator('router_offline', {
                'serial': router.serial_number,
                'partner': router.reseller.name if router.reseller else 'unassigned',
                'last_seen': router.last_seen.strftime('%Y-%m-%d %H:%M') if router.last_seen else 'never',
            })
            cache.set(cache_key, True, timeout=3600)
            count += 1
        return count

    def _check_payment_failure_spike(self):
        """Alert once per 10 min if >=5 platform-wide payment failures in last 10 min."""
        ten_min_ago = timezone.now() - timedelta(minutes=10)
        count = Payment.objects.filter(
            paystack_status='failed', created_at__gte=ten_min_ago,
        ).count()
        if count < 5:
            return 0
        cache_key = 'alert_payfail_spike'
        if cache.get(cache_key):
            return 0
        notify_operator('payment_failure_spike', {'count': count})
        cache.set(cache_key, True, timeout=600)
        return 1

    def _check_free_limits(self):
        """Free-limit alerts stay SMS-only for now (no operator event defined)."""
        resellers = Reseller.objects.filter(payment_verified=False, status='active')
        count = 0
        for reseller in resellers:
            limit = reseller.get_free_subscriber_limit()
            active = Subscription.objects.filter(
                reseller=reseller, status='active',
            ).count()
            if active < limit:
                continue
            cache_key = f'alert_freelimit_{reseller.id}'
            if cache.get(cache_key):
                continue
            send_operator_alert(
                f'Free limit reached: {reseller.name} ({active}/{limit} subscribers). '
                f'Bank not verified.'
            )
            cache.set(cache_key, True, timeout=86400)
            count += 1
        return count
