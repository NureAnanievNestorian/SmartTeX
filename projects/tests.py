import json
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from SmartTeX.markup import MarkupType
from projects.models import Project
from projects.services import (
    create_project_text_file,
    list_project_assets,
    main_source_filename,
    read_source_content,
    source_file_path,
    split_typst_sections,
    synctex_line_to_pdf,
)
from templates_lib.models import Template


class ProjectTypstSupportTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.temp_dir))
        self.settings_override.enable()
        self.user = User.objects.create_user(username="tester", password="secret123")
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self) -> None:
        self.settings_override.disable()
        super().tearDown()

    def test_create_project_with_typst_markup_uses_main_typ(self) -> None:
        response = self.client.post(
            "/api/projects/",
            data=json.dumps({"title": "Typst Project", "markup_type": "typst"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        project = Project.objects.get(pk=payload["id"])

        self.assertEqual(project.markup_type, MarkupType.TYPST)
        self.assertEqual(payload["main_file_name"], "main.typ")
        self.assertEqual(main_source_filename(project), "main.typ")
        self.assertTrue(source_file_path(project).exists())
        self.assertIn("SmartTeX", read_source_content(project))

    def test_template_markup_type_overrides_explicit_request_markup(self) -> None:
        template = Template.objects.create(
            title="Typst Template",
            content="= Template\n",
            markup_type=MarkupType.TYPST,
        )

        response = self.client.post(
            "/api/projects/",
            data=json.dumps({"title": "Mixed Request", "markup_type": "latex", "template_id": template.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        project = Project.objects.get(pk=response.json()["id"])
        self.assertEqual(project.markup_type, MarkupType.TYPST)
        self.assertEqual(read_source_content(project), "= Template\n")

    def test_create_project_text_file_supports_nested_paths(self) -> None:
        project = Project.objects.create(owner=self.user, title="Nested", markup_type=MarkupType.TYPST)

        asset = create_project_text_file(project, "chapters/intro.typ", "= Intro\n")
        listed = list_project_assets(project)

        self.assertEqual(asset["name"], "chapters/intro.typ")
        self.assertTrue((source_file_path(project).parent / "chapters" / "intro.typ").exists())
        self.assertEqual([item["name"] for item in listed], ["chapters/intro.typ"])

    def test_split_typst_sections_detects_heading_levels(self) -> None:
        chunks = split_typst_sections("Preface\n= Intro\nBody\n== Details\nMore\n")

        self.assertEqual([chunk.title for chunk in chunks], ["Преамбула / До першого розділу", "Intro", "Details"])
        self.assertEqual([chunk.command for chunk in chunks[1:]], ["heading1", "heading2"])

    def test_synctex_is_rejected_for_typst_projects(self) -> None:
        project = Project.objects.create(owner=self.user, title="No SyncTeX", markup_type=MarkupType.TYPST)

        with self.assertRaisesMessage(ValueError, "Source mapping is not available for Typst projects"):
            synctex_line_to_pdf(project, line=1)
