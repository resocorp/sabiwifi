from django.db import models


class OpenWISPBinding(models.Model):
    """
    Maps SabiWiFi entities (Reseller or Router) to their OpenWISP UUIDs.

    Exactly one of reseller / router must be set (enforced by DB constraint).
    Created automatically:
      - reseller → org:    via post_save signal in integrations/signals.py
      - router   → device: in dashboard/views.py::router_add when a reseller claims it
    """
    BINDING_TYPES = [
        ('org', 'Organization'),
        ('device', 'Device'),
    ]

    reseller = models.OneToOneField(
        'accounts.Reseller',
        null=True, blank=True,
        on_delete=models.CASCADE,
        related_name='openwisp_binding',
    )
    router = models.OneToOneField(
        'routers.Router',
        null=True, blank=True,
        on_delete=models.CASCADE,
        related_name='openwisp_binding',
    )
    openwisp_id = models.UUIDField(
        help_text='UUID of the corresponding object in OpenWISP',
    )
    openwisp_type = models.CharField(max_length=10, choices=BINDING_TYPES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(reseller__isnull=False, router__isnull=True) |
                    models.Q(reseller__isnull=True, router__isnull=False)
                ),
                name='openwisp_binding_exactly_one_entity',
            ),
        ]

    def __str__(self):
        entity = self.reseller or self.router
        return f'{entity} → OpenWISP {self.openwisp_type} {self.openwisp_id}'
