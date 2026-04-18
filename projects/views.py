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
from templates_lib.models import Template

from .models import Project
from .services import (
    build_version_diff,
    compile_project,
    create_project_version,
    delete_project_asset,
    delete_project_files,
    get_project_version,
    get_project_pdf_page_count,
    list_project_versions,
    has_pdf,
    initialize_main_tex,
    is_tex_too_large,
    list_project_assets,
    list_tex_sections,
    pdf_file_path,
    project_pdf_download_name,
    pdf_relative_url,
    pdf_version,
    project_asset_path,
    read_compile_log,
    read_project_asset_content,
    read_tex_content,
    read_project_window,
    rename_project_asset,
    render_pdf_page_image,
    synctex_line_to_pdf,
    synctex_pdf_to_line,
    write_project_window,
    rollback_to_version,
    save_project_asset,
    search_project_content,
    get_tex_section,
    insert_text_at_position,
    update_tex_section,
    write_tex_content,
    IMAGE_EXTENSIONS,
)

DEFAULT_LATEX = r"""\\documentclass{article}
\\usepackage[ukrainian]{babel}
\\usepackage{fontspec}
\\setmainfont{Times New Roman}
\\begin{document}
Hello, SmartTeX!
\\end{document}
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


def _project_payload(project: Project) -> dict:
    return {
        "id": project.id,
        "title": project.title,
        "template_id": project.template_id,
        "last_status": project.last_status,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


def _project_with_owner(project_id: int, user) -> Project:
    return get_object_or_404(Project, id=project_id, owner=user)


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
    return render(request, "projects/editor.html", {"project": project})


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
            data = [_project_payload(p) for p in qs]
            return JsonResponse(data, safe=False)

        safe_limit = max(1, min(int(limit or 24), 120))
        if before_id is not None:
            qs = qs.filter(id__lt=before_id)
        rows = list(qs.order_by("-id")[: safe_limit + 1])
        has_more = len(rows) > safe_limit
        items = rows[:safe_limit]
        data = [_project_payload(p) for p in items]
        next_before_id = items[-1].id if has_more and items else None
        return JsonResponse({"projects": data, "has_more": has_more, "next_before_id": next_before_id})

    body = _json_body(request)
    title = body.get("title", "").strip() or "Новий проєкт"
    template_id = body.get("template_id")

    template_obj = None
    content = DEFAULT_LATEX
    if template_id is not None:
        template_obj = get_object_or_404(Template, id=template_id, is_active=True)
        content = template_obj.content

    if is_tex_too_large(content):
        return JsonResponse({"detail": "Template content exceeds 1MB"}, status=400)

    with transaction.atomic():
        project = Project.objects.create(owner=user, title=title, template=template_obj)
        initialize_main_tex(project, content)
        create_project_version(
            project=project,
            actor=user,
            source="api",
            operation="create_project",
            target="main.tex",
            summary="Initial project document",
            before_content="",
            after_content=content,
        )
    return JsonResponse(_project_payload(project), status=201)


@csrf_exempt
@require_http_methods(["GET", "PATCH", "DELETE"])
def api_project_detail(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        data = _project_payload(project)
        data["template"] = project.template.title if project.template else None
        return JsonResponse(data)

    if request.method == "PATCH":
        body = _json_body(request)
        title = body.get("title", "").strip()
        if title:
            project.title = title
            project.save(update_fields=["title", "updated_at"])
        return JsonResponse(_project_payload(project))

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
        return JsonResponse({"content": read_tex_content(project)})

    body = _json_body(request)
    content = body.get("content", "")
    if not isinstance(content, str):
        return JsonResponse({"detail": "content must be a string"}, status=400)
    if is_tex_too_large(content):
        return JsonResponse({"detail": "File exceeds 1MB"}, status=400)
    try:
        meta = _change_meta(request, body)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    before = read_tex_content(project)
    write_tex_content(project, content)
    project.last_status = Project.CompileStatus.PENDING
    project.save(update_fields=["last_status", "updated_at"])
    if meta["source"] == "mcp" and before != content:
        create_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="update_project_file",
            target="main.tex",
            summary=meta["summary"],
            before_content=before,
            after_content=content,
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
            file_name=request.GET.get("file_name", "main.tex"),
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

    before = read_tex_content(project)
    try:
        payload = write_project_window(
            project,
            file_name=str(body.get("file_name", "main.tex")),
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
    after = read_tex_content(project)
    if meta["source"] == "mcp" and before != after:
        target = f"{payload.get('file_name', 'main.tex')}:{payload.get('mode', 'window')}"
        create_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="write_project_window",
            target=target,
            summary=meta["summary"],
            before_content=before,
            after_content=after,
        )
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_project_assets(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        return JsonResponse({"files": list_project_assets(project)})

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
        if upload_ext not in IMAGE_EXTENSIONS:
            return JsonResponse({"detail": "Only image uploads are allowed"}, status=400)
        try:
            asset = save_project_asset(project, upload.name, upload.read())
        except ValueError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        if meta["source"] == "mcp":
            create_project_version(
                project=project,
                actor=user,
                source=meta["source"],
                operation="upload_project_file",
                target=asset["name"],
                summary=meta["summary"],
                before_content="",
                after_content=f"[binary upload] {asset['name']} ({asset['size']} bytes)",
            )
        return JsonResponse(asset, status=201)

    body = raw_body
    filename = str(body.get("filename", "")).strip()
    content_base64 = body.get("content_base64")
    text_content = body.get("text_content")
    if not filename:
        return JsonResponse({"detail": "filename is required"}, status=400)
    if content_base64 is None and text_content is None:
        return JsonResponse({"detail": "content_base64 or text_content is required"}, status=400)
    if _ext_from_name(filename) not in IMAGE_EXTENSIONS:
        return JsonResponse({"detail": "Only image uploads are allowed"}, status=400)

    try:
        if content_base64 is not None:
            payload = base64.b64decode(content_base64, validate=True)
        else:
            payload = str(text_content).encode("utf-8")
        asset = save_project_asset(project, filename, payload)
    except (ValueError, binascii.Error) as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    if meta["source"] == "mcp":
        create_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="upload_project_file",
            target=asset["name"],
            summary=meta["summary"],
            before_content="",
            after_content=f"[binary upload] {asset['name']} ({asset['size']} bytes)",
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

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(open(path, "rb"), content_type=content_type)

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

    if meta["source"] == "mcp":
        deleted_name = str(payload.get("name") or filename)
        create_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="delete_project_file",
            target=deleted_name,
            summary=meta["summary"],
            before_content=f"[binary file] {deleted_name}",
            after_content="",
        )
    return JsonResponse(payload)


@csrf_exempt
@require_GET
def api_project_asset_content(request: HttpRequest, project_id: int, filename: str) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)
    include_text = _as_bool(request.GET.get("include_text"), default=False)
    try:
        payload = read_project_asset_content(project, filename, include_text=include_text)
    except ValueError as exc:
        message = str(exc)
        status = 404 if message == "file not found" else 400
        return JsonResponse({"detail": message}, status=status)
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["POST"])
def api_project_asset_rename(request: HttpRequest, project_id: int, filename: str) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()

    project = _project_with_owner(project_id, user)
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

    if meta["source"] == "mcp":
        old_name = str(payload.get("old_name") or filename)
        new_name = str(payload.get("name") or new_filename)
        if old_name != new_name:
            create_project_version(
                project=project,
                actor=user,
                source=meta["source"],
                operation="rename_project_file",
                target=f"{old_name}->{new_name}",
                summary=meta["summary"],
                before_content=f"[binary file] {old_name}",
                after_content=f"[binary file] {new_name}",
            )
    return JsonResponse(payload)


@csrf_exempt
@require_GET
def api_project_sections(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    return JsonResponse({"sections": list_tex_sections(project)})


@csrf_exempt
@require_http_methods(["GET", "PUT"])
def api_project_section(request: HttpRequest, project_id: int, section_index: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)

    if request.method == "GET":
        try:
            payload = get_tex_section(project, section_index)
        except ValueError as exc:
            return JsonResponse({"detail": str(exc)}, status=404)
        return JsonResponse(payload)

    body = _json_body(request)
    content = body.get("content")
    if not isinstance(content, str):
        return JsonResponse({"detail": "content must be a string"}, status=400)
    try:
        meta = _change_meta(request, body)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    before_file = read_tex_content(project)
    try:
        payload = update_tex_section(project, section_index, content)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    after_file = read_tex_content(project)

    project.last_status = Project.CompileStatus.PENDING
    project.save(update_fields=["last_status", "updated_at"])
    if meta["source"] == "mcp" and before_file != after_file:
        create_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="update_project_section",
            target=f"main.tex:section:{section_index}",
            summary=meta["summary"],
            before_content=before_file,
            after_content=after_file,
        )
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["POST"])
def api_project_insert(request: HttpRequest, project_id: int) -> JsonResponse:
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
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

    before = read_tex_content(project)
    try:
        result = insert_text_at_position(project, position, text)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    after = read_tex_content(project)

    project.last_status = Project.CompileStatus.PENDING
    project.save(update_fields=["last_status", "updated_at"])
    if meta["source"] == "mcp" and before != after:
        create_project_version(
            project=project,
            actor=user,
            source=meta["source"],
            operation="insert_text_at_position",
            target=f"main.tex:char:{position}",
            summary=meta["summary"],
            before_content=before,
            after_content=after,
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
    return JsonResponse(list_project_versions(project, limit=limit, before_id=before_id))


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

    before = read_tex_content(project)
    try:
        rollback_to_version(project, version)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    after = read_tex_content(project)

    project.last_status = Project.CompileStatus.PENDING
    project.save(update_fields=["last_status", "updated_at"])
    create_project_version(
        project=project,
        actor=user,
        source=meta["source"],
        operation="rollback",
        target="main.tex",
        summary=rollback_summary,
        before_content=before,
        after_content=after,
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
        return JsonResponse(
            {
                "status": project.last_status,
                "pdf_url": pdf_relative_url(project) if has_pdf(project) else None,
                "pdf_version": pdf_version(project),
                "log": result.log,
            }
        )

    return JsonResponse(
        {
            "status": project.last_status,
            "pdf_url": pdf_relative_url(project) if has_pdf(project) else None,
            "pdf_version": pdf_version(project),
            "log": read_compile_log(project),
        }
    )


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
        file_name = str(request.GET.get("file_name", "main.tex"))
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
    content = DEFAULT_LATEX
    if template_id:
        template_obj = get_object_or_404(Template, id=template_id, is_active=True)
        content = template_obj.content

    if is_tex_too_large(content):
        return HttpResponseForbidden("Template file exceeds 1MB")

    with transaction.atomic():
        project = Project.objects.create(owner=request.user, title=title, template=template_obj)
        initialize_main_tex(project, content)
        create_project_version(
            project=project,
            actor=request.user,
            source="web",
            operation="create_project",
            target="main.tex",
            summary="Initial project document",
            before_content="",
            after_content=content,
        )
    return redirect("projects:editor", project_id=project.id)
