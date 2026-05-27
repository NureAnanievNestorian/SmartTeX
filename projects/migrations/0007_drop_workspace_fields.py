from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0006_typst_project_mode_and_version_metadata"),
    ]

    operations = [
        migrations.RunSQL(
            sql='ALTER TABLE projects_project DROP COLUMN IF EXISTS document_metadata; '
                'ALTER TABLE projects_project DROP COLUMN IF EXISTS project_mode;',
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
