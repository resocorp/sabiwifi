"""
Wallet operations — all balance mutations go through these helpers.
Uses select_for_update() to prevent race conditions.
"""
import logging
import secrets
from decimal import Decimal

from django.db import transaction

from billing.models import Wallet, WalletTransaction, Payment
from plans.services import activate_subscription

logger = logging.getLogger(__name__)


class InsufficientBalance(Exception):
    pass


def get_or_create_wallet(subscriber):
    """Get or create a wallet for a subscriber."""
    wallet, _ = Wallet.objects.get_or_create(subscriber=subscriber)
    return wallet


@transaction.atomic
def credit_wallet(subscriber, amount, txn_type, reference='', description='', created_by='system'):
    """Credit a subscriber's wallet. Returns the WalletTransaction."""
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError('Credit amount must be positive.')

    wallet = get_or_create_wallet(subscriber)
    wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
    wallet.balance_ngn += amount
    wallet.save(update_fields=['balance_ngn', 'updated_at'])

    txn = WalletTransaction.objects.create(
        wallet=wallet,
        amount_ngn=amount,
        balance_after=wallet.balance_ngn,
        transaction_type=txn_type,
        reference=reference,
        description=description,
        created_by=created_by,
    )
    logger.info(f'Wallet credit: {subscriber.phone} +₦{amount} ({txn_type}), balance=₦{wallet.balance_ngn}')
    return txn


@transaction.atomic
def debit_wallet(subscriber, amount, txn_type, reference='', description='', created_by='system'):
    """Debit a subscriber's wallet. Raises InsufficientBalance if not enough."""
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError('Debit amount must be positive.')

    wallet = get_or_create_wallet(subscriber)
    wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)

    if wallet.balance_ngn < amount:
        raise InsufficientBalance(
            f'Wallet balance ₦{wallet.balance_ngn} insufficient for ₦{amount} debit.'
        )

    wallet.balance_ngn -= amount
    wallet.save(update_fields=['balance_ngn', 'updated_at'])

    txn = WalletTransaction.objects.create(
        wallet=wallet,
        amount_ngn=-amount,
        balance_after=wallet.balance_ngn,
        transaction_type=txn_type,
        reference=reference,
        description=description,
        created_by=created_by,
    )
    logger.info(f'Wallet debit: {subscriber.phone} -₦{amount} ({txn_type}), balance=₦{wallet.balance_ngn}')
    return txn


@transaction.atomic
def purchase_plan_from_wallet(subscriber, plan):
    """
    Purchase a plan using wallet balance.
    Debits wallet, creates Subscription, assigns RADIUS, creates Payment record.
    Returns the new Subscription.
    """
    debit_wallet(
        subscriber, plan.price_ngn, 'plan_purchase',
        reference=f'plan-{plan.pk}',
        description=f'Purchased {plan.name}',
    )

    sub = activate_subscription(subscriber, plan)

    # Create Payment record for tracking
    Payment.objects.create(
        subscriber=subscriber,
        plan=plan,
        reseller=subscriber.reseller,
        payment_type='plan',
        amount_ngn=plan.price_ngn,
        paystack_reference=f'wallet-{secrets.token_hex(8)}',
        paystack_status='success',
        payment_method='wallet',
    )

    logger.info(f'Wallet plan purchase: {subscriber.phone} → {plan.name}')
    return sub


@transaction.atomic
def renew_plan_from_wallet(subscriber, plan):
    """
    Auto-renew a plan using wallet balance.
    Same as purchase_plan_from_wallet but uses 'plan_renewal' transaction type.
    Returns the new Subscription, or None if insufficient balance.
    """
    wallet = get_or_create_wallet(subscriber)
    if wallet.balance_ngn < plan.price_ngn:
        return None

    debit_wallet(
        subscriber, plan.price_ngn, 'plan_renewal',
        reference=f'renewal-{plan.pk}',
        description=f'Auto-renewed {plan.name}',
        created_by='system',
    )

    sub = activate_subscription(subscriber, plan)

    Payment.objects.create(
        subscriber=subscriber,
        plan=plan,
        reseller=subscriber.reseller,
        payment_type='auto_renewal',
        amount_ngn=plan.price_ngn,
        paystack_reference=f'renewal-{secrets.token_hex(8)}',
        paystack_status='success',
        payment_method='wallet',
    )

    logger.info(f'Auto-renewed: {subscriber.phone} → {plan.name}')
    return sub
