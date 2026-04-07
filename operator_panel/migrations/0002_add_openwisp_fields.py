from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('operator_panel', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='platformsettings',
            name='openwisp_api_url',
            field=models.CharField(
                blank=True, default='', max_length=255,
                help_text='OpenWISP controller base URL, e.g. https://wisp.sabiwifi.com',
            ),
        ),
        migrations.AddField(
            model_name='platformsettings',
            name='openwisp_api_token',
            field=models.CharField(
                blank=True, default='', max_length=255,
                help_text='OpenWISP REST API bearer token (from OpenWISP admin → API tokens)',
            ),
        ),
        migrations.AddField(
            model_name='platformsettings',
            name='openwisp_shared_secret',
            field=models.CharField(
                blank=True, default='', max_length=128,
                help_text='Shared secret baked into firmware for device auto-registration with OpenWISP',
            ),
        ),
        migrations.AddField(
            model_name='platformsettings',
            name='openwisp_pool_org_id',
            field=models.UUIDField(
                null=True, blank=True,
                help_text='UUID of the OpenWISP "pool" organization where unclaimed devices land after first boot',
            ),
        ),
    ]
