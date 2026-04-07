"""Reseller dashboard server-rendered views."""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.db.models import Sum, Count, Q
from accounts.models import Reseller, Subscriber
from plans.models import ServicePlan, Subscription
from billing.models import Payment
from routers.models import Router


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

    # Check if we should show Getting Started vs Overview
    # Show getting started until all checklist items complete
    checklist_complete = has_online_router and has_plans and reseller.payment_verified
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

    # Pending router (for Getting Started)
    pending_router = routers.filter(status='pending_provision').first()
    online_router = routers.filter(status='online').first()

    context = {
        'reseller': reseller,
        'show_getting_started': show_getting_started,
        'has_router': has_router,
        'has_online_router': has_online_router,
        'has_plans': has_plans,
        'pending_router': pending_router,
        'online_router': online_router,
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

        form_data = {
            'name': request.POST.get('name', '').strip(),
            'download_mbps': request.POST.get('download_mbps', 5),
            'upload_mbps': request.POST.get('upload_mbps', 5),
            'duration_days': request.POST.get('duration_days', 30),
            'duration_hours': request.POST.get('duration_hours', 0),
            'data_cap_gb': request.POST.get('data_cap_gb') or None,
            'max_devices': request.POST.get('max_devices', 1),
            'price_ngn': request.POST.get('price_ngn', 0),
        }

        # Build a mock request for serializer context
        class MockRequest:
            def __init__(self, user):
                self.user = user

        serializer = ServicePlanSerializer(
            data=form_data,
            context={'request': MockRequest(request.user)}
        )
        if serializer.is_valid():
            serializer.save()
            messages.success(request, 'Plan created successfully!')
            return redirect('dashboard-plans')
        else:
            errors = serializer.errors

    return render(request, 'dashboard/plan_form.html', {
        'reseller': reseller,
        'errors': errors,
        'form_data': form_data,
        'is_edit': False,
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

        form_data = {
            'name': request.POST.get('name', '').strip(),
            'download_mbps': request.POST.get('download_mbps', 5),
            'upload_mbps': request.POST.get('upload_mbps', 5),
            'duration_days': request.POST.get('duration_days', 30),
            'duration_hours': request.POST.get('duration_hours', 0),
            'data_cap_gb': request.POST.get('data_cap_gb') or None,
            'max_devices': request.POST.get('max_devices', 1),
            'price_ngn': request.POST.get('price_ngn', 0),
        }

        class MockRequest:
            def __init__(self, user):
                self.user = user

        serializer = ServicePlanSerializer(
            plan, data=form_data, partial=True,
            context={'request': MockRequest(request.user)}
        )
        if serializer.is_valid():
            serializer.save()
            messages.success(request, 'Plan updated successfully!')
            return redirect('dashboard-plans')
        else:
            errors = serializer.errors

    return render(request, 'dashboard/plan_form.html', {
        'reseller': reseller,
        'plan': plan,
        'errors': errors,
        'form_data': {
            'name': plan.name,
            'download_mbps': plan.download_mbps,
            'upload_mbps': plan.upload_mbps,
            'duration_days': plan.duration_days,
            'duration_hours': plan.duration_hours,
            'data_cap_gb': plan.data_cap_gb,
            'max_devices': plan.max_devices,
            'price_ngn': plan.price_ngn,
        },
        'is_edit': True,
    })


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

    # Annotate with current subscription
    subs_with_plans = []
    for sub in subscribers.order_by('-created_at'):
        current_sub = Subscription.objects.filter(
            subscriber=sub, status='active'
        ).select_related('plan').first()
        subs_with_plans.append({
            'subscriber': sub,
            'subscription': current_sub,
        })

    if status_filter == 'active':
        subs_with_plans = [s for s in subs_with_plans if s['subscription']]
    elif status_filter == 'expired':
        subs_with_plans = [s for s in subs_with_plans if not s['subscription']]

    return render(request, 'dashboard/subscribers_list.html', {
        'reseller': reseller,
        'subscribers': subs_with_plans,
        'search': search,
        'status_filter': status_filter,
        'total_count': len(subs_with_plans),
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


def _handle_mikrotik_add(request, reseller, errors, logger):
    """Claim a MikroTik router by serial number (existing flow)."""
    from routers.views import _generate_credentials
    from routers.wg_utils import add_peer, WireGuardError
    from django.core.cache import cache
    from radius.models import Nas
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

    Nas.objects.update_or_create(
        nasname=router.wg_tunnel_ip,
        defaults={
            'shortname': router.serial_number,
            'type': 'other',
            'secret': router.nas_secret,
            'description': f'SabiWiFi router {router.serial_number}',
        }
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
    from radius.models import Nas

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

    # FreeRADIUS NAS entry
    Nas.objects.update_or_create(
        nasname=router.wg_tunnel_ip,
        defaults={
            'shortname': mac,
            'type': 'other',
            'secret': router.nas_secret,
            'description': f'SabiWiFi OpenWrt {mac}',
        }
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
