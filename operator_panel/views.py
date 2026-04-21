"""Operator overview dashboard — aggregate metrics across all resellers."""
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.utils import timezone
from django.db.models import Sum, Count, Q
from django.views.decorators.http import require_http_methods
from django.http import JsonResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response
from accounts.models import Reseller, Subscriber
from plans.models import Subscription
from billing.models import Payment
from routers.models import Router
from operator_panel.models import PlatformSettings
from operator_panel.services import operator_wa


@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_staff, login_url='/login/')
def operator_overview(request):
    """Platform-wide metrics dashboard for the operator."""
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Resellers
    total_resellers = Reseller.objects.count()
    active_resellers = Reseller.objects.filter(status='active').count()

    # Subscribers
    total_subscribers = Subscriber.objects.count()
    active_subscriptions = Subscription.objects.filter(status='active').count()

    # Routers
    total_routers = Router.objects.exclude(status='available').count()
    online_routers = Router.objects.filter(status='online').count()
    offline_routers = Router.objects.filter(status='offline').count()

    # Revenue
    monthly_revenue = Payment.objects.filter(
        paystack_status='success', created_at__gte=month_start,
    ).aggregate(
        total=Sum('amount_ngn'),
        platform=Sum('platform_amount_ngn'),
    )

    today_revenue = Payment.objects.filter(
        paystack_status='success', created_at__gte=today_start,
    ).aggregate(total=Sum('amount_ngn'))['total'] or 0

    # Top resellers by revenue this month
    top_resellers = Reseller.objects.filter(
        payments__paystack_status='success',
        payments__created_at__gte=month_start,
    ).annotate(
        month_revenue=Sum('payments__amount_ngn')
    ).order_by('-month_revenue')[:10]

    # Routers phoning home (last_seen within 5 min, regardless of status)
    five_min_ago = now - timezone.timedelta(minutes=5)
    phoning_home_routers = Router.objects.filter(
        last_seen__gte=five_min_ago
    ).select_related('reseller').order_by('-last_seen')

    # Unclaimed routers that have phoned home at least once
    unclaimed_with_contact = Router.objects.filter(
        status='available', last_seen__isnull=False
    ).select_related('reseller').order_by('-last_seen')[:10]

    # Recent alerts placeholder
    alerts = []
    offline_routers_list = Router.objects.filter(status='offline').select_related('reseller')[:5]
    for r in offline_routers_list:
        alerts.append({
            'type': 'warning',
            'message': f'Router {r.serial_number} ({r.reseller.name if r.reseller else "unassigned"}) is offline',
            'time': r.last_seen,
        })

    # Enhanced stats (cached 60s)
    from django.core.cache import cache
    from operator_panel.stats import overview as overview_stats
    enhanced = cache.get('op_overview_enhanced_v1')
    if enhanced is None:
        enhanced = overview_stats.all_stats()
        cache.set('op_overview_enhanced_v1', enhanced, timeout=60)

    context = {
        'total_resellers': total_resellers,
        'active_resellers': active_resellers,
        'total_subscribers': total_subscribers,
        'active_subscriptions': active_subscriptions,
        'total_routers': total_routers,
        'online_routers': online_routers,
        'offline_routers': offline_routers,
        'monthly_revenue': monthly_revenue['total'] or 0,
        'monthly_platform_revenue': monthly_revenue['platform'] or 0,
        'today_revenue': today_revenue,
        'top_resellers': top_resellers,
        'alerts': alerts,
        'phoning_home_routers': phoning_home_routers,
        'unclaimed_with_contact': unclaimed_with_contact,
        'enhanced': enhanced,
    }
    return render(request, 'operator/overview.html', context)


# ---------------------------------------------------------------------------
# Operator settings (WhatsApp + notifications)
# ---------------------------------------------------------------------------

_NOTIFY_FIELDS = [
    'notify_on_new_reseller',
    'notify_on_reseller_activated',
    'notify_on_new_order',
    'notify_on_router_offline',
    'notify_on_router_recovered',
    'notify_on_payment_failure',
    'notify_on_payment_failure_spike',
    'notify_daily_summary',
]


@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_staff, login_url='/login/')
def operator_business(request):
    """Shop, vouchers, payments, subscriptions, messaging usage."""
    from operator_panel.stats import business as biz
    from django.core.cache import cache
    cache_key = 'op_business_stats_v1'
    stats = cache.get(cache_key)
    if stats is None:
        stats = biz.all_stats()
        cache.set(cache_key, stats, timeout=60)
    return render(request, 'operator/business.html', {'s': stats})


@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_staff, login_url='/login/')
def operator_partners(request):
    """Partner list with health scoring."""
    from operator_panel.stats import partners as partners_stats
    rows = partners_stats.partner_rows(request.GET.get('filter'))
    # Pagination
    from django.core.paginator import Paginator
    paginator = Paginator(rows, 25)
    page = paginator.get_page(request.GET.get('page') or 1)
    return render(request, 'operator/partners.html', {
        'page': page,
        'filter': request.GET.get('filter') or '',
    })


