"""
Lead + InstallationOrder API — reseller dashboard.
"""
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from leads.models import Lead, InstallationOrder
from leads.services import convert_lead_to_subscriber


def _serialise_lead(lead):
    return {
        'id': lead.pk,
        'name': lead.name,
        'phone': lead.phone,
        'email': lead.email,
        'address': lead.address,
        'source': lead.source,
        'intent': lead.intent,
        'status': lead.status,
        'interested_plan_id': lead.interested_plan_id,
        'quoted_amount_ngn': str(lead.quoted_amount_ngn) if lead.quoted_amount_ngn else None,
        'assigned_staff_id': lead.assigned_staff_id,
        'converted_subscriber_id': lead.converted_subscriber_id,
        'notes': lead.notes,
        'created_at': lead.created_at.isoformat(),
        'updated_at': lead.updated_at.isoformat(),
    }


def _serialise_installation(order):
    return {
        'id': order.pk,
        'lead_id': order.lead_id,
        'payment_id': order.payment_id,
        'status': order.status,
        'service_mode': order.service_mode,
        'address': order.address,
        'scheduled_for': order.scheduled_for.isoformat() if order.scheduled_for else None,
        'assigned_tech_id': order.assigned_tech_id,
        'router_id': order.router_id,
        'pppoe_username': order.pppoe_username,
        'pppoe_password': order.pppoe_password,
        'notes': order.notes,
        'completed_at': order.completed_at.isoformat() if order.completed_at else None,
        'created_at': order.created_at.isoformat(),
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def lead_list(request):
    reseller = request.user.reseller
    qs = Lead.objects.filter(reseller=reseller)
    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)
    intent = request.GET.get('intent')
    if intent:
        qs = qs.filter(intent=intent)
    return Response([_serialise_lead(l) for l in qs[:200]])


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def lead_create(request):
    reseller = request.user.reseller
    data = request.data
    if not data.get('phone'):
        return Response({'error': 'phone required'}, status=400)
    lead = Lead(reseller=reseller)
    _apply_lead_fields(lead, data)
    lead.save()
    return Response(_serialise_lead(lead))


def _apply_lead_fields(lead, data):
    for field in ('name', 'phone', 'email', 'address', 'notes', 'lost_reason'):
        if field in data:
            setattr(lead, field, data[field] or '')
    for field in ('source', 'intent', 'status'):
        if field in data:
            setattr(lead, field, data[field])
    if 'interested_plan_id' in data:
        lead.interested_plan_id = data['interested_plan_id']
    if 'assigned_staff_id' in data:
        lead.assigned_staff_id = data['assigned_staff_id']
    if 'quoted_amount_ngn' in data:
        lead.quoted_amount_ngn = data['quoted_amount_ngn']


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def lead_detail(request, pk):
    reseller = request.user.reseller
    lead = get_object_or_404(Lead, pk=pk, reseller=reseller)
    orders = [_serialise_installation(o) for o in lead.installation_orders.all()]
    return Response({'lead': _serialise_lead(lead), 'installations': orders})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def lead_update(request, pk):
    reseller = request.user.reseller
    lead = get_object_or_404(Lead, pk=pk, reseller=reseller)
    _apply_lead_fields(lead, request.data)
    lead.save()
    return Response(_serialise_lead(lead))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def lead_send_quote(request, pk):
    """
    Generate a Paystack payment link for this lead and send it over WA / SMS.
    Thin wrapper over billing.services.create_lead_payment_link.
    """
    reseller = request.user.reseller
    lead = get_object_or_404(Lead, pk=pk, reseller=reseller)

    amount = request.data.get('amount') or lead.quoted_amount_ngn
    if not amount:
        return Response({'error': 'amount required'}, status=400)
    description = request.data.get('description', '')

    from billing.services import create_lead_payment_link
    try:
        payment, url = create_lead_payment_link(
            lead=lead, amount=amount, description=description,
        )
    except ValueError as exc:
        return Response({'error': str(exc)}, status=400)

    lead.status = Lead.STATUS_QUOTED
    lead.quoted_amount_ngn = amount
    lead.save(update_fields=['status', 'quoted_amount_ngn', 'updated_at'])

    # Send the link via the channel the reseller prefers — WA first if connected.
    channel = request.data.get('channel', 'whatsapp')
    message = (
        f'Hello {lead.name or ""}! Here is your {reseller.name} payment link: {url}\n'
        f'Amount: NGN {amount}. {description}'
    ).strip()
    sent_via = None
    if channel == 'whatsapp':
        from notifications.notify import send_whatsapp
        if send_whatsapp(reseller.slug, lead.phone, message):
            sent_via = 'whatsapp'
    if not sent_via and channel in ('sms', 'whatsapp'):
        from notifications.sms import SMSService
        try:
            SMSService().send_sms(lead.phone, message)
            sent_via = 'sms'
        except Exception:
            pass

    return Response({
        'ok': True, 'payment_id': payment.pk, 'url': url, 'sent_via': sent_via,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def lead_convert(request, pk):
    """
    Manually convert a paid-out-of-band lead (no webhook) into a Subscriber.
    Normal flow is automatic from the Paystack webhook.
    """
    reseller = request.user.reseller
    lead = get_object_or_404(Lead, pk=pk, reseller=reseller)
    service_mode = request.data.get('service_mode', InstallationOrder.SERVICE_MODE_PPPOE)
    subscriber, order = convert_lead_to_subscriber(lead, service_mode=service_mode)
    return Response({
        'ok': True, 'subscriber_id': subscriber.pk, 'installation_id': order.pk,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def lead_mark_lost(request, pk):
    reseller = request.user.reseller
    lead = get_object_or_404(Lead, pk=pk, reseller=reseller)
    lead.status = Lead.STATUS_LOST
    lead.lost_reason = request.data.get('reason', '') or lead.lost_reason
    lead.save(update_fields=['status', 'lost_reason', 'updated_at'])
    return Response({'ok': True})


# ---------------------------------------------------------------------------
# InstallationOrder views
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def installation_list(request):
    reseller = request.user.reseller
    qs = InstallationOrder.objects.filter(reseller=reseller)
    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)
    return Response([_serialise_installation(o) for o in qs[:200]])


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def installation_detail(request, pk):
    reseller = request.user.reseller
    order = get_object_or_404(InstallationOrder, pk=pk, reseller=reseller)
    return Response(_serialise_installation(order))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def installation_update(request, pk):
    reseller = request.user.reseller
    order = get_object_or_404(InstallationOrder, pk=pk, reseller=reseller)
    data = request.data
    for field in ('status', 'service_mode', 'address', 'notes', 'pppoe_username', 'pppoe_password'):
        if field in data:
            setattr(order, field, data[field] or '')
    if 'scheduled_for' in data:
        order.scheduled_for = data['scheduled_for'] or None
    if 'assigned_tech_id' in data:
        order.assigned_tech_id = data['assigned_tech_id']
    if 'router_id' in data:
        order.router_id = data['router_id']
    if order.status == InstallationOrder.STATUS_COMPLETED and not order.completed_at:
        order.completed_at = timezone.now()
        # Mark the parent lead as installed
        lead = order.lead
        if lead.status != Lead.STATUS_INSTALLED:
            lead.status = Lead.STATUS_INSTALLED
            lead.save(update_fields=['status', 'updated_at'])
    order.save()
    return Response(_serialise_installation(order))
