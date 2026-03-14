"""
Management command: import_serials
Bulk-import router serial numbers from a text file or CSV.

Usage:
    python manage.py import_serials serials.txt
    python manage.py import_serials serials.csv

File format: one serial number per line (or CSV first column).
Lines starting with # are treated as comments and skipped.
"""
import csv
import logging
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from routers.models import Router

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Import router serial numbers from a text file or CSV.'

    def add_arguments(self, parser):
        parser.add_argument(
            'file',
            type=str,
            help='Path to text file or CSV with serial numbers (one per line).',
        )

    def handle(self, *args, **options):
        file_path = Path(options['file'])
        if not file_path.exists():
            raise CommandError(f"File not found: {file_path}")

        serials = self._read_serials(file_path)
        if not serials:
            self.stdout.write(self.style.WARNING("No serial numbers found in file."))
            return

        created = 0
        skipped = 0
        errors = 0

        for serial in serials:
            try:
                _, was_created = Router.objects.get_or_create(
                    serial_number=serial,
                    defaults={'status': 'available'},
                )
                if was_created:
                    created += 1
                    self.stdout.write(f"  + {serial}")
                else:
                    skipped += 1
                    self.stdout.write(f"  = {serial} (already exists)")
            except Exception as e:
                errors += 1
                self.stdout.write(self.style.ERROR(f"  ! {serial}: {e}"))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Import complete: {created} created, {skipped} skipped, {errors} errors "
            f"(from {len(serials)} total)"
        ))
        logger.info(
            f"Serial import: {created} created, {skipped} skipped, {errors} errors"
        )

    def _read_serials(self, file_path):
        """Read serial numbers from a text or CSV file."""
        serials = []
        suffix = file_path.suffix.lower()

        with open(file_path, 'r') as f:
            if suffix == '.csv':
                reader = csv.reader(f)
                for row in reader:
                    if not row:
                        continue
                    serial = row[0].strip().upper()
                    if serial and not serial.startswith('#'):
                        serials.append(serial)
            else:
                for line in f:
                    serial = line.strip().upper()
                    if serial and not serial.startswith('#'):
                        serials.append(serial)

        return serials
