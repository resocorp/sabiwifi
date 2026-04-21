"""
Lead lifecycle helpers used by views and (later) AI agents.

Contains the one-way Lead → Subscriber conversion, which runs when a Paystack
payment clears for a lead-payment. Reuses the existing Subscriber model so
lead-originated subscribers behave identically to portal-originated ones.
"""
import secrets

from django.db import transaction
from django.utils import timezone

from leads.models import Lead, InstallationOrder


def _generate_pppoe_credentials(phone):
    """Stable username (phone) + random 10-char password."""
    return phone, secrets.token_urlsafe(8)[:10]


@transaction.atomic
def convert_lead_to_subscriber(lead, payment=None, service_mode=InstallationOrder.SERVICE_MODE_PPPOE):
    """
    Convert a paid lead into a Subscriber and create the InstallationOrder.

    Idempotent: if lead.converted_subscriber is already set, returns the
    existing pair without duplicating side-effects.
    """
    if lead.converted_subscriber_id:
        order = InstallationOrder.objects.filter(
            lead=lead, reseller=lead.reseller,
        ).order_by('-created_at').first()
        return lead.converted_subscriber, order

    from accounts.models import Subscriber
    subscriber, created = Subscriber.objects.get_or_create(
        reseller=lead.reseller,
        phone=lead.phone,
        defaults={
            'email': lead.email or '',
            'source': Subscriber.SOURCE_STAFF,
            'verified': True,
            'signup_fee_paid': True,
        },
    )
    # If the subscriber already existed (phone collision with a portal signup)
    # we still proceed — link back but don't overwrite their pin.
    if created:
        subscriber.generate_auth_token()
        subscriber.save(update_fields=['auth_token'])

    username = password = ''
    if service_mode == InstallationOrder.SERVICE_MODE_PPPOE:
        username, password = _generate_pppoe_credentials(subscriber.phone)

    order = InstallationOrder.objects.create(
        reseller=lead.reseller,
        lead=lead,
        payment=payment,
        status=InstallationOrder.STATUS_PENDING,
        service_mode=service_mode,
        address=lead.address or '',
        pppoe_username=username,
        pppoe_password=password,
    )

    lead.converted_subscriber = subscriber
    lead.converted_at = timezone.now()
    lead.status = Lead.STATUS_PAID
    lead.save(update_fields=['converted_subscriber', 'converted_at', 'status', 'updated_at'])

    return subscriber, order
