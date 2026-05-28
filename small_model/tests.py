from datetime import timedelta
from pathlib import Path
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from projects.models import Project
from projects.services import project_dir
from templates_lib.models import Template

from longdoc.models import ChangeProposal
from longdoc.session_service import SessionWriteError
from longdoc.proposal_service import serialize_change_proposal

from .models import ProjectSmallModelSettings, UserSmallModelAccess, UserSmallModelFeatureGrant, UserSmallModelQuota
from .provider import SmallModelResponse
from .services.circuit_breaker import CircuitBreakerService
from .services.compile_log_triage import CompileLogTriageService
from .services.diff_utils import build_diff_review_input
from .services.do_not_touch import validate_do_not_touch
from .services.policy_engine import ProposalPolicyEngine
from .services.quota_service import SmallModelQuotaService
from .task_types import FEATURE_DIFF_SAFETY_REVIEWER


class SmallModelControlLayerTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="smcl-user", password="secret")
        self.template = Template.objects.create(title="SMCL", content="")
        self.project = Project.objects.create(owner=self.user, title="SMCL Project", template=self.template, main_file="main.tex")

    def test_quota_requires_enabled_access_record(self) -> None:
        UserSmallModelQuota.objects.create(user=self.user)

        result = SmallModelQuotaService.check_quota(self.user)

        self.assertFalse(result.quota_ok)
        self.assertEqual(result.reason, "small_model_access_disabled")

    def test_diff_stats_use_full_diff_when_body_is_truncated(self) -> None:
        diff = "diff --git a/main.tex b/main.tex\n--- a/main.tex\n+++ b/main.tex\n@@ -1,1 +1,1 @@\n"
        diff += "".join(f"-old line {idx} \\\\label{{x{idx}}}\n+new line {idx}\n" for idx in range(500))

        review_input = build_diff_review_input(diff, soft_limit=1024, hard_cap=1500)

        self.assertTrue(review_input.diff_stats["diff_truncated"])
        self.assertEqual(review_input.diff_stats["lines_added"], 500)
        self.assertEqual(review_input.diff_stats["lines_removed"], 500)
        self.assertGreater(len(review_input.deleted_labels_or_refs), 0)
        self.assertLessEqual(len(review_input.unified_diff), 1600)

    @override_settings(SMALL_MODEL_FEATURE_ENABLED=False)
    def test_policy_skips_diff_budget_when_smcl_disabled(self) -> None:
        proposal = ChangeProposal(
            project=self.project,
            goal="Large change",
            smcl_metadata={"edit_intent": {"max_changed_lines": 1, "max_files": 1, "edit_mode": "micro_edit"}},
        )
        diff = "diff --git a/main.tex b/main.tex\n--- a/main.tex\n+++ b/main.tex\n@@ -1,1 +1,3 @@\n-a\n+b\n+c\n+d\n"

        result = ProposalPolicyEngine.post_patch_check(self.user, self.project, proposal, diff)

        self.assertEqual(result.action, "allow")
        self.assertFalse(result.smcl_used)

    @override_settings(SMALL_MODEL_FEATURE_ENABLED=True)
    def test_reviewer_enabled_rejects_over_budget_when_provider_unavailable(self) -> None:
        access = UserSmallModelAccess.objects.create(user=self.user, enabled=True, provider="mock")
        UserSmallModelFeatureGrant.objects.create(access=access, feature_key=FEATURE_DIFF_SAFETY_REVIEWER)
        UserSmallModelQuota.objects.create(user=self.user)
        ProjectSmallModelSettings.objects.create(
            project=self.project,
            small_model_control_enabled=True,
            diff_safety_reviewer_enabled=True,
        )
        proposal = ChangeProposal(
            project=self.project,
            goal="Tiny change",
            smcl_metadata={"edit_intent": {"max_changed_lines": 1, "max_files": 1, "edit_mode": "micro_edit"}},
        )
        diff = "diff --git a/main.tex b/main.tex\n--- a/main.tex\n+++ b/main.tex\n@@ -1,1 +1,3 @@\n-a\n+b\n+c\n+d\n"

        with mock.patch("small_model.services.base.get_provider", side_effect=RuntimeError("no provider")):
            result = ProposalPolicyEngine.post_patch_check(self.user, self.project, proposal, diff)

        self.assertEqual(result.action, "reject")
        self.assertEqual(result.risk_level, "high")
        self.assertEqual(result.warnings[0]["code"], "OVEREDIT_RISK")

    def test_serializer_exposes_smcl_fields_at_top_level(self) -> None:
        proposal = ChangeProposal.objects.create(
            project=self.project,
            goal="Warn user",
            status=ChangeProposal.Status.READY_FOR_REVIEW,
            expires_at=timezone.now() + timedelta(days=1),
            smcl_risk_level="medium",
            smcl_warnings=[{"severity": "medium", "code": "OVEREDIT_RISK", "message": "Risk", "source": "test"}],
        )

        payload = serialize_change_proposal(proposal)

        self.assertEqual(payload["smcl_risk_level"], "medium")
        self.assertEqual(payload["smcl_warnings"][0]["code"], "OVEREDIT_RISK")

    def test_do_not_touch_file_marker_rejects_patch(self) -> None:
        with override_settings(MEDIA_ROOT=Path(self.enterContext(tempfile.TemporaryDirectory()))):
            target_dir = project_dir(self.project)
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "main.tex").write_text("% smcl:do_not_touch:file\nHello", encoding="utf-8")
            with self.assertRaises(SessionWriteError) as ctx:
                validate_do_not_touch(
                    self.project,
                    [{"filename": "main.tex", "op": "replace_text", "old_text": "Hello", "new_text": "Hi"}],
                )
        self.assertEqual(ctx.exception.error, "DO_NOT_TOUCH_FILE")

    def test_post_compile_check_returns_quiet_allow_when_smcl_disabled(self) -> None:
        proposal = ChangeProposal.objects.create(
            project=self.project,
            goal="Compile failure",
            status=ChangeProposal.Status.VALIDATING,
            expires_at=timezone.now() + timedelta(days=1),
            patch_ops=[{"filename": "main.tex", "op": "replace_text"}],
        )

        result = ProposalPolicyEngine.post_compile_check(
            self.user,
            self.project,
            proposal,
            {"status": "error", "log": "! Undefined control sequence", "diagnostics": []},
        )

        self.assertEqual(result.action, "allow")
        self.assertFalse(result.warnings)
        self.assertEqual(result.risk_level, "low")

    def test_post_compile_check_stops_for_pre_existing_error_triage(self) -> None:
        proposal = ChangeProposal.objects.create(
            project=self.project,
            goal="Compile failure",
            status=ChangeProposal.Status.VALIDATING,
            expires_at=timezone.now() + timedelta(days=1),
            patch_ops=[{"filename": "main.tex", "op": "replace_text"}],
        )

        with mock.patch(
            "small_model.services.policy_engine.CompileLogTriageService.is_enabled",
            return_value=(True, None, None),
        ), mock.patch(
            "small_model.services.policy_engine.CompileLogTriageService.triage",
            return_value={"error_origin": "pre_existing_error", "safe_to_retry": False},
        ):
            result = ProposalPolicyEngine.post_compile_check(
                self.user,
                self.project,
                proposal,
                {"status": "error", "log": "pre-existing", "diagnostics": []},
            )

        self.assertEqual(result.action, "stop_and_ask_user")
        self.assertEqual(result.risk_level, "high")
        self.assertIn("pre-existing", result.reason)

    @override_settings(SMALL_MODEL_FEATURE_ENABLED=True)
    def test_circuit_breaker_provider_failure_tracks_unavailable_streak(self) -> None:
        access = UserSmallModelAccess.objects.create(user=self.user, enabled=True, provider="mock")
        UserSmallModelFeatureGrant.objects.create(access=access, feature_key="circuit_breaker")
        UserSmallModelQuota.objects.create(user=self.user)
        ProjectSmallModelSettings.objects.create(
            project=self.project,
            small_model_control_enabled=True,
            circuit_breaker_enabled=True,
        )
        first = SmallModelResponse(success=False, error_code="PROVIDER_ERROR")
        with mock.patch("small_model.services.base.get_provider") as get_provider_mock:
            get_provider_mock.return_value.generate_json.return_value = first
            result = CircuitBreakerService().evaluate(
                user=self.user,
                project=self.project,
                payload={"compile_failures": 1, "smcl_unavailable_streak": 1},
            )

        self.assertEqual(result["decision"], "narrow_scope")
        self.assertEqual(result["smcl_unavailable_streak"], 2)

    @override_settings(SMALL_MODEL_FEATURE_ENABLED=True)
    def test_pre_proposal_uses_single_provider_call(self) -> None:
        access = UserSmallModelAccess.objects.create(user=self.user, enabled=True, provider="mock")
        UserSmallModelFeatureGrant.objects.create(access=access, feature_key="edit_intent_classifier")
        UserSmallModelQuota.objects.create(user=self.user)
        ProjectSmallModelSettings.objects.create(
            project=self.project,
            small_model_control_enabled=True,
            edit_intent_classifier_enabled=True,
        )
        provider = mock.Mock()
        provider.provider_name = "mock"
        provider.model_name = "mock"
        provider.generate_json.return_value = SmallModelResponse(
            success=True,
            parsed_json={
                "task_brief": "tight scope",
                "relevant_files": [],
                "relevant_section_ids": [],
                "relevant_summaries": [],
                "do_not_touch_files": [],
                "do_not_touch_section_ids": [],
                "recommended_read_strategy": "range_only",
                "max_read_lines": 40,
                "edit_mode": "paragraph_edit",
                "allowed_ops": ["patch_file_lines"],
                "forbidden_ops": ["update_project_file"],
                "max_files": 1,
                "max_changed_lines": 10,
                "read_strategy": "range_only",
                "compile_required": True,
                "requires_user_clarification": False,
                "clarification_reason": None,
            },
            provider_name="mock",
            model_name="mock",
            input_tokens_estimate=10,
            output_tokens_estimate=10,
        )

        with mock.patch("small_model.services.base.get_provider", return_value=provider):
            result = ProposalPolicyEngine.pre_proposal_check(self.user, self.project, "Tighten one paragraph.")

        self.assertEqual(provider.generate_json.call_count, 1)
        self.assertEqual(result.action, "allow")
        self.assertIn("context_compressor", result.metadata)
        self.assertEqual(result.metadata["edit_intent"]["max_changed_lines"], 10)

    @override_settings(SMALL_MODEL_FEATURE_ENABLED=True)
    def test_compile_triage_skips_provider_for_obvious_log(self) -> None:
        access = UserSmallModelAccess.objects.create(user=self.user, enabled=True, provider="mock")
        UserSmallModelFeatureGrant.objects.create(access=access, feature_key="compile_log_triage")
        UserSmallModelQuota.objects.create(user=self.user)
        ProjectSmallModelSettings.objects.create(
            project=self.project,
            small_model_control_enabled=True,
            compile_log_triage_enabled=True,
        )

        with mock.patch("small_model.services.base.get_provider", side_effect=AssertionError("provider should not be called")):
            result = CompileLogTriageService().triage(
                user=self.user,
                project=self.project,
                compile_log="! Undefined control sequence.",
            )

        self.assertEqual(result["error_category"], "missing_import")
        self.assertFalse(result["safe_to_retry"])

    def test_post_compile_check_skips_breaker_provider_for_first_non_retryable_failure(self) -> None:
        proposal = ChangeProposal.objects.create(
            project=self.project,
            goal="Compile failure",
            status=ChangeProposal.Status.VALIDATING,
            expires_at=timezone.now() + timedelta(days=1),
            patch_ops=[{"filename": "main.tex", "op": "replace_text"}],
        )

        with mock.patch(
            "small_model.services.policy_engine.CompileLogTriageService.is_enabled",
            return_value=(True, None, None),
        ), mock.patch(
            "small_model.services.policy_engine.CompileLogTriageService.triage",
            return_value={"error_origin": "patch_error", "safe_to_retry": False, "retry_scope": "do_not_retry"},
        ), mock.patch(
            "small_model.services.policy_engine.CircuitBreakerService.evaluate",
            side_effect=AssertionError("breaker provider path should not be used"),
        ):
            result = ProposalPolicyEngine.post_compile_check(
                self.user,
                self.project,
                proposal,
                {"status": "error", "log": "! Undefined control sequence", "diagnostics": []},
            )

        self.assertEqual(result.action, "allow")
        self.assertEqual(result.risk_level, "medium")
