"""
Conversation + Message write-path helpers.

All mutation goes through these functions so future AI agents, webhook
handlers, and human-reply views share the same bookkeeping (unread counts,
last_message_at, state transitions, delivery-receipt correlation).
"""
from django.db import transaction
from django.utils import timezone

from conversations.models import Conversation, Message


def normalise_inbound_phone(raw_phone):
    """
    Convert the raw international phone WhatsApp sends us (e.g. '2348012345678')
    to local format using whichever active Country matches. Returns the
    normalised local phone or '' if no country matches.
    """
    from accounts.models import Country
    if not raw_phone:
        return ''
    # Try intl format with +
    candidates = [raw_phone, '+' + raw_phone]
    for country in Country.objects.filter(is_active=True).order_by('sort_order'):
        for candidate in candidates:
            local = country.normalize_to_local(candidate)
            if local:
                return local
    return ''


def _link_existing_contact(conversation):
    """
    Best-effort link conversation.contact_phone to an existing Subscriber
    (if any). Does NOT create anything — just associates.
    """
    if conversation.subscriber_id or not conversation.contact_phone:
        return
    from accounts.models import Subscriber
    subscriber = Subscriber.objects.filter(
        reseller=conversation.reseller, phone=conversation.contact_phone,
    ).first()
    if subscriber:
        conversation.subscriber = subscriber


@transaction.atomic
def record_inbound_message(
    *, reseller, channel, external_thread_id, body, attachments,
    external_message_id, sender_phone='', display_name='', timestamp=None,
):
    """
    Idempotently record an inbound customer message.

    Creates (or reuses) a Conversation and appends the Message. Deduplicates
    by external_message_id so the same provider retry doesn't produce two
    rows. Updates conversation counters + timestamps. Returns the Message
    (or None if it was a duplicate).
    """
    ts = timestamp or timezone.now()

    # Dedupe: if we've already recorded this provider-side id in this reseller's
    # conversations, skip. We scope to the reseller to avoid one bad id
    # colliding across tenants.
    if external_message_id:
        existing = Message.objects.filter(
            conversation__reseller=reseller,
            external_message_id=external_message_id,
            direction=Message.DIRECTION_IN,
        ).first()
        if existing:
            return None

    contact_phone = normalise_inbound_phone(sender_phone) or sender_phone

    conversation, created = Conversation.objects.select_for_update().get_or_create(
        reseller=reseller,
        channel=channel,
        external_thread_id=external_thread_id,
        defaults={
            'contact_phone': contact_phone,
            'contact_display_name': display_name,
            'last_message_at': ts,
            'last_inbound_at': ts,
            'unread_count': 1,
        },
    )

    if created:
        _link_existing_contact(conversation)
        conversation.save()
    else:
        # Keep contact fields fresh if we now know more
        if contact_phone and not conversation.contact_phone:
            conversation.contact_phone = contact_phone
        if display_name and not conversation.contact_display_name:
            conversation.contact_display_name = display_name
        if not conversation.subscriber_id and conversation.contact_phone:
            _link_existing_contact(conversation)
        conversation.last_message_at = ts
        conversation.last_inbound_at = ts
        conversation.unread_count = (conversation.unread_count or 0) + 1
        # A resolved conversation becomes open again on new inbound
        if conversation.state == Conversation.STATE_RESOLVED:
            conversation.state = Conversation.STATE_OPEN
        conversation.save()

    msg = Message.objects.create(
        conversation=conversation,
        direction=Message.DIRECTION_IN,
        body=body or '',
        attachments=attachments or [],
        external_message_id=external_message_id or '',
        sender_phone=contact_phone,
        source=Message.SOURCE_CUSTOMER,
        created_at=ts,
    )

    # Fire the inbound router as an RQ job (noop if reseller hasn't opted in).
    # Wrapped in try/except + late-import so a broken ai/ config can never
    # block the inbound write path.
    try:
        if conversation.ai_enabled and hasattr(reseller, 'ai_config'):
            from ai.jobs import enqueue_inbound_router
            transaction.on_commit(lambda mid=msg.pk: enqueue_inbound_router(mid))
    except Exception:
        pass

    return msg


@transaction.atomic
def record_outbound_message(
    *, conversation, body, source, attachments=None, sent_by=None,
    external_message_id='', delivery_status=Message.DELIVERY_PENDING,
    agent_run_id=None,
):
    """
    Append an outbound message and bump the conversation's outbound counters.
    The actual wire-send (WA sidecar / SMS / email) is triggered by the
    caller — this function only records durable state.
    """
    now = timezone.now()
    msg = Message.objects.create(
        conversation=conversation,
        direction=Message.DIRECTION_OUT,
        body=body or '',
        attachments=attachments or [],
        source=source,
        external_message_id=external_message_id or '',
        delivery_status=delivery_status,
        sent_by=sent_by,
        agent_run_id=agent_run_id,
        created_at=now,
    )
    conversation.last_message_at = now
    conversation.last_outbound_at = now
    # Any outbound by a human marks the thread as read
    if source == Message.SOURCE_HUMAN:
        conversation.unread_count = 0
    conversation.save(update_fields=['last_message_at', 'last_outbound_at', 'unread_count'])
    return msg
