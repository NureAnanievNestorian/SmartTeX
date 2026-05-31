import json
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

from .deepseek_provider import DeepSeekProvider
from .models import ProjectSmallModelSettings, UserSmallModelAccess, UserSmallModelQuota
from .gemini_provider import GeminiProvider
from .provider import SmallModelResponse
from .registry import get_provider
from . import schemas
from .services.circuit_breaker import CircuitBreakerService
from .services.compile_log_triage import CompileLogTriageService
from .services.diff_utils import build_diff_review_input
from .services.do_not_touch import validate_do_not_touch
from .services.edit_intent_classifier import sanitize_smcl_edit_intent, CONSERVATIVE_PARAGRAPH
from .services.policy_engine import ProposalPolicyEngine
from .services.pre_proposal import PreProposalAnalysisService, _is_safe_path, _sanitize_candidate_files, _sanitize_read_plan
from .services.quota_service import SmallModelQuotaService
from .task_types import FEATURE_DIFF_SAFETY_REVIEWER, FEATURE_EDIT_INTENT_CLASSIFIER


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

    def test_gemini_provider_repairs_truncated_json_object(self) -> None:
        provider = GeminiProvider(api_key="test-key", model_name="gemini-test")

        parsed = provider._try_repair_json_object('{"allowed_ops":["replace"],"clarification_reason":null,')

        self.assertEqual(parsed, {"allowed_ops": ["replace"], "clarification_reason": None})

    def test_deepseek_provider_repairs_truncated_json_object(self) -> None:
        provider = DeepSeekProvider(api_key="test-key", model_name="deepseek-test")

        parsed = provider._try_repair_json_object('{"allowed_ops":["replace"],"clarification_reason":null,')

        self.assertEqual(parsed, {"allowed_ops": ["replace"], "clarification_reason": None})

    @override_settings(DEEPSEEK_API_KEY="test-key")
    def test_registry_returns_deepseek_provider(self) -> None:
        provider = get_provider("deepseek")

        self.assertIsInstance(provider, DeepSeekProvider)

    def test_deepseek_provider_parses_chat_completion_json(self) -> None:
        provider = DeepSeekProvider(api_key="test-key", model_name="deepseek-v4-flash", config={"temperature": 0.1, "max_output_tokens": 256, "thinking_type": "disabled"})
        response_body = json_bytes({
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"content": '{"risk_level":"low","recommendation":"allow"}'}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        })

        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse(response_body)) as urlopen_mock:
            response = provider.generate_json(
                task_type="diff_safety_review",
                system_instruction="Return JSON.",
                input_payload={"diff": "tiny"},
                response_schema={"type": "object"},
                user=self.user,
                project=self.project,
                timeout_seconds=9,
            )

        self.assertTrue(response.success)
        self.assertEqual(response.provider_name, "deepseek")
        self.assertEqual(response.model_name, "deepseek-v4-flash")
        self.assertEqual(response.parsed_json, {"risk_level": "low", "recommendation": "allow"})
        self.assertEqual(response.input_tokens_estimate, 12)
        self.assertEqual(response.output_tokens_estimate, 7)
        request = urlopen_mock.call_args.args[0]
        self.assertEqual(urlopen_mock.call_args.kwargs["timeout"], 9)
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")

    def test_pre_proposal_fast_path_for_simple_replace_request(self) -> None:
        result = PreProposalAnalysisService()._deterministic_fast_path(
            "Замінити формулювання «інформаційна система» на «вебзастосунок» без інших змін."
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["edit_intent"]["edit_mode"], "micro_edit")
        self.assertEqual(result["edit_intent"]["max_changed_lines"], 5)
        self.assertEqual(result["edit_intent"]["max_files"], 1)

    def test_pre_proposal_fast_path_skips_broad_scope_requests(self) -> None:
        result = PreProposalAnalysisService()._deterministic_fast_path(
            "Замінити формулювання і додати новий розділ з описом системи."
        )

        self.assertIsNone(result)

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
    def test_reviewer_enabled_warns_over_budget_when_provider_unavailable(self) -> None:
        access = UserSmallModelAccess.objects.create(user=self.user, enabled=True)
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

        provider = mock.Mock()
        provider.provider_name = "mock"
        provider.model_name = "mock"
        provider.generate_json.side_effect = RuntimeError("no provider")

        with mock.patch("small_model.services.base.get_provider", return_value=provider):
            result = ProposalPolicyEngine.post_patch_check(self.user, self.project, proposal, diff)

        self.assertEqual(result.action, "warn")
        self.assertEqual(result.risk_level, "medium")
        self.assertEqual(result.warnings[0]["code"], "OVEREDIT_RISK")
        self.assertIn("SMCL_BUDGET_ADVISORY", {item["code"] for item in result.warnings})
        self.assertTrue(result.smcl_used)

    def test_diff_review_skips_provider_for_tiny_low_risk_diff(self) -> None:
        diff = (
            "diff --git a/main.typ b/main.typ\n"
            "--- a/main.typ\n"
            "+++ b/main.typ\n"
            "@@ -10,1 +10,1 @@\n"
            '-"інформаційна система"\n'
            '+"вебзастосунок"\n'
        )
        with mock.patch("small_model.services.diff_safety_reviewer.DiffSafetyReviewService.call_provider", side_effect=AssertionError("provider should not be called")):
            result = ProposalPolicyEngine.post_patch_check(
                self.user,
                self.project,
                ChangeProposal(
                    project=self.project,
                    goal="Replace one phrase",
                    smcl_metadata={"edit_intent": {"max_changed_lines": 15, "max_files": 1, "edit_mode": "paragraph_edit"}},
                ),
                diff,
            )

        self.assertEqual(result.action, "allow")
        self.assertFalse(result.smcl_used)

    @override_settings(SMALL_MODEL_FEATURE_ENABLED=True)
    def test_provider_narrower_patch_reject_without_semantic_risk_is_downgraded_to_warning(self) -> None:
        UserSmallModelAccess.objects.create(user=self.user, enabled=True)
        UserSmallModelQuota.objects.create(user=self.user)
        ProjectSmallModelSettings.objects.create(
            project=self.project,
            small_model_control_enabled=True,
            diff_safety_reviewer_enabled=True,
        )
        proposal = ChangeProposal(
            project=self.project,
            goal="Revise two nearby blocks",
            smcl_metadata={"edit_intent": {"max_changed_lines": 10, "max_files": 1, "edit_mode": "paragraph_edit"}},
        )
        diff = (
            "diff --git a/main.tex b/main.tex\n"
            "--- a/main.tex\n"
            "+++ b/main.tex\n"
            "@@ -1,3 +1,5 @@\n"
            "-a\n-b\n-c\n+d\n+e\n+f\n"
        )
        provider = mock.Mock()
        provider.provider_name = "mock"
        provider.model_name = "mock"
        provider.generate_json.return_value = SmallModelResponse(
            success=True,
            parsed_json={
                "risk_level": "medium",
                "overedit_detected": False,
                "unrelated_changes_detected": False,
                "suspicious_deletions": [],
                "deleted_labels_or_refs": [],
                "changed_imports_or_includes": [],
                "recommendation": "reject_and_request_narrower_patch",
                "rejection_reason": "Too broad for paragraph scope.",
            },
            provider_name="mock",
            model_name="mock",
            input_tokens_estimate=10,
            output_tokens_estimate=10,
        )

        with mock.patch("small_model.services.base.get_provider", return_value=provider):
            result = ProposalPolicyEngine.post_patch_check(self.user, self.project, proposal, diff)

        self.assertEqual(result.action, "warn")
        self.assertIn("SMCL_REJECT_DOWNGRADED", {item["code"] for item in result.warnings})

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
        access = UserSmallModelAccess.objects.create(user=self.user, enabled=True)
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
        access = UserSmallModelAccess.objects.create(user=self.user, enabled=True)
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
    def test_pre_proposal_includes_navigation_context_in_provider_payload(self) -> None:
        UserSmallModelAccess.objects.create(user=self.user, enabled=True)
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
                "compile_required": False,
                "requires_user_clarification": False,
                "clarification_reason": None,
            },
            provider_name="mock",
            model_name="mock",
            input_tokens_estimate=10,
            output_tokens_estimate=10,
        )

        with mock.patch("small_model.services.base.get_provider", return_value=provider), mock.patch(
            "small_model.services.pre_proposal._build_navigation_context",
            return_value={
                "entrypoint_file": "main.typ",
                "navigation_mode": "indexed_keyword",
                "navigation_warnings": [],
                "retrieved_targets": [
                    {
                        "filename": "sections/ch3.typ",
                        "region_title": "3.1 Аналіз",
                        "line_start": 10,
                        "line_end": 30,
                        "reason": "title match",
                        "confidence": "high",
                        "match_kind": "exact_match",
                        "file_role": "content_section",
                        "snippet": "3.1 Аналіз...",
                    }
                ],
            },
        ):
            ProposalPolicyEngine.pre_proposal_check(self.user, self.project, "Adjust section 3.1 ordering.")

        payload = provider.generate_json.call_args.kwargs["input_payload"]
        self.assertEqual(payload["navigation_context"]["entrypoint_file"], "main.typ")
        self.assertEqual(payload["navigation_context"]["retrieved_targets"][0]["filename"], "sections/ch3.typ")

    @override_settings(SMALL_MODEL_FEATURE_ENABLED=True)
    def test_compile_triage_skips_provider_for_obvious_log(self) -> None:
        access = UserSmallModelAccess.objects.create(user=self.user, enabled=True)
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


class SmclEnumValidationTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="enum-user", password="secret")
        self.template = Template.objects.create(title="Enum", content="")
        self.project = Project.objects.create(owner=self.user, title="Enum Project", template=self.template, main_file="main.tex")

    # --- edit_mode ---

    def test_invalid_edit_mode_triggers_fallback(self) -> None:
        result, fallback_used = sanitize_smcl_edit_intent({"edit_mode": "patch", "read_strategy": "range_only"})

        self.assertTrue(fallback_used)
        self.assertEqual(result["edit_mode"], CONSERVATIVE_PARAGRAPH["edit_mode"])

    def test_valid_edit_mode_is_accepted(self) -> None:
        _, fallback_used = sanitize_smcl_edit_intent({"edit_mode": "compile_fix", "read_strategy": "range_only"})

        self.assertFalse(fallback_used)

    def test_all_valid_edit_modes_accepted(self) -> None:
        for mode in schemas.VALID_EDIT_MODES:
            _, fallback_used = sanitize_smcl_edit_intent({"edit_mode": mode, "read_strategy": "range_only"})
            self.assertFalse(fallback_used, msg=f"edit_mode={mode!r} should not trigger fallback")

    # --- read_strategy ---

    def test_full_read_strategy_replaced_with_range_only(self) -> None:
        result, fallback_used = sanitize_smcl_edit_intent(
            {"edit_mode": "paragraph_edit", "read_strategy": "full", "recommended_read_strategy": "full_file"}
        )

        self.assertTrue(fallback_used)
        self.assertEqual(result["read_strategy"], "range_only")
        self.assertEqual(result["recommended_read_strategy"], "range_only")

    def test_valid_read_strategies_accepted(self) -> None:
        for strategy in schemas.VALID_READ_STRATEGIES:
            result, fallback_used = sanitize_smcl_edit_intent(
                {"edit_mode": "paragraph_edit", "read_strategy": strategy}
            )
            self.assertEqual(result["read_strategy"], strategy, msg=f"strategy={strategy!r} should be preserved")

    def test_target_file_if_under_cap_accepted(self) -> None:
        result, _ = sanitize_smcl_edit_intent(
            {"edit_mode": "paragraph_edit", "read_strategy": "target_file_if_under_cap"}
        )

        self.assertEqual(result["read_strategy"], "target_file_if_under_cap")

    # --- allowed_ops ---

    def test_generic_allowed_ops_stripped(self) -> None:
        result, fallback_used = sanitize_smcl_edit_intent(
            {"edit_mode": "paragraph_edit", "read_strategy": "range_only", "allowed_ops": ["edit", "read", "write"]}
        )

        self.assertTrue(fallback_used)
        self.assertNotIn("edit", result["allowed_ops"])
        self.assertNotIn("read", result["allowed_ops"])
        self.assertNotIn("write", result["allowed_ops"])

    def test_all_generic_allowed_ops_triggers_fallback_ops(self) -> None:
        result, fallback_used = sanitize_smcl_edit_intent(
            {"edit_mode": "paragraph_edit", "read_strategy": "range_only", "allowed_ops": ["edit", "delete", "create"]}
        )

        self.assertTrue(fallback_used)
        self.assertIn("patch_file_lines", result["allowed_ops"])

    def test_valid_allowed_ops_preserved(self) -> None:
        valid = ["grep_file", "read_file_lines", "patch_file_lines"]
        result, fallback_used = sanitize_smcl_edit_intent(
            {"edit_mode": "paragraph_edit", "read_strategy": "range_only", "allowed_ops": valid}
        )

        self.assertFalse(fallback_used)
        self.assertEqual(result["allowed_ops"], valid)

    # --- forbidden_ops ---

    def test_generic_forbidden_ops_stripped(self) -> None:
        result, fallback_used = sanitize_smcl_edit_intent(
            {"edit_mode": "paragraph_edit", "read_strategy": "range_only", "forbidden_ops": ["delete", "create"]}
        )

        self.assertTrue(fallback_used)
        self.assertNotIn("delete", result["forbidden_ops"])

    # --- budget caps ---

    def test_max_changed_lines_capped_to_budget(self) -> None:
        result, _ = sanitize_smcl_edit_intent(
            {"edit_mode": "micro_edit", "read_strategy": "range_only", "max_changed_lines": 999}
        )

        self.assertEqual(result["max_changed_lines"], 5)

    def test_max_read_lines_capped_to_global_cap(self) -> None:
        result, _ = sanitize_smcl_edit_intent(
            {"edit_mode": "paragraph_edit", "read_strategy": "range_only", "max_read_lines": 9999}
        )

        self.assertLessEqual(result["max_read_lines"], schemas.MAX_READ_LINES_CAP)

    # --- candidate_files ---

    def test_unsafe_path_removed_from_candidate_files(self) -> None:
        raw = [
            {"path": "../etc/passwd", "confidence": "high", "reason": "evil"},
            {"path": "src/lib.typ", "confidence": "high", "reason": "ok"},
        ]
        result = _sanitize_candidate_files(raw)

        paths = [f["path"] for f in result]
        self.assertNotIn("../etc/passwd", paths)
        self.assertIn("src/lib.typ", paths)

    def test_candidate_files_confidence_normalised(self) -> None:
        raw = [{"path": "src/lib.typ", "confidence": "SUPER_HIGH", "reason": "x"}]
        result = _sanitize_candidate_files(raw)

        self.assertEqual(result[0]["confidence"], "low")

    def test_candidate_files_capped_at_max(self) -> None:
        raw = [{"path": f"file{i}.typ", "confidence": "low", "reason": ""} for i in range(20)]
        result = _sanitize_candidate_files(raw)

        self.assertLessEqual(len(result), schemas.MAX_CANDIDATE_FILES)

    # --- read_plan ---

    def test_unsafe_tool_removed_from_read_plan(self) -> None:
        raw = [
            {"tool": "read_project_file", "target_file": "main.typ", "reason": "bad"},
            {"tool": "grep_file", "target_file": "src/lib.typ", "pattern": "bibliography(", "reason": "good"},
        ]
        result = _sanitize_read_plan(raw)

        tools = [s["tool"] for s in result]
        self.assertNotIn("read_project_file", tools)
        self.assertIn("grep_file", tools)

    def test_unsafe_target_file_in_read_plan_nulled(self) -> None:
        raw = [{"tool": "read_file_lines", "target_file": "../../etc/passwd", "reason": "bad path"}]
        result = _sanitize_read_plan(raw)

        self.assertIsNone(result[0]["target_file"])

    def test_read_plan_capped_at_max(self) -> None:
        raw = [{"tool": "grep_file", "target_file": f"file{i}.typ", "pattern": "x", "reason": ""} for i in range(20)]
        result = _sanitize_read_plan(raw)

        self.assertLessEqual(len(result), schemas.MAX_READ_PLAN_STEPS)

    # --- path safety ---

    def test_is_safe_path_rejects_traversal(self) -> None:
        self.assertFalse(_is_safe_path("../secret"))
        self.assertFalse(_is_safe_path("/etc/passwd"))
        self.assertFalse(_is_safe_path(""))

    def test_is_safe_path_accepts_normal_paths(self) -> None:
        self.assertTrue(_is_safe_path("src/lib.typ"))
        self.assertTrue(_is_safe_path("sources.yml"))
        self.assertTrue(_is_safe_path("csl/dstu-8302-2015.csl"))

    # --- provider fallback ---

    @override_settings(SMALL_MODEL_FEATURE_ENABLED=True)
    def test_provider_returning_invalid_json_uses_fallback(self) -> None:
        access = UserSmallModelAccess.objects.create(user=self.user, enabled=True)
        UserSmallModelQuota.objects.create(user=self.user)
        ProjectSmallModelSettings.objects.create(
            project=self.project,
            small_model_control_enabled=True,
            edit_intent_classifier_enabled=True,
        )
        bad_response = SmallModelResponse(success=False, error_code="PROVIDER_ERROR")

        with mock.patch("small_model.services.base.get_provider") as gp:
            gp.return_value.generate_json.return_value = bad_response
            result = ProposalPolicyEngine.pre_proposal_check(self.user, self.project, "Fix bibliography error in compile.")

        self.assertEqual(result.action, "allow")
        self.assertTrue(result.fallback_used)
        self.assertIn("edit_intent", result.metadata)

    @override_settings(SMALL_MODEL_FEATURE_ENABLED=True)
    def test_provider_returning_invalid_edit_mode_uses_fallback(self) -> None:
        access = UserSmallModelAccess.objects.create(user=self.user, enabled=True)
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
            parsed_json={"edit_mode": "patch", "read_strategy": "full", "allowed_ops": ["edit", "read"]},
            provider_name="mock",
            model_name="mock",
            input_tokens_estimate=5,
            output_tokens_estimate=5,
        )

        with mock.patch("small_model.services.base.get_provider", return_value=provider):
            result = ProposalPolicyEngine.pre_proposal_check(self.user, self.project, "Change the title.")

        self.assertEqual(result.action, "allow")
        self.assertTrue(result.fallback_used)
        edit_intent = result.metadata.get("edit_intent", {})
        self.assertIn(edit_intent.get("edit_mode"), schemas.VALID_EDIT_MODES)
        self.assertIn(edit_intent.get("read_strategy"), schemas.VALID_READ_STRATEGIES)

    # --- deterministic compile fast path ---

    def test_compile_request_uses_compile_fix_fast_path(self) -> None:
        result = PreProposalAnalysisService()._deterministic_fast_path(
            "Fix the bibliography compile error — sources.yml path is wrong."
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["edit_intent"]["edit_mode"], "compile_fix")
        self.assertTrue(result["edit_intent"]["compile_required"])

    def test_smcl_fallback_used_false_on_deterministic_fast_path(self) -> None:
        result = PreProposalAnalysisService()._deterministic_fast_path(
            "Замінити формулювання «інформаційна система» на «вебзастосунок»."
        )

        self.assertIsNotNone(result)
        self.assertFalse(result["smcl_fallback_used"])

    # --- context_compressor includes candidate_files and read_plan ---

    @override_settings(SMALL_MODEL_FEATURE_ENABLED=True)
    def test_pre_proposal_returns_candidate_files_and_read_plan(self) -> None:
        access = UserSmallModelAccess.objects.create(user=self.user, enabled=True)
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
                "task_brief": "Fix bib path",
                "edit_mode": "compile_fix",
                "read_strategy": "range_only",
                "recommended_read_strategy": "range_only",
                "max_read_lines": 120,
                "max_changed_lines": 20,
                "max_files": 2,
                "allowed_ops": ["grep_file", "read_file_lines", "patch_file_lines"],
                "forbidden_ops": ["read_project_file", "update_project_file"],
                "compile_required": True,
                "requires_user_clarification": False,
                "candidate_files": [
                    {"path": "src/lib.typ", "confidence": "high", "reason": "Contains bibliography() call"},
                    {"path": "../evil", "confidence": "high", "reason": "evil path"},
                ],
                "read_plan": [
                    {"tool": "grep_file", "target_file": "src/lib.typ", "pattern": "bibliography(", "context_lines": 5, "reason": "Find bib call"},
                    {"tool": "read_project_file", "target_file": "main.typ", "reason": "bad tool"},
                ],
                "forbidden_reads": ["read_project_file", "full_project_read"],
            },
            provider_name="mock",
            model_name="mock",
            input_tokens_estimate=10,
            output_tokens_estimate=20,
        )

        with mock.patch("small_model.services.base.get_provider", return_value=provider):
            result = ProposalPolicyEngine.pre_proposal_check(self.user, self.project, "Adjust references configuration.")

        compressor = result.metadata.get("context_compressor", {})
        candidate_paths = [f["path"] for f in compressor.get("candidate_files", [])]
        self.assertIn("src/lib.typ", candidate_paths)
        self.assertNotIn("../evil", candidate_paths)
        read_plan_tools = [s["tool"] for s in compressor.get("read_plan", [])]
        self.assertIn("grep_file", read_plan_tools)
        self.assertNotIn("read_project_file", read_plan_tools)


class FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def json_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")
