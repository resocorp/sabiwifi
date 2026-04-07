"""Server-rendered views for reseller signup, login, and landing page."""
from django.shortcuts import render, redirect
from django.contrib.auth import login, authenticate
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from accounts.models import Reseller
from accounts.serializers import ResellerSignupSerializer
import re


def landing_page(request):
    """Public landing page — sales page for potential resellers."""
    if request.user.is_authenticated and hasattr(request.user, 'reseller'):
        return redirect('dashboard-overview')
    from operator_panel.models import PlatformSettings
    settings_obj = PlatformSettings.load()
    # Use first phone in notification_phones as WA contact (stripped of non-digits)
    wa_number = ''
    phones = settings_obj.notification_phones or []
    if phones:
        wa_number = re.sub(r'[^\d]', '', str(phones[0]))
    return render(request, 'public/landing.html', {'wa_number': wa_number})


def signup_page(request):
    """One-step reseller signup form."""
    if request.user.is_authenticated and hasattr(request.user, 'reseller'):
        return redirect('dashboard-overview')

    errors = {}
    form_data = {}

    if request.method == 'POST':
        form_data = {
            'business_name': request.POST.get('business_name', '').strip(),
            'owner_name': request.POST.get('owner_name', '').strip(),
            'email': request.POST.get('email', '').strip(),
            'phone': request.POST.get('phone', '').strip(),
            'password': request.POST.get('password', ''),
        }

        serializer = ResellerSignupSerializer(data=form_data)
        if serializer.is_valid():
            reseller = serializer.save()
            login(request, reseller.user)
            return redirect('dashboard-overview')
        else:
            errors = serializer.errors

    return render(request, 'registration/signup.html', {
        'errors': errors,
        'form_data': form_data,
    })


def login_page(request):
    """Reseller login form."""
    if request.user.is_authenticated and hasattr(request.user, 'reseller'):
        return redirect('dashboard-overview')

    errors = {}
    email = ''

    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        password = request.POST.get('password', '')

        user = authenticate(request, username=email, password=password)
        if user is not None:
            if hasattr(user, 'reseller'):
                login(request, user)
                next_url = request.GET.get('next', 'dashboard-overview')
                return redirect(next_url)
            else:
                errors['non_field_errors'] = ['No reseller account found for this email.']
        else:
            errors['non_field_errors'] = ['Invalid email or password.']

    return render(request, 'registration/login.html', {
        'errors': errors,
        'email': email,
    })
