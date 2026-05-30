from django.urls import path

from .views import api_prep_check, api_prepare_document_work, api_smart_search

urlpatterns = [
    path("projects/<int:project_id>/navigation/prepare/", api_prepare_document_work),
    path("projects/<int:project_id>/navigation/prep-check/", api_prep_check),
    path("projects/<int:project_id>/navigation/search/", api_smart_search),
]
