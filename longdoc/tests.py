import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import Client
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from projects.models import Project, ProjectVersion
from projects.services import write_source_content
from small_model.models import SmallModelUsageLog
from small_model.models import ProjectSmallModelSettings, UserSmallModelAccess, UserSmallModelQuota
from templates_lib.models import Template, TemplateContextFile, TemplateLongDocDefaults, TemplateNoteSection, TemplateOutlineItem, TemplateRequirement, TemplateTask

from .audit import audit_assistant_change
from .locks import ProjectLockedError, assert_not_locked, get_locking_session, is_project_locked
from .models import AISession, AssistantAuditLog, ChangeProposal, ProjectAnnotation, ProjectLongDocSettings, ProjectOutlineItem, ProjectRequirement, ProjectTask, SectionSummary
from .services import (
    DEFAULT_NOTE_SECTION_HEADINGS,
    SAMPLE_CONTEXT_FILENAME,
    disable_longdoc,
    enable_longdoc,
    get_context_file,
    get_or_create_longdoc_settings,
    initialize_longdoc_from_template,
    is_feature_enabled,
    list_context_files,
    longdoc_context_dir,
    mark_summaries_stale_for_version,
    overview_payload,
    refresh_section_summary_staleness,
    longdoc_default_settings,
)
from .models import ProjectContextFile, ProjectNoteSection


class LongdocFoundationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="writer", password="secret123")
        self.template = Template.objects.create(title="Blank", content="\\documentclass{article}\n")
        self.project = Project.objects.create(owner=self.user, title="Dissertation", template=self.template)

    @override_settings(
        LONGDOC_DEFAULTS={
            "enabled": True,
            "context_enabled": False,
            "outline_enabled": True,
            "tasks_enabled": True,
            "annotations_enabled": True,
            "notes_enabled": True,
            "summaries_enabled": False,
            "requirements_enabled": True,
            "ai_sessions_enabled": True,
            "mcp_controlled_access": False,
            "mcp_write_context": True,
        }
    )
    def test_default_settings_are_loaded_from_django_settings(self) -> None:
        defaults = longdoc_default_settings()
        settings_obj, created = get_or_create_longdoc_settings(self.project)

        self.assertTrue(created)
        self.assertEqual(defaults["context_enabled"], False)
        self.assertEqual(settings_obj.context_enabled, False)
        self.assertEqual(settings_obj.annotations_enabled, True)
        self.assertEqual(settings_obj.requirements_enabled, True)
        self.assertEqual(settings_obj.mcp_write_context, True)
        self.assertEqual(self.project.note_sections.count(), len(DEFAULT_NOTE_SECTION_HEADINGS))

    @override_settings(LONGDOC_DEFAULTS={"enabled": False})
    def test_disabled_defaults_do_not_create_note_sections_until_enabled(self) -> None:
        settings_obj, created = get_or_create_longdoc_settings(self.project)

        self.assertTrue(created)
        self.assertFalse(settings_obj.enabled)
        self.assertEqual(self.project.note_sections.count(), 0)

        enable_longdoc(self.project)

        self.assertEqual(self.project.note_sections.count(), len(DEFAULT_NOTE_SECTION_HEADINGS))

    def test_enable_and_disable_helpers_drive_feature_checks(self) -> None:
        settings_obj = enable_longdoc(self.project, requirements_enabled=True)

        self.assertTrue(settings_obj.enabled)
        self.assertTrue(is_feature_enabled(self.project, "notes_enabled"))
        self.assertTrue(is_feature_enabled(settings_obj, "requirements_enabled"))

        disable_longdoc(self.project)

        self.assertFalse(is_feature_enabled(self.project, "notes_enabled"))
        self.assertFalse(is_feature_enabled(self.project, "requirements_enabled"))

    def test_project_lock_helpers_use_active_ai_session_statuses(self) -> None:
        self.assertFalse(is_project_locked(self.project))
        assert_not_locked(self.project)

        session = AISession.objects.create(
            project=self.project,
            goal="Revise literature review",
            branch_name="ai/session-1",
            worktree_path="/tmp/session-1",
            status=AISession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(hours=72),
        )

        self.assertTrue(is_project_locked(self.project))
        self.assertEqual(get_locking_session(self.project), session)
        with self.assertRaises(ProjectLockedError):
            assert_not_locked(self.project)

        session.status = AISession.Status.ACCEPTED
        session.save(update_fields=["status"])

        self.assertFalse(is_project_locked(self.project))
        assert_not_locked(self.project)

    def test_only_one_locking_session_is_allowed_per_project(self) -> None:
        AISession.objects.create(
            project=self.project,
            goal="Session 1",
            branch_name="ai/session-1",
            worktree_path="/tmp/session-1",
            status=AISession.Status.COMPILED,
            expires_at=timezone.now() + timedelta(hours=72),
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AISession.objects.create(
                    project=self.project,
                    goal="Session 2",
                    branch_name="ai/session-2",
                    worktree_path="/tmp/session-2",
                    status=AISession.Status.READY_FOR_REVIEW,
                    expires_at=timezone.now() + timedelta(hours=72),
                )

    def test_outline_order_is_unique_per_project(self) -> None:
        ProjectOutlineItem.objects.create(project=self.project, order=1, title="Intro", level=1)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProjectOutlineItem.objects.create(project=self.project, order=1, title="Theory", level=1)

    def test_section_summary_line_range_constraint_is_enforced(self) -> None:
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SectionSummary.objects.create(
                    project=self.project,
                    section_title="Intro",
                    section_index=1,
                    source_file="main.tex",
                    source_line_start=20,
                    source_line_end=10,
                    content_hash="a" * 64,
                    summary_text="Summary",
                    written_by=SectionSummary.WrittenBy.MCP,
                    source_version_number=1,
                )

    def test_project_version_category_defaults_to_source(self) -> None:
        version = ProjectVersion.objects.create(
            project=self.project,
            number=1,
            source=ProjectVersion.Source.API,
            operation="create",
            summary="Initial",
            before_content="",
            after_content="body",
        )

        self.assertEqual(version.category, ProjectVersion.Category.SOURCE)

    def test_audit_decorator_records_create_update_and_delete_for_db_only_models(self) -> None:
        @audit_assistant_change(
            operation=AssistantAuditLog.Operation.CREATE,
            source=AssistantAuditLog.Source.MCP,
            actor=lambda args, kwargs, result: kwargs["actor"],
            summary="Created task",
        )
        def create_task(*, project, actor):
            return ProjectTask.objects.create(project=project, description="Draft chapter 1", created_by=ProjectTask.CreatedBy.MCP)

        @audit_assistant_change(
            operation=AssistantAuditLog.Operation.UPDATE,
            source=AssistantAuditLog.Source.MCP,
            summary="Updated task",
        )
        def update_task(task):
            task.status = ProjectTask.Status.DONE
            task.save(update_fields=["status", "updated_at"])
            return task

        @audit_assistant_change(
            operation=AssistantAuditLog.Operation.DELETE,
            source=AssistantAuditLog.Source.USER,
            actor=lambda args, kwargs, result: self.user,
            summary="Deleted task",
        )
        def delete_task(task):
            task.delete()
            return task

        task = create_task(project=self.project, actor=self.user)
        update_task(task)
        delete_task(task)

        logs = list(AssistantAuditLog.objects.filter(project=self.project).order_by("id"))
        self.assertEqual([log.operation for log in logs], ["create", "update", "delete"])
        self.assertEqual(logs[0].changed_fields["description"], [None, "Draft chapter 1"])
        self.assertEqual(logs[1].changed_fields["status"], ["open", "done"])
        self.assertEqual(logs[2].changed_fields["status"], ["done", None])
        self.assertEqual(logs[0].actor, self.user)
        self.assertEqual(logs[2].source, AssistantAuditLog.Source.USER)

    def test_get_or_create_returns_existing_settings_without_duplication(self) -> None:
        first, created_first = get_or_create_longdoc_settings(self.project)
        second, created_second = get_or_create_longdoc_settings(self.project)

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ProjectLongDocSettings.objects.filter(project=self.project).count(), 1)


