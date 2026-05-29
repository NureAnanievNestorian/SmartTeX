from django.db import migrations, models


def copy_longdoc_defaults_to_template(apps, schema_editor):
    TemplateLongDocDefaults = apps.get_model("templates_lib", "TemplateLongDocDefaults")
    Template = apps.get_model("templates_lib", "Template")
    field_map = {
        "enabled": "longdoc_enabled",
        "context_enabled": "longdoc_context_enabled",
        "outline_enabled": "longdoc_outline_enabled",
        "tasks_enabled": "longdoc_tasks_enabled",
        "notes_enabled": "longdoc_notes_enabled",
        "summaries_enabled": "longdoc_summaries_enabled",
        "requirements_enabled": "longdoc_requirements_enabled",
        "ai_sessions_enabled": "longdoc_ai_sessions_enabled",
    }
    for defaults in TemplateLongDocDefaults.objects.select_related("template"):
        tmpl = defaults.template
        for src, dst in field_map.items():
            setattr(tmpl, dst, getattr(defaults, src))
        tmpl.save(update_fields=list(field_map.values()))


class Migration(migrations.Migration):

    dependencies = [
        ("templates_lib", "0006_template_longdoc"),
    ]

    operations = [
        migrations.AddField(
            model_name="template",
            name="longdoc_enabled",
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="template",
            name="longdoc_context_enabled",
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="template",
            name="longdoc_outline_enabled",
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="template",
            name="longdoc_tasks_enabled",
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="template",
            name="longdoc_notes_enabled",
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="template",
            name="longdoc_summaries_enabled",
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="template",
            name="longdoc_requirements_enabled",
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="template",
            name="longdoc_ai_sessions_enabled",
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
        migrations.RunPython(copy_longdoc_defaults_to_template, migrations.RunPython.noop),
        migrations.DeleteModel(
            name="TemplateLongDocDefaults",
        ),
    ]
