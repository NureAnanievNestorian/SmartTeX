from django.apps import AppConfig


class SmallModelAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "small_model"
    verbose_name = "Small Model Control Layer"
