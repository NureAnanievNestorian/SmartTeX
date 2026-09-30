"""Copy a project's current files and document data without its history."""

import shutil
from pathlib import Path

from django.db import transaction

from longdoc.models import (
    ProjectContextFile, ProjectLongDocSettings, ProjectNoteSection,
)
from small_model.models import ProjectSmallModelSettings

from .models import Project
from .services import project_dir


def _copy_row(row, **overrides):
    values = {
        field.attname: getattr(row, field.attname)
        for field in row._meta.concrete_fields
        if not field.primary_key and not getattr(field, "auto_now", False)
        and not getattr(field, "auto_now_add", False)
    }
    values.update(overrides)
    return type(row).objects.create(**values)


def duplicate_project(source: Project) -> Project:
    destination = None
    destination_root = None

    def ignore_history(directory, names):
        path = Path(directory)
        ignored = {
            name for name in names
            if name == ".git" or name.startswith(".smarttex-git")
        }
        if path == project_dir(source) / ".smarttex":
            ignored.add("sessions")
        for name in set(names) - ignored:
            if (path / name).is_symlink():
                raise ValueError("Не вдалося скопіювати проєкт: він містить символічні посилання.")
        return ignored

    try:
        with transaction.atomic():
            destination = Project.objects.create(
                owner=source.owner,
                title=("Копія — " + source.title)[:255],
                template=source.template,
                markup_type=source.markup_type,
                main_file=source.main_file,
                last_status=source.last_status,
                github_repo_url=source.github_repo_url,
                github_sync_interval_minutes=source.github_sync_interval_minutes,
                github_sync_enabled=False,
            )
            source_root = project_dir(source)
            if not source_root.is_dir() or source_root.is_symlink():
                raise ValueError("Файли оригінального проєкту не знайдено.")
            root = project_dir(destination)
            root.mkdir(parents=True, exist_ok=False)
            destination_root = root
            shutil.copytree(source_root, root, ignore=ignore_history, dirs_exist_ok=True)

            for model in (
                ProjectLongDocSettings, ProjectSmallModelSettings,
                ProjectContextFile, ProjectNoteSection,
            ):
                for row in model.objects.filter(project=source):
                    _copy_row(row, project_id=destination.id)

            outline_map = {}
            outline = list(source.outline_items.all())
            for row in outline:
                outline_map[row.id] = _copy_row(row, project_id=destination.id, parent_id=None)
            for row in outline:
                if row.parent_id in outline_map:
                    copied = outline_map[row.id]
                    copied.parent = outline_map[row.parent_id]
                    copied.save(update_fields=["parent"])

            task_map = {
                row.id: _copy_row(row, project_id=destination.id, ai_session_id=None)
                for row in source.tasks.all()
            }
            for row in source.annotations.all():
                task = task_map.get(row.task_id)
                _copy_row(
                    row, project_id=destination.id,
                    task_id=task.id if task else None, resolved_by_session_id=None,
                )
            for row in source.section_summaries.all():
                _copy_row(row, project_id=destination.id, source_version_number=0)
            for row in source.requirements.all():
                requirement = _copy_row(row, project_id=destination.id)
                for ref in row.section_refs.all():
                    outline_item = outline_map.get(ref.outline_item_id)
                    _copy_row(
                        ref, requirement_id=requirement.id,
                        outline_item_id=outline_item.id if outline_item else None,
                    )
        return destination
    except Exception:
        if destination_root is not None:
            shutil.rmtree(destination_root, ignore_errors=True)
        raise
