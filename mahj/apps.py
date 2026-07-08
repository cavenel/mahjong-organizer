from django.apps import AppConfig


class MahjConfig(AppConfig):
    name = 'mahj'

    def ready(self):
        from . import signals
        signals.connect_signals()