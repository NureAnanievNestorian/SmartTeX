"""Management command: commit any on-disk project files that are not yet tracked by git.

Safe to run multiple times.  Per-project errors are logged and skipped so a single
bad project does not abort the whole run.

Usage:
    python manage.py backfill_project_git
    python manage.py backfill_project_git --project-id 42        # single project
    python manage.py backfill_project_git --dry-run              # show what would be committed
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Commit untracked project files into each project's git repository."

    def add_arguments(self, parser):
        parser.add_argument(
            "--project-id",
            type=int,
            default=None,
            help="Only process this project ID.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print what would be committed without actually committing.",
        )

    def handle(self, *args, **options):
        from projects.models import Project
        from projects.services import (
            _git_executable,
            _project_git_is_healthy,
            _run_project_git,
            ensure_project_git_repo,
            list_git_trackable_files,
            project_dir,
        )

        dry_run: bool = options["dry_run"]
        project_id: int | None = options["project_id"]

        if not _git_executable():
            self.stderr.write("git is not available — nothing to do.")
            return

        qs = Project.objects.all().order_by("id")
        if project_id is not None:
            qs = qs.filter(id=project_id)

        total = qs.count()
        self.stdout.write(f"Processing {total} project(s){' [DRY RUN]' if dry_run else ''}…")

        committed = skipped = errors = 0

        for project in qs.iterator():
            pdir = project_dir(project)
            if not pdir.exists():
                self.stdout.write(f"  [{project.id}] No directory — skip")
                skipped += 1
                continue

            try:
                ensure_project_git_repo(project)

                if not _project_git_is_healthy(project):
                    self.stdout.write(f"  [{project.id}] Git repo unhealthy — skip")
                    skipped += 1
                    continue

                trackable = list_git_trackable_files(project)
                if not trackable:
                    skipped += 1
                    continue

                # Ask git which of the trackable files are not yet committed.
                status_proc = _run_project_git(
                    project,
                    ["status", "--short", "--", *trackable],
                    check=False,
                )
                untracked_lines = [
                    line for line in (status_proc.stdout or "").splitlines()
                    if line.strip()
                ]
                if not untracked_lines:
                    skipped += 1
                    continue

                self.stdout.write(
                    f"  [{project.id}] {len(untracked_lines)} file(s) to commit:"
                )
                for line in untracked_lines:
                    self.stdout.write(f"    {line}")

                if dry_run:
                    skipped += 1
                    continue

                _run_project_git(project, ["add", "-A", "--", *trackable])
                _run_project_git(
                    project,
                    [
                        "commit", "--quiet", "-m",
                        "chore: backfill untracked project files\n\noperation: backfill_project_git\nsource: api",
                        "--", *trackable,
                    ],
                )
                self.stdout.write(self.style.SUCCESS(f"  [{project.id}] committed"))
                committed += 1

            except Exception as exc:
                self.stderr.write(f"  [{project.id}] ERROR: {exc}")
                logger.exception("backfill_project_git failed for project %s", project.id)
                errors += 1

        self.stdout.write(
            f"\nDone. committed={committed} skipped={skipped} errors={errors}"
        )
