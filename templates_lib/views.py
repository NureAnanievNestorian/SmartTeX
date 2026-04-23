from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from accounts.auth_helpers import get_api_user

from .models import Template
from .services import has_template_pdf, template_pdf_path, template_pdf_url


def _template_payload(template: Template, with_content: bool = False) -> dict:
    payload = {
        "id": template.id,
        "title": template.title,
        "description": template.description,
        "category": template.category,
        "category_display": template.get_category_display(),
        "markup_type": template.markup_type,
        "created_at": template.created_at.isoformat(),
        "updated_at": template.updated_at.isoformat(),
    }
    if with_content:
        payload["content"] = template.content
    return payload


@require_GET
def api_templates_list(request: HttpRequest) -> JsonResponse:
    if not get_api_user(request):
        return JsonResponse({"detail": "Authentication required"}, status=401)
    def _parse_int(v: str | None) -> int | None:
        if v is None or str(v).strip() == "":
            return None
        return int(v)

    try:
        limit = _parse_int(request.GET.get("limit"))
        before_id = _parse_int(request.GET.get("before_id"))
    except ValueError:
        return JsonResponse({"detail": "limit/before_id must be integers"}, status=400)

    qs = Template.objects.filter(is_active=True)
    if limit is None and before_id is None:
        data = [_template_payload(t) for t in qs]
        return JsonResponse(data, safe=False)

    safe_limit = max(1, min(int(limit or 24), 120))
    if before_id is not None:
        qs = qs.filter(id__lt=before_id)
    rows = list(qs.order_by("-id")[: safe_limit + 1])
    has_more = len(rows) > safe_limit
    items = rows[:safe_limit]
    data = [_template_payload(t) for t in items]
    next_before_id = items[-1].id if has_more and items else None
    return JsonResponse({"templates": data, "has_more": has_more, "next_before_id": next_before_id})


@require_GET
def api_template_detail(request: HttpRequest, template_id: int) -> JsonResponse:
    if not get_api_user(request):
        return JsonResponse({"detail": "Authentication required"}, status=401)
    template = get_object_or_404(Template, id=template_id, is_active=True)
    return JsonResponse(_template_payload(template, with_content=True))


@login_required
@require_GET
def template_preview_page(request: HttpRequest, template_id: int):
    template = get_object_or_404(Template, id=template_id, is_active=True)
    pdf_exists = has_template_pdf(template)
    pdf_url = template_pdf_url(template) if pdf_exists else None
    return render(request, "templates_lib/preview.html", {
        "template_obj": template,
        "pdf_exists": pdf_exists,
        "pdf_url": pdf_url,
    })


@login_required
@require_GET
def templates_list_page(request: HttpRequest):
    page_size = 24
    rows = list(Template.objects.filter(is_active=True).order_by("-id")[: page_size + 1])
    has_more = len(rows) > page_size
    templates = rows[:page_size]
    next_before_id = templates[-1].id if has_more and templates else None
    templates_count = Template.objects.filter(is_active=True).count()
    return render(
        request,
        "templates_lib/list.html",
        {
            "templates": templates,
            "templates_count": templates_count,
            "templates_has_more": has_more,
            "templates_next_before_id": next_before_id,
        },
    )


@login_required
@require_GET
def api_template_pdf(request: HttpRequest, template_id: int) -> FileResponse:
    template = get_object_or_404(Template, id=template_id, is_active=True)
    pdf_path = template_pdf_path(template)
    if not pdf_path.exists():
        raise Http404("PDF preview not yet generated")
    return FileResponse(open(pdf_path, "rb"), content_type="application/pdf")
