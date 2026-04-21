"""Reseller dashboard server-rendered views."""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.core.paginator import Paginator
from django.db.models import Sum, Count, Q, Prefetch, Exists, OuterRef
from accounts.models import Reseller, Subscriber
from plans.models import ServicePlan, Subscription
from billing.models import Payment
from routers.models import Router


class _SerializerRequest:
    """Minimal request-like object for DRF serializer context."""
    def __init__(self, user):
        self.user = user


def _get_reseller(request):
    """Get the reseller for the current user, or redirect to login."""
    if not hasattr(request.user, 'reseller'):
        return None
    return request.user.reseller


@login_required
def overview(request):
    """Dashboard home — Getting Started or Overview depending on state."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Router state
    routers = Router.objects.filter(reseller=reseller)
    routers_total = routers.count()
    routers_online = routers.filter(status='online').count()
    has_router = routers_total > 0
    has_online_router = routers_online > 0

    # Plan state
    plans = ServicePlan.objects.filter(reseller=reseller)
    has_plans = plans.exists()

    # Getting Started is a one-time onboarding state. Once the reseller has
    # added a router, a plan, and verified payment, they always see the full
    # dashboard — offline/failed routers are surfaced as alert banners there,
    # not by regressing into onboarding.
    checklist_complete = has_router and has_plans and reseller.payment_verified
    show_getting_started = not checklist_complete

    # Stats
    active_subs = Subscription.objects.filter(reseller=reseller, status='active').count()

    monthly_revenue = Payment.objects.filter(
        reseller=reseller, paystack_status='success', created_at__gte=month_start,
    ).aggregate(total=Sum('amount_ngn'))['total'] or 0

    monthly_earnings = Payment.objects.filter(
        reseller=reseller, paystack_status='success', created_at__gte=month_start,
    ).aggregate(total=Sum('reseller_amount_ngn'))['total'] or 0

    today_payments = Payment.objects.filter(
        reseller=reseller, paystack_status='success', created_at__gte=today_start,
    ).aggregate(total=Sum('amount_ngn'))['total'] or 0

    today_signups = Subscriber.objects.filter(
        reseller=reseller, created_at__gte=today_start
    ).count()

    # Recent activity
    recent_payments = Payment.objects.filter(
        reseller=reseller,
    ).select_related('subscriber', 'plan').order_by('-created_at')[:10]

    # Router states (for Getting Started) — mutually exclusive branches in template
    pending_router = routers.filter(status='pending_provision').first()
    online_router = routers.filter(status='online').first()
    failed_router = routers.filter(status='failed').first()
    offline_router = routers.filter(status__in=['offline', 'provisioned']).first()

    # Offline routers (for Overview banner) — all of them, with duration info.
    offline_routers = list(routers.filter(status__in=['offline', 'provisioned', 'failed']))

    context = {
        'reseller': reseller,
        'show_getting_started': show_getting_started,
        'has_router': has_router,
        'has_online_router': has_online_router,
        'has_plans': has_plans,
        'pending_router': pending_router,
        'online_router': online_router,
        'failed_router': failed_router,
        'offline_router': offline_router,
        'offline_routers': offline_routers,
        'routers_online': routers_online,
        'routers_total': routers_total,
        'active_subs': active_subs,
        'monthly_revenue': monthly_revenue,
        'monthly_earnings': monthly_earnings,
        'today_payments': today_payments,
        'today_signups': today_signups,
        'recent_payments': recent_payments,
        'now': now,
    }
    return render(request, 'dashboard/overview.html', context)


@login_required
def plans_list(request):
    """List reseller's service plans."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    plans = ServicePlan.objects.filter(reseller=reseller).annotate(
        active_subscribers=Count(
            'subscriptions', filter=Q(subscriptions__status='active')
        )
    )
    return render(request, 'dashboard/plans_list.html', {
        'reseller': reseller,
        'plans': plans,
    })


def _extract_plan_form_data(POST):
    """Extract all plan fields from POST data."""
    def _int(val, default=0):
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _decimal(val):
        if not val:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    data = {
        'name': POST.get('name', '').strip(),
        'download_mbps': POST.get('download_mbps', 5),
        'upload_mbps': POST.get('upload_mbps', 5),
        'duration_days': POST.get('duration_days', 30),
        'duration_hours': POST.get('duration_hours', 0),
        'data_cap_gb': _decimal(POST.get('data_cap_gb')),
        'download_cap_gb': _decimal(POST.get('download_cap_gb')),
        'upload_cap_gb': _decimal(POST.get('upload_cap_gb')),
        'burst_download_mbps': _int(POST.get('burst_download_mbps')),
        'burst_upload_mbps': _int(POST.get('burst_upload_mbps')),
        'burst_threshold_download_mbps': _int(POST.get('burst_threshold_download_mbps')),
        'burst_threshold_upload_mbps': _int(POST.get('burst_threshold_upload_mbps')),
        'burst_time_seconds': _int(POST.get('burst_time_seconds')),
        'priority': _int(POST.get('priority'), 8),
        'online_time_limit_minutes': _int(POST.get('online_time_limit_minutes')),
        'ip_pool_name': POST.get('ip_pool_name', '').strip(),
        'daily_download_mb': _int(POST.get('daily_download_mb')),
        'daily_upload_mb': _int(POST.get('daily_upload_mb')),
        'daily_total_mb': _int(POST.get('daily_total_mb')),
        'daily_time_minutes': _int(POST.get('daily_time_minutes')),
        'max_devices': POST.get('max_devices', 1),
        'price_ngn': POST.get('price_ngn', 0),
        'allow_auto_renew': POST.get('allow_auto_renew') == 'on',
    }

    fallback = POST.get('fallback_plan')
    if fallback:
        data['fallback_plan'] = _int(fallback) or None
    else:
        data['fallback_plan'] = None

    daily_fallback = POST.get('daily_fallback_plan')
    if daily_fallback:
        data['daily_fallback_plan'] = _int(daily_fallback) or None
    else:
        data['daily_fallback_plan'] = None

    router_ids = POST.getlist('routers') if hasattr(POST, 'getlist') else []
    data['routers'] = [_int(r) for r in router_ids if r]

    return data


