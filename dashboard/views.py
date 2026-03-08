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
    """Add router by serial number."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    errors = {}
    if request.method == 'POST':
        serial = request.POST.get('serial_number', '').strip().upper()
        if not serial:
            errors['serial_number'] = ['Serial number is required.']
        else:
            try:
                router = Router.objects.get(serial_number=serial)
                if router.reseller is not None:
                    errors['serial_number'] = ['This router is already assigned to another account.']
                else:
                    # Assign router
                    from routers.views import _generate_credentials
                    from django.core.cache import cache
                    from radius.models import Nas

                    wg_private_key = _generate_credentials(router)
                    router.reseller = reseller
                    router.status = 'pending_provision'
                    router.save()

                    # Write NAS entry
                    Nas.objects.update_or_create(
                        nasname=router.wg_tunnel_ip,
                        defaults={
                            'shortname': router.serial_number,
                            'type': 'other',
                            'secret': router.nas_secret,
                            'description': f'SabiWiFi router {router.serial_number}',
                        }
                    )

                    cache.set(f'wg_privkey_{router.serial_number}', wg_private_key, timeout=86400)
                    messages.success(request, 'Router added! Waiting for it to come online...')
                    return redirect('dashboard-overview')
            except Router.DoesNotExist:
                errors['serial_number'] = ["We don't recognize this serial number."]

    return render(request, 'dashboard/router_add.html', {
        'reseller': reseller,
        'errors': errors,
    })


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
            reseller.branding = branding
            reseller.save()
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

        return redirect('dashboard-settings')

    return render(request, 'dashboard/settings.html', {
        'reseller': reseller,
        'errors': errors,
    })
