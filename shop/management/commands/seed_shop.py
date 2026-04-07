"""Seed shop with sample products and shipping zones for testing."""
from django.core.management.base import BaseCommand
from shop.models import Category, Product, ShippingZone


class Command(BaseCommand):
    help = 'Seed shop with sample products and shipping zones'

    def handle(self, *args, **options):
        # Shipping zones
        zones = [
            ('Lagos', ['Lagos'], 1500),
            ('South West', ['Ogun', 'Oyo', 'Osun', 'Ondo', 'Ekiti'], 2000),
            ('South East', ['Anambra', 'Enugu', 'Imo', 'Abia', 'Ebonyi'], 2500),
            ('South South', ['Rivers', 'Delta', 'Edo', 'Cross River', 'Bayelsa', 'Akwa Ibom'], 2500),
            ('North Central', ['FCT', 'Kogi', 'Benue', 'Niger', 'Kwara', 'Plateau', 'Nasarawa'], 3000),
            ('North West', ['Kano', 'Kaduna', 'Katsina', 'Kebbi', 'Sokoto', 'Zamfara', 'Jigawa'], 3500),
            ('North East', ['Borno', 'Adamawa', 'Taraba', 'Yobe', 'Bauchi', 'Gombe'], 3500),
        ]
        for name, states, rate in zones:
            obj, created = ShippingZone.objects.get_or_create(
                name=name,
                defaults={'states': states, 'flat_rate_ngn': rate},
            )
            if created:
                self.stdout.write(f'  Zone: {name} — ₦{rate:,}')

        # Categories
        routers_cat, _ = Category.objects.get_or_create(
            slug='routers', defaults={'name': 'Routers', 'sort_order': 1}
        )
        accessories_cat, _ = Category.objects.get_or_create(
            slug='accessories', defaults={'name': 'Accessories', 'sort_order': 2}
        )
        cables_cat, _ = Category.objects.get_or_create(
            slug='cables', defaults={'name': 'Cables & Connectors', 'sort_order': 3}
        )

        # Products
        products = [
            {
                'category': routers_cat,
                'name': 'MikroTik hAP ac²',
                'slug': 'mikrotik-hap-ac2',
                'description': 'Dual-band home access point with 5× Ethernet ports, 2.4 GHz and 5 GHz radios. Ideal for small-to-medium hotspot deployments.\n\nIncludes: Router, power adapter, quick guide.',
                'price_ngn': 45000,
                'stock': 12,
                'is_featured': True,
                'primary_image_url': 'https://i.mt.lv/cdn/rb_images/hap_ac2/main.jpg',
            },
            {
                'category': routers_cat,
                'name': 'MikroTik hEX S',
                'slug': 'mikrotik-hex-s',
                'description': '5-port Gigabit Ethernet router with SFP port and PoE output. Perfect for WireGuard VPN tunnels and RADIUS authentication.\n\nIncludes: Router, power adapter.',
                'price_ngn': 55000,
                'stock': 8,
                'is_featured': True,
                'primary_image_url': 'https://i.mt.lv/cdn/rb_images/RB760iGS/main.jpg',
            },
            {
                'category': routers_cat,
                'name': 'MikroTik wAP ac',
                'slug': 'mikrotik-wap-ac',
                'description': 'Outdoor-capable dual-band access point. Weatherproof and suitable for mounting on walls or poles.\n\nIncludes: Access point, PoE injector, mounting kit.',
                'price_ngn': 38000,
                'stock': 15,
                'primary_image_url': 'https://i.mt.lv/cdn/rb_images/RBwAPG-5HacT2HnD/main.jpg',
            },
            {
                'category': accessories_cat,
                'name': 'PoE Injector 48V',
                'slug': 'poe-injector-48v',
                'description': 'Passive PoE injector for powering MikroTik access points over Ethernet cable. Input: 100-240V AC. Output: 48V DC.',
                'price_ngn': 4500,
                'stock': 30,
                'primary_image_url': '',
            },
            {
                'category': accessories_cat,
                'name': 'Outdoor Weatherproof Enclosure',
                'slug': 'outdoor-enclosure',
                'description': 'IP65-rated enclosure for mounting routers outdoors. Fits most MikroTik RB series boards. Includes cable gland and mounting brackets.',
                'price_ngn': 8500,
                'stock': 20,
                'primary_image_url': '',
            },
            {
                'category': cables_cat,
                'name': 'Cat6 UTP Cable (305m box)',
                'slug': 'cat6-utp-305m',
                'description': 'Full 305-metre box of Cat6 UTP Ethernet cable. Suitable for indoor and light outdoor runs. 23 AWG solid copper conductors.',
                'price_ngn': 28000,
                'stock': 10,
                'primary_image_url': '',
            },
            {
                'category': cables_cat,
                'name': 'RJ45 Crimping Kit',
                'slug': 'rj45-crimping-kit',
                'description': 'Complete kit: crimping tool, cable stripper, RJ45 connectors (×100), and cable tester. Everything you need for custom-length Ethernet cables.',
                'price_ngn': 6500,
                'stock': 25,
                'primary_image_url': '',
            },
        ]

        for p_data in products:
            _, created = Product.objects.get_or_create(
                slug=p_data['slug'],
                defaults=p_data,
            )
            if created:
                self.stdout.write(f'  Product: {p_data["name"]}')

        self.stdout.write(self.style.SUCCESS('Shop seeded successfully.'))