@login_required
def plan_create(request):
    """Create a new service plan."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    errors = {}
    form_data = {}

    if request.method == 'POST':
        from plans.serializers import ServicePlanSerializer

        form_data = _extract_plan_form_data(request.POST)

        serializer = ServicePlanSerializer(
            data=form_data,
            context={'request': _SerializerRequest(request.user)}
        )
        if serializer.is_valid():
            serializer.save()
            messages.success(request, 'Plan created successfully!')
            return redirect('dashboard-plans')
        else:
            errors = serializer.errors

    fallback_plans = ServicePlan.objects.filter(reseller=reseller).order_by('name')
    available_routers = Router.objects.filter(reseller=reseller).order_by('location_name', 'serial_number')

    return render(request, 'dashboard/plan_form.html', {
        'reseller': reseller,
        'errors': errors,
        'form_data': form_data,
        'is_edit': False,
        'fallback_plans': fallback_plans,
        'available_routers': available_routers,
        'selected_router_ids': set(form_data.get('routers') or []),
    })


@login_required
def plan_edit(request, pk):
    """Edit an existing service plan."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    plan = get_object_or_404(ServicePlan, pk=pk, reseller=reseller)
    errors = {}

    if request.method == 'POST':
        from plans.serializers import ServicePlanSerializer

        form_data = _extract_plan_form_data(request.POST)

        serializer = ServicePlanSerializer(
            plan, data=form_data, partial=True,
            context={'request': _SerializerRequest(request.user)}
        )
        if serializer.is_valid():
            serializer.save()
            messages.success(request, 'Plan updated successfully!')
            return redirect('dashboard-plans')
        else:
            errors = serializer.errors

    fallback_plans = ServicePlan.objects.filter(reseller=reseller).exclude(pk=plan.pk).order_by('name')
    available_routers = Router.objects.filter(reseller=reseller).order_by('location_name', 'serial_number')
    selected_router_ids = set(plan.routers.values_list('pk', flat=True))

    return render(request, 'dashboard/plan_form.html', {
        'reseller': reseller,
        'plan': plan,
        'errors': errors,
        'available_routers': available_routers,
        'selected_router_ids': selected_router_ids,
        'form_data': {
            'name': plan.name,
            'download_mbps': plan.download_mbps,
            'upload_mbps': plan.upload_mbps,
            'duration_days': plan.duration_days,
            'duration_hours': plan.duration_hours,
            'data_cap_gb': plan.data_cap_gb,
            'download_cap_gb': plan.download_cap_gb,
            'upload_cap_gb': plan.upload_cap_gb,
            'burst_download_mbps': plan.burst_download_mbps,
            'burst_upload_mbps': plan.burst_upload_mbps,
            'burst_threshold_download_mbps': plan.burst_threshold_download_mbps,
            'burst_threshold_upload_mbps': plan.burst_threshold_upload_mbps,
            'burst_time_seconds': plan.burst_time_seconds,
            'priority': plan.priority,
            'online_time_limit_minutes': plan.online_time_limit_minutes,
            'ip_pool_name': plan.ip_pool_name,
            'fallback_plan': plan.fallback_plan_id,
            'daily_fallback_plan': plan.daily_fallback_plan_id,
            'daily_download_mb': plan.daily_download_mb,
            'daily_upload_mb': plan.daily_upload_mb,
            'daily_total_mb': plan.daily_total_mb,
            'daily_time_minutes': plan.daily_time_minutes,
            'max_devices': plan.max_devices,
            'price_ngn': plan.price_ngn,
            'allow_auto_renew': plan.allow_auto_renew,
        },
        'fallback_plans': fallback_plans,
        'is_edit': True,
    })


@login_required
def plan_disable(request, pk):
    """Soft-disable a plan (stops new signups; existing subscribers keep access until expiry)."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    plan = get_object_or_404(ServicePlan, pk=pk, reseller=reseller)
    if request.method == 'POST' and plan.is_active:
        plan.is_active = False
        plan.save()
        messages.success(request, f'Plan "{plan.name}" disabled.')
    return redirect('dashboard-plans')


@login_required
def plan_enable(request, pk):
    """Re-enable a disabled plan."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    plan = get_object_or_404(ServicePlan, pk=pk, reseller=reseller)
    if request.method == 'POST' and not plan.is_active:
        plan.is_active = True
        plan.save()
        messages.success(request, f'Plan "{plan.name}" enabled.')
    return redirect('dashboard-plans')


