"""
Reseller dashboard — CRM pages (Inbox, Leads, Tickets, Staff).

These are thin server-rendered shells; the actual data is fetched client-side
from the JSON APIs in conversations/, leads/, tickets/, staff/. Keeps the
dashboard templates fast to iterate on and means the same API serves both the
web UI and (later) the mobile app.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render


def _reseller_or_redirect(request):
    if not hasattr(request.user, 'reseller'):
        return None, redirect('login')
    return request.user.reseller, None


@login_required
def inbox(request):
    reseller, redir = _reseller_or_redirect(request)
    if redir:
        return redir
    return render(request, 'dashboard/inbox.html', {
        'active_tab': 'inbox', 'reseller': reseller,
    })


@login_required
def leads(request):
    reseller, redir = _reseller_or_redirect(request)
    if redir:
        return redir
    from plans.models import ServicePlan
    plans = ServicePlan.objects.filter(reseller=reseller, is_active=True)
    return render(request, 'dashboard/leads.html', {
        'active_tab': 'leads', 'reseller': reseller, 'plans': plans,
    })


@login_required
def tickets(request):
    reseller, redir = _reseller_or_redirect(request)
    if redir:
        return redir
    return render(request, 'dashboard/tickets.html', {
        'active_tab': 'tickets', 'reseller': reseller,
    })


@login_required
def staff(request):
    reseller, redir = _reseller_or_redirect(request)
    if redir:
        return redir
    return render(request, 'dashboard/staff.html', {
        'active_tab': 'staff', 'reseller': reseller,
    })
