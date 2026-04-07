"""
Signals that keep SabiWiFi entities in sync with OpenWISP.

  Reseller created → create OpenWISP Organization
"""
import logging
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='accounts.Reseller')
def sync_reseller_to_openwisp(sender, instance, created, **kwargs):
    """
    Create a matching OpenWISP Organization when a new Reseller is saved.
    Skips gracefully if OpenWISP is not configured or the org already exists.
    Never raises — OpenWISP unavailability must not block reseller creation.
    """
    if not created:
        return

    # Already has a binding (shouldn't happen on create, but guard anyway)
    if hasattr(instance, 'openwisp_binding'):
        return

    try:
        from integrations.openwisp import OpenWISPClient, OpenWISPError
        from integrations.models import OpenWISPBinding

        client = OpenWISPClient()
        org = client.create_organization(
            name=getattr(instance, 'business_name', None) or str(instance),
            slug=instance.slug,
        )
        OpenWISPBinding.objects.create(
            reseller=instance,
            openwisp_id=org['id'],
            openwisp_type='org',
        )
        logger.info(
            f'OpenWISP org created for reseller {instance.slug}: {org["id"]}'
        )
    except Exception as exc:
        logger.warning(
            f'Could not create OpenWISP org for reseller {getattr(instance, "slug", instance)}: {exc}'
        )
