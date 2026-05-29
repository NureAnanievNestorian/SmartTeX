from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0009_projectversion_category'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='github_sync_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='project',
            name='github_repo_url',
            field=models.CharField(blank=True, default='', max_length=512),
        ),
        migrations.AddField(
            model_name='project',
            name='github_pat',
            field=models.CharField(blank=True, default='', max_length=256),
        ),
    ]
