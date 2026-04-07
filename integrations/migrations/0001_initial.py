import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0004_add_signup_fee_fields'),
        ('routers', '0004_add_device_type'),
    ]

    operations = [
        migrations.CreateModel(
            name='OpenWISPBinding',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('openwisp_id', models.UUIDField(help_text='UUID of the corresponding object in OpenWISP')),
                ('openwisp_type', models.CharField(choices=[('org', 'Organization'), ('device', 'Device')], max_length=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('reseller', models.OneToOneField(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='openwisp_binding',
                    to='accounts.reseller',
                )),
                ('router', models.OneToOneField(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='openwisp_binding',
                    to='routers.router',
                )),
            ],
            options={'constraints': [
                models.CheckConstraint(
                    check=(
                        models.Q(reseller__isnull=False, router__isnull=True) |
                        models.Q(reseller__isnull=True, router__isnull=False)
                    ),
                    name='openwisp_binding_exactly_one_entity',
                ),
            ]},
        ),
    ]
