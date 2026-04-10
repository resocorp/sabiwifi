"""RADIUS integration for voucher activation."""
import logging
import secrets

from django.utils import timezone
from datetime import timedelta

from accounts.models import Subscriber
from plans.models import Subscription
from radius.utils import assign_subscriber_to_plan, disconnect_subscriber_sessions

logger = logging.getLogger(__name__)


def activate_voucher(voucher):
    """
    Activate a voucher: create/retrieve subscriber, assign RADIUS, create subscription.
    Returns (subscriber, auth_token).
    """
    if voucher.subscriber:
        # Already activated — refresh auth token and re-sync RADIUS
        subscriber = voucher.subscriber
        subscriber.auth_token = secrets.token_hex(32)
        subscriber.save(update_fields=['auth_token', 'updated_at'])
        disconnect_subscriber_sessions(subscriber)
        assign_subscriber_to_plan(subscriber, voucher.plan)
        return subscriber, subscriber.auth_token

    # First activation — create subscriber with PIN as phone
    auth_token = secrets.token_hex(32)
    subscriber = Subscriber.objects.create(
        reseller=voucher.reseller,
        phone=voucher.pin,
        email='',
        verified=True,
        auth_token=auth_token,
        is_voucher_user=True,
    )

    # Calculate expiry
    now = timezone.now()
    voucher.subscriber = subscriber
    voucher.activated_at = now
    voucher.status = 'active'

    if voucher.batch.validity_type == 'fixed_date' and voucher.batch.validity_fixed_date:
        voucher.expires_at = voucher.batch.validity_fixed_date
    elif voucher.batch.validity_days:
        voucher.expires_at = now + timedelta(days=voucher.batch.validity_days)
    else:
        # Use plan duration as fallback
        plan = voucher.plan
        if plan.duration_days > 0:
            voucher.expires_at = now + timedelta(days=plan.duration_days)
        elif plan.duration_hours > 0:
            voucher.expires_at = now + timedelta(hours=float(plan.duration_hours))
        else:
            voucher.expires_at = now + timedelta(days=365)

    voucher.save()

    # Create subscription
    Subscription.objects.filter(
        subscriber=subscriber, status='active'
    ).update(status='expired')

    Subscription.objects.create(
        subscriber=subscriber,
        plan=voucher.plan,
        reseller=voucher.reseller,
        start_date=now,
        expiry_date=voucher.expires_at,
        status='active',
    )

    # Write RADIUS credentials
    assign_subscriber_to_plan(subscriber, voucher.plan)

    logger.info(f"Voucher {voucher.pin} activated for reseller {voucher.reseller.name}")
    return subscriber, auth_token
