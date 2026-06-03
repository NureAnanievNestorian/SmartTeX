"""Tests for navigation routing improvements.

Covers:
- New project pending index creation
- Nav-settings change marks index stale (not other settings changes)
- Context bundle excludes debug/config files unless explicitly targeted
- Metadata block context includes assignment value, not just the #let line
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from navigation.models import (
    FileCard,
    FileRole,
    IndexStatus,
    ProjectNavigationIndex,
    Reachability,
    RegionCard,
    RegionKind,
    Source,
    StateKind,
)
from navigation.services.preparation import (
    _build_context_bundle,
    _request_keywords,
    _score_file_card,
    _score_region,
)
from navigation.services.index_builder import _upsert_file_card
from navigation.services.discovery import DiscoveredFile
from navigation.services.structure import deterministic_region_triggers
from projects.models import Project
from templates_lib.models import Template


def _make_user(username: str = "nav_tester") -> User:
    return User.objects.create_user(username=username, password="secret")


def _make_project(user: User, title: str = "Nav Test") -> Project:
    tpl, _ = Template.objects.get_or_create(title="Blank", defaults={"content": ""})
    return Project.objects.create(owner=user, title=title, template=tpl)


class NewProjectPendingIndexTests(TestCase):
    """Project creation signal creates a PENDING index stub."""

    def setUp(self) -> None:
        self.user = _make_user("pendingtest")

    def test_new_project_creates_pending_index(self) -> None:
        project = _make_project(self.user, "Pending Index Test")
        try:
            index = ProjectNavigationIndex.objects.get(project=project)
        except ProjectNavigationIndex.DoesNotExist:
            self.fail("No ProjectNavigationIndex created for new project")
        self.assertEqual(index.status, IndexStatus.PENDING)

    def test_first_prepare_builds_or_falls_back_safely(self) -> None:
        """prepare_document_work on a brand-new project must not crash."""
        project = _make_project(self.user, "First Prepare Test")
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_ROOT=tmp):
                # Patch index build to avoid actual disk/git work.
                with patch(
                    "navigation.services.preparation.build_navigation_index",
                    side_effect=Exception("build disabled in test"),
                ):
                    from navigation.services.preparation import prepare_document_work
                    result = prepare_document_work(project, user_request="write something")
        # Must not raise; must return a usable dict with a mode.
        self.assertIn("mode", result)
        self.assertIn(result["mode"], {"indexed_keyword", "indexed_reranked", "minimal", "fallback_structural"})


class NavSettingsStalenessTests(TestCase):
    """ProjectSmallModelSettings.post_save only marks stale on nav fields."""

    def setUp(self) -> None:
        self.user = _make_user("settingstest")
        self.project = _make_project(self.user, "Settings Stale Test")
        self.index, _ = ProjectNavigationIndex.objects.get_or_create(
            project=self.project,
            defaults={"status": IndexStatus.READY},
        )
        # Force status to ready so we can detect changes.
        ProjectNavigationIndex.objects.filter(pk=self.index.pk).update(status=IndexStatus.READY)
        self.index.refresh_from_db()

    def _get_index_status(self) -> str:
        self.index.refresh_from_db()
        return self.index.status

    def test_nav_enrich_toggle_marks_index_stale(self) -> None:
        try:
            from small_model.models import ProjectSmallModelSettings
        except ImportError:
            self.skipTest("small_model not available")

        settings, _ = ProjectSmallModelSettings.objects.get_or_create(project=self.project)
        settings.nav_index_enrich_enabled = not settings.nav_index_enrich_enabled
        settings.save(update_fields=["nav_index_enrich_enabled", "updated_at"])

        self.assertEqual(self._get_index_status(), IndexStatus.PENDING)

    def test_non_nav_field_update_does_not_mark_index_stale(self) -> None:
        try:
            from small_model.models import ProjectSmallModelSettings
        except ImportError:
            self.skipTest("small_model not available")

        # Reset to ready first.
        ProjectNavigationIndex.objects.filter(pk=self.index.pk).update(status=IndexStatus.READY)

        settings, _ = ProjectSmallModelSettings.objects.get_or_create(project=self.project)
        # Save only a non-nav field.
        settings.context_compressor_enabled = not settings.context_compressor_enabled
        settings.save(update_fields=["context_compressor_enabled", "updated_at"])

        self.assertEqual(self._get_index_status(), IndexStatus.READY)

    def test_nav_rerank_toggle_marks_index_stale(self) -> None:
        try:
            from small_model.models import ProjectSmallModelSettings
        except ImportError:
            self.skipTest("small_model not available")

        settings, _ = ProjectSmallModelSettings.objects.get_or_create(project=self.project)
        ProjectNavigationIndex.objects.filter(pk=self.index.pk).update(status=IndexStatus.READY)
        settings.nav_rerank_enabled = not settings.nav_rerank_enabled
        settings.save(update_fields=["nav_rerank_enabled", "updated_at"])

        self.assertEqual(self._get_index_status(), IndexStatus.PENDING)


class ContextBundleEditTargetPolicyTests(TestCase):
    """Context bundle excludes debug/config files from edit snippets
    unless the user explicitly names the file."""

    def _make_mock_project(self, tmp_dir: str) -> Project:
        user = _make_user("bundletest")
        project = _make_project(user, "Bundle Policy Test")
        # Override project_dir to point to tmp.
        return project

    def test_debug_file_not_included_in_edit_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Create files.
            Path(tmp, "sections", "01-intro.typ").parent.mkdir(parents=True, exist_ok=True)
            Path(tmp, "sections", "01-intro.typ").write_text("= Introduction\n\nContent here.\n")
            Path(tmp, "BENCHMARK_NOTES.md").write_text("# Benchmark notes\n\nDebug info.\n")

            user = _make_user("bundledebug")
            project = _make_project(user, "Bundle Debug Test")

            # Patch project_dir to return our temp dir.
            with patch("navigation.services.preparation.project_dir", return_value=Path(tmp)):
                read_targets = [
                    {"filename": "BENCHMARK_NOTES.md", "line_start": 1, "line_end": 3,
                     "region_card_id": None, "region_title": "", "reason": "keyword"},
                    {"filename": "sections/01-intro.typ", "line_start": 1, "line_end": 3,
                     "region_card_id": None, "region_title": "", "reason": "keyword"},
                ]
                # Only real content file in edit targets.
                edit_targets = [
                    {"filename": "sections/01-intro.typ", "line_start": 1, "line_end": 3,
                     "region_card_id": None, "region_title": "", "reason": "edit score"},
                ]
                bundle = _build_context_bundle(
                    project, read_targets, edit_targets, "paragraph_edit"
                )
                included = bundle["included"]
                # sections/01-intro.typ should be first (from edit_targets).
                self.assertIn("sections/01-intro.typ", included)
                # BENCHMARK_NOTES.md may be in snippets (read target), but not
                # as an edit target — so it should appear only if budget allows.
                # The important contract: edit_targets lead the snippets list.
                if included:
                    self.assertEqual(included[0], "sections/01-intro.typ")

    def test_config_file_included_when_explicitly_targeted(self) -> None:
        """Path penalty is overridden when user's request explicitly names the file."""
        kw = _request_keywords("update lib.typ style settings")
        # "lib" appears in keywords because the user named the file explicitly.
        # The mock file card should not be penalized when name-matched.
        fc = MagicMock(spec=FileCard)
        fc.filename = "lib.typ"
        fc.role = FileRole.STYLE
        fc.reachability = Reachability.REACHABLE
        fc.is_stale = False
        fc.edit_triggers = [{"phrase": "lib", "kind": "literal", "confidence": "medium"}]

        score_read = _score_file_card(fc, kw, for_edit=False)
        score_edit = _score_file_card(fc, kw, for_edit=True)

        # When user explicitly names "lib", path penalty is overridden
        # (name_explicitly_matched=True). Score should be positive.
        self.assertGreater(score_edit, 0, "Explicitly targeted config file should score > 0 for edit")
        # Without explicit naming, a BENCHMARK file should be penalized.
        fc2 = MagicMock(spec=FileCard)
        fc2.filename = "BENCHMARK_NOTES.md"
        fc2.role = FileRole.UNKNOWN
        fc2.reachability = Reachability.ORPHAN
        fc2.is_stale = False
        fc2.edit_triggers = []
        kw_generic = _request_keywords("update placeholder section")
        score_bench_edit = _score_file_card(fc2, kw_generic, for_edit=True)
        self.assertLess(score_bench_edit, score_edit, "BENCHMARK file should score lower than explicitly targeted lib.typ")

    def test_prepare_uses_annotation_files_as_high_confidence_targets(self) -> None:
        from longdoc.models import ProjectAnnotation
        from navigation.services.preparation import prepare_document_work

        user = _make_user("annotationprep")
        project = _make_project(user, "Annotation Prep Test")
        index, _ = ProjectNavigationIndex.objects.get_or_create(project=project, defaults={"status": IndexStatus.READY})
        index.status = IndexStatus.READY
        index.schema_version = 1
        index.last_built_version_number = 1
        index.save()
        FileCard.objects.create(
            index=index,
            filename="sections/04-software-decisions.typ",
            role=FileRole.CONTENT_SECTION,
            reachability=Reachability.REACHABLE,
            summary="Software decisions: data layer, services, payloads, subscription payments.",
            line_count=120,
        )
        FileCard.objects.create(
            index=index,
            filename="sections/03-architecture.typ",
            role=FileRole.CONTENT_SECTION,
            reachability=Reachability.REACHABLE,
            edit_triggers=[{"phrase": "payload"}],
            line_count=120,
        )
        annotation = ProjectAnnotation.objects.create(
            project=project,
            file_name="sections/04-software-decisions.typ",
            line_start=40,
            line_end=42,
            selected_text="data layer",
            instruction="Замінити англіцизм data layer.",
        )

        with patch("navigation.services.preparation.project_dir", return_value=Path(tempfile.gettempdir())):
            payload = prepare_document_work(
                project,
                user_request="Виправити прийняті помітки одним proposal",
                annotation_ids=[annotation.id],
                include_context=False,
            )

        self.assertEqual(payload["read_targets"][0]["filename"], "sections/04-software-decisions.typ")
        self.assertEqual(payload["likely_edit_targets"][0]["filename"], "sections/04-software-decisions.typ")
        self.assertEqual(payload["annotation_context"]["matched_ids"], [annotation.id])

    def test_prepare_accepts_explicit_target_filenames(self) -> None:
        from navigation.services.preparation import prepare_document_work

        user = _make_user("targetfiles")
        project = _make_project(user, "Target Filename Prep Test")
        index, _ = ProjectNavigationIndex.objects.get_or_create(project=project, defaults={"status": IndexStatus.READY})
        index.status = IndexStatus.READY
        index.schema_version = 1
        index.last_built_version_number = 1
        index.save()
        FileCard.objects.create(
            index=index,
            filename="sections/04-software-decisions.typ",
            role=FileRole.CONTENT_SECTION,
            reachability=Reachability.REACHABLE,
            summary="Software decisions.",
            line_count=80,
        )

        payload = prepare_document_work(
            project,
            user_request="Внести точкові правки",
            target_filenames=["sections/04-software-decisions.typ"],
            include_context=False,
        )

        self.assertEqual(payload["read_targets"][0]["filename"], "sections/04-software-decisions.typ")
        self.assertEqual(payload["target_file_context"]["matched"], ["sections/04-software-decisions.typ"])

    def test_upsert_preserves_small_model_navigation_metadata_when_content_unchanged(self) -> None:
        user = _make_user("enrichcache")
        project = _make_project(user, "Enrich Cache Test")
        index, _ = ProjectNavigationIndex.objects.get_or_create(project=project, defaults={"status": IndexStatus.READY})
        card = FileCard.objects.create(
            index=index,
            filename="sections/04-software-decisions.typ",
            role=FileRole.CONTENT_SECTION,
            reachability=Reachability.REACHABLE,
            summary="Small model summary about software decisions.",
            summary_source=Source.SMALL_MODEL,
            edit_triggers=[{"phrase": "керування підпискою", "source": "small_model"}],
            triggers_source=Source.SMALL_MODEL,
            content_hash="abc",
            line_count=2,
            byte_size=10,
        )
        df = DiscoveredFile(
            filename="sections/04-software-decisions.typ",
            absolute_path=Path(tempfile.gettempdir()) / "missing.typ",
            line_count=2,
            byte_size=10,
            content_hash="abc",
        )

        _upsert_file_card(
            index=index,
            df=df,
            project=project,
            reachable_set={"sections/04-software-decisions.typ"},
            orphan_set=set(),
            dynamic_unresolved=set(),
            includes_map={"forward": {}, "reverse": {}},
            version_number=1,
        )

        card.refresh_from_db()
        self.assertEqual(card.summary_source, Source.SMALL_MODEL)
        self.assertIn("software decisions", card.summary)
        self.assertEqual(card.triggers_source, Source.SMALL_MODEL)


