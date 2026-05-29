from __future__ import annotations

import os
import re
import sys
import difflib
import fnmatch
import hashlib
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
MCP_MAX_READ_LINES = max(1, int(os.getenv("MCP_MAX_READ_LINES", "300")))
MCP_MAX_GREP_MATCHES = max(1, int(os.getenv("MCP_MAX_GREP_MATCHES", "20")))
MCP_MAX_GREP_CONTEXT = max(0, int(os.getenv("MCP_MAX_GREP_CONTEXT", "10")))
MCP_MAX_PATCH_LINES = max(1, int(os.getenv("MCP_MAX_PATCH_LINES", "50")))
MCP_MAX_SESSION_FILES = max(1, int(os.getenv("MCP_MAX_SESSION_FILES", "5")))
MCP_MAX_PROPOSAL_LINES = max(1, int(os.getenv("MCP_MAX_PROPOSAL_LINES", "500")))
MCP_MAX_NEW_FILE_LINES = max(1, int(os.getenv("MCP_MAX_NEW_FILE_LINES", "200")))
MCP_MAX_FULL_READ_BYTES = max(1024, int(os.getenv("MCP_MAX_FULL_READ_BYTES", "65536")))
MCP_SESSION_READ_BUDGET = max(1, int(os.getenv("MCP_SESSION_READ_BUDGET", "2000")))
MCP_READ_BUDGET_HARD = os.getenv("MCP_READ_BUDGET_HARD", "false").lower() in {"1", "true", "yes"}
MCP_MAX_SEARCH_RESULTS = max(1, int(os.getenv("MCP_MAX_SEARCH_RESULTS", "30")))
SOURCE_EXTENSIONS = {".tex", ".typ"}
TEXT_EXTENSIONS = {".tex", ".typ", ".sty", ".cls", ".bib", ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".csl"}
READ_BUDGET_STATE: dict[tuple[str, int], int] = {}
REPLACE_DRY_RUN_STATE: dict[tuple[str, int, str], str] = {}


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


def _authorization_from_legacy_request(request: Request) -> str | None:
    """Normalize legacy header-based token formats to Bearer."""
    authorization = (request.headers.get("authorization") or "").strip()
    if authorization:
        lowered = authorization.lower()
        if lowered.startswith("token "):
            value = authorization[6:].strip()
            if value:
                return f"Bearer {value}"
        return None

    token = (request.headers.get("x-api-token") or "").strip()
    if token:
        return f"Bearer {token}"
    return None


def _call(method: str, path: str, data: dict[str, Any] | None = None) -> Any:
    url = f"{BASE_URL}{path}"
    with httpx.Client(timeout=60, headers=_headers()) as client:
        response = client.request(method, url, json=data)
    response.raise_for_status()

    ctype = response.headers.get("content-type", "")
    if "application/json" in ctype:
        return response.json()
    return {"status_code": response.status_code, "text": response.text}


def _call_allow_json_errors(method: str, path: str, data: dict[str, Any] | None = None) -> Any:
    url = f"{BASE_URL}{path}"
    with httpx.Client(timeout=60, headers=_headers()) as client:
        response = client.request(method, url, json=data)
    ctype = response.headers.get("content-type", "")
    payload = response.json() if "application/json" in ctype else {"detail": response.text}
    if response.is_error and not (isinstance(payload, dict) and payload.get("error")):
        response.raise_for_status()
    return payload


def _normalized_project_file_name(file_name: str | None, project_id: int) -> str:
    return str(file_name or _project_main_file_name(project_id)).strip()


def _rejection(error: str, message: str, suggestion: str, **extra: Any) -> dict[str, Any]:
    return {
        "error": error,
        "message": message,
        "suggestion": suggestion,
        **extra,
    }


def _call_upload(path: str, file_bytes: bytes, filename: str = "upload.zip") -> Any:
    url = f"{BASE_URL}{path}"
    headers = {k: v for k, v in _headers().items() if k.lower() != "accept"}
    with httpx.Client(timeout=120, headers=headers) as client:
        response = client.post(url, files={"file": (filename, file_bytes, "application/octet-stream")})
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


def _normalized_mcp_path() -> str:
    path = (MCP_PATH or "/mcp").strip() or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    if path != "/":
        path = path.rstrip("/")
    return path


def _protected_resource_metadata_path() -> str:
    mcp_path = _normalized_mcp_path()
    if mcp_path == "/":
        return "/.well-known/oauth-protected-resource"
    return f"/.well-known/oauth-protected-resource{mcp_path}"


def _protected_resource_metadata_url() -> str:
    return f"{MCP_SERVER_PUBLIC_URL}{_protected_resource_metadata_path()}"


def _canonical_mcp_resource_url() -> str:
    return f"{MCP_SERVER_PUBLIC_URL}{_normalized_mcp_path()}"


def _protected_resource_metadata_payload() -> dict[str, Any]:
    return {
        "resource": _canonical_mcp_resource_url(),
        "authorization_servers": [AUTH_SERVER_ISSUER_URL],
        "scopes_supported": ["smarttex:read", "smarttex:write"],
        "bearer_methods_supported": ["header"],
    }


def _compact_compiler_log(log_text: str, max_chars: int = 4000) -> tuple[str, bool]:
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
        compact, was_truncated = _compact_compiler_log(raw_log, max_chars=max_log_chars)
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
                "file_name": section.get("file_name"),
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
        "file_name": payload.get("file_name"),
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


def _project_meta(project_id: int) -> dict[str, Any]:
    payload = _call("GET", f"/api/projects/{project_id}/")
    if not isinstance(payload, dict):
        return {}
    return payload


def _project_longdoc_meta(project_id: int) -> dict[str, Any]:
    meta = _project_meta(project_id)
    longdoc = meta.get("longdoc") if isinstance(meta, dict) else {}
    return longdoc if isinstance(longdoc, dict) else {}


def _project_main_file_name(project_id: int) -> str:
    project_meta = _project_meta(project_id)
    file_name = str(project_meta.get("main_file_name") or "").strip()
    if file_name:
        return file_name
    return "main.typ" if project_meta.get("markup_type") == "typst" else "main.tex"


def _project_controlled_mode_enabled(project_id: int) -> bool:
    longdoc = _project_longdoc_meta(project_id)
    return bool(longdoc.get("enabled") and longdoc.get("mcp_controlled_access"))


def _controlled_source_write_rejection(project_id: int, filename: str | None = None) -> dict[str, Any] | None:
    if not _project_controlled_mode_enabled(project_id):
        return None
    name = filename or _project_main_file_name(project_id)
    if Path(name).suffix.lower() not in SOURCE_EXTENSIONS:
        return None
    return _rejection(
        "USE_PROPOSAL_WORKFLOW",
        f"Direct writes to source files are disabled in controlled MCP mode.",
        "Use propose_document_change to submit the change for user review.",
    )


def _controlled_folder_write_rejection(project_id: int) -> dict[str, Any] | None:
    if not _project_controlled_mode_enabled(project_id):
        return None
    return _rejection(
        "USE_PROPOSAL_WORKFLOW",
        "Direct folder creation on the main branch is disabled in controlled MCP mode.",
        "Submit a propose_document_change with a create_new_file patch op — the parent folders are created in staging.",
    )


def _project_longdoc_feature_enabled(project_id: int, feature_name: str) -> bool:
    longdoc = _project_longdoc_meta(project_id)
    return bool(longdoc.get("enabled") and longdoc.get(feature_name))


def _project_files_payload(project_id: int) -> list[dict[str, Any]]:
    payload = _call("GET", f"/api/projects/{project_id}/files/")
    files = payload.get("files") if isinstance(payload, dict) else []
    return files if isinstance(files, list) else []


def _project_file_metadata(project_id: int, file_name: str) -> dict[str, Any] | None:
    normalized = _normalized_project_file_name(file_name, project_id)
    for item in _project_files_payload(project_id):
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "") == normalized:
            return item
    return None


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


async def _notify_longdoc_updates(ctx: Context | None, project_id: int, *resource_names: str) -> None:
    names = resource_names or ("overview", "context", "outline", "tasks", "notes", "summaries", "requirements")
    for resource_name in names:
        await _notify_resource_updated(ctx, _resource_uri(project_id, resource_name))


def _read_main_file_info(project_id: int) -> dict[str, Any]:
    file_name = _project_main_file_name(project_id)
    window = _call(
        "GET",
        f"/api/projects/{project_id}/read-window/?{urlencode({'file_name': file_name, 'start_line': 1, 'end_line': 1})}",
    )
    project_meta = _project_meta(project_id)
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
        "file_name": file_name,
        "line_count": int(window.get("total_lines", 0)) if isinstance(window, dict) else 0,
        "char_count": int(window.get("total_chars", 0)) if isinstance(window, dict) else 0,
        "last_modified": project_meta.get("updated_at") if isinstance(project_meta, dict) else None,
        "image_assets": image_assets,
    }


def _budget_key(project_id: int) -> tuple[str, int]:
    token = _current_bearer_token() or LEGACY_TOKEN or "anonymous"
    return token, int(project_id)


def _read_budget_remaining(project_id: int) -> int:
    try:
        payload = _call_allow_json_errors("GET", f"/api/projects/{project_id}/mcp-budget/")
        if isinstance(payload, dict) and "remaining" in payload:
            val = int(payload["remaining"])
            READ_BUDGET_STATE[_budget_key(project_id)] = val
            return val
    except Exception:
        pass
    return READ_BUDGET_STATE.get(_budget_key(project_id), MCP_SESSION_READ_BUDGET)


def _attach_read_budget(payload: dict[str, Any], project_id: int) -> dict[str, Any]:
    payload["read_budget_remaining"] = max(0, _read_budget_remaining(project_id))
    return payload


