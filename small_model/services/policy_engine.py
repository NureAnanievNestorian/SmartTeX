from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from small_model.services.circuit_breaker import CircuitBreakerService
from small_model.services.compile_log_triage import CompileLogTriageService
from small_model.services.diff_safety_reviewer import DiffSafetyReviewService
from small_model.services.diff_utils import warning
from small_model.services.edit_intent_classifier import CONSERVATIVE_PARAGRAPH, EDIT_MODE_MAX_HUNKS
from small_model.services.pre_proposal import PreProposalAnalysisService


@dataclass(slots=True)
class PolicyResult:
    action: str = "allow"
    reason: str | None = None
    smcl_used: bool = False
    fallback_used: bool = False
    warnings: list[dict[str, str]] = field(default_factory=list)
    risk_level: str = "low"
    metadata: dict[str, Any] = field(default_factory=dict)


class ProposalPolicyEngine:
    @staticmethod
    def pre_proposal_check(user, project, user_request: str) -> PolicyResult:
        combined = PreProposalAnalysisService().run(user=user, project=project, user_request=user_request)
        compressor = combined.get("context_compressor") or {}
        classifier = combined.get("edit_intent") or {}
        metadata: dict[str, Any] = {}
        if compressor:
            metadata["context_compressor"] = compressor
        if classifier:
            metadata["edit_intent"] = classifier
        elif not compressor:
            return PolicyResult(action="allow", smcl_used=False, fallback_used=True)
        if not classifier:
            metadata["edit_intent"] = dict(CONSERVATIVE_PARAGRAPH)
        if metadata["edit_intent"].get("requires_user_clarification"):
            return PolicyResult(
                action="stop",
                reason=metadata["edit_intent"].get("clarification_reason") or "Please clarify the requested edit scope.",
                smcl_used=bool(compressor or classifier),
                metadata=metadata,
            )
        sanitize_fallback = bool(combined.get("smcl_fallback_used"))
        return PolicyResult(action="allow", smcl_used=bool(compressor or classifier), fallback_used=not bool(classifier) or sanitize_fallback, metadata=metadata)

    @staticmethod
    def post_patch_check(user, project, proposal, diff: str) -> PolicyResult:
        metadata = getattr(proposal, "smcl_metadata", {}) or {}
        edit_intent = metadata.get("edit_intent") or CONSERVATIVE_PARAGRAPH
        reviewer = DiffSafetyReviewService()
        enabled, _, _ = reviewer.is_enabled(user, project)
        edit_mode = str(edit_intent.get("edit_mode") or "paragraph_edit")
        patch_budget = {
            "max_changed_lines": int(edit_intent.get("max_changed_lines") or 15),
            "max_files": int(edit_intent.get("max_files") or 1),
            "max_hunks": EDIT_MODE_MAX_HUNKS.get(edit_mode, 5),
        }
        scope_confidence = str(edit_intent.get("scope_confidence") or "high").lower()
        scope_confidence_reason = edit_intent.get("scope_confidence_reason")
        review = reviewer.review(
            user=user,
            project=project,
            proposal_goal=proposal.goal,
            diff_text=diff,
            edit_mode=edit_mode,
            patch_budget=patch_budget,
            scope_confidence=scope_confidence,
            scope_confidence_reason=str(scope_confidence_reason) if scope_confidence_reason else None,
        )
        action = review.get("action") or "allow"
        return PolicyResult(
            action=action,
            reason=review.get("reason"),
            smcl_used=enabled and bool(review.get("review_payload")),
            fallback_used=(not enabled) or (action in {"warn", "reject"} and not bool(review.get("review_payload"))),
            warnings=review.get("warnings") or [],
            risk_level=review.get("risk_level") or "low",
            metadata={
                "diff_review": review.get("review_payload") or {},
                "semantic_diff_summary": (review.get("review_payload") or {}).get("semantic_summary") or {},
            },
        )

    @staticmethod
    def post_compile_check(user, project, proposal, compile_result: dict[str, Any]) -> PolicyResult:
        compile_log = str(compile_result.get("log") or getattr(proposal.internal_session, "compile_log", "") or "")
        diagnostics = compile_result.get("diagnostics") if isinstance(compile_result.get("diagnostics"), list) else []
        changed_files = [op.get("filename") for op in (proposal.patch_ops or []) if isinstance(op, dict) and op.get("filename")]
        metadata = getattr(proposal, "smcl_metadata", {}) or {}

        triage_service = CompileLogTriageService()
        triage_enabled, _, _ = triage_service.is_enabled(user, project)
        triage = (
            triage_service.triage(
                user=user,
                project=project,
                compile_log=compile_log,
                diagnostics=diagnostics,
                changed_files=changed_files,
            )
            if triage_enabled
            else {}
        )
        breaker = {"decision": "continue", "reason": "", "deterministic": True}
        circuit_payload = {
            "proposal_id": proposal.id,
            "attempt_number": int(metadata.get("compile_attempts") or 1),
            "compile_failures": int(metadata.get("compile_failures") or 1),
            "rejected_patches": int(metadata.get("rejected_patches") or 0),
            "repeated_tool_calls": metadata.get("repeated_tool_calls") or {},
            "diff_size_history": metadata.get("diff_size_history") or [],
            "files_touched_total": len(set(changed_files)),
            "max_files": int((metadata.get("edit_intent") or CONSERVATIVE_PARAGRAPH).get("max_files") or 1),
            "compile_log_triage_result": triage,
            "smcl_unavailable_streak": int(metadata.get("smcl_unavailable_streak") or 0),
        }
        breaker_service = CircuitBreakerService()
        breaker = breaker_service.evaluate_deterministic(
            compile_failures=circuit_payload["compile_failures"],
            tool_calls=list((circuit_payload.get("repeated_tool_calls") or {}).keys()),
            files_touched_total=circuit_payload["files_touched_total"],
            max_files=circuit_payload["max_files"],
            diff_size_history=circuit_payload["diff_size_history"],
            rejected_patches=circuit_payload["rejected_patches"],
        )

        result_metadata = {
            "compile_log_triage": triage,
            "circuit_breaker": breaker,
            "compile_failures": circuit_payload["compile_failures"] + 1,
            "smcl_unavailable_streak": int(breaker.get("smcl_unavailable_streak") or 0),
        }
        warnings = []
        if triage_enabled or breaker.get("decision") != "continue":
            warnings.append(
                warning(
                    "medium",
                    "COMPILE_FAILED",
                    "The document did not compile after applying the suggested change.",
                    "compile_log_triage",
                )
            )
        if triage.get("error_origin") == "pre_existing_error":
            return PolicyResult(
                action="stop_and_ask_user",
                reason="The document appears to have pre-existing compile errors unrelated to this patch.",
                smcl_used=True,
                warnings=warnings,
                risk_level="high",
                metadata=result_metadata,
            )
        if breaker.get("decision") in {"stop_and_ask_user", "narrow_scope"}:
            result_metadata["circuit_breaker"] = breaker
            return PolicyResult(
                action=str(breaker.get("decision")),
                reason=str(breaker.get("reason") or "Compile-fix loop risk detected."),
                smcl_used=not bool(breaker.get("deterministic")),
                fallback_used=bool(breaker.get("deterministic")),
                warnings=warnings,
                risk_level="high" if breaker.get("decision") == "stop_and_ask_user" else "medium",
                metadata=result_metadata,
            )
        should_run_breaker_model = (
            bool(triage_enabled)
            and not triage.get("safe_to_retry", False)
            and triage.get("retry_scope") not in {"do_not_retry", ""}
        ) or (
            circuit_payload["compile_failures"] >= 2
            or circuit_payload["rejected_patches"] > 0
            or bool(circuit_payload["repeated_tool_calls"])
            or len(circuit_payload["diff_size_history"]) >= 2
        )
        if should_run_breaker_model:
            breaker = breaker_service.evaluate(user=user, project=project, payload=circuit_payload)
            result_metadata["circuit_breaker"] = breaker
        if breaker.get("decision") in {"stop_and_ask_user", "narrow_scope"}:
            return PolicyResult(
                action=str(breaker.get("decision")),
                reason=str(breaker.get("reason") or "Compile-fix loop risk detected."),
                smcl_used=not bool(breaker.get("deterministic")),
                fallback_used=bool(breaker.get("deterministic")),
                warnings=warnings,
                risk_level="high" if breaker.get("decision") == "stop_and_ask_user" else "medium",
                metadata=result_metadata,
            )
        return PolicyResult(
            action="allow",
            reason=None,
            smcl_used=triage_enabled or not bool(breaker.get("deterministic")),
            warnings=warnings,
            risk_level="medium" if warnings else "low",
            metadata=result_metadata,
        )
