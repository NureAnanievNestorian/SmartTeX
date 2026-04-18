from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Avoid local Django app package named `mcp` shadowing MCP SDK package.
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if p not in ("", str(PROJECT_ROOT))]

from fastmcp import Context, FastMCP  # noqa: E402
from fastmcp.server.auth import AccessToken, RemoteAuthProvider, TokenVerifier  # noqa: E402
from fastmcp.server.dependencies import get_access_token  # noqa: E402
from pydantic import AnyHttpUrl  # noqa: E402

BASE_URL = os.getenv("DJANGO_API_BASE_URL", "http://web:8000").rstrip("/")
LEGACY_TOKEN = os.getenv("MCP_API_TOKEN", "").strip()
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "streamable-http")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "9000"))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")
PUBLIC_BASE_URL = os.getenv("MCP_PUBLIC_BASE_URL", BASE_URL).rstrip("/")
MCP_SERVER_PUBLIC_URL = os.getenv("MCP_SERVER_PUBLIC_URL", "http://localhost:9000").rstrip("/")
WEB_PUBLIC_BASE_URL = os.getenv("WEB_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
AUTH_SERVER_ISSUER_URL = os.getenv("MCP_AUTH_SERVER_ISSUER_URL", WEB_PUBLIC_BASE_URL).rstrip("/")
MCP_INTROSPECTION_URL = os.getenv("MCP_INTROSPECTION_URL", f"{BASE_URL}/oauth/introspect/")
MCP_INTROSPECTION_SECRET = os.getenv("MCP_INTROSPECTION_SECRET", "").strip()
MCP_OAUTH_ENABLED = os.getenv("MCP_OAUTH_ENABLED", "True").lower() in {"1", "true", "yes"}
MCP_CORS_ORIGINS = [o.strip() for o in os.getenv("MCP_CORS_ORIGINS", "*").split(",") if o.strip()]


class DjangoIntrospectionTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        headers = {"Accept": "application/json"}
        if MCP_INTROSPECTION_SECRET:
            headers["X-Introspection-Secret"] = MCP_INTROSPECTION_SECRET
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    MCP_INTROSPECTION_URL,
                    data={"token": token},
                    headers=headers,
                )
        except Exception:
            return None
        if response.status_code != 200:
            return None
        payload = response.json()
        if not payload.get("active"):
            return None
        scope_raw = payload.get("scope", "")
        scopes = [s for s in str(scope_raw).split(" ") if s]
        exp = payload.get("exp")
        expires_at = int(exp) if isinstance(exp, int | float) else None
        return AccessToken(
            token=token,
            client_id=str(payload.get("client_id", "")),
            scopes=scopes,
            expires_at=expires_at,
            claims={
                "sub": str(payload.get("sub", "")),
                "username": str(payload.get("username", "")),
            },
        )


def _current_bearer_token() -> str | None:
    try:
        token = get_access_token()
    except Exception:
        return None
    if not token:
        return None
    raw = getattr(token, "token", "")
    return str(raw).strip() or None


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "X-Change-Source": "mcp"}
    bearer = _current_bearer_token()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    elif LEGACY_TOKEN:
        headers["Authorization"] = f"Token {LEGACY_TOKEN}"
    return headers


def _call(method: str, path: str, data: dict[str, Any] | None = None) -> Any:
    url = f"{BASE_URL}{path}"
    with httpx.Client(timeout=60, headers=_headers()) as client:
        response = client.request(method, url, json=data)
    response.raise_for_status()

    ctype = response.headers.get("content-type", "")
    if "application/json" in ctype:
        return response.json()
    return {"status_code": response.status_code, "text": response.text}


def _require_summary(change_summary: str) -> str:
    summary = (change_summary or "").strip()
    if not summary:
        raise ValueError("change_summary is required and must be non-empty")
    return summary


