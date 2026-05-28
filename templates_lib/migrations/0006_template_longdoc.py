from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("templates_lib", "0005_template_main_file"),
    ]

    operations = [
        migrations.CreateModel(
            name="TemplateLongDocDefaults",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("enabled", models.BooleanField(default=True)),
                ("context_enabled", models.BooleanField(default=True)),
                ("outline_enabled", models.BooleanField(default=True)),
                ("tasks_enabled", models.BooleanField(default=True)),
                ("notes_enabled", models.BooleanField(default=True)),
                ("summaries_enabled", models.BooleanField(default=True)),
                ("requirements_enabled", models.BooleanField(default=False)),
                ("ai_sessions_enabled", models.BooleanField(default=True)),
                ("template", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="longdoc_defaults", to="templates_lib.template")),
            ],
            options={
                "verbose_name": "Template long-document defaults",
                "verbose_name_plural": "Template long-document defaults",
            },
        ),
        migrations.CreateModel(
            name="TemplateOutlineItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order", models.PositiveIntegerField(default=0)),
                ("title", models.CharField(max_length=500)),
                ("level", models.PositiveSmallIntegerField(default=1)),
                ("status", models.CharField(default="missing", max_length=20)),
                ("expected_pages", models.DecimalField(blank=True, decimal_places=1, max_digits=5, null=True)),
                ("notes", models.TextField(blank=True)),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="outline_items", to="templates_lib.template")),
            ],
            options={
                "ordering": ["template_id", "order"],
                "unique_together": {("template", "order")},
            },
        ),
        migrations.CreateModel(
            name="TemplateRequirement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("req_id", models.CharField(max_length=50)),
                ("description", models.TextField()),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="requirements", to="templates_lib.template")),
            ],
            options={
                "ordering": ["template_id", "req_id"],
                "unique_together": {("template", "req_id")},
            },
        ),
        migrations.CreateModel(
            name="TemplateContextFile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("filename", models.CharField(max_length=255)),
                ("display_name", models.CharField(blank=True, max_length=255)),
                ("description", models.TextField(blank=True)),
                ("content", models.TextField()),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="context_files", to="templates_lib.template")),
            ],
            options={
                "ordering": ["template_id", "filename"],
                "unique_together": {("template", "filename")},
            },
        ),
        migrations.CreateModel(
            name="TemplateTask",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("description", models.TextField()),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tasks", to="templates_lib.template")),
            ],
            options={
                "ordering": ["template_id", "id"],
            },
        ),
        migrations.CreateModel(
            name="TemplateNoteSection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("heading", models.CharField(max_length=255)),
                ("body", models.TextField(blank=True)),
                ("order", models.PositiveIntegerField(default=0)),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="note_sections", to="templates_lib.template")),
            ],
            options={
                "ordering": ["template_id", "order"],
                "unique_together": {("template", "heading")},
            },
        ),
    ]
