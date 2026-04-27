from django.apps import AppConfig


class MahjConfig(AppConfig):
    name = 'mahj'
    # Keep the original Django app_label so the existing migration history,
    # DB table prefixes (SOMMC2018_*), and content_type rows stay valid.
    label = 'SOMMC2018'

    def ready(self):
        from . import signals
        signals.connect_signals()