@login_required
def plan_delete(request, pk):
    """Hard-delete a plan. Refuses if any subscription is active."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    plan = get_object_or_404(ServicePlan, pk=pk, reseller=reseller)

    if request.method != 'POST':
        return redirect('dashboard-plans')

    active_count = plan.subscriptions.filter(status='active').count()
    if active_count:
        messages.error(
            request,
            f'Cannot delete "{plan.name}" — {active_count} active subscriber'
            f'{"s" if active_count != 1 else ""} still on this plan. '
            f'Disable it instead to stop new signups.'
        )
        return redirect('dashboard-plans')

    # Clean up RADIUS group attributes before deleting the plan row.
    from radius.models import Radgroupreply, Radgroupcheck, Radusergroup
    group_name = plan.radius_group_name
    Radgroupreply.objects.filter(groupname=group_name).delete()
    Radgroupcheck.objects.filter(groupname=group_name).delete()
    Radusergroup.objects.filter(groupname=group_name).delete()

    plan_name = plan.name
    plan.delete()
    messages.success(request, f'Plan "{plan_name}" deleted.')
    return redirect('dashboard-plans')


@login_required
def subscribers_list(request):
    """List reseller's subscribers."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    search = request.GET.get('q', '')
    status_filter = request.GET.get('status', '')

    subscribers = Subscriber.objects.filter(reseller=reseller)

    if search:
        subscribers = subscribers.filter(phone__icontains=search)

    # Filter by active/expired at the DB level
    has_active = Exists(
        Subscription.objects.filter(subscriber=OuterRef('pk'), status='active')
    )
    if status_filter == 'active':
        subscribers = subscribers.filter(has_active)
    elif status_filter == 'expired':
        subscribers = subscribers.exclude(has_active)

    # Prefetch active subscriptions in a single query
    active_subs_prefetch = Prefetch(
        'subscriptions',
        queryset=Subscription.objects.filter(status='active').select_related('plan'),
        to_attr='active_subscriptions',
    )
    subscribers = subscribers.prefetch_related(active_subs_prefetch).order_by('-created_at')

    paginator = Paginator(subscribers, 25)
    page = paginator.get_page(request.GET.get('page'))

    subs_with_plans = []
    for sub in page:
        current_sub = sub.active_subscriptions[0] if sub.active_subscriptions else None
        subs_with_plans.append({
            'subscriber': sub,
            'subscription': current_sub,
        })

    return render(request, 'dashboard/subscribers_list.html', {
        'reseller': reseller,
        'subscribers': subs_with_plans,
        'page_obj': page,
        'search': search,
        'status_filter': status_filter,
        'total_count': paginator.count,
    })


@login_required
def subscriber_create(request):
    """Manually create a subscriber (reseller-initiated). Optional plan activation."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    plans = ServicePlan.objects.filter(reseller=reseller, is_active=True).order_by('name')
    errors = {}
    form_data = {
        'phone': '',
        'email': '',
        'pin': '',
        'plan_id': '',
    }

    if request.method == 'POST':
        from accounts.models import Country, Subscriber as SubscriberModel
        from radius.utils import set_subscriber_radius_password
        from plans.services import activate_subscription

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

        if not errors and SubscriberModel.objects.filter(
            reseller=reseller, phone=form_data['phone']
        ).exists():
            errors['phone'] = ['A subscriber with this phone already exists.']

        if not errors:
            subscriber = SubscriberModel.objects.create(
                reseller=reseller,
                country=country,
                phone=form_data['phone'],
                email=form_data['email'],
                verified=True,
                source=SubscriberModel.SOURCE_RESELLER,
            )
            subscriber.set_pin(pin)
            subscriber.generate_auth_token()
            subscriber.save()
            set_subscriber_radius_password(subscriber, pin)

            if chosen_plan:
                activate_subscription(subscriber, chosen_plan)
                messages.success(
                    request,
                    f'Subscriber {subscriber.phone} created and assigned to {chosen_plan.name}.'
                )
            else:
                messages.success(request, f'Subscriber {subscriber.phone} created.')
            return redirect('dashboard-subscriber-detail', pk=subscriber.pk)

    from accounts.models import Country
    countries = Country.objects.filter(is_active=True).order_by('sort_order', 'name')

    return render(request, 'dashboard/subscriber_form.html', {
        'reseller': reseller,
        'errors': errors,
        'form_data': form_data,
        'plans': plans,
        'countries': countries,
    })


@login_required
def subscriber_detail(request, pk):
    """Subscriber detail page with plan, usage, payments."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    subscriber = get_object_or_404(Subscriber, pk=pk, reseller=reseller)
    current_sub = Subscription.objects.filter(
        subscriber=subscriber, status='active'
    ).select_related('plan').first()

    payments = Payment.objects.filter(subscriber=subscriber).order_by('-created_at')
    subscriptions = Subscription.objects.filter(subscriber=subscriber).order_by('-start_date')

    return render(request, 'dashboard/subscriber_detail.html', {
        'reseller': reseller,
        'subscriber': subscriber,
        'current_subscription': current_sub,
        'payments': payments,
        'subscriptions': subscriptions,
    })


@login_required
def payments_list(request):
    """Payments & earnings page."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    payments = Payment.objects.filter(reseller=reseller).select_related(
        'subscriber', 'plan'
    ).order_by('-created_at')

    monthly_total = payments.filter(
        paystack_status='success', created_at__gte=month_start
    ).aggregate(
        total=Sum('amount_ngn'),
        platform_total=Sum('platform_amount_ngn'),
        reseller_total=Sum('reseller_amount_ngn'),
    )

    return render(request, 'dashboard/payments.html', {
        'reseller': reseller,
        'payments': payments[:50],
        'monthly_revenue': monthly_total['total'] or 0,
        'monthly_platform_fee': monthly_total['platform_total'] or 0,
        'monthly_earnings': monthly_total['reseller_total'] or 0,
    })


@login_required
def routers_list(request):
    """Router management page."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    routers = Router.objects.filter(reseller=reseller)
    return render(request, 'dashboard/routers.html', {
        'reseller': reseller,
        'routers': routers,
    })


@login_required
def router_add(request):
    """
    Add a router to the reseller's account.

    MikroTik: reseller enters the serial number printed on the device sticker.
              Server generates WireGuard credentials and pushes them via RouterOS API.

    OpenWrt:  reseller enters the MAC address printed on the device sticker.
              Device auto-registered in SabiWiFi via heartbeat cron (runs every 2 min).
              Server allocates WG tunnel IP + NAS secret, adds WG peer, and pushes
              the per-device context vars to OpenWISP so templates re-apply within 5 min.
    """
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    import logging
    logger = logging.getLogger(__name__)
    errors = {}

    if request.method == 'POST':
        device_type = request.POST.get('device_type', 'mikrotik')

        if device_type == 'openwrt':
            _handle_openwrt_add(request, reseller, errors, logger)
        else:
            _handle_mikrotik_add(request, reseller, errors, logger)

        if not errors:
            return redirect('dashboard-overview')

    return render(request, 'dashboard/router_add.html', {
        'reseller': reseller,
        'errors': errors,
    })


