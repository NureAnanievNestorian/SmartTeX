from __future__ import annotations

from typing import Any

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.services.diff_utils import build_diff_review_input, warning
from small_model.task_types import FEATURE_DIFF_SAFETY_REVIEWER, TASK_DIFF_SAFETY_REVIEW


class DiffSafetyReviewService(SmallModelCallMixin):
    feature_key = FEATURE_DIFF_SAFETY_REVIEWER
    task_type = TASK_DIFF_SAFETY_REVIEW

    def review(
        self,
        *,
        user,
        project,
        proposal_goal: str,
        diff_text: str,
        edit_mode: str = "paragraph_edit",
        patch_budget: dict[str, int] | None = None,
        scope_confidence: str = "high",
        scope_confidence_reason: str | None = None,
    ) -> dict[str, Any]:
        patch_budget = patch_budget or {"max_changed_lines": 15, "max_files": 1}
        review_input = build_diff_review_input(diff_text)
        max_changed = int(patch_budget.get("max_changed_lines") or 15)
        stats = review_input.diff_stats
        # When the small model itself reported low/medium confidence in the
        # budget it picked, treat the budget as advisory rather than a hard
        # cap. The big model still sees an SMCL_BUDGET_ADVISORY warning telling
        # it to verify the diff is actually legitimate for the request.
        budget_advisory = scope_confidence in {"low", "medium"}
        deterministic_warnings: list[dict[str, str]] = []
        if stats["total_changed_lines"] > max_changed:
            deterministic_warnings.append(
                warning(
                    "medium",
                    "OVEREDIT_RISK",
                    "Diff is larger than expected for this edit scope.",
                    "deterministic_diff_stats",
                )
            )
            if budget_advisory:
                deterministic_warnings.append(
                    warning(
                        "medium",
                        "SMCL_BUDGET_ADVISORY",
                        (
                            f"Patch budget ({max_changed} lines) was advisory: small-model scope "
                            f"confidence was '{scope_confidence}'"
                            + (f" ({scope_confidence_reason})" if scope_confidence_reason else "")
                            + ". Verify that every change is required by the user's request before applying."
                        ),
                        "scope_confidence",
                    )
                )
        if review_input.deleted_labels_or_refs:
            deterministic_warnings.append(
                warning(
                    "medium",
                    "DELETED_LABEL_OR_REF",
                    "The diff deletes labels, references, citations, or Typst labels.",
                    "deterministic_diff_stats",
                )
            )
        if review_input.changed_imports_or_includes:
            deterministic_warnings.append(
                warning(
                    "medium",
                    "CHANGED_IMPORT_OR_INCLUDE",
                    "The diff changes imports, includes, or bibliography directives.",
                    "deterministic_diff_stats",
                )
            )
        if stats["total_changed_lines"] > max_changed and not budget_advisory:
            return {
                "action": "reject",
                "reason": "Diff exceeds the deterministic patch budget.",
                "risk_level": "high",
                "warnings": deterministic_warnings,
                "review_payload": {
                    "diff_stats": stats,
                    "changed_files": review_input.changed_files,
                    "touched_headings": review_input.touched_headings,
                    "deleted_labels_or_refs": review_input.deleted_labels_or_refs,
                    "changed_imports_or_includes": review_input.changed_imports_or_includes,
                    "unified_diff": review_input.unified_diff,
                },
            }
        if deterministic_warnings and not self._should_consult_provider(stats, review_input, max_changed):
            return {"action": "warn", "reason": None, "risk_level": "medium", "warnings": deterministic_warnings, "review_payload": {}}
        if self._is_tiny_low_risk_diff(stats, review_input):
            return {"action": "allow", "reason": None, "risk_level": "low", "warnings": [], "review_payload": {}}

        if (
            stats["diff_char_length"] > 12288
            and stats["total_changed_lines"] > max_changed * 2
            and not budget_advisory
        ):
            return {
                "action": "reject",
                "reason": "Diff is too large for the declared edit scope.",
                "risk_level": "high",
                "warnings": deterministic_warnings,
                "review_payload": self._payload(proposal_goal, edit_mode, patch_budget, review_input),
            }

        enabled, _, _ = self.is_enabled(user, project)
        if not enabled:
            return self._deterministic_fallback(
                stats, max_changed, deterministic_warnings, review_input, budget_advisory=budget_advisory
            )

        payload = self._payload(proposal_goal, edit_mode, patch_budget, review_input)
        payload["scope_confidence"] = scope_confidence
        if scope_confidence_reason:
            payload["scope_confidence_reason"] = scope_confidence_reason
        response = self.call_provider(
            user=user,
            project=project,
            system_instruction=(
                "Review the diff for over-editing, drift, accidental deletions, and unrelated changes. "
                "If scope_confidence is 'low' or 'medium', the patch budget is advisory — judge whether "
                "the diff is legitimate for the user's request rather than rejecting on size alone."
            ),
            input_payload=payload,
            response_schema=schemas.DIFF_SAFETY_SCHEMA,
        )
        if not response.success or not response.parsed_json:
            return self._deterministic_fallback(
                stats, max_changed, deterministic_warnings, review_input, budget_advisory=budget_advisory
            )

        result = response.parsed_json
        risk = str(result.get("risk_level") or "low")
        warnings = list(deterministic_warnings)
        if result.get("recommendation") == "warn_user":
            warnings.append(
                warning(
                    "medium",
                    "SMCL_DIFF_WARNING",
                    "The AI safety reviewer found risks in this suggested change.",
                    "diff_safety_reviewer",
                )
            )
        if result.get("recommendation") == "reject_and_request_narrower_patch":
            return {
                "action": "reject",
                "reason": result.get("rejection_reason") or "AI safety reviewer requested a narrower patch.",
                "risk_level": risk,
                "warnings": warnings,
                "review_payload": payload,
            }
        return {"action": "warn" if warnings else "allow", "reason": None, "risk_level": risk, "warnings": warnings, "review_payload": payload}

    def _should_consult_provider(self, stats, review_input, max_changed):
        total_changed = int(stats.get("total_changed_lines") or 0)
        files_changed = int(stats.get("files_changed") or 0)
        hunks = int(stats.get("hunks") or 0)
        if total_changed >= max(4, max_changed // 2):
            return True
        if files_changed > 1 or hunks > 2:
            return True
        if len(review_input.touched_headings) > 2:
            return True
        return False

    def _is_tiny_low_risk_diff(self, stats, review_input):
        return (
            int(stats.get("files_changed") or 0) == 1
            and int(stats.get("total_changed_lines") or 0) <= 4
            and int(stats.get("hunks") or 0) == 1
            and not review_input.deleted_labels_or_refs
            and not review_input.changed_imports_or_includes
            and len(review_input.touched_headings) <= 1
        )

    def _payload(self, goal, edit_mode, patch_budget, review_input):
        return {
            "proposal_goal": goal,
            "edit_mode": edit_mode,
            "patch_budget": patch_budget,
            "diff_stats": review_input.diff_stats,
            "changed_files": review_input.changed_files,
            "touched_headings": review_input.touched_headings,
            "deleted_labels_or_refs": review_input.deleted_labels_or_refs,
            "changed_imports_or_includes": review_input.changed_imports_or_includes,
            "unified_diff": review_input.unified_diff,
        }

    def _deterministic_fallback(self, stats, max_changed, warnings, review_input, *, budget_advisory: bool = False):
        if stats["total_changed_lines"] > max_changed and not budget_advisory:
            return {
                "action": "reject",
                "reason": "Diff exceeds the deterministic patch budget.",
                "risk_level": "high",
                "warnings": warnings,
                "review_payload": {
                    "diff_stats": stats,
                    "changed_files": review_input.changed_files,
                    "touched_headings": review_input.touched_headings,
                    "deleted_labels_or_refs": review_input.deleted_labels_or_refs,
                    "changed_imports_or_includes": review_input.changed_imports_or_includes,
                    "unified_diff": review_input.unified_diff,
                },
            }
        if warnings:
            return {"action": "warn", "reason": None, "risk_level": "medium", "warnings": warnings, "review_payload": {}}
        return {"action": "allow", "reason": None, "risk_level": "low", "warnings": [], "review_payload": {}}
