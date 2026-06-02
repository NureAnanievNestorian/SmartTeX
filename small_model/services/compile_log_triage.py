from __future__ import annotations

import re

from small_model import schemas
from small_model.services.base import SmallModelCallMixin
from small_model.services.payload import PayloadSanitizer
from small_model.task_types import FEATURE_COMPILE_LOG_TRIAGE, TASK_COMPILE_LOG_TRIAGE


class CompileLogTriageService(SmallModelCallMixin):
    feature_key = FEATURE_COMPILE_LOG_TRIAGE
    task_type = TASK_COMPILE_LOG_TRIAGE

    def triage(self, *, user, project, compile_log: str, diagnostics: list | None = None, changed_files: list | None = None) -> dict:
        deterministic = self._deterministic_triage(compile_log)
        if deterministic is not None:
            return deterministic
        enabled, _, _ = self.is_enabled(user, project)
        if not enabled:
            return self._fallback(compile_log)
        payload = PayloadSanitizer.clean_payload(
            {
                "compile_log": PayloadSanitizer.trim_text(compile_log, max_chars=2000),
                "diagnostics": diagnostics or [],
                "changed_files": changed_files or [],
                "document_graph_summary": "",
            }
        )
        response = self.call_provider(
            user=user,
            project=project,
            system_instruction=(
                "Classify the compile failure, identify the most likely root cause, and decide whether retrying is safe. "
                "Prefer upstream root causes over the file where the compiler reported the symptom. "
                "Use changed_files aggressively: if the failing file is not in changed_files but a template, main, prelude, or wrapper file is, "
                "suspect structural breakage there first. "
                "If many citation/reference labels fail at once, do not assume the cited section is wrong; instead consider broken bibliography wiring, "
                "broken document wrapper/show block, removed include/import chain, or a changed main/template file that stopped attaching references to the document. "
                "Treat mass label failures as a likely upstream configuration/template error unless the changed_files directly modified the cited labels or source file. "
                "likely_file may be an upstream changed file rather than the diagnostic file when that better explains the failure. "
                "Return concise fields only; do not suggest speculative fixes that ignore changed_files."
            ),
            input_payload=payload,
            response_schema=schemas.COMPILE_LOG_TRIAGE_SCHEMA,
        )
        if response.success and response.parsed_json:
            return response.parsed_json
        return self._fallback(compile_log)

    def _deterministic_triage(self, compile_log: str) -> dict | None:
        text = compile_log or ""
        if not text.strip():
            return None
        if re.search(r"(fatal error occurred before patch|pre-existing|before applying changes)", text, re.I):
            return {
                "error_category": "pre_existing_error",
                "error_origin": "pre_existing_error",
                "likely_file": None,
                "likely_line": None,
                "likely_cause": "Compile log indicates errors existed before this patch.",
                "safe_fix_strategy": "Ask the user to resolve the baseline compile issue first.",
                "safe_to_retry": False,
                "retry_scope": "do_not_retry",
            }
        if re.search(r"(undefined control sequence|missing import|cannot find|file .* not found|undefined citation|undefined reference)", text, re.I):
            return self._fallback(text)
        return None

    def _fallback(self, compile_log: str) -> dict:
        text = compile_log or ""
        category = "missing_import" if re.search(r"(undefined control sequence|not found|missing import|cannot find)", text, re.I) else "unknown"
        return {
            "error_category": category,
            "error_origin": "patch_error",
            "likely_file": None,
            "likely_line": None,
            "likely_cause": "",
            "safe_fix_strategy": "",
            "safe_to_retry": False,
            "retry_scope": "do_not_retry",
        }
