"""
Lead lifecycle helpers used by views and (later) AI agents.

Contains:
  convert_lead_to_subscriber  — runs when Paystack payment clears for a lead.
                                Creates Subscriber + InstallationOrder. The
                                billing webhook also raises an install ticket.
  complete_installation       — runs when the field tech finishes the install.
                                Activates the subscription, syncs RADIUS, sends
                                the "you're connected" handoff, resolves the
                                linked install ticket.

Reuses the existing Subscriber model so lead-originated subscribers behave
identically to portal-originated ones.
"""
import logging
import secrets

from django.db import transaction
from django.utils import timezone

from leads.models import Lead, InstallationOrder

logger = logging.getLogger(__name__)


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


class InstallationCompletionError(Exception):
    """Raised when complete_installation cannot proceed (e.g. no plan attached)."""


@transaction.atomic
def complete_installation(order, plan=None, actor='system'):
    """
    Mark an InstallationOrder as completed and provision the subscriber for
    service end-to-end.

    Idempotent: if order.completed_at is already set, this is a no-op and
    returns (subscriber, None).

    Steps:
      1. Resolve the ServicePlan to activate (param > lead.interested_plan).
      2. Flip order.status → COMPLETED, set completed_at, mark lead INSTALLED.
      3. activate_subscription(): expire old, create new Subscription, sync the
         RADIUS group via assign_subscriber_to_plan().
      4. For PPPoE installs, also set the raw radcheck password to the order's
         pppoe_password so MS-CHAPv2 dialing works. (assign_subscriber_to_plan
         skips Cleartext-Password writes for SOURCE_STAFF subscribers.)
      5. Send the "you're connected" message to the subscriber over WA + SMS,
         including PPPoE creds for that mode.
      6. Resolve the linked install ticket (which fires the existing customer
         milestone ping via tickets.services.change_status).
    """
    from plans.services import activate_subscription
    from radius.utils import set_subscriber_radius_password

    lead = order.lead
    subscriber = lead.converted_subscriber
    if subscriber is None:
        raise InstallationCompletionError(
            f'InstallationOrder {order.pk}: lead has no converted_subscriber. '
            'Run convert_lead_to_subscriber first.'
        )

    if order.completed_at:
        return subscriber, None

    plan = plan or lead.interested_plan
    if plan is None:
        raise InstallationCompletionError(
            f'InstallationOrder {order.pk}: no ServicePlan attached. Set '
            'lead.interested_plan or pass plan=... before completing.'
        )

    now = timezone.now()
    order.status = InstallationOrder.STATUS_COMPLETED
    order.completed_at = now
    order.save(update_fields=['status', 'completed_at', 'updated_at'])

    if lead.status != Lead.STATUS_INSTALLED:
        lead.status = Lead.STATUS_INSTALLED
        lead.save(update_fields=['status', 'updated_at'])

    subscription = activate_subscription(subscriber, plan, reseller=order.reseller)

    if order.service_mode == InstallationOrder.SERVICE_MODE_PPPOE and order.pppoe_password:
        set_subscriber_radius_password(subscriber, order.pppoe_password)

    _send_install_complete_handoff(subscriber, order, plan)
    _resolve_install_ticket(order, actor=actor)

    logger.info(
        'Installation %s completed → subscriber %s on plan %s (subscription %s)',
        order.pk, subscriber.pk, plan.pk, subscription.pk,
    )
    return subscriber, subscription


def _send_install_complete_handoff(subscriber, order, plan):
    """Tell the subscriber they're online + how to log in. WA first, SMS fallback."""
    from notifications.notify import send_whatsapp
    from notifications.sms import get_sms_service

    reseller = order.reseller
    name = getattr(subscriber, 'name', '') or order.lead.name or 'there'
    portal_url = _portal_url_for(reseller)

    lines = [
        f"Hi {name}! Your {reseller.name} install is complete and you're online.",
        f"Plan: {plan.name}.",
    ]
    if order.service_mode == InstallationOrder.SERVICE_MODE_PPPOE and order.pppoe_username:
        lines.append(
            f'PPPoE login — username: {order.pppoe_username}  password: {order.pppoe_password}'
        )
    lines.append(f'Manage your account: {portal_url}')
    body = '\n'.join(lines)

    sent = False
    try:
        sent = bool(send_whatsapp(reseller.slug, subscriber.phone, body))
    except Exception as exc:  # pragma: no cover — fire and forget
        logger.warning('install handoff WA send failed for %s: %s', subscriber.phone, exc)
    if not sent:
        try:
            get_sms_service().send_sms(subscriber.phone, body)
        except Exception as exc:  # pragma: no cover
            logger.warning('install handoff SMS send failed for %s: %s', subscriber.phone, exc)


def _portal_url_for(reseller):
    """Best-effort portal URL: branding override > settings.PLATFORM_DOMAIN."""
    from django.conf import settings

    branding = getattr(reseller, 'branding', None) or {}
    if isinstance(branding, dict):
        url = branding.get('portal_url') or branding.get('domain')
        if url:
            return url if url.startswith('http') else f'https://{url}'
    domain = getattr(settings, 'PLATFORM_DOMAIN', None) or 'sabiwifi.ng'
    return f'https://{domain}/portal/'


def _resolve_install_ticket(order, actor='system'):
    """Mark the install ticket linked to this order as resolved."""
    from tickets.models import Ticket
    from tickets.services import change_status

    ticket = (
        Ticket.objects
        .filter(installation_order=order, type=Ticket.TYPE_INSTALL)
        .exclude(status__in=Ticket.TERMINAL_STATUSES)
        .first()
    )
    if ticket is None:
        return
    change_status(ticket, Ticket.STATUS_RESOLVED, actor=actor,
                  note='Installation completed.')
