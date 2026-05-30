import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("projects", "0011_project_github_sync_interval"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectNavigationIndex",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("schema_version", models.PositiveSmallIntegerField(default=1)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("building", "Building"), ("ready", "Ready"), ("partial", "Partial"), ("failed", "Failed")], default="pending", max_length=16)),
                ("markup_type_snapshot", models.CharField(blank=True, default="", max_length=16)),
                ("main_file_snapshot", models.CharField(blank=True, default="", max_length=255)),
                ("entrypoint_file", models.CharField(blank=True, default="", max_length=255)),
                ("last_built_at", models.DateTimeField(blank=True, null=True)),
                ("last_built_version_number", models.PositiveIntegerField(default=0)),
                ("last_partial_refresh_at", models.DateTimeField(blank=True, null=True)),
                ("coverage", models.JSONField(blank=True, default=dict)),
                ("build_error", models.TextField(blank=True, default="")),
                ("notes", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("project", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="navigation_index", to="projects.project")),
            ],
        ),
        migrations.CreateModel(
            name="FileCard",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("filename", models.CharField(max_length=512)),
                ("role", models.CharField(choices=[("entrypoint", "Entrypoint"), ("content_section", "Content section"), ("metadata", "Metadata"), ("style", "Style"), ("class", "Class"), ("bib", "Bibliography"), ("csl", "CSL"), ("config", "Config"), ("asset_metadata", "Asset metadata"), ("auxiliary", "Auxiliary"), ("unknown", "Unknown")], default="unknown", max_length=24)),
                ("role_source", models.CharField(choices=[("deterministic", "Deterministic"), ("small_model", "Small model")], default="deterministic", max_length=16)),
                ("role_confidence", models.CharField(choices=[("low", "Low"), ("medium", "Medium"), ("high", "High")], default="low", max_length=8)),
                ("state", models.CharField(choices=[("real", "Real"), ("demo", "Demo"), ("placeholder", "Placeholder"), ("unknown", "Unknown")], default="unknown", max_length=16)),
                ("state_source", models.CharField(choices=[("deterministic", "Deterministic"), ("small_model", "Small model")], default="deterministic", max_length=16)),
                ("state_confidence", models.CharField(choices=[("low", "Low"), ("medium", "Medium"), ("high", "High")], default="low", max_length=8)),
                ("reachability", models.CharField(choices=[("reachable", "Reachable"), ("orphan", "Orphan"), ("missing", "Missing"), ("dynamic_unresolved", "Dynamic unresolved"), ("excluded", "Excluded")], default="orphan", max_length=24)),
                ("included_by_filenames", models.JSONField(blank=True, default=list)),
                ("includes_out_filenames", models.JSONField(blank=True, default=list)),
                ("summary", models.CharField(blank=True, default="", max_length=280)),
                ("summary_source", models.CharField(choices=[("deterministic", "Deterministic"), ("small_model", "Small model")], default="deterministic", max_length=16)),
                ("summary_confidence", models.CharField(choices=[("low", "Low"), ("medium", "Medium"), ("high", "High")], default="low", max_length=8)),
                ("edit_triggers", models.JSONField(blank=True, default=list)),
                ("triggers_source", models.CharField(choices=[("deterministic", "Deterministic"), ("small_model", "Small model")], default="deterministic", max_length=16)),
                ("line_count", models.PositiveIntegerField(default=0)),
                ("byte_size", models.PositiveIntegerField(default=0)),
                ("content_hash", models.CharField(blank=True, default="", max_length=64)),
                ("last_version_number", models.PositiveIntegerField(default=0)),
                ("last_indexed_at", models.DateTimeField(blank=True, null=True)),
                ("is_stale", models.BooleanField(default=False)),
                ("exclusion_reason", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("index", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="file_cards", to="navigation.projectnavigationindex")),
            ],
            options={
                "indexes": [
                    models.Index(fields=["index", "is_stale"], name="navigation__index_i_5c1555_idx"),
                    models.Index(fields=["index", "role"], name="navigation__index_i_c468d5_idx"),
                    models.Index(fields=["index", "reachability"], name="navigation__index_i_10d73e_idx"),
                    models.Index(fields=["index", "content_hash"], name="navigation__index_i_d0119f_idx"),
                ],
                "unique_together": {("index", "filename")},
            },
        ),
        migrations.CreateModel(
            name="RegionCard",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("region_kind", models.CharField(choices=[("heading_section", "Heading section"), ("metadata_block", "Metadata block"), ("front_matter", "Front matter"), ("bibliography_block", "Bibliography block"), ("code_block", "Code block"), ("figure_block", "Figure block"), ("unknown", "Unknown")], default="unknown", max_length=24)),
                ("title", models.CharField(blank=True, default="", max_length=512)),
                ("level", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("order", models.PositiveIntegerField(default=0)),
                ("line_start", models.PositiveIntegerField(default=1)),
                ("line_end", models.PositiveIntegerField(default=1)),
                ("state", models.CharField(choices=[("real", "Real"), ("demo", "Demo"), ("placeholder", "Placeholder"), ("unknown", "Unknown")], default="unknown", max_length=16)),
                ("state_source", models.CharField(choices=[("deterministic", "Deterministic"), ("small_model", "Small model")], default="deterministic", max_length=16)),
                ("state_confidence", models.CharField(choices=[("low", "Low"), ("medium", "Medium"), ("high", "High")], default="low", max_length=8)),
                ("summary", models.CharField(blank=True, default="", max_length=280)),
                ("summary_source", models.CharField(choices=[("deterministic", "Deterministic"), ("small_model", "Small model")], default="deterministic", max_length=16)),
                ("summary_confidence", models.CharField(choices=[("low", "Low"), ("medium", "Medium"), ("high", "High")], default="low", max_length=8)),
                ("edit_triggers", models.JSONField(blank=True, default=list)),
                ("triggers_source", models.CharField(choices=[("deterministic", "Deterministic"), ("small_model", "Small model")], default="deterministic", max_length=16)),
                ("content_hash", models.CharField(blank=True, default="", max_length=64)),
                ("last_indexed_at", models.DateTimeField(blank=True, null=True)),
                ("is_stale", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("file_card", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="region_cards", to="navigation.filecard")),
            ],
            options={
                "ordering": ["file_card_id", "order"],
                "indexes": [
                    models.Index(fields=["file_card", "is_stale"], name="navigation__file_ca_6fb9f7_idx"),
                    models.Index(fields=["file_card", "region_kind"], name="navigation__file_ca_246c4a_idx"),
                    models.Index(fields=["file_card", "content_hash"], name="navigation__file_ca_972713_idx"),
                ],
                "unique_together": {("file_card", "order")},
            },
        ),
    ]
