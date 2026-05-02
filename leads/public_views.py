"""
Public lead-intake endpoint — sabiwifi.com TikTok funnel.

Unauthenticated; rate-limited; dedup-aware. Creates a Lead row attributed to
PlatformSettings.platform_reseller (or unassigned if not configured), sends a
confirmation SMS to the prospect, and broadcasts to operator phones so the
sales team can follow up in the omni inbox.
"""
import logging
from datetime import timedelta

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from leads.models import Lead

logger = logging.getLogger(__name__)


class LeadIntakeThrottle(AnonRateThrottle):
    rate = '10/hour'
    scope = 'lead_intake'


VALID_INTENTS = {Lead.INTENT_HOME, Lead.INTENT_SME, Lead.INTENT_CLUSTER}
UTM_FIELDS = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content')


def _normalize_phone_for_intake(raw):
    """Best-effort phone normalisation to local NG format for dedup + storage."""
    from accounts.models import Country
    if not raw:
        return ''
    try:
        ng = Country.objects.get(code='NG')
        local = ng.normalize_to_local(raw)
        if local:
            return local
    except Country.DoesNotExist:
        pass
    return str(raw).strip()


def _to_decimal_or_none(val):
    from decimal import Decimal, InvalidOperation
    if val in (None, '', 'null'):
        return None
    try:
        d = Decimal(str(val))
    except (InvalidOperation, TypeError):
        return None
    if not (-90 <= d <= 90 if False else True):  # placeholder; bound check below
        return None
    return d


def _broadcast_to_ops(lead):
    """WhatsApp/SMS the operator notification phones with a new-lead summary."""
    from operator_panel.models import PlatformSettings
    from notifications.notify import send_whatsapp
    from notifications.sms import SMSService

    settings_obj = PlatformSettings.load()
    phones = settings_obj.notification_phones or []
    if not phones:
        return

    intent_label = dict(Lead.INTENT_CHOICES).get(lead.intent, lead.intent)
    msg = (
        f'New SabiWiFi lead\n'
        f'Name: {lead.name}\n'
        f'Phone: {lead.phone}\n'
        f'Intent: {intent_label}\n'
        f'Address: {lead.address or "(not given)"}\n'
    )
    if lead.latitude and lead.longitude:
        msg += f'Map: https://maps.google.com/?q={lead.latitude},{lead.longitude}\n'
    if lead.utm_source:
        msg += f'Source: {lead.utm_source}'
        if lead.utm_campaign:
            msg += f' / {lead.utm_campaign}'
        msg += '\n'

    channel = settings_obj.operator_notification_channel
    sms_service = SMSService() if channel in ('sms', 'both') else None

    # Use the platform_reseller as the WA "from" identity if configured;
    # otherwise pick any verified WA-connected reseller as a fallback.
    wa_reseller_slug = None
    if settings_obj.platform_reseller_id:
        wa_reseller_slug = settings_obj.platform_reseller.slug
    if not wa_reseller_slug:
        # Fall back to first WA-connected reseller.
        from notifications.models import WhatsappSession
        wa_session = WhatsappSession.objects.filter(status='ready').first()
        if wa_session:
            wa_reseller_slug = wa_session.reseller.slug

    for phone in phones:
        intl = _to_international(phone)
        sent = False
        if channel in ('whatsapp', 'both') and wa_reseller_slug:
            try:
                if send_whatsapp(wa_reseller_slug, intl, msg):
                    sent = True
            except Exception as exc:
                logger.warning(f'lead-intake WA broadcast failed for {phone}: {exc}')
        if not sent and sms_service is not None:
            try:
                sms_service.send_sms(intl, msg)
            except Exception as exc:
                logger.warning(f'lead-intake SMS broadcast failed for {phone}: {exc}')


def _to_international(phone):
    """Best-effort E.164 conversion for SMS providers (Termii needs +234… form)."""
    from accounts.models import Country
    try:
        ng = Country.objects.get(code='NG')
        intl = ng.to_international(phone)
        if intl:
            return intl
    except Country.DoesNotExist:
        pass
    return phone


