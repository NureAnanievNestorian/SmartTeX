from django.apps import AppConfig


class TemplatesLibConfig(AppConfig):
    name = 'templates_lib'

    def ready(self) -> None:
        import templates_lib.signals  # noqa: F401
