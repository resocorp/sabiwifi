"""
Management command: cleanup_otp
Runs daily at 3am via cron. Cleans up expired OTP cache entries.
Django's cache backend handles expiry automatically for most backends,
but this ensures no stale data in file/DB-backed caches.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Clean up expired OTP and verification cache entries.'

    def handle(self, *args, **options):
        from django.core.cache import cache
        # Django's default cache backends auto-expire entries.
        # This command exists for DB/file-backed caches that need explicit cleanup.
        try:
            if hasattr(cache, 'clear_expired'):
                cache.clear_expired()
                self.stdout.write("Expired cache entries cleaned.")
            else:
                self.stdout.write("Cache backend handles expiry automatically.")
        except Exception as e:
            self.stdout.write(f"Cache cleanup skipped: {e}")
