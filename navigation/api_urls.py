from django.urls import path

from .views import api_prepare_document_work

urlpatterns = [
    path("projects/<int:project_id>/navigation/prepare/", api_prepare_document_work),
]
