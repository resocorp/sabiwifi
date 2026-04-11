"""RADIUS integration for voucher activation."""
import logging
import secrets

from django.utils import timezone
from datetime import timedelta

from accounts.models import Subscriber
from plans.services import activate_subscription, calculate_plan_expiry
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
        voucher.expires_at = calculate_plan_expiry(voucher.plan, from_time=now)

    voucher.save()

    # Create subscription and sync RADIUS
    activate_subscription(
        subscriber, voucher.plan,
        reseller=voucher.reseller, expiry_date=voucher.expires_at,
    )

    logger.info(f"Voucher {voucher.pin} activated for reseller {voucher.reseller.name}")
    return subscriber, auth_token
