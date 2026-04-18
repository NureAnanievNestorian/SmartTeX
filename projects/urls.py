from django.urls import path

from .views import ai_connect_guide, create_project_from_dashboard, dashboard, editor, home

app_name = "projects"

urlpatterns = [
    path("", home, name="home"),
    path("dashboard/", dashboard, name="dashboard"),
    path("ai-connect/", ai_connect_guide, name="ai-connect"),
    path("projects/new/", create_project_from_dashboard, name="create"),
    path("projects/<int:project_id>/", editor, name="editor"),
]
