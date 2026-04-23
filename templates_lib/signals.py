import threading

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Template
from .services import compile_template_preview


def _compile_in_background(template_id: int) -> None:
    try:
        template = Template.objects.get(pk=template_id)
    except Exception:
        return
    try:
        compile_template_preview(template)
    except Exception:
        return


@receiver(post_save, sender=Template)
def on_template_saved(sender, instance: Template, **kwargs) -> None:
    if not instance.is_active:
        return
    t = threading.Thread(target=_compile_in_background, args=(instance.pk,), daemon=True)
    t.start()
