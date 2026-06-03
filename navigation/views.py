from __future__ import annotations

import json
import logging

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.auth_helpers import get_api_user
from projects.views import _project_with_owner

from .services.enforcement import check_preparation_freshness
from .services.preparation import prepare_document_work
from .services.smart_search import smart_search

logger = logging.getLogger(__name__)


def _json_body(request: HttpRequest) -> dict:
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _unauthorized() -> JsonResponse:
    return JsonResponse({"detail": "Authentication required"}, status=401)


@csrf_exempt
@require_http_methods(["POST"])
def api_prepare_document_work(request: HttpRequest, project_id: int) -> JsonResponse:
    """Prepare a document/project write workflow for MCP/web clients.

    This endpoint is intentionally read/preparation-only for user content. It may
    update navigation-index bookkeeping and cache a preparation result, but it
    never creates proposals and never edits project source files.
    """
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    body = _json_body(request)
    result = prepare_document_work(
        project,
        user_request=str(body.get("user_request") or body.get("request") or ""),
        preparation_id=(str(body.get("preparation_id")) if body.get("preparation_id") else None),
        previous_error=body.get("previous_error") if isinstance(body.get("previous_error"), dict) else None,
        attempted_patch_ops=body.get("attempted_patch_ops") if isinstance(body.get("attempted_patch_ops"), list) else None,
        selected_file=(str(body.get("selected_file")) if body.get("selected_file") else None),
        selected_region_id=(int(body["selected_region_id"]) if str(body.get("selected_region_id") or "").strip().isdigit() else None),
        annotation_ids=body.get("annotation_ids") if isinstance(body.get("annotation_ids"), list) else None,
        target_filenames=body.get("target_filenames") if isinstance(body.get("target_filenames"), list) else None,
    )
    return JsonResponse(result)


@csrf_exempt
@require_http_methods(["POST"])
def api_smart_search(request: HttpRequest, project_id: int) -> JsonResponse:
    """Smart search across project documents using the navigation index.

    Read-only: never edits files, never creates proposals.
    """
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    body = _json_body(request)
    query = str(body.get("query") or "").strip()
    scope = str(body.get("scope") or "reachable_document")
    selected_file = (str(body.get("selected_file")) if body.get("selected_file") else None)
    include_orphans = bool(body.get("include_orphans", False))
    include_extra = bool(body.get("include_extra", False))
    include_config = bool(body.get("include_config", False))
    use_small_model = bool(body.get("use_small_model", True))
    try:
        max_results = int(body.get("max_results") or 20)
    except (TypeError, ValueError):
        max_results = 20
    try:
        result = smart_search(
            project,
            query=query,
            scope=scope,
            selected_file=selected_file,
            include_orphans=include_orphans,
            include_extra=include_extra,
            include_config=include_config,
            use_small_model=use_small_model,
            max_results=max_results,
        )
    except Exception as exc:
        logger.exception("api_smart_search failed: %s", exc)
        return JsonResponse({"error": str(exc)}, status=500)
    return JsonResponse(result)


@csrf_exempt
@require_http_methods(["GET"])
def api_prep_check(request: HttpRequest, project_id: int) -> JsonResponse:
    """Return enforcement mode and preparation freshness for a project.

    Called by the MCP server before enforced write/read tools.
    Query param: preparation_id (optional).
    """
    user = get_api_user(request)
    if not user:
        return _unauthorized()
    project = _project_with_owner(project_id, user)
    preparation_id = (request.GET.get("preparation_id") or "").strip() or None
    freshness = check_preparation_freshness(project, preparation_id)
    return JsonResponse({
        "fresh": freshness["fresh"],
        "fresh_reason": freshness.get("reason", ""),
        "last_prep_mode": freshness.get("mode", "none"),
        "context_bundle_present": freshness.get("context_bundle_present", False),
    })
