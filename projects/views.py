import json
import base64
import binascii
import mimetypes
import posixpath
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, HttpRequest, HttpResponse, HttpResponseForbidden, HttpResponseNotFound, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods
from asgiref.sync import async_to_sync

from accounts.auth_helpers import get_api_user
from SmartTeX.markup import MarkupType, source_filename_for_markup
from longdoc.locks import get_locking_change_proposal, get_locking_session
from longdoc.services import get_longdoc_settings_or_none, initialize_longdoc_from_template
from small_model.models import ProjectSmallModelSettings, UserSmallModelAccess
from small_model.services.quota_service import SmallModelQuotaService
from templates_lib.models import Template
from templates_lib.services import normalize_template_main_file

from .models import GitHubInstallation, Project, ProjectVersion
from .services import (
    ALLOWED_UPLOAD_EXTENSIONS,
    build_project_zip,
    build_typst_citation_index,
    build_version_diff,
    cancel_github_sync,
    schedule_github_sync,
    compile_state_for_status,
    commit_project_changes,
    commit_project_text_changes,
    compile_project,
    create_project_directory,
    create_project_text_file,
    create_project_version,
    create_text_project_version,
    ensure_project_dir,
    delete_project_asset,
    delete_project_files,
    get_project_version,
    get_project_pdf_page_count,
    list_project_versions,
    has_pdf,
    initialize_main_source,
    is_source_too_large,
    list_project_assets,
    list_source_sections,
    main_source_filename,
    parse_compile_diagnostics,
    pdf_file_path,
    project_pdf_download_name,
    pdf_relative_url,
    pdf_version,
    project_asset_path,
    project_dir,
    push_to_github,
    read_compile_log,
    read_project_asset_content,
    write_project_asset_text,
    read_source_content,
    read_project_window,
    rename_project_asset,
    render_pdf_page_image,
    synctex_line_to_pdf,
    synctex_pdf_to_line,
    write_project_window,
    rollback_to_version,
    save_project_asset,
    search_project_content,
    get_source_section,
    insert_text_at_position,
    update_source_section,
    write_source_content,
    extract_project_zip,
    IMAGE_EXTENSIONS,
    TEXT_EXTENSIONS,
)
from .plantuml_job import render_plantuml_svg, _sha256, _load_hashes, _save_hashes

DEFAULT_LATEX = r"""\\documentclass{article}
\\usepackage[ukrainian]{babel}
\\usepackage{fontspec}
\\setmainfont{Times New Roman}
\\begin{document}
Hello, SmartTeX!
\\end{document}
"""

DEFAULT_TYPST = """= SmartTeX

Hello, SmartTeX!
"""

_TINYMIST_PREVIEW_BRIDGE = """
<script>
(() => {
  if (window.__smarttexPreviewBridgeInstalled) return;
  window.__smarttexPreviewBridgeInstalled = true;
  const PREVIEW_PROJECT_ID = __SMARTTEX_PREVIEW_PROJECT_ID__;

  const STYLE_ID = "smarttex-preview-sync-style";
  const HIGHLIGHT_CLASS = "smarttex-preview-sync-highlight";

  function debugEnabled() {
    try {
      return window.localStorage.getItem("smarttex.preview.debug") === "1";
    } catch (_) {
      return false;
    }
  }

  function debug(...args) {
    if (debugEnabled()) console.log("[smarttex-preview-bridge]", ...args);
  }

  const NativeWebSocket = window.WebSocket;
  window.WebSocket = function(url, protocols) {
    try {
      const resolved = new URL(String(url), window.location.href);
      if (resolved.pathname === "/" || resolved.pathname === "") {
        resolved.pathname = "/ws/typst-preview/";
      }
      if (resolved.pathname === "/ws/typst-preview/" && !resolved.searchParams.has("preview_project")) {
        resolved.searchParams.set("preview_project", String(PREVIEW_PROJECT_ID));
        const previewTheme = new URL(window.location.href).searchParams.get("theme");
        if (previewTheme && !resolved.searchParams.has("preview_theme")) {
          resolved.searchParams.set("preview_theme", previewTheme);
        }
        url = resolved.toString();
      }
      debug("patched websocket url", url);
    } catch (_) {}
    return protocols !== undefined ? new NativeWebSocket(url, protocols) : new NativeWebSocket(url);
  };
  window.WebSocket.prototype = NativeWebSocket.prototype;
  Object.setPrototypeOf(window.WebSocket, NativeWebSocket);

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
      .${HIGHLIGHT_CLASS} {
        outline: 2px solid rgba(59,130,246,.9) !important;
        outline-offset: 4px !important;
        border-radius: 6px !important;
        transition: outline-color .18s ease;
      }
    `;
    document.head.appendChild(style);
  }

  function normalize(text) {
    return String(text || "")
      .replace(/\\s+/g, " ")
      .replace(/[“”«»"]/g, '"')
      .replace(/[’']/g, "'")
      .trim()
      .toLowerCase();
  }

  function findTextElement(targets) {
    const wanted = targets
      .map(item => ({ ...item, norm: normalize(item.value) }))
      .filter(item => item.norm.length >= 3);
    if (!wanted.length) return null;

    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const text = normalize(node.nodeValue || "");
        return text.length >= 3 ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });

    let best = null;
    let node;
    while ((node = walker.nextNode())) {
      const hay = normalize(node.nodeValue || "");
      let score = 0;
      for (const target of wanted) {
        if (hay === target.norm) score = Math.max(score, target.weight + 60);
        else if (hay.includes(target.norm)) score = Math.max(score, target.weight + 25);
        else if (target.norm.includes(hay) && hay.length >= 8) score = Math.max(score, target.weight + 10);
      }
      if (!score) continue;
      const el = node.parentElement?.closest("svg text, h1, h2, h3, h4, h5, h6, p, div, span, li, td, th");
      if (!el) continue;
      if (!best || score > best.score) best = { el, score };
    }
    return best?.el || null;
  }

  let highlightTimer = null;
  function revealElement(el) {
    if (!el) return false;
    ensureStyle();
    el.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
    document.querySelectorAll("." + HIGHLIGHT_CLASS).forEach(node => node.classList.remove(HIGHLIGHT_CLASS));
    el.classList.add(HIGHLIGHT_CLASS);
    clearTimeout(highlightTimer);
    highlightTimer = setTimeout(() => el.classList.remove(HIGHLIGHT_CLASS), 1600);
    return true;
  }

  function bestClickableText(event) {
    const path = event.composedPath ? event.composedPath() : [];
    for (const item of path) {
      if (!(item instanceof Element)) continue;
      const text = normalize(item.innerText || item.textContent || "");
      if (text.length >= 3) return text.slice(0, 220);
    }
    return "";
  }

  function isVisualOnlyTarget(event) {
    const path = event.composedPath ? event.composedPath() : [];
    for (const item of path) {
      if (!(item instanceof Element)) continue;
      const tag = String(item.tagName || "").toLowerCase();
      if (["img", "image", "canvas", "svg", "path", "rect", "g", "figure", "foreignobject"].includes(tag)) return true;
      if (item.closest?.("img, image, canvas, path, rect, g, figure, foreignObject")) return true;
      if (item.classList?.contains("typst-page-inner")) return true;
    }
    return false;
  }

  function isTextNavigationTarget(event) {
    const path = event.composedPath ? event.composedPath() : [];
    for (const item of path) {
      if (!(item instanceof Element)) continue;
      const tag = String(item.tagName || "").toLowerCase();
      if (["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "span", "code", "text"].includes(tag)) return true;
      if (tag === "div") {
        const text = normalize(item.textContent || "");
        if (text.length >= 3 && text.length <= 240) return true;
      }
    }
    return false;
  }

  function parseSourceLocation(value) {
    const raw = String(value || "").trim();
    if (!raw) return null;

    const direct = raw.match(/([^\\s]+\\.typ):(\\d+)(?::(\\d+))?/);
    if (direct) {
      return {
        filename: direct[1].replace(/^file:\\/\\//, ""),
        line: Number(direct[2] || 1),
        column: Number(direct[3] || 1),
      };
    }

    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        const filename = parsed.filename || parsed.file || parsed.path || parsed.uri;
        const line = Number(parsed.line || parsed.lineNumber || parsed.row || 1);
        const column = Number(parsed.column || parsed.character || parsed.col || 1);
        if (filename && Number.isFinite(line)) {
          return { filename: String(filename).replace(/^file:\\/\\//, ""), line, column: Number.isFinite(column) ? column : 1 };
        }
      }
    } catch (_) {}

    try {
      const url = new URL(raw, window.location.origin);
      const filename = url.searchParams.get("file") || url.searchParams.get("filename") || url.searchParams.get("path") || url.searchParams.get("uri");
      const line = Number(url.searchParams.get("line") || url.searchParams.get("row") || 1);
      const column = Number(url.searchParams.get("column") || url.searchParams.get("character") || url.searchParams.get("col") || 1);
      if (filename && Number.isFinite(line)) {
        return { filename: String(filename).replace(/^file:\\/\\//, ""), line, column: Number.isFinite(column) ? column : 1 };
      }
    } catch (_) {}

    return null;
  }

  function extractSourceLocation(event) {
    const path = event.composedPath ? event.composedPath() : [];
    for (const item of path) {
      if (!(item instanceof Element)) continue;
      const attrs = item.getAttributeNames ? item.getAttributeNames() : [];
      for (const name of attrs) {
        const loc = parseSourceLocation(item.getAttribute(name));
        if (loc) return loc;
      }
      for (const value of Object.values(item.dataset || {})) {
        const loc = parseSourceLocation(value);
        if (loc) return loc;
      }
    }
    return null;
  }

  function nearestHeadingText(event) {
    const path = event.composedPath ? event.composedPath() : [];
    for (const item of path) {
      if (!(item instanceof Element)) continue;
      let current = item;
      while (current && current !== document.body) {
        const prevHeading = current.previousElementSibling;
        if (prevHeading && /^(H[1-6]|text)$/i.test(prevHeading.tagName || "")) {
          const text = normalize(prevHeading.textContent || "");
          if (text.length >= 3) return text.slice(0, 220);
        }
        current = current.parentElement;
      }
    }
    return "";
  }

  window.addEventListener("message", event => {
    if (event.origin !== window.location.origin) return;
    const data = event.data || {};
    if (data?.type !== "smarttex-preview-reveal") return;
    const payload = data.payload || {};
    debug("reveal request", payload);
    const el = findTextElement([
      { value: payload.heading, weight: 100 },
      { value: payload.lineText, weight: 70 },
      { value: payload.excerpt, weight: 40 },
    ]);
    debug("reveal target", el, {
      heading: payload.heading,
      lineText: payload.lineText,
      excerpt: payload.excerpt,
    });
    revealElement(el);
  });

  document.addEventListener("click", event => {
    const visualOnly = isVisualOnlyTarget(event);
    if (visualOnly || !isTextNavigationTarget(event)) {
      debug("click ignored visual target", event.target);
      return;
    }
    const location = extractSourceLocation(event);
    const text = bestClickableText(event);
    if (!location && !text) return;
    const payload = {
      text,
      heading: nearestHeadingText(event),
      location,
    };
    debug("click payload", payload, event.target);
    window.parent.postMessage({
      type: "smarttex-preview-click",
      payload
    }, window.location.origin);
  }, true);

  debug("bridge ready");
  window.parent.postMessage({ type: "smarttex-preview-ready" }, window.location.origin);
})();
</script>
"""


