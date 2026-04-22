"""
Staff directory API — reseller manages their own team (field techs,
care agents, dispatchers). Platform operators manage platform-level
staff separately via the admin.

Login provisioning: when a staff record has `can_log_in=True` and an email,
we attach a Django auth.User so the staff member can sign in. Setting
`can_log_in=False` later deactivates the User but preserves audit data.
"""
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import can, effective_reseller
from staff.models import StaffMember


def _serialise(sm):
    return {
        'id': sm.pk,
        'name': sm.name,
        'role': sm.role,
        'role_label': sm.get_role_display(),
        'phone': sm.phone,
        'whatsapp': sm.whatsapp,
        'email': sm.email,
        'coverage_areas': sm.coverage_areas,
        'shift_hours': sm.shift_hours,
        'active': sm.active,
        'current_load': sm.current_load,
        'notes': sm.notes,
        'can_log_in': sm.can_log_in,
        'has_user': bool(sm.user_id),
    }


def _require(request, *caps):
    if not any(can(request.user, c) for c in caps):
        raise PermissionDenied()


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def staff_list(request):
    _require(request, 'staff_crud', 'staff_read')
    reseller = effective_reseller(request.user)
    qs = StaffMember.objects.filter(reseller=reseller)
    role = request.GET.get('role')
    if role:
        qs = qs.filter(role=role)
    if request.GET.get('active_only') == '1':
        qs = qs.filter(active=True)
    return Response([_serialise(s) for s in qs])


def _apply_fields(sm, data):
    for field in ('name', 'role', 'phone', 'whatsapp', 'email', 'notes'):
        if field in data:
            setattr(sm, field, data[field] or '')
    for field in ('coverage_areas', 'shift_hours'):
        if field in data:
            setattr(sm, field, data[field])
    if 'active' in data:
        sm.active = bool(data['active'])
    if 'can_log_in' in data:
        sm.can_log_in = bool(data['can_log_in'])


def _sync_login_user(sm, *, password=None):
    """Create / activate / deactivate the auth.User backing this staff record.

    - If can_log_in=True and email is set: ensure a User exists (username=email)
      and is active. If `password` is provided, set it.
    - If can_log_in=False: deactivate the linked User (preserves audit history).
    """
    if sm.can_log_in:
        if not sm.email:
            return  # silently skip — caller validates email upstream
        user = sm.user
        if user is None:
            user = User.objects.filter(username=sm.email).first()
            if user is None:
                user = User.objects.create_user(username=sm.email, email=sm.email)
            sm.user = user
        else:
            # Keep username in sync with the staff email
            if user.username != sm.email:
                user.username = sm.email
                user.email = sm.email
        user.is_active = True
        if password:
            user.set_password(password)
        user.save()
        sm.save(update_fields=['user'])
    else:
        if sm.user_id:
            sm.user.is_active = False
            sm.user.save(update_fields=['is_active'])


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def staff_create(request):
    _require(request, 'staff_crud')
    reseller = effective_reseller(request.user)
    data = request.data
    if not data.get('name') or not data.get('phone') or not data.get('role'):
        return Response({'error': 'name, phone, role required'}, status=400)
    valid_roles = {r[0] for r in StaffMember.ROLE_CHOICES}
    if data['role'] not in valid_roles:
        return Response({'error': 'Invalid role'}, status=400)

    if data.get('can_log_in'):
        if not data.get('email'):
            return Response({'error': 'email required when can_log_in is true'},
                            status=400)
        if not data.get('password'):
            return Response({'error': 'password required when can_log_in is true'},
                            status=400)

    with transaction.atomic():
        sm = StaffMember(reseller=reseller)
        _apply_fields(sm, data)
        sm.save()
        _sync_login_user(sm, password=data.get('password'))

    return Response(_serialise(sm))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def staff_update(request, pk):
    _require(request, 'staff_crud')
    reseller = effective_reseller(request.user)
    sm = get_object_or_404(StaffMember, pk=pk, reseller=reseller)
    new_password = request.data.get('password')

    with transaction.atomic():
        _apply_fields(sm, request.data)
        sm.save()
        # Only sync user if can_log_in is in the payload OR a new password was provided
        if 'can_log_in' in request.data or new_password:
            _sync_login_user(sm, password=new_password)

    return Response(_serialise(sm))


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def staff_delete(request, pk):
    _require(request, 'staff_crud')
    reseller = effective_reseller(request.user)
    sm = get_object_or_404(StaffMember, pk=pk, reseller=reseller)
    # Deactivate the login first (preserve audit history), then delete record.
    if sm.user_id:
        sm.user.is_active = False
        sm.user.save(update_fields=['is_active'])
    sm.delete()
    return Response({'ok': True})
