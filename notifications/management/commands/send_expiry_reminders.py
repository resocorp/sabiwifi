"""
Management command: send_expiry_reminders
Runs every 15 minutes via cron. Sends SMS reminders to subscribers
whose plans expire within 24 hours.
"""
import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.core.cache import cache
from django.utils import timezone
from plans.models import Subscription
from notifications.sms import get_sms_service

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Send SMS expiry reminders to subscribers whose plans expire within 24 hours.'

    def handle(self, *args, **options):
        now = timezone.now()
        reminder_window_start = now
        reminder_window_end = now + timedelta(hours=24)

        expiring_subs = Subscription.objects.filter(
            status='active',
            expiry_date__gte=reminder_window_start,
            expiry_date__lte=reminder_window_end,
        ).select_related('subscriber', 'plan')

        sms = get_sms_service()
        sent = 0

        for sub in expiring_subs:
            cache_key = f'expiry_reminder_{sub.id}'
            if cache.get(cache_key):
                continue

            phone = sub.subscriber.phone
            plan_name = sub.plan.name
            sms.send_expiry_reminder(phone, plan_name)
            cache.set(cache_key, True, timeout=86400)
            sent += 1
            logger.info(f"Expiry reminder sent to {phone} for {plan_name}")

        self.stdout.write(f"Expiry reminders: {sent} sent.")
