"""
Shared subscription lifecycle helpers.

Centralises the "expire → create → RADIUS sync" logic used by portal,
billing, wallet, and voucher flows.
"""
from datetime import timedelta

from django.utils import timezone

from plans.models import Subscription
from radius.utils import assign_subscriber_to_plan


def calculate_plan_expiry(plan, from_time=None):
    """
    Return the expiry datetime for a plan starting at *from_time* (default: now).
    Falls back to 100 years for plans with no duration set (unlimited).
    """
    now = from_time or timezone.now()
    days = int(plan.duration_days or 0)
    hours = float(plan.duration_hours or 0)
    if days or hours:
        return now + timedelta(days=days, hours=hours)
    return now + timedelta(days=36500)


def activate_subscription(subscriber, plan, reseller=None, expiry_date=None):
    """
    Expire any active subscription, create a new one, and sync RADIUS.
    Returns the new Subscription.

    *reseller* defaults to subscriber.reseller if not provided.
    *expiry_date* overrides the plan-based expiry calculation (used by vouchers).
    """
    reseller = reseller or subscriber.reseller
    now = timezone.now()

    Subscription.objects.filter(
        subscriber=subscriber, status='active',
    ).update(status='expired')

    sub = Subscription.objects.create(
        subscriber=subscriber,
        plan=plan,
        reseller=reseller,
        start_date=now,
        expiry_date=expiry_date or calculate_plan_expiry(plan, from_time=now),
        status='active',
    )
    assign_subscriber_to_plan(subscriber, plan)
    return sub