@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_staff, login_url='/login/')
def staff_subscriber_create(request, reseller_pk):
    """Platform staff creates a subscriber on behalf of a reseller."""
    from django.contrib import messages
    from django.shortcuts import get_object_or_404
    from accounts.models import Country
    from plans.models import ServicePlan
    from radius.utils import set_subscriber_radius_password
    from plans.services import activate_subscription

    reseller = get_object_or_404(Reseller, pk=reseller_pk)
    plans = ServicePlan.objects.filter(reseller=reseller, is_active=True).order_by('name')
    countries = Country.objects.filter(is_active=True).order_by('sort_order', 'name')

    errors = {}
    form_data = {
        'phone': '',
        'email': '',
        'pin': '',
        'plan_id': '',
    }

    if request.method == 'POST':
        form_data['phone'] = request.POST.get('phone', '').strip()
        form_data['email'] = request.POST.get('email', '').strip().lower()
        form_data['pin'] = request.POST.get('pin', '').strip()
        form_data['plan_id'] = request.POST.get('plan_id', '').strip()
        country_code = request.POST.get('country', 'NG').upper()

        country = Country.objects.filter(code=country_code, is_active=True).first()
        if not country:
            errors['country'] = ['Country not supported.']
        else:
            normalized = country.normalize_to_local(form_data['phone'])
            if not normalized:
                errors['phone'] = [f'Invalid phone number for {country.name}.']
            else:
                form_data['phone'] = normalized

        pin = form_data['pin']
        if not pin or len(pin) < 5 or len(pin) > 8 or not pin.isdigit():
            errors['pin'] = ['PIN must be 5-8 digits.']

        chosen_plan = None
        if form_data['plan_id']:
            chosen_plan = plans.filter(pk=form_data['plan_id']).first()
            if not chosen_plan:
                errors['plan_id'] = ['Selected plan not found.']

        if not errors and Subscriber.objects.filter(
            reseller=reseller, phone=form_data['phone']
        ).exists():
            errors['phone'] = ['A subscriber with this phone already exists.']

        if not errors:
            subscriber = Subscriber.objects.create(
                reseller=reseller,
                country=country,
                phone=form_data['phone'],
                email=form_data['email'],
                verified=True,
                source=Subscriber.SOURCE_STAFF,
            )
            subscriber.set_pin(pin)
            subscriber.generate_auth_token()
            subscriber.save()
            set_subscriber_radius_password(subscriber, pin)

            if chosen_plan:
                activate_subscription(subscriber, chosen_plan)
                messages.success(
                    request,
                    f'Created {subscriber.phone} for {reseller.name} on {chosen_plan.name}.'
                )
            else:
                messages.success(request, f'Created {subscriber.phone} for {reseller.name}.')
            return redirect('operator-partners')

    return render(request, 'operator/subscriber_form.html', {
        'reseller': reseller,
        'errors': errors,
        'form_data': form_data,
        'plans': plans,
        'countries': countries,
    })


@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_staff, login_url='/login/')
def operator_network(request):
    """Live RADIUS/network stats."""
    from django.core.cache import cache
    from operator_panel.stats import network as network_stats

    cache_key = 'op_network_stats_v1'
    stats = cache.get(cache_key)
    if stats is None:
        stats = network_stats.all_stats()
        cache.set(cache_key, stats, timeout=60)
    return render(request, 'operator/network.html', {'s': stats})


@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_staff, login_url='/login/')
def operator_settings(request):
    """Operator WhatsApp connection + notification configuration page."""
    ps = PlatformSettings.load()
    context = {
        'ps': ps,
        'notify_fields': _NOTIFY_FIELDS,
    }
    return render(request, 'operator/settings.html', context)


@api_view(['GET'])
def operator_wa_status(request):
    if not request.user.is_staff:
        return Response({'error': 'Forbidden'}, status=403)
    return Response(operator_wa.get_status())


@api_view(['POST'])
def operator_wa_connect(request):
    if not request.user.is_staff:
        return Response({'error': 'Forbidden'}, status=403)
    result = operator_wa.connect()
    if result.get('ok'):
        return Response({'ok': True, 'status': result.get('status', 'connecting')})
    return Response({'error': result.get('error', 'WA service error')}, status=503)


@api_view(['POST'])
def operator_wa_disconnect(request):
    if not request.user.is_staff:
        return Response({'error': 'Forbidden'}, status=403)
    operator_wa.disconnect()
    return Response({'ok': True})


@api_view(['POST'])
def operator_wa_test(request):
    if not request.user.is_staff:
        return Response({'error': 'Forbidden'}, status=403)
    phone = request.data.get('phone', '').strip()
    if not phone:
        # Fall back to first operator notification phone
        ps = PlatformSettings.load()
        phones = ps.notification_phones or []
        if not phones:
            return Response({'error': 'Phone number required.'}, status=400)
        phone = phones[0]
    ok = operator_wa.send(phone, '✅ SabiWiFi operator WhatsApp test successful!')
    if ok:
        return Response({'ok': True, 'message': f'Test queued to {phone}.'})
    return Response({'error': 'Send failed. Is WhatsApp connected?'}, status=503)


@api_view(['POST'])
def operator_settings_save(request):
    """Persist notification toggles, channel, and recipient phones."""
    if not request.user.is_staff:
        return Response({'error': 'Forbidden'}, status=403)

    ps = PlatformSettings.load()
    data = request.data

    # Boolean toggles
    for field in _NOTIFY_FIELDS:
        if field in data:
            setattr(ps, field, bool(data[field]))

    # Channel
    channel = data.get('operator_notification_channel')
    if channel in ('sms', 'whatsapp', 'both'):
        ps.operator_notification_channel = channel

    # Phones — accept list or comma-separated string
    phones = data.get('notification_phones')
    if phones is not None:
        if isinstance(phones, str):
            phones = [p.strip() for p in phones.split(',') if p.strip()]
        elif isinstance(phones, list):
            phones = [str(p).strip() for p in phones if str(p).strip()]
        else:
            phones = []
        ps.notification_phones = phones

    ps.save()
    return Response({'ok': True})
