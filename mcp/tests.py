import asyncio
import importlib.util
import os
import site
import sys
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase

_external_mcp_package = None
for _base in site.getsitepackages():
    _package_dir = f"{_base}/mcp"
    _init_file = f"{_package_dir}/__init__.py"
    if not os.path.exists(_init_file):
        continue
    _spec = importlib.util.spec_from_file_location("mcp", _init_file, submodule_search_locations=[_package_dir])
    if _spec and _spec.loader:
        _external_mcp_package = importlib.util.module_from_spec(_spec)
        sys.modules["mcp"] = _external_mcp_package
        _spec.loader.exec_module(_external_mcp_package)
        break

import mcp_http_server as server


class ControlledMCPToolTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        server.READ_BUDGET_STATE.clear()
        server.REPLACE_DRY_RUN_STATE.clear()

    def test_read_project_file_rejects_full_read_over_cap(self) -> None:
        with (
            patch.object(server, "_project_controlled_mode_enabled", return_value=True),
            patch.object(
                server,
                "_file_line_info",
                return_value={"file_name": "main.tex", "line_count": 1842, "size_bytes": 88064},
            ),
        ):
            result = server.read_project_file(project_id=1, file_name="main.tex")

        self.assertEqual(result["error"], "FILE_TOO_LARGE")
        self.assertIn("read_file_lines", result["suggestion"])
        self.assertEqual(result["line_count"], 1842)
        self.assertEqual(result["size_bytes"], 88064)

    def test_read_file_lines_enforces_per_call_limit(self) -> None:
        with patch.object(server, "MCP_MAX_READ_LINES", 5):
            result = server.read_file_lines(project_id=1, filename="main.tex", start_line=1, end_line=8)

        self.assertEqual(result["error"], "READ_LIMIT_EXCEEDED")
        self.assertEqual(result["max_read_lines"], 5)

    def test_read_file_lines_hard_budget_rejects(self) -> None:
        key = ("token-1", 1)
        server.READ_BUDGET_STATE[key] = 5
        with (
            patch.object(server, "MCP_READ_BUDGET_HARD", True),
            patch.object(server, "_current_bearer_token", return_value="token-1"),
        ):
            result = server.read_file_lines(project_id=1, filename="main.tex", start_line=1, end_line=10)

        self.assertEqual(result["error"], "READ_BUDGET_EXHAUSTED")
        self.assertEqual(result["read_budget_remaining"], 5)

    def test_read_file_lines_soft_budget_warns_and_returns_content(self) -> None:
        key = ("token-2", 1)
        server.READ_BUDGET_STATE[key] = 5
        with (
            patch.object(server, "MCP_READ_BUDGET_HARD", False),
            patch.object(server, "_current_bearer_token", return_value="token-2"),
            patch.object(
                server,
                "_read_file_lines_raw",
                return_value={
                    "file_name": "main.tex",
                    "content": "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n",
                    "start_line": 1,
                    "end_line": 10,
                    "total_lines": 20,
                },
            ),
        ):
            result = server.read_file_lines(project_id=1, filename="main.tex", start_line=1, end_line=10)

        self.assertEqual(result["filename"], "main.tex")
        self.assertEqual(result["read_budget_remaining"], 0)
        self.assertEqual(result["budget_warning"]["error"], "READ_BUDGET_EXHAUSTED")

    def test_replace_in_project_file_requires_exact_once_match(self) -> None:
        with (
            patch.object(server, "_project_controlled_mode_enabled", return_value=True),
            patch.object(server, "_project_main_file_name", return_value="main.tex"),
            patch.object(server, "read_project_file", return_value={"content": "foo\nfoo\n"}),
        ):
            result = asyncio.run(
                server.replace_in_project_file(
                    project_id=1,
                    pattern="foo",
                    replacement="bar",
                    change_summary="Replace foo",
                    ctx=None,
                    dry_run=True,
                )
            )

        self.assertEqual(result["error"], "AMBIGUOUS_MATCH")
        self.assertEqual(result["match_count"], 2)

    def test_replace_in_project_file_requires_matching_dry_run_before_apply(self) -> None:
        with (
            patch.object(server, "_project_controlled_mode_enabled", return_value=True),
            patch.object(server, "_project_main_file_name", return_value="main.tex"),
            patch.object(server, "read_project_file", return_value={"content": "alpha beta\n"}),
        ):
            result = asyncio.run(
                server.replace_in_project_file(
                    project_id=1,
                    pattern="beta",
                    replacement="gamma",
                    change_summary="Replace beta",
                    ctx=None,
                    dry_run=False,
                )
            )

        self.assertEqual(result["error"], "DRY_RUN_REQUIRED")

    def test_patch_file_lines_rejects_anchor_mismatch(self) -> None:
        with (
            patch.object(
                server,
                "_file_line_info",
                return_value={"file_name": "main.tex", "line_count": 20, "total_chars": 200, "is_text": True},
            ),
            patch.object(server, "_read_file_lines_raw", return_value={"content": "different anchor\n"}),
        ):
            result = asyncio.run(
                server.patch_file_lines(
                    project_id=1,
                    filename="main.tex",
                    start_line=10,
                    end_line=12,
                    new_content="new text\n",
                    change_summary="Patch block",
                    ctx=None,
                    anchor_before="expected anchor",
                )
            )

        self.assertEqual(result["error"], "ANCHOR_MISMATCH")
        self.assertEqual(result["searched_line"], 9)

    def test_patch_file_lines_applies_with_matching_anchors(self) -> None:
        def fake_read_lines(_project_id, _filename, start_line, end_line):
            mapping = {
                (9, 9): {"content": "prev line\n"},
                (13, 13): {"content": "next line\n"},
                (10, 12): {"content": "old line\nsecond line\nthird line\n"},
            }
            return mapping[(start_line, end_line)]

        def fake_call(method, path, data=None):
            if path.endswith("/write-window/"):
                return {"detail": "saved", "file_name": "main.tex"}
            if "/versions/" in path:
                return {"versions": [{"number": 7}]}
            raise AssertionError(f"unexpected call: {method} {path}")

        with (
            patch.object(
                server,
                "_file_line_info",
                return_value={"file_name": "main.tex", "line_count": 20, "total_chars": 200, "is_text": True},
            ),
            patch.object(server, "_read_file_lines_raw", side_effect=fake_read_lines),
            patch.object(server, "_call", side_effect=fake_call),
            patch.object(server, "_notify_project_write_updates", new=AsyncMock()),
        ):
            result = asyncio.run(
                server.patch_file_lines(
                    project_id=1,
                    filename="main.tex",
                    start_line=10,
                    end_line=12,
                    new_content="new line\nsecond line\n",
                    change_summary="Patch block",
                    ctx=None,
                    anchor_before="prev line",
                    anchor_after="next line",
                )
            )

        self.assertEqual(result["version_number"], 7)
        self.assertIn("new line", result["diff_text"])
        self.assertGreaterEqual(result["lines_added"], 1)
        self.assertGreaterEqual(result["lines_removed"], 1)

    def test_append_to_file_appends_after_named_section(self) -> None:
        def fake_call(method, path, data=None):
            if path.endswith("/sections/"):
                return {"sections": [{"index": 3, "title": "Conclusion", "file_name": "main.tex"}]}
            if path.endswith("/sections/3/"):
                return {"end_char": 120, "end_line": 40}
            if path.endswith("/write-window/"):
                self.assertEqual(data["start_char"], 120)
                self.assertEqual(data["end_char"], 120)
                return {"detail": "saved", "file_name": "main.tex"}
            if "/versions/" in path:
                return {"versions": [{"number": 8}]}
            raise AssertionError(f"unexpected call: {method} {path}")

        with (
            patch.object(
                server,
                "_file_line_info",
                return_value={"file_name": "main.tex", "line_count": 50, "total_chars": 220, "is_text": True},
            ),
            patch.object(server, "_call", side_effect=fake_call),
            patch.object(server, "_notify_project_write_updates", new=AsyncMock()),
        ):
            result = asyncio.run(
                server.append_to_file(
                    project_id=1,
                    filename="main.tex",
                    content="\nClosing remark.\n",
                    anchor_section="Conclusion",
                    change_summary="Append conclusion note",
                    ctx=None,
                )
            )

        self.assertEqual(result["version_number"], 8)
        self.assertEqual(result["appended_at_line"], 40)

    def test_update_context_file_rejects_when_mcp_context_writes_disabled(self) -> None:
        with patch.object(
            server,
            "_project_longdoc_meta",
            return_value={"enabled": True, "context_enabled": True, "mcp_write_context": False},
        ):
            result = asyncio.run(
                server.update_context_file(
                    project_id=1,
                    filename="project-brief.md",
                    content="Updated",
                    change_summary="Update context",
                    ctx=None,
                )
            )

        self.assertEqual(result["error"], "MCP_CONTEXT_WRITES_DISABLED")

    def test_get_longdoc_overview_passes_through_payload(self) -> None:
        with patch.object(
            server,
            "_call_allow_json_errors",
            return_value={"project_id": 1, "context_file_count": 2, "task_counts": {"open": 1, "done": 0}},
        ):
            result = server.get_longdoc_overview(project_id=1)

        self.assertEqual(result["project_id"], 1)
        self.assertEqual(result["context_file_count"], 2)

    def test_append_to_note_section_requires_matching_heading(self) -> None:
        with patch.object(server, "_call_allow_json_errors", return_value={"note_sections": [{"id": 2, "heading": "Decisions", "body": "A"}]}):
            result = asyncio.run(
                server.append_to_note_section(
                    project_id=1,
                    heading="Missing",
                    text="\nB",
                    change_summary="Append note",
                    ctx=None,
                )
            )

        self.assertEqual(result["error"], "NOTE_SECTION_NOT_FOUND")

    def test_update_section_summary_passes_through_api_payload(self) -> None:
        with (
            patch.object(server, "_call_allow_json_errors", return_value={"id": 3, "section_title": "Intro", "is_stale": False}),
            patch.object(server, "_notify_longdoc_updates", new=AsyncMock()),
        ):
            result = asyncio.run(
                server.update_section_summary(
                    project_id=1,
                    section_title="Intro",
                    summary_text="Summary text",
                    section_index=1,
                    change_summary="Refresh intro summary",
                    source_file="main.tex",
                    ctx=None,
                )
            )

        self.assertEqual(result["section_title"], "Intro")
        self.assertFalse(result["is_stale"])

    def test_update_requirement_coverage_returns_structured_rejection(self) -> None:
        with patch.object(
            server,
            "_call_allow_json_errors",
            return_value={
                "error": "PROJECT_LOCKED",
                "detail": "Project is locked.",
                "suggestion": "Wait for the active AI session to finish.",
            },
        ):
            result = asyncio.run(
                server.update_requirement_coverage(
                    project_id=1,
                    requirement_id=9,
                    coverage="covered",
                    change_summary="Mark covered",
                    ctx=None,
                )
            )

        self.assertEqual(result["error"], "PROJECT_LOCKED")

    # ── read_context_file size enforcement ────────────────────────────────

    def test_read_context_file_truncates_content_over_max_bytes(self) -> None:
        big_content = "x" * (server.MCP_MAX_FULL_READ_BYTES + 100)
        with (
            patch.object(
                server,
                "_call_allow_json_errors",
                return_value={"filename": "brief.md", "content": big_content},
            ),
            patch.object(server, "_consume_read_budget", return_value=None),
        ):
            result = server.read_context_file(project_id=1, filename="brief.md")

        self.assertTrue(result.get("truncated"))
        self.assertLessEqual(len(result["content"].encode("utf-8")), server.MCP_MAX_FULL_READ_BYTES + 4)
        self.assertEqual(result["total_bytes"], len(big_content.encode("utf-8")))

    def test_read_context_file_hard_budget_blocks_when_exhausted(self) -> None:
        with (
            patch.object(
                server,
                "_call_allow_json_errors",
                return_value={"filename": "brief.md", "content": "small content"},
            ),
            patch.object(
                server,
                "_consume_read_budget",
                return_value={
                    "error": "READ_BUDGET_EXHAUSTED",
                    "message": "Budget exhausted.",
                    "suggestion": "Use list_context_files instead.",
                    "read_budget_remaining": 0,
                    "requested_lines": 1,
                },
            ),
            patch.object(server, "MCP_READ_BUDGET_HARD", True),
        ):
            result = server.read_context_file(project_id=1, filename="brief.md")

        self.assertEqual(result["error"], "READ_BUDGET_EXHAUSTED")

    # ── MCP session tool smoke tests ──────────────────────────────────────

    def test_get_active_session_returns_payload(self) -> None:
        with patch.object(
            server,
            "_call_allow_json_errors",
            return_value={"session": None},
        ):
            result = server.get_active_session(project_id=1)

        self.assertEqual(result, {"session": None})

    def test_create_ai_session_forwards_goal(self) -> None:
        with (
            patch.object(
                server,
                "_call_allow_json_errors",
                return_value={"session": {"id": 1, "goal": "Write chapter", "status": "active"}},
            ),
            patch.object(server, "_notify_longdoc_updates", new=AsyncMock()),
        ):
            result = asyncio.run(
                server.create_ai_session(project_id=1, goal="Write chapter", ctx=None)
            )

        self.assertEqual(result["session"]["goal"], "Write chapter")

    def test_get_session_diff_returns_diff(self) -> None:
        with patch.object(
            server,
            "_call_allow_json_errors",
            return_value={"diff_text": "--- a/main.tex\n+++ b/main.tex\n", "session_id": 1},
        ):
            result = server.get_session_diff(project_id=1)

        self.assertIn("diff_text", result)

    def test_finalize_ai_session_passes_summary(self) -> None:
        with (
            patch.object(
                server,
                "_call_allow_json_errors",
                return_value={"session": {"id": 1, "status": "ready_for_review"}, "batch_id": 5},
            ),
            patch.object(server, "_notify_longdoc_updates", new=AsyncMock()),
        ):
            result = asyncio.run(
                server.finalize_ai_session(project_id=1, summary="Done for review", ctx=None)
            )

        self.assertEqual(result["session"]["status"], "ready_for_review")
