from django.apps import AppConfig


class PlansConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'plans'
    verbose_name = 'Service Plans'

    def ready(self):
        import plans.signals  # noqa: F401