@login_required
def router_remove(request, pk):
    """
    Remove a router from the reseller's account.

    Unassigns the device (does not delete it from the platform). Cleans up
    WireGuard peer, FreeRADIUS NAS entry, and the per-reseller tunnel/secret
    state so the device can be re-assigned by the platform operator.
    """
    import logging
    logger = logging.getLogger(__name__)

    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    router = get_object_or_404(Router, pk=pk, reseller=reseller)

    if request.method != 'POST':
        return redirect('dashboard-routers')

    from routers.wg_utils import remove_peer, WireGuardError
    from radius.models import Nas
    from radius.utils import _reload_freeradius

    label = router.location_name or router.serial_number

    if router.wg_public_key:
        try:
            remove_peer(router.wg_public_key)
        except WireGuardError as e:
            logger.error(f'Failed to remove WG peer for {router.serial_number}: {e}')

    if router.wg_tunnel_ip:
        Nas.objects.filter(nasname=str(router.wg_tunnel_ip)).delete()
        _reload_freeradius()

    router.reseller = None
    router.status = 'available'
    router.wg_public_key = ''
    router.wg_private_key = ''
    router.wg_tunnel_ip = None
    router.nas_secret = ''
    router.last_seen = None
    router.offline_since = None
    router.save()

    messages.success(request, f'Router "{label}" removed from your account.')
    return redirect('dashboard-routers')


def _handle_mikrotik_add(request, reseller, errors, logger):
    """Claim a MikroTik router by serial number (existing flow)."""
    from routers.views import _generate_credentials
    from routers.wg_utils import add_peer, WireGuardError
    from django.core.cache import cache
    from django.contrib import messages

    serial = request.POST.get('serial_number', '').strip().upper()
    if not serial:
        errors['serial_number'] = ['Serial number is required.']
        return

    try:
        router = Router.objects.get(serial_number=serial, device_type='mikrotik')
    except Router.DoesNotExist:
        errors['serial_number'] = [
            "Serial number not found. Power on the router and wait a few "
            "minutes for it to register, then try again."
        ]
        return

    if router.reseller is not None:
        errors['serial_number'] = ['This router is already assigned to another account.']
        return

    wg_private_key = _generate_credentials(router)
    router.reseller = reseller
    router.status = 'pending_provision'
    router.save()

    from radius.utils import upsert_nas_and_reload
    upsert_nas_and_reload(
        nasname=router.wg_tunnel_ip,
        shortname=router.serial_number,
        secret=router.nas_secret,
        description=f'SabiWiFi router {router.serial_number}',
    )

    if router.wg_public_key:
        try:
            add_peer(router.wg_public_key, router.wg_tunnel_ip)
        except WireGuardError as e:
            logger.error(f"Failed to add WG peer for MikroTik {serial}: {e}")

    cache.set(f'wg_privkey_{router.serial_number}', wg_private_key, timeout=86400)
    messages.success(request, 'Router added! Waiting for it to come online...')


def _handle_openwrt_add(request, reseller, errors, logger):
    """
    Claim an OpenWrt router by MAC address.

    Flow:
      1. Look up Router in local DB (auto-created by heartbeat)
         OR find in OpenWISP if not yet in local DB
      2. Allocate WireGuard tunnel IP + NAS secret
      3. Add WireGuard peer on server (device already sent pubkey via heartbeat)
      4. Create FreeRADIUS NAS entry
      5. Create OpenWISPBinding (device → reseller org)
      6. Update device context vars in OpenWISP (wg_tunnel_ip, nas_secret, ssid)
    """
    import secrets as secrets_mod
    from django.contrib import messages
    from routers.serial_utils import is_valid_mac, normalize_mac
    from routers.views import _allocate_tunnel_ip
    from routers.wg_utils import add_peer, WireGuardError

    raw_mac = request.POST.get('mac_address', '').strip().upper().replace(':', '').replace('-', '')
    if not raw_mac:
        errors['mac_address'] = ['MAC address is required.']
        return

    if not is_valid_mac(raw_mac):
        errors['mac_address'] = ['Invalid MAC address. Enter the 12-digit hex address from the sticker (e.g. A4B1C2D3E4F5).']
        return

    mac = normalize_mac(raw_mac)

    # Try local DB first (populated by heartbeat cron)
    router = Router.objects.filter(serial_number=mac, device_type='openwrt').first()

    # If not in local DB, check OpenWISP
    if not router:
        router = _find_openwrt_in_openwisp(mac, logger)

    if not router:
        errors['mac_address'] = [
            "Device not found. Make sure it's powered on and connected to the internet — "
            "it should register within 2 minutes."
        ]
        return

    if router.reseller is not None:
        errors['mac_address'] = ['This router is already assigned to another account.']
        return

    # Allocate credentials
    router.wg_tunnel_ip = _allocate_tunnel_ip()
    router.nas_secret = secrets_mod.token_urlsafe(24)
    router.reseller = reseller
    router.status = 'pending_provision'
    router.save()

    # FreeRADIUS NAS entry + reload
    from radius.utils import upsert_nas_and_reload
    upsert_nas_and_reload(
        nasname=router.wg_tunnel_ip,
        shortname=mac,
        secret=router.nas_secret,
        description=f'SabiWiFi OpenWrt {mac}',
    )

    # Add WireGuard peer (pubkey saved by heartbeat)
    if router.wg_public_key:
        try:
            add_peer(router.wg_public_key, router.wg_tunnel_ip)
        except WireGuardError as e:
            logger.warning(f"WG peer add failed for OpenWrt {mac}: {e}")
    else:
        logger.warning(f"No WG public key yet for OpenWrt {mac} — peer will be added on next heartbeat")

    # Register in OpenWISP and push context vars
    _register_openwrt_in_openwisp(router, reseller, logger)

    messages.success(request, 'Router added! Config will be pushed within 5 minutes.')


