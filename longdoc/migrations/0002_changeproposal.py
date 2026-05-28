from datetime import timedelta

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("longdoc", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ChangeProposal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("goal", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("validating", "Validating"),
                            ("failed_validation", "Failed Validation"),
                            ("failed_compile", "Failed Compile"),
                            ("ready_for_review", "Ready for Review"),
                            ("accepted", "Accepted"),
                            ("discarded", "Discarded"),
                            ("expired", "Expired"),
                        ],
                        default="validating",
                        max_length=30,
                    ),
                ),
                (
                    "validation_status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("passed", "Passed"), ("failed", "Failed")],
                        default="pending",
                        max_length=20,
                    ),
                ),
                (
                    "compile_status",
                    models.CharField(
                        choices=[("not_run", "Not Run"), ("success", "Success"), ("error", "Error")],
                        default="not_run",
                        max_length=20,
                    ),
                ),
                ("compile_error_summary", models.TextField(blank=True)),
                ("graph_validation_errors", models.JSONField(blank=True, default=list)),
                ("user_visible_message", models.TextField(blank=True)),
                ("patch_ops", models.JSONField(blank=True, default=list)),
                ("changed_files", models.JSONField(blank=True, default=list)),
                ("diff_summary", models.TextField(blank=True)),
                ("created_by", models.CharField(choices=[("mcp", "MCP"), ("user", "User")], default="mcp", max_length=20)),
                ("expires_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("discarded_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "addresses_outline_item",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="change_proposals",
                        to="longdoc.projectoutlineitem",
                    ),
                ),
                (
                    "addresses_task",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="change_proposals",
                        to="longdoc.projecttask",
                    ),
                ),
                (
                    "internal_session",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="change_proposal",
                        to="longdoc.aisession",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="change_proposals",
                        to="projects.project",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="changeproposal",
            index=models.Index(fields=["project", "status"], name="longdoc_cha_project_098b91_idx"),
        ),
        migrations.AddIndex(
            model_name="changeproposal",
            index=models.Index(fields=["expires_at"], name="longdoc_cha_expires_7d7848_idx"),
        ),
        migrations.AddConstraint(
            model_name="changeproposal",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("status__in", ("draft", "validating", "failed_validation", "failed_compile", "ready_for_review"))
                ),
                fields=("project",),
                name="longdoc_one_locking_change_proposal_per_project",
            ),
        ),
    ]
