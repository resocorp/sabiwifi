"""Server-rendered captive portal and subscriber self-service pages."""
from django.shortcuts import render
from routers.models import Router
from accounts.models import Country


def _get_reseller_from_serial(serial):
    """Resolve router serial → reseller for branding."""
    if not serial:
        return None
    try:
        router = Router.objects.select_related('reseller').get(serial_number=serial.upper())
        return router.reseller
    except Router.DoesNotExist:
        return None


def portal_login(request):
    """Captive portal login page — shows reseller branding."""
    serial = request.GET.get('r', '')
    mac = request.GET.get('mac', '')
    link_login = request.GET.get('link-login', '')
    link_orig = request.GET.get('link-orig', '')
    error = request.GET.get('error', '')

    reseller = _get_reseller_from_serial(serial)
    branding = reseller.branding if reseller else {}
    template_name = branding.get('template', 'modern')

    # Phase 2: Full portal implementation
    return render(request, f'portal/{template_name}/login.html', {
        'reseller': reseller,
        'branding': branding,
        'serial': serial,
        'mac': mac,
        'link_login': link_login,
        'link_orig': link_orig,
        'error': error,
    })


def portal_connected(request):
    """Post-authentication success page."""
    serial = request.GET.get('r', '')
    reseller = _get_reseller_from_serial(serial)
    branding = reseller.branding if reseller else {}
    template_name = branding.get('template', 'modern')

    return render(request, f'portal/{template_name}/connected.html', {
        'reseller': reseller,
        'branding': branding,
    })


def portal_signup(request):
    """Captive portal signup page."""
    serial = request.GET.get('r', '')
    mac = request.GET.get('mac', '')
    link_login = request.GET.get('link-login', '')
    link_orig = request.GET.get('link-orig', '')

    reseller = _get_reseller_from_serial(serial)
    branding = reseller.branding if reseller else {}
    template_name = branding.get('template', 'modern')

    countries = Country.objects.filter(is_active=True).order_by('sort_order', 'name')

    return render(request, f'portal/{template_name}/signup.html', {
        'reseller': reseller,
        'branding': branding,
        'serial': serial,
        'mac': mac,
        'link_login': link_login,
        'link_orig': link_orig,
        'countries': countries,
    })


def portal_account_page(request):
    """Subscriber self-service portal — view plan, change PIN, switch plan."""
    serial = request.GET.get('r', '')
    reseller = _get_reseller_from_serial(serial)
    branding = reseller.branding if reseller else {}
    template_name = branding.get('template', 'modern')

    return render(request, f'portal/{template_name}/account.html', {
        'reseller': reseller,
        'branding': branding,
        'serial': serial,
    })