def _inject_typst_preview_bridge(body: bytes, project_id: int) -> bytes:
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError:
        html = body.decode("utf-8", errors="ignore")
    bridge = _TINYMIST_PREVIEW_BRIDGE.replace("__SMARTTEX_PREVIEW_PROJECT_ID__", str(int(project_id)))
    lower = html.lower()
    marker = "</body>"
    idx = lower.rfind(marker)
    if idx == -1:
        return (html + bridge).encode("utf-8")
    return (html[:idx] + bridge + html[idx:]).encode("utf-8")


def _json_body(request: HttpRequest) -> dict:
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def _unauthorized() -> JsonResponse:
    return JsonResponse({"detail": "Authentication required"}, status=401)


def _check_project_lock(project: Project) -> JsonResponse | None:
    """Return a 423 response if the project is locked by a suggested change, else None."""
    proposal = get_locking_change_proposal(project)
    session = get_locking_session(project)
    if proposal is None and session is None:
        return None
    return JsonResponse(
        {
            "error": "PROJECT_LOCKED",
            "message": "A suggested change is active. Accept or discard it before making changes.",
            "proposal_id": proposal.id if proposal else None,
        },
        status=423,
    )


def _project_payload(project: Project, user=None) -> dict:
    source_file_name = main_source_filename(project)
    longdoc_settings = get_longdoc_settings_or_none(project)
    small_model_settings = ProjectSmallModelSettings.objects.filter(project=project).first()
    locking_proposal = get_locking_change_proposal(project) if longdoc_settings and longdoc_settings.enabled else None
    locking_session = get_locking_session(project) if longdoc_settings and longdoc_settings.enabled else None
    locked = locking_proposal is not None or locking_session is not None
    small_model_access = UserSmallModelAccess.objects.filter(user=user, enabled=True).first() if user else None
    quota = SmallModelQuotaService.check_quota(user) if user and small_model_access else None
    quota_reason = quota.reason if quota else ""
    quota_warning_visible = bool(
        small_model_settings
        and small_model_settings.small_model_control_enabled
        and quota
        and not quota.quota_ok
        and quota_reason.endswith("_exceeded")
    )
    return {
        "id": project.id,
        "title": project.title,
        "template_id": project.template_id,
        "markup_type": project.markup_type,
        "main_file_name": source_file_name,
        "supports_synctex": project.markup_type == MarkupType.LATEX,
        "tinymist": {
            "lsp_enabled": bool(getattr(settings, "TINYMIST_LSP_ENABLED", True)),
            "preview_enabled": bool(getattr(settings, "TINYMIST_PREVIEW_ENABLED", True)),
            "lsp_autostart": bool(getattr(settings, "TINYMIST_LSP_AUTOSTART", True)),
            "preview_autostart": bool(getattr(settings, "TINYMIST_PREVIEW_AUTOSTART", True)),
        },
        "last_status": project.last_status,
        "longdoc": {
            "enabled": bool(longdoc_settings and longdoc_settings.enabled),
            "context_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.context_enabled),
            "outline_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.outline_enabled),
            "tasks_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.tasks_enabled),
            "annotations_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.annotations_enabled),
            "notes_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.notes_enabled),
            "summaries_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.summaries_enabled),
            "requirements_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.requirements_enabled),
            "ai_sessions_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.ai_sessions_enabled),
            "mcp_controlled_access": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.mcp_controlled_access),
            "mcp_write_context": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.mcp_write_context),
            "locked": locked,
            "locking_proposal_id": locking_proposal.id if locking_proposal else None,
            "locking_session_id": locking_session.id if locking_session else None,
            "locking_session_status": locking_session.status if locking_session else "",
        },
        "small_model": {
            "enabled": bool(small_model_settings and small_model_settings.small_model_control_enabled),
            "user_has_access": bool(small_model_access),
            "quota_ok": bool(quota.quota_ok) if quota is not None else True,
            "quota_reason": quota_reason,
            "credits_remaining": float(quota.credits_remaining) if quota is not None else 0,
            "quota_warning_visible": quota_warning_visible,
        },
        "github": {
            "sync_enabled": project.github_sync_enabled,
            "repo_url": project.github_repo_url,
            "app_connected": hasattr(project.owner, "github_installation"),
            "sync_interval_minutes": project.github_sync_interval_minutes,
        },
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


def _project_with_owner(project_id: int, user) -> Project:
    return get_object_or_404(Project, id=project_id, owner=user)


def _compile_project_after_create(project: Project) -> None:
    result = compile_project(project)
    project.last_status = result.status
    project.save(update_fields=["last_status", "updated_at"])


def _compile_payload(project: Project, *, status: str, log: str, request_mode: str) -> dict[str, object]:
    diagnostics = parse_compile_diagnostics(project, log)
    return {
        "status": status,
        "compile_state": compile_state_for_status(status, request_mode=request_mode),
        "pdf_url": pdf_relative_url(project) if has_pdf(project) else None,
        "pdf_version": pdf_version(project),
        "log": log,
        "diagnostics": diagnostics,
    }


def _change_meta(request: HttpRequest, body: dict | None = None) -> dict:
    body = body or {}
    source = (
        request.headers.get("X-Change-Source")
        or body.get("change_source")
        or "api"
    ).strip().lower()
    summary = str(
        request.headers.get("X-Change-Summary")
        or body.get("change_summary")
        or ""
    ).strip()
    if source == "mcp" and not summary:
        raise ValueError("change_summary is required for MCP edits")
    if source not in {"mcp", "web", "api"}:
        source = "api"
    return {"source": source, "summary": summary}


def _as_bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(v: str | None) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    return int(v)


def _default_content_for_markup(markup_type: str) -> str:
    if markup_type == MarkupType.TYPST:
        return DEFAULT_TYPST
    return DEFAULT_LATEX


def _default_change_summary(operation: str, target: str = "") -> str:
    label = target.strip() if target else "document"
    mapping = {
        "create_project": "Initialized project",
        "update_project_file": f"Updated {label}",
        "write_project_window": f"Edited {label}",
        "create_project_file": f"Created {label}",
        "update_project_asset": f"Updated {label}",
        "delete_project_file": f"Deleted {label}",
        "rename_project_file": f"Renamed {label}",
        "update_project_section": f"Updated section in {label}",
        "insert_text_at_position": f"Inserted text into {label}",
        "rollback": f"Rolled back {label}",
        "create_project_folder": f"Created folder {label}",
        "upload_project_file": f"Uploaded file {label}",
    }
    return mapping.get(operation, f"Updated {label}")


