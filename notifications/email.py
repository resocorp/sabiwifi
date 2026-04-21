"""
Email send helper. Thin wrapper around Django's send_mail so callers don't
need to import Django mail directly; mirrors notifications.sms / notify so
the three channels look the same at the call site.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)


def send_email(to, subject, body_text, body_html='', from_email=None):
    """
    Fire-and-forget email send. Returns True on SMTP accept, False otherwise.
    `body_html` is optional — if present it's attached as the HTML alternative.
    """
    if not to:
        return False
    sender = from_email or getattr(settings, 'DEFAULT_FROM_EMAIL',
                                   'noreply@sabiwifi.com')
    try:
        msg = EmailMultiAlternatives(subject or '', body_text or '', sender, [to])
        if body_html:
            msg.attach_alternative(body_html, 'text/html')
        msg.send(fail_silently=False)
        return True
    except Exception as exc:
        logger.warning(f'Email send to {to} failed: {exc}')
        return False
