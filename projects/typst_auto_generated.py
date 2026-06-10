from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_AUTO_BLOCK_RE = re.compile(
    r"^// smarttex:auto-begin\n.*?^// smarttex:auto-end\n?",
    re.MULTILINE | re.DOTALL,
)


def _iter_project_typ_files(workdir: Path) -> list[Path]:
    files: list[Path] = []
    for path in workdir.rglob("*.typ"):
        if ".git" in path.parts or ".smarttex" in path.parts:
            continue
        files.append(path)
    return sorted(files)


def _inject_block_into_file(path: Path, block: str) -> None:
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    clean = _AUTO_BLOCK_RE.sub("", content)
    path.write_text(block + clean, encoding="utf-8")


def _remove_block_from_file(path: Path) -> None:
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    new_content = _AUTO_BLOCK_RE.sub("", content)
    if new_content != content:
        path.write_text(new_content, encoding="utf-8")


def reachable_typst_sources(project: Any, workdir: Path) -> list[Path]:
    from longdoc.document_graph import inspect_document_graph

    graph = inspect_document_graph(project, root=workdir)
    reachable: list[Path] = []
    for rel in sorted(graph.reachable_files):
        path = workdir / rel
        if path.suffix.lower() != ".typ" or not path.exists():
            continue
        reachable.append(path)
    return reachable


def build_auto_import_block(import_line: str) -> str:
    return f"// smarttex:auto-begin\n{import_line}\n// smarttex:auto-end\n"


def inject_auto_import_into_reachable_typst(project: Any, workdir: Path, import_line: str) -> None:
    block = build_auto_import_block(import_line)
    for path in reachable_typst_sources(project, workdir):
        _inject_block_into_file(path, block)


def sync_auto_import_for_typst_symbol(project: Any, workdir: Path, import_line: str, symbol: str) -> None:
    block = build_auto_import_block(import_line)
    reachable = set(reachable_typst_sources(project, workdir))
    symbol_call_re = re.compile(rf"(?:#\s*)?{re.escape(symbol)}\s*\(")

    for path in _iter_project_typ_files(workdir):
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        clean = _AUTO_BLOCK_RE.sub("", content)
        should_import = path in reachable and bool(symbol_call_re.search(clean))
        next_content = block + clean if should_import else clean
        if next_content != content:
            path.write_text(next_content, encoding="utf-8")


def remove_auto_imports_from_all_typst(workdir: Path) -> None:
    for path in _iter_project_typ_files(workdir):
        _remove_block_from_file(path)