def _find_openwrt_in_openwisp(mac, logger):
    """
    If a device has registered with OpenWISP but hasn't sent a heartbeat yet,
    find it there and create the local Router record.
    """
    try:
        from integrations.openwisp import OpenWISPClient, OpenWISPError
        client = OpenWISPClient()
        device = client.find_device_by_mac(mac)
        if not device:
            return None
        # Create local Router mirroring the OpenWISP device
        router, _ = Router.objects.get_or_create(
            serial_number=mac,
            defaults={'device_type': 'openwrt', 'status': 'available'},
        )
        return router
    except Exception as e:
        logger.info(f'OpenWISP lookup for {mac} failed (may not be configured yet): {e}')
        return None


def _register_openwrt_in_openwisp(router, reseller, logger):
    """
    Create OpenWISPBinding for the router, move device to reseller's org,
    and push per-device context variables (wg_tunnel_ip, nas_secret, ssid).
    """
    try:
        from integrations.openwisp import OpenWISPClient, OpenWISPError
        from integrations.models import OpenWISPBinding

        client = OpenWISPClient()
        device = client.find_device_by_mac(router.serial_number)
        if not device:
            logger.warning(f'OpenWISP device not found for {router.serial_number} during claiming')
            return

        device_id = device['id']

        # Move device to reseller's OpenWISP organization
        if hasattr(reseller, 'openwisp_binding'):
            org_id = str(reseller.openwisp_binding.openwisp_id)
            client.move_device_to_org(device_id, org_id)

        # Push per-device context variables for config templates
        ssid = (reseller.branding or {}).get('ssid', 'SabiWiFi')
        client.update_device_context(device_id, {
            'wg_tunnel_ip': router.wg_tunnel_ip,
            'nas_secret': router.nas_secret,
            'ssid': ssid,
            'ssid_5g': f'{ssid}-5G',
        })

        # Record the binding
        OpenWISPBinding.objects.update_or_create(
            router=router,
            defaults={
                'openwisp_id': device_id,
                'openwisp_type': 'device',
            },
        )
        logger.info(f'OpenWrt {router.serial_number} registered in OpenWISP: {device_id}')

    except Exception as e:
        logger.warning(f'OpenWISP registration for {router.serial_number} failed: {e}')


@login_required
def settings_page(request):
    """Reseller settings — branding, account, bank."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    errors = {}
    if request.method == 'POST':
        section = request.POST.get('section', '')

        if section == 'branding':
            branding = reseller.branding or {}
            branding['portal_title'] = request.POST.get('portal_title', branding.get('portal_title', ''))
            branding['welcome_text'] = request.POST.get('welcome_text', branding.get('welcome_text', ''))
            branding['primary_color'] = request.POST.get('primary_color', branding.get('primary_color', '#0052CC'))
            branding['template'] = request.POST.get('template', branding.get('template', 'modern'))

            # Logo upload (max 50KB)
            logo_file = request.FILES.get('logo')
            if logo_file:
                if logo_file.size > 50 * 1024:
                    errors['logo'] = ['Logo must be under 50KB.']
                else:
                    import os
                    from django.conf import settings as django_settings
                    upload_dir = os.path.join(django_settings.MEDIA_ROOT, 'resellers', reseller.slug)
                    os.makedirs(upload_dir, exist_ok=True)
                    logo_path = os.path.join(upload_dir, 'logo.png')
                    with open(logo_path, 'wb') as f:
                        for chunk in logo_file.chunks():
                            f.write(chunk)
                    branding['logo'] = f'/media/resellers/{reseller.slug}/logo.png'

            # Remove logo
            if request.POST.get('remove_logo') == '1':
                branding['logo'] = ''

            # Background image upload (max 200KB, bold template only)
            bg_file = request.FILES.get('bg_image')
            if bg_file:
                if bg_file.size > 200 * 1024:
                    errors['bg_image'] = ['Background image must be under 200KB.']
                else:
                    import os
                    from django.conf import settings as django_settings
                    upload_dir = os.path.join(django_settings.MEDIA_ROOT, 'resellers', reseller.slug)
                    os.makedirs(upload_dir, exist_ok=True)
                    bg_path = os.path.join(upload_dir, 'bg.jpg')
                    with open(bg_path, 'wb') as f:
                        for chunk in bg_file.chunks():
                            f.write(chunk)
                    branding['bg_image'] = f'/media/resellers/{reseller.slug}/bg.jpg'

            # Remove background
            if request.POST.get('remove_bg') == '1':
                branding['bg_image'] = ''

            reseller.branding = branding
            reseller.save()
            if not errors:
                messages.success(request, 'Branding updated!')

        elif section == 'account':
            reseller.name = request.POST.get('business_name', reseller.name).strip()
            reseller.email = request.POST.get('email', reseller.email).strip()
            reseller.phone = request.POST.get('phone', reseller.phone).strip()
            reseller.location = request.POST.get('location', reseller.location).strip()
            reseller.save()
            messages.success(request, 'Account settings updated!')

        elif section == 'ssid':
            ssid = request.POST.get('ssid', '').strip()
            if ssid:
                branding = reseller.branding or {}
                branding['ssid'] = ssid
                reseller.branding = branding
                reseller.save()
                messages.success(request, 'WiFi network name updated!')

        elif section == 'signup_fee':
            from decimal import Decimal, InvalidOperation
            if reseller.payment_verified:
                reseller.signup_fee_enabled = request.POST.get('signup_fee_enabled') == '1'
                try:
                    amount = Decimal(request.POST.get('signup_fee_amount', '0') or '0')
                    reseller.signup_fee_amount = max(Decimal('0'), amount)
                except InvalidOperation:
                    reseller.signup_fee_amount = Decimal('0')
                reseller.save()
                messages.success(request, 'Account creation fee updated!')
            else:
                messages.error(request, 'You must verify your bank account before enabling signup fees.')

        return redirect('dashboard-settings')

    return render(request, 'dashboard/settings.html', {
        'reseller': reseller,
        'errors': errors,
    })


@login_required
def broadcasts_page(request):
    """Broadcasts — compose and track bulk messages."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')
    try:
        wa_connected = reseller.whatsapp_session.status == 'connected'
    except Exception:
        wa_connected = False
    return render(request, 'dashboard/broadcasts.html', {
        'reseller': reseller,
        'wa_connected': wa_connected,
    })


