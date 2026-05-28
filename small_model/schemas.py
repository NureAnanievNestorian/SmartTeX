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

CIRCUIT_BREAKER_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string"},
        "reason": {"type": "string"},
        "suggested_scope_reduction": {"type": "string", "nullable": True},
    },
}
