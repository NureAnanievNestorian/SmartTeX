from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class SmallModelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "small_model"
    verbose_name = "Small Model Control Layer"

    def ready(self) -> None:
        provider = str(getattr(settings, "SMALL_MODEL_PROVIDER", "mock") or "mock").strip().lower()
        if provider == "gemini" and not str(getattr(settings, "GEMINI_API_KEY", "")).strip():
            raise ImproperlyConfigured("GEMINI_API_KEY is required when SMALL_MODEL_PROVIDER='gemini'.")
