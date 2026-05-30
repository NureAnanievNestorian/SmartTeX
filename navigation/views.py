from __future__ import annotations

import json

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.auth_helpers import get_api_user
from projects.views import _project_with_owner

from .services.enforcement import check_preparation_freshness
from .services.preparation import prepare_document_work


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
    )
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
