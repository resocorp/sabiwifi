"""
Sync RADIUS group attributes when a ServicePlan is created or updated.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver
from plans.models import ServicePlan


@receiver(post_save, sender=ServicePlan)
def sync_radius_groups(sender, instance, **kwargs):
    """Write RADIUS group reply/check attributes when a plan is saved."""
    from radius.utils import sync_plan_to_radius
    sync_plan_to_radius(instance)
