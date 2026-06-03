from __future__ import annotations

from typing import Any

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.services.diff_utils import build_diff_review_input, warning
from small_model.task_types import FEATURE_DIFF_SAFETY_REVIEWER, TASK_DIFF_SAFETY_REVIEW


class DiffSafetyReviewService(SmallModelCallMixin):
    feature_key = FEATURE_DIFF_SAFETY_REVIEWER
    task_type = TASK_DIFF_SAFETY_REVIEW

    @staticmethod
    def _deterministic_review_payload(review_input, stats) -> dict[str, Any]:
        return {
            "diff_stats": stats,
            "changed_files": review_input.changed_files,
            "touched_headings": review_input.touched_headings,
            "deleted_headings": review_input.deleted_headings,
            "deleted_text_samples": review_input.deleted_text_samples,
            "deleted_labels_or_refs": review_input.deleted_labels_or_refs,
            "changed_imports_or_includes": review_input.changed_imports_or_includes,
            "unified_diff": review_input.unified_diff,
        }

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
        max_files = int(patch_budget.get("max_files") or 1)
        max_hunks = int(patch_budget.get("max_hunks") or 5)
        stats = review_input.diff_stats
        files_changed = int(stats.get("files_changed") or 0)
        hunks = int(stats.get("hunks") or 0)
        total_changed = int(stats.get("total_changed_lines") or 0)

        # When the small model itself reported low/medium confidence in the
        # budget it picked, treat the budget as advisory rather than a hard
        # cap. The big model still sees an SMCL_BUDGET_ADVISORY warning telling
        # it to verify the diff is actually legitimate for the request.
        budget_advisory = scope_confidence in {"low", "medium"}

        # A single-hunk, single-file diff that exceeds line count is likely a
        # block wrap (e.g. adding font-size around a table) — treat the line
        # budget as advisory even if scope_confidence was 'high', since the
        # number of changed lines is an artefact of the block size, not scatter.
        single_block_expansion = (hunks == 1 and files_changed == 1 and total_changed > max_changed)
        effective_advisory = budget_advisory or single_block_expansion

        deterministic_warnings: list[dict[str, str]] = []
        if total_changed > max_changed:
            deterministic_warnings.append(
                warning(
                    "medium",
                    "OVEREDIT_RISK",
                    "Diff is larger than expected for this edit scope.",
                    "deterministic_diff_stats",
                )
            )
            if effective_advisory:
                deterministic_warnings.append(
                    warning(
                        "medium",
                        "SMCL_BUDGET_ADVISORY",
                        (
                            f"Patch budget ({max_changed} lines) was advisory: "
                            + (
                                "single-hunk block expansion detected (likely a wrap/unwrap)."
                                if single_block_expansion and not budget_advisory
                                else f"small-model scope confidence was '{scope_confidence}'"
                                + (f" ({scope_confidence_reason})" if scope_confidence_reason else "")
                                + "."
                            )
                            + " Verify that every change is required by the user's request before applying."
                        ),
                        "scope_confidence",
                    )
                )
        if files_changed > max_files:
            deterministic_warnings.append(
                warning(
                    "medium",
                    "TOO_MANY_FILES",
                    f"Diff touches {files_changed} file(s) but budget allows {max_files}.",
                    "deterministic_diff_stats",
                )
            )
        if hunks > max_hunks:
            deterministic_warnings.append(
                warning(
                    "medium",
                    "TOO_MANY_HUNKS",
                    f"Diff has {hunks} separate hunk(s) but budget allows {max_hunks} — changes may be too scattered.",
                    "deterministic_diff_stats",
                )
            )
        if review_input.deleted_headings:
            deterministic_warnings.append(
                warning(
                    "high",
                    "DELETED_HEADING",
                    (
                        "The diff deletes document heading(s): "
                        f"{', '.join(review_input.deleted_headings[:3])}. Verify this was explicitly requested."
                    ),
                    "deterministic_diff_stats",
                )
            )
        if review_input.deleted_text_samples and stats.get("lines_removed", 0) > stats.get("lines_added", 0) + 2:
            deterministic_warnings.append(
                warning(
                    "medium",
                    "SUSPICIOUS_TEXT_DELETION",
                    "The diff removes more prose than it adds; verify no unrelated paragraph or content was accidentally deleted.",
                    "deterministic_diff_stats",
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

        if (
            (files_changed > max_files) or (hunks > max_hunks and total_changed > max_changed) or (total_changed > max_changed)
        ) and not effective_advisory:
            deterministic_warnings.append(
                warning(
                    "medium",
                    "DETERMINISTIC_BUDGET_MISMATCH",
                    (
                        "The diff exceeded the declared patch budget. Treat the scope limits as advisory and "
                        "verify that each touched file and hunk is necessary for the user's request."
                    ),
                    "deterministic_diff_stats",
                )
            )
        if deterministic_warnings and not self._should_consult_provider(stats, review_input, max_changed):
            return {
                "action": "warn",
                "reason": None,
                "risk_level": "medium",
                "warnings": deterministic_warnings,
                "review_payload": self._deterministic_review_payload(review_input, stats),
            }
        if not deterministic_warnings and self._is_tiny_low_risk_diff(stats, review_input):
            return {"action": "allow", "reason": None, "risk_level": "low", "warnings": [], "review_payload": {}}

        if (
            stats["diff_char_length"] > 12288
            and stats["total_changed_lines"] > max_changed * 2
            and not budget_advisory
        ):
            deterministic_warnings.append(
                warning(
                    "medium",
                    "DIFF_TOO_LARGE_FOR_DIRECT_REVIEW",
                    "The diff is very large for the declared scope; inspect for unrelated changes before accepting.",
                    "deterministic_diff_stats",
                )
            )

        enabled, _, _ = self.is_enabled(user, project)
        if not enabled:
            return self._deterministic_fallback(
                stats, max_changed, deterministic_warnings, review_input,
                budget_advisory=effective_advisory, patch_budget=patch_budget,
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
                "Pay special attention to deleted headings and prose: if a heading or meaningful paragraph disappears "
                "without being clearly requested by the proposal goal, warn the user even when the document still compiles. "
                "Patch-budget mismatches are advisory signals, not standalone rejection grounds. "
                "If scope_confidence is 'low' or 'medium', the patch budget is advisory — judge whether "
                "the diff is legitimate for the user's request rather than rejecting on size alone. "
                "Reject only for substantive problems such as unrelated edits, suspicious deletions, or semantic drift."
            ),
            input_payload=payload,
            response_schema=schemas.DIFF_SAFETY_SCHEMA,
        )
        if not response.success or not response.parsed_json:
            return self._deterministic_fallback(
                stats, max_changed, deterministic_warnings, review_input,
                budget_advisory=effective_advisory, patch_budget=patch_budget,
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
        if result.get("suspicious_deletions"):
            warnings.append(
                warning(
                    "high",
                    "SMCL_SUSPICIOUS_DELETION",
                    "The AI safety reviewer found deletion(s) that may be unrelated to the requested change.",
                    "diff_safety_reviewer",
                )
            )
        if result.get("recommendation") == "reject_and_request_narrower_patch":
            if not self._provider_reject_has_substantive_basis(result):
                warnings.append(
                    warning(
                        "medium",
                        "SMCL_REJECT_DOWNGRADED",
                        "AI safety reviewer requested a narrower patch without identifying a substantive semantic risk; downgraded to warning.",
                        "diff_safety_reviewer",
                    )
                )
                return {"action": "warn", "reason": None, "risk_level": risk, "warnings": warnings, "review_payload": payload}
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
        if review_input.deleted_headings or review_input.deleted_text_samples:
            return True
        return False

    def _is_tiny_low_risk_diff(self, stats, review_input):
        return (
            int(stats.get("files_changed") or 0) == 1
            and int(stats.get("total_changed_lines") or 0) <= 4
            and int(stats.get("hunks") or 0) == 1
            and not review_input.deleted_labels_or_refs
            and not review_input.changed_imports_or_includes
            and not review_input.deleted_headings
            and not review_input.deleted_text_samples
            and len(review_input.touched_headings) <= 1
        )

    def _provider_reject_has_substantive_basis(self, result: dict[str, Any]) -> bool:
        if bool(result.get("overedit_detected")) or bool(result.get("unrelated_changes_detected")):
            return True
        if result.get("suspicious_deletions"):
            return True
        if result.get("deleted_labels_or_refs"):
            return True
        return False

    def _payload(self, goal, edit_mode, patch_budget, review_input):
        return {
            "proposal_goal": goal,
            "edit_mode": edit_mode,
            "patch_budget": patch_budget,
            "diff_stats": review_input.diff_stats,
            "changed_files": review_input.changed_files,
            "touched_headings": review_input.touched_headings,
            "deleted_headings": review_input.deleted_headings,
            "deleted_text_samples": review_input.deleted_text_samples,
            "deleted_labels_or_refs": review_input.deleted_labels_or_refs,
            "changed_imports_or_includes": review_input.changed_imports_or_includes,
            "unified_diff": review_input.unified_diff,
        }

    def _deterministic_fallback(self, stats, max_changed, warnings, review_input, *, budget_advisory: bool = False, patch_budget: dict | None = None):
        max_files = int((patch_budget or {}).get("max_files") or 1)
        max_hunks = int((patch_budget or {}).get("max_hunks") or 5)
        files_changed = int(stats.get("files_changed") or 0)
        hunks = int(stats.get("hunks") or 0)
        total_changed = int(stats.get("total_changed_lines") or 0)
        single_block = hunks == 1 and files_changed == 1
        effective_advisory = budget_advisory or (single_block and total_changed > max_changed)

        if (
            (files_changed > max_files) or (hunks > max_hunks and total_changed > max_changed) or (total_changed > max_changed)
        ) and not effective_advisory:
            warnings = list(warnings) + [
                warning(
                    "medium",
                    "DETERMINISTIC_BUDGET_MISMATCH",
                    (
                        "The diff exceeded the declared patch budget. Treat the scope limits as advisory and "
                        "verify that each touched file and hunk is necessary for the user's request."
                    ),
                    "deterministic_diff_stats",
                )
            ]
        if warnings:
            return {
                "action": "warn",
                "reason": None,
                "risk_level": "medium",
                "warnings": warnings,
                "review_payload": self._deterministic_review_payload(review_input, stats),
            }
        return {"action": "allow", "reason": None, "risk_level": "low", "warnings": [], "review_payload": {}}