def _send_submitter_confirmation(lead):
    """SMS the prospect a 'thanks, we'll be in touch' confirmation."""
    from notifications.sms import SMSService
    name = lead.name.split()[0] if lead.name else 'there'
    msg = (
        f'Hi {name}, thanks for your interest in SabiWiFi! '
        f'Our team will reach out via WhatsApp/call within 4 hours to confirm your details.'
    )
    try:
        SMSService().send_sms(_to_international(lead.phone), msg)
    except Exception as exc:
        logger.warning(f'lead-intake submitter SMS failed: {exc}')


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([LeadIntakeThrottle])
def lead_intake(request):
    """
    Public lead-intake from sabiwifi.com landing form.

    Body: {intent, name, phone, address, email?, latitude?, longitude?,
           utm_source?, utm_medium?, utm_campaign?, utm_term?, utm_content?,
           landing_url?, click_id?}

    Dedup: same phone+intent within 24h is silently treated as success.
    """
    data = request.data

    intent = (data.get('intent') or '').strip().lower()
    if intent not in VALID_INTENTS:
        return Response({'error': 'Please choose what kind of WiFi you need.'}, status=400)

    name = (data.get('name') or '').strip()
    if len(name) < 2:
        return Response({'error': 'Please enter your full name.'}, status=400)

    raw_phone = (data.get('phone') or '').strip()
    phone = _normalize_phone_for_intake(raw_phone)
    if not phone or len(phone) < 7:
        return Response({'error': 'Please enter a valid phone number.'}, status=400)

    address = (data.get('address') or '').strip()
    if len(address) < 5:
        return Response({'error': 'Please enter your address.'}, status=400)

    email = (data.get('email') or '').strip().lower()
    if email and '@' not in email:
        return Response({'error': 'Please enter a valid email or leave it blank.'}, status=400)

    # GPS — required so ops can confirm coverage before quoting.
    lat = _to_decimal_or_none(data.get('latitude'))
    lng = _to_decimal_or_none(data.get('longitude'))
    if lat is None or lng is None or not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return Response(
            {'error': 'We need your pinned location to confirm coverage. Tap "Pin my location" and allow access.'},
            status=400,
        )

    # Dedup: same phone+intent inside 24h
    cutoff = timezone.now() - timedelta(hours=24)
    existing = Lead.objects.filter(
        phone=phone, intent=intent, created_at__gte=cutoff,
    ).first()
    if existing:
        return Response({
            'success': True,
            'lead_id': existing.pk,
            'message': "We already have your details — our team will reach out shortly. Thanks for your patience!",
            'duplicate': True,
        })

    # Resolve platform reseller (may be None — that's fine, ops triages)
    from operator_panel.models import PlatformSettings
    settings_obj = PlatformSettings.load()
    reseller = settings_obj.platform_reseller

    # Resolve chosen pricing plan if supplied
    chosen_plan = None
    chosen_plan_id = data.get('chosen_plan_id')
    if chosen_plan_id:
        from leads.models import PricingPlan
        try:
            chosen_plan = PricingPlan.objects.get(
                pk=int(chosen_plan_id), is_active=True, category=intent,
            )
        except (PricingPlan.DoesNotExist, ValueError, TypeError):
            chosen_plan = None  # silently ignore mismatched/inactive plans

    lead = Lead.objects.create(
        reseller=reseller,
        chosen_plan=chosen_plan,
        phone=phone,
        name=name,
        email=email,
        address=address,
        latitude=lat,
        longitude=lng,
        source=Lead.SOURCE_WEB,
        intent=intent,
        status=Lead.STATUS_NEW,
        utm_source=(data.get('utm_source') or '')[:128],
        utm_medium=(data.get('utm_medium') or '')[:128],
        utm_campaign=(data.get('utm_campaign') or '')[:128],
        utm_term=(data.get('utm_term') or '')[:128],
        utm_content=(data.get('utm_content') or '')[:128],
        landing_url=(data.get('landing_url') or '')[:200],
        click_id=(data.get('click_id') or '')[:128],
    )

    # Side effects (best-effort; never fail the response if these error)
    try:
        _send_submitter_confirmation(lead)
    except Exception as exc:
        logger.warning(f'lead-intake confirmation failed: {exc}')
    try:
        _broadcast_to_ops(lead)
    except Exception as exc:
        logger.warning(f'lead-intake ops broadcast failed: {exc}')

    return Response({
        'success': True,
        'lead_id': lead.pk,
        'message': "Thanks! We'll WhatsApp you within 4 hours.",
    })
