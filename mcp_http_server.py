from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Avoid local Django app package named `mcp` shadowing MCP SDK package.
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if p not in ("", str(PROJECT_ROOT))]

from fastmcp import FastMCP  # noqa: E402
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
    instructions=(
        "Use SmartTeX API tools to list projects/templates, manage TeX and project files, "
        "work with section-level content, run compilation, and fetch compile logs."
    ),
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
def list_projects() -> list[dict[str, Any]]:
    return _call("GET", "/api/projects/")


@mcp.tool
def find_project(project_id: int | None = None, name_query: str | None = None) -> list[dict[str, Any]]:
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
def get_project_file(project_id: int) -> dict[str, Any]:
    return _call("GET", f"/api/projects/{project_id}/file/")


@mcp.tool
def update_project_file(project_id: int, content: str, change_summary: str) -> dict[str, Any]:
    summary = _require_summary(change_summary)
    return _call(
        "PUT",
        f"/api/projects/{project_id}/file/",
        {"content": content, "change_summary": summary, "change_source": "mcp"},
    )


@mcp.tool
def list_project_files(project_id: int) -> dict[str, Any]:
    return _call("GET", f"/api/projects/{project_id}/files/")


@mcp.tool
def upload_project_file(project_id: int, filename: str, content_base64: str, change_summary: str) -> dict[str, Any]:
    summary = _require_summary(change_summary)
    return _call(
        "POST",
        f"/api/projects/{project_id}/files/",
        {
            "filename": filename,
            "content_base64": content_base64,
            "change_summary": summary,
            "change_source": "mcp",
        },
    )


@mcp.tool
def list_project_sections(project_id: int) -> dict[str, Any]:
    return _call("GET", f"/api/projects/{project_id}/sections/")


@mcp.tool
def get_project_section(project_id: int, section_index: int) -> dict[str, Any]:
    return _call("GET", f"/api/projects/{project_id}/sections/{section_index}/")


@mcp.tool
def update_project_section(project_id: int, section_index: int, content: str, change_summary: str) -> dict[str, Any]:
    summary = _require_summary(change_summary)
    return _call(
        "PUT",
        f"/api/projects/{project_id}/sections/{section_index}/",
        {"content": content, "change_summary": summary, "change_source": "mcp"},
    )


@mcp.tool
def insert_text_at_position(project_id: int, position: int, text: str, change_summary: str) -> dict[str, Any]:
    summary = _require_summary(change_summary)
    return _call(
        "POST",
        f"/api/projects/{project_id}/insert/",
        {"position": position, "text": text, "change_summary": summary, "change_source": "mcp"},
    )


@mcp.tool
def search_project_content(
    project_id: int,
    query: str,
    is_regex: bool = False,
    ignore_case: bool = True,
    max_results: int = 200,
    include_main: bool = True,
    include_assets: bool = True,
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
    return _call("GET", f"/api/projects/{project_id}/search/?{params}")


@mcp.tool
def read_project_window(
    project_id: int,
    start_line: int | None = None,
    end_line: int | None = None,
    start_char: int | None = None,
    end_char: int | None = None,
    file_name: str = "main.tex",
) -> dict[str, Any]:
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
def list_project_versions(project_id: int) -> dict[str, Any]:
    return _call("GET", f"/api/projects/{project_id}/versions/")


@mcp.tool
def get_project_version_diff(project_id: int, version_id: int) -> dict[str, Any]:
    return _call("GET", f"/api/projects/{project_id}/versions/{version_id}/")


@mcp.tool
def rollback_project_version(project_id: int, version_id: int, summary: str) -> dict[str, Any]:
    rollback_summary = _require_summary(summary)
    return _call(
        "POST",
        f"/api/projects/{project_id}/versions/{version_id}/rollback/",
        {"summary": rollback_summary, "change_source": "mcp"},
    )


@mcp.tool
def compile_project(project_id: int, compact_log: bool = True, max_log_chars: int = 4000) -> dict[str, Any]:
    payload = _call("POST", f"/api/projects/{project_id}/compile/")
    return _enrich_compile_payload(
        project_id,
        payload,
        compact_log=bool(compact_log),
        max_log_chars=max(500, min(int(max_log_chars), 20000)),
    )


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
    return _call("GET", "/api/templates/")


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
