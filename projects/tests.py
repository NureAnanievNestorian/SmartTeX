import io
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings

from SmartTeX.markup import MarkupType
from projects.models import Project, ProjectVersion
from projects.pdf_embed_job import _IMPORT_LINE
from small_model.models import ProjectSmallModelSettings, UserSmallModelAccess, UserSmallModelQuota
from projects.services import (
    analyze_typst_project_import,
    build_typst_citation_index,
    create_project_text_file,
    list_project_assets,
    main_source_filename,
    parse_compile_diagnostics,
    project_git_dir,
    read_source_content,
    source_file_path,
    split_typst_sections,
    synctex_line_to_pdf,
)
from projects.typst_auto_generated import inject_auto_import_into_reachable_typst, remove_auto_imports_from_all_typst
from templates_lib.models import Template
from templates_lib.services import compile_template_preview, create_template_from_project, template_preview_dir


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
        self.assertEqual(project.project_mode, Project.ProjectMode.TYPST_IDE)
        self.assertTrue(project_git_dir(project).exists())
        self.assertIn('#include "chapters/01-introduction.typ"', read_source_content(project))
        self.assertTrue((source_file_path(project).parent / "chapters" / "01-introduction.typ").exists())

    def test_dashboard_project_creation_succeeds_without_git_on_path(self) -> None:
        with patch("projects.services.shutil.which", return_value=None):
            response = self.client.post(
                "/projects/new/",
                data={"title": "No Git Available", "markup_type": "latex"},
            )

        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(title="No Git Available")
        self.assertFalse(project_git_dir(project).exists())
        self.assertFalse(ProjectVersion.objects.filter(project=project).exists())

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
        self.assertEqual(project.project_mode, Project.ProjectMode.LEGACY)

    def test_project_detail_exposes_small_model_quota_warning_for_enabled_project(self) -> None:
        from decimal import Decimal
        project = Project.objects.create(owner=self.user, title="Quota Warning", markup_type=MarkupType.TYPST)
        UserSmallModelAccess.objects.create(user=self.user, enabled=True)
        UserSmallModelQuota.objects.create(
            user=self.user,
            credits_limit=Decimal("1.0"),
            credits_used=Decimal("1.0"),
        )
        ProjectSmallModelSettings.objects.create(project=project, small_model_control_enabled=True)

        response = self.client.get(f"/api/projects/{project.id}/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["small_model"]["enabled"])
        self.assertFalse(payload["small_model"]["quota_ok"])
        self.assertTrue(payload["small_model"]["quota_warning_visible"])
        self.assertEqual(payload["small_model"]["quota_reason"], "credits_limit_exceeded")

    def test_create_project_text_file_supports_nested_paths(self) -> None:
        project = Project.objects.create(owner=self.user, title="Nested", markup_type=MarkupType.TYPST)

        asset = create_project_text_file(project, "chapters/intro.typ", "= Intro\n")
        listed = list_project_assets(project)

        self.assertEqual(asset["name"], "chapters/intro.typ")
        self.assertTrue((source_file_path(project).parent / "chapters" / "intro.typ").exists())
        self.assertEqual([item["name"] for item in listed], ["chapters", "chapters/intro.typ"])

    def test_split_typst_sections_detects_heading_levels(self) -> None:
        chunks = split_typst_sections("Preface\n= Intro\nBody\n== Details\nMore\n")

        self.assertEqual([chunk.title for chunk in chunks], ["Преамбула / До першого розділу", "Intro", "Details"])
        self.assertEqual([chunk.command for chunk in chunks[1:]], ["heading1", "heading2"])

    def test_synctex_is_rejected_for_typst_projects(self) -> None:
        project = Project.objects.create(owner=self.user, title="No SyncTeX", markup_type=MarkupType.TYPST)

        with self.assertRaisesMessage(ValueError, "Source mapping is not available for Typst projects"):
            synctex_line_to_pdf(project, line=1)

    def test_updating_auxiliary_text_file_creates_git_backed_version(self) -> None:
        create_response = self.client.post(
            "/api/projects/",
            data=json.dumps({"title": "Git Versioned", "markup_type": "typst"}),
            content_type="application/json",
        )
        project_id = create_response.json()["id"]

        update_response = self.client.put(
            f"/api/projects/{project_id}/files/chapters%2F01-introduction.typ/content/",
            data=json.dumps({"content": "= Introduction\n\nUpdated body.\n"}),
            content_type="application/json",
        )

        self.assertEqual(update_response.status_code, 200)
        versions_response = self.client.get(f"/api/projects/{project_id}/versions/")
        self.assertEqual(versions_response.status_code, 200)
        versions = versions_response.json()["versions"]
        self.assertGreaterEqual(len(versions), 2)
        latest = versions[0]
        self.assertEqual(latest["target_file"], "chapters/01-introduction.typ")
        self.assertEqual(latest["snapshot_kind"], "text")
        self.assertTrue(latest["event_payload"].get("git_commit"))

    def test_creating_typst_chapter_updates_document_metadata(self) -> None:
        create_response = self.client.post(
            "/api/projects/",
            data=json.dumps({"title": "Metadata Sync", "markup_type": "typst"}),
            content_type="application/json",
        )
        project_id = create_response.json()["id"]

        response = self.client.post(
            f"/api/projects/{project_id}/files/",
            data=json.dumps({"filename": "chapters/06-extra.typ", "text_content": "= Extra\n\nNotes.\n"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        project = Project.objects.get(pk=project_id)
        chapter_paths = [row["path"] for row in project.document_metadata.get("chapters", [])]
        self.assertIn("chapters/06-extra.typ", chapter_paths)

    def test_reordering_typst_chapter_updates_metadata_and_main_file(self) -> None:
        create_response = self.client.post(
            "/api/projects/",
            data=json.dumps({"title": "Reorder Me", "markup_type": "typst"}),
            content_type="application/json",
        )
        project_id = create_response.json()["id"]

        response = self.client.post(
            f"/api/projects/{project_id}/document-order/",
            data=json.dumps({"kind": "chapters", "path": "chapters/02-theory.typ", "direction": "up"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        project = Project.objects.get(pk=project_id)
        chapter_paths = [row["path"] for row in project.document_metadata.get("chapters", [])]
        self.assertEqual(chapter_paths[:2], ["chapters/02-theory.typ", "chapters/01-introduction.typ"])
        main_text = read_source_content(project)
        self.assertLess(
            main_text.index('#include "chapters/02-theory.typ"'),
            main_text.index('#include "chapters/01-introduction.typ"'),
        )

    def test_compile_get_returns_structured_diagnostics_payload(self) -> None:
        create_response = self.client.post(
            "/api/projects/",
            data=json.dumps({"title": "Diagnostics", "markup_type": "typst"}),
            content_type="application/json",
        )
        project_id = create_response.json()["id"]

        response = self.client.get(f"/api/projects/{project_id}/compile/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("compile_state", payload)
        self.assertIn("diagnostics", payload)
        self.assertIsInstance(payload["diagnostics"], list)

    def test_document_metadata_endpoint_updates_typst_scaffold(self) -> None:
        create_response = self.client.post(
            "/api/projects/",
            data=json.dumps({"title": "Metadata", "markup_type": "typst"}),
            content_type="application/json",
        )
        project_id = create_response.json()["id"]

        response = self.client.patch(
            f"/api/projects/{project_id}/document-metadata/",
            data=json.dumps(
                {
                    "document_title": "Applied Title",
                    "author": "Alice Example",
                    "institution": "OpenAI University",
                    "bibliography_path": "refs/sources.bib",
                    "figure_paths": ["assets/figure-1.png"],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        project = Project.objects.get(pk=project_id)
        self.assertEqual(project.document_metadata["author"], "Alice Example")
        self.assertEqual(project.document_metadata["institution"], "OpenAI University")
        self.assertEqual(project.document_metadata["bibliography"]["path"], "refs/sources.bib")
        self.assertIn('#let doc-author = "Alice Example"', read_source_content(project))
        self.assertTrue((source_file_path(project).parent / "refs" / "sources.bib").exists())

    def test_typst_import_analysis_detects_structure_from_existing_files(self) -> None:
        create_response = self.client.post(
            "/api/projects/",
            data=json.dumps({"title": "Importable", "markup_type": "typst"}),
            content_type="application/json",
        )
        project_id = create_response.json()["id"]
        project = Project.objects.get(pk=project_id)

        payload = analyze_typst_project_import(project, use_existing_files=True)

        self.assertGreaterEqual(payload["detected_counts"]["chapters"], 1)
        self.assertEqual(payload["metadata"]["bibliography"]["path"], "bibliography/references.bib")

    def test_build_typst_citation_index_finds_yaml_keys_from_reachable_graph(self) -> None:
        project = Project.objects.create(owner=self.user, title="Citation Graph", markup_type=MarkupType.TYPST)
        root = source_file_path(project).parent
        root.mkdir(parents=True, exist_ok=True)
        (root / "chapters").mkdir(parents=True, exist_ok=True)
        (root / "main.typ").write_text(
            '#import "template.typ": coursework-v2\n'
            '#show: coursework-v2.with(bib-path: bytes(read("sources.yml")))\n'
            '#include "chapters/01-introduction.typ"\n',
            encoding="utf-8",
        )
        (root / "template.typ").write_text(
            '#let coursework-v2(doc, bib-path: none) = {\n'
            '  doc\n'
            '  bibliography(bib-path, style: "csl/dstu-8302-2015.csl")\n'
            '}\n',
            encoding="utf-8",
        )
        (root / "sources.yml").write_text(
            'spotify-web-api:\n'
            '  type: web\n'
            'another-source:\n'
            '  type: article\n',
            encoding="utf-8",
        )

        payload = build_typst_citation_index(project)

        self.assertEqual(payload["source_files"], ["sources.yml"])
        self.assertIn("main.typ", payload["reachable_files"])
        self.assertIn("template.typ", payload["reachable_files"])
        self.assertEqual(
            [item["key"] for item in payload["entries"]],
            ["another-source", "spotify-web-api"],
        )

    def test_typst_citations_endpoint_returns_reachable_yaml_entries(self) -> None:
        project = Project.objects.create(owner=self.user, title="Citation Endpoint", markup_type=MarkupType.TYPST)
        root = source_file_path(project).parent
        root.mkdir(parents=True, exist_ok=True)
        (root / "chapters").mkdir(parents=True, exist_ok=True)
        (root / "main.typ").write_text(
            '#show: doc.with(data: bytes(read("sources.yml")))\n'
            '#include "chapters/01-introduction.typ"\n',
            encoding="utf-8",
        )
        (root / "sources.yml").write_text(
            'spotify-web-api:\n'
            '  type: web\n',
            encoding="utf-8",
        )

        response = self.client.get(f"/api/projects/{project.id}/typst/citations/?prefix=spotify")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["entries"][0]["key"], "spotify-web-api")
        self.assertEqual(payload["entries"][0]["file"], "sources.yml")

    def test_hidden_git_repo_is_not_exposed_as_asset(self) -> None:
        create_response = self.client.post(
            "/api/projects/",
            data=json.dumps({"title": "Hidden Git", "markup_type": "typst"}),
            content_type="application/json",
        )
        project_id = create_response.json()["id"]

        response = self.client.get(f"/api/projects/{project_id}/files/")

        self.assertEqual(response.status_code, 200)
        names = [item["name"] for item in response.json()["files"]]
        self.assertFalse(any(".smarttex-git" in name for name in names))

    def test_project_zip_download_excludes_hidden_and_system_files(self) -> None:
        create_response = self.client.post(
            "/api/projects/",
            data=json.dumps({"title": "Zip Export", "markup_type": "typst"}),
            content_type="application/json",
        )
        project = Project.objects.get(pk=create_response.json()["id"])
        root = source_file_path(project).parent
        (root / ".hidden.txt").write_text("secret", encoding="utf-8")
        (root / "__MACOSX").mkdir(exist_ok=True)
        (root / "notes.txt").write_text("visible", encoding="utf-8")
        (root / "main.log").write_text("artifact", encoding="utf-8")
        (root / ".smarttex-git").mkdir(exist_ok=True)
        (root / ".smarttex-git" / "config").write_text("git", encoding="utf-8")

        response = self.client.get(f"/api/projects/{project.id}/download-zip/")

        self.assertEqual(response.status_code, 200)
        archive = b"".join(response.streaming_content)
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            names = set(zf.namelist())
        self.assertIn("main.typ", names)
        self.assertIn("notes.txt", names)
        self.assertNotIn(".hidden.txt", names)
        self.assertNotIn("main.log", names)
        self.assertFalse(any(name.startswith(".smarttex-git/") for name in names))

    def test_create_template_zip_includes_pdf_embed_support_files(self) -> None:
        project = Project.objects.create(owner=self.user, title="Template Export", markup_type=MarkupType.TYPST)
        root = source_file_path(project).parent
        helper = root / ".smarttex" / "auto_generated" / "pdf_includes.typ"
        manifest = root / ".smarttex" / "pdf_includes.json"
        cache_page = root / ".smarttex" / "cache" / "pdf-pages" / "thesis" / "page-001.jpg"
        helper.parent.mkdir(parents=True, exist_ok=True)
        cache_page.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("#let smarttex-include-pdf = none\n", encoding="utf-8")
        manifest.write_text('{"appendix.pdf": {"enabled": true}}', encoding="utf-8")
        cache_page.write_bytes(b"jpg")

        template = create_template_from_project(project, title="With PDF Embed")

        self.assertTrue(template.zip_file)
        template.zip_file.open("rb")
        with zipfile.ZipFile(template.zip_file) as zf:
            names = set(zf.namelist())

        self.assertIn(".smarttex/auto_generated/pdf_includes.typ", names)
        self.assertIn(".smarttex/pdf_includes.json", names)
        self.assertIn(".smarttex/cache/pdf-pages/thesis/page-001.jpg", names)

    def test_template_preview_runs_pre_compile_jobs(self) -> None:
        template = Template.objects.create(
            title="Template Preview",
            markup_type=MarkupType.TYPST,
            main_file="main.typ",
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("main.typ", '#smarttex-include-pdf("docs/sample.pdf")\n')
            zf.writestr(".smarttex/pdf_includes.json", '{"docs/sample.pdf":{"enabled":true}}')
            zf.writestr(".smarttex/auto_generated/pdf_includes.typ", "#let smarttex-include-pdf = none\n")
        template.zip_file.save("template.zip", ContentFile(buf.getvalue()))

        def fake_pre_compile(project, *, workdir):
            helper = workdir / ".smarttex" / "auto_generated" / "pdf_includes.typ"
            self.assertEqual(project.markup_type, MarkupType.TYPST)
            self.assertEqual(project.main_file, "main.typ")
            self.assertTrue(helper.exists())
            helper.write_text("#let smarttex-include-pdf(path) = path\n", encoding="utf-8")
            return []

        def fake_run(cmd, **kwargs):
            preview_dir = template_preview_dir(template)
            helper = preview_dir / ".smarttex" / "auto_generated" / "pdf_includes.typ"
            self.assertTrue(helper.exists())
            (preview_dir / "preview.pdf").write_bytes(b"%PDF-1.4 fake")
            return subprocess.CompletedProcess(cmd, 0, stdout="Success\n", stderr="")

        with patch("projects.pre_compile.run_pre_compile_jobs", side_effect=fake_pre_compile), patch(
            "subprocess.run", side_effect=fake_run
        ):
            result = compile_template_preview(template)

        self.assertEqual(result.status, "success")

    def test_parse_compile_diagnostics_extracts_typst_and_latex_locations(self) -> None:
        typst_project = Project.objects.create(owner=self.user, title="Typst Parse", markup_type=MarkupType.TYPST)
        latex_project = Project.objects.create(owner=self.user, title="Latex Parse", markup_type=MarkupType.LATEX)

        typst_diags = parse_compile_diagnostics(
            typst_project,
            'error: unexpected token\n --> chapters/01-introduction.typ:12:4\n',
        )
        latex_diags = parse_compile_diagnostics(
            latex_project,
            '! Undefined control sequence.\nl.42 \\badcommand\n',
        )

        self.assertEqual(typst_diags[0]["file"], "chapters/01-introduction.typ")
        self.assertEqual(typst_diags[0]["line"], 12)
        self.assertEqual(latex_diags[0]["line"], 42)

    def test_pdf_embed_import_is_injected_into_reachable_typst_tree_only(self) -> None:
        project = Project.objects.create(owner=self.user, title="PDF Embed Reachable", markup_type=MarkupType.TYPST)
        root = source_file_path(project).parent
        (root / "sections").mkdir(parents=True, exist_ok=True)
        (root / "main.typ").write_text(
            '#include "sections/a.typ"\n',
            encoding="utf-8",
        )
        (root / "sections" / "a.typ").write_text(
            '#include "b.typ"\n#smarttex-include-pdf("docs/file.pdf")\n',
            encoding="utf-8",
        )
        (root / "sections" / "b.typ").write_text(
            '#smarttex-include-pdf("docs/file.pdf")\n',
            encoding="utf-8",
        )
        (root / "orphan.typ").write_text(
            '#smarttex-include-pdf("docs/file.pdf")\n',
            encoding="utf-8",
        )

        inject_auto_import_into_reachable_typst(project, root, _IMPORT_LINE)

        self.assertIn(_IMPORT_LINE, (root / "main.typ").read_text(encoding="utf-8"))
        self.assertIn(_IMPORT_LINE, (root / "sections" / "a.typ").read_text(encoding="utf-8"))
        self.assertIn(_IMPORT_LINE, (root / "sections" / "b.typ").read_text(encoding="utf-8"))
        self.assertNotIn(_IMPORT_LINE, (root / "orphan.typ").read_text(encoding="utf-8"))

    def test_pdf_embed_remove_imports_cleans_all_typ_files(self) -> None:
        project = Project.objects.create(owner=self.user, title="PDF Embed Cleanup", markup_type=MarkupType.TYPST)
        root = source_file_path(project).parent
        auto_block = f"// smarttex:auto-begin\n{_IMPORT_LINE}\n// smarttex:auto-end\n"
        (root / "main.typ").write_text(auto_block + "= Main\n", encoding="utf-8")
        (root / "orphan.typ").write_text(auto_block + "= Orphan\n", encoding="utf-8")

        remove_auto_imports_from_all_typst(root)

        self.assertNotIn(_IMPORT_LINE, (root / "main.typ").read_text(encoding="utf-8"))
        self.assertNotIn(_IMPORT_LINE, (root / "orphan.typ").read_text(encoding="utf-8"))
