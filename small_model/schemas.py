VALID_EDIT_MODES = frozenset({
    "micro_edit", "paragraph_edit", "section_edit", "new_section", "compile_fix", "review_only",
})

VALID_READ_STRATEGIES = frozenset({
    "file_map_only", "summary_only", "range_only", "target_file_if_under_cap",
})

VALID_ALLOWED_OPS = frozenset({
    "find_project_files", "file_line_count", "grep_file", "read_file_lines",
    "replace_exact", "patch_file_lines", "insert_after_anchor", "insert_before_anchor",
    "replace_between_anchors", "append_to_file", "propose_document_change",
    # legacy internal names kept for backward compat
    "replace_in_project_file", "update_project_section",
})

VALID_FORBIDDEN_OPS = frozenset({
    "read_project_file", "update_project_file", "update_project_section",
    "create_new_file", "delete_file", "rename_file",
    "rewrite_section", "full_file_overwrite", "arbitrary_shell",
})

VALID_READ_PLAN_TOOLS = frozenset({
    "find_project_files", "file_line_count", "grep_file", "read_file_lines",
})

VALID_SCOPE_CONFIDENCE = frozenset({"low", "medium", "high"})

MAX_CANDIDATE_FILES = 5
MAX_READ_PLAN_STEPS = 8
MAX_READ_LINES_CAP = 200

PRE_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "task_brief": {"type": "string"},
        "relevant_files": {"type": "array", "items": {"type": "string"}},
        "relevant_section_ids": {"type": "array", "items": {"type": "string"}},
        "relevant_summaries": {"type": "array", "items": {"type": "string"}},
        "do_not_touch_files": {"type": "array", "items": {"type": "string"}},
        "do_not_touch_section_ids": {"type": "array", "items": {"type": "string"}},
        "recommended_read_strategy": {"type": "string"},
        "max_read_lines": {"type": "integer"},
        "edit_mode": {"type": "string"},
        "allowed_ops": {"type": "array", "items": {"type": "string"}},
        "forbidden_ops": {"type": "array", "items": {"type": "string"}},
        "max_files": {"type": "integer"},
        "max_changed_lines": {"type": "integer"},
        "read_strategy": {"type": "string"},
        "compile_required": {"type": "boolean"},
        "requires_user_clarification": {"type": "boolean"},
        "clarification_reason": {"type": "string", "nullable": True},
        "scope_confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "scope_confidence_reason": {"type": "string", "nullable": True},
        "candidate_files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "reason": {"type": "string"},
                },
            },
        },
        "read_plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "target_file": {"type": "string", "nullable": True},
                    "pattern": {"type": "string", "nullable": True},
                    "context_lines": {"type": "integer", "nullable": True},
                    "max_lines": {"type": "integer", "nullable": True},
                    "reason": {"type": "string"},
                },
            },
        },
        "forbidden_reads": {"type": "array", "items": {"type": "string"}},
    },
}

CONTEXT_COMPRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "task_brief": {"type": "string"},
        "relevant_files": {"type": "array", "items": {"type": "string"}},
        "relevant_section_ids": {"type": "array", "items": {"type": "string"}},
        "relevant_summaries": {"type": "array", "items": {"type": "string"}},
        "do_not_touch_files": {"type": "array", "items": {"type": "string"}},
        "do_not_touch_section_ids": {"type": "array", "items": {"type": "string"}},
        "recommended_read_strategy": {"type": "string"},
        "max_read_lines": {"type": "integer"},
    },
}

EDIT_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "edit_mode": {"type": "string"},
        "allowed_ops": {"type": "array", "items": {"type": "string"}},
        "forbidden_ops": {"type": "array", "items": {"type": "string"}},
        "max_files": {"type": "integer"},
        "max_changed_lines": {"type": "integer"},
        "read_strategy": {"type": "string"},
        "compile_required": {"type": "boolean"},
        "requires_user_clarification": {"type": "boolean"},
        "clarification_reason": {"type": "string", "nullable": True},
        "scope_confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "scope_confidence_reason": {"type": "string", "nullable": True},
    },
}

DIFF_SAFETY_SCHEMA = {
    "type": "object",
    "properties": {
        "risk_level": {"type": "string"},
        "overedit_detected": {"type": "boolean"},
        "unrelated_changes_detected": {"type": "boolean"},
        "suspicious_deletions": {"type": "array", "items": {"type": "string"}},
        "deleted_labels_or_refs": {"type": "array", "items": {"type": "string"}},
        "changed_imports_or_includes": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "string"},
        "rejection_reason": {"type": "string", "nullable": True},
    },
}

COMPILE_LOG_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "error_category": {"type": "string"},
        "error_origin": {"type": "string"},
        "likely_file": {"type": "string", "nullable": True},
        "likely_line": {"type": "integer", "nullable": True},
        "likely_cause": {"type": "string"},
        "safe_fix_strategy": {"type": "string"},
        "safe_to_retry": {"type": "boolean"},
        "retry_scope": {"type": "string"},
    },
}

NAV_FILE_CARD_ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "state": {"type": "string", "enum": ["real", "demo", "placeholder", "unknown"]},
        "state_confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary_confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "role_refinement": {"type": "string", "nullable": True},
        "role_confidence": {"type": "string", "enum": ["low", "medium", "high"], "nullable": True},
        "edit_triggers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string"},
                    "weight": {"type": "number"},
                },
            },
        },
        "semantic_labels": {"type": "array", "items": {"type": "string"}},
        "primary_terms": {"type": "array", "items": {"type": "string"}},
    },
}

NAV_REGION_CARD_ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "state": {"type": "string", "enum": ["real", "demo", "placeholder", "unknown"]},
        "state_confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary_confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "edit_triggers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string"},
                    "weight": {"type": "number"},
                },
            },
        },
        "semantic_labels": {"type": "array", "items": {"type": "string"}},
        "primary_terms": {"type": "array", "items": {"type": "string"}},
    },
}

NAV_RERANK_TARGETS_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "reason": {"type": "string"},
                },
            },
        },
        "scope_confidence": {"type": "string", "enum": ["low", "medium", "high"], "nullable": True},
    },
}

SEARCH_RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "match_kind": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

NAV_REPAIR_GUIDANCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "error_kind": {
            "type": "string",
            "enum": [
                "unknown_op",
                "include_required",
                "graph_error",
                "compile_error",
                "out_of_bounds",
                "stale_token",
                "malformed_include_file",
                "use_proposal_workflow",
                "other",
            ],
        },
        "diagnosis": {
            "type": "string",
            "maxLength": 240,
        },
        "fix_hint": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "rewrite_op": {
                    "type": ["object", "null"],
                    "additionalProperties": True,
                },
                "add_op": {
                    "type": ["object", "null"],
                    "additionalProperties": True,
                },
                "additional_read_targets": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
            },
            "required": ["rewrite_op", "add_op", "additional_read_targets"],
        },
    },
    "required": ["error_kind", "diagnosis", "fix_hint"],
}

CIRCUIT_BREAKER_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string"},
        "reason": {"type": "string"},
        "suggested_scope_reduction": {"type": "string", "nullable": True},
    },
}
