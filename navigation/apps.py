from django.apps import AppConfig


class NavigationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "navigation"
    verbose_name = "Project Navigation Index"

    def ready(self) -> None:
        from . import signals as _nav_signals

        _nav_signals._register()
