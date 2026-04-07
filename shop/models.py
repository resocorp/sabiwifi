"""Shop models — products, orders, shipping zones."""
import io
import os
import secrets
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models
from PIL import Image as PILImage

MAX_IMAGE_MB = 5


def validate_image_size(value):
    if value.size > MAX_IMAGE_MB * 1024 * 1024:
        raise ValidationError(f'Image must be under {MAX_IMAGE_MB}MB. This file is {value.size / 1024 / 1024:.1f}MB.')


def _optimize_image(image_field, max_dim=800, quality=80):
    """Resize to max_dim, convert to WebP, and replace the file in-place."""
    if not image_field:
        return
    try:
        img = PILImage.open(image_field)
    except Exception:
        return

    # Convert palette / RGBA correctly
    if img.mode in ('P', 'RGBA', 'LA'):
        img = img.convert('RGBA')
        # Composite onto white background for non-transparent WebP
        bg = PILImage.new('RGBA', img.size, (255, 255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg.convert('RGB')
    elif img.mode != 'RGB':
        img = img.convert('RGB')

    # Resize if larger than max_dim on either side
    img.thumbnail((max_dim, max_dim), PILImage.LANCZOS)

    # Encode as WebP
    buf = io.BytesIO()
    img.save(buf, format='WEBP', quality=quality, method=4)
    buf.seek(0)

    # Replace the file with .webp version — use basename only since
    # upload_to will prepend the directory on save
    basename = os.path.basename(image_field.name)
    new_name = os.path.splitext(basename)[0] + '.webp'
    image_field.save(new_name, ContentFile(buf.read()), save=False)


NIGERIAN_STATES = [
    'Abia', 'Adamawa', 'Akwa Ibom', 'Anambra', 'Bauchi', 'Bayelsa',
    'Benue', 'Borno', 'Cross River', 'Delta', 'Ebonyi', 'Edo', 'Ekiti',
    'Enugu', 'FCT', 'Gombe', 'Imo', 'Jigawa', 'Kaduna', 'Kano', 'Katsina',
    'Kebbi', 'Kogi', 'Kwara', 'Lagos', 'Nasarawa', 'Niger', 'Ogun', 'Ondo',
    'Osun', 'Oyo', 'Plateau', 'Rivers', 'Sokoto', 'Taraba', 'Yobe', 'Zamfara',
]

STATE_CHOICES = [(s, s) for s in NIGERIAN_STATES]


class Category(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name_plural = 'Categories'
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name


class Product(models.Model):
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='products'
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    price_ngn = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    primary_image = models.ImageField(
        upload_to='shop/products/', blank=True,
        validators=[validate_image_size],
        help_text='Max 5MB. Auto-converted to optimised WebP.')
    primary_image_url = models.URLField(max_length=500, blank=True,
        help_text='Legacy — use primary_image instead')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_featured', 'name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        is_new_image = False
        if self.pk:
            try:
                old = Product.objects.get(pk=self.pk)
                is_new_image = old.primary_image != self.primary_image
            except Product.DoesNotExist:
                is_new_image = bool(self.primary_image)
        else:
            is_new_image = bool(self.primary_image)

        if is_new_image and self.primary_image:
            _optimize_image(self.primary_image)
        super().save(*args, **kwargs)

    @property
    def in_stock(self):
        return self.stock > 0

    @property
    def image_url(self):
        """Return the best available image URL (uploaded file takes priority)."""
        if self.primary_image:
            return self.primary_image.url
        return self.primary_image_url or ''

    @property
    def price_display(self):
        return f'₦{self.price_ngn:,.0f}'


class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(
        upload_to='shop/gallery/', blank=True,
        validators=[validate_image_size],
        help_text='Max 5MB. Auto-converted to optimised WebP.')
    image_url = models.URLField(max_length=500, blank=True,
        help_text='Legacy — use image field instead')
    caption = models.CharField(max_length=200, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    def save(self, *args, **kwargs):
        is_new_image = False
        if self.pk:
            try:
                old = ProductImage.objects.get(pk=self.pk)
                is_new_image = old.image != self.image
            except ProductImage.DoesNotExist:
                is_new_image = bool(self.image)
        else:
            is_new_image = bool(self.image)

        if is_new_image and self.image:
            _optimize_image(self.image)
        super().save(*args, **kwargs)

    @property
    def url(self):
        """Return the best available image URL."""
        if self.image:
            return self.image.url
        return self.image_url or ''

    class Meta:
        ordering = ['sort_order']

    def __str__(self):
        return f'{self.product.name} — image {self.sort_order + 1}'


class CrossSell(models.Model):
    """Products shown as "You may also like" on a product detail page."""
    from_product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='cross_sells'
    )
    to_product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='cross_sold_by'
    )

    class Meta:
        unique_together = ('from_product', 'to_product')

    def __str__(self):
        return f'{self.from_product.name} → {self.to_product.name}'


class ShippingZone(models.Model):
    name = models.CharField(max_length=100, help_text='e.g. Lagos, South West, Nationwide')
    states = models.JSONField(
        default=list,
        help_text='List of state names covered by this zone (e.g. ["Lagos", "Ogun"])'
    )
    flat_rate_ngn = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def __str__(self):
        return f'{self.name} — ₦{self.flat_rate_ngn:,.0f}'


ORDER_STATUS_CHOICES = [
    ('pending', 'Pending Payment'),
    ('paid', 'Paid'),
    ('processing', 'Processing'),
    ('shipped', 'Shipped'),
    ('delivered', 'Delivered'),
    ('cancelled', 'Cancelled'),
]


class Order(models.Model):
    reference = models.CharField(max_length=60, unique=True, db_index=True)

    # Customer
    customer_name = models.CharField(max_length=200)
    customer_email = models.EmailField()
    customer_phone = models.CharField(max_length=20)

    # Delivery
    address_line1 = models.CharField(max_length=255)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=50)

    # Pricing
    shipping_zone = models.ForeignKey(
        ShippingZone, on_delete=models.SET_NULL, null=True, blank=True
    )
    subtotal_ngn = models.DecimalField(max_digits=10, decimal_places=2)
    shipping_ngn = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_ngn = models.DecimalField(max_digits=10, decimal_places=2)

    # Payment
    paystack_reference = models.CharField(max_length=100, blank=True)
    paystack_status = models.CharField(max_length=20, default='pending')

    # Fulfillment
    status = models.CharField(max_length=20, choices=ORDER_STATUS_CHOICES, default='pending')
    tracking_number = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Order {self.reference} — {self.customer_name}'

    @classmethod
    def generate_reference(cls):
        return f'swshop_{secrets.token_hex(10)}'


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True
    )
    product_name = models.CharField(max_length=200)    # snapshot
    product_price_ngn = models.DecimalField(max_digits=10, decimal_places=2)  # snapshot
    quantity = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f'{self.quantity}× {self.product_name}'

    @property
    def line_total(self):
        return self.product_price_ngn * self.quantity
