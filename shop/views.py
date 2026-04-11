"""Shop views — storefront pages and API endpoints."""
import hashlib
import hmac
import json
import logging
import secrets
from decimal import Decimal, InvalidOperation

import requests as http_requests
from django.conf import settings
from django.shortcuts import render, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from django.db import models
from shop.models import (
    Category, Product, ShippingZone, Order, OrderItem,
    NIGERIAN_STATES,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _get_paystack_keys():
    from billing.providers.paystack import get_paystack_keys
    return get_paystack_keys()


def _get_shipping_for_state(state):
    """Return (ShippingZone|None, rate_ngn) for a given state name."""
    zones = ShippingZone.objects.all()
    for zone in zones:
        if state in (zone.states or []):
            return zone, zone.flat_rate_ngn
    return None, Decimal('0')


# ─────────────────────────────────────────────
#  PAGE VIEWS
# ─────────────────────────────────────────────

def shop_home(request):
    """Shop homepage — product grid with optional category filter."""
    category_slug = request.GET.get('cat', '')
    categories = Category.objects.prefetch_related('products').all()
    products = Product.objects.filter(is_active=True).select_related('category')

    active_category = None
    if category_slug:
        active_category = Category.objects.filter(slug=category_slug).first()
        if active_category:
            products = products.filter(category=active_category)

    return render(request, 'shop/home.html', {
        'products': products,
        'categories': categories,
        'active_category': active_category,
    })


def shop_product(request, slug):
    """Product detail page."""
    product = get_object_or_404(Product, slug=slug, is_active=True)
    images = list(product.images.all())
    cross_sells = Product.objects.filter(
        cross_sold_by__from_product=product,
        is_active=True,
    ).distinct()[:4]
    return render(request, 'shop/product.html', {
        'product': product,
        'images': images,
        'cross_sells': cross_sells,
    })


def shop_cart(request):
    """Cart page — cart data lives in localStorage."""
    return render(request, 'shop/cart.html')


def shop_checkout(request):
    """Checkout page — form + Paystack inline."""
    _, pub_key = _get_paystack_keys()
    return render(request, 'shop/checkout.html', {
        'states': NIGERIAN_STATES,
        'paystack_public_key': pub_key,
    })


def shop_order(request, ref):
    """Order confirmation / status page."""
    order = get_object_or_404(Order, reference=ref)
    return render(request, 'shop/order.html', {'order': order})


# ─────────────────────────────────────────────
#  API VIEWS
# ─────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([AllowAny])
def api_shipping_rate(request):
    """Return shipping rate for a given state."""
    state = request.GET.get('state', '')
    zone, rate = _get_shipping_for_state(state)
    return Response({
        'state': state,
        'shipping_ngn': str(rate),
        'zone_name': zone.name if zone else '',
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def api_initiate_payment(request):
    """
    Create order from cart and initialize Paystack payment.

    Request body:
        cart: [{product_id, qty}]
        customer: {name, email, phone, address_line1, address_line2, city, state}
    """
    cart = request.data.get('cart', [])
    customer = request.data.get('customer', {})

    # Validate customer fields
    required = ['name', 'email', 'phone', 'address_line1', 'city', 'state']
    missing = [f for f in required if not customer.get(f, '').strip()]
    if missing:
        return Response({'error': f'Missing fields: {", ".join(missing)}'}, status=400)

    if not cart:
        return Response({'error': 'Cart is empty.'}, status=400)

    # Validate and price cart items
    line_items = []
    for item in cart:
        try:
            product = Product.objects.get(id=item['product_id'], is_active=True)
        except (Product.DoesNotExist, KeyError, TypeError):
            return Response({'error': f'Product not found: {item.get("product_id")}'}, status=400)

        qty = int(item.get('qty', 1))
        if qty < 1:
            continue
        if product.stock < qty:
            return Response({'error': f'"{product.name}" only has {product.stock} in stock.'}, status=400)
        line_items.append((product, qty))

    if not line_items:
        return Response({'error': 'No valid items in cart.'}, status=400)

    # Calculate totals
    subtotal = sum(p.price_ngn * qty for p, qty in line_items)
    state = customer['state'].strip()
    zone, shipping = _get_shipping_for_state(state)
    total = subtotal + shipping

    # Create Order
    reference = Order.generate_reference()
    order = Order.objects.create(
        reference=reference,
        customer_name=customer['name'].strip(),
        customer_email=customer['email'].strip().lower(),
        customer_phone=customer['phone'].strip(),
        address_line1=customer['address_line1'].strip(),
        address_line2=customer.get('address_line2', '').strip(),
        city=customer['city'].strip(),
        state=state,
        shipping_zone=zone,
        subtotal_ngn=subtotal,
        shipping_ngn=shipping,
        total_ngn=total,
        paystack_reference=reference,
    )
    for product, qty in line_items:
        OrderItem.objects.create(
            order=order,
            product=product,
            product_name=product.name,
            product_price_ngn=product.price_ngn,
            quantity=qty,
        )

    # Initialize Paystack (direct — no subaccount split)
    secret_key, pub_key = _get_paystack_keys()
    if not secret_key:
        logger.error('Paystack secret key not configured for shop payment.')
        order.delete()
        return Response({'error': 'Payment gateway not configured.'}, status=503)

    amount_kobo = int(total * 100)
    platform_domain = getattr(settings, 'PLATFORM_DOMAIN', 'sabiwifi.ng')

    try:
        resp = http_requests.post(
            'https://api.paystack.co/transaction/initialize',
            json={
                'amount': amount_kobo,
                'email': customer['email'].strip().lower(),
                'reference': reference,
                'callback_url': f'https://{platform_domain}/shop/order/{reference}/',
                'metadata': {
                    'order_reference': reference,
                    'customer_name': customer['name'].strip(),
                },
            },
            headers={
                'Authorization': f'Bearer {secret_key}',
                'Content-Type': 'application/json',
            },
            timeout=15,
        )
        body = resp.json()
    except Exception as exc:
        logger.error(f'Paystack shop init error: {exc}')
        order.delete()
        return Response({'error': 'Payment gateway unavailable. Please try again.'}, status=502)

    if not body.get('status'):
        logger.error(f'Paystack shop init failed: {body}')
        order.delete()
        return Response({'error': body.get('message', 'Could not initialize payment.')}, status=400)

    return Response({
        'reference': reference,
        'access_code': body['data']['access_code'],
        'public_key': pub_key,
        'amount_kobo': amount_kobo,
        'order_url': f'/shop/order/{reference}/',
    })


@api_view(['GET'])
@permission_classes([AllowAny])
def api_order_status(request, ref):
    """Return order status and items."""
    try:
        order = Order.objects.prefetch_related('items').get(reference=ref)
    except Order.DoesNotExist:
        return Response({'error': 'Order not found.'}, status=404)

    return Response({
        'reference': order.reference,
        'status': order.status,
        'paystack_status': order.paystack_status,
        'customer_name': order.customer_name,
        'total_ngn': str(order.total_ngn),
        'items': [
            {
                'name': item.product_name,
                'qty': item.quantity,
                'price': str(item.product_price_ngn),
                'line_total': str(item.line_total),
            }
            for item in order.items.all()
        ],
    })


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def api_shop_webhook(request):
    """
    Paystack webhook for shop orders.
    Verifies HMAC SHA-512 signature, marks order paid, decrements stock.
    """
    secret_key, _ = _get_paystack_keys()
    signature = request.headers.get('X-Paystack-Signature', '')

    if signature:
        if not secret_key:
            return Response({'error': 'Webhook not configured.'}, status=503)
        computed = hmac.new(
            secret_key.encode('utf-8'),
            request.body,
            hashlib.sha512,
        ).hexdigest()
        if not hmac.compare_digest(computed, signature):
            return Response({'error': 'Invalid signature.'}, status=400)
    elif secret_key:
        return Response({'error': 'Missing signature.'}, status=400)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return Response({'error': 'Invalid payload.'}, status=400)

    if payload.get('event') != 'charge.success':
        return Response({'status': 'ignored'})

    reference = payload.get('data', {}).get('reference', '')
    if not reference or not reference.startswith('swshop_'):
        return Response({'status': 'not_applicable'})

    try:
        order = Order.objects.prefetch_related('items__product').get(
            reference=reference
        )
    except Order.DoesNotExist:
        return Response({'status': 'not_found'})

    if order.paystack_status == 'success':
        return Response({'status': 'already_processed'})

    order.paystack_status = 'success'
    order.status = 'paid'
    order.save(update_fields=['paystack_status', 'status', 'updated_at'])

    # Decrement stock
    for item in order.items.all():
        if item.product:
            Product.objects.filter(
                pk=item.product_id, stock__gte=item.quantity
            ).update(stock=models.F('stock') - item.quantity)

    logger.info(f'Shop order {reference} paid.')
    return Response({'status': 'ok'})
