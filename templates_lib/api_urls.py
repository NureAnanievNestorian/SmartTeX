from django.urls import path

from .views import api_template_detail, api_templates_list

urlpatterns = [
    path("templates/", api_templates_list),
    path("templates/<int:template_id>/", api_template_detail),
]
