from django.apps import AppConfig


class Sommc2018Config(AppConfig):
    name = 'SOMMC2018'
    
    def ready(self):
        from . import signals
        signals.connect_signals()