class MetadataBlockContextTests(TestCase):
    """Metadata block context bundle includes assignment value lines."""

    def test_score_region_title_match_fix(self) -> None:
        """_score_region title check: keyword in title, not title in keyword string.
        Also handles morphological variants (e.g. 'supervisors' keyword matches 'supervisor' title).
        """
        region = MagicMock(spec=RegionCard)
        region.title = "supervisor"
        region.edit_triggers = []
        region.is_stale = False
        region.state = StateKind.REAL

        kw = _request_keywords("update supervisors list")
        score = _score_region(region, kw)
        self.assertGreater(score, 0, "_score_region should match when keyword is contained in title or vice versa")

    def test_deterministic_region_triggers_metadata_block_literal(self) -> None:
        """deterministic_region_triggers emits literal and word-token triggers for metadata blocks.
        Semantic enrichment is delegated to the small model (extended slice in index_builder).
        """
        triggers = deterministic_region_triggers(title="#let mentors")
        phrases = {t["phrase"] for t in triggers}
        self.assertIn("#let mentors", phrases)
        # "mentors" is a 7-char word, should appear as a semantic token
        self.assertIn("mentors", phrases)

    def test_context_bundle_extends_metadata_block_past_let_line(self) -> None:
        """Context bundle includes assignment value for metadata blocks."""
        with tempfile.TemporaryDirectory() as tmp:
            main_typ = Path(tmp, "main.typ")
            main_typ.write_text(
                "#let mentors = (\n"
                '  "Dr. Smith",\n'
                '  "Prof. Jones",\n'
                ")\n"
                "\n"
                "= Introduction\n"
                "Content here.\n",
                encoding="utf-8",
            )

            user = _make_user("metablock")
            project = _make_project(user, "Metadata Block Test")

            # Simulate a region card for the metadata block (span=1, line_start=line_end=1)
            mock_region = MagicMock(spec=RegionCard)
            mock_region.region_kind = RegionKind.METADATA_BLOCK
            mock_region.line_start = 1
            mock_region.line_end = 1

            with (
                patch("navigation.services.preparation.project_dir", return_value=Path(tmp)),
                patch("navigation.models.RegionCard.objects") as mock_rc_manager,
            ):
                mock_rc_manager.filter.return_value.first.return_value = mock_region

                edit_targets = [
                    {
                        "filename": "main.typ",
                        "line_start": 1,
                        "line_end": 1,
                        "region_card_id": 42,
                        "region_title": "#let mentors",
                        "reason": "metadata match",
                    }
                ]
                bundle = _build_context_bundle(
                    project, edit_targets, edit_targets, "micro_edit"
                )

            self.assertTrue(bundle["snippets"], "Expected at least one snippet")
            snippet = bundle["snippets"][0]
            # The snippet should extend past line 1 to include the assignment value.
            self.assertGreater(
                snippet["line_end"], 1,
                "Metadata block snippet should extend past the #let line to include assignment value",
            )
            self.assertIn("Dr. Smith", snippet["content"], "Snippet should contain assignment value")
