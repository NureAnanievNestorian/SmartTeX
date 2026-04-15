from django.urls import path

from .views import api_template_pdf, template_preview_page, templates_list_page

app_name = "templates_lib"

urlpatterns = [
    path("templates/", templates_list_page, name="list"),
    path("templates/<int:template_id>/preview/", template_preview_page, name="preview"),
    path("templates/<int:template_id>/pdf/", api_template_pdf, name="pdf"),
]