def _consume_read_budget(
        project_id: int,
        lines: int,
        *,
        suggestion: str,
) -> dict[str, Any] | None:
    cost = max(0, int(lines))
    remaining_before = _read_budget_remaining(project_id)
    raw_remaining_after = remaining_before - cost
    # Persist to Django cache; fall back to in-memory on failure.
    try:
        resp = _call_allow_json_errors("POST", f"/api/projects/{project_id}/mcp-budget/", {"lines": cost})
        if isinstance(resp, dict) and "remaining" in resp:
            remaining_after_stored = int(resp["remaining"])
            READ_BUDGET_STATE[_budget_key(project_id)] = remaining_after_stored
        else:
            raise ValueError("unexpected budget response")
    except Exception:
        key = _budget_key(project_id)
        remaining_after_stored = max(0, raw_remaining_after)
        READ_BUDGET_STATE[key] = remaining_after_stored
    if MCP_READ_BUDGET_HARD and raw_remaining_after < 0:
        return _rejection(
            "READ_BUDGET_EXHAUSTED",
            f"Read budget exhausted for project {project_id}. Requested {cost} more lines with {max(0, remaining_before)} remaining.",
            suggestion,
            read_budget_remaining=max(0, remaining_before),
            requested_lines=cost,
        )
    if raw_remaining_after < 0:
        return _rejection(
            "READ_BUDGET_EXHAUSTED",
            f"Read budget is exhausted for project {project_id}, but the read was allowed because hard enforcement is off.",
            suggestion,
            read_budget_remaining=0,
            requested_lines=cost,
            warning=True,
        )
    return None


def _read_file_lines_raw(project_id: int, file_name: str, start_line: int, end_line: int) -> dict[str, Any]:
    params = urlencode(
        {
            "file_name": _normalized_project_file_name(file_name, project_id),
            "start_line": int(start_line),
            "end_line": int(end_line),
        }
    )
    payload = _call("GET", f"/api/projects/{project_id}/read-window/?{params}")
    return payload if isinstance(payload, dict) else {}


def _file_line_info(project_id: int, file_name: str) -> dict[str, Any]:
    normalized = _normalized_project_file_name(file_name, project_id)
    metadata = _project_file_metadata(project_id, normalized)
    if metadata is None:
        raise ValueError("file not found")
    if bool(metadata.get("is_dir")):
        raise ValueError("file is a directory")
    window = _read_file_lines_raw(project_id, normalized, 1, 1)
    return {
        "file_name": normalized,
        "size_bytes": int(metadata.get("size") or 0),
        "line_count": int(window.get("total_lines") or 0),
        "total_chars": int(window.get("total_chars") or 0),
        "modified_at": metadata.get("updated_at"),
        "extension": metadata.get("extension") or "",
        "is_text": bool(metadata.get("is_text")),
        "is_image": bool(metadata.get("is_image")),
    }


def _normalize_line_text(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())


def _compute_diff_stats(before: str, after: str) -> tuple[str, int, int]:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )
    lines_added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    lines_removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    return "\n".join(diff_lines), lines_added, lines_removed


def _ensure_patch_size_allowed(lines_added: int, lines_removed: int) -> dict[str, Any] | None:
    changed = lines_added + lines_removed
    if changed <= MCP_MAX_PATCH_LINES:
        return None
    return _rejection(
        "PATCH_TOO_LARGE",
        f"Requested patch changes {changed} diff lines, exceeding the limit of {MCP_MAX_PATCH_LINES}.",
        "Reduce the edit scope, then retry with patch_file_lines or update_project_section on a smaller range.",
        lines_added=lines_added,
        lines_removed=lines_removed,
        max_patch_lines=MCP_MAX_PATCH_LINES,
    )


def _latest_version_number(project_id: int, file_name: str | None = None) -> int | None:
    params = {"limit": 1}
    if file_name:
        params["file"] = file_name
    payload = _call("GET", f"/api/projects/{project_id}/versions/?{urlencode(params)}")
    versions = payload.get("versions") if isinstance(payload, dict) else []
    if not versions:
        return None
    first = versions[0]
    if not isinstance(first, dict):
        return None
    number = first.get("number")
    if isinstance(number, int):
        return number
    version_id = first.get("id")
    return int(version_id) if isinstance(version_id, int) else None


def _replace_preview_key(project_id: int, file_name: str, pattern: str, replacement: str, is_regex: bool, ignore_case: bool, max_replacements: int) -> tuple[str, int, str]:
    token = _current_bearer_token() or LEGACY_TOKEN or "anonymous"
    digest = hashlib.sha256(
        f"{project_id}\0{file_name}\0{pattern}\0{replacement}\0{int(is_regex)}\0{int(ignore_case)}\0{int(max_replacements)}".encode("utf-8")
    ).hexdigest()
    return token, int(project_id), digest


def _char_window_line_cost(payload: dict[str, Any]) -> int:
    start_line = int(payload.get("start_line") or 1)
    end_line = int(payload.get("end_line") or start_line)
    return max(1, end_line - start_line + 1)


def _line_column_to_position(
        project_id: int,
        line: int,
        column: int,
        file_name: str | None = None,
) -> int:
    resolved_file_name = file_name or _project_main_file_name(project_id)
    safe_line = int(line)
    safe_column = int(column)
    if safe_line < 1:
        raise ValueError("line must be >= 1")
    if safe_column < 1:
        raise ValueError("column must be >= 1")

    line_window = _call(
        "GET",
        f"/api/projects/{project_id}/read-window/?{urlencode({'file_name': resolved_file_name, 'start_line': 1, 'end_line': safe_line})}",
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
        scopes_supported=["smarttex:read", "smarttex:write"],
    )

