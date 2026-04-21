"""
RQ-enqueued wrappers around notifications.notify so callers can fire-and-forget
from request handlers while the actual outbound send (WA/SMS/email) runs in
the worker with retry / dead-letter-queue semantics.

Usage:
    from notifications.jobs import enqueue_notify_subscriber
    enqueue_notify_subscriber(subscriber.pk, 'welcome', {'plan': 'Basic'})
"""
import logging

import django_rq

logger = logging.getLogger(__name__)


def _notify_subscriber_job(subscriber_id, event_type, context):
    from accounts.models import Subscriber
    from notifications.notify import notify_subscriber
    try:
        subscriber = Subscriber.objects.get(pk=subscriber_id)
    except Subscriber.DoesNotExist:
        logger.warning(f'notify job: subscriber {subscriber_id} gone')
        return
    notify_subscriber(subscriber, event_type, context or {})


def _notify_reseller_job(reseller_id, event_type, context):
    from accounts.models import Reseller
    from notifications.notify import notify_reseller
    try:
        reseller = Reseller.objects.get(pk=reseller_id)
    except Reseller.DoesNotExist:
        return
    notify_reseller(reseller, event_type, context or {})


def enqueue_notify_subscriber(subscriber_id, event_type, context=None):
    queue = django_rq.get_queue('default')
    return queue.enqueue(_notify_subscriber_job, subscriber_id, event_type, context or {})


def enqueue_notify_reseller(reseller_id, event_type, context=None):
    queue = django_rq.get_queue('default')
    return queue.enqueue(_notify_reseller_job, reseller_id, event_type, context or {})
