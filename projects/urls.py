from django.urls import path

from .views import create_project_from_dashboard, dashboard, editor, home

app_name = "projects"

urlpatterns = [
    path("", home, name="home"),
    path("dashboard/", dashboard, name="dashboard"),
    path("projects/new/", create_project_from_dashboard, name="create"),
    path("projects/<int:project_id>/", editor, name="editor"),
]
