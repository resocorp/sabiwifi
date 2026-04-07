from django.db import migrations, models


class Migration(migrations.Migration):
    """Add OpenWISP fields to the simple_history historical table."""

    dependencies = [
        ('operator_panel', '0002_add_openwisp_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='historicalplatformsettings',
            name='openwisp_api_url',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='historicalplatformsettings',
            name='openwisp_api_token',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='historicalplatformsettings',
            name='openwisp_shared_secret',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='historicalplatformsettings',
            name='openwisp_pool_org_id',
            field=models.UUIDField(null=True, blank=True),
        ),
    ]
