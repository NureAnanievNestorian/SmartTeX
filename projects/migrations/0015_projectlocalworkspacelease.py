from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0014_local_runtime_jobs"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectLocalWorkspaceLease",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("workspace_id", models.CharField(db_index=True, max_length=128)),
                ("agent_id", models.CharField(blank=True, default="", max_length=128)),
                ("base_version_number", models.PositiveIntegerField(default=0)),
                ("last_seen_at", models.DateTimeField(auto_now=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="local_workspace_leases",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "project",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="local_workspace_lease",
                        to="projects.project",
                    ),
                ),
            ],
            options={"ordering": ["project_id"]},
        ),
    ]
