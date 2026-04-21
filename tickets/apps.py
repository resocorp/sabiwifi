from django.apps import AppConfig


class TicketsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tickets'
    verbose_name = 'Tickets & SLA'

    def ready(self):
        from tickets import signals  # noqa: F401
