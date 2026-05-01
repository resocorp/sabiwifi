"""Seed initial PricingPlan rows so the public landing has cards on day one.
Idempotent — only creates rows that don't already exist for a category."""
from django.db import migrations
from decimal import Decimal


SEED_PLANS = [
    {
        'category': 'home',
        'name': 'Home Internet',
        'tagline': 'Reliable WiFi for your home or home-office.',
        'upfront_label': 'Installation',
        'upfront_price_ngn': Decimal('100000'),
        'upfront_discount_price_ngn': Decimal('50000'),
        'monthly_price_ngn': Decimal('25000'),
        'features': "Unlimited data\nUp to 25 Mbps\nUnlimited devices\nNext-day install\n24/7 support",
        'sort_order': 10,
    },
    {
        'category': 'sme',
        'name': 'SME Internet',
        'tagline': 'For hotels, small factories, and businesses.',
        'upfront_label': 'Installation',
        'upfront_price_ngn': Decimal('150000'),
        'upfront_discount_price_ngn': Decimal('75000'),
        'monthly_price_ngn': Decimal('40000'),
        'features': "Unlimited data\nUp to 50 Mbps\nUnlimited devices\nPriority support\nFree site survey",
        'sort_order': 20,
    },
    {
        'category': 'cluster',
        'name': 'Building Hotspot',
        'tagline': 'Managed WiFi for hostels, lodges, large estates.',
        'upfront_label': 'Account opening',
        'upfront_price_ngn': Decimal('5000'),
        'upfront_discount_price_ngn': None,
        'monthly_price_ngn': Decimal('6000'),
        'features': "Unlimited data\n1 device per account\n1 month validity\nResident pay-as-you-go\nWe handle rollout",
        'sort_order': 30,
    },
]


def seed_plans(apps, schema_editor):
    PricingPlan = apps.get_model('leads', 'PricingPlan')
    for entry in SEED_PLANS:
        if PricingPlan.objects.filter(category=entry['category']).exists():
            continue
        PricingPlan.objects.create(**entry)


def unseed_plans(apps, schema_editor):
    PricingPlan = apps.get_model('leads', 'PricingPlan')
    for entry in SEED_PLANS:
        PricingPlan.objects.filter(
            category=entry['category'], name=entry['name'],
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('leads', '0004_pricingplan_lead_chosen_plan'),
    ]
    operations = [
        migrations.RunPython(seed_plans, unseed_plans),
    ]
