"""Voucher and refill card batch generation utilities."""
import secrets
import logging

from vouchers.models import Voucher, RefillCard

logger = logging.getLogger(__name__)


def generate_voucher_batch(batch):
    """
    Generate unique PINs for a VoucherBatch and bulk-create Voucher records.
    Returns the number of vouchers created.
    """
    existing_pins = set(
        Voucher.objects.filter(reseller=batch.reseller)
        .values_list('pin', flat=True)
    )

    pin_digits = batch.pin_length
    prefix = batch.prefix or ''
    vouchers = []
    generated_pins = set()
    max_attempts = batch.quantity * 10  # safety limit
    attempts = 0

    while len(generated_pins) < batch.quantity and attempts < max_attempts:
        attempts += 1
        # Generate numeric portion
        numeric = str(secrets.randbelow(10 ** pin_digits)).zfill(pin_digits)
        pin = f'{prefix}{numeric}'

        if pin in existing_pins or pin in generated_pins:
            continue

        generated_pins.add(pin)
        vouchers.append(Voucher(
            batch=batch,
            reseller=batch.reseller,
            plan=batch.plan,
            pin=pin,
            status='unused',
        ))

    created = Voucher.objects.bulk_create(vouchers)
    logger.info(f"Generated {len(created)} vouchers for batch '{batch.name}' (reseller: {batch.reseller.name})")
    return len(created)


def generate_refill_batch(batch):
    """
    Generate unique PINs for a RefillCardBatch and bulk-create RefillCard records.
    Returns the number of cards created.
    """
    existing_pins = set(
        RefillCard.objects.filter(reseller=batch.reseller)
        .values_list('pin', flat=True)
    )

    pin_digits = batch.pin_length
    prefix = batch.prefix or ''
    cards = []
    generated_pins = set()
    max_attempts = batch.quantity * 10
    attempts = 0

    while len(generated_pins) < batch.quantity and attempts < max_attempts:
        attempts += 1
        numeric = str(secrets.randbelow(10 ** pin_digits)).zfill(pin_digits)
        pin = f'{prefix}{numeric}'

        if pin in existing_pins or pin in generated_pins:
            continue

        generated_pins.add(pin)
        cards.append(RefillCard(
            batch=batch,
            reseller=batch.reseller,
            pin=pin,
            value_ngn=batch.value_ngn,
            status='unused',
        ))

    created = RefillCard.objects.bulk_create(cards)
    logger.info(f"Generated {len(created)} refill cards for batch '{batch.name}' (reseller: {batch.reseller.name})")
    return len(created)
