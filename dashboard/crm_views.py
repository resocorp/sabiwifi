"""
Reseller dashboard — CRM pages (Inbox, Leads, Tickets, Staff).

These are thin server-rendered shells; the actual data is fetched client-side
from the JSON APIs in conversations/, leads/, tickets/, staff/. Keeps the
dashboard templates fast to iterate on and means the same API serves both the
web UI and (later) the mobile app.
"""
from django.shortcuts import render

from accounts.permissions import require_cap


@require_cap('inbox_customer', 'inbox_field', 'inbox_field_own')
def inbox(request):
    return render(request, 'dashboard/inbox.html', {
        'active_tab': 'inbox',
        'reseller': request.effective_reseller,
    })


@require_cap('leads')
def leads(request):
    from plans.models import ServicePlan
    reseller = request.effective_reseller
    plans = ServicePlan.objects.filter(reseller=reseller, is_active=True)
    return render(request, 'dashboard/leads.html', {
        'active_tab': 'leads', 'reseller': reseller, 'plans': plans,
    })


@require_cap('tickets', 'tickets_read', 'tickets_own')
def tickets(request):
    return render(request, 'dashboard/tickets.html', {
        'active_tab': 'tickets',
        'reseller': request.effective_reseller,
    })


@require_cap('staff_crud', 'staff_read')
def staff(request):
    return render(request, 'dashboard/staff.html', {
        'active_tab': 'staff',
        'reseller': request.effective_reseller,
    })