# ── Voucher Management ──────────────────────────────────────────────

@login_required
def voucher_batches(request):
    """List all voucher batches for this reseller."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    from vouchers.models import VoucherBatch
    from django.db.models import Count, Q

    batches = VoucherBatch.objects.filter(reseller=reseller).select_related('plan').annotate(
        total=Count('vouchers'),
        used=Count('vouchers', filter=~Q(vouchers__status='unused')),
        available=Count('vouchers', filter=Q(vouchers__status='unused')),
    ).order_by('-created_at')

    return render(request, 'dashboard/voucher_batches.html', {
        'reseller': reseller,
        'batches': batches,
        'active_tab': 'vouchers',
    })


@login_required
def voucher_batch_create(request):
    """Create a new voucher batch."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    plans = ServicePlan.objects.filter(reseller=reseller, is_active=True)

    if request.method == 'POST':
        from vouchers.models import VoucherBatch
        from vouchers.utils import generate_voucher_batch

        plan_id = request.POST.get('plan')
        try:
            plan = ServicePlan.objects.get(pk=plan_id, reseller=reseller)
        except ServicePlan.DoesNotExist:
            messages.error(request, 'Invalid plan selected.')
            return render(request, 'dashboard/voucher_batch_form.html', {
                'reseller': reseller, 'plans': plans, 'active_tab': 'vouchers',
            })

        quantity = int(request.POST.get('quantity', 10))
        if quantity < 1 or quantity > 5000:
            messages.error(request, 'Quantity must be between 1 and 5000.')
            return render(request, 'dashboard/voucher_batch_form.html', {
                'reseller': reseller, 'plans': plans, 'active_tab': 'vouchers',
            })

        batch = VoucherBatch.objects.create(
            reseller=reseller,
            plan=plan,
            name=request.POST.get('name', f'{plan.name} batch'),
            quantity=quantity,
            pin_length=int(request.POST.get('pin_length', 8)),
            prefix=request.POST.get('prefix', '').strip().upper(),
            validity_type=request.POST.get('validity_type', 'from_activation'),
            validity_days=int(request.POST.get('validity_days', 0)) or None,
            validity_fixed_date=request.POST.get('validity_fixed_date') or None,
            simultaneous_use=int(request.POST.get('simultaneous_use', 1)),
        )
        count = generate_voucher_batch(batch)
        messages.success(request, f'Generated {count} vouchers.')
        return redirect('dashboard-voucher-batch-detail', pk=batch.pk)

    return render(request, 'dashboard/voucher_batch_form.html', {
        'reseller': reseller,
        'plans': plans,
        'active_tab': 'vouchers',
    })


@login_required
def voucher_batch_detail(request, pk):
    """View vouchers in a batch."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    from vouchers.models import VoucherBatch
    batch = get_object_or_404(VoucherBatch, pk=pk, reseller=reseller)
    vouchers = batch.vouchers.select_related('subscriber').order_by('pin')

    return render(request, 'dashboard/voucher_batch_detail.html', {
        'reseller': reseller,
        'batch': batch,
        'vouchers': vouchers,
        'active_tab': 'vouchers',
    })


@login_required
def voucher_batch_export_csv(request, pk):
    """Export voucher PINs as CSV for printing."""
    import csv
    from django.http import HttpResponse

    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    from vouchers.models import VoucherBatch
    batch = get_object_or_404(VoucherBatch, pk=pk, reseller=reseller)
    vouchers = batch.vouchers.order_by('pin')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="vouchers-{batch.pk}.csv"'

    writer = csv.writer(response, delimiter=';')
    writer.writerow(['id', 'pin', 'plan', 'status'])
    for v in vouchers:
        writer.writerow([v.pk, v.pin, v.plan.name, v.status])

    return response


@login_required
def voucher_batch_toggle(request, pk):
    """Disable or re-enable a voucher batch."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    from vouchers.models import VoucherBatch
    batch = get_object_or_404(VoucherBatch, pk=pk, reseller=reseller)

    if request.method == 'POST':
        if batch.status == 'active':
            batch.status = 'disabled'
            batch.vouchers.filter(status='unused').update(status='disabled')
            messages.success(request, f'Batch "{batch.name}" disabled. Unused vouchers revoked.')
        else:
            batch.status = 'active'
            batch.vouchers.filter(status='disabled').update(status='unused')
            messages.success(request, f'Batch "{batch.name}" re-enabled.')
        batch.save(update_fields=['status'])

    return redirect('dashboard-voucher-batches')


# ── Refill Cards ─────────────────────────────────────────────────────

