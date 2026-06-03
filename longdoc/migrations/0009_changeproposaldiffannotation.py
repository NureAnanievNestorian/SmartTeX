import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("longdoc", "0008_projectannotation_ai_draft"),
    ]

    operations = [
        migrations.CreateModel(
            name="ChangeProposalDiffAnnotation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file_name", models.CharField(max_length=500)),
                ("side", models.CharField(choices=[("old", "Old"), ("new", "New"), ("context", "Context")], default="new", max_length=20)),
                ("line_number", models.PositiveIntegerField()),
                ("selected_text", models.TextField(blank=True)),
                ("instruction", models.TextField()),
                ("status", models.CharField(choices=[("open", "Open"), ("done", "Done"), ("dismissed", "Dismissed")], default="open", max_length=20)),
                ("created_by", models.CharField(choices=[("user", "User"), ("mcp", "MCP")], default="user", max_length=20)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "proposal",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="diff_annotations", to="longdoc.changeproposal"),
                ),
                (
                    "resolved_by_session",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="resolved_diff_annotations",
                        to="longdoc.aisession",
                    ),
                ),
            ],
            options={
                "ordering": ["proposal_id", "status", "file_name", "line_number", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="changeproposaldiffannotation",
            index=models.Index(fields=["proposal", "status"], name="longdoc_cha_proposa_e5c113_idx"),
        ),
        migrations.AddIndex(
            model_name="changeproposaldiffannotation",
            index=models.Index(fields=["proposal", "file_name", "line_number"], name="longdoc_cha_proposa_22dd0f_idx"),
        ),
    ]
