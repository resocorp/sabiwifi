"""
Termii SMS integration.
Handles OTP delivery, subscriber notifications, and operator alerts.
"""
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TERMII_BASE_URL = 'https://api.ng.termii.com/api'


class SMSService:
    """Termii SMS service wrapper."""

    def __init__(self, api_key=None, sender_id=None):
        self.api_key = api_key or settings.TERMII_API_KEY
        self.sender_id = sender_id or settings.TERMII_SENDER_ID

    def send_sms(self, phone, message):
        """Send a single SMS message. Returns True on success."""
        if not self.api_key or self.api_key == 'placeholder':
            logger.info(f"[SMS-DEV] To {phone}: {message}")
            return True

        try:
            payload = {
                'to': phone.replace('+', ''),
                'from': self.sender_id,
                'sms': message,
                'type': 'plain',
                'channel': 'generic',
                'api_key': self.api_key,
            }
            resp = requests.post(
                f'{TERMII_BASE_URL}/sms/send',
                json=payload,
                timeout=15,
            )
            data = resp.json()
            if resp.status_code == 200 and data.get('message_id'):
                logger.info(f"SMS sent to {phone}: {data.get('message_id')}")
                return True
            else:
                logger.error(f"SMS failed to {phone}: {data}")
                return False
        except Exception as e:
            logger.error(f"SMS error to {phone}: {e}")
            return False

    def send_otp(self, phone, otp_code):
        """Send OTP verification code."""
        message = f"Your SabiWiFi verification code is: {otp_code}. Valid for 10 minutes."
        return self.send_sms(phone, message)

    def send_expiry_reminder(self, phone, plan_name, domain='sabiwifi.ng'):
        """Send plan expiry reminder (24h before)."""
        message = (
            f"Your SabiWiFi {plan_name} plan expires tomorrow. "
            f"Renew now: {domain}/account"
        )
        return self.send_sms(phone, message)

    def send_expired_notice(self, phone, plan_name, domain='sabiwifi.ng'):
        """Send plan expired notification."""
        message = (
            f"Your SabiWiFi {plan_name} plan has expired. "
            f"Renew to stay connected: {domain}/account"
        )
        return self.send_sms(phone, message)

    def send_pin_reset_otp(self, phone, otp_code):
        """Send PIN reset OTP."""
        message = f"Your SabiWiFi PIN reset code is: {otp_code}. Valid for 10 minutes."
        return self.send_sms(phone, message)

    def send_payment_confirmation(self, phone, plan_name, amount):
        """Send payment confirmation."""
        message = f"SabiWiFi: Payment of ₦{amount:,.0f} received for {plan_name}. Enjoy!"
        return self.send_sms(phone, message)


def send_operator_alert(message):
    """Send SMS alert to all operator notification phones."""
    from operator_panel.models import PlatformSettings
    ps = PlatformSettings.load()
    sms = SMSService(api_key=ps.termii_api_key, sender_id=ps.termii_sender_id)

    for phone in ps.notification_phones:
        sms.send_sms(phone, f"[SabiWiFi] {message}")


def get_sms_service():
    """Get an SMSService instance with current settings."""
    return SMSService()
