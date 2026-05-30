import json
import base64
import binascii
import mimetypes
import posixpath
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, HttpRequest, HttpResponseForbidden, HttpResponseNotFound, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from accounts.auth_helpers import get_api_user
from SmartTeX.markup import MarkupType, source_filename_for_markup
from longdoc.locks import get_locking_change_proposal, get_locking_session
from longdoc.services import get_longdoc_settings_or_none, initialize_longdoc_from_template
from small_model.models import ProjectSmallModelSettings, UserSmallModelAccess
from small_model.services.quota_service import SmallModelQuotaService
from templates_lib.models import Template
from templates_lib.services import normalize_template_main_file

from .models import Project, ProjectVersion
from .services import (
    ALLOWED_UPLOAD_EXTENSIONS,
    build_project_zip,
    build_version_diff,
    cancel_github_sync,
    compile_state_for_status,
    commit_project_text_changes,
    compile_project,
    create_project_directory,
    create_project_text_file,
    create_project_version,
    create_text_project_version,
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
        "last_status": project.last_status,
        "longdoc": {
            "enabled": bool(longdoc_settings and longdoc_settings.enabled),
            "context_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.context_enabled),
            "outline_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.outline_enabled),
            "tasks_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.tasks_enabled),
            "notes_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.notes_enabled),
            "summaries_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.summaries_enabled),
            "requirements_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.requirements_enabled),
            "ai_sessions_enabled": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.ai_sessions_enabled),
            "mcp_controlled_access": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.mcp_controlled_access),
            "mcp_write_context": bool(longdoc_settings and longdoc_settings.enabled and longdoc_settings.mcp_write_context),
            "locked": locked,
            "locking_proposal_id": locking_proposal.id if locking_proposal else None,
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
            "pat_set": bool(project.github_pat),
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
        if "github_pat" in body:
            project.github_pat = str(body["github_pat"] or "").strip()
            update_fields.append("github_pat")
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
                event_payload={"name": asset["name"], "size": asset["size"], "kind": "binary_upload"},
                is_revertible=False,
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
            event_payload={"name": asset["name"], "size": asset["size"], "kind": "binary_upload"},
            is_revertible=False,
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
    deleted_is_text = bool(payload.get("is_text"))
    if deleted_is_text:
        commit_info = commit_project_text_changes(
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
                "kind": "text_delete",
                "git_commit": commit_info.commit_hash or "",
                "before_commit": commit_info.before_commit or "",
            },
            is_revertible=bool(commit_info.before_commit),
        )
    else:
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
            event_payload={"name": deleted_name, "kind": "binary_delete"},
            is_revertible=False,
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
        if bool(payload.get("is_text")):
            commit_info = commit_project_text_changes(
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
                    "kind": "text_rename",
                    "git_commit": commit_info.commit_hash or "",
                    "before_commit": commit_info.before_commit or "",
                },
                is_revertible=bool(commit_info.before_commit),
            )
        else:
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
                event_payload={"old_name": old_name, "new_name": new_name, "kind": "binary_rename"},
                is_revertible=False,
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
    if not project.github_repo_url or not project.github_pat:
        return JsonResponse({"error": "GitHub repo URL and PAT must be configured"}, status=400)
    try:
        push_to_github(project)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=502)
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
