from django.apps import AppConfig


class LongdocConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "longdoc"
    verbose_name = "Long Documents"

    def ready(self) -> None:
        from . import signals  # noqa: F401
