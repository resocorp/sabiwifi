"""Update pricing copy: install ETA on Home Internet, audience hint on Building Hotspot."""
from django.db import migrations


UPDATES = [
    {
        'category': 'home',
        'features': "Unlimited data\nUp to 25 Mbps\nUnlimited devices\nInstall in 24-72 hrs\n24/7 support",
    },
    {
        'category': 'cluster',
        'tagline': 'Managed WiFi for hostels, lodges, and locations of 50+ residents.',
    },
]


def apply_updates(apps, schema_editor):
    PricingPlan = apps.get_model('leads', 'PricingPlan')
    for entry in UPDATES:
        PricingPlan.objects.filter(category=entry['category']).update(
            **{k: v for k, v in entry.items() if k != 'category'}
        )


def revert_updates(apps, schema_editor):
    PricingPlan = apps.get_model('leads', 'PricingPlan')
    PricingPlan.objects.filter(category='home').update(
        features="Unlimited data\nUp to 25 Mbps\nUnlimited devices\nNext-day install\n24/7 support",
    )
    PricingPlan.objects.filter(category='cluster').update(
        tagline='Managed WiFi for hostels, lodges, large estates.',
    )


class Migration(migrations.Migration):
    dependencies = [
        ('leads', '0006_pricingplan_best_for'),
    ]
    operations = [
        migrations.RunPython(apply_updates, revert_updates),
    ]