def _absolute_url(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{PUBLIC_BASE_URL}{path if path.startswith('/') else '/' + path}"


def _compact_latex_log(log_text: str, max_chars: int = 4000) -> tuple[str, bool]:
    text = str(log_text or "")
    if not text:
        return "", False

    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "", False

    markers = (
        "!",
        "error",
        "warning",
        "not found",
        "undefined",
        "overfull",
        "underfull",
        "timed out",
    )
    important = [ln for ln in lines if any(m in ln.lower() for m in markers)]
    tail = lines[-50:]

    picked: list[str] = []
    seen: set[str] = set()
    for ln in [*important[:80], *tail]:
        if ln not in seen:
            picked.append(ln)
            seen.add(ln)

    compact = "\n".join(picked if picked else tail)
    if len(compact) <= max_chars:
        return compact, compact != text

    truncated = compact[:max_chars].rstrip() + "\n...[log truncated]"
    return truncated, True


def _enrich_compile_payload(
        project_id: int,
        payload: dict[str, Any],
        compact_log: bool = True,
        max_log_chars: int = 4000,
) -> dict[str, Any]:
    pdf_url = payload.get("pdf_url")
    enriched = {
        **payload,
        "pdf_url_external": _absolute_url(pdf_url),
        "pdf_download_url": _absolute_url(f"/api/projects/{project_id}/pdf/"),
    }
    if compact_log and "log" in enriched:
        raw_log = str(enriched.get("log") or "")
        compact, was_truncated = _compact_latex_log(raw_log, max_chars=max_log_chars)
        enriched["log"] = compact
        enriched["log_compacted"] = True
        enriched["log_truncated"] = was_truncated
        enriched["log_original_length"] = len(raw_log)
    return enriched


def _with_optional_compile(
        project_id: int,
        update_result: Any,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    if not compileAlso:
        if isinstance(update_result, dict):
            return update_result
        return {"result": update_result}

    compile_payload = _call("POST", f"/api/projects/{project_id}/compile/")
    compile_result = _enrich_compile_payload(
        project_id,
        compile_payload,
        compact_log=bool(compileLogCompact),
        max_log_chars=max(500, min(int(compileMaxLogChars), 20000)),
    )

    if isinstance(update_result, dict):
        return {**update_result, "compile": compile_result}
    return {"result": update_result, "compile": compile_result}


def _compact_sections_payload(payload: dict[str, Any], compact: bool = True) -> dict[str, Any]:
    if not compact:
        return payload
    sections = payload.get("sections")
    if not isinstance(sections, list):
        return payload

    compact_sections: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        compact_sections.append(
            {
                "index": section.get("index"),
                "level": section.get("level"),
                "command": section.get("command"),
                "title": section.get("title"),
                "start_line": section.get("start_line"),
                "end_line": section.get("end_line"),
                "line_count": section.get("line_count"),
            }
        )

    return {
        **payload,
        "sections": compact_sections,
        "sections_compacted": True,
    }


def _compact_single_section_payload(
        payload: dict[str, Any],
        *,
        include_content: bool = False,
        content_preview_chars: int = 800,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    content = str(payload.get("content") or "")
    compact: dict[str, Any] = {
        "index": payload.get("index"),
        "command": payload.get("command"),
        "level": payload.get("level"),
        "title": payload.get("title"),
        "start_line": payload.get("start_line"),
        "end_line": payload.get("end_line"),
        "start_char": payload.get("start_char"),
        "end_char": payload.get("end_char"),
        "content_length": len(content),
    }
    if include_content:
        limit = max(100, min(int(content_preview_chars), 20000))
        compact["content"] = content[:limit]
        compact["content_truncated"] = len(content) > limit
    return compact


def _compact_search_payload(
        payload: dict[str, Any],
        *,
        include_line_text: bool = False,
        max_matches: int = 50,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    matches = payload.get("matches")
    if not isinstance(matches, list):
        return payload

    safe_max = max(1, min(int(max_matches), 500))
    sliced = matches[:safe_max]

    compact_matches: list[dict[str, Any]] = []
    for m in sliced:
        if not isinstance(m, dict):
            continue
        item = {
            "file_name": m.get("file_name"),
            "line": m.get("line"),
            "column": m.get("column"),
            "match_text": m.get("match_text"),
        }
        if include_line_text:
            item["line_text"] = m.get("line_text")
        compact_matches.append(item)

    original_count = len(matches)
    return {
        **payload,
        "matches": compact_matches,
        "matches_compacted": True,
        "matches_original_count": original_count,
        "matches_returned": len(compact_matches),
        "truncated": bool(payload.get("truncated")) or original_count > safe_max,
    }


def _resource_uri(project_id: int, resource_name: str) -> str:
    return f"smarttex://projects/{int(project_id)}/{resource_name}"


async def _notify_resource_updated(ctx: Context | None, uri: str) -> None:
    if ctx is None:
        return
    try:
        await ctx.session.send_resource_updated(uri)
    except Exception:
        # Resource subscriptions are best-effort; tool operations must still succeed.
        return


async def _notify_project_write_updates(
        ctx: Context | None,
        project_id: int,
        *,
        include_compile_log: bool = False,
) -> None:
    await _notify_resource_updated(ctx, _resource_uri(project_id, "sections"))
    await _notify_resource_updated(ctx, _resource_uri(project_id, "file-info"))
    if include_compile_log:
        await _notify_resource_updated(ctx, _resource_uri(project_id, "compile-log"))


def _read_main_file_info(project_id: int) -> dict[str, Any]:
    window = _call(
        "GET",
        f"/api/projects/{project_id}/read-window/?{urlencode({'file_name': 'main.tex', 'start_line': 1, 'end_line': 1})}",
    )
    project_meta = _call("GET", f"/api/projects/{project_id}/")
    assets_payload = _call("GET", f"/api/projects/{project_id}/files/")

    files = assets_payload.get("files") if isinstance(assets_payload, dict) else []
    image_assets: list[dict[str, Any]] = []
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            if bool(item.get("is_image")):
                image_assets.append(
                    {
                        "name": item.get("name"),
                        "extension": item.get("extension"),
                        "size": item.get("size"),
                        "updated_at": item.get("updated_at"),
                    }
                )

    return {
        "project_id": int(project_id),
        "file_name": "main.tex",
        "line_count": int(window.get("total_lines", 0)) if isinstance(window, dict) else 0,
        "char_count": int(window.get("total_chars", 0)) if isinstance(window, dict) else 0,
        "last_modified": project_meta.get("updated_at") if isinstance(project_meta, dict) else None,
        "image_assets": image_assets,
    }


def _line_column_to_position(
        project_id: int,
        line: int,
        column: int,
        file_name: str = "main.tex",
) -> int:
    safe_line = int(line)
    safe_column = int(column)
    if safe_line < 1:
        raise ValueError("line must be >= 1")
    if safe_column < 1:
        raise ValueError("column must be >= 1")

    line_window = _call(
        "GET",
        f"/api/projects/{project_id}/read-window/?{urlencode({'file_name': file_name, 'start_line': 1, 'end_line': safe_line})}",
    )
    if not isinstance(line_window, dict):
        raise ValueError("unable to read file window")
    snippet = str(line_window.get("content") or "")
    lines = snippet.splitlines(keepends=True)
    if len(lines) < safe_line:
        raise ValueError("line out of bounds")

    target_line = lines[safe_line - 1].rstrip("\n")
    max_column = len(target_line) + 1
    effective_column = min(safe_column, max_column)
    before_chars = sum(len(item) for item in lines[: safe_line - 1])
    return before_chars + (effective_column - 1)


def _preview_replacements(
        content: str,
        pattern: str,
        replacement: str,
        *,
        is_regex: bool,
        ignore_case: bool,
        max_replacements: int,
        preview_limit: int = 20,
) -> dict[str, Any]:
    if not pattern:
        raise ValueError("pattern is required")

    flags = re.IGNORECASE if ignore_case else 0
    expr = pattern if is_regex else re.escape(pattern)
    try:
        regex = re.compile(expr, flags)
    except re.error as exc:
        raise ValueError(f"invalid regex pattern: {exc}") from exc

    all_matches = list(regex.finditer(content))
    match_count = len(all_matches)
    replace_count = 0 if max_replacements <= 0 else int(max_replacements)
    updated, replacement_count = regex.subn(replacement, content, count=replace_count)

    previews: list[dict[str, Any]] = []
    for idx, match in enumerate(all_matches[:preview_limit], start=1):
        start = match.start()
        end = match.end()
        before_text = match.group(0)
        if is_regex:
            after_text = regex.sub(replacement, before_text, count=1)
        else:
            after_text = replacement
        line = content.count("\n", 0, start) + 1
        col = start - content.rfind("\n", 0, start)
        previews.append(
            {
                "index": idx,
                "line": line,
                "column": col,
                "start_char": start,
                "end_char": end,
                "before": before_text,
                "after": after_text,
            }
        )

    return {
        "updated_content": updated,
        "match_count": match_count,
        "replacement_count": replacement_count,
        "preview": previews,
        "preview_truncated": match_count > preview_limit,
    }


auth_provider = None
if MCP_OAUTH_ENABLED:
    verifier = DjangoIntrospectionTokenVerifier(
        base_url=AnyHttpUrl(MCP_SERVER_PUBLIC_URL),
        resource_base_url=AnyHttpUrl(MCP_SERVER_PUBLIC_URL),
    )
    auth_provider = RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(AUTH_SERVER_ISSUER_URL)],
        base_url=AnyHttpUrl(MCP_SERVER_PUBLIC_URL),
        resource_base_url=AnyHttpUrl(MCP_SERVER_PUBLIC_URL),
        scopes_supported=["openid", "profile", "smarttex:read", "smarttex:write"],
    )

mcp = FastMCP(
    name="SmartTeX MCP",
    instructions="""
    ## SmartTeX MCP — LuaLaTeX editor assistant

    SmartTeX is a web-based LuaLaTeX editor. Documents are compiled with `lualatex`.
    Use LuaLaTeX-compatible syntax and packages only (e.g. `fontspec`, `unicode-math` — yes; `inputenc` — no).

    ### Project identity
    - NEVER auto-select a project. Wait until the user explicitly names a project (by name or id).
    - Once identified, store the project_id for the entire session — call `list_projects` only once if needed to resolve a name to an id.

    ### Before any edit — mandatory orientation
    1. Call `list_project_sections` (compact=True) — understand structure, get line ranges.
    2. Assume the document is NOT empty. Read before writing.
    3. NEVER call `get_project_file` to read the entire file.
    4. Read only what you need — section content or a narrow window.

    ### Choosing the right read strategy
    | Situation | Tool |
    |---|---|
    | Need document structure | `list_project_sections` compact=True |
    | Need content of one section | `get_project_section` include_content=True |
    | Section is large, need only a fragment | `read_project_window` with exact start_line/end_line |
    | Don't know where something is | `search_project_content` first, then read that window |

    ### Choosing the right edit strategy
    Priority order — always use the most targeted option available:

    1. **`replace_in_project_file`** — for repetitive pattern-based changes across the file (rename a label, fix recurring syntax). Always use `dry_run=True` first to verify scope.
    2. **`update_project_section`** — for meaningful content changes within a named section.
    3. **`rewrite_project_window`** — for targeted changes within a section: if the edit touches only a small fragment of a large section, use `search_project_content` or `list_project_sections` to find the exact line range, then rewrite only those lines.
    4. **`update_project_file`** — ONLY if user explicitly requests a full document replacement. Never use speculatively.

    **Key principle**: the edit scope must match the change scope. Rewriting 200 lines to change 3 is always wrong.

    ### Preserving document integrity
    - Always read the current content of what you're about to change before writing.
    - Preserve existing formatting, indentation, and LaTeX structure.
    - Never introduce or remove blank lines outside the edit target.
    - Never change `\\begin{document}`, preamble, or `\\end{document}` unless user explicitly asks.
    - After a window rewrite, verify line counts are consistent — a rewrite must not silently shift unrelated content.

    ### change_summary — derive automatically
    Every write requires a non-empty `change_summary`. Derive it from user intent. Never ask the user.
    Example: "fix the abstract" → `change_summary="Rewrote abstract per user request"`

    ### Compilation
    - Do NOT compile unless user explicitly asks.
    - To fix compilation errors: `get_compile_log` → locate via `search_project_content` → fix with targeted edit → then compile.

    ### What never to do
    - Never select a project without explicit user instruction.
    - Never read the full file to find a fragment — search first.
    - Never rewrite a full section to change a few lines — use window rewrite with found line range.
    - Never rewrite the full file to change one section.
    - Never compile speculatively.
    - Never ask the user for `change_summary`, `section_index`, or line numbers — derive them.
    """,
    auth=auth_provider,
)


class MCPCompatibilityMiddleware(BaseHTTPMiddleware):
    """Return 200 for generic GET probes to reduce client/proxy false negatives."""

    async def dispatch(self, request: Request, call_next):
        if request.method == "GET" and request.url.path == MCP_PATH:
            accept = request.headers.get("accept", "")
            if "text/event-stream" not in accept and "application/json" not in accept:
                return JSONResponse(
                    {
                        "ok": True,
                        "name": "SmartTeX MCP",
                        "transport": MCP_TRANSPORT,
                        "path": MCP_PATH,
                    }
                )
        return await call_next(request)



@mcp.tool
def list_projects(project_id: int | None = None, name_query: str | None = None) -> list[dict[str, Any]]:
    projects = _call("GET", "/api/projects/")
    if not isinstance(projects, list):
        return []

    normalized_query = (name_query or "").strip().lower()
    results: list[dict[str, Any]] = []

    for project in projects:
        if not isinstance(project, dict):
            continue

        pid = project.get("id")
        title = str(project.get("title", ""))

        if project_id is not None and pid != project_id:
            continue
        if normalized_query and normalized_query not in title.lower():
            continue

        results.append(project)

    return results


@mcp.tool
def read_project_file(
        project_id: int,
        start_line: int | None = None,
        end_line: int | None = None,
        start_char: int | None = None,
        end_char: int | None = None,
        file_name: str = "main.tex",
) -> dict[str, Any]:
    """Read project file content; use start_line/end_line to avoid reading the entire file."""
    if (
            start_line is None
            and end_line is None
            and start_char is None
            and end_char is None
    ):
        probe = _call(
            "GET",
            f"/api/projects/{project_id}/read-window/?{urlencode({'file_name': file_name, 'start_line': 1, 'end_line': 1})}",
        )
        total_lines = 1
        if isinstance(probe, dict):
            total_lines = max(1, int(probe.get("total_lines") or 1))
        params = urlencode({"file_name": file_name, "start_line": 1, "end_line": total_lines})
        return _call("GET", f"/api/projects/{project_id}/read-window/?{params}")

    query: dict[str, Any] = {"file_name": file_name}
    if start_line is not None:
        query["start_line"] = int(start_line)
    if end_line is not None:
        query["end_line"] = int(end_line)
    if start_char is not None:
        query["start_char"] = int(start_char)
    if end_char is not None:
        query["end_char"] = int(end_char)
    params = urlencode(query)
    return _call("GET", f"/api/projects/{project_id}/read-window/?{params}")


@mcp.tool
async def update_project_file(
        project_id: int,
        content: str,
        change_summary: str,
        ctx: Context,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    summary = _require_summary(change_summary)
    payload = _call(
        "PUT",
        f"/api/projects/{project_id}/file/",
        {"content": content, "change_summary": summary, "change_source": "mcp"},
    )
    result = _with_optional_compile(
        project_id,
        payload,
        compileAlso=compileAlso,
        compileLogCompact=compileLogCompact,
        compileMaxLogChars=compileMaxLogChars,
    )
    await _notify_project_write_updates(ctx, project_id, include_compile_log=bool(compileAlso))
    return result


@mcp.tool
def list_project_image_assets(project_id: int) -> dict[str, Any]:
    return _call("GET", f"/api/projects/{project_id}/files/")


@mcp.tool
async def upload_project_image_asset(
        project_id: int,
        asset_filename: str,
        content_base64: str,
        change_summary: str,
        ctx: Context,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    summary = _require_summary(change_summary)
    ext = Path(asset_filename).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp"}:
        raise ValueError("MCP upload_project_image_asset supports images only")
    payload = _call(
        "POST",
        f"/api/projects/{project_id}/files/",
        {
            "filename": asset_filename,
            "content_base64": content_base64,
            "change_summary": summary,
            "change_source": "mcp",
        },
    )
    result = _with_optional_compile(
        project_id,
        payload,
        compileAlso=compileAlso,
        compileLogCompact=compileLogCompact,
        compileMaxLogChars=compileMaxLogChars,
    )
    await _notify_project_write_updates(ctx, project_id, include_compile_log=bool(compileAlso))
    return result


@mcp.tool
def get_project_image_asset_content(project_id: int, asset_filename: str, include_text: bool = False) -> dict[str, Any]:
    params = urlencode({"include_text": str(bool(include_text)).lower()})
    safe_name = quote(asset_filename, safe="")
    return _call("GET", f"/api/projects/{project_id}/files/{safe_name}/content/?{params}")


@mcp.tool
async def rename_project_image_asset(
        project_id: int,
        asset_filename: str,
        new_asset_filename: str,
        change_summary: str,
        ctx: Context,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    summary = _require_summary(change_summary)
    safe_name = quote(asset_filename, safe="")
    payload = _call(
        "POST",
        f"/api/projects/{project_id}/files/{safe_name}/rename/",
        {"new_filename": new_asset_filename, "change_summary": summary, "change_source": "mcp"},
    )
    result = _with_optional_compile(
        project_id,
        payload,
        compileAlso=compileAlso,
        compileLogCompact=compileLogCompact,
        compileMaxLogChars=compileMaxLogChars,
    )
    await _notify_project_write_updates(ctx, project_id, include_compile_log=bool(compileAlso))
    return result


@mcp.tool
async def delete_project_image_asset(
        project_id: int,
        asset_filename: str,
        change_summary: str,
        ctx: Context,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    summary = _require_summary(change_summary)
    safe_name = quote(asset_filename, safe="")
    payload = _call(
        "DELETE",
        f"/api/projects/{project_id}/files/{safe_name}/",
        {"change_summary": summary, "change_source": "mcp"},
    )
    result = _with_optional_compile(
        project_id,
        payload,
        compileAlso=compileAlso,
        compileLogCompact=compileLogCompact,
        compileMaxLogChars=compileMaxLogChars,
    )
    await _notify_project_write_updates(ctx, project_id, include_compile_log=bool(compileAlso))
    return result


@mcp.tool
def list_project_sections(project_id: int, compact: bool = True) -> dict[str, Any]:
    payload = _call("GET", f"/api/projects/{project_id}/sections/")
    if isinstance(payload, dict):
        return _compact_sections_payload(payload, compact=bool(compact))
    return {"sections": [], "sections_compacted": bool(compact)}


@mcp.tool
def find_project_section_by_title(
        project_id: int,
        title_query: str,
        compact: bool = True,
        exact: bool = False,
) -> dict[str, Any]:
    query = (title_query or "").strip().lower()
    if not query:
        raise ValueError("title_query is required")

    payload = _call("GET", f"/api/projects/{project_id}/sections/")
    if not isinstance(payload, dict):
        return {"sections": [], "total_matches": 0}

    sections = payload.get("sections")
    if not isinstance(sections, list):
        return {"sections": [], "total_matches": 0}

    matches: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        title = str(section.get("title", "")).strip()
        normalized_title = title.lower()
        if exact:
            ok = normalized_title == query
        else:
            ok = query in normalized_title
        if ok:
            matches.append(section)

    out: dict[str, Any] = {"sections": matches, "total_matches": len(matches)}
    if compact:
        out = _compact_sections_payload(out, compact=True)
    return out


@mcp.tool
def get_project_section(
        project_id: int,
        section_index: int,
        compact: bool = True,
        include_content: bool = False,
        content_preview_chars: int = 800,
) -> dict[str, Any]:
    payload = _call("GET", f"/api/projects/{project_id}/sections/{section_index}/")
    if not isinstance(payload, dict):
        return {}
    if not compact:
        return payload
    return _compact_single_section_payload(
        payload,
        include_content=bool(include_content),
        content_preview_chars=int(content_preview_chars),
    )


@mcp.tool
async def update_project_section(
        project_id: int,
        section_index: int,
        content: str,
        change_summary: str,
        ctx: Context,
        compact: bool = True,
        include_content: bool = False,
        content_preview_chars: int = 800,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    summary = _require_summary(change_summary)
    payload = _call(
        "PUT",
        f"/api/projects/{project_id}/sections/{section_index}/",
        {"content": content, "change_summary": summary, "change_source": "mcp"},
    )
    if not isinstance(payload, dict):
        shaped: dict[str, Any] = {}
    elif not compact:
        shaped = payload
    else:
        shaped = _compact_single_section_payload(
            payload,
            include_content=bool(include_content),
            content_preview_chars=int(content_preview_chars),
        )
    result = _with_optional_compile(
        project_id,
        shaped,
        compileAlso=compileAlso,
        compileLogCompact=compileLogCompact,
        compileMaxLogChars=compileMaxLogChars,
    )
    await _notify_project_write_updates(ctx, project_id, include_compile_log=bool(compileAlso))
    return result


@mcp.tool
async def insert_text_at_position(
        project_id: int,
        text: str,
        change_summary: str,
        ctx: Context,
        position: int | None = None,
        line: int | None = None,
        column: int | None = None,
        file_name: str = "main.tex",
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    """Insert text using either absolute char `position` or 1-based `line`/`column`."""
    summary = _require_summary(change_summary)
    if file_name != "main.tex":
        raise ValueError("insert_text_at_position currently supports main.tex only")
    if position is None:
        if line is None:
            raise ValueError("provide either position or line")
        resolved_column = 1 if column is None else int(column)
        position = _line_column_to_position(
            project_id,
            line=int(line),
            column=resolved_column,
            file_name=file_name,
        )
    payload = _call(
        "POST",
        f"/api/projects/{project_id}/insert/",
        {"position": position, "text": text, "change_summary": summary, "change_source": "mcp"},
    )
    result = _with_optional_compile(
        project_id,
        payload,
        compileAlso=compileAlso,
        compileLogCompact=compileLogCompact,
        compileMaxLogChars=compileMaxLogChars,
    )
    await _notify_project_write_updates(ctx, project_id, include_compile_log=bool(compileAlso))
    return result


@mcp.tool
async def replace_in_project_file(
        project_id: int,
        pattern: str,
        replacement: str,
        change_summary: str,
        ctx: Context,
        is_regex: bool = False,
        ignore_case: bool = False,
        max_replacements: int = 0,  # 0 = all
        dry_run: bool = True,
) -> dict[str, Any]:
    file_payload = read_project_file(project_id=project_id, file_name="main.tex")
    content = str(file_payload.get("content") or "")
    analysis = _preview_replacements(
        content,
        pattern,
        replacement,
        is_regex=bool(is_regex),
        ignore_case=bool(ignore_case),
        max_replacements=int(max_replacements),
    )

    response: dict[str, Any] = {
        "project_id": int(project_id),
        "file_name": "main.tex",
        "dry_run": bool(dry_run),
        "is_regex": bool(is_regex),
        "ignore_case": bool(ignore_case),
        "max_replacements": int(max_replacements),
        "match_count": analysis["match_count"],
        "replacement_count": analysis["replacement_count"],
        "preview": analysis["preview"],
        "preview_truncated": analysis["preview_truncated"],
    }

    if dry_run:
        response["detail"] = "Dry run only. Re-run with dry_run=False to apply changes."
        return response

    summary = _require_summary(change_summary)
    if analysis["replacement_count"] == 0:
        response["detail"] = "No replacements applied (0 matches)."
        return response

    payload = _call(
        "PUT",
        f"/api/projects/{project_id}/file/",
        {
            "content": analysis["updated_content"],
            "change_summary": summary,
            "change_source": "mcp",
        },
    )
    response["write_result"] = payload
    response["detail"] = "Replacements applied."
    await _notify_project_write_updates(ctx, project_id, include_compile_log=False)
    return response


@mcp.tool
def search_project_content(
        project_id: int,
        query: str,
        is_regex: bool = False,
        ignore_case: bool = True,
        max_results: int = 200,
        include_main: bool = True,
        include_assets: bool = True,
        compact: bool = True,
        include_line_text: bool = False,
        max_matches_in_response: int = 50,
) -> dict[str, Any]:
    params = urlencode(
        {
            "query": query,
            "is_regex": str(bool(is_regex)).lower(),
            "ignore_case": str(bool(ignore_case)).lower(),
            "max_results": int(max_results),
            "include_main": str(bool(include_main)).lower(),
            "include_assets": str(bool(include_assets)).lower(),
        }
    )
    payload = _call("GET", f"/api/projects/{project_id}/search/?{params}")
    if not isinstance(payload, dict):
        return {}
    if not compact:
        return payload
    return _compact_search_payload(
        payload,
        include_line_text=bool(include_line_text),
        max_matches=int(max_matches_in_response),
    )


@mcp.tool
def read_project_window(
        project_id: int,
        start_line: int | None = None,
        end_line: int | None = None,
        start_char: int | None = None,
        end_char: int | None = None,
        file_name: str = "main.tex",
) -> dict[str, Any]:
    """Deprecated alias for read_project_file."""
    return read_project_file(
        project_id=project_id,
        start_line=start_line,
        end_line=end_line,
        start_char=start_char,
        end_char=end_char,
        file_name=file_name,
    )


@mcp.tool
async def rewrite_project_window(
        project_id: int,
        replacement: str,
        ctx: Context,
        file_name: str = "main.tex",
        start_line: int | None = None,
        end_line: int | None = None,
        start_char: int | None = None,
        end_char: int | None = None,
        change_summary: str = "",
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    summary = _require_summary(change_summary)
    payload = {
        "file_name": file_name,
        "replacement": replacement,
        "change_summary": summary,
        "change_source": "mcp",
    }
    if start_line is not None:
        payload["start_line"] = int(start_line)
    if end_line is not None:
        payload["end_line"] = int(end_line)
    if start_char is not None:
        payload["start_char"] = int(start_char)
    if end_char is not None:
        payload["end_char"] = int(end_char)
    result = _call("POST", f"/api/projects/{project_id}/write-window/", payload)
    shaped = _with_optional_compile(
        project_id,
        result,
        compileAlso=compileAlso,
        compileLogCompact=compileLogCompact,
        compileMaxLogChars=compileMaxLogChars,
    )
    await _notify_project_write_updates(ctx, project_id, include_compile_log=bool(compileAlso))
    return shaped


@mcp.tool
def get_project_pdf_page_image(
        project_id: int,
        page: int = 1,
        scale: float = 1.5,
        image_format: str = "png",
) -> dict[str, Any]:
    params = urlencode(
        {
            "page": int(page),
            "scale": float(scale),
            "image_format": image_format,
        }
    )
    return _call("GET", f"/api/projects/{project_id}/pdf-page-image/?{params}")


@mcp.tool
def get_project_pdf_page_count(project_id: int) -> dict[str, Any]:
    return _call("GET", f"/api/projects/{project_id}/pdf-page-count/")


@mcp.tool
def synctex_line_to_page(
        project_id: int,
        line: int,
        file_name: str = "main.tex",
        column: int = 1,
) -> dict[str, Any]:
    params = urlencode(
        {
            "line": int(line),
            "column": int(column),
            "file_name": file_name,
        }
    )
    return _call("GET", f"/api/projects/{project_id}/synctex/line/?{params}")


@mcp.tool
def synctex_page_to_line(project_id: int, page: int, x: float, y: float) -> dict[str, Any]:
    params = urlencode({"page": int(page), "x": float(x), "y": float(y)})
    return _call("GET", f"/api/projects/{project_id}/synctex/pdf/?{params}")


@mcp.tool
def list_project_versions(project_id: int, compact: bool = True, limit: int = 20) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit), 100))
    payload = _call("GET", f"/api/projects/{project_id}/versions/?{urlencode({'limit': safe_limit})}")
    if not compact or not isinstance(payload, dict):
        return payload
    versions = payload.get("versions")
    if not isinstance(versions, list):
        return payload
    return {
        **payload,
        "versions": [
            {
                "id": item.get("id"),
                "version": item.get("number"),
                "source": item.get("source"),
                "operation": item.get("operation"),
                "summary": item.get("summary"),
                "created_at": item.get("created_at"),
            }
            for item in versions
            if isinstance(item, dict)
        ],
        "versions_compacted": True,
    }


@mcp.tool
def get_project_version_diff(project_id: int, version_id: int) -> dict[str, Any]:
    return _call("GET", f"/api/projects/{project_id}/versions/{version_id}/")


@mcp.tool
async def rollback_project_version(
        project_id: int,
        version_id: int,
        summary: str,
        ctx: Context,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    rollback_summary = _require_summary(summary)
    payload = _call(
        "POST",
        f"/api/projects/{project_id}/versions/{version_id}/rollback/",
        {"summary": rollback_summary, "change_source": "mcp"},
    )
    result = _with_optional_compile(
        project_id,
        payload,
        compileAlso=compileAlso,
        compileLogCompact=compileLogCompact,
        compileMaxLogChars=compileMaxLogChars,
    )
    await _notify_project_write_updates(ctx, project_id, include_compile_log=bool(compileAlso))
    return result


@mcp.tool
async def compile_project(project_id: int, ctx: Context, compact_log: bool = True, max_log_chars: int = 4000) -> dict[
    str, Any]:
    payload = _call("POST", f"/api/projects/{project_id}/compile/")
    result = _enrich_compile_payload(
        project_id,
        payload,
        compact_log=bool(compact_log),
        max_log_chars=max(500, min(int(max_log_chars), 20000)),
    )
    await _notify_resource_updated(ctx, _resource_uri(project_id, "compile-log"))
    return result


@mcp.tool
def get_compile_log(project_id: int, compact_log: bool = True, max_log_chars: int = 4000) -> dict[str, Any]:
    payload = _call("GET", f"/api/projects/{project_id}/compile/")
    return _enrich_compile_payload(
        project_id,
        payload,
        compact_log=bool(compact_log),
        max_log_chars=max(500, min(int(max_log_chars), 20000)),
    )


@mcp.tool
def list_templates() -> list[dict[str, Any]]:
    payload = _call("GET", "/api/templates/")
    items: list[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("templates"), list):
        items = payload["templates"]
    else:
        return []
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # List endpoint should stay lightweight; omit raw template content if backend includes it.
        compact_item = {k: v for k, v in item.items() if k != "content"}
        cleaned.append(compact_item)
    return cleaned


@mcp.resource(
    "smarttex://projects/{project_id}/sections",
    name="project-sections",
    title="Project Sections",
    description="Compact list of TeX sections for a project.",
    mime_type="application/json",
)
def resource_project_sections(project_id: int) -> dict[str, Any]:
    payload = _call("GET", f"/api/projects/{int(project_id)}/sections/")
    if isinstance(payload, dict):
        return _compact_sections_payload(payload, compact=True)
    return {"sections": [], "sections_compacted": True}


@mcp.resource(
    "smarttex://projects/{project_id}/compile-log",
    name="project-compile-log",
    title="Project Compile Log",
    description="Latest compile status and compacted compiler log for a project.",
    mime_type="application/json",
)
def resource_project_compile_log(project_id: int) -> dict[str, Any]:
    payload = _call("GET", f"/api/projects/{int(project_id)}/compile/")
    if not isinstance(payload, dict):
        return {"status": "unknown", "log": ""}
    return _enrich_compile_payload(
        int(project_id),
        payload,
        compact_log=True,
        max_log_chars=4000,
    )


@mcp.resource(
    "smarttex://projects/{project_id}/file-info",
    name="project-file-info",
    title="Project Main File Metadata",
    description="Metadata for main.tex including size counters and image assets.",
    mime_type="application/json",
)
def resource_project_file_info(project_id: int) -> dict[str, Any]:
    return _read_main_file_info(int(project_id))


if __name__ == "__main__":
    app = mcp.http_app(path=MCP_PATH, transport=MCP_TRANSPORT)
    app.add_middleware(MCPCompatibilityMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=MCP_CORS_ORIGINS if MCP_CORS_ORIGINS != ["*"] else ["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["*"],
        allow_credentials=False,
    )
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")