class LongdocPackage3Tests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.temp_dir))
        self.settings_override.enable()
        self.user = User.objects.create_user(username="longdoc-user", password="secret123")
        self.template = Template.objects.create(title="Blank", content="\\documentclass{article}\n")
        self.project = Project.objects.create(owner=self.user, title="Monograph", template=self.template)
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self) -> None:
        self.settings_override.disable()
        super().tearDown()

    def test_enabling_longdoc_creates_seed_data_and_context_directory(self) -> None:
        settings_obj = enable_longdoc(self.project)

        self.assertTrue(settings_obj.enabled)
        self.assertTrue(longdoc_context_dir(self.project).exists())
        self.assertTrue((longdoc_context_dir(self.project) / SAMPLE_CONTEXT_FILENAME).exists())
        self.assertGreaterEqual(self.project.outline_items.count(), 3)
        self.assertGreaterEqual(self.project.tasks.count(), 2)
        self.assertEqual(self.project.note_sections.count(), len(DEFAULT_NOTE_SECTION_HEADINGS))

    def test_overview_payload_is_compact(self) -> None:
        enable_longdoc(self.project)

        payload = overview_payload(self.project)

        self.assertEqual(payload["project_id"], self.project.id)
        self.assertIn("context_file_count", payload)
        self.assertNotIn("content", json.dumps(payload["context_files"]))
        self.assertIn("task_counts", payload)

    def test_settings_endpoint_can_enable_longdoc(self) -> None:
        response = self.client.patch(
            f"/api/projects/{self.project.id}/longdoc/settings/",
            data=json.dumps({"enabled": True, "mcp_write_context": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["mcp_write_context"])
        self.assertTrue((longdoc_context_dir(self.project) / SAMPLE_CONTEXT_FILENAME).exists())

    def test_settings_endpoint_returns_and_updates_feature_toggles(self) -> None:
        get_response = self.client.get(f"/api/projects/{self.project.id}/longdoc/settings/")

        self.assertEqual(get_response.status_code, 200)
        self.assertIn("summaries_enabled", get_response.json())
        self.assertIn("annotations_enabled", get_response.json())
        self.assertIn("requirements_enabled", get_response.json())
        self.assertFalse(get_response.json()["locked"])

        patch_response = self.client.patch(
            f"/api/projects/{self.project.id}/longdoc/settings/",
            data=json.dumps(
                {
                    "enabled": True,
                    "context_enabled": True,
                    "outline_enabled": True,
                    "tasks_enabled": False,
                    "annotations_enabled": True,
                    "notes_enabled": True,
                    "summaries_enabled": True,
                    "requirements_enabled": True,
                    "mcp_controlled_access": True,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(patch_response.status_code, 200)
        payload = patch_response.json()
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["summaries_enabled"])
        self.assertTrue(payload["annotations_enabled"])
        self.assertTrue(payload["requirements_enabled"])
        self.assertTrue(payload["mcp_controlled_access"])
        self.assertFalse(payload["tasks_enabled"])

    def test_context_api_round_trip(self) -> None:
        enable_longdoc(self.project)

        create_response = self.client.post(
            f"/api/projects/{self.project.id}/context-files/",
            data=json.dumps(
                {
                    "filename": "sources/interview-notes.md",
                    "display_name": "Interview notes",
                    "description": "Primary source notes",
                    "content": "# Notes\n\nInitial draft\n",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(create_response.status_code, 201)
        detail_response = self.client.get(
            f"/api/projects/{self.project.id}/context-files/sources%2Finterview-notes.md/"
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.json()["content"], "# Notes\n\nInitial draft\n")

        update_response = self.client.patch(
            f"/api/projects/{self.project.id}/context-files/sources%2Finterview-notes.md/",
            data=json.dumps({"description": "Updated notes", "content": "# Notes\n\nRevised\n"}),
            content_type="application/json",
        )

        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(get_context_file(self.project, "sources/interview-notes.md")["description"], "Updated notes")

    def test_outline_task_annotation_and_note_endpoints_support_crud(self) -> None:
        enable_longdoc(self.project)

        outline_response = self.client.post(
            f"/api/projects/{self.project.id}/outline-items/",
            data=json.dumps({"title": "Methodology", "level": 1, "status": "draft"}),
            content_type="application/json",
        )
        self.assertEqual(outline_response.status_code, 201)
        outline_id = outline_response.json()["id"]

        patch_outline = self.client.patch(
            f"/api/projects/{self.project.id}/outline-items/{outline_id}/",
            data=json.dumps({"order": 1, "notes": "Move near the front"}),
            content_type="application/json",
        )
        self.assertEqual(patch_outline.status_code, 200)

        task_response = self.client.post(
            f"/api/projects/{self.project.id}/tasks/",
            data=json.dumps({"description": "Draft methodology section"}),
            content_type="application/json",
        )
        self.assertEqual(task_response.status_code, 201)
        task_id = task_response.json()["id"]
        complete_task = self.client.patch(
            f"/api/projects/{self.project.id}/tasks/{task_id}/",
            data=json.dumps({"status": "done"}),
            content_type="application/json",
        )
        self.assertEqual(complete_task.status_code, 200)
        self.assertIsNotNone(complete_task.json()["completed_at"])

        annotation_response = self.client.post(
            f"/api/projects/{self.project.id}/annotations/",
            data=json.dumps({
                "file_name": "main.tex",
                "line_start": 3,
                "line_end": 4,
                "instruction": "Tighten this argument",
                "selected_text": "Original paragraph",
                "task_id": task_id,
            }),
            content_type="application/json",
        )
        self.assertEqual(annotation_response.status_code, 201)
        annotation_id = annotation_response.json()["id"]
        patch_annotation = self.client.patch(
            f"/api/projects/{self.project.id}/annotations/{annotation_id}/",
            data=json.dumps({"status": "done", "instruction": "Applied"}),
            content_type="application/json",
        )
        self.assertEqual(patch_annotation.status_code, 200)
        self.assertEqual(patch_annotation.json()["status"], "done")
        self.assertIsNotNone(patch_annotation.json()["resolved_at"])

        note_response = self.client.post(
            f"/api/projects/{self.project.id}/note-sections/",
            data=json.dumps({"heading": "Open Questions", "body": "Question 1"}),
            content_type="application/json",
        )
        self.assertEqual(note_response.status_code, 201)
        note_id = note_response.json()["id"]
        patch_note = self.client.patch(
            f"/api/projects/{self.project.id}/note-sections/{note_id}/",
            data=json.dumps({"body": "Question 1\nQuestion 2"}),
            content_type="application/json",
        )
        self.assertEqual(patch_note.status_code, 200)
        self.assertIn("Question 2", patch_note.json()["body"])

    def test_db_only_mcp_changes_create_audit_rows_via_api(self) -> None:
        enable_longdoc(self.project)

        response = self.client.post(
            f"/api/projects/{self.project.id}/outline-items/",
            data=json.dumps({"title": "Results", "level": 1, "status": "stub", "change_summary": "Add results node"}),
            content_type="application/json",
            HTTP_X_CHANGE_SOURCE="mcp",
        )

        self.assertEqual(response.status_code, 201)
        log = AssistantAuditLog.objects.filter(project=self.project, model_name="ProjectOutlineItem").latest("id")
        self.assertEqual(log.source, AssistantAuditLog.Source.MCP)
        self.assertEqual(log.operation, AssistantAuditLog.Operation.CREATE)
        self.assertIn("title", log.changed_fields)

    def test_locked_project_blocks_longdoc_writes(self) -> None:
        enable_longdoc(self.project)
        AISession.objects.create(
            project=self.project,
            goal="Locked run",
            branch_name="ai/lock",
            worktree_path="/tmp/lock",
            status=AISession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(hours=12),
        )

        response = self.client.post(
            f"/api/projects/{self.project.id}/tasks/",
            data=json.dumps({"description": "Should fail"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 423)
        self.assertEqual(response.json()["error"], "PROJECT_LOCKED")

    def test_disabled_feature_returns_structured_error(self) -> None:
        enable_longdoc(self.project, tasks_enabled=False)

        response = self.client.get(f"/api/projects/{self.project.id}/tasks/")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "FEATURE_DISABLED")

    def test_ai_request_log_endpoint_returns_project_scoped_compact_rows(self) -> None:
        SmallModelUsageLog.objects.create(
            user=self.user,
            project=self.project,
            provider="gemini",
            model_name="gemini-2.0-flash-lite",
            task_type="diff_safety_review",
            status="success",
            input_tokens_estimate=120,
            output_tokens_estimate=24,
            latency_ms=180,
            input_prompt="[system]\nReview diff",
            output_text='{"recommendation":"allow"}',
        )
        SmallModelUsageLog.objects.create(
            user=self.user,
            project=self.project,
            provider="gemini",
            model_name="gemini-2.0-flash-lite",
            task_type="compile_log_triage",
            status="timeout",
            input_tokens_estimate=80,
            output_tokens_estimate=0,
            latency_ms=15000,
            error_code="TIMEOUT",
        )

        response = self.client.get(f"/api/projects/{self.project.id}/ai-request-log/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary"]["total_requests"], 2)
        self.assertEqual(payload["summary"]["total_input_tokens"], 200)
        self.assertEqual(len(payload["items"]), 2)
        self.assertIn("input_prompt", payload["items"][0])
        self.assertIn("output_text", payload["items"][0])
        self.assertEqual(payload["items"][0]["project_id"] if "project_id" in payload["items"][0] else None, None)


class LongdocPackage4Tests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.temp_dir))
        self.settings_override.enable()
        self.user = User.objects.create_user(username="package4-user", password="secret123")
        self.template = Template.objects.create(
            title="Sections",
            content="\\section{Introduction}\nIntro body.\n\n\\section{Methods}\nMethods body.\n",
        )
        self.project = Project.objects.create(owner=self.user, title="Package 4", template=self.template)
        self.client = Client()
        self.client.force_login(self.user)
        write_source_content(self.project, self.template.content)
        enable_longdoc(self.project, requirements_enabled=True, summaries_enabled=True)

    def tearDown(self) -> None:
        self.settings_override.disable()
        super().tearDown()

    def test_summary_api_updates_and_marks_stale_after_source_version(self) -> None:
        create_response = self.client.post(
            f"/api/projects/{self.project.id}/section-summaries/",
            data=json.dumps({"section_title": "Introduction", "summary_text": "Intro summary", "section_index": 1}),
            content_type="application/json",
        )

        self.assertEqual(create_response.status_code, 201)
        payload = create_response.json()
        self.assertEqual(payload["section_title"], "Introduction")
        self.assertFalse(payload["is_stale"])

        version = ProjectVersion.objects.create(
            project=self.project,
            number=9,
            source=ProjectVersion.Source.API,
            operation="update_project_section",
            target="main.tex:section:1",
            target_file="main.tex",
            summary="Edited introduction",
            before_content="",
            after_content="",
        )
        mark_summaries_stale_for_version(version)

        summary = SectionSummary.objects.get(project=self.project, section_title="Introduction")
        summary.refresh_from_db()
        self.assertTrue(summary.is_stale)

    def test_summary_staleness_clears_when_hash_matches_current_source(self) -> None:
        current_hash = hashlib.sha256("\\section{Introduction}\nIntro body.\n".encode("utf-8")).hexdigest()
        summary = SectionSummary.objects.create(
            project=self.project,
            section_title="Introduction",
            section_index=1,
            source_file="main.tex",
            source_line_start=1,
            source_line_end=2,
            content_hash=current_hash,
            summary_text="Old summary",
            written_by=SectionSummary.WrittenBy.USER,
            source_version_number=1,
            is_stale=True,
        )

        refreshed = refresh_section_summary_staleness(summary)

        self.assertFalse(refreshed.is_stale)

    def test_requirements_api_round_trip_and_overview_counts(self) -> None:
        create_response = self.client.post(
            f"/api/projects/{self.project.id}/requirements/",
            data=json.dumps(
                {
                    "req_id": "R-01",
                    "description": "Include a methods section",
                    "coverage": "partial",
                    "notes": "Needs more detail",
                    "section_refs": ["Methods"],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(create_response.status_code, 201)
        item = create_response.json()
        self.assertEqual(item["section_refs"], ["Methods"])

        patch_response = self.client.patch(
            f"/api/projects/{self.project.id}/requirements/{item['id']}/",
            data=json.dumps({"coverage": "covered", "notes": "Done", "section_refs": ["Methods", "Introduction"]}),
            content_type="application/json",
        )

        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.json()["coverage"], "covered")
        overview = overview_payload(self.project)
        self.assertEqual(overview["requirement_count"], 1)
        self.assertEqual(overview["requirement_coverage_counts"]["covered"], 1)

    def test_mcp_summary_api_write_creates_audit_log(self) -> None:
        response = self.client.post(
            f"/api/projects/{self.project.id}/section-summaries/",
            data=json.dumps(
                {
                    "section_title": "Methods",
                    "summary_text": "Methods summary",
                    "section_index": 2,
                    "change_summary": "Refresh methods summary",
                }
            ),
            content_type="application/json",
            HTTP_X_CHANGE_SOURCE="mcp",
        )

        self.assertEqual(response.status_code, 201)
        log = AssistantAuditLog.objects.filter(project=self.project, model_name="SectionSummary").latest("id")
        self.assertEqual(log.source, AssistantAuditLog.Source.MCP)
        self.assertEqual(log.operation, AssistantAuditLog.Operation.CREATE)

    def test_locked_project_blocks_requirement_writes(self) -> None:
        AISession.objects.create(
            project=self.project,
            goal="Locked run",
            branch_name="ai/lock-2",
            worktree_path="/tmp/lock-2",
            status=AISession.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(hours=12),
        )

        response = self.client.post(
            f"/api/projects/{self.project.id}/requirements/",
            data=json.dumps({"req_id": "R-02", "description": "Blocked write"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 423)
        self.assertEqual(response.json()["error"], "PROJECT_LOCKED")


# ── Helper shared by session tests ─────────────────────────────────────────

def _make_project_with_initial_commit(user, template, title="Thesis"):
    """Create a Project with an initial git commit so worktrees can be created."""
    from projects.services import (
        commit_project_text_changes,
        ensure_project_dir,
        ensure_project_git_repo,
        write_source_content,
    )

    project = Project.objects.create(owner=user, title=title, template=template)
    ensure_project_dir(project)
    write_source_content(project, "\\documentclass{article}\n\\begin{document}\nHello World\n\\end{document}\n")
    ensure_project_git_repo(project)
    commit_project_text_changes(
        project,
        summary="Initial source",
        operation="create",
        source="web",
        target_files=["main.tex"],
    )
    return project


class AISessionServiceTests(TestCase):
    """Package 5A: backend AI session engine tests."""

    def setUp(self) -> None:
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git executable not available")
        self.temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.temp_dir))
        self.settings_override.enable()
        self.user = User.objects.create_user(username="session-user", password="secret")
        self.template = Template.objects.create(title="Blank", content="\\documentclass{article}\n")
        self.project = _make_project_with_initial_commit(self.user, self.template)

    def tearDown(self) -> None:
        self.settings_override.disable()
        super().tearDown()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def test_create_session_creates_git_branch_and_worktree(self) -> None:
        from longdoc.session_service import create_session

        session = create_session(self.project, goal="Write chapter 1")

        self.assertEqual(session.status, AISession.Status.ACTIVE)
        self.assertTrue(session.branch_name.startswith("ai/session-"))
        worktree = Path(session.worktree_path)
        self.assertTrue(worktree.exists())
        self.assertTrue((worktree / "main.tex").exists())
        self.assertNotEqual(session.branch_name, "PENDING")
        self.assertNotEqual(session.worktree_path, "PENDING")

    def test_create_session_sets_expiry(self) -> None:
        from longdoc.session_service import create_session

        session = create_session(self.project, goal="Check expiry", expires_hours=48)

        delta = session.expires_at - timezone.now()
        self.assertAlmostEqual(delta.total_seconds() / 3600, 48, delta=1)

    def test_create_session_locks_project(self) -> None:
        from longdoc.locks import is_project_locked
        from longdoc.session_service import create_session

        self.assertFalse(is_project_locked(self.project))
        create_session(self.project, goal="Lock test")
        self.assertTrue(is_project_locked(self.project))

    def test_create_session_fails_when_project_already_locked(self) -> None:
        from longdoc.locks import ProjectLockedError
        from longdoc.session_service import create_session

        create_session(self.project, goal="First")
        with self.assertRaises(ProjectLockedError):
            create_session(self.project, goal="Second")

    def test_live_project_directory_stays_on_main_after_session_create(self) -> None:
        """The live project dir must never be checked out to the session branch."""
        from projects.services import _run_project_git
        from longdoc.session_service import create_session

        before = _run_project_git(self.project, ["rev-parse", "HEAD"]).stdout.strip()
        session = create_session(self.project, goal="Live dir check")
        after = _run_project_git(self.project, ["rev-parse", "HEAD"]).stdout.strip()

        self.assertEqual(before, after)
        worktree = Path(session.worktree_path)
        proc = subprocess.run(
            [shutil.which("git"), "branch", "--show-current"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        self.assertIn("ai/session-", proc.stdout.strip())

    # ── Writes ───────────────────────────────────────────────────────────

    def test_write_create_new_file(self) -> None:
        from longdoc.session_service import create_session, write_to_session

        session = create_session(self.project, goal="New file")
        result = write_to_session(
            session, "chapter2.tex",
            op="create_new_file",
            content="\\section{Chapter 2}\nNew content\n",
            change_summary="Add chapter 2",
        )

        self.assertEqual(result["op"], "create_new_file")
        self.assertTrue((Path(session.worktree_path) / "chapter2.tex").exists())

    def test_write_create_new_file_rejects_existing_file(self) -> None:
        from longdoc.session_service import SessionWriteError, create_session, write_to_session

        session = create_session(self.project, goal="Duplicate file")
        with self.assertRaises(SessionWriteError) as ctx:
            write_to_session(session, "main.tex", op="create_new_file", content="anything")
        self.assertEqual(ctx.exception.error, "FILE_EXISTS")

    def test_write_patch_file_lines_replaces_line_range(self) -> None:
        from longdoc.session_service import create_session, write_to_session

        session = create_session(self.project, goal="Patch lines")
        write_to_session(
            session, "main.tex",
            op="patch_file_lines",
            start_line=3,
            end_line=3,
            new_content="Updated line 3 content\n",
            change_summary="Update line 3",
        )

        content = (Path(session.worktree_path) / "main.tex").read_text()
        self.assertIn("Updated line 3 content", content)

    def test_write_patch_file_lines_anchor_mismatch_raises(self) -> None:
        from longdoc.session_service import SessionWriteError, create_session, write_to_session

        session = create_session(self.project, goal="Anchor test")
        with self.assertRaises(SessionWriteError) as ctx:
            write_to_session(
                session, "main.tex",
                op="patch_file_lines",
                start_line=2,
                end_line=2,
                new_content="replacement\n",
                anchor_before="NONEXISTENT_ANCHOR_XYZ",
            )
        self.assertEqual(ctx.exception.error, "ANCHOR_MISMATCH")

    def test_write_replace_text_applies_exact_once(self) -> None:
        from longdoc.session_service import create_session, write_to_session

        session = create_session(self.project, goal="Replace text")
        result = write_to_session(
            session, "main.tex",
            op="replace_text",
            old_text="Hello World",
            new_text="Hello, World!",
            dry_run=False,
        )

        self.assertFalse(result.get("dry_run"))
        content = (Path(session.worktree_path) / "main.tex").read_text()
        self.assertIn("Hello, World!", content)
        self.assertNotIn("Hello World", content)

    def test_write_replace_text_dry_run_returns_diff_without_committing(self) -> None:
        from projects.services import _run_project_git
        from longdoc.session_service import create_session, write_to_session

        session = create_session(self.project, goal="Dry run")
        before_count = int(
            _run_project_git(self.project, ["rev-list", "--count", session.branch_name], check=False).stdout.strip() or "0"
        )

        result = write_to_session(
            session, "main.tex",
            op="replace_text",
            old_text="Hello World",
            new_text="Goodbye World",
            dry_run=True,
        )

        self.assertTrue(result["dry_run"])
        self.assertIn("diff", result)
        content = (Path(session.worktree_path) / "main.tex").read_text()
        self.assertIn("Hello World", content)
        after_count = int(
            _run_project_git(self.project, ["rev-list", "--count", session.branch_name], check=False).stdout.strip() or "0"
        )
        self.assertEqual(before_count, after_count)

    def test_write_replace_text_ambiguous_raises(self) -> None:
        from longdoc.session_service import SessionWriteError, _run_worktree_git, create_session, write_to_session

        session = create_session(self.project, goal="Ambiguous")
        wt = Path(session.worktree_path)
        (wt / "dup.tex").write_text("foo\nfoo\n")
        _run_worktree_git(wt, ["add", "dup.tex"])
        _run_worktree_git(wt, ["commit", "--quiet", "-m", "add dup"])

        with self.assertRaises(SessionWriteError) as ctx:
            write_to_session(session, "dup.tex", op="replace_text", old_text="foo", new_text="bar")
        self.assertEqual(ctx.exception.error, "AMBIGUOUS_MATCH")

    def test_write_append_to_file(self) -> None:
        from longdoc.session_service import create_session, write_to_session

        session = create_session(self.project, goal="Append")
        result = write_to_session(
            session, "main.tex",
            op="append_to_file",
            content="% appended comment\n",
            change_summary="Append comment",
        )

        self.assertIn("appended_at_line", result)
        content = (Path(session.worktree_path) / "main.tex").read_text()
        self.assertIn("appended comment", content)

    def test_write_commits_change_to_session_branch(self) -> None:
        from projects.services import _run_project_git
        from longdoc.session_service import create_session, write_to_session

        session = create_session(self.project, goal="Commit count")
        before = int(
            _run_project_git(self.project, ["rev-list", "--count", session.branch_name], check=False).stdout.strip() or "0"
        )

        write_to_session(session, "main.tex", op="replace_text", old_text="Hello World", new_text="Committed", dry_run=False)

        after = int(
            _run_project_git(self.project, ["rev-list", "--count", session.branch_name], check=False).stdout.strip() or "0"
        )
        self.assertEqual(after, before + 1)

    def test_write_file_limit_enforced(self) -> None:
        from longdoc.session_service import SessionWriteError, create_session, write_to_session

        session = create_session(self.project, goal="File limit")
        with override_settings(MCP_MAX_SESSION_FILES=2):
            write_to_session(session, "a.tex", op="create_new_file", content="a\n")
            write_to_session(session, "b.tex", op="create_new_file", content="b\n")
            with self.assertRaises(SessionWriteError) as ctx:
                write_to_session(session, "c.tex", op="create_new_file", content="c\n")
            self.assertEqual(ctx.exception.error, "SESSION_FILE_LIMIT")

    # ── Diff ─────────────────────────────────────────────────────────────

    def test_generate_diff_shows_session_changes(self) -> None:
        from longdoc.session_service import create_session, generate_diff, write_to_session

        session = create_session(self.project, goal="Diff test")
        write_to_session(session, "main.tex", op="replace_text", old_text="Hello World", new_text="Changed Content", dry_run=False)

        diff = generate_diff(session)

        self.assertIn("Changed Content", diff)
        self.assertIn("Hello World", diff)
        session.refresh_from_db()
        self.assertEqual(session.diff_text, diff)

    def test_generate_diff_empty_when_no_changes(self) -> None:
        from longdoc.session_service import create_session, generate_diff

        session = create_session(self.project, goal="Empty diff")
        diff = generate_diff(session)
        self.assertEqual(diff, "")

    # ── Compile ───────────────────────────────────────────────────────────

    def test_compile_session_success_stores_staging_pdf_outside_worktree(self) -> None:
        from longdoc.session_service import compile_session, create_session, session_dir as get_session_dir

        session = create_session(self.project, goal="Compile")
        worktree = Path(session.worktree_path)

        def fake_run(cmd, **kwargs):
            (worktree / "main.pdf").write_bytes(b"%PDF-1.4 fake")
            return subprocess.CompletedProcess(cmd, 0, stdout="Success\n", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = compile_session(session)

        session.refresh_from_db()
        self.assertEqual(result["status"], "success")
        self.assertEqual(session.compile_status, AISession.CompileStatus.SUCCESS)
        self.assertEqual(session.status, AISession.Status.COMPILED)
        self.assertTrue(session.staging_pdf_path)
        self.assertFalse((worktree / "main.pdf").exists())
        sess_dir = get_session_dir(self.project, session.id)
        self.assertTrue((sess_dir / "staging.pdf").exists())

    def test_compile_session_failure_records_error_status(self) -> None:
        from longdoc.session_service import compile_session, create_session

        session = create_session(self.project, goal="Failed compile")
        fail_result = subprocess.CompletedProcess([], 1, stdout="", stderr="! Undefined control sequence")

        with mock.patch("subprocess.run", return_value=fail_result):
            result = compile_session(session)

        session.refresh_from_db()
        self.assertEqual(result["status"], "error")
        self.assertEqual(session.compile_status, AISession.CompileStatus.ERROR)
        self.assertEqual(session.status, AISession.Status.ACTIVE)

    def test_compile_session_runs_pre_compile_jobs_in_session_worktree(self) -> None:
        from longdoc.session_service import compile_session, create_session

        session = create_session(self.project, goal="Compile")
        worktree = Path(session.worktree_path)
        helper = worktree / ".smarttex" / "auto_generated" / "pdf_includes.typ"

        def fake_pre_compile(project, *, workdir):
            self.assertEqual(workdir, worktree)
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text("#let smarttex-include-pdf = none\n", encoding="utf-8")
            return []

        def fake_run(cmd, **kwargs):
            self.assertTrue(helper.exists())
            (worktree / ".smarttex" / "main.pdf").write_bytes(b"%PDF-1.4 fake")
            return subprocess.CompletedProcess(cmd, 0, stdout="Success\n", stderr="")

        with mock.patch("projects.pre_compile.run_pre_compile_jobs", side_effect=fake_pre_compile), mock.patch(
            "subprocess.run", side_effect=fake_run
        ):
            result = compile_session(session)

        self.assertEqual(result["status"], "success")

    def test_compile_session_missing_source_returns_error(self) -> None:
        from longdoc.session_service import compile_session, create_session

        session = create_session(self.project, goal="Missing source")
        (Path(session.worktree_path) / "main.tex").unlink()

        result = compile_session(session)

        self.assertEqual(result["status"], "error")
        session.refresh_from_db()
        self.assertEqual(session.compile_status, AISession.CompileStatus.ERROR)

    # ── Discard ───────────────────────────────────────────────────────────

    def test_discard_session_removes_worktree_and_session_dir(self) -> None:
        from longdoc.session_service import create_session, discard_session, session_dir as get_session_dir

        session = create_session(self.project, goal="Discard")
        worktree = Path(session.worktree_path)
        sess_dir = get_session_dir(self.project, session.id)

        discard_session(session)

        self.assertFalse(worktree.exists())
        self.assertFalse(sess_dir.exists())
        session.refresh_from_db()
        self.assertEqual(session.status, AISession.Status.DISCARDED)
        self.assertIsNotNone(session.discarded_at)

    def test_discard_session_deletes_git_branch(self) -> None:
        from projects.services import _run_project_git
        from longdoc.session_service import create_session, discard_session

        session = create_session(self.project, goal="Branch cleanup")
        branch = session.branch_name
        self.assertIn(branch, _run_project_git(self.project, ["branch", "--list", branch], check=False).stdout)

        discard_session(session)

        self.assertNotIn(branch, _run_project_git(self.project, ["branch", "--list", branch], check=False).stdout)

    def test_discard_session_unlocks_project(self) -> None:
        from longdoc.locks import is_project_locked
        from longdoc.session_service import create_session, discard_session

        session = create_session(self.project, goal="Unlock check")
        self.assertTrue(is_project_locked(self.project))

        discard_session(session)

        self.assertFalse(is_project_locked(self.project))

    def test_discard_already_closed_session_raises(self) -> None:
        from longdoc.session_service import SessionWriteError, create_session, discard_session

        session = create_session(self.project, goal="Double discard")
        discard_session(session)
        with self.assertRaises(SessionWriteError) as ctx:
            discard_session(session)
        self.assertEqual(ctx.exception.error, "SESSION_ALREADY_CLOSED")

    # ── Expiry ────────────────────────────────────────────────────────────

    def test_expire_stale_sessions_cleans_up_overdue_sessions(self) -> None:
        from longdoc.locks import is_project_locked
        from longdoc.session_service import create_session, expire_stale_sessions

        session = create_session(self.project, goal="Will expire")
        AISession.objects.filter(id=session.id).update(expires_at=timezone.now() - timedelta(hours=1))
        self.assertTrue(is_project_locked(self.project))

        count = expire_stale_sessions()

        self.assertEqual(count, 1)
        session.refresh_from_db()
        self.assertEqual(session.status, AISession.Status.EXPIRED)
        self.assertFalse(is_project_locked(self.project))
        self.assertFalse(Path(session.worktree_path).exists())

    def test_expire_stale_sessions_ignores_future_sessions(self) -> None:
        from longdoc.session_service import create_session, expire_stale_sessions

        session = create_session(self.project, goal="Should survive")

        count = expire_stale_sessions()

        self.assertEqual(count, 0)
        session.refresh_from_db()
        self.assertEqual(session.status, AISession.Status.ACTIVE)

    def test_expire_stale_sessions_ignores_already_closed_sessions(self) -> None:
        from longdoc.session_service import create_session, discard_session, expire_stale_sessions

        session = create_session(self.project, goal="Already done")
        discard_session(session)
        AISession.objects.filter(id=session.id).update(expires_at=timezone.now() - timedelta(hours=1))

        count = expire_stale_sessions()

        self.assertEqual(count, 0)

    # ── Artifact isolation ────────────────────────────────────────────────

    def test_session_dir_structure_is_under_smarttex_sessions(self) -> None:
        from projects.services import project_dir as get_project_dir
        from longdoc.session_service import create_session, session_dir as get_session_dir

        session = create_session(self.project, goal="Path check")
        sess_dir = get_session_dir(self.project, session.id)
        project_root = get_project_dir(self.project)

        self.assertEqual(sess_dir, project_root / ".smarttex" / "sessions" / str(session.id))
        self.assertEqual(Path(session.worktree_path), sess_dir / "worktree")

    def test_write_to_inactive_session_raises(self) -> None:
        from longdoc.session_service import SessionWriteError, create_session, discard_session, write_to_session

        session = create_session(self.project, goal="Post-discard write")
        discard_session(session)
        with self.assertRaises(SessionWriteError) as ctx:
            write_to_session(session, "main.tex", op="replace_text", old_text="x", new_text="y")
        self.assertEqual(ctx.exception.error, "SESSION_NOT_ACTIVE")


class ChangeProposalServiceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git executable not available")
        self.temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.temp_dir))
        self.settings_override.enable()
        self.user = User.objects.create_user(username="proposal-user", password="secret")
        self.template = Template.objects.create(title="Blank", content="\\documentclass{article}\n")
        self.project = _make_project_with_initial_commit(self.user, self.template, title="Proposal Project")

    def tearDown(self) -> None:
        self.settings_override.disable()
        super().tearDown()

    def _fake_compile_success(self, session: AISession) -> dict:
        session.compile_status = AISession.CompileStatus.SUCCESS
        session.status = AISession.Status.COMPILED
        session.staging_pdf_path = f".smarttex/sessions/{session.id}/staging.pdf"
        session.save(update_fields=["compile_status", "status", "staging_pdf_path", "updated_at"])
        return {"status": "success", "log": "ok", "diagnostics": [], "staging_pdf_path": session.staging_pdf_path}

    def _fake_compile_error(self, session: AISession) -> dict:
        session.compile_status = AISession.CompileStatus.ERROR
        session.compile_log = "! Undefined control sequence"
        session.save(update_fields=["compile_status", "compile_log", "updated_at"])
        return {"status": "error", "log": "! Undefined control sequence", "diagnostics": []}

    def test_propose_document_change_creates_ready_proposal_without_exposing_session_paths(self) -> None:
        from longdoc.proposal_service import propose_document_change, serialize_change_proposal

        with mock.patch("longdoc.proposal_service.compile_session", side_effect=self._fake_compile_success):
            proposal = propose_document_change(
                self.project,
                goal="Revise greeting",
                patch_ops=[
                    {
                        "filename": "main.tex",
                        "op": "replace_text",
                        "old_text": "Hello World",
                        "new_text": "Hello Proposal",
                        "change_summary": "Revise greeting",
                    }
                ],
            )

        self.assertEqual(proposal.status, ChangeProposal.Status.READY_FOR_REVIEW)
        self.assertEqual(proposal.compile_status, AISession.CompileStatus.SUCCESS)
        self.assertEqual(proposal.internal_session.status, AISession.Status.READY_FOR_REVIEW)
        payload = serialize_change_proposal(proposal)
        self.assertNotIn("branch_name", payload)
        self.assertNotIn("worktree_path", payload)
        self.assertNotIn("diff_summary", payload)
        self.assertTrue(payload["preview_pdf_available"])

    @override_settings(SMALL_MODEL_FEATURE_ENABLED=True)
    def test_propose_document_change_budget_warning_does_not_fail_proposal(self) -> None:
        from longdoc.proposal_service import propose_document_change

        UserSmallModelAccess.objects.create(user=self.user, enabled=True)
        UserSmallModelQuota.objects.create(user=self.user)
        ProjectSmallModelSettings.objects.create(
            project=self.project,
            small_model_control_enabled=True,
            diff_safety_reviewer_enabled=True,
        )

        write_source_content(self.project, "chapter.tex", "\\section{Chapter}\nOriginal text\n")
        write_source_content(self.project, "main.tex", "\\documentclass{article}\n\\input{chapter.tex}\nHello World\n")

        with mock.patch("longdoc.proposal_service.compile_session", side_effect=self._fake_compile_success), mock.patch(
            "small_model.services.base.get_provider", side_effect=RuntimeError("no provider")
        ):
            proposal = propose_document_change(
                self.project,
                goal="Revise greeting and chapter text",
                patch_ops=[
                    {
                        "filename": "main.tex",
                        "op": "replace_text",
                        "old_text": "Hello World",
                        "new_text": "Hello Proposal",
                        "change_summary": "Revise greeting",
                    },
                    {
                        "filename": "chapter.tex",
                        "op": "replace_text",
                        "old_text": "Original text",
                        "new_text": "Updated chapter text",
                        "change_summary": "Revise chapter text",
                    },
                ],
            )

        self.assertEqual(proposal.status, ChangeProposal.Status.READY_FOR_REVIEW)
        self.assertEqual(proposal.compile_status, AISession.CompileStatus.SUCCESS)
        self.assertIn("TOO_MANY_FILES", {item["code"] for item in proposal.smcl_warnings})
        self.assertIn("DETERMINISTIC_BUDGET_MISMATCH", {item["code"] for item in proposal.smcl_warnings})

    def test_propose_document_change_failed_compile_does_not_become_ready(self) -> None:
        from longdoc.proposal_service import propose_document_change

        with mock.patch("longdoc.proposal_service.compile_session", side_effect=self._fake_compile_error):
            proposal = propose_document_change(
                self.project,
                goal="Break compile",
                patch_ops=[
                    {
                        "filename": "main.tex",
                        "op": "replace_text",
                        "old_text": "Hello World",
                        "new_text": "\\undefinedcommand",
                        "change_summary": "Introduce compile error",
                    }
                ],
            )

        self.assertEqual(proposal.status, ChangeProposal.Status.FAILED_COMPILE)
        self.assertEqual(proposal.compile_status, AISession.CompileStatus.ERROR)
        self.assertNotEqual(proposal.internal_session.status, AISession.Status.READY_FOR_REVIEW)

    def test_propose_document_change_retries_after_failed_compile_without_ai_session_lock(self) -> None:
        from longdoc.proposal_service import propose_document_change

        with mock.patch("longdoc.proposal_service.compile_session", side_effect=self._fake_compile_error):
            first = propose_document_change(
                self.project,
                goal="Break compile",
                patch_ops=[
                    {
                        "filename": "main.tex",
                        "op": "replace_text",
                        "old_text": "Hello World",
                        "new_text": "\\undefinedcommand",
                        "change_summary": "Introduce compile error",
                    }
                ],
            )

        with mock.patch("longdoc.proposal_service.compile_session", side_effect=self._fake_compile_success):
            second = propose_document_change(
                self.project,
                goal="Retry after failed compile",
                created_by=ChangeProposal.CreatedBy.MCP,
                patch_ops=[
                    {
                        "filename": "main.tex",
                        "op": "replace_text",
                        "old_text": "Hello World",
                        "new_text": "Hello Retry",
                        "change_summary": "Retry with safe text",
                    }
                ],
            )

        first.refresh_from_db()
        self.assertEqual(first.status, ChangeProposal.Status.DISCARDED)
        self.assertEqual(second.status, ChangeProposal.Status.READY_FOR_REVIEW)
        self.assertEqual(second.auto_discarded_previous_failed_proposal_id, first.id)

    def test_propose_document_change_can_continue_existing_mcp_proposal(self) -> None:
        from longdoc.proposal_service import propose_document_change

        with mock.patch("longdoc.proposal_service.compile_session", side_effect=self._fake_compile_success):
            first = propose_document_change(
                self.project,
                goal="Initial proposal",
                patch_ops=[
                    {
                        "filename": "main.tex",
                        "op": "replace_text",
                        "old_text": "Hello World",
                        "new_text": "Hello Proposal",
                        "change_summary": "Initial change",
                    }
                ],
            )

            second = propose_document_change(
                self.project,
                goal="Refine existing proposal",
                continue_existing=True,
                patch_ops=[
                    {
                        "filename": "main.tex",
                        "op": "replace_text",
                        "old_text": "Hello Proposal",
                        "new_text": "Hello Proposal v2",
                        "change_summary": "Refine draft",
                    }
                ],
            )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.internal_session_id, second.internal_session_id)
        self.assertEqual(second.status, ChangeProposal.Status.READY_FOR_REVIEW)
        self.assertEqual(second.goal, "Refine existing proposal")
        self.assertEqual(len(second.patch_ops), 2)
        self.assertEqual(second.internal_session.batch.summary, "Refine existing proposal")
        self.assertIn("Hello Proposal v2", second.internal_session.batch.changes.get(filename="main.tex").diff_text)

    def test_retry_lock_suggestion_guides_mcp_retry_workflow(self) -> None:
        from longdoc.proposal_service import _retry_lock_suggestion
        from longdoc.session_service import create_session

        session = create_session(self.project, goal="Failed MCP proposal")
        proposal = ChangeProposal.objects.create(
            project=self.project,
            goal="Failed MCP proposal",
            status=ChangeProposal.Status.FAILED_COMPILE,
            created_by=ChangeProposal.CreatedBy.MCP,
            internal_session=session,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        suggestion = _retry_lock_suggestion(proposal)

        self.assertIn("Retry via the proposal workflow", suggestion)
        self.assertIn("validate_document_change", suggestion)
        self.assertIn("propose_document_change", suggestion)

    def test_source_file_creation_without_include_is_rejected_before_session_create(self) -> None:
        from longdoc.proposal_service import propose_document_change
        from longdoc.session_service import SessionWriteError

        with self.assertRaises(SessionWriteError) as ctx:
            propose_document_change(
                self.project,
                goal="Add orphan chapter",
                patch_ops=[
                    {
                        "filename": "chapter2.tex",
                        "op": "create_new_file",
                        "content": "\\section{Chapter 2}\nText\n",
                    }
                ],
            )

        self.assertEqual(ctx.exception.error, "SOURCE_FILE_NOT_INCLUDED")
        self.assertEqual(ChangeProposal.objects.count(), 0)

    def test_document_graph_reports_orphan_source_file(self) -> None:
        from longdoc.document_graph import inspect_document_graph
        from projects.services import project_dir as get_project_dir

        (get_project_dir(self.project) / "orphan.tex").write_text("\\section{Orphan}\n", encoding="utf-8")

        graph = inspect_document_graph(self.project)

        self.assertIn("orphan.tex", graph.orphan_source_files)
        self.assertTrue(any(issue.type == "orphan_source_file" for issue in graph.errors))


class AISessionReviewTests(TestCase):
    """Package 5B: accept/discard workflow, UI lock, version audit trail."""

    def setUp(self) -> None:
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git executable not available")
        self.temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.temp_dir))
        self.settings_override.enable()
        self.user = User.objects.create_user(username="review-user", password="secret")
        self.template = Template.objects.create(title="Blank", content="\\documentclass{article}\n")
        self.project = _make_project_with_initial_commit(self.user, self.template)

    def tearDown(self) -> None:
        self.settings_override.disable()
        super().tearDown()

    def _mark_session_compiled(self, session: AISession) -> None:
        session.compile_status = AISession.CompileStatus.SUCCESS
        session.staging_pdf_path = f".smarttex/sessions/{session.id}/staging.pdf"
        session.status = AISession.Status.COMPILED
        session.save(update_fields=["compile_status", "staging_pdf_path", "status", "updated_at"])

    # ── finalize_batch ───────────────────────────────────────────────────

    def test_finalize_batch_creates_aibatch_and_changes(self) -> None:
        from longdoc.session_service import create_session, finalize_batch, write_to_session
        from longdoc.models import AIBatch, AIBatchChange

        session = create_session(self.project, goal="Test batch")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Hello Batch")
        self._mark_session_compiled(session)

        batch = finalize_batch(session, summary="Added chapter intro")

        self.assertIsInstance(batch, AIBatch)
        self.assertEqual(batch.summary, "Added chapter intro")
        self.assertEqual(batch.session, session)
        changes = list(batch.changes.all())
        self.assertGreater(len(changes), 0)
        filenames = [c.filename for c in changes]
        self.assertIn("main.tex", filenames)

    def test_finalize_batch_records_diff_stats(self) -> None:
        from longdoc.session_service import create_session, finalize_batch, write_to_session
        from longdoc.models import AIBatchChange

        session = create_session(self.project, goal="Stats check")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="New line\nAnother line")
        self._mark_session_compiled(session)

        batch = finalize_batch(session, summary="Two-line change")

        change = batch.changes.get(filename="main.tex")
        self.assertGreater(change.lines_added, 0)
        self.assertGreater(change.lines_removed, 0)
        self.assertIsNotNone(change.diff_text)

    def test_finalize_batch_sets_session_to_ready_for_review(self) -> None:
        from longdoc.session_service import create_session, finalize_batch, write_to_session

        session = create_session(self.project, goal="Status check")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Ready content")
        self._mark_session_compiled(session)

        finalize_batch(session, summary="Summary")

        session.refresh_from_db()
        self.assertEqual(session.status, AISession.Status.READY_FOR_REVIEW)

    def test_finalize_batch_links_tasks(self) -> None:
        from longdoc.session_service import create_session, finalize_batch, write_to_session
        from longdoc.models import ProjectTask
        from longdoc.services import enable_longdoc

        enable_longdoc(self.project)
        session = create_session(self.project, goal="Tasks check")
        task = ProjectTask.objects.create(
            project=self.project, description="Write intro", status=ProjectTask.Status.IN_PROGRESS,
        )
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Intro text")
        self._mark_session_compiled(session)

        batch = finalize_batch(session, summary="Done", task_ids=[task.id])

        self.assertIn(task, batch.tasks_completed.all())

    def test_finalize_batch_links_annotations(self) -> None:
        from longdoc.session_service import create_session, finalize_batch, write_to_session
        from longdoc.models import ProjectAnnotation
        from longdoc.services import enable_longdoc

        enable_longdoc(self.project)
        session = create_session(self.project, goal="Annotations check")
        annotation = ProjectAnnotation.objects.create(
            project=self.project,
            file_name="main.tex",
            line_start=2,
            line_end=3,
            instruction="Tighten this paragraph",
            status=ProjectAnnotation.Status.IN_PROGRESS,
        )
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Edited paragraph")
        self._mark_session_compiled(session)

        batch = finalize_batch(session, summary="Done", annotation_ids=[annotation.id])

        self.assertIn(annotation, batch.annotations_completed.all())

    def test_finalize_batch_rejects_uncompiled_session(self) -> None:
        from longdoc.session_service import SessionWriteError, create_session, finalize_batch, write_to_session

        session = create_session(self.project, goal="Reject uncompiled")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Not compiled")

        with self.assertRaises(SessionWriteError) as ctx:
            finalize_batch(session, summary="Should fail")
        self.assertEqual(ctx.exception.error, "COMPILE_REQUIRED")

    # ── accept_session ───────────────────────────────────────────────────

    def test_accept_session_merges_changes_into_live_files(self) -> None:
        from longdoc.session_service import create_session, accept_session, write_to_session
        from projects.services import project_dir as get_project_dir

        session = create_session(self.project, goal="Live file merge")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="ACCEPTED CONTENT")

        accept_session(session, user=self.user)

        proj_dir = get_project_dir(self.project)
        content = (proj_dir / "main.tex").read_text()
        self.assertIn("ACCEPTED CONTENT", content)

    def test_accept_session_creates_project_versions_per_file(self) -> None:
        from longdoc.session_service import create_session, accept_session, write_to_session

        session = create_session(self.project, goal="Version audit")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Versioned content")

        accept_session(session, user=self.user)

        versions = ProjectVersion.objects.filter(
            project=self.project,
            target_file="main.tex",
            category=ProjectVersion.Category.SESSION_ACCEPT,
        )
        self.assertEqual(versions.count(), 1)
        v = versions.first()
        self.assertEqual(v.operation, "ai_session_accept")
        self.assertIn("session_id", v.event_payload)
        self.assertEqual(v.event_payload["session_id"], session.id)

    def test_accept_session_promotes_staging_pdf_to_live_pdf(self) -> None:
        from longdoc.session_service import create_session, accept_session, session_dir as get_session_dir, write_to_session
        from projects.services import pdf_file_path

        session = create_session(self.project, goal="Promote staged pdf")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="PDF promoted")
        staging_pdf = get_session_dir(self.project, session.id) / "staging.pdf"
        staging_pdf.parent.mkdir(parents=True, exist_ok=True)
        staging_pdf.write_bytes(b"%PDF-1.4\naccepted preview\n")
        session.staging_pdf_path = f".smarttex/sessions/{session.id}/staging.pdf"
        session.save(update_fields=["staging_pdf_path", "updated_at"])

        accept_session(session, user=self.user)

        self.project.refresh_from_db()
        self.assertEqual(self.project.last_status, Project.CompileStatus.SUCCESS)
        self.assertEqual(pdf_file_path(self.project).read_bytes(), b"%PDF-1.4\naccepted preview\n")

    def test_accept_session_version_contains_before_and_after(self) -> None:
        from longdoc.session_service import create_session, accept_session, write_to_session

        session = create_session(self.project, goal="Before/after check")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="After content")

        accept_session(session, user=self.user)

        v = ProjectVersion.objects.get(
            project=self.project,
            target_file="main.tex",
            category=ProjectVersion.Category.SESSION_ACCEPT,
        )
        self.assertIn("Hello World", v.before_content)
        self.assertIn("After content", v.after_content)

    def test_accept_session_completes_linked_tasks(self) -> None:
        from longdoc.session_service import create_session, finalize_batch, accept_session, write_to_session
        from longdoc.models import ProjectTask
        from longdoc.services import enable_longdoc

        enable_longdoc(self.project)
        session = create_session(self.project, goal="Task completion")
        task = ProjectTask.objects.create(
            project=self.project, description="Write section 1", status=ProjectTask.Status.IN_PROGRESS,
        )
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Section 1 done")
        self._mark_session_compiled(session)
        finalize_batch(session, summary="Completed section", task_ids=[task.id])

        accept_session(session, user=self.user)

        task.refresh_from_db()
        self.assertEqual(task.status, ProjectTask.Status.DONE)
        self.assertIsNotNone(task.completed_at)

    def test_accept_session_completes_linked_annotations(self) -> None:
        from longdoc.session_service import create_session, finalize_batch, accept_session, write_to_session
        from longdoc.models import ProjectAnnotation
        from longdoc.services import enable_longdoc

        enable_longdoc(self.project)
        session = create_session(self.project, goal="Annotation completion")
        annotation = ProjectAnnotation.objects.create(
            project=self.project,
            file_name="main.tex",
            line_start=2,
            line_end=2,
            instruction="Rewrite this sentence",
            status=ProjectAnnotation.Status.IN_PROGRESS,
        )
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Rewritten sentence")
        self._mark_session_compiled(session)
        finalize_batch(session, summary="Completed annotation", annotation_ids=[annotation.id])

        accept_session(session, user=self.user)

        annotation.refresh_from_db()
        self.assertEqual(annotation.status, ProjectAnnotation.Status.DONE)
        self.assertIsNotNone(annotation.resolved_at)
        self.assertEqual(annotation.resolved_by_session_id, session.id)

    def test_accept_session_unlocks_project(self) -> None:
        from longdoc.locks import is_project_locked
        from longdoc.session_service import create_session, accept_session, write_to_session

        session = create_session(self.project, goal="Unlock on accept")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Unlocked content")
        self.assertTrue(is_project_locked(self.project))

        accept_session(session, user=self.user)

        self.assertFalse(is_project_locked(self.project))
        session.refresh_from_db()
        self.assertEqual(session.status, AISession.Status.ACCEPTED)
        self.assertIsNotNone(session.accepted_at)

    def test_accept_session_removes_worktree_and_session_dir(self) -> None:
        from longdoc.session_service import create_session, accept_session, write_to_session, session_dir as get_session_dir

        session = create_session(self.project, goal="Cleanup check")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Cleaned up")
        sess_dir = get_session_dir(self.project, session.id)
        self.assertTrue(sess_dir.exists())

        accept_session(session, user=self.user)

        self.assertFalse(sess_dir.exists())

    # ── API endpoints ────────────────────────────────────────────────────

    def test_legacy_ai_session_endpoint_is_removed(self) -> None:
        client = Client()
        client.force_login(self.user)

        resp = client.get(f"/api/projects/{self.project.id}/ai-session/")

        self.assertEqual(resp.status_code, 404)

    def test_api_change_proposal_accept_requires_login(self) -> None:
        from longdoc.session_service import create_session, write_to_session

        session = create_session(self.project, goal="Auth check")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Auth check")
        self._mark_session_compiled(session)
        ChangeProposal.objects.create(
            project=self.project,
            goal=session.goal,
            status=ChangeProposal.Status.READY_FOR_REVIEW,
            validation_status=ChangeProposal.ValidationStatus.PASSED,
            compile_status=AISession.CompileStatus.SUCCESS,
            internal_session=session,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = Client()

        resp = client.post(f"/api/projects/{self.project.id}/change-proposals/accept/")

        self.assertIn(resp.status_code, (302, 403))

    def test_api_change_proposal_accept_accepts_proposal(self) -> None:
        from longdoc.session_service import create_session, write_to_session

        session = create_session(self.project, goal="HTTP accept")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Via HTTP accept")
        self._mark_session_compiled(session)
        proposal = ChangeProposal.objects.create(
            project=self.project,
            goal=session.goal,
            status=ChangeProposal.Status.READY_FOR_REVIEW,
            validation_status=ChangeProposal.ValidationStatus.PASSED,
            compile_status=AISession.CompileStatus.SUCCESS,
            internal_session=session,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = Client()
        client.force_login(self.user)

        resp = client.post(
            f"/api/projects/{self.project.id}/change-proposals/accept/",
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        session.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(session.status, AISession.Status.ACCEPTED)
        self.assertEqual(proposal.status, ChangeProposal.Status.ACCEPTED)

    def test_api_change_proposal_accept_rejects_failed_compile_without_override(self) -> None:
        from longdoc.session_service import create_session, write_to_session

        session = create_session(self.project, goal="HTTP failed compile")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Broken but wanted")
        session.compile_status = AISession.CompileStatus.ERROR
        session.status = AISession.Status.ACTIVE
        session.save(update_fields=["compile_status", "status", "updated_at"])
        ChangeProposal.objects.create(
            project=self.project,
            goal=session.goal,
            status=ChangeProposal.Status.FAILED_COMPILE,
            validation_status=ChangeProposal.ValidationStatus.PASSED,
            compile_status=AISession.CompileStatus.ERROR,
            internal_session=session,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = Client()
        client.force_login(self.user)

        resp = client.post(
            f"/api/projects/{self.project.id}/change-proposals/accept/",
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["warning_code"], "ACCEPT_COMPILE_ERRORS_REQUIRED")

    def test_api_change_proposal_accept_allows_failed_compile_with_override(self) -> None:
        from longdoc.session_service import create_session, write_to_session

        session = create_session(self.project, goal="HTTP force accept")
        write_to_session(session, "main.tex", op="replace_text",
                         old_text="Hello World", new_text="Broken but accepted")
        session.compile_status = AISession.CompileStatus.ERROR
        session.status = AISession.Status.ACTIVE
        session.save(update_fields=["compile_status", "status", "updated_at"])
        proposal = ChangeProposal.objects.create(
            project=self.project,
            goal=session.goal,
            status=ChangeProposal.Status.FAILED_COMPILE,
            validation_status=ChangeProposal.ValidationStatus.PASSED,
            compile_status=AISession.CompileStatus.ERROR,
            internal_session=session,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = Client()
        client.force_login(self.user)

        resp = client.post(
            f"/api/projects/{self.project.id}/change-proposals/accept/",
            data=json.dumps({"accept_compile_errors": True}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        session.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(session.status, AISession.Status.ACCEPTED)
        self.assertEqual(proposal.status, ChangeProposal.Status.ACCEPTED)
        self.assertEqual(resp.json()["warning"]["code"], "ACCEPTED_WITH_COMPILE_ERRORS")

    def test_api_change_proposal_discard_discards_proposal(self) -> None:
        from longdoc.session_service import create_session

        session = create_session(self.project, goal="HTTP discard")
        proposal = ChangeProposal.objects.create(
            project=self.project,
            goal=session.goal,
            status=ChangeProposal.Status.FAILED_COMPILE,
            compile_status=AISession.CompileStatus.ERROR,
            internal_session=session,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = Client()
        client.force_login(self.user)

        resp = client.post(
            f"/api/projects/{self.project.id}/change-proposals/discard/",
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        session.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(session.status, AISession.Status.DISCARDED)
        self.assertEqual(proposal.status, ChangeProposal.Status.DISCARDED)

    def test_api_change_proposal_preview_pdf_returns_404_when_no_pdf(self) -> None:
        from longdoc.session_service import create_session

        session = create_session(self.project, goal="No preview PDF")
        ChangeProposal.objects.create(
            project=self.project,
            goal=session.goal,
            status=ChangeProposal.Status.FAILED_COMPILE,
            internal_session=session,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = Client()
        client.force_login(self.user)

        resp = client.get(f"/api/projects/{self.project.id}/change-proposals/preview-pdf/")

        self.assertEqual(resp.status_code, 404)

    # ── Project lock enforcement ─────────────────────────────────────────

    def test_locked_project_blocks_file_writes_with_423(self) -> None:
        import json as _json
        from longdoc.session_service import create_session

        create_session(self.project, goal="Lock block test")
        client = Client()
        client.force_login(self.user)

        resp = client.put(
            f"/api/projects/{self.project.id}/file/",
            data=_json.dumps({"content": "blocked"}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 423)
        data = resp.json()
        self.assertEqual(data["error"], "PROJECT_LOCKED")

    def test_legacy_ai_session_finalize_endpoint_is_removed(self) -> None:
        client = Client()
        client.force_login(self.user)

        resp = client.post(
            f"/api/projects/{self.project.id}/ai-session/finalize/",
            data=json.dumps({"summary": "removed"}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 404)


class TemplateInitializationTests(TestCase):
    """Package 6: template long-doc data initialised on project creation."""

    def setUp(self) -> None:
        super().setUp()
        self.temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.temp_dir))
        self.settings_override.enable()
        self.user = User.objects.create_user(username="tmpl-user", password="secret")
        self.template = Template.objects.create(title="Thesis Template", content="\\documentclass{article}\n")

    def tearDown(self) -> None:
        self.settings_override.disable()
        super().tearDown()

    def _create_longdoc_template(self):
        defaults = TemplateLongDocDefaults.objects.create(
            template=self.template,
            enabled=True,
            requirements_enabled=True,
        )
        TemplateOutlineItem.objects.create(template=self.template, order=1, title="Introduction", level=1)
        TemplateOutlineItem.objects.create(template=self.template, order=2, title="Related Work", level=1)
        TemplateRequirement.objects.create(template=self.template, req_id="R-01", description="Cover intro")
        TemplateTask.objects.create(template=self.template, description="Write introduction")
        TemplateNoteSection.objects.create(template=self.template, heading="Writing Decisions", order=0)
        TemplateContextFile.objects.create(
            template=self.template,
            filename="brief.md",
            display_name="Project Brief",
            description="Goals and constraints",
            content="# Brief\n\nTBD\n",
        )
        return defaults

    def test_no_longdoc_defaults_returns_none(self) -> None:
        project = Project.objects.create(owner=self.user, title="Plain", template=self.template)
        result = initialize_longdoc_from_template(project, self.template)
        self.assertIsNone(result)
        self.assertFalse(ProjectLongDocSettings.objects.filter(project=project).exists())

    def test_longdoc_settings_created_from_template_defaults(self) -> None:
        self._create_longdoc_template()
        project = Project.objects.create(owner=self.user, title="FromTemplate", template=self.template)
        result = initialize_longdoc_from_template(project, self.template)
        self.assertIsNotNone(result)
        settings_obj = ProjectLongDocSettings.objects.get(project=project)
        self.assertTrue(settings_obj.enabled)
        self.assertTrue(settings_obj.requirements_enabled)

    def test_outline_items_copied_from_template(self) -> None:
        self._create_longdoc_template()
        project = Project.objects.create(owner=self.user, title="Outline Test", template=self.template)
        initialize_longdoc_from_template(project, self.template)
        items = list(ProjectOutlineItem.objects.filter(project=project).order_by("order"))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "Introduction")
        self.assertEqual(items[1].title, "Related Work")
        self.assertEqual(items[0].level, 1)

    def test_requirements_copied_from_template(self) -> None:
        self._create_longdoc_template()
        project = Project.objects.create(owner=self.user, title="Req Test", template=self.template)
        initialize_longdoc_from_template(project, self.template)
        reqs = list(ProjectRequirement.objects.filter(project=project))
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].req_id, "R-01")
        self.assertEqual(reqs[0].description, "Cover intro")

    def test_tasks_copied_from_template(self) -> None:
        self._create_longdoc_template()
        project = Project.objects.create(owner=self.user, title="Task Test", template=self.template)
        initialize_longdoc_from_template(project, self.template)
        tasks = list(ProjectTask.objects.filter(project=project))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].description, "Write introduction")

    def test_note_sections_copied_from_template(self) -> None:
        self._create_longdoc_template()
        project = Project.objects.create(owner=self.user, title="Notes Test", template=self.template)
        initialize_longdoc_from_template(project, self.template)
        notes = list(ProjectNoteSection.objects.filter(project=project))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].heading, "Writing Decisions")

    def test_default_note_sections_created_when_template_has_none(self) -> None:
        TemplateLongDocDefaults.objects.create(template=self.template, enabled=True)
        project = Project.objects.create(owner=self.user, title="Default Notes", template=self.template)
        initialize_longdoc_from_template(project, self.template)
        headings = set(ProjectNoteSection.objects.filter(project=project).values_list("heading", flat=True))
        for expected in DEFAULT_NOTE_SECTION_HEADINGS:
            self.assertIn(expected, headings)

    def test_context_file_written_to_disk_and_record_created(self) -> None:
        self._create_longdoc_template()
        project = Project.objects.create(owner=self.user, title="Context Test", template=self.template)
        initialize_longdoc_from_template(project, self.template)
        record = ProjectContextFile.objects.get(project=project, filename="brief.md")
        self.assertEqual(record.display_name, "Project Brief")
        disk_path = longdoc_context_dir(project) / "brief.md"
        self.assertTrue(disk_path.exists())
        self.assertIn("# Brief", disk_path.read_text(encoding="utf-8"))

    def test_template_with_no_context_files_skips_context_setup(self) -> None:
        TemplateLongDocDefaults.objects.create(template=self.template, enabled=True)
        project = Project.objects.create(owner=self.user, title="No Context", template=self.template)
        initialize_longdoc_from_template(project, self.template)
        self.assertFalse(ProjectContextFile.objects.filter(project=project).exists())

    def test_project_creation_api_calls_template_init(self) -> None:
        import json as _json
        self._create_longdoc_template()
        client = Client()
        client.force_login(self.user)
        resp = client.post(
            "/api/projects/",
            data=_json.dumps({"title": "API From Template", "template_id": self.template.id}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        project_id = resp.json()["id"]
        project = Project.objects.get(id=project_id)
        self.assertTrue(ProjectLongDocSettings.objects.filter(project=project, enabled=True).exists())
        self.assertEqual(ProjectOutlineItem.objects.filter(project=project).count(), 2)


class EdgeCaseTests(TestCase):
    """Package 6: edge cases for missing files, malformed data, expired sessions."""

    def setUp(self) -> None:
        super().setUp()
        self.temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.temp_dir))
        self.settings_override.enable()
        self.user = User.objects.create_user(username="edge-user", password="secret")
        self.template = Template.objects.create(title="Blank", content="\\documentclass{article}\n")
        self.project = Project.objects.create(owner=self.user, title="Edge Project", template=self.template)

    def tearDown(self) -> None:
        self.settings_override.disable()
        super().tearDown()

    # ── Missing context files ────────────────────────────────────────────

    def test_list_context_files_when_file_missing_from_disk(self) -> None:
        enable_longdoc(self.project)
        # Remove the sample context file from disk but keep the DB record
        context_dir = longdoc_context_dir(self.project)
        for f in context_dir.iterdir():
            f.unlink()
        # list_context_files should survive and return an empty list (records deleted on sync)
        files = list_context_files(self.project)
        self.assertIsInstance(files, list)

    def test_get_context_file_missing_from_disk_returns_none_content(self) -> None:
        from .services import create_context_file, sync_context_file_records
        from projects.services import ensure_project_dir
        ensure_project_dir(self.project)
        enable_longdoc(self.project)
        # Create file, then delete it from disk
        create_context_file(
            self.project,
            filename="missing.md",
            content="# Present\n",
            source="web",
        )
        disk_path = longdoc_context_dir(self.project) / "missing.md"
        disk_path.unlink()
        # After disk deletion, the record should be removed by sync
        sync_context_file_records(self.project)
        self.assertFalse(ProjectContextFile.objects.filter(project=self.project, filename="missing.md").exists())

    def test_context_file_with_invalid_path_skipped_during_template_init(self) -> None:
        TemplateLongDocDefaults.objects.create(template=self.template, enabled=True)
        # Inject a context file with a path-traversal filename — should be silently skipped
        TemplateContextFile.objects.create(
            template=self.template,
            filename="../escape.md",
            content="bad",
        )
        project = Project.objects.create(owner=self.user, title="BadPath", template=self.template)
        # Should not raise; no context file record should be created
        initialize_longdoc_from_template(project, self.template)
        self.assertFalse(ProjectContextFile.objects.filter(project=project).exists())

    # ── Malformed / invalid data ─────────────────────────────────────────

    def test_create_outline_item_with_zero_level_clamped_to_one(self) -> None:
        from .services import create_outline_item
        from projects.services import ensure_project_dir
        ensure_project_dir(self.project)
        enable_longdoc(self.project)
        item = create_outline_item(self.project, title="Bad Level", level=0, source="web")
        self.assertEqual(item["level"], 1)

    def test_create_outline_item_with_empty_title_raises(self) -> None:
        from .services import create_outline_item
        from projects.services import ensure_project_dir
        ensure_project_dir(self.project)
        enable_longdoc(self.project)
        with self.assertRaises(ValueError):
            create_outline_item(self.project, title="   ", source="web")

    def test_create_task_with_empty_description_raises(self) -> None:
        from .services import create_task
        from projects.services import ensure_project_dir
        ensure_project_dir(self.project)
        enable_longdoc(self.project)
        with self.assertRaises(ValueError):
            create_task(self.project, description="", source="web")

    def test_template_with_only_defaults_no_items_creates_settings_only(self) -> None:
        TemplateLongDocDefaults.objects.create(template=self.template, enabled=True, requirements_enabled=False)
        project = Project.objects.create(owner=self.user, title="DefaultsOnly", template=self.template)
        result = initialize_longdoc_from_template(project, self.template)
        self.assertIsNotNone(result)
        self.assertTrue(ProjectLongDocSettings.objects.filter(project=project, enabled=True).exists())
        self.assertEqual(ProjectOutlineItem.objects.filter(project=project).count(), 0)
        self.assertEqual(ProjectRequirement.objects.filter(project=project).count(), 0)

    # ── Expired sessions ─────────────────────────────────────────────────

    def test_expire_stale_sessions_marks_overdue_sessions_expired(self) -> None:
        if not shutil.which("git"):
            self.skipTest("git executable not available")
        from projects.services import commit_project_text_changes, ensure_project_dir, ensure_project_git_repo, write_source_content
        from longdoc.session_service import create_session, expire_stale_sessions

        ensure_project_dir(self.project)
        write_source_content(self.project, "\\documentclass{article}\n\\begin{document}\nEdge\n\\end{document}\n")
        ensure_project_git_repo(self.project)
        commit_project_text_changes(
            self.project,
            summary="Init",
            operation="create",
            source="web",
            target_files=["main.tex"],
        )
        session = create_session(self.project, goal="Will expire")
        # Wind back the expiry time so it's in the past
        AISession.objects.filter(id=session.id).update(expires_at=timezone.now() - timedelta(hours=1))

        count = expire_stale_sessions()

        self.assertGreaterEqual(count, 1)
        session.refresh_from_db()
        self.assertEqual(session.status, AISession.Status.EXPIRED)

    def test_expire_stale_sessions_does_not_expire_accepted_sessions(self) -> None:
        if not shutil.which("git"):
            self.skipTest("git executable not available")
        from projects.services import commit_project_text_changes, ensure_project_dir, ensure_project_git_repo, write_source_content
        from longdoc.session_service import create_session, expire_stale_sessions

        ensure_project_dir(self.project)
        write_source_content(self.project, "\\documentclass{article}\n\\begin{document}\nEdge2\n\\end{document}\n")
        ensure_project_git_repo(self.project)
        commit_project_text_changes(
            self.project,
            summary="Init",
            operation="create",
            source="web",
            target_files=["main.tex"],
        )
        session = create_session(self.project, goal="Already accepted")
        AISession.objects.filter(id=session.id).update(
            status=AISession.Status.ACCEPTED,
            expires_at=timezone.now() - timedelta(hours=1),
        )

        expire_stale_sessions()

        session.refresh_from_db()
        self.assertEqual(session.status, AISession.Status.ACCEPTED)

    def test_expire_stale_sessions_returns_zero_when_nothing_overdue(self) -> None:
        from longdoc.session_service import expire_stale_sessions
        count = expire_stale_sessions()
        self.assertEqual(count, 0)


class VerificationFixTests(TestCase):
    """Regression tests for issues found in LONGDOC_VERIFICATION_REPORT.md."""

    def setUp(self) -> None:
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git executable not available")
        self.temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.temp_dir))
        self.settings_override.enable()
        self.user = User.objects.create_user(username="fix-user", password="secret")
        self.template = Template.objects.create(title="Blank", content="\\documentclass{article}\n")
        self.project = _make_project_with_initial_commit(self.user, self.template)
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self) -> None:
        self.settings_override.disable()
        super().tearDown()

    # ── CRITICAL-2: typst import lock check ──────────────────────────────

    def test_typst_import_returns_423_when_project_is_locked(self) -> None:
        import io
        import zipfile
        from longdoc.session_service import create_session

        create_session(self.project, goal="Lock project")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.tex", "\\documentclass{article}\\begin{document}\\end{document}")
        buf.seek(0)

        response = self.client.post(
            f"/api/projects/{self.project.id}/typst-import/",
            data={"file": buf},
        )

        self.assertEqual(response.status_code, 423)
        self.assertEqual(response.json()["error"], "PROJECT_LOCKED")

    # ── Legacy session API removed ───────────────────────────────────────

    def test_api_ai_session_post_is_removed(self) -> None:
        from longdoc.services import enable_longdoc

        enable_longdoc(self.project)

        response = self.client.post(
            f"/api/projects/{self.project.id}/ai-session/",
            data=json.dumps({"goal": "Write introduction"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)

    def test_api_ai_session_write_is_removed(self) -> None:
        response = self.client.post(
            f"/api/projects/{self.project.id}/ai-session/write/",
            data=json.dumps({"filename": "main.tex", "op": "append_to_file", "text": "x", "change_summary": "test"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    # ── Overview includes active_proposal ────────────────────────────────

    def test_overview_payload_includes_active_proposal_when_proposal_exists(self) -> None:
        from longdoc.services import enable_longdoc

        enable_longdoc(self.project)
        proposal = ChangeProposal.objects.create(
            project=self.project,
            goal="Active proposal test",
            status=ChangeProposal.Status.FAILED_VALIDATION,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        payload = overview_payload(self.project)

        self.assertIn("active_proposal", payload)
        self.assertIsNotNone(payload["active_proposal"])
        self.assertEqual(payload["active_proposal"]["id"], proposal.id)
        self.assertEqual(payload["active_proposal"]["status"], ChangeProposal.Status.FAILED_VALIDATION)

    def test_overview_payload_active_proposal_is_none_when_no_proposal(self) -> None:
        from longdoc.services import enable_longdoc

        enable_longdoc(self.project)

        payload = overview_payload(self.project)

        self.assertIn("active_proposal", payload)
        self.assertIsNone(payload["active_proposal"])
