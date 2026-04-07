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
    """Captive portal login page — shows reseller branding.

    Supports both MikroTik params (r, mac, link-login, link-orig) and
    UAM/uspot params (nasid, uamip, uamport, challenge, sessionid, userurl).
    """
    # MikroTik-style params
    serial = request.GET.get('r', '')
    mac = request.GET.get('mac', '')
    link_login = request.GET.get('link-login', '')
    link_orig = request.GET.get('link-orig', '')
    error = request.GET.get('error', '')
    reason = request.GET.get('reason', '')

    # UAM/uspot params — nasid is the router MAC (same as serial_number)
    nasid = request.GET.get('nasid', '')
    uamip = request.GET.get('uamip', '')
    uamport = request.GET.get('uamport', '')
    challenge = request.GET.get('challenge', '')
    sessionid = request.GET.get('sessionid', '')
    userurl = request.GET.get('userurl', '')
    res = request.GET.get('res', '')
    called = request.GET.get('called', '')
    client_ip = request.GET.get('ip', '')
    client_mac = request.GET.get('mac', '')
    uam_secret_md = request.GET.get('md', '')

    # Use nasid as serial if MikroTik serial not provided
    if not serial and nasid:
        serial = nasid

    reseller = _get_reseller_from_serial(serial)
    branding = reseller.branding if reseller else {}
    template_name = branding.get('template', 'modern')

    # Build UAM logon URL for the login form to POST credentials back to the router
    uam_logon_url = ''
    if uamip and uamport:
        uam_logon_url = f'http://{uamip}:{uamport}/logon'

    return render(request, f'portal/{template_name}/login.html', {
        'reseller': reseller,
        'branding': branding,
        'serial': serial,
        'mac': mac or client_mac,
        'link_login': link_login,
        'link_orig': link_orig or userurl,
        'error': error,
        'reason': reason,
        # UAM-specific context
        'uam_mode': bool(uamip),
        'uamip': uamip,
        'uamport': uamport,
        'uam_logon_url': uam_logon_url,
        'challenge': challenge,
        'sessionid': sessionid,
        'userurl': userurl,
        'res': res,
        'called': called,
        'client_ip': client_ip,
    })


def portal_connected(request):
    """Post-authentication success page — shows account info and countdown."""
    serial = request.GET.get('r', '')
    reseller = _get_reseller_from_serial(serial)
    branding = reseller.branding if reseller else {}
    template_name = branding.get('template', 'modern')

    return render(request, f'portal/{template_name}/connected.html', {
        'reseller': reseller,
        'branding': branding,
        'serial': serial,
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
