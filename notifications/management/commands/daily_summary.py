"""
Management command: daily_summary
Runs daily at 8am via cron. Sends operator a platform summary via the
configured channel (SMS, WhatsApp, or both) + recipients.
"""
import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Sum
from accounts.models import Reseller, Subscriber
from plans.models import Subscription
from billing.models import Payment
from routers.models import Router
from operator_panel.services.notify_operator import notify as notify_operator

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Send daily summary to operator via configured channel.'

    def handle(self, *args, **options):
        now = timezone.now()
        yesterday = now - timedelta(days=1)

        revenue = Payment.objects.filter(
            paystack_status='success', created_at__gte=yesterday,
        ).aggregate(total=Sum('amount_ngn'))['total'] or 0

        orders = Payment.objects.filter(
            paystack_status='success', created_at__gte=yesterday,
        ).count()

        new_subs = Subscriber.objects.filter(created_at__gte=yesterday).count()
        offline = Router.objects.filter(status='offline', reseller__isnull=False).count()

        summary = notify_operator('daily_summary', {
            'revenue': f'{revenue:,.0f}',
            'orders': orders,
            'new_subs': new_subs,
            'offline': offline,
        })
        self.stdout.write(f'Daily summary dispatch: {summary}')
        logger.info(f'Daily summary: {summary}')
