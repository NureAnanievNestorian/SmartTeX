from django.db.models.signals import post_save
from django.dispatch import receiver

from projects.models import ProjectVersion

from .services import mark_summaries_stale_for_version


@receiver(post_save, sender=ProjectVersion)
def mark_longdoc_summaries_stale_on_version(sender, instance: ProjectVersion, created: bool, **kwargs) -> None:
    if not created:
        return
    mark_summaries_stale_for_version(instance)