def _read_text_target(project: Project, file_name: str) -> str:
    target = str(file_name or main_source_filename(project)).strip() or main_source_filename(project)
    if target == main_source_filename(project):
        return read_source_content(project)
    payload = read_project_asset_content(project, target, include_text=True)
    return str(payload.get("text_content") or "")


def _normalize_markup_type(raw_value: object) -> str:
    value = str(raw_value or MarkupType.LATEX).strip().lower()
    if value not in {choice.value for choice in MarkupType}:
        raise ValueError("markup_type must be one of: latex, typst")
    return value


def _configured_mcp_url(request: HttpRequest) -> str:
    configured_base = str(getattr(settings, "MCP_SERVER_PUBLIC_URL", "") or "").strip()
    configured_path = str(getattr(settings, "MCP_PATH", "/mcp") or "/mcp").strip()

    mcp_path = configured_path or "/"
    if not mcp_path.startswith("/"):
        mcp_path = f"/{mcp_path}"
    if mcp_path != "/":
        mcp_path = mcp_path.rstrip("/")

    if configured_base:
        parts = urlsplit(configured_base)
        base_path = parts.path or "/"
        if mcp_path == "/":
            final_path = base_path
        else:
            final_path = posixpath.join(base_path.rstrip("/") or "/", mcp_path.lstrip("/"))
            if not final_path.startswith("/"):
                final_path = f"/{final_path}"
        return urlunsplit((parts.scheme, parts.netloc, final_path, "", ""))

    return request.build_absolute_uri(mcp_path)


def home(request: HttpRequest):
    if request.user.is_authenticated:
        return redirect("projects:dashboard")
    return render(request, "projects/home.html")


@require_GET
def ai_connect_guide(request: HttpRequest):
    return render(
        request,
        "projects/ai_connect_guide.html",
        {"mcp_url": _configured_mcp_url(request)},
    )


@require_GET
def ai_workflow_guide(request: HttpRequest):
    return render(request, "projects/ai_workflow_guide.html")


@login_required
@require_GET
def dashboard(request: HttpRequest):
    page_size = 24
    rows = list(
        Project.objects.filter(owner=request.user)
        .select_related("template")
        .order_by("-id")[: page_size + 1]
    )
    has_more = len(rows) > page_size
    project_items = rows[:page_size]
    next_before_id = project_items[-1].id if has_more and project_items else None
    projects_count = Project.objects.filter(owner=request.user).count()
    templates = Template.objects.filter(is_active=True)
    return render(
        request,
        "projects/dashboard.html",
        {
            "projects": project_items,
            "projects_count": projects_count,
            "projects_has_more": has_more,
            "projects_next_before_id": next_before_id,
            "templates": templates,
        },
    )


@login_required
@require_GET
def editor(request: HttpRequest, project_id: int):
    project = _project_with_owner(project_id, request.user)
    return render(request, "projects/editor.html", {
        "project": project,
        "session_review": False,
        "has_active_session": False,
        "user_is_staff": request.user.is_staff,
    })


