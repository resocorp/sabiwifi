"""Seed initial supported countries."""
from django.db import migrations


COUNTRIES = [
    # (name, code, dial_code, local_prefix, local_phone_length, flag_emoji, sort_order)
    ('Nigeria',       'NG', '+234', '0', 11, '🇳🇬', 1),
    ('Ghana',         'GH', '+233', '0', 10, '🇬🇭', 2),
    ('Kenya',         'KE', '+254', '0', 10, '🇰🇪', 3),
    ('South Africa',  'ZA', '+27',  '0', 10, '🇿🇦', 4),
    ('Uganda',        'UG', '+256', '0', 10, '🇺🇬', 5),
    ('Tanzania',      'TZ', '+255', '0', 10, '🇹🇿', 6),
    ('Rwanda',        'RW', '+250', '0', 10, '🇷🇼', 7),
    ('Cameroon',      'CM', '+237', '0', 9,  '🇨🇲', 8),
]


def seed_countries(apps, schema_editor):
    Country = apps.get_model('accounts', 'Country')
    for name, code, dial_code, local_prefix, local_length, flag, order in COUNTRIES:
        Country.objects.get_or_create(
            code=code,
            defaults=dict(
                name=name,
                dial_code=dial_code,
                local_prefix=local_prefix,
                local_phone_length=local_length,
                flag_emoji=flag,
                sort_order=order,
                is_active=(code == 'NG'),  # Only Nigeria active initially
            ),
        )


def remove_countries(apps, schema_editor):
    Country = apps.get_model('accounts', 'Country')
    Country.objects.filter(code__in=[c[1] for c in COUNTRIES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_add_country_model'),
    ]

    operations = [
        migrations.RunPython(seed_countries, remove_countries),
    ]
