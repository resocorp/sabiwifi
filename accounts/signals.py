"""
Reseller lifecycle signals → operator notifications.
"""
import logging

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from accounts.models import Reseller

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Reseller)
def _capture_status_transition(sender, instance, **kwargs):
    """Stash the DB status before save so post_save can detect transitions."""
    if not instance.pk:
        instance._prev_status = None
        return
    try:
        instance._prev_status = sender.objects.only('status').get(pk=instance.pk).status
    except sender.DoesNotExist:
        instance._prev_status = None


@receiver(post_save, sender=Reseller)
def _notify_reseller_events(sender, instance, created, **kwargs):
    try:
        from operator_panel.services.notify_operator import notify
    except Exception:
        return

    if created:
        try:
            notify('new_reseller', {
                'name': instance.name,
                'phone': instance.phone or '—',
                'email': instance.email or '—',
            })
        except Exception as exc:
            logger.warning(f'notify new_reseller failed: {exc}')
        return

    prev = getattr(instance, '_prev_status', None)
    if prev != 'active' and instance.status == 'active':
        try:
            notify('reseller_activated', {
                'name': instance.name,
                'subaccount': instance.paystack_subaccount_code or '—',
            })
        except Exception as exc:
            logger.warning(f'notify reseller_activated failed: {exc}')