@login_required
@require_GET
def session_review(request: HttpRequest, project_id: int):
    project = _project_with_owner(project_id, request.user)
    return render(
        request,
        "projects/editor.html",
        {
            "project": project,
            "session_review": True,
            "has_active_session": get_locking_change_proposal(project) is not None,
        },
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_projects(request: HttpRequest) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    if request.method == "GET":
        try:
            limit = _parse_int(request.GET.get("limit"))
            before_id = _parse_int(request.GET.get("before_id"))
        except ValueError:
            return JsonResponse({"detail": "limit/before_id must be integers"}, status=400)

        qs = Project.objects.filter(owner=user).select_related("template")
        if limit is None and before_id is None:
            data = [_project_payload(p, user) for p in qs]
            return JsonResponse(data, safe=False)

        safe_limit = max(1, min(int(limit or 24), 120))
        if before_id is not None:
            qs = qs.filter(id__lt=before_id)
        rows = list(qs.order_by("-id")[: safe_limit + 1])
        has_more = len(rows) > safe_limit
        items = rows[:safe_limit]
        data = [_project_payload(p, user) for p in items]
        next_before_id = items[-1].id if has_more and items else None
        return JsonResponse({"projects": data, "has_more": has_more, "next_before_id": next_before_id})

    body = _json_body(request)
    title = body.get("title", "").strip() or "Новий проєкт"
    template_id = body.get("template_id")

    template_obj = None
    try:
        markup_type = _normalize_markup_type(body.get("markup_type"))
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    content = _default_content_for_markup(markup_type)
    project_main_file = ""
    if template_id is not None:
        template_obj = get_object_or_404(Template, id=template_id, is_active=True)
        markup_type = template_obj.markup_type
        content = template_obj.content or _default_content_for_markup(markup_type)
        project_main_file = normalize_template_main_file(template_obj)

    if is_source_too_large(content):
        return JsonResponse({"detail": "Template content exceeds 1MB"}, status=400)

    with transaction.atomic():
        project = Project.objects.create(
            owner=user,
            title=title,
            template=template_obj,
            markup_type=markup_type,
            main_file=project_main_file,
        )
        initialize_main_source(project, content)
        create_text_project_version(
            project=project,
            actor=user,
            source="api",
            operation="create_project",
            target=main_source_filename(project),
            target_file=main_source_filename(project),
            summary=_default_change_summary("create_project", main_source_filename(project)),
            tracked_files=[main_source_filename(project)],
        )
        if template_obj is not None:
            initialize_longdoc_from_template(project, template_obj)
    _compile_project_after_create(project)
    return JsonResponse(_project_payload(project, user), status=201)


@csrf_exempt
@require_http_methods(["GET", "PATCH", "DELETE"])
def api_project_detail(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        data = _project_payload(project, user)
        data["template"] = project.template.title if project.template else None
        return JsonResponse(data)

    if request.method == "PATCH":
        body = _json_body(request)
        update_fields = ["updated_at"]
        title = body.get("title", "").strip()
        if title:
            project.title = title
            update_fields.append("title")
        if "main_file" in body:
            new_main = str(body["main_file"] or "").strip()
            if new_main:
                main_path = project_dir(project) / new_main
                if not main_path.exists():
                    return JsonResponse({"error": "file not found"}, status=400)
            project.main_file = new_main
            update_fields.append("main_file")
        if "github_repo_url" in body:
            project.github_repo_url = str(body["github_repo_url"] or "").strip()
            update_fields.append("github_repo_url")
        if "github_sync_enabled" in body:
            project.github_sync_enabled = bool(body["github_sync_enabled"])
            update_fields.append("github_sync_enabled")
            if not project.github_sync_enabled:
                cancel_github_sync(project)
        if "github_sync_interval_minutes" in body:
            try:
                minutes = int(body["github_sync_interval_minutes"])
            except (TypeError, ValueError):
                return JsonResponse({"detail": "github_sync_interval_minutes must be an integer"}, status=400)
            from .services import GITHUB_SYNC_INTERVAL_MIN, GITHUB_SYNC_INTERVAL_MAX
            minutes = max(GITHUB_SYNC_INTERVAL_MIN, min(minutes, GITHUB_SYNC_INTERVAL_MAX))
            project.github_sync_interval_minutes = minutes
            update_fields.append("github_sync_interval_minutes")
        project.save(update_fields=update_fields)
        if project.github_sync_enabled:
            schedule_github_sync(project)
        return JsonResponse(_project_payload(project, user))

    delete_project_files(project)
    project.delete()
    return JsonResponse({}, status=204)


@csrf_exempt
@require_http_methods(["GET", "PUT"])
def api_project_file(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        return JsonResponse({"file_name": main_source_filename(project), "content": read_source_content(project)})

    if lock_resp := _check_project_lock(project):
        return lock_resp

    body = _json_body(request)
    content = body.get("content", "")
    if not isinstance(content, str):
        return JsonResponse({"detail": "content must be a string"}, status=400)
    if is_source_too_large(content):
        return JsonResponse({"detail": "File exceeds 1MB"}, status=400)
    try:
        meta = _change_meta(request, body)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    before = read_source_content(project)
    write_source_content(project, content)
    project.last_status = Project.CompileStatus.PENDING
    project.save(update_fields=["last_status", "updated_at"])
    if before != content and meta["source"] == "mcp":
        create_text_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="update_project_file",
            target=main_source_filename(project),
            target_file=main_source_filename(project),
            summary=meta["summary"] or _default_change_summary("update_project_file", main_source_filename(project)),
        )
    return JsonResponse({"detail": "saved"})


@csrf_exempt
@require_GET
def api_project_search(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    query = (request.GET.get("query") or request.GET.get("pattern") or "").strip()
    if not query:
        return JsonResponse({"detail": "query is required"}, status=400)

    try:
        payload = search_project_content(
            project,
            query=query,
            is_regex=_as_bool(request.GET.get("is_regex"), default=False),
            ignore_case=_as_bool(request.GET.get("ignore_case"), default=True),
            max_results=int(request.GET.get("max_results", "200")),
            include_main=_as_bool(request.GET.get("include_main"), default=True),
            include_assets=_as_bool(request.GET.get("include_assets"), default=True),
        )
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    return JsonResponse(payload)


@csrf_exempt
@require_GET
def api_project_read_window(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    def _to_int(name: str) -> int | None:
        raw = request.GET.get(name)
        if raw is None or str(raw).strip() == "":
            return None
        return int(raw)

    try:
        payload = read_project_window(
            project,
            file_name=request.GET.get("file_name") or main_source_filename(project),
            start_line=_to_int("start_line"),
            end_line=_to_int("end_line"),
            start_char=_to_int("start_char"),
            end_char=_to_int("end_char"),
        )
    except (ValueError, TypeError) as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["POST"])
def api_project_write_window(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if lock_resp := _check_project_lock(project):
        return lock_resp
    body = _json_body(request)

    replacement = body.get("replacement")
    if not isinstance(replacement, str):
        return JsonResponse({"detail": "replacement must be a string"}, status=400)

    def _to_int(v) -> int | None:
        if v is None or str(v).strip() == "":
            return None
        return int(v)

    try:
        meta = _change_meta(request, body)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    target_file = str(body.get("file_name") or main_source_filename(project))
    try:
        before_text = _read_text_target(project, target_file)
        try:
            payload = write_project_window(
                project,
                file_name=target_file,
                replacement=replacement,
                start_line=_to_int(body.get("start_line")),
                end_line=_to_int(body.get("end_line")),
                start_char=_to_int(body.get("start_char")),
                end_char=_to_int(body.get("end_char")),
            )
        except (ValueError, TypeError) as exc:
            return JsonResponse({"detail": str(exc)}, status=400)

        project.last_status = Project.CompileStatus.PENDING
        project.save(update_fields=["last_status", "updated_at"])
        after_text = _read_text_target(project, target_file)
        if before_text != after_text and meta["source"] == "mcp":
            target = f"{payload.get('file_name', main_source_filename(project))}:{payload.get('mode', 'window')}"
            create_text_project_version(
                project=project,
                actor=user,
                source=meta["source"],
                operation="write_project_window",
                target=target,
                target_file=str(payload.get("file_name") or target_file),
                summary=meta["summary"] or _default_change_summary("write_project_window", str(payload.get("file_name") or target_file)),
            )
        return JsonResponse(payload)
    except Exception as exc:
        import logging, traceback
        logging.getLogger(__name__).exception("write-window failed for project %s file=%r", project_id, target_file)
        return JsonResponse(
            {
                "detail": f"{type(exc).__name__}: {exc}",
                "file_name": target_file,
                "traceback": traceback.format_exc().splitlines()[-6:],
            },
            status=500,
        )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_project_assets(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        return JsonResponse({"files": list_project_assets(project)})

    if lock_resp := _check_project_lock(project):
        return lock_resp

    # Support both multipart uploads (web UI) and JSON/base64 uploads (MCP).
    raw_body = _json_body(request) if request.content_type == "application/json" else {}
    try:
        meta = _change_meta(request, raw_body)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    def _ext_from_name(name: str) -> str:
        clean = str(name or "").strip().lower()
        return f".{clean.rsplit('.', 1)[-1]}" if "." in clean else ""

    if request.FILES.get("file"):
        upload = request.FILES["file"]
        upload_ext = _ext_from_name(getattr(upload, "name", ""))

        if upload_ext == ".zip":
            try:
                created = extract_project_zip(project, upload.read())
            except ValueError as exc:
                return JsonResponse({"detail": str(exc)}, status=400)
            project.last_status = Project.CompileStatus.PENDING
            project.save(update_fields=["last_status", "updated_at"])
            return JsonResponse({"files": created}, status=201)

        if upload_ext not in ALLOWED_UPLOAD_EXTENSIONS:
            return JsonResponse({"detail": "Unsupported file type"}, status=400)
        try:
            if upload_ext in TEXT_EXTENSIONS:
                text_content = upload.read().decode("utf-8")
                asset = create_project_text_file(project, upload.name, text_content)
            else:
                asset = save_project_asset(project, upload.name, upload.read())
        except ValueError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        project.last_status = Project.CompileStatus.PENDING
        project.save(update_fields=["last_status", "updated_at"])
        if asset.get("is_text"):
            create_text_project_version(
                project=project,
                actor=user,
                source=meta["source"],
                operation="create_project_file",
                target=asset["name"],
                target_file=asset["name"],
                summary=meta["summary"] or _default_change_summary("create_project_file", asset["name"]),
            )
        else:
            commit_info = commit_project_changes(
                project,
                summary=meta["summary"] or _default_change_summary("upload_project_file", asset["name"]),
                operation="upload_project_file",
                source=meta["source"],
                target_files=[asset["name"]],
            )
            create_project_version(
                project=project,
                actor=user,
                source=meta["source"],
                operation="upload_project_file",
                target=asset["name"],
                target_file=asset["name"],
                summary=meta["summary"] or _default_change_summary("upload_project_file", asset["name"]),
                before_content="",
                after_content="",
                snapshot_kind=ProjectVersion.SnapshotKind.EVENT,
                event_payload={"name": asset["name"], "size": asset["size"], "kind": "binary_upload", "git_commit": commit_info.commit_hash or ""},
                is_revertible=bool(commit_info.commit_hash),
            )
        return JsonResponse(asset, status=201)

    body = raw_body
    filename = str(body.get("filename", "")).strip()
    entry_kind = str(body.get("entry_kind", "")).strip().lower()
    content_base64 = body.get("content_base64")
    text_content = body.get("text_content")
    if not filename:
        return JsonResponse({"detail": "filename is required"}, status=400)
    if entry_kind == "directory":
        try:
            asset = create_project_directory(project, filename)
        except ValueError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        project.last_status = Project.CompileStatus.PENDING
        project.save(update_fields=["last_status", "updated_at"])
        create_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="create_project_folder",
            target=asset["name"],
            target_file=asset["name"],
            summary=meta["summary"] or _default_change_summary("create_project_folder", asset["name"]),
            before_content="",
            after_content="",
            snapshot_kind=ProjectVersion.SnapshotKind.EVENT,
            event_payload={"name": asset["name"], "kind": "folder_create"},
            is_revertible=False,
        )
        return JsonResponse(asset, status=201)
    if content_base64 is None and text_content is None:
        return JsonResponse({"detail": "content_base64 or text_content is required"}, status=400)
    ext = _ext_from_name(filename)
    is_image = ext in IMAGE_EXTENSIONS
    is_text = ext in TEXT_EXTENSIONS
    is_pdf = ext == ".pdf"
    if not is_image and not is_text and not is_pdf:
        return JsonResponse({"detail": "Unsupported file type"}, status=400)

    try:
        if is_text:
            if content_base64 is not None:
                decoded_text = base64.b64decode(content_base64, validate=True).decode("utf-8")
            else:
                decoded_text = str(text_content)
            asset = create_project_text_file(project, filename, decoded_text)
        else:
            if content_base64 is not None:
                payload = base64.b64decode(content_base64, validate=True)
            else:
                payload = str(text_content).encode("utf-8")
            asset = save_project_asset(project, filename, payload)
    except (ValueError, binascii.Error) as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    project.last_status = Project.CompileStatus.PENDING
    project.save(update_fields=["last_status", "updated_at"])
    if is_text:
        create_text_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="create_project_file",
            target=asset["name"],
            target_file=asset["name"],
            summary=meta["summary"] or _default_change_summary("create_project_file", asset["name"]),
        )
    else:
        commit_info = commit_project_changes(
            project,
            summary=meta["summary"] or _default_change_summary("upload_project_file", asset["name"]),
            operation="upload_project_file",
            source=meta["source"],
            target_files=[asset["name"]],
        )
        create_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="upload_project_file",
            target=asset["name"],
            target_file=asset["name"],
            summary=meta["summary"] or _default_change_summary("upload_project_file", asset["name"]),
            before_content="",
            after_content="",
            snapshot_kind=ProjectVersion.SnapshotKind.EVENT,
            event_payload={"name": asset["name"], "size": asset["size"], "kind": "binary_upload", "git_commit": commit_info.commit_hash or ""},
            is_revertible=bool(commit_info.commit_hash),
        )
    return JsonResponse(asset, status=201)


@csrf_exempt
@require_http_methods(["GET", "DELETE"])
def api_project_asset(request: HttpRequest, project_id: int, filename: str):
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)
    if request.method == "GET":
        try:
            path = project_asset_path(project, filename)
        except ValueError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)

        if not path.exists():
            return HttpResponseNotFound("File not found")
        if path.is_dir():
            return JsonResponse({"detail": "Cannot download a folder"}, status=400)

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(open(path, "rb"), content_type=content_type)

    if lock_resp := _check_project_lock(project):
        return lock_resp

    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    try:
        payload = delete_project_asset(project, filename)
    except ValueError as exc:
        message = str(exc)
        status = 404 if message == "file not found" else 400
        return JsonResponse({"detail": message}, status=status)
    project.last_status = Project.CompileStatus.PENDING
    project.save(update_fields=["last_status", "updated_at"])
    deleted_name = str(payload.get("name") or filename)
    commit_info = commit_project_changes(
        project,
        summary=meta["summary"] or _default_change_summary("delete_project_file", deleted_name),
        operation="delete_project_file",
        source=meta["source"],
        target_files=[deleted_name],
    )
    create_project_version(
        project=project,
        actor=user,
        source=meta["source"],
        operation="delete_project_file",
        target=deleted_name,
        target_file=deleted_name,
        summary=meta["summary"] or _default_change_summary("delete_project_file", deleted_name),
        before_content="",
        after_content="",
        snapshot_kind=ProjectVersion.SnapshotKind.EVENT,
        event_payload={
            "name": deleted_name,
            "kind": "file_delete",
            "git_commit": commit_info.commit_hash or "",
            "before_commit": commit_info.before_commit or "",
        },
        is_revertible=bool(commit_info.before_commit),
    )
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["GET", "PUT"])
def api_project_asset_content(request: HttpRequest, project_id: int, filename: str) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)
    if request.method == "GET":
        include_text = _as_bool(request.GET.get("include_text"), default=False)
        try:
            payload = read_project_asset_content(project, filename, include_text=include_text)
        except ValueError as exc:
            message = str(exc)
            status = 404 if message == "file not found" else 400
            return JsonResponse({"detail": message}, status=status)
        return JsonResponse(payload)

    if lock_resp := _check_project_lock(project):
        return lock_resp

    body = _json_body(request)
    content = body.get("content")
    if not isinstance(content, str):
        return JsonResponse({"detail": "content must be a string"}, status=400)
    try:
        meta = _change_meta(request, body)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    before_text = _read_text_target(project, filename)
    try:
        payload = write_project_asset_text(project, filename, content)
    except ValueError as exc:
        message = str(exc)
        status = 404 if message == "file not found" else 400
        return JsonResponse({"detail": message}, status=status)
    project.last_status = Project.CompileStatus.PENDING
    project.save(update_fields=["last_status", "updated_at"])
    if before_text != content:
        create_text_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="update_project_asset",
            target=filename,
            target_file=filename,
            summary=meta["summary"] or _default_change_summary("update_project_asset", filename),
        )
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["POST"])
def api_project_asset_rename(request: HttpRequest, project_id: int, filename: str) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)
    if lock_resp := _check_project_lock(project):
        return lock_resp
    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    new_filename = str(body.get("new_filename", "")).strip()
    if not new_filename:
        return JsonResponse({"detail": "new_filename is required"}, status=400)

    try:
        payload = rename_project_asset(project, filename, new_filename)
    except ValueError as exc:
        message = str(exc)
        status = 404 if message == "file not found" else 400
        return JsonResponse({"detail": message}, status=status)

    old_name = str(payload.get("old_name") or filename)
    new_name = str(payload.get("name") or new_filename)
    if old_name != new_name:
        commit_info = commit_project_changes(
            project,
            summary=meta["summary"] or _default_change_summary("rename_project_file", f"{old_name}->{new_name}"),
            operation="rename_project_file",
            source=meta["source"],
            target_files=[old_name, new_name],
        )
        create_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="rename_project_file",
            target=f"{old_name}->{new_name}",
            target_file=new_name,
            summary=meta["summary"] or _default_change_summary("rename_project_file", f"{old_name}->{new_name}"),
            before_content="",
            after_content="",
            snapshot_kind=ProjectVersion.SnapshotKind.EVENT,
            event_payload={
                "old_name": old_name,
                "new_name": new_name,
                "kind": "file_rename",
                "git_commit": commit_info.commit_hash or "",
                "before_commit": commit_info.before_commit or "",
            },
            is_revertible=bool(commit_info.before_commit),
        )
    return JsonResponse(payload)


