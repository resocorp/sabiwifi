"""
Business-side metrics: shop, vouchers, payments, subscriptions, messaging.
"""
from datetime import timedelta

from django.db.models import Count, Sum, Avg, Q
from django.utils import timezone

from accounts.models import Subscriber
from plans.models import Subscription
from billing.models import Payment
from shop.models import Order, OrderItem, Product
from vouchers.models import VoucherBatch, Voucher, RefillCardBatch, RefillCard
from notifications.models import NotificationLog


def shop_stats():
    now = timezone.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = now - timedelta(days=7)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    paid = Order.objects.filter(paystack_status='success')

    gmv_today = paid.filter(created_at__gte=today).aggregate(t=Sum('total_ngn'))['t'] or 0
    gmv_week = paid.filter(created_at__gte=week).aggregate(t=Sum('total_ngn'))['t'] or 0
    gmv_month = paid.filter(created_at__gte=month).aggregate(t=Sum('total_ngn'))['t'] or 0

    orders_today = paid.filter(created_at__gte=today).count()
    orders_month = paid.filter(created_at__gte=month).count()

    pending_fulfillment = Order.objects.filter(status='paid').count()

    # Top 5 products this month
    top_products = list(
        OrderItem.objects.filter(
            order__paystack_status='success', order__created_at__gte=month,
        ).values('product__name').annotate(
            revenue=Sum('product_price_ngn'),
            units=Sum('quantity'),
        ).order_by('-revenue')[:5]
    )

    return {
        'gmv_today': gmv_today,
        'gmv_week': gmv_week,
        'gmv_month': gmv_month,
        'orders_today': orders_today,
        'orders_month': orders_month,
        'pending_fulfillment': pending_fulfillment,
        'top_products': top_products,
    }


def voucher_stats():
    batches = VoucherBatch.objects.count()
    total = Voucher.objects.count()
    # A voucher is considered "redeemed" once it's been activated
    # (status moves from 'unused' → 'active'/'expired').
    redeemed = Voucher.objects.exclude(status='unused').count()
    revenue = Voucher.objects.exclude(status='unused').aggregate(
        t=Sum('plan__price_ngn'),
    )['t'] or 0

    refill_batches = RefillCardBatch.objects.count()
    refill_total = RefillCard.objects.count()
    refill_redeemed = RefillCard.objects.filter(status='used').count()
    refill_revenue = RefillCard.objects.filter(status='used').aggregate(
        t=Sum('value_ngn'),
    )['t'] or 0

    return {
        'batches': batches,
        'total': total,
        'redeemed': redeemed,
        'redemption_rate': round(100 * redeemed / total, 1) if total else 0,
        'revenue': revenue,
        'refill_batches': refill_batches,
        'refill_total': refill_total,
        'refill_redeemed': refill_redeemed,
        'refill_revenue': refill_revenue,
    }


def subscription_stats():
    now = timezone.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = now - timedelta(days=7)
    thirty_ago = now - timedelta(days=30)
    next_7 = now + timedelta(days=7)

    new_today = Subscriber.objects.filter(created_at__gte=today).count()
    new_week = Subscriber.objects.filter(created_at__gte=week).count()
    expiring_7d = Subscription.objects.filter(
        status='active', expiry_date__lte=next_7, expiry_date__gte=now,
    ).count()
    expired_30d = Subscription.objects.filter(
        status='expired', expiry_date__gte=thirty_ago,
    ).count()
    # Simple renewal rate: how many subscribers with an expired plan in
    # last 30d also have a newer active plan
    expired_sub_ids = set(
        Subscription.objects.filter(
            status='expired', expiry_date__gte=thirty_ago,
        ).values_list('subscriber_id', flat=True)
    )
    renewed = Subscription.objects.filter(
        status='active', subscriber_id__in=expired_sub_ids,
    ).values_list('subscriber_id', flat=True).distinct().count()
    renewal_rate = round(100 * renewed / len(expired_sub_ids), 1) if expired_sub_ids else None
    churn_rate = (100 - renewal_rate) if renewal_rate is not None else None

    return {
        'new_today': new_today,
        'new_week': new_week,
        'expiring_7d': expiring_7d,
        'expired_30d': expired_30d,
        'renewed': renewed,
        'renewal_rate': renewal_rate,
        'churn_rate': churn_rate,
    }


def payment_stats():
    now = timezone.now()
    thirty_ago = now - timedelta(days=30)
    payments = Payment.objects.filter(created_at__gte=thirty_ago)

    total = payments.count()
    success = payments.filter(paystack_status='success').count()
    failed = payments.filter(paystack_status='failed').count()
    avg_value = payments.filter(paystack_status='success').aggregate(
        a=Avg('amount_ngn'),
    )['a'] or 0

    return {
        'total_30d': total,
        'success_30d': success,
        'failed_30d': failed,
        'success_rate': round(100 * success / total, 1) if total else None,
        'avg_value': avg_value,
    }


def messaging_stats():
    since = timezone.now() - timedelta(days=30)
    logs = NotificationLog.objects.filter(created_at__gte=since)
    by_channel = logs.values('channel', 'status').annotate(n=Count('id'))
    out = {'whatsapp': {'sent': 0, 'failed': 0}, 'sms': {'sent': 0, 'failed': 0}}
    for row in by_channel:
        ch = row['channel']
        if ch not in out:
            continue
        if row['status'] == 'sent':
            out[ch]['sent'] += row['n']
        elif row['status'] == 'failed':
            out[ch]['failed'] += row['n']
    return out


def all_stats():
    return {
        'shop': shop_stats(),
        'vouchers': voucher_stats(),
        'subscriptions': subscription_stats(),
        'payments': payment_stats(),
        'messaging': messaging_stats(),
    }
