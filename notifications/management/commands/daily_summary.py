"""
Management command: daily_summary
Runs daily at 8am via cron. Sends operator a summary SMS.
"""
import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Sum, Count
from operator_panel.models import PlatformSettings
from accounts.models import Reseller, Subscriber
from plans.models import Subscription
from billing.models import Payment
from routers.models import Router
from notifications.sms import send_operator_alert

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Send daily summary SMS to operator.'

    def handle(self, *args, **options):
        ps = PlatformSettings.load()
        if not ps.notify_daily_summary:
            self.stdout.write("Daily summary notifications disabled.")
            return

        now = timezone.now()
        yesterday = now - timedelta(days=1)

        # Aggregate stats
        total_resellers = Reseller.objects.filter(status='active').count()
        total_subscribers = Subscriber.objects.count()
        active_subs = Subscription.objects.filter(status='active').count()

        routers_online = Router.objects.filter(status='online').count()
        routers_total = Router.objects.filter(reseller__isnull=False).count()

        yesterday_revenue = Payment.objects.filter(
            paystack_status='success',
            created_at__gte=yesterday,
        ).aggregate(total=Sum('amount_ngn'))['total'] or 0

        yesterday_payments = Payment.objects.filter(
            paystack_status='success',
            created_at__gte=yesterday,
        ).count()

        yesterday_failures = Payment.objects.filter(
            paystack_status='failed',
            created_at__gte=yesterday,
        ).count()

        new_subscribers = Subscriber.objects.filter(
            created_at__gte=yesterday,
        ).count()

        platform_revenue = Payment.objects.filter(
            paystack_status='success',
            created_at__gte=yesterday,
        ).aggregate(total=Sum('platform_amount_ngn'))['total'] or 0

        message = (
            f"Daily Summary ({now.strftime('%b %d')}):\n"
            f"Revenue: ₦{yesterday_revenue:,.0f} ({yesterday_payments} payments, {yesterday_failures} failed)\n"
            f"Platform: ₦{platform_revenue:,.0f}\n"
            f"Resellers: {total_resellers} active\n"
            f"Subs: {active_subs} active / {total_subscribers} total (+{new_subscribers} new)\n"
            f"Routers: {routers_online}/{routers_total} online"
        )

        send_operator_alert(message)
        self.stdout.write(f"Daily summary sent.")
        logger.info(f"Daily summary: {message}")
