from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from SmartTeX.markup import MarkupType
from projects.models import Project
from projects.services import main_source_filename, project_dir


SOURCE_EXTENSIONS = {".tex", ".typ"}


@dataclass(frozen=True)
class GraphIssue:
    type: str
    file: str
    message: str
    suggestion: str = ""
    imported_from: str = ""

    def as_dict(self) -> dict[str, str]:
        payload = {
            "type": self.type,
            "file": self.file,
            "message": self.message,
        }
        if self.suggestion:
            payload["suggestion"] = self.suggestion
        if self.imported_from:
            payload["imported_from"] = self.imported_from
        return payload


@dataclass
class DocumentGraph:
    main_file: str
    compile_root: str
    reachable_files: set[str] = field(default_factory=set)
    missing_files: list[GraphIssue] = field(default_factory=list)
    orphan_source_files: list[str] = field(default_factory=list)
    unresolved_dynamic_imports: list[dict[str, str]] = field(default_factory=list)
    graph_complete: bool = True

    @property
    def errors(self) -> list[GraphIssue]:
        orphan_errors = [
            GraphIssue(
                type="orphan_source_file",
                file=f,
                message=f"File exists but is not reachable from {self.main_file}",
                suggestion=(
                    f"Include {f} from {self.main_file}, or patch an already-reachable source file instead."
                ),
            )
            for f in self.orphan_source_files
        ]
        return [*self.missing_files, *orphan_errors]

    def as_dict(self) -> dict[str, Any]:
        return {
            "main_file": self.main_file,
            "compile_root": self.compile_root,
            "reachable_files": sorted(self.reachable_files),
            "missing_files": [issue.as_dict() for issue in self.missing_files],
            "orphan_source_files": sorted(self.orphan_source_files),
            "unresolved_dynamic_imports": self.unresolved_dynamic_imports,
            "graph_complete": self.graph_complete,
            "errors": [issue.as_dict() for issue in self.errors],
            "writing_workflow_guidance": (
                "Use this graph to confirm which source files are compiled. New .tex or .typ files must be "
                "included from the main document or from another reachable source file."
            ),
        }


def _safe_rel(path: str) -> str:
    return str(Path(path.replace("\\", "/")).as_posix()).lstrip("/")


def _source_files(root: Path, suffix: str) -> set[str]:
    files: set[str] = set()
    for path in root.rglob(f"*{suffix}"):
        if ".git" in path.parts or ".smarttex" in path.parts:
            continue
        files.add(path.relative_to(root).as_posix())
    return files


def _resolve_ref(current_file: str, ref: str, markup_type: str) -> str:
    ref = ref.strip().replace("\\", "/").lstrip("/")
    base = Path(current_file).parent
    rel = (base / ref).as_posix() if str(base) != "." else ref
    if markup_type == MarkupType.LATEX and Path(rel).suffix == "":
        rel = f"{rel}.tex"
    return _safe_rel(rel)


def _typst_refs(content: str) -> tuple[list[str], list[str]]:
    literal: list[str] = []
    dynamic: list[str] = []
    for match in re.finditer(r"#(?:import|include)\s+([^:\n]+)", content):
        expr = match.group(1).strip()
        str_match = re.match(r'"([^"]+)"', expr)
        if str_match:
            literal.append(str_match.group(1))
        else:
            dynamic.append(expr[:120])
    return literal, dynamic


def _latex_refs(content: str) -> tuple[list[str], list[str]]:
    literal: list[str] = []
    dynamic: list[str] = []
    patterns = [
        r"\\(?:input|include)\{([^{}]+)\}",
        r"\\InputIfFileExists\{([^{}]+)\}",
        r"\\bibliography\{([^{}]+)\}",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            value = match.group(1).strip()
            if "\\" in value:
                dynamic.append(value[:120])
            else:
                literal.extend(v.strip() for v in value.split(",") if v.strip())
    for match in re.finditer(r"\\usepackage(?:\[[^\]]*\])?\{([^{}]+)\}", content):
        for value in match.group(1).split(","):
            value = value.strip()
            if value and "/" in value:
                literal.append(value if value.endswith(".sty") else f"{value}.sty")
    return literal, dynamic


def inspect_document_graph(project: Project, *, root: Path | None = None) -> DocumentGraph:
    compile_root = root or project_dir(project)
    main_file = main_source_filename(project)
    markup_type = project.markup_type
    source_suffix = ".typ" if markup_type == MarkupType.TYPST else ".tex"
    graph = DocumentGraph(main_file=main_file, compile_root=".")

    stack = [main_file]
    visited: set[str] = set()
    while stack:
        rel = _safe_rel(stack.pop())
        if rel in visited:
            continue
        visited.add(rel)
        path = compile_root / rel
        if not path.exists():
            graph.missing_files.append(
                GraphIssue(
                    type="missing_file",
                    file=rel,
                    imported_from="",
                    message=f"{rel} is referenced but does not exist.",
                    suggestion="Create the file or correct the include/import path.",
                )
            )
            continue
        graph.reachable_files.add(rel)
        if path.suffix.lower() != source_suffix:
            continue

        content = path.read_text(encoding="utf-8", errors="ignore")
        refs, dynamic_refs = _typst_refs(content) if markup_type == MarkupType.TYPST else _latex_refs(content)
        for expr in dynamic_refs:
            graph.graph_complete = False
            graph.unresolved_dynamic_imports.append({"file": rel, "expression": expr})
        for ref in refs:
            resolved = _resolve_ref(rel, ref, markup_type)
            target = compile_root / resolved
            if not target.exists():
                graph.missing_files.append(
                    GraphIssue(
                        type="missing_file",
                        file=resolved,
                        imported_from=rel,
                        message=f"{resolved} is referenced from {rel} but does not exist.",
                        suggestion="Create the file or correct the include/import path.",
                    )
                )
                continue
            if target.suffix.lower() == source_suffix:
                stack.append(resolved)
            else:
                graph.reachable_files.add(resolved)

    all_sources = _source_files(compile_root, source_suffix)
    graph.orphan_source_files = sorted(all_sources - graph.reachable_files)
    return graph


def introduced_graph_errors(before: DocumentGraph, after: DocumentGraph) -> list[dict[str, str]]:
    before_keys = {(issue.type, issue.file, issue.imported_from) for issue in before.errors}
    introduced: list[dict[str, str]] = []
    for issue in after.errors:
        key = (issue.type, issue.file, issue.imported_from)
        if key in before_keys:
            continue
        payload = issue.as_dict()
        payload["scope"] = "introduced_by_proposal"
        introduced.append(payload)
    return introduced