mcp = FastMCP(
    name="SmartTeX MCP",
    instructions="""
    ## SmartTeX MCP — document editor assistant (LaTeX / Typst)

    SmartTeX edits and compiles two markup formats: LaTeX and Typst.

    ### Project identity
    - NEVER auto-select a project. Wait until the user explicitly names a project (by name or id).
    - Once identified, store the project_id for the entire session — call `list_projects` only once if needed to resolve a name to an id.
    - Before editing, read `markup_type` from `list_projects` or project metadata.
    - If `markup_type == "latex"`: write LuaLaTeX-compatible code only (`fontspec`, `unicode-math`; no `inputenc`).
    - If `markup_type == "typst"`: write valid Typst syntax only (`#set`, `#show`, `= Heading`).
    - Never mix LaTeX and Typst syntax in one file.
    - The active main source file is `main_file_name` from project metadata (defaults to `main.tex` or `main.typ`).

    ### Before any edit — mandatory orientation
    1. Call `list_project_sections` (compact=True) — understand structure and get line ranges.
       Each section now includes `file_name` — note which file it belongs to before editing.
    2. Assume the document is NOT empty. Read before writing.
    3. NEVER call `get_project_file` to read the entire file.
    4. Read only what you need — section content or a narrow window.
    5. In controlled MCP mode, prefer `file_line_count`, `read_file_lines`, and `grep_file` over broad reads.

    ### Choosing the right read strategy
    | Situation | Tool |
    |---|---|
    | Need document structure | `list_project_sections` compact=True |
    | Need content of one section | `get_project_section` include_content=True |
    | Section is large, need only a fragment | `read_project_window` with exact start_line/end_line |
    | Don't know where something is | `search_project_content` first, then read that window |
    | Need line-precise read from any text file | `read_file_lines` |
    | Need file inventory / sizes / line counts | `find_project_files`, `file_line_count` |
    | Need targeted search in one file | `grep_file` |
    | Need content of an auxiliary file | `get_project_file_content(include_text=True)` |

    ### Choosing the right edit strategy
    Priority order — always use the most targeted option available:

    1. **`patch_file_lines`** — preferred for small, exact line-range edits. Use anchors whenever possible.
    2. **`replace_in_project_file`** — for one exact match only. Always use `dry_run=True` first, then repeat the exact request with `dry_run=False`.
    3. **`update_project_section`** — for meaningful content changes within a named section. This is the preferred large-source-file rewrite path.
    4. **`append_to_file`** — for additive content at EOF or after a named section.
    5. **`rewrite_project_window`** — for targeted changes within auxiliary text files or carefully bounded windows.
    6. **`update_project_file`** — ONLY outside controlled MCP mode, and only if the user explicitly requests a full document replacement.

    **Key principle**: the edit scope must match the change scope. Rewriting 200 lines to change 3 is always wrong.

    ### Working with project files
    - Typst projects have a multi-file structure: `main.typ` orchestrates chapter files like `chapters/01-introduction.typ`.
    - LaTeX projects use `main.tex` plus helpers such as `.sty`, `.cls`, `.bib`.
    - Sections returned by `list_project_sections` include `file_name` — use it to target the right file for edits.
    - To inspect all project entries (files and folders), use `list_project_files`.
    - To read an auxiliary text file, use `get_project_file_content(include_text=True)`.
    - To create a new text file, use `create_project_text_file` (non-source files only in controlled mode).
    - To create a new folder, use `create_project_folder` (disabled in controlled mode — use a proposal with a `create_new_file` op at the nested path instead).
    - To rename/delete entries, use `rename_project_file` / `delete_project_file`.
    - To edit an existing auxiliary text file, use `rewrite_project_window` with explicit `file_name`.
    - To import a *user-supplied* ZIP archive into a Typst project, use `import_project_zip` with the ZIP as base64. Never use it as a workaround for write rejections — bundling generated files into a ZIP to bypass `USE_PROPOSAL_WORKFLOW` is not allowed; use `propose_document_change` with multiple `create_new_file` ops instead.

    ### Controlled MCP mode write routing
    In controlled mode, direct writes to source files (`.tex`, `.typ`) and to folders on the main branch are rejected with `USE_PROPOSAL_WORKFLOW`. The single correct response is `propose_document_change` with the appropriate patch ops:
    - new source file (any nested path) → `create_new_file` op (parent folders auto-created in staging)
    - existing source file edit → `update_section` / `replace_text` / `patch_lines` ops
    - multiple files → ONE proposal carrying multiple ops, not several proposals and not a ZIP import
    Non-source assets (images, `.bib`, `.cls`, `.sty`) remain creatable directly. If a direct-write tool returns `USE_PROPOSAL_WORKFLOW`, do not look for another tool that bypasses it — the gate is intentional.
    - Changing any imported `.typ` file still requires compile to update PDF.

    ### Writing Assistant data
    - If long-document mode is enabled, prefer the structured tools over editing hidden support files directly.
    - Use `get_longdoc_overview` first for a compact snapshot of context, outline, tasks, notes, summaries, and requirements.
    - Context files: `list_context_files`, `read_context_file`, `update_context_file`.
    - Outline: `read_outline`, `add_outline_item`, `update_outline_item`.
    - Tasks: `list_tasks`, `add_task`, `complete_task`, `update_task_status`.
    - Notes: `read_notes`, `append_to_note_section`, `replace_note_section`.
    - Section summaries: `list_section_summaries`, `read_section_summary`, `update_section_summary`.
    - Requirements: `list_requirements`, `add_requirement`, `update_requirement_coverage`.
    - If a longdoc tool returns `FEATURE_DISABLED` or `PROJECT_LOCKED`, stop and follow the suggestion instead of retrying writes.

    ### Version history
    - `list_project_versions` returns a compacted history with `target_file` and `is_revertible`.
    - Use `file_filter` to scope history to a single file (e.g. `file_filter="chapters/intro.typ"`).
    - Only versions with `is_revertible=True` can be rolled back via `rollback_project_version`.
    - Compile results also appear in version history (operation=`compile`).

    ### Compile results
    - Compile payloads now include `compile_state` (`synced`/`failed`/`out_of_date`) and `diagnostics` (list of `{file, line, column, severity, message}`).
    - Use `diagnostics` to locate errors precisely instead of grepping the raw log.

    ### Preserving document integrity
    - Always read the current content of what you're about to change before writing.
    - Preserve existing formatting, indentation, and document structure.
    - Never introduce or remove blank lines outside the edit target.
    - In controlled MCP mode, expect full-file reads/writes to be capped or rejected. Follow the suggested patch/read tools instead of retrying the blocked tool.
    - For LaTeX, never change `\\begin{document}`, preamble, or `\\end{document}` unless user explicitly asks.
    - After a window rewrite, verify line counts are consistent — a rewrite must not silently shift unrelated content.

    ### change_summary — derive automatically
    Every write requires a non-empty `change_summary`. Derive it from user intent. Never ask the user.
    Example: "fix the abstract" → `change_summary="Rewrote abstract per user request"`

    ### Compilation
    - Do NOT compile unless user explicitly asks.
    - To fix compilation errors: `get_compile_log` → check `diagnostics` for exact location → fix with targeted edit → then compile.
    - SyncTeX mappings are available only for LaTeX projects. Do not call SyncTeX tools for Typst.

    ### What never to do
    - Never select a project without explicit user instruction.
    - Never read the full file to find a fragment — search first.
    - Never ignore a structured rejection response. Follow the `suggestion` field and switch to the narrower tool it recommends.
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
        authorization = _authorization_from_legacy_request(request)
        if authorization:
            scope = request.scope
            headers = list(scope.get("headers") or [])
            headers.append((b"authorization", authorization.encode("latin-1")))
            scope["headers"] = headers

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
        response = await call_next(request)
        if response.status_code == 401 and request.url.path.rstrip("/") == _normalized_mcp_path().rstrip("/"):
            response.headers.setdefault(
                "WWW-Authenticate",
                (
                    'Bearer realm="mcp", error="invalid_token", '
                    f'resource_metadata="{_protected_resource_metadata_url()}"'
                ),
            )
        return response


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource_metadata_root(_request: Request):
    return JSONResponse(_protected_resource_metadata_payload())


@mcp.custom_route(_protected_resource_metadata_path(), methods=["GET"])
async def oauth_protected_resource_metadata_for_mcp(_request: Request):
    return JSONResponse(_protected_resource_metadata_payload())



@mcp.tool
def list_projects(project_id: int | None = None, name_query: str | None = None) -> list[dict[str, Any]]:
    """List user projects and their metadata (`id`, `title`, `markup_type`, compile state)."""
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
        file_name: str | None = None,
) -> dict[str, Any]:
    """Read a text file window from the project (`main.tex`/`main.typ` by default)."""
    resolved_file_name = file_name or _project_main_file_name(project_id)
    controlled = _project_controlled_mode_enabled(project_id)
    if (
            start_line is None
            and end_line is None
            and start_char is None
            and end_char is None
    ):
        info = _file_line_info(project_id, resolved_file_name)
        total_lines = max(1, int(info["line_count"] or 1))
        if controlled and int(info["size_bytes"] or 0) > MCP_MAX_FULL_READ_BYTES:
            return _attach_read_budget(
                _rejection(
                    "FILE_TOO_LARGE",
                    f"{resolved_file_name} is {total_lines} lines ({int(info['size_bytes'])} bytes). Full reads are limited to {MCP_MAX_FULL_READ_BYTES} bytes.",
                    "Use file_line_count to inspect the file, read_file_lines for a range, or get_project_section to read by section.",
                    line_count=total_lines,
                    size_bytes=int(info["size_bytes"] or 0),
                ),
                project_id,
            )
        budget_rejection = _consume_read_budget(
            project_id,
            total_lines,
            suggestion="Use grep_file or read_file_lines for narrower reads, or get_project_section for section-scoped access.",
        ) if controlled else None
        if budget_rejection and not budget_rejection.get("warning"):
            return budget_rejection
        params = urlencode({"file_name": resolved_file_name, "start_line": 1, "end_line": total_lines})
        payload = _call("GET", f"/api/projects/{project_id}/read-window/?{params}")
        if not isinstance(payload, dict):
            return {}
        if budget_rejection:
            payload["budget_warning"] = budget_rejection
        return _attach_read_budget(payload, project_id)

    if controlled and (start_line is not None or end_line is not None) and start_char is None and end_char is None:
        return read_file_lines(
            project_id=project_id,
            filename=resolved_file_name,
            start_line=1 if start_line is None else int(start_line),
            end_line=(1 if start_line is None else int(start_line)) if end_line is None else int(end_line),
        )

    query: dict[str, Any] = {"file_name": resolved_file_name}
    if start_line is not None:
        query["start_line"] = int(start_line)
    if end_line is not None:
        query["end_line"] = int(end_line)
    if start_char is not None:
        query["start_char"] = int(start_char)
    if end_char is not None:
        query["end_char"] = int(end_char)
    params = urlencode(query)
    payload = _call("GET", f"/api/projects/{project_id}/read-window/?{params}")
    if not isinstance(payload, dict):
        return {}
    if controlled and (start_char is not None or end_char is not None):
        budget_rejection = _consume_read_budget(
            project_id,
            _char_window_line_cost(payload),
            suggestion="Use read_file_lines or grep_file for more targeted reads.",
        )
        if budget_rejection and not budget_rejection.get("warning"):
            return budget_rejection
        if budget_rejection:
            payload["budget_warning"] = budget_rejection
    return _attach_read_budget(payload, project_id)


@mcp.tool
def find_project_files(project_id: int, pattern: str | None = None, file_type: str = "any") -> dict[str, Any]:
    """Find project files by glob pattern and coarse file type."""
    normalized_type = str(file_type or "any").strip().lower()
    if normalized_type not in {"text", "image", "pdf", "any"}:
        raise ValueError("file_type must be one of: text, image, pdf, any")
    files = []
    for item in _project_files_payload(project_id):
        if not isinstance(item, dict) or bool(item.get("is_dir")):
            continue
        name = str(item.get("name") or "")
        if pattern and not fnmatch.fnmatch(name, pattern):
            continue
        ext = str(item.get("extension") or "").lower()
        if normalized_type == "text" and not bool(item.get("is_text")):
            continue
        if normalized_type == "image" and not bool(item.get("is_image")):
            continue
        if normalized_type == "pdf" and ext != ".pdf":
            continue
        line_count = None
        if bool(item.get("is_text")):
            line_count = _file_line_info(project_id, name)["line_count"]
        files.append(
            {
                "path": name,
                "size_bytes": int(item.get("size") or 0),
                "line_count": line_count,
                "modified_at": item.get("updated_at"),
            }
        )
    return _attach_read_budget({"files": files}, project_id)


@mcp.tool
def file_line_count(project_id: int, filename: str) -> dict[str, Any]:
    """Return line count and size metadata for a text file."""
    info = _file_line_info(project_id, filename)
    payload = {
        "filename": info["file_name"],
        "lines": info["line_count"],
        "size_bytes": info["size_bytes"],
    }
    return _attach_read_budget(payload, project_id)


@mcp.tool
def read_file_lines(project_id: int, filename: str, start_line: int, end_line: int) -> dict[str, Any]:
    """Read a 1-indexed inclusive line range from a project text file."""
    safe_start = int(start_line)
    safe_end = int(end_line)
    if safe_start < 1 or safe_end < 1:
        raise ValueError("line numbers are 1-based and must be positive")
    if safe_end < safe_start:
        raise ValueError("end_line must be >= start_line")
    requested_lines = safe_end - safe_start + 1
    if requested_lines > MCP_MAX_READ_LINES:
        return _attach_read_budget(
            _rejection(
                "READ_LIMIT_EXCEEDED",
                f"Requested {requested_lines} lines from {filename}, exceeding the per-read limit of {MCP_MAX_READ_LINES}.",
                "Split the read into smaller read_file_lines calls or use get_project_section if you need a whole section.",
                requested_lines=requested_lines,
                max_read_lines=MCP_MAX_READ_LINES,
            ),
            project_id,
        )
    budget_rejection = _consume_read_budget(
        project_id,
        requested_lines,
        suggestion="Use grep_file to locate the exact area first, then read a narrower line range.",
    )
    if budget_rejection and not budget_rejection.get("warning"):
        return budget_rejection
    payload = _read_file_lines_raw(project_id, filename, safe_start, safe_end)
    shaped = {
        "filename": payload.get("file_name") or filename,
        "content": payload.get("content") or "",
        "start_line": int(payload.get("start_line") or safe_start),
        "end_line": int(payload.get("end_line") or safe_end),
        "total_lines": int(payload.get("total_lines") or 0),
        "truncated": requested_lines > int(payload.get("end_line") or safe_end) - int(payload.get("start_line") or safe_start) + 1,
    }
    if budget_rejection:
        shaped["budget_warning"] = budget_rejection
    return _attach_read_budget(shaped, project_id)


@mcp.tool
def grep_file(
        project_id: int,
        filename: str,
        pattern: str,
        context_lines: int = 3,
        max_matches: int = 20,
        use_regex: bool = False,
) -> dict[str, Any]:
    """Search one file with optional surrounding context lines."""
    if not pattern:
        raise ValueError("pattern is required")
    safe_context = max(0, min(int(context_lines), MCP_MAX_GREP_CONTEXT))
    safe_max_matches = max(1, min(int(max_matches), MCP_MAX_GREP_MATCHES))
    estimated_cost = safe_max_matches * (1 + (2 * safe_context))
    budget_rejection = _consume_read_budget(
        project_id,
        estimated_cost,
        suggestion="Reduce max_matches or context_lines, or use read_file_lines after locating a narrower region.",
    )
    if budget_rejection and not budget_rejection.get("warning"):
        return budget_rejection
    info = _file_line_info(project_id, filename)
    if not info["is_text"]:
        return _attach_read_budget(
            _rejection(
                "NOT_TEXT_FILE",
                f"{info['file_name']} is not a text file.",
                "Use get_project_file_content for binary files or choose a text file.",
            ),
            project_id,
        )
    total_lines = max(1, int(info["line_count"]))
    all_lines: list[str] = []
    for start in range(1, total_lines + 1, MCP_MAX_READ_LINES):
        end = min(total_lines, start + MCP_MAX_READ_LINES - 1)
        payload = _read_file_lines_raw(project_id, info["file_name"], start, end)
        all_lines.extend(str(payload.get("content") or "").splitlines())
    lines = all_lines
    flags = 0
    expr = pattern
    if not use_regex:
        expr = re.escape(pattern)
    try:
        regex = re.compile(expr, flags)
    except re.error as exc:
        raise ValueError(f"invalid regex pattern: {exc}") from exc
    matches = []
    for index, line in enumerate(lines, start=1):
        if not regex.search(line):
            continue
        before_start = max(1, index - safe_context)
        after_end = min(len(lines), index + safe_context)
        matches.append(
            {
                "line_number": index,
                "line": line,
                "before": lines[before_start - 1 : index - 1],
                "after": lines[index:after_end],
            }
        )
        if len(matches) >= safe_max_matches:
            break
    payload = {
        "filename": info["file_name"],
        "matches": matches,
        "max_matches": safe_max_matches,
        "context_lines": safe_context,
        "truncated": len(matches) >= safe_max_matches,
    }
    if budget_rejection:
        payload["budget_warning"] = budget_rejection
    return _attach_read_budget(payload, project_id)


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
    """Replace the whole main source file (`main.tex` or `main.typ`).

    DISABLED in controlled MCP mode (returns USE_PROPOSAL_WORKFLOW). To modify an
    existing source file under controlled mode, use `propose_document_change`
    (which routes the change through staging + review). Outside controlled mode,
    only use this for an explicit full-document replacement — for targeted edits
    prefer `patch_file_lines`, `replace_in_project_file`, or `update_project_section`.
    """
    rejection = _controlled_source_write_rejection(project_id)
    if rejection:
        return rejection
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
def list_project_files(project_id: int) -> dict[str, Any]:
    """List project entries (files and folders)."""
    return _call("GET", f"/api/projects/{project_id}/files/")


@mcp.tool
def list_project_image_assets(project_id: int) -> dict[str, Any]:
    """[deprecated] Use `list_project_files`."""
    return list_project_files(project_id)


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
    """Upload an image file only (`.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.svg`, `.webp`)."""
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
def get_project_file_content(project_id: int, asset_filename: str, include_text: bool = False) -> dict[str, Any]:
    """Read file bytes (base64). Set `include_text=True` for text files."""
    params = urlencode({"include_text": str(bool(include_text)).lower()})
    safe_name = quote(asset_filename, safe="")
    return _call("GET", f"/api/projects/{project_id}/files/{safe_name}/content/?{params}")


@mcp.tool
def get_project_image_asset_content(project_id: int, asset_filename: str, include_text: bool = False) -> dict[str, Any]:
    """[deprecated] Use `get_project_file_content`."""
    return get_project_file_content(project_id, asset_filename, include_text=include_text)


@mcp.tool
async def create_project_text_file(
        project_id: int,
        filename: str,
        content: str,
        change_summary: str,
        ctx: Context,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    """Create a new text file (supports nested paths like `chapters/intro.typ`).

    DISABLED for source files (`.tex`, `.typ`) in controlled MCP mode — returns
    USE_PROPOSAL_WORKFLOW. To create a new source file under controlled mode,
    submit `propose_document_change` with a `create_new_file` patch op (parent
    folders are created implicitly). Non-source text files (e.g. `.bib`, `.cls`,
    `.sty`) are accepted directly even in controlled mode.

    Prefer this tool over `import_project_zip` for batch-creating files: call it
    once per file. `import_project_zip` is intended only for genuine
    user-supplied archives, not as a workaround for write rejections.
    """
    rejection = _controlled_source_write_rejection(project_id, filename)
    if rejection:
        return rejection
    summary = _require_summary(change_summary)
    payload = _call(
        "POST",
        f"/api/projects/{project_id}/files/",
        {
            "filename": filename,
            "text_content": content,
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
async def create_project_folder(
        project_id: int,
        folder_path: str,
        change_summary: str,
        ctx: Context,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    """Create a new folder in the project (supports nested paths like `chapters/appendix`).

    DISABLED in controlled MCP mode — returns USE_PROPOSAL_WORKFLOW. Folder
    creation on the main branch is not allowed; route it through
    `propose_document_change` with a `create_new_file` patch op whose filename
    targets the desired nested path (parent folders are created in staging).
    """
    rejection = _controlled_folder_write_rejection(project_id)
    if rejection:
        return rejection
    summary = _require_summary(change_summary)
    payload = _call(
        "POST",
        f"/api/projects/{project_id}/files/",
        {
            "filename": folder_path,
            "entry_kind": "directory",
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
    """[deprecated] Use `rename_project_file`."""
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
    """[deprecated] Use `delete_project_file`."""
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
async def rename_project_file(
        project_id: int,
        asset_filename: str,
        new_asset_filename: str,
        change_summary: str,
        ctx: Context,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    """Rename a project file or folder."""
    return await rename_project_image_asset(
        project_id=project_id,
        asset_filename=asset_filename,
        new_asset_filename=new_asset_filename,
        change_summary=change_summary,
        ctx=ctx,
        compileAlso=compileAlso,
        compileLogCompact=compileLogCompact,
        compileMaxLogChars=compileMaxLogChars,
    )


@mcp.tool
async def delete_project_file(
        project_id: int,
        asset_filename: str,
        change_summary: str,
        ctx: Context,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    """Delete a project file or folder."""
    rejection = _controlled_source_write_rejection(project_id, asset_filename)
    if rejection:
        return rejection
    return await delete_project_image_asset(
        project_id=project_id,
        asset_filename=asset_filename,
        change_summary=change_summary,
        ctx=ctx,
        compileAlso=compileAlso,
        compileLogCompact=compileLogCompact,
        compileMaxLogChars=compileMaxLogChars,
    )


@mcp.tool
def list_project_sections(project_id: int, compact: bool = True) -> dict[str, Any]:
    """List detected document sections/headings from the main source file."""
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
    """Get one section from the main source file by section index."""
    payload = _call("GET", f"/api/projects/{project_id}/sections/{section_index}/")
    if not isinstance(payload, dict):
        return {}
    content = str(payload.get("content") or "")
    if _project_controlled_mode_enabled(project_id) and content:
        line_count = max(1, len(content.splitlines()))
        budget_rejection = _consume_read_budget(
            project_id,
            line_count,
            suggestion="Use read_file_lines for a narrower excerpt or grep_file to target the exact subsection.",
        )
        if budget_rejection and not budget_rejection.get("warning"):
            return budget_rejection
        if line_count > MCP_MAX_READ_LINES:
            payload["read_warning"] = _rejection(
                "SECTION_READ_LARGE",
                f"Section {section_index} spans {line_count} lines, exceeding the recommended read size of {MCP_MAX_READ_LINES}.",
                "Use read_file_lines with the section's line range if you only need part of the section.",
                line_count=line_count,
                max_read_lines=MCP_MAX_READ_LINES,
                warning=True,
            )
        if budget_rejection:
            payload["budget_warning"] = budget_rejection
    if not compact:
        return _attach_read_budget(payload, project_id)
    shaped = _compact_single_section_payload(
        payload,
        include_content=bool(include_content),
        content_preview_chars=int(content_preview_chars),
    )
    if "read_warning" in payload:
        shaped["read_warning"] = payload["read_warning"]
    if "budget_warning" in payload:
        shaped["budget_warning"] = payload["budget_warning"]
    return _attach_read_budget(shaped, project_id)


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
    """Replace one section in the main source file."""
    rejection = _controlled_source_write_rejection(project_id)
    if rejection:
        return rejection
    summary = _require_summary(change_summary)
    before_payload = _call("GET", f"/api/projects/{project_id}/sections/{section_index}/")
    before_content = str(before_payload.get("content") or "") if isinstance(before_payload, dict) else ""
    if _project_controlled_mode_enabled(project_id):
        _, lines_added, lines_removed = _compute_diff_stats(before_content, content)
        patch_rejection = _ensure_patch_size_allowed(lines_added, lines_removed)
        if patch_rejection:
            return patch_rejection
    payload = _call(
        "PUT",
        f"/api/projects/{project_id}/sections/{section_index}/",
        {"content": content, "change_summary": summary, "change_source": "mcp"},
    )
    updated_content = str(payload.get("content") or "") if isinstance(payload, dict) else content
    diff_text, lines_added, lines_removed = _compute_diff_stats(before_content, updated_content)
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
    shaped["diff_text"] = diff_text
    shaped["lines_added"] = lines_added
    shaped["lines_removed"] = lines_removed
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
        file_name: str | None = None,
        anchor_text: str | None = None,
        insert_after_line: int | None = None,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    """Insert text into the main source file using absolute char `position` or 1-based `line`/`column`."""
    summary = _require_summary(change_summary)
    resolved_file_name = file_name or _project_main_file_name(project_id)
    rejection = _controlled_source_write_rejection(project_id, resolved_file_name)
    if rejection:
        return rejection
    controlled = _project_controlled_mode_enabled(project_id)
    if controlled and not str(anchor_text or "").strip():
        return _rejection(
            "ANCHOR_REQUIRED",
            "insert_text_at_position requires anchor_text in controlled MCP mode.",
            "Provide anchor_text together with insert_after_line or a precise line/column so the insertion can be verified.",
        )
    if insert_after_line is not None:
        line_info = _file_line_info(project_id, resolved_file_name)
        safe_insert_after = int(insert_after_line)
        if safe_insert_after < 1 or safe_insert_after > max(1, int(line_info["line_count"])):
            raise ValueError("insert_after_line is out of bounds")
        line_payload = _read_file_lines_raw(project_id, resolved_file_name, safe_insert_after, safe_insert_after)
        anchor_line = str(line_payload.get("content") or "").splitlines()[0] if str(line_payload.get("content") or "").splitlines() else ""
        if controlled and _normalize_line_text(anchor_line) != _normalize_line_text(anchor_text):
            return _rejection(
                "ANCHOR_MISMATCH",
                f"anchor_text did not match line {safe_insert_after} in {resolved_file_name}.",
                "Re-run grep_file to find the current anchor text, then retry with the updated line number.",
                searched_line=safe_insert_after,
            )
        line = safe_insert_after + 1
        column = 1
        position = None
    if position is None:
        if line is None:
            raise ValueError("provide either position or line")
        resolved_column = 1 if column is None else int(column)
        position = _line_column_to_position(
            project_id,
            line=int(line),
            column=resolved_column,
            file_name=resolved_file_name,
        )
    if controlled:
        anchor = str(anchor_text or "")
        window_start = max(0, int(position) - max(32, len(anchor)))
        window_end = int(position) + max(32, len(anchor))
        anchor_window = _call(
            "GET",
            f"/api/projects/{project_id}/read-window/?{urlencode({'file_name': resolved_file_name, 'start_char': window_start, 'end_char': window_end})}",
        )
        snippet = str(anchor_window.get("content") or "") if isinstance(anchor_window, dict) else ""
        relative_position = int(position) - window_start
        before_snippet = snippet[:relative_position]
        after_snippet = snippet[relative_position:]
        if not (before_snippet.endswith(anchor) or after_snippet.startswith(anchor)):
            return _rejection(
                "ANCHOR_MISMATCH",
                f"anchor_text was not found immediately before or after the insertion point in {resolved_file_name}.",
                "Re-run grep_file to locate the current insertion point, then retry with the updated anchor_text and line.",
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
    """Pattern replace in the main source file; use `dry_run=True` first."""
    rejection = _controlled_source_write_rejection(project_id)
    if rejection:
        return rejection
    main_file_name = _project_main_file_name(project_id)
    file_payload = read_project_file(project_id=project_id, file_name=main_file_name)
    if isinstance(file_payload, dict) and file_payload.get("error"):
        return file_payload
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
        "file_name": main_file_name,
        "dry_run": bool(dry_run),
        "is_regex": bool(is_regex),
        "ignore_case": bool(ignore_case),
        "max_replacements": int(max_replacements),
        "match_count": analysis["match_count"],
        "replacement_count": analysis["replacement_count"],
        "preview": analysis["preview"],
        "preview_truncated": analysis["preview_truncated"],
    }
    controlled = _project_controlled_mode_enabled(project_id)
    first_match_line = analysis["preview"][0]["line"] if analysis["preview"] else None
    if controlled and analysis["match_count"] != 1:
        return _rejection(
            "AMBIGUOUS_MATCH" if analysis["match_count"] > 1 else "NO_MATCH",
            f"Pattern appears {analysis['match_count']} times in {main_file_name}. Exact-once match required.",
            "Call grep_file to locate the specific occurrence, then use patch_file_lines with anchor_before and anchor_after.",
            match_count=analysis["match_count"],
            first_match_line=first_match_line,
        )

    if dry_run:
        if controlled:
            REPLACE_DRY_RUN_STATE[_replace_preview_key(project_id, main_file_name, pattern, replacement, bool(is_regex), bool(ignore_case), int(max_replacements))] = analysis["updated_content"]
        response["detail"] = "Dry run only. Re-run with dry_run=False to apply changes."
        return response

    summary = _require_summary(change_summary)
    if analysis["replacement_count"] == 0:
        response["detail"] = "No replacements applied (0 matches)."
        return response
    if controlled:
        dry_run_key = _replace_preview_key(project_id, main_file_name, pattern, replacement, bool(is_regex), bool(ignore_case), int(max_replacements))
        preview_content = REPLACE_DRY_RUN_STATE.get(dry_run_key)
        if preview_content != analysis["updated_content"]:
            return _rejection(
                "DRY_RUN_REQUIRED",
                "replace_in_project_file requires a matching dry_run=True preview before applying changes in controlled MCP mode.",
                "Run replace_in_project_file with dry_run=True, verify the preview, then repeat the exact request with dry_run=False.",
            )
        diff_text, lines_added, lines_removed = _compute_diff_stats(content, analysis["updated_content"])
        patch_rejection = _ensure_patch_size_allowed(lines_added, lines_removed)
        if patch_rejection:
            return patch_rejection

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
    if controlled:
        response["diff_text"] = diff_text
        response["lines_added"] = lines_added
        response["lines_removed"] = lines_removed
        REPLACE_DRY_RUN_STATE.pop(dry_run_key, None)
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
        filename: str | None = None,
        include_main: bool = True,
        include_assets: bool = True,
        compact: bool = True,
        include_line_text: bool = False,
        max_matches_in_response: int = 50,
) -> dict[str, Any]:
    """Search text across project files (main source + optional assets)."""
    capped_max_results = min(int(max_results), MCP_MAX_SEARCH_RESULTS) if _project_controlled_mode_enabled(project_id) else int(max_results)
    params = urlencode(
        {
            "query": query,
            "is_regex": str(bool(is_regex)).lower(),
            "ignore_case": str(bool(ignore_case)).lower(),
            "max_results": capped_max_results,
            "include_main": str(bool(include_main)).lower(),
            "include_assets": str(bool(include_assets)).lower(),
        }
    )
    payload = _call("GET", f"/api/projects/{project_id}/search/?{params}")
    if not isinstance(payload, dict):
        return {}
    if filename:
        matches = payload.get("matches")
        if isinstance(matches, list):
            payload["matches"] = [m for m in matches if isinstance(m, dict) and str(m.get("file_name") or "") == filename]
            payload["truncated"] = bool(payload.get("truncated")) or len(payload["matches"]) >= capped_max_results
    payload["read_budget_remaining"] = max(0, _read_budget_remaining(project_id))
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
        file_name: str | None = None,
) -> dict[str, Any]:
    """[deprecated] Use `read_project_file`."""
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
        file_name: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        start_char: int | None = None,
        end_char: int | None = None,
        change_summary: str = "",
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    """Rewrite a line/char window in a target text file (`file_name` defaults to main source)."""
    rejection = _controlled_source_write_rejection(project_id, file_name)
    if rejection:
        return rejection
    summary = _require_summary(change_summary)
    payload = {
        "file_name": file_name or _project_main_file_name(project_id),
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
async def patch_file_lines(
        project_id: int,
        filename: str,
        start_line: int,
        end_line: int,
        new_content: str,
        change_summary: str,
        ctx: Context,
        anchor_before: str | None = None,
        anchor_after: str | None = None,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    """Replace an inclusive line range with optional anchor verification."""
    rejection = _controlled_source_write_rejection(project_id, filename)
    if rejection:
        return rejection
    summary = _require_summary(change_summary)
    info = _file_line_info(project_id, filename)
    if not info["is_text"]:
        return _rejection(
            "NOT_TEXT_FILE",
            f"{info['file_name']} is not a text file.",
            "Choose a text file or use a file upload tool for binary assets.",
        )
    safe_start = int(start_line)
    safe_end = int(end_line)
    if safe_start < 1 or safe_end < safe_start:
        raise ValueError("line range is invalid")
    total_lines = max(1, int(info["line_count"]))
    if safe_end > total_lines:
        raise ValueError("end_line out of bounds")
    if anchor_before is not None:
        if safe_start == 1:
            return _rejection(
                "ANCHOR_MISMATCH",
                "anchor_before cannot be validated above line 1.",
                "Remove anchor_before for a top-of-file patch or re-run grep_file to confirm the target range.",
                searched_line=0,
            )
        before_line_payload = _read_file_lines_raw(project_id, info["file_name"], safe_start - 1, safe_start - 1)
        before_line = str(before_line_payload.get("content") or "").splitlines()[0] if str(before_line_payload.get("content") or "").splitlines() else ""
        if _normalize_line_text(before_line) != _normalize_line_text(anchor_before):
            return _rejection(
                "ANCHOR_MISMATCH",
                f"anchor_before not found at line {safe_start - 1}. The file may have changed.",
                "Re-run grep_file to find the current location of the anchor, then retry with the updated line number.",
                searched_line=safe_start - 1,
            )
    if anchor_after is not None:
        if safe_end >= total_lines:
            return _rejection(
                "ANCHOR_MISMATCH",
                f"anchor_after cannot be validated below line {total_lines}.",
                "Remove anchor_after for an end-of-file patch or re-run grep_file to confirm the target range.",
                searched_line=total_lines + 1,
            )
        after_line_payload = _read_file_lines_raw(project_id, info["file_name"], safe_end + 1, safe_end + 1)
        after_line = str(after_line_payload.get("content") or "").splitlines()[0] if str(after_line_payload.get("content") or "").splitlines() else ""
        if _normalize_line_text(after_line) != _normalize_line_text(anchor_after):
            return _rejection(
                "ANCHOR_MISMATCH",
                f"anchor_after not found at line {safe_end + 1}. The file may have changed.",
                "Re-run grep_file to find the current location of the anchor, then retry with the updated line number.",
                searched_line=safe_end + 1,
            )
    before_payload = _read_file_lines_raw(project_id, info["file_name"], safe_start, safe_end)
    before_content = str(before_payload.get("content") or "")
    diff_text, lines_added, lines_removed = _compute_diff_stats(before_content, new_content)
    patch_rejection = _ensure_patch_size_allowed(lines_added, lines_removed)
    if patch_rejection:
        return patch_rejection
    result = _call(
        "POST",
        f"/api/projects/{project_id}/write-window/",
        {
            "file_name": info["file_name"],
            "replacement": new_content,
            "start_line": safe_start,
            "end_line": safe_end,
            "change_summary": summary,
            "change_source": "mcp",
        },
    )
    shaped = _with_optional_compile(
        project_id,
        {
            **(result if isinstance(result, dict) else {}),
            "diff_text": diff_text,
            "lines_added": lines_added,
            "lines_removed": lines_removed,
            "version_number": _latest_version_number(project_id, info["file_name"]),
        },
        compileAlso=compileAlso,
        compileLogCompact=compileLogCompact,
        compileMaxLogChars=compileMaxLogChars,
    )
    await _notify_project_write_updates(ctx, project_id, include_compile_log=bool(compileAlso))
    return shaped


@mcp.tool
async def append_to_file(
        project_id: int,
        filename: str,
        content: str,
        change_summary: str,
        ctx: Context,
        anchor_section: str | None = None,
        compileAlso: bool = False,
        compileLogCompact: bool = True,
        compileMaxLogChars: int = 4000,
) -> dict[str, Any]:
    """Append content at EOF or immediately after a named section."""
    rejection = _controlled_source_write_rejection(project_id, filename)
    if rejection:
        return rejection
    summary = _require_summary(change_summary)
    info = _file_line_info(project_id, filename)
    if not info["is_text"]:
        return _rejection(
            "NOT_TEXT_FILE",
            f"{info['file_name']} is not a text file.",
            "Choose a text file or use a file upload tool for binary assets.",
        )
    insertion_char = int(info["total_chars"])
    appended_at_line = int(info["line_count"])
    if anchor_section:
        sections_payload = _call("GET", f"/api/projects/{project_id}/sections/")
        sections = sections_payload.get("sections") if isinstance(sections_payload, dict) else []
        matches = [
            s for s in sections
            if isinstance(s, dict)
            and str(s.get("title") or "") == str(anchor_section)
            and str(s.get("file_name") or _project_main_file_name(project_id)) == info["file_name"]
        ]
        if not matches:
            return _rejection(
                "SECTION_NOT_FOUND",
                f"Section '{anchor_section}' was not found in {info['file_name']}.",
                "Call list_project_sections or find_project_section_by_title, then retry with the exact section title.",
            )
        target_section = matches[-1]
        section_index = int(target_section.get("index"))
        section_payload = _call("GET", f"/api/projects/{project_id}/sections/{section_index}/")
        insertion_char = int(section_payload.get("end_char") or insertion_char)
        appended_at_line = int(section_payload.get("end_line") or appended_at_line)
    _, lines_added, lines_removed = _compute_diff_stats("", content)
    patch_rejection = _ensure_patch_size_allowed(lines_added, lines_removed)
    if patch_rejection:
        return patch_rejection
    result = _call(
        "POST",
        f"/api/projects/{project_id}/write-window/",
        {
            "file_name": info["file_name"],
            "replacement": content,
            "start_char": insertion_char,
            "end_char": insertion_char,
            "change_summary": summary,
            "change_source": "mcp",
        },
    )
    shaped = _with_optional_compile(
        project_id,
        {
            **(result if isinstance(result, dict) else {}),
            "appended_at_line": appended_at_line,
            "version_number": _latest_version_number(project_id, info["file_name"]),
            "lines_added": lines_added,
            "lines_removed": lines_removed,
        },
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
        file_name: str | None = None,
        column: int = 1,
) -> dict[str, Any]:
    """Map source line/column to PDF coordinates (LaTeX only; Typst is unsupported)."""
    params = urlencode(
        {
            "line": int(line),
            "column": int(column),
            "file_name": file_name or _project_main_file_name(project_id),
        }
    )
    return _call("GET", f"/api/projects/{project_id}/synctex/line/?{params}")


@mcp.tool
def synctex_page_to_line(project_id: int, page: int, x: float, y: float) -> dict[str, Any]:
    """Map PDF coordinates back to source position (LaTeX only; Typst is unsupported)."""
    params = urlencode({"page": int(page), "x": float(x), "y": float(y)})
    return _call("GET", f"/api/projects/{project_id}/synctex/pdf/?{params}")


@mcp.tool
def list_project_versions(
        project_id: int,
        compact: bool = True,
        limit: int = 20,
        file_filter: str | None = None,
) -> dict[str, Any]:
    """List version history for a project. Use `file_filter` to restrict to a specific file."""
    safe_limit = max(1, min(int(limit), 100))
    params: dict[str, Any] = {"limit": safe_limit}
    if file_filter:
        params["file"] = file_filter
    payload = _call("GET", f"/api/projects/{project_id}/versions/?{urlencode(params)}")
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
                "target_file": item.get("target_file"),
                "is_revertible": item.get("is_revertible"),
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


@mcp.tool
async def import_project_zip(
        project_id: int,
        zip_base64: str,
        ctx: Context,
) -> dict[str, Any]:
    """Import a user-supplied ZIP archive into a Typst project.

    Intended ONLY for archives the user actually brought (existing Typst project,
    asset bundle, etc.). DO NOT use this to batch-create files you generated
    yourself as a way to avoid per-file write rejections — create files one by
    one with `create_project_text_file`, or for source files in controlled mode
    submit a single `propose_document_change` with multiple `create_new_file`
    patch ops. The import bypasses the proposal/review flow and rewrites files
    directly even in controlled MCP mode.

    Pass the ZIP file contents as a base64-encoded string in `zip_base64`.
    Returns the list of files that were created or updated.

    DISABLED in controlled MCP mode — returns USE_PROPOSAL_WORKFLOW. Under
    controlled mode, all source-file additions must go through
    `propose_document_change`; if a user genuinely needs to import an external
    archive, they should do it via the project UI or temporarily disable
    controlled mode.
    """
    if _project_controlled_mode_enabled(project_id):
        return _rejection(
            "USE_PROPOSAL_WORKFLOW",
            "ZIP import to the main branch is disabled in controlled MCP mode.",
            "Submit a propose_document_change with create_new_file ops for each file, or import the archive via the project UI.",
        )
    import base64
    try:
        zip_bytes = base64.b64decode(zip_base64)
    except Exception as exc:
        raise ValueError(f"zip_base64 is not valid base64: {exc}") from exc
    result = _call_upload(f"/api/projects/{project_id}/typst-import/", zip_bytes, filename="import.zip")
    await _notify_project_write_updates(ctx, project_id)
    return result if isinstance(result, dict) else {"files": result}


@mcp.tool
def get_longdoc_overview(project_id: int) -> dict[str, Any]:
    """Read the compact Writing Assistant overview for a project.

    The response includes active_session (current AI session or null),
    limits (MCP budget/size limits), read_budget_remaining (lines left),
    and resources (MCP resource URIs for this project).
    """
    payload = _call_allow_json_errors("GET", f"/api/projects/{project_id}/longdoc/overview/")
    if isinstance(payload, dict) and not payload.get("error"):
        payload.setdefault("read_budget_remaining", max(0, _read_budget_remaining(project_id)))
        payload.setdefault("limits", {
            "max_read_lines": MCP_MAX_READ_LINES,
            "max_full_read_bytes": MCP_MAX_FULL_READ_BYTES,
            "max_patch_lines": MCP_MAX_PATCH_LINES,
            "max_grep_matches": MCP_MAX_GREP_MATCHES,
            "session_read_budget": MCP_SESSION_READ_BUDGET,
            "max_session_files": MCP_MAX_SESSION_FILES,
            "max_proposal_lines": MCP_MAX_PROPOSAL_LINES,
            "max_new_file_lines": MCP_MAX_NEW_FILE_LINES,
        })
        payload.setdefault("resources", [
            _resource_uri(project_id, name)
            for name in ("overview", "context", "outline", "tasks", "notes", "summaries", "requirements", "change-proposal")
        ])
    return payload


@mcp.tool
def get_project_overview(project_id: int) -> dict[str, Any]:
    """Return project Writing Assistant context and active suggested-change status.

    This is the proposal-oriented overview. It does not expose session branches,
    worktrees, or staging filesystem paths.
    """
    return get_longdoc_overview(project_id)


@mcp.tool
def inspect_document_graph(project_id: int) -> dict[str, Any]:
    """Inspect the compiled document graph: main file, reachable sources, missing includes, and orphan sources."""
    return _call_allow_json_errors("GET", f"/api/projects/{project_id}/document-graph/")


@mcp.tool
def find_edit_targets(
        project_id: int,
        query: str,
        filename: str | None = None,
        max_results: int = 8,
) -> dict[str, Any]:
    """Find candidate files and line ranges for a requested edit target."""
    if not query.strip():
        raise ValueError("query is required")
    results: list[dict[str, Any]] = []
    sections = list_project_sections(project_id=project_id, compact=True)
    for section in sections.get("sections", []) if isinstance(sections, dict) else []:
        if not isinstance(section, dict):
            continue
        title = str(section.get("title") or "")
        file_name = str(section.get("file_name") or _project_main_file_name(project_id))
        if filename and file_name != filename:
            continue
        if query.lower() in title.lower():
            results.append(
                {
                    "kind": "section",
                    "filename": file_name,
                    "title": title,
                    "start_line": section.get("start_line"),
                    "end_line": section.get("end_line"),
                    "confidence": "high",
                }
            )
    if len(results) < max_results:
        search = search_project_content(
            project_id=project_id,
            query=query,
            filename=filename,
            max_results=max(1, int(max_results) - len(results)),
        )
        for match in search.get("matches", []) if isinstance(search, dict) else []:
            if isinstance(match, dict):
                results.append(
                    {
                        "kind": "text_match",
                        "filename": match.get("file_name") or match.get("filename"),
                        "line": match.get("line") or match.get("line_number"),
                        "preview": match.get("preview") or match.get("line_text"),
                        "confidence": "medium",
                    }
                )
    return {"query": query, "targets": results[: max(1, int(max_results))]}


@mcp.tool
async def propose_document_change(
        project_id: int,
        goal: str,
        patch_ops: list[dict[str, Any]],
        ctx: Context,
        addresses_task_id: int | None = None,
        addresses_outline_item_id: int | None = None,
) -> dict[str, Any]:
    """Submit a suggested document change for user review.

    THIS IS THE CANONICAL WAY TO MODIFY SOURCE FILES IN CONTROLLED MCP MODE.
    If `update_project_file`, `create_project_text_file`, `patch_file_lines`,
    `replace_in_project_file`, `rewrite_project_window`, `append_to_file`, or
    similar direct-write tools return `USE_PROPOSAL_WORKFLOW`, route the same
    change through this tool instead — do not look for another tool that
    happens to bypass the rejection.

    Supports multi-file changes in a single proposal: pass several patch ops
    (`create_new_file`, `update_section`, `replace_text`, `patch_lines`, ...)
    in one call rather than issuing many small proposals or, worse, packing
    files into `import_project_zip`.

    SmartTeX creates the hidden staging session, applies patches, validates the
    document graph, compiles, creates the review diff, and returns proposal
    status. Do not call legacy AI-session tools for normal writing changes.
    """
    payload: dict[str, Any] = {
        "goal": goal,
        "patch_ops": list(patch_ops or []),
    }
    if addresses_task_id is not None:
        payload["addresses_task_id"] = int(addresses_task_id)
    if addresses_outline_item_id is not None:
        payload["addresses_outline_item_id"] = int(addresses_outline_item_id)
    result = _call_allow_json_errors("POST", f"/api/projects/{project_id}/change-proposals/", payload)
    if isinstance(result, dict) and not result.get("error"):
        await _notify_longdoc_updates(ctx, project_id, "overview", "change-proposal")
    return result


@mcp.tool
def get_change_proposal_status(project_id: int) -> dict[str, Any]:
    """Return the active suggested-change status without exposing session internals."""
    return _call_allow_json_errors("GET", f"/api/projects/{project_id}/change-proposals/status/")


@mcp.tool
async def cancel_change_proposal(project_id: int, ctx: Context) -> dict[str, Any]:
    """Cancel a draft or failed suggested change.

    Ready-for-review proposals are user-owned and must be accepted or discarded in the web UI.
    """
    result = _call_allow_json_errors("POST", f"/api/projects/{project_id}/change-proposals/cancel/")
    if isinstance(result, dict) and not result.get("error"):
        await _notify_longdoc_updates(ctx, project_id, "overview", "change-proposal")
    return result


@mcp.tool
def preview_patch(project_id: int, patch_op: dict[str, Any]) -> dict[str, Any]:
    """Dry-run one proposal patch operation against the live project and return a unified diff preview."""
    return _call_allow_json_errors("POST", f"/api/projects/{project_id}/preview-patch/", patch_op)


@mcp.tool
def list_context_files(project_id: int) -> dict[str, Any]:
    """List long-document context files with metadata only."""
    payload = _call_allow_json_errors("GET", f"/api/projects/{project_id}/context-files/")
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    files = payload.get("context_files") if isinstance(payload, dict) else []
    return {"context_files": files if isinstance(files, list) else []}


@mcp.tool
def read_context_file(project_id: int, filename: str) -> dict[str, Any]:
    """Read one long-document context file.

    Content is capped at MCP_MAX_FULL_READ_BYTES and counts against the read budget.
    """
    safe_name = quote(filename, safe="")
    payload = _call_allow_json_errors("GET", f"/api/projects/{project_id}/context-files/{safe_name}/")
    if isinstance(payload, dict) and not payload.get("error"):
        content = payload.get("content") or ""
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > MCP_MAX_FULL_READ_BYTES:
            payload["content"] = content.encode("utf-8")[:MCP_MAX_FULL_READ_BYTES].decode("utf-8", errors="replace")
            payload["truncated"] = True
            payload["truncated_at_bytes"] = MCP_MAX_FULL_READ_BYTES
            payload["total_bytes"] = content_bytes
        line_count = max(1, len(content.splitlines()))
        budget_rejection = _consume_read_budget(
            project_id,
            line_count,
            suggestion="Use list_context_files to see metadata without loading file contents.",
        )
        if budget_rejection and MCP_READ_BUDGET_HARD:
            return budget_rejection
    return payload


@mcp.tool
async def update_context_file(
        project_id: int,
        filename: str,
        change_summary: str,
        ctx: Context,
        content: str | None = None,
        description: str | None = None,
        display_name: str | None = None,
        create_if_missing: bool = False,
) -> dict[str, Any]:
    """Create or update a long-document context file."""
    summary = _require_summary(change_summary)
    longdoc = _project_longdoc_meta(project_id)
    if not (longdoc.get("enabled") and longdoc.get("context_enabled")):
        return _rejection(
            "FEATURE_DISABLED",
            "Context files are disabled for this project.",
            "Enable long-document mode and the Context feature before updating context files.",
        )
    if not longdoc.get("mcp_write_context"):
        return _rejection(
            "MCP_CONTEXT_WRITES_DISABLED",
            "MCP context-file writes are disabled for this project.",
            "Ask the user to enable MCP context writes or update the context file in the UI instead.",
        )
    safe_name = quote(filename, safe="")
    payload = _call_allow_json_errors(
        "PATCH",
        f"/api/projects/{project_id}/context-files/{safe_name}/",
        {
            "content": content,
            "description": description,
            "display_name": display_name,
            "create_if_missing": bool(create_if_missing),
            "change_summary": summary,
            "change_source": "mcp",
        },
    )
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    await _notify_longdoc_updates(ctx, project_id, "overview", "context")
    return payload if isinstance(payload, dict) else {"detail": "updated"}


@mcp.tool
def read_outline(project_id: int) -> dict[str, Any]:
    """Read Writing Assistant outline items."""
    payload = _call_allow_json_errors("GET", f"/api/projects/{project_id}/outline-items/")
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    items = payload.get("outline_items") if isinstance(payload, dict) else []
    return {"outline_items": items if isinstance(items, list) else []}


@mcp.tool
async def add_outline_item(
        project_id: int,
        title: str,
        change_summary: str,
        ctx: Context,
        level: int = 1,
        status: str = "missing",
        order: int | None = None,
        notes: str = "",
) -> dict[str, Any]:
    """Add one Writing Assistant outline item."""
    payload = _call_allow_json_errors(
        "POST",
        f"/api/projects/{project_id}/outline-items/",
        {
            "title": title,
            "level": int(level),
            "status": status,
            "order": order,
            "notes": notes,
            "change_summary": _require_summary(change_summary),
            "change_source": "mcp",
        },
    )
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    await _notify_longdoc_updates(ctx, project_id, "overview", "outline")
    return payload if isinstance(payload, dict) else {"detail": "created"}


@mcp.tool
async def update_outline_item(
        project_id: int,
        item_id: int,
        change_summary: str,
        ctx: Context,
        title: str | None = None,
        level: int | None = None,
        status: str | None = None,
        order: int | None = None,
        notes: str | None = None,
) -> dict[str, Any]:
    """Update one Writing Assistant outline item."""
    payload = _call_allow_json_errors(
        "PATCH",
        f"/api/projects/{project_id}/outline-items/{int(item_id)}/",
        {
            **({"title": title} if title is not None else {}),
            **({"level": int(level)} if level is not None else {}),
            **({"status": status} if status is not None else {}),
            **({"order": int(order)} if order is not None else {}),
            **({"notes": notes} if notes is not None else {}),
            "change_summary": _require_summary(change_summary),
            "change_source": "mcp",
        },
    )
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    await _notify_longdoc_updates(ctx, project_id, "overview", "outline")
    return payload if isinstance(payload, dict) else {"detail": "updated"}


@mcp.tool
def list_tasks(project_id: int) -> dict[str, Any]:
    """List Writing Assistant tasks."""
    payload = _call_allow_json_errors("GET", f"/api/projects/{project_id}/tasks/")
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    items = payload.get("tasks") if isinstance(payload, dict) else []
    return {"tasks": items if isinstance(items, list) else []}


@mcp.tool
async def add_task(project_id: int, description: str, change_summary: str, ctx: Context) -> dict[str, Any]:
    """Add one Writing Assistant task."""
    payload = _call_allow_json_errors(
        "POST",
        f"/api/projects/{project_id}/tasks/",
        {
            "description": description,
            "change_summary": _require_summary(change_summary),
            "change_source": "mcp",
        },
    )
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    await _notify_longdoc_updates(ctx, project_id, "overview", "tasks")
    return payload if isinstance(payload, dict) else {"detail": "created"}


@mcp.tool
async def complete_task(project_id: int, task_id: int, change_summary: str, ctx: Context) -> dict[str, Any]:
    """Mark one Writing Assistant task as done."""
    return await update_task_status(
        project_id=project_id,
        task_id=task_id,
        status="done",
        change_summary=change_summary,
        ctx=ctx,
    )


@mcp.tool
async def update_task_status(
        project_id: int,
        task_id: int,
        status: str,
        change_summary: str,
        ctx: Context,
        description: str | None = None,
) -> dict[str, Any]:
    """Update one Writing Assistant task status or description."""
    payload = _call_allow_json_errors(
        "PATCH",
        f"/api/projects/{project_id}/tasks/{int(task_id)}/",
        {
            "status": status,
            **({"description": description} if description is not None else {}),
            "change_summary": _require_summary(change_summary),
            "change_source": "mcp",
        },
    )
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    await _notify_longdoc_updates(ctx, project_id, "overview", "tasks")
    return payload if isinstance(payload, dict) else {"detail": "updated"}


@mcp.tool
def read_notes(project_id: int) -> dict[str, Any]:
    """Read Writing Assistant note sections."""
    payload = _call_allow_json_errors("GET", f"/api/projects/{project_id}/note-sections/")
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    items = payload.get("note_sections") if isinstance(payload, dict) else []
    return {"note_sections": items if isinstance(items, list) else []}


@mcp.tool
async def append_to_note_section(
        project_id: int,
        heading: str,
        text: str,
        change_summary: str,
        ctx: Context,
) -> dict[str, Any]:
    """Append text to an existing note section by heading."""
    notes_payload = _call_allow_json_errors("GET", f"/api/projects/{project_id}/note-sections/")
    if isinstance(notes_payload, dict) and notes_payload.get("error"):
        return notes_payload
    sections = notes_payload.get("note_sections") if isinstance(notes_payload, dict) else []
    match = next((item for item in sections if isinstance(item, dict) and str(item.get("heading") or "") == heading), None)
    if not match:
        return _rejection(
            "NOTE_SECTION_NOT_FOUND",
            f"Note section '{heading}' was not found.",
            "Call read_notes to inspect current headings, then retry with the exact heading.",
        )
    body = str(match.get("body") or "")
    updated_body = f"{body}{text}" if body else text
    payload = _call_allow_json_errors(
        "PATCH",
        f"/api/projects/{project_id}/note-sections/{int(match['id'])}/",
        {
            "body": updated_body,
            "change_summary": _require_summary(change_summary),
            "change_source": "mcp",
        },
    )
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    await _notify_longdoc_updates(ctx, project_id, "overview", "notes")
    return payload if isinstance(payload, dict) else {"detail": "updated"}


@mcp.tool
async def replace_note_section(
        project_id: int,
        section_id: int,
        body: str,
        change_summary: str,
        ctx: Context,
        heading: str | None = None,
) -> dict[str, Any]:
    """Replace a Writing Assistant note section body."""
    payload = _call_allow_json_errors(
        "PATCH",
        f"/api/projects/{project_id}/note-sections/{int(section_id)}/",
        {
            "body": body,
            **({"heading": heading} if heading is not None else {}),
            "change_summary": _require_summary(change_summary),
            "change_source": "mcp",
        },
    )
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    await _notify_longdoc_updates(ctx, project_id, "overview", "notes")
    return payload if isinstance(payload, dict) else {"detail": "updated"}


@mcp.tool
def list_section_summaries(project_id: int) -> dict[str, Any]:
    """List Writing Assistant section summaries."""
    payload = _call_allow_json_errors("GET", f"/api/projects/{project_id}/section-summaries/")
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    items = payload.get("section_summaries") if isinstance(payload, dict) else []
    return {"section_summaries": items if isinstance(items, list) else []}


@mcp.tool
def read_section_summary(project_id: int, section_title: str) -> dict[str, Any]:
    """Read one Writing Assistant section summary by exact section title."""
    payload = _call_allow_json_errors(
        "GET",
        f"/api/projects/{project_id}/section-summaries/?{urlencode({'section_title': section_title})}",
    )
    return payload if isinstance(payload, dict) else {}


@mcp.tool
async def update_section_summary(
        project_id: int,
        section_title: str,
        summary_text: str,
        change_summary: str,
        ctx: Context,
        section_index: int | None = None,
        source_file: str | None = None,
) -> dict[str, Any]:
    """Create or update one Writing Assistant section summary."""
    payload = _call_allow_json_errors(
        "POST",
        f"/api/projects/{project_id}/section-summaries/",
        {
            "section_title": section_title,
            "summary_text": summary_text,
            **({"section_index": int(section_index)} if section_index is not None else {}),
            **({"source_file": source_file} if source_file is not None else {}),
            "change_summary": _require_summary(change_summary),
            "change_source": "mcp",
        },
    )
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    await _notify_longdoc_updates(ctx, project_id, "overview", "outline", "summaries")
    return payload if isinstance(payload, dict) else {"detail": "updated"}


@mcp.tool
def list_requirements(project_id: int) -> dict[str, Any]:
    """List Writing Assistant requirements and coverage."""
    payload = _call_allow_json_errors("GET", f"/api/projects/{project_id}/requirements/")
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    items = payload.get("requirements") if isinstance(payload, dict) else []
    return {"requirements": items if isinstance(items, list) else []}


@mcp.tool
async def add_requirement(
        project_id: int,
        req_id: str,
        description: str,
        change_summary: str,
        ctx: Context,
        coverage: str = "unchecked",
        notes: str = "",
        section_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Add one Writing Assistant requirement."""
    payload = _call_allow_json_errors(
        "POST",
        f"/api/projects/{project_id}/requirements/",
        {
            "req_id": req_id,
            "description": description,
            "coverage": coverage,
            "notes": notes,
            "section_refs": list(section_refs or []),
            "change_summary": _require_summary(change_summary),
            "change_source": "mcp",
        },
    )
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    await _notify_longdoc_updates(ctx, project_id, "overview", "requirements")
    return payload if isinstance(payload, dict) else {"detail": "created"}


@mcp.tool
async def update_requirement_coverage(
        project_id: int,
        requirement_id: int,
        coverage: str,
        change_summary: str,
        ctx: Context,
        notes: str | None = None,
        section_refs: list[str] | None = None,
        description: str | None = None,
) -> dict[str, Any]:
    """Update requirement coverage, notes, refs, or description."""
    payload = _call_allow_json_errors(
        "PATCH",
        f"/api/projects/{project_id}/requirements/{int(requirement_id)}/",
        {
            "coverage": coverage,
            **({"notes": notes} if notes is not None else {}),
            **({"section_refs": list(section_refs)} if section_refs is not None else {}),
            **({"description": description} if description is not None else {}),
            "change_summary": _require_summary(change_summary),
            "change_source": "mcp",
        },
    )
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    await _notify_longdoc_updates(ctx, project_id, "overview", "requirements")
    return payload if isinstance(payload, dict) else {"detail": "updated"}


@mcp.resource(
    "smarttex://projects/{project_id}/change-proposal",
    name="project-change-proposal",
    title="Writing Assistant Suggested Change",
    description="Current suggested-change state without internal session details.",
    mime_type="application/json",
)
def resource_project_change_proposal(project_id: int) -> dict[str, Any]:
    payload = _call_allow_json_errors("GET", f"/api/projects/{int(project_id)}/change-proposals/status/")
    return payload if isinstance(payload, dict) else {"proposal": None}


@mcp.resource(
    "smarttex://projects/{project_id}/overview",
    name="project-longdoc-overview",
    title="Writing Assistant Overview",
    description="Compact Writing Assistant state without full content dumps.",
    mime_type="application/json",
)
def resource_project_longdoc_overview(project_id: int) -> dict[str, Any]:
    payload = _call_allow_json_errors("GET", f"/api/projects/{int(project_id)}/longdoc/overview/")
    return payload if isinstance(payload, dict) else {}


@mcp.resource(
    "smarttex://projects/{project_id}/context",
    name="project-context",
    title="Writing Assistant Context Files",
    description="Context file metadata for a project without file contents.",
    mime_type="application/json",
)
def resource_project_context(project_id: int) -> dict[str, Any]:
    payload = _call_allow_json_errors("GET", f"/api/projects/{int(project_id)}/context-files/")
    if isinstance(payload, dict) and isinstance(payload.get("context_files"), list):
        return {"context_files": payload["context_files"]}
    return payload if isinstance(payload, dict) else {"context_files": []}


@mcp.resource(
    "smarttex://projects/{project_id}/outline",
    name="project-longdoc-outline",
    title="Writing Assistant Outline",
    description="All Writing Assistant outline items as structured JSON.",
    mime_type="application/json",
)
def resource_project_longdoc_outline(project_id: int) -> dict[str, Any]:
    payload = _call_allow_json_errors("GET", f"/api/projects/{int(project_id)}/outline-items/")
    if isinstance(payload, dict) and isinstance(payload.get("outline_items"), list):
        return {"outline_items": payload["outline_items"]}
    return payload if isinstance(payload, dict) else {"outline_items": []}


@mcp.resource(
    "smarttex://projects/{project_id}/tasks",
    name="project-longdoc-tasks",
    title="Writing Assistant Tasks",
    description="Writing Assistant tasks for a project.",
    mime_type="application/json",
)
def resource_project_longdoc_tasks(project_id: int) -> dict[str, Any]:
    payload = _call_allow_json_errors("GET", f"/api/projects/{int(project_id)}/tasks/")
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        return {"tasks": payload["tasks"]}
    return payload if isinstance(payload, dict) else {"tasks": []}


@mcp.resource(
    "smarttex://projects/{project_id}/notes",
    name="project-longdoc-notes",
    title="Writing Assistant Notes",
    description="Writing Assistant note sections for a project.",
    mime_type="application/json",
)
def resource_project_longdoc_notes(project_id: int) -> dict[str, Any]:
    payload = _call_allow_json_errors("GET", f"/api/projects/{int(project_id)}/note-sections/?compact=true")
    if isinstance(payload, dict) and isinstance(payload.get("note_sections"), list):
        return {"note_sections": payload["note_sections"]}
    return payload if isinstance(payload, dict) else {"note_sections": []}


@mcp.resource(
    "smarttex://projects/{project_id}/summaries",
    name="project-longdoc-summaries",
    title="Writing Assistant Section Summaries",
    description="All structured section summaries with staleness state.",
    mime_type="application/json",
)
def resource_project_longdoc_summaries(project_id: int) -> dict[str, Any]:
    payload = _call_allow_json_errors("GET", f"/api/projects/{int(project_id)}/section-summaries/")
    if isinstance(payload, dict) and isinstance(payload.get("section_summaries"), list):
        return {"section_summaries": payload["section_summaries"]}
    return payload if isinstance(payload, dict) else {"section_summaries": []}


@mcp.resource(
    "smarttex://projects/{project_id}/requirements",
    name="project-longdoc-requirements",
    title="Writing Assistant Requirements",
    description="All requirements and their coverage state for a project.",
    mime_type="application/json",
)
def resource_project_longdoc_requirements(project_id: int) -> dict[str, Any]:
    payload = _call_allow_json_errors("GET", f"/api/projects/{int(project_id)}/requirements/")
    if isinstance(payload, dict) and isinstance(payload.get("requirements"), list):
        return {"requirements": payload["requirements"]}
    return payload if isinstance(payload, dict) else {"requirements": []}


@mcp.resource(
    "smarttex://projects/{project_id}/sections",
    name="project-sections",
    title="Project Sections",
    description="Compact list of document sections for a project.",
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
    description="Metadata for the main source file including size counters and project files.",
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