@login_required
def refill_batches(request):
    """List all refill card batches."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    from vouchers.models import RefillCardBatch
    from django.db.models import Count, Q

    batches = RefillCardBatch.objects.filter(reseller=reseller).annotate(
        total=Count('refill_cards'),
        used=Count('refill_cards', filter=Q(refill_cards__status='used')),
        available=Count('refill_cards', filter=Q(refill_cards__status='unused')),
    ).order_by('-created_at')

    return render(request, 'dashboard/refill_batches.html', {
        'reseller': reseller,
        'batches': batches,
        'active_tab': 'vouchers',
    })


@login_required
def refill_batch_create(request):
    """Create a new refill card batch."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    if request.method == 'POST':
        from vouchers.models import RefillCardBatch
        from vouchers.utils import generate_refill_batch

        quantity = int(request.POST.get('quantity', 10))
        if quantity < 1 or quantity > 5000:
            messages.error(request, 'Quantity must be between 1 and 5000.')
            return redirect('dashboard-refill-batch-create')

        value = request.POST.get('value_ngn', '0')
        batch = RefillCardBatch.objects.create(
            reseller=reseller,
            name=request.POST.get('name', f'₦{value} refill batch'),
            quantity=quantity,
            value_ngn=value,
            pin_length=int(request.POST.get('pin_length', 10)),
            prefix=request.POST.get('prefix', '').strip().upper(),
        )
        count = generate_refill_batch(batch)
        messages.success(request, f'Generated {count} refill cards.')
        return redirect('dashboard-refill-batches')

    return render(request, 'dashboard/refill_batch_form.html', {
        'reseller': reseller,
        'active_tab': 'vouchers',
    })


@login_required
def refill_batch_export_csv(request, pk):
    """Export refill card PINs as CSV."""
    import csv
    from django.http import HttpResponse

    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    from vouchers.models import RefillCardBatch
    batch = get_object_or_404(RefillCardBatch, pk=pk, reseller=reseller)
    cards = batch.refill_cards.order_by('pin')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="refill-cards-{batch.pk}.csv"'

    writer = csv.writer(response, delimiter=';')
    writer.writerow(['id', 'pin', 'value_ngn', 'status'])
    for c in cards:
        writer.writerow([c.pk, c.pin, c.value_ngn, c.status])

    return response


# ── Online Users & Session Management ────────────────────────────────

@login_required
def online_users(request):
    """Show currently connected users from radacct."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    from radius.models import Radacct
    from django.db import connection

    # Efficient single query: join radacct with subscriber, filtered by reseller
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT ra.username, ra.nasipaddress, ra.acctstarttime,
                   ra.acctsessiontime, ra.acctinputoctets, ra.acctoutputoctets,
                   ra.framedipaddress, ra.acctsessionid, s.id as subscriber_id
            FROM radacct ra
            JOIN accounts_subscriber s ON s.phone = ra.username
            WHERE ra.acctstoptime IS NULL
              AND s.reseller_id = %s
            ORDER BY ra.acctstarttime DESC
        """, [reseller.pk])
        columns = [col[0] for col in cursor.description]
        sessions = [dict(zip(columns, row)) for row in cursor.fetchall()]

    # Map NAS IPs to router names
    router_map = {}
    if sessions:
        nas_ips = {s['nasipaddress'] for s in sessions}
        routers = Router.objects.filter(wg_tunnel_ip__in=nas_ips).values('wg_tunnel_ip', 'location_name', 'serial_number')
        router_map = {r['wg_tunnel_ip']: r['location_name'] or r['serial_number'] for r in routers}

    for s in sessions:
        s['router_name'] = router_map.get(s['nasipaddress'], s['nasipaddress'])
        # Format data for display
        dl = s.get('acctoutputoctets') or 0
        ul = s.get('acctinputoctets') or 0
        total_mb = (dl + ul) / (1024 * 1024)
        s['data_display'] = f'{total_mb:.1f} MB' if total_mb < 1024 else f'{total_mb / 1024:.2f} GB'
        session_time = s.get('acctsessiontime') or 0
        hours, remainder = divmod(session_time, 3600)
        minutes, _ = divmod(remainder, 60)
        s['time_display'] = f'{hours}h {minutes}m' if hours else f'{minutes}m'

    return render(request, 'dashboard/online_users.html', {
        'reseller': reseller,
        'sessions': sessions,
        'session_count': len(sessions),
        'active_tab': 'online_users',
    })


