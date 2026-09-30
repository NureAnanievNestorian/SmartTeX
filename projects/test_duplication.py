import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from longdoc.models import (
    AISession, ProjectAnnotation, ProjectContextFile, ProjectLongDocSettings,
    ProjectNoteSection, ProjectOutlineItem, ProjectRequirement, ProjectTask,
    RequirementSectionRef, SectionSummary,
)
from small_model.models import ProjectSmallModelSettings
from templates_lib.models import Template

from .models import Project, ProjectLocalRuntime, ProjectVersion
from .services import project_dir


class ProjectDuplicationTests(TestCase):
    def setUp(self):
        media = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(override_settings(MEDIA_ROOT=Path(media)))
        self.user = User.objects.create_user(username="copy-owner")
        self.client.force_login(self.user)
        self.project = Project.objects.create(
            owner=self.user, title="Лабораторна", markup_type="typst",
            main_file="nested/lab.typ", last_status="success",
            github_repo_url="https://github.com/example/lab",
        )
        # Enable sync without scheduling a real remote operation.
        Project.objects.filter(pk=self.project.pk).update(github_sync_enabled=True)
        self.root = project_dir(self.project)
        self.files = {
            "nested/lab.typ": b'= Original\n#import "/.smarttex/auto_generated/pdf_includes.typ"\n',
            "images/figure.png": b"image-bytes",
            "attachments/report.pdf": b"%PDF attachment",
            ".editorconfig": b"root = true",
            ".smarttex/context/brief.md": b"Project context",
            ".smarttex/pdf_includes.json": b'{"report": {"path": "attachments/report.pdf"}}',
            ".smarttex/auto_generated/pdf_includes.typ": b"#let included = true",
            ".smarttex/cache/pdf-pages/report/page-1.svg": b"<svg/>",
            ".smarttex/main.pdf": b"%PDF preview",
        }
        for name, content in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.root / "empty-folder").mkdir()
        for name in (".smarttex-git/HEAD", ".smarttex-git.corrupt-old/HEAD", ".git/config", ".smarttex/sessions/1/worktree/old.typ"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("history")
        ProjectVersion.objects.create(
            project=self.project, number=7, operation="edit", summary="Old edit",
            before_content="old", after_content="new",
        )
        self.url = f"/api/projects/{self.project.id}/duplicate/"

    def test_copies_files_and_data_remaps_relations_without_history(self):
        # Template content must not overwrite the project's current files.
        template = Template.objects.bulk_create([Template(title="Template", content="old template")])[0]
        self.project.template = template
        self.project.save(update_fields=["template"])
        ProjectLongDocSettings.objects.create(project=self.project, enabled=True, quick_annotation_templates=["Review"])
        ProjectSmallModelSettings.objects.create(project=self.project, nav_repair_enabled=True)
        ProjectLocalRuntime.objects.create(project=self.project, enabled=True, agent_id="source-agent")
        context = ProjectContextFile.objects.create(project=self.project, filename="brief.md", size_bytes=15, is_read_only=False)
        parent = ProjectOutlineItem.objects.create(project=self.project, order=0, title="Chapter", level=1)
        child = ProjectOutlineItem.objects.create(project=self.project, order=1, title="Section", level=2, parent=parent)
        session = AISession.objects.create(
            project=self.project, goal="Old goal", branch_name="old", worktree_path="old",
            status="accepted", expires_at=timezone.now(),
        )
        task = ProjectTask.objects.create(project=self.project, description="Done task", status="done", ai_session=session, completed_at=timezone.now())
        ProjectAnnotation.objects.create(project=self.project, task=task, file_name="nested/lab.typ", instruction="Check", status="done", resolved_by_session=session)
        ProjectNoteSection.objects.create(project=self.project, heading="Notes", body="Keep this", order=2)
        SectionSummary.objects.create(
            project=self.project, section_title="Chapter", source_file="nested/lab.typ",
            content_hash="abc", summary_text="Keep summary", written_by="user", source_version_number=7,
        )
        requirement = ProjectRequirement.objects.create(project=self.project, req_id="R1", description="Requirement", coverage="covered")
        RequirementSectionRef.objects.create(requirement=requirement, section_title="Section", outline_item=child)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 201, response.content)
        copied = Project.objects.get(pk=response.json()["id"])
        self.assertEqual(copied.title, "Копія — Лабораторна")
        self.assertEqual(response.json()["url"], f"/projects/{copied.id}/")
        self.assertEqual(copied.owner, self.user)
        self.assertEqual(copied.template, template)
        self.assertEqual(copied.main_file, "nested/lab.typ")
        self.assertEqual(copied.markup_type, "typst")
        self.assertFalse(copied.github_sync_enabled)
        self.assertEqual(copied.github_repo_url, self.project.github_repo_url)
        copied_root = project_dir(copied)
        for name, content in self.files.items():
            self.assertEqual((copied_root / name).read_bytes(), content, name)
        self.assertTrue((copied_root / "empty-folder").is_dir())
        for name in (".smarttex-git", ".smarttex-git.corrupt-old", ".git", ".smarttex/sessions"):
            self.assertFalse((copied_root / name).exists(), name)
        self.assertFalse(copied.versions.exists())
        self.assertFalse(copied.ai_sessions.exists())
        self.assertFalse(copied.change_proposals.exists())
        self.assertFalse(ProjectLocalRuntime.objects.filter(project=copied).exists())
        self.assertEqual(copied.longdoc_settings.quick_annotation_templates, ["Review"])
        self.assertTrue(copied.small_model_settings.nav_repair_enabled)
        self.assertEqual(copied.context_files.get().filename, context.filename)
        self.assertFalse(copied.context_files.get().is_read_only)
        copied_child = copied.outline_items.get(title="Section")
        self.assertEqual(copied_child.parent.project_id, copied.id)
        self.assertEqual(copied_child.parent.title, "Chapter")
        copied_task = copied.tasks.get()
        self.assertEqual(copied_task.status, "done")
        self.assertIsNone(copied_task.ai_session_id)
        annotation = copied.annotations.get()
        self.assertEqual(annotation.task_id, copied_task.id)
        self.assertIsNone(annotation.resolved_by_session_id)
        self.assertEqual(copied.note_sections.get().body, "Keep this")
        self.assertEqual(copied.section_summaries.get().summary_text, "Keep summary")
        self.assertEqual(copied.section_summaries.get().source_version_number, 0)
        copied_ref = copied.requirements.get().section_refs.get()
        self.assertEqual(copied_ref.outline_item_id, copied_child.id)
        (copied_root / "nested/lab.typ").write_text("Changed copy")
        self.assertEqual((self.root / "nested/lab.typ").read_bytes(), self.files["nested/lab.typ"])
        self.assertEqual(self.project.versions.count(), 1)

    def test_requires_owner_post_and_csrf(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        stranger = User.objects.create_user(username="stranger")
        self.client.force_login(stranger)
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.post(self.url).status_code, 401)
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.user)
        self.assertEqual(strict_client.post(self.url).status_code, 403)
        self.assertEqual(Project.objects.count(), 1)

    def test_failed_copy_rolls_back_record_and_partial_files(self):
        destinations = []

        def fail_copy(source, destination, **kwargs):
            destinations.append(destination)
            (destination / "partial.typ").write_text("partial")
            raise OSError("Copy failed")

        with patch("projects.duplication.shutil.copytree", side_effect=fail_copy):
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Project.objects.count(), 1)
        self.assertFalse(destinations[0].exists())
        self.assertTrue(self.root.exists())

    def test_title_limit_and_latex_files(self):
        self.project.title = "А" * 255
        self.project.markup_type = "latex"
        self.project.main_file = "main.tex"
        self.project.save()
        (self.root / "main.tex").write_text("\\documentclass{article}")
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 201)
        copied = Project.objects.get(pk=response.json()["id"])
        self.assertEqual(len(copied.title), 255)
        self.assertEqual(copied.markup_type, "latex")
        self.assertTrue((project_dir(copied) / "main.tex").exists())

    def test_symlink_does_not_copy_files_outside_project(self):
        (self.root / "linked.typ").symlink_to(self.root / "nested/lab.typ")
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Project.objects.count(), 1)

    def test_existing_destination_is_not_deleted_on_failure(self):
        existing = self.root.parent / str(self.project.id + 1)
        existing.mkdir()
        (existing / "keep.typ").write_text("Keep existing data")
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 400)
        self.assertEqual((existing / "keep.typ").read_text(), "Keep existing data")
        self.assertEqual(Project.objects.count(), 1)
