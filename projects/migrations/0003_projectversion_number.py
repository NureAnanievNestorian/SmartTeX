from django.db import migrations, models


def assign_version_numbers(apps, schema_editor):
    ProjectVersion = apps.get_model("projects", "ProjectVersion")
    project_ids = ProjectVersion.objects.values_list("project_id", flat=True).distinct()
    for project_id in project_ids:
        versions = list(
            ProjectVersion.objects.filter(project_id=project_id)
            .order_by("created_at", "id")
        )
        for i, v in enumerate(versions, start=1):
            v.number = i
            v.save(update_fields=["number"])


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0002_projectversion"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectversion",
            name="number",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.RunPython(assign_version_numbers, migrations.RunPython.noop),
    ]