@csrf_exempt
@require_GET
def api_project_sections(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    return JsonResponse({"sections": list_source_sections(project)})


@csrf_exempt
@require_http_methods(["POST"])
def api_project_typst_import(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if lock_resp := _check_project_lock(project):
        return lock_resp

    if not request.FILES.get("file"):
        return JsonResponse({"detail": "file is required"}, status=400)

    zip_bytes = request.FILES["file"].read()
    try:
        created = extract_project_zip(project, zip_bytes)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    project.last_status = Project.CompileStatus.PENDING
    project.save(update_fields=["last_status", "updated_at"])
    create_text_project_version(
        project=project,
        actor=user,
        source="web",
        operation="import_zip",
        target=main_source_filename(project),
        target_file=main_source_filename(project),
        summary="Imported ZIP",
    )
    return JsonResponse({"files": created}, status=201)


@csrf_exempt
@require_http_methods(["GET", "PUT"])
def api_project_section(request: HttpRequest, project_id: int, section_index: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        try:
            payload = get_source_section(project, section_index)
        except ValueError as exc:
            return JsonResponse({"detail": str(exc)}, status=404)
        return JsonResponse(payload)

    if lock_resp := _check_project_lock(project):
        return lock_resp

    body = _json_body(request)
    content = body.get("content")
    if not isinstance(content, str):
        return JsonResponse({"detail": "content must be a string"}, status=400)
    try:
        meta = _change_meta(request, body)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    section_before = get_source_section(project, section_index)
    try:
        payload = update_source_section(project, section_index, content)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    project.last_status = Project.CompileStatus.PENDING
    project.save(update_fields=["last_status", "updated_at"])
    if section_before.get("content") != payload.get("content"):
        target_file = str(payload.get("file_name") or main_source_filename(project))
        create_text_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="update_project_section",
            target=f"{target_file}:section:{section_index}",
            target_file=target_file,
            summary=meta["summary"] or _default_change_summary("update_project_section", target_file),
        )
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["POST"])
def api_project_insert(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if lock_resp := _check_project_lock(project):
        return lock_resp
    body = _json_body(request)
    position = body.get("position")
    text = body.get("text")
    if not isinstance(position, int):
        return JsonResponse({"detail": "position must be an integer"}, status=400)
    if not isinstance(text, str):
        return JsonResponse({"detail": "text must be a string"}, status=400)
    try:
        meta = _change_meta(request, body)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    before = read_source_content(project)
    try:
        result = insert_text_at_position(project, position, text)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    project.last_status = Project.CompileStatus.PENDING
    project.save(update_fields=["last_status", "updated_at"])
    after = read_source_content(project)
    if before != after:
        create_text_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="insert_text_at_position",
            target=f"{main_source_filename(project)}:char:{position}",
            target_file=main_source_filename(project),
            summary=meta["summary"] or _default_change_summary("insert_text_at_position", main_source_filename(project)),
        )
    return JsonResponse(result)


@csrf_exempt
@require_GET
def api_project_versions(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    try:
        limit = int(request.GET.get("limit", "40"))
    except ValueError:
        return JsonResponse({"detail": "limit must be an integer"}, status=400)
    raw_before = request.GET.get("before_id")
    before_id = None
    if raw_before not in (None, ""):
        try:
            before_id = int(raw_before)
        except ValueError:
            return JsonResponse({"detail": "before_id must be an integer"}, status=400)
    file_filter = request.GET.get("file") or None
    return JsonResponse(list_project_versions(project, limit=limit, before_id=before_id, file_filter=file_filter))


@csrf_exempt
@require_GET
def api_project_version_detail(request: HttpRequest, project_id: int, version_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    try:
        version = get_project_version(project, version_id)
    except Exception:
        return JsonResponse({"detail": "version not found"}, status=404)
    return JsonResponse(
        {
            "id": version.id,
            "source": version.source,
            "operation": version.operation,
            "target": version.target,
            "target_file": version.target_file,
            "snapshot_kind": version.snapshot_kind,
            "event_payload": version.event_payload,
            "is_revertible": version.is_revertible,
            "summary": version.summary,
            "created_at": version.created_at.isoformat(),
            "actor": version.actor.username if version.actor else None,
            "diff": build_version_diff(version),
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def api_project_version_rollback(request: HttpRequest, project_id: int, version_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if lock_resp := _check_project_lock(project):
        return lock_resp
    body = _json_body(request)
    try:
        meta = _change_meta(request, body)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    rollback_summary = str(body.get("summary", "")).strip()
    if not rollback_summary:
        rollback_summary = meta["summary"] or f"Rollback to version {version_id}"

    try:
        version = get_project_version(project, version_id)
    except Exception:
        return JsonResponse({"detail": "version not found"}, status=404)

    rollback_target = version.target_file or main_source_filename(project)
    before = _read_text_target(project, rollback_target)
    try:
        rollback_to_version(project, version)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    after = _read_text_target(project, rollback_target)

    project.last_status = Project.CompileStatus.PENDING
    project.save(update_fields=["last_status", "updated_at"])
    if before != after:
        create_text_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="rollback",
            target=rollback_target,
            target_file=rollback_target,
            summary=rollback_summary,
        )
    return JsonResponse({"detail": "rolled back", "version_id": version_id})


@csrf_exempt
@require_http_methods(["POST", "GET"])
def api_project_compile(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)

    if request.method == "POST":
        result = compile_project(project)
        project.last_status = result.status
        project.save(update_fields=["last_status", "updated_at"])
        commit_info = commit_project_text_changes(
            project,
            summary="Compiled",
            operation="compile",
            source="web",
            target_files=[],
        )
        if commit_info.commit_hash:
            create_project_version(
                project=project,
                actor=user,
                source="web",
                operation="compile",
                target=main_source_filename(project),
                target_file=main_source_filename(project),
                summary="Compiled",
                before_content="",
                after_content="",
                snapshot_kind=ProjectVersion.SnapshotKind.TEXT,
                event_payload={
                    "git_commit": commit_info.commit_hash,
                    "before_commit": commit_info.before_commit or "",
                },
                is_revertible=True,
            )
        return JsonResponse(_compile_payload(project, status=project.last_status, log=result.log, request_mode="compile"))

    return JsonResponse(_compile_payload(project, status=project.last_status, log=read_compile_log(project), request_mode="read"))


@csrf_exempt
@require_http_methods(["POST"])
def api_project_plantuml(request: HttpRequest, project_id: int) -> JsonResponse:
    """Write a .puml source file and immediately render it to SVG.

    Body JSON: {filename: str (base name, e.g. "diagram"), source: str}
    Returns: {puml_file, svg_file, success, error?}
    """
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)

    body = _json_body(request)
    raw_name = str(body.get("filename") or "").strip()
    source = str(body.get("source") or "").strip()

    if not raw_name:
        return JsonResponse({"detail": "filename is required"}, status=400)
    if not source:
        return JsonResponse({"detail": "source is required"}, status=400)

    # Normalise: strip any supplied extension, then add .puml
    base_name = raw_name.removesuffix(".puml").removesuffix(".svg")
    puml_rel = f"{base_name}.puml"
    svg_rel = f"{base_name}.svg"

    # Validate path
    from pathlib import PurePosixPath
    try:
        parts = PurePosixPath(puml_rel).parts
        if any(p in ("..", "") for p in parts) or puml_rel.startswith("/"):
            raise ValueError
    except Exception:
        return JsonResponse({"detail": "invalid filename"}, status=400)

    workdir = ensure_project_dir(project)

    puml_path = project_asset_path(project, puml_rel)
    puml_path.parent.mkdir(parents=True, exist_ok=True)
    puml_path.write_text(source, encoding="utf-8")

    try:
        svg_bytes = render_plantuml_svg(source)
    except Exception as exc:
        return JsonResponse({"success": False, "puml_file": puml_rel, "svg_file": svg_rel, "error": str(exc)}, status=502)

    svg_path = project_asset_path(project, svg_rel)
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_bytes(svg_bytes)

    # Update hash cache so pre-compile job won't re-render this on next compile
    hashes = _load_hashes(workdir)
    hashes[puml_rel] = _sha256(source.encode("utf-8"))
    _save_hashes(workdir, hashes)

    raw_source = (
        request.headers.get("X-Change-Source")
        or body.get("change_source")
        or "api"
    ).strip().lower()
    version_source = raw_source if raw_source in {"mcp", "web", "api"} else "api"
    summary = str(body.get("change_summary") or "").strip() or f"Render PlantUML diagram {svg_rel}"

    # Commit both .puml source and rendered .svg to git so they are tracked and can be deleted later.
    commit_info = commit_project_changes(
        project,
        summary=summary,
        operation="plantuml_render",
        source=version_source,
        target_files=[puml_rel, svg_rel],
    )
    create_project_version(
        project=project,
        actor=user,
        source=version_source,
        operation="plantuml_render",
        target=svg_rel,
        target_file=svg_rel,
        summary=summary,
        before_content="",
        after_content="",
        snapshot_kind=ProjectVersion.SnapshotKind.EVENT,
        event_payload={
            "puml_file": puml_rel,
            "svg_file": svg_rel,
            "kind": "plantuml_render",
            "git_commit": commit_info.commit_hash or "",
        },
        is_revertible=False,
    )

    return JsonResponse({"success": True, "puml_file": puml_rel, "svg_file": svg_rel})


@csrf_exempt
@require_GET
def api_project_pdf(request: HttpRequest, project_id: int):
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)
    path = pdf_file_path(project)
    if not path.exists():
        return HttpResponseNotFound("PDF not found")

    return FileResponse(
        open(path, "rb"),
        content_type="application/pdf",
        filename=project_pdf_download_name(project),
    )


@csrf_exempt
@require_http_methods(["POST"])
def api_project_create_template(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    if not user.is_staff:
        return JsonResponse({"detail": "Admin only."}, status=403)

    project = _project_with_owner(project_id, user)
    body = _json_body(request)
    title = str(body.get("title") or "").strip() or project.title
    category = str(body.get("category") or "other").strip()

    from templates_lib.models import Template as TemplateModel
    from templates_lib.services import create_template_from_project
    if category not in dict(TemplateModel.Category.choices):
        category = TemplateModel.Category.OTHER

    template = create_template_from_project(project, title=title, category=category)
    return JsonResponse({"id": template.id, "title": template.title}, status=201)


@csrf_exempt
@require_GET
def api_project_download_zip(request: HttpRequest, project_id: int):
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)
    archive = build_project_zip(project)
    filename_base = project_pdf_download_name(project).rsplit(".", 1)[0] or "project"
    return FileResponse(archive, content_type="application/zip", filename=f"{filename_base}.zip")


@csrf_exempt
@require_http_methods(["POST"])
def api_project_github_sync(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if not project.github_repo_url:
        return JsonResponse({"error": "GitHub repo URL must be configured"}, status=400)
    if not hasattr(user, "github_installation"):
        return JsonResponse({"error": "GitHub App not connected. Please connect via GitHub App."}, status=400)
    try:
        push_to_github(project)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    return JsonResponse({"ok": True})


@csrf_exempt
@require_GET
def api_github_app_install_url(request: HttpRequest) -> JsonResponse:
    import os
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    app_slug = os.environ.get("GITHUB_APP_SLUG", "")
    if not app_slug:
        return JsonResponse({"error": "GitHub App not configured"}, status=503)
    install_url = f"https://github.com/apps/{app_slug}/installations/new"
    return JsonResponse({"url": install_url})


@csrf_exempt
@require_http_methods(["GET"])
def github_app_callback(request: HttpRequest) -> JsonResponse:
    import os
    installation_id = request.GET.get("installation_id")
    if not installation_id:
        return JsonResponse({"error": "installation_id missing"}, status=400)
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    try:
        installation_id = int(installation_id)
    except (TypeError, ValueError):
        return JsonResponse({"error": "invalid installation_id"}, status=400)
    GitHubInstallation.objects.update_or_create(
        user=user,
        defaults={"installation_id": installation_id},
    )
    frontend_url = os.environ.get("FRONTEND_URL", "/")
    return redirect(f"{frontend_url}?github_connected=1")


@csrf_exempt
@require_http_methods(["DELETE"])
def api_github_app_disconnect(request: HttpRequest) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    GitHubInstallation.objects.filter(user=user).delete()
    return JsonResponse({"ok": True})


@csrf_exempt
@require_GET
def api_project_pdf_page_image(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    try:
        page = int(request.GET.get("page", "1"))
        scale = float(request.GET.get("scale", "1.5"))
        image_format = str(request.GET.get("image_format", "png"))
        payload = render_pdf_page_image(
            project,
            page=page,
            scale=scale,
            image_format=image_format,
        )
    except (ValueError, TypeError) as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    return JsonResponse(payload)


@csrf_exempt
@require_GET
def api_project_pdf_page_count(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    try:
        payload = get_project_pdf_page_count(project)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_project_pdf_embed(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        from .services import get_pdf_embed_manifest
        return JsonResponse({"embeds": get_pdf_embed_manifest(project)})

    try:
        body = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"detail": "invalid JSON"}, status=400)

    pdf_path = str(body.get("file") or "").strip()
    if not pdf_path:
        return JsonResponse({"detail": "file is required"}, status=400)
    if not pdf_path.lower().endswith(".pdf"):
        return JsonResponse({"detail": "only PDF files can be embedded"}, status=400)

    enabled = bool(body.get("enabled", True))
    from .services import set_pdf_embed_enabled
    entry = set_pdf_embed_enabled(project, pdf_path, enabled)

    action = "enabled" if enabled else "disabled"
    commit_project_changes(
        project,
        summary=f"PDF embed {action}: {pdf_path}",
        operation="update_pdf_embed",
        source=ProjectVersion.Source.WEB,
        target_files=[".smarttex/pdf_includes.json"],
    )

    return JsonResponse({"file": pdf_path, "enabled": enabled, "entry": entry})


@csrf_exempt
@require_GET
def api_project_synctex_line(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    try:
        line = int(request.GET.get("line", "0"))
        column = int(request.GET.get("column", "1"))
        file_name = str(request.GET.get("file_name") or main_source_filename(project))
        payload = synctex_line_to_pdf(
            project,
            line=line,
            column=column,
            file_name=file_name,
        )
    except (ValueError, TypeError) as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    return JsonResponse(payload)


@csrf_exempt
@require_GET
def api_project_synctex_pdf(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    try:
        page = int(request.GET.get("page", "0"))
        x = float(request.GET.get("x", "0"))
        y = float(request.GET.get("y", "0"))
        payload = synctex_pdf_to_line(project, page=page, x=x, y=y)
    except (ValueError, TypeError) as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    return JsonResponse(payload)


@csrf_exempt
@require_GET
def api_project_typst_preview(request: HttpRequest, project_id: int, subpath: str = ""):
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if project.markup_type != MarkupType.TYPST:
        return JsonResponse({"detail": "Typst preview is only available for Typst projects"}, status=400)
    if not bool(getattr(settings, "TINYMIST_PREVIEW_ENABLED", True)):
        return JsonResponse({"detail": "Tinymist preview is disabled"}, status=404)

    from .tinymist_preview_service import get_or_create_preview_session, restart_preview_session

    try:
        session = async_to_sync(get_or_create_preview_session)(
            project.id,
            user.id,
            request.GET.get("theme"),
        )
    except Exception as exc:
        return JsonResponse({"detail": f"Tinymist preview unavailable: {exc}"}, status=502)

    target_path = "/" + str(subpath or "").lstrip("/")
    query = request.META.get("QUERY_STRING", "")
    target_url = f"http://127.0.0.1:{session.port}{target_path}"
    if query:
        target_url = f"{target_url}?{query}"

    proxy_req = urllib.request.Request(
        target_url,
        headers={
            "Accept": request.headers.get("Accept", "*/*"),
            "User-Agent": request.headers.get("User-Agent", "SmartTeX"),
        },
        method="GET",
    )
    def _fetch_upstream(preview_session):
        resolved_target = f"http://127.0.0.1:{preview_session.port}{target_path}"
        if query:
            resolved_target = f"{resolved_target}?{query}"
        req = urllib.request.Request(
            resolved_target,
            headers={
                "Accept": request.headers.get("Accept", "*/*"),
                "User-Agent": request.headers.get("User-Agent", "SmartTeX"),
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=20) as upstream:
            body = upstream.read()
            status = int(getattr(upstream, "status", 200) or 200)
            content_type = upstream.headers.get("Content-Type", "text/html; charset=utf-8")
            if target_path == "/" and content_type.startswith("text/html"):
                body = _inject_typst_preview_bridge(body, project.id)
            response = HttpResponse(body, status=status, content_type=content_type)
            for header in ("Cache-Control", "ETag", "Last-Modified"):
                value = upstream.headers.get(header)
                if value:
                    response[header] = value
            return response

    try:
        return _fetch_upstream(session)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return HttpResponse(body, status=exc.code, content_type=exc.headers.get("Content-Type", "text/plain; charset=utf-8"))
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, OSError) and getattr(reason, "errno", None) == 111:
            try:
                session = async_to_sync(restart_preview_session)(project.id, user.id, request.GET.get("theme"))
                return _fetch_upstream(session)
            except Exception as restart_exc:
                return JsonResponse({"detail": f"Tinymist preview proxy error: {restart_exc}"}, status=502)
        return JsonResponse({"detail": f"Tinymist preview proxy error: {exc}"}, status=502)
    except Exception as exc:
        return JsonResponse({"detail": f"Tinymist preview proxy error: {exc}"}, status=502)


@csrf_exempt
@require_http_methods(["POST"])
def api_project_typst_preview_restart(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if project.markup_type != MarkupType.TYPST:
        return JsonResponse({"detail": "Typst preview is only available for Typst projects"}, status=400)
    if not bool(getattr(settings, "TINYMIST_PREVIEW_ENABLED", True)):
        return JsonResponse({"detail": "Tinymist preview is disabled"}, status=404)

    from .tinymist_preview_service import restart_preview_session

    body = _json_body(request)
    try:
        session = async_to_sync(restart_preview_session)(
            project.id,
            user.id,
            body.get("theme") or request.GET.get("theme"),
        )
    except Exception as exc:
        return JsonResponse({"detail": f"Tinymist preview restart failed: {exc}"}, status=502)
    return JsonResponse({"ok": True, "port": session.port})


def _typst_only(project: Project) -> JsonResponse | None:
    if project.markup_type != MarkupType.TYPST:
        return JsonResponse({"detail": "This endpoint is only available for Typst projects"}, status=400)
    return None


def _tinymist_lsp_enabled() -> JsonResponse | None:
    if not bool(getattr(settings, "TINYMIST_LSP_ENABLED", True)):
        return JsonResponse({"detail": "Tinymist LSP is disabled"}, status=404)
    return None


def _lsp_position(line: int, character: int) -> dict:
    return {"line": line, "character": character}


def _lsp_text_document(uri: str) -> dict:
    return {"uri": uri}


def _severity_label(n: int) -> str:
    return {1: "error", 2: "warning", 3: "information", 4: "hint"}.get(n, "unknown")


def _normalize_lsp_location(loc: dict, project_root: str) -> dict:
    uri = loc.get("uri", "")
    if uri.startswith(project_root):
        uri = uri[len(project_root):].lstrip("/")
    r = loc.get("range", {})
    start = r.get("start", {})
    return {"file": uri, "line": start.get("line", 0) + 1, "column": start.get("character", 0) + 1}


def _apply_text_edits(content: str, edits: list[dict]) -> str:
    """Apply LSP TextEdit list to content (edits must be in reverse order by range start)."""
    lines = content.split("\n")
    for edit in sorted(edits, key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]), reverse=True):
        r = edit["range"]
        new_text = edit["newText"]
        sl, sc = r["start"]["line"], r["start"]["character"]
        el, ec = r["end"]["line"], r["end"]["character"]
        start_line = lines[sl] if sl < len(lines) else ""
        end_line = lines[el] if el < len(lines) else ""
        prefix = start_line[:sc]
        suffix = end_line[ec:]
        replacement = (prefix + new_text + suffix).split("\n")
        lines[sl:el + 1] = replacement
    return "\n".join(lines)


@csrf_exempt
@require_GET
def api_project_tinymist_diagnostics(request: HttpRequest, project_id: int) -> JsonResponse:
    """Return LSP diagnostics for a Typst file via tinymist (no compilation needed)."""
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if err := _typst_only(project):
        return err
    if err := _tinymist_lsp_enabled():
        return err

    file_name = request.GET.get("file_name") or main_source_filename(project)
    from .tinymist_service import close_api_session, get_or_create_api_session
    from .services import project_dir

    root = project_dir(project)
    file_path = root / file_name
    if not file_path.exists():
        return JsonResponse({"detail": f"File not found: {file_name}"}, status=404)

    text = file_path.read_text(encoding="utf-8", errors="replace")
    uri = file_path.as_uri()

    async def _get_diags():
        session = await get_or_create_api_session(project.id, user.id)
        try:
            await session.api_open_file(uri, text)
            return await session.collect_diagnostics(uri, timeout=4.0)
        finally:
            if not bool(getattr(settings, "TINYMIST_API_SESSION_PERSIST", False)):
                await close_api_session(project.id, user.id)

    try:
        raw = async_to_sync(_get_diags)()
    except Exception as exc:
        return JsonResponse({"detail": f"Tinymist LSP error: {exc}"}, status=502)

    project_root_uri = root.as_uri() + "/"
    diagnostics = [
        {
            "file": file_name,
            "line": d["range"]["start"]["line"] + 1,
            "column": d["range"]["start"]["character"] + 1,
            "end_line": d["range"]["end"]["line"] + 1,
            "end_column": d["range"]["end"]["character"] + 1,
            "severity": _severity_label(d.get("severity", 1)),
            "message": d.get("message", ""),
            "source": d.get("source", "tinymist"),
        }
        for d in raw
    ]
    return JsonResponse({"file": file_name, "diagnostics": diagnostics})


@csrf_exempt
@require_GET
def api_project_tinymist_symbols(request: HttpRequest, project_id: int) -> JsonResponse:
    """Return document symbol outline for a Typst file via tinymist LSP."""
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if err := _typst_only(project):
        return err
    if err := _tinymist_lsp_enabled():
        return err

    file_name = request.GET.get("file_name") or main_source_filename(project)
    from .tinymist_service import close_api_session, get_or_create_api_session
    from .services import project_dir

    root = project_dir(project)
    file_path = root / file_name
    if not file_path.exists():
        return JsonResponse({"detail": f"File not found: {file_name}"}, status=404)

    text = file_path.read_text(encoding="utf-8", errors="replace")
    uri = file_path.as_uri()

    async def _get_symbols():
        session = await get_or_create_api_session(project.id, user.id)
        try:
            await session.api_open_file(uri, text)
            return await session.api_request("textDocument/documentSymbol", {
                "textDocument": _lsp_text_document(uri),
            })
        finally:
            if not bool(getattr(settings, "TINYMIST_API_SESSION_PERSIST", False)):
                await close_api_session(project.id, user.id)

    try:
        result = async_to_sync(_get_symbols)()
    except Exception as exc:
        return JsonResponse({"detail": f"Tinymist LSP error: {exc}"}, status=502)

    def _flatten(syms: list, depth: int = 0) -> list:
        out = []
        for s in (syms or []):
            out.append({
                "name": s.get("name", ""),
                "kind": s.get("kind"),
                "line": s.get("range", {}).get("start", {}).get("line", 0) + 1,
                "depth": depth,
            })
            out.extend(_flatten(s.get("children", []), depth + 1))
        return out

    return JsonResponse({"file": file_name, "symbols": _flatten(result or [])})


@csrf_exempt
@require_GET
def api_project_tinymist_format(request: HttpRequest, project_id: int) -> JsonResponse:
    """Return formatted content for a Typst file via tinymist LSP."""
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if err := _typst_only(project):
        return err
    if err := _tinymist_lsp_enabled():
        return err

    file_name = request.GET.get("file_name") or main_source_filename(project)
    from .tinymist_service import close_api_session, get_or_create_api_session
    from .services import project_dir

    root = project_dir(project)
    file_path = root / file_name
    if not file_path.exists():
        return JsonResponse({"detail": f"File not found: {file_name}"}, status=404)

    text = file_path.read_text(encoding="utf-8", errors="replace")
    uri = file_path.as_uri()

    async def _format():
        session = await get_or_create_api_session(project.id, user.id)
        try:
            await session.api_open_file(uri, text)
            return await session.api_request("textDocument/formatting", {
                "textDocument": _lsp_text_document(uri),
                "options": {"tabSize": 2, "insertSpaces": True},
            })
        finally:
            if not bool(getattr(settings, "TINYMIST_API_SESSION_PERSIST", False)):
                await close_api_session(project.id, user.id)

    try:
        edits = async_to_sync(_format)()
    except Exception as exc:
        return JsonResponse({"detail": f"Tinymist LSP error: {exc}"}, status=502)

    if not edits:
        return JsonResponse({"file": file_name, "formatted": text, "changed": False})

    formatted = _apply_text_edits(text, edits)
    return JsonResponse({"file": file_name, "formatted": formatted, "changed": formatted != text})


@csrf_exempt
@require_GET
def api_project_tinymist_definition(request: HttpRequest, project_id: int) -> JsonResponse:
    """Return the definition location for a symbol at a given position via tinymist LSP."""
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if err := _typst_only(project):
        return err
    if err := _tinymist_lsp_enabled():
        return err

    file_name = request.GET.get("file_name") or main_source_filename(project)
    try:
        line = int(request.GET.get("line", "1")) - 1
        column = int(request.GET.get("column", "1")) - 1
    except (TypeError, ValueError) as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    from .tinymist_service import close_api_session, get_or_create_api_session
    from .services import project_dir

    root = project_dir(project)
    file_path = root / file_name
    if not file_path.exists():
        return JsonResponse({"detail": f"File not found: {file_name}"}, status=404)

    text = file_path.read_text(encoding="utf-8", errors="replace")
    uri = file_path.as_uri()

    async def _definition():
        session = await get_or_create_api_session(project.id, user.id)
        try:
            await session.api_open_file(uri, text)
            return await session.api_request("textDocument/definition", {
                "textDocument": _lsp_text_document(uri),
                "position": _lsp_position(line, column),
            })
        finally:
            if not bool(getattr(settings, "TINYMIST_API_SESSION_PERSIST", False)):
                await close_api_session(project.id, user.id)

    try:
        result = async_to_sync(_definition)()
    except Exception as exc:
        return JsonResponse({"detail": f"Tinymist LSP error: {exc}"}, status=502)

    if not result:
        return JsonResponse({"file": file_name, "line": line + 1, "column": column + 1, "definition": None})

    locations = result if isinstance(result, list) else [result]
    project_root_uri = root.as_uri() + "/"
    normalized = [_normalize_lsp_location(loc, project_root_uri) for loc in locations]
    return JsonResponse({"definition": normalized[0] if len(normalized) == 1 else normalized})


@csrf_exempt
@require_GET
def api_project_tinymist_references(request: HttpRequest, project_id: int) -> JsonResponse:
    """Return all references to a symbol at a given position via tinymist LSP."""
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if err := _typst_only(project):
        return err
    if err := _tinymist_lsp_enabled():
        return err

    file_name = request.GET.get("file_name") or main_source_filename(project)
    try:
        line = int(request.GET.get("line", "1")) - 1
        column = int(request.GET.get("column", "1")) - 1
    except (TypeError, ValueError) as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    from .tinymist_service import close_api_session, get_or_create_api_session
    from .services import project_dir

    root = project_dir(project)
    file_path = root / file_name
    if not file_path.exists():
        return JsonResponse({"detail": f"File not found: {file_name}"}, status=404)

    text = file_path.read_text(encoding="utf-8", errors="replace")
    uri = file_path.as_uri()

    async def _references():
        session = await get_or_create_api_session(project.id, user.id)
        try:
            await session.api_open_file(uri, text)
            return await session.api_request("textDocument/references", {
                "textDocument": _lsp_text_document(uri),
                "position": _lsp_position(line, column),
                "context": {"includeDeclaration": True},
            })
        finally:
            if not bool(getattr(settings, "TINYMIST_API_SESSION_PERSIST", False)):
                await close_api_session(project.id, user.id)

    try:
        result = async_to_sync(_references)()
    except Exception as exc:
        return JsonResponse({"detail": f"Tinymist LSP error: {exc}"}, status=502)

    project_root_uri = root.as_uri() + "/"
    references = [_normalize_lsp_location(loc, project_root_uri) for loc in (result or [])]
    return JsonResponse({"references": references, "count": len(references)})


@csrf_exempt
@require_GET
def api_project_typst_citations(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    if err := _typst_only(project):
        return err

    citation_index = build_typst_citation_index(project)
    prefix = str(request.GET.get("prefix") or "").strip().lower()
    entries = citation_index.get("entries") or []
    if prefix:
        entries = [item for item in entries if str(item.get("key") or "").lower().startswith(prefix)]
    return JsonResponse({
        "entries": entries,
        "count": len(entries),
        "source_files": citation_index.get("source_files") or [],
        "reachable_files": citation_index.get("reachable_files") or [],
    })


@login_required
@require_http_methods(["POST"])
def create_project_from_dashboard(request: HttpRequest):
    title = request.POST.get("title", "").strip() or "Новий проєкт"
    template_id = request.POST.get("template_id")

    template_obj = None
    requested_markup_type = request.POST.get("markup_type")
    try:
        markup_type = _normalize_markup_type(requested_markup_type)
    except ValueError:
        markup_type = MarkupType.LATEX

    template_zip = None
    project_main_file = ""
    content = _default_content_for_markup(markup_type)
    if template_id:
        template_obj = get_object_or_404(Template, id=template_id, is_active=True)
        markup_type = template_obj.markup_type
        project_main_file = normalize_template_main_file(template_obj)
        content = template_obj.content or _default_content_for_markup(markup_type)
        if template_obj.zip_file:
            template_zip = template_obj.zip_file

    zip_only = template_zip and not (template_obj and template_obj.content)

    if not zip_only and is_source_too_large(content):
        return HttpResponseForbidden("Template file exceeds 1MB")

    with transaction.atomic():
        project = Project.objects.create(
            owner=request.user,
            title=title,
            template=template_obj,
            markup_type=markup_type,
            main_file=project_main_file,
        )
        if not zip_only:
            initialize_main_source(project, content)
            create_text_project_version(
                project=project,
                actor=request.user,
                source="web",
                operation="create_project",
                target=main_source_filename(project),
                target_file=main_source_filename(project),
                summary=_default_change_summary("create_project", main_source_filename(project)),
            )

    if template_zip:
        try:
            zip_bytes = template_zip.read()
            created = extract_project_zip(project, zip_bytes, allow_main_override=True)
            # Prefer the configured template main file, then fall back to common names from the ZIP.
            configured_main = normalize_template_main_file(template_obj) if template_obj else ""
            created_names = [f["name"] for f in created]
            if configured_main and configured_main in created_names:
                detected = configured_main
            else:
                default_main = source_filename_for_markup(markup_type)
                main_candidates = [name for name in created_names if name in (default_main, "main.tex", "main.typ")]
                detected = main_candidates[0] if main_candidates else (created_names[0] if created_names else None)
            if detected and project.main_file != detected:
                project.main_file = detected
                project.save(update_fields=["main_file"])
            create_text_project_version(
                project=project,
                actor=request.user,
                source="web",
                operation="import_zip",
                target=main_source_filename(project),
                target_file=main_source_filename(project),
                summary=f"Initialized from template ZIP: {template_obj.title}",
            )
        except (ValueError, OSError):
            pass

    if template_obj is not None:
        initialize_longdoc_from_template(project, template_obj)

    _compile_project_after_create(project)
    return redirect("projects:editor", project_id=project.id)
