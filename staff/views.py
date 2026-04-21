"""
Staff directory API — reseller manages their own team (field techs,
care agents, dispatchers). Platform operators manage platform-level
staff separately via the admin.
"""
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

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
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def staff_list(request):
    reseller = request.user.reseller
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


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def staff_create(request):
    reseller = request.user.reseller
    data = request.data
    if not data.get('name') or not data.get('phone') or not data.get('role'):
        return Response({'error': 'name, phone, role required'}, status=400)
    valid_roles = {r[0] for r in StaffMember.ROLE_CHOICES}
    if data['role'] not in valid_roles:
        return Response({'error': 'Invalid role'}, status=400)
    sm = StaffMember(reseller=reseller)
    _apply_fields(sm, data)
    sm.save()
    return Response(_serialise(sm))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def staff_update(request, pk):
    reseller = request.user.reseller
    sm = get_object_or_404(StaffMember, pk=pk, reseller=reseller)
    _apply_fields(sm, request.data)
    sm.save()
    return Response(_serialise(sm))


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def staff_delete(request, pk):
    reseller = request.user.reseller
    StaffMember.objects.filter(pk=pk, reseller=reseller).delete()
    return Response({'ok': True})