@login_required
def disconnect_user(request, pk):
    """Manually disconnect a subscriber's sessions via CoA."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    subscriber = get_object_or_404(Subscriber, pk=pk, reseller=reseller)

    if request.method == 'POST':
        from radius.utils import disconnect_subscriber_sessions
        disconnect_subscriber_sessions(subscriber)
        messages.success(request, f'Disconnect sent for {subscriber.phone}.')

    return redirect('dashboard-online-users')


# ── Reports & Analytics ──────────────────────────────────────────────

@login_required
def traffic_report(request):
    """Per-subscriber traffic usage report from radacct."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    from django.db import connection
    from datetime import timedelta

    days = int(request.GET.get('days', 30))
    since = timezone.now() - timedelta(days=days)

    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT ra.username,
                   SUM(ra.acctoutputoctets) as total_download,
                   SUM(ra.acctinputoctets) as total_upload,
                   SUM(ra.acctsessiontime) as total_time,
                   COUNT(*) as total_sessions
            FROM radacct ra
            JOIN accounts_subscriber s ON s.phone = ra.username
            WHERE s.reseller_id = %s AND ra.acctstarttime >= %s
            GROUP BY ra.username
            ORDER BY total_download DESC
            LIMIT 100
        """, [reseller.pk, since])
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    for r in rows:
        dl = r.get('total_download') or 0
        ul = r.get('total_upload') or 0
        total_mb = (dl + ul) / (1024 * 1024)
        r['data_display'] = f'{total_mb:.1f} MB' if total_mb < 1024 else f'{total_mb / 1024:.2f} GB'
        r['dl_display'] = f'{dl / (1024 * 1024):.1f} MB'
        r['ul_display'] = f'{ul / (1024 * 1024):.1f} MB'
        t = r.get('total_time') or 0
        hours = t // 3600
        minutes = (t % 3600) // 60
        r['time_display'] = f'{hours}h {minutes}m'

    return render(request, 'dashboard/traffic_report.html', {
        'reseller': reseller,
        'rows': rows,
        'days': days,
        'active_tab': 'reports',
    })


@login_required
def session_report(request):
    """Session history from radacct."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    from django.db import connection
    from datetime import timedelta

    phone = request.GET.get('phone', '')
    days = int(request.GET.get('days', 7))
    since = timezone.now() - timedelta(days=days)

    params = [reseller.pk, since]
    phone_filter = ''
    if phone:
        phone_filter = 'AND ra.username = %s'
        params.append(phone)

    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT ra.username, ra.nasipaddress, ra.acctstarttime, ra.acctstoptime,
                   ra.acctsessiontime, ra.acctinputoctets, ra.acctoutputoctets,
                   ra.framedipaddress, ra.acctterminatecause
            FROM radacct ra
            JOIN accounts_subscriber s ON s.phone = ra.username
            WHERE s.reseller_id = %s AND ra.acctstarttime >= %s {phone_filter}
            ORDER BY ra.acctstarttime DESC
            LIMIT 200
        """, params)
        columns = [col[0] for col in cursor.description]
        sessions = [dict(zip(columns, row)) for row in cursor.fetchall()]

    router_map = {}
    if sessions:
        nas_ips = {s['nasipaddress'] for s in sessions}
        routers_qs = Router.objects.filter(wg_tunnel_ip__in=nas_ips).values('wg_tunnel_ip', 'location_name', 'serial_number')
        router_map = {r['wg_tunnel_ip']: r['location_name'] or r['serial_number'] for r in routers_qs}

    for s in sessions:
        s['router_name'] = router_map.get(s['nasipaddress'], s['nasipaddress'] or '—')
        dl = s.get('acctoutputoctets') or 0
        ul = s.get('acctinputoctets') or 0
        total_mb = (dl + ul) / (1024 * 1024)
        s['data_display'] = f'{total_mb:.1f} MB' if total_mb < 1024 else f'{total_mb / 1024:.2f} GB'
        t = s.get('acctsessiontime') or 0
        hours = t // 3600
        minutes = (t % 3600) // 60
        s['time_display'] = f'{hours}h {minutes}m'

    return render(request, 'dashboard/session_report.html', {
        'reseller': reseller,
        'sessions': sessions,
        'phone': phone,
        'days': days,
        'active_tab': 'reports',
    })


@login_required
def financial_report(request):
    """Revenue report from Payment model."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    from datetime import timedelta

    days = int(request.GET.get('days', 30))
    since = timezone.now() - timedelta(days=days)

    payments = Payment.objects.filter(
        reseller=reseller,
        paystack_status='success',
        created_at__gte=since,
    ).select_related('plan', 'subscriber')

    total_revenue = payments.aggregate(total=Sum('amount_ngn'))['total'] or 0
    total_earnings = payments.aggregate(total=Sum('reseller_amount_ngn'))['total'] or 0

    # Per-plan breakdown
    plan_breakdown = payments.values('plan__name').annotate(
        count=Count('id'),
        revenue=Sum('amount_ngn'),
    ).order_by('-revenue')

    # Per-method breakdown
    method_breakdown = payments.values('payment_method').annotate(
        count=Count('id'),
        revenue=Sum('amount_ngn'),
    ).order_by('-revenue')

    return render(request, 'dashboard/financial_report.html', {
        'reseller': reseller,
        'payments': payments.order_by('-created_at')[:100],
        'total_revenue': total_revenue,
        'total_earnings': total_earnings,
        'plan_breakdown': plan_breakdown,
        'method_breakdown': method_breakdown,
        'days': days,
        'active_tab': 'reports',
    })


@login_required
def report_csv_export(request, report_type):
    """Export report data as CSV."""
    import csv
    from django.http import StreamingHttpResponse
    from datetime import timedelta

    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    days = int(request.GET.get('days', 30))
    since = timezone.now() - timedelta(days=days)

    if report_type == 'traffic':
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT ra.username,
                       SUM(ra.acctoutputoctets) as download_bytes,
                       SUM(ra.acctinputoctets) as upload_bytes,
                       SUM(ra.acctsessiontime) as online_seconds,
                       COUNT(*) as sessions
                FROM radacct ra
                JOIN accounts_subscriber s ON s.phone = ra.username
                WHERE s.reseller_id = %s AND ra.acctstarttime >= %s
                GROUP BY ra.username
                ORDER BY download_bytes DESC
            """, [reseller.pk, since])
            rows = cursor.fetchall()

        def generate():
            yield 'username;download_bytes;upload_bytes;online_seconds;sessions\n'
            for row in rows:
                yield f'{row[0]};{row[1]};{row[2]};{row[3]};{row[4]}\n'

        response = StreamingHttpResponse(generate(), content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="traffic-report-{days}d.csv"'
        return response

    elif report_type == 'financial':
        payments = Payment.objects.filter(
            reseller=reseller, paystack_status='success', created_at__gte=since,
        ).select_related('plan', 'subscriber').order_by('-created_at')

        def generate():
            yield 'date;subscriber;plan;amount;method;reseller_amount;platform_amount\n'
            for p in payments.iterator():
                yield (
                    f'{p.created_at.strftime("%Y-%m-%d %H:%M")};'
                    f'{p.subscriber.phone};'
                    f'{p.plan.name if p.plan else "N/A"};'
                    f'{p.amount_ngn};{p.payment_method};'
                    f'{p.reseller_amount_ngn};{p.platform_amount_ngn}\n'
                )

        response = StreamingHttpResponse(generate(), content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="financial-report-{days}d.csv"'
        return response

    return redirect('dashboard-overview